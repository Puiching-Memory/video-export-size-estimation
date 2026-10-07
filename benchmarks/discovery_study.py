"""Costed development-only study of global preview and rare-content discovery.

Preview preparation and padded encoding costs are counted. Measured probes are
shared between experimental strategies, but each strategy is charged their cold
measurement times. This is a costed replay, not a common-deadline experiment.
"""

import argparse
import copy
import json
import math
import statistics
import subprocess
import time
from pathlib import Path

import numpy as np

from vsize import EncodeSpec
from vsize.context_probe import policy_fingerprint, probe
from vsize.inference import Sampler
from vsize.media import Media
from vsize.runtime import Cache, Deadline, digest


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "results/development-2026-10-02/manifest.json"
REFERENCE_SOURCE = ROOT / "results/core-validation-2026-10-03/components/references.json"
BLOCK_SECONDS = 2
SEED = 20261003


class PreviewDeadline(Deadline):
    """Capture the actual existing Media._visual_features preview for reuse."""

    def run(self, command):
        started = time.monotonic()
        result = super().run(command)
        if "rawvideo" in command and "pipe:1" in command:
            self.preview_raw = result.stdout
            self.preview_command = command
            self.preview_process_wall_seconds = time.monotonic() - started
        return result


def feature_blocks(blocks, raw, fps, *, maximum):
    if len(raw) % (96 * 54):
        raise RuntimeError("incomplete preview frame")
    frames = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 54, 96)
    spatial = [[] for _ in blocks]
    temporal = [[] for _ in blocks]
    previous = None
    ends = np.array([b["start"] + b["duration"] for b in blocks])
    for index, frame in enumerate(frames):
        block = min(len(blocks) - 1, int(np.searchsorted(ends, index / fps, side="right")))
        plane = frame.astype(np.int16)
        spatial[block].append(
            float(np.abs(np.diff(plane, axis=0)).mean() + np.abs(np.diff(plane, axis=1)).mean())
        )
        temporal[block].append(
            float(np.abs(plane - previous).mean()) if previous is not None else 0
        )
        previous = plane
    result = copy.deepcopy(blocks)
    matrix = []
    for block, texture, change in zip(result, spatial, temporal):
        values = {
            "texture_mean": statistics.fmean(texture) if texture else 0,
            "change_mean": statistics.fmean(change) if change else 0,
            "texture_max": max(texture) if texture else 0,
            "change_max": max(change) if change else 0,
            "preview_frames": len(texture),
        }
        block.update(values)
        selected = [values["texture_mean"], values["change_mean"]]
        if maximum:
            selected += [values["texture_max"], values["change_max"]]
        matrix.append([math.log1p(value) for value in selected])
    features = np.asarray(matrix)
    spread = features.std(axis=0)
    normalized = (features - features.mean(axis=0)) / np.maximum(spread, 1e-3)
    for block, vector in zip(result, normalized):
        block["features"] = vector.tolist()
    return result


def preview(media, fps, original):
    started = time.monotonic()
    media.blocks = copy.deepcopy(original)
    if fps == 8:
        media._visual_features(BLOCK_SECONDS)
        raw = media.deadline.preview_raw
        command = media.deadline.preview_command
        process_wall = media.deadline.preview_process_wall_seconds
        mean_blocks = copy.deepcopy(media.blocks)
    else:
        filters = [media.spec.video_filter] if media.spec.video_filter else []
        filters += [f"fps={fps}", "scale=96:54:flags=area", "format=gray"]
        command = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-xerror",
            "-threads",
            "1",
            "-filter_threads",
            "1",
            "-i",
            media.input_path,
            "-map",
            f"0:{media.video['index']}",
            "-vf",
            ",".join(filters),
            "-an",
            "-sn",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "gray",
            "pipe:1",
        ]
        raw = media.deadline.run(command).stdout
        process_wall = media.deadline.preview_process_wall_seconds
        mean_blocks = feature_blocks(original, raw, fps, maximum=False)
    max_blocks = feature_blocks(original, raw, fps, maximum=True)
    return {
        "fps": fps,
        "command": command,
        "decoded_source_seconds": media.duration,
        "output_preview_frames": len(raw) // (96 * 54),
        "output_bytes": len(raw),
        "preview_process_wall_seconds": process_wall,
        "preview_prepare_wall_seconds": time.monotonic() - started,
        "mean_blocks": mean_blocks,
        "max_blocks": max_blocks,
    }


def padded_three_sampler(blocks, uniform=False):
    """Use exactly three pilots when possible, including flat preview cases."""
    sampler = Sampler(blocks, SEED, count=3, uniform=uniform)
    if len(sampler.pilots) >= min(3, len(blocks)):
        return sampler
    extras = Sampler(blocks, SEED, count=3, uniform=True).pilots
    for index in extras + list(range(len(blocks))):
        if index not in sampler.pilots and len(sampler.pilots) < min(3, len(blocks)):
            sampler.pilots.append(index)
    vectors = [np.array(b["features"]) for b in blocks]
    sampler.groups = [[] for _ in sampler.pilots]
    for index, vector in enumerate(vectors):
        nearest = min(
            range(len(sampler.pilots)),
            key=lambda j: (
                float(np.square(vector - vectors[sampler.pilots[j]]).sum()),
                abs(index - sampler.pilots[j]),
            ),
        )
        sampler.groups[nearest].append(index)
    remaining = [i for i in range(len(blocks)) if i not in sampler.pilots]
    import random

    random.Random(SEED).shuffle(remaining)
    sampler.audit_order = remaining
    sampler.order = sampler.pilots + remaining
    return sampler


def strategy_estimates(name, sampler, measured, initialization_wall, preparation_wall, max_audits):
    outputs = []
    for count in range(3, min(len(sampler.order), 3 + max_audits) + 1):
        indices = sampler.order[:count]
        raw = copy.deepcopy(sampler)
        periodic = copy.deepcopy(sampler)
        for index in indices:
            record = measured[index]
            raw.add(index, record)
            periodic.add(
                index, {**record, "payload_bytes": record["periodic_adjusted_payload_bytes"]}
            )
        sei = statistics.fmean(measured[index]["global_init_sei_bytes"] for index in indices)
        raw_estimate = raw.estimate()
        periodic_estimate = periodic.estimate()
        probe_wall = sum(measured[index]["cold_measurement_wall_seconds"] for index in indices)
        outputs.append(
            {
                "method": name,
                "pilot_indices": sampler.pilots,
                "strata": sampler.groups,
                "audit_indices": indices[3:],
                "sample_count": count,
                "raw_payload_estimate": raw_estimate["estimated_payload_bytes"] + sei,
                "periodic_payload_estimate": periodic_estimate["estimated_payload_bytes"] + sei,
                "raw_file_estimate_legacy_mux": raw_estimate["estimated_bytes"] + sei,
                "periodic_file_estimate_legacy_mux": periodic_estimate["estimated_bytes"] + sei,
                "legacy_mux_bytes": raw_estimate["fixed_bytes"],
                "global_init_sei_once": sei,
                "initialization_wall_seconds": initialization_wall,
                "preview_prepare_wall_seconds": preparation_wall,
                "probe_measurement_wall_seconds": probe_wall,
                "costed_total_wall_seconds": initialization_wall + preparation_wall + probe_wall,
                "encoded_media_seconds": sum(
                    measured[index]["encoded_media_seconds"] for index in indices
                ),
                "encoded_fraction": sum(
                    measured[index]["encoded_media_seconds"] for index in indices
                )
                / sum(b["duration"] for b in sampler.blocks),
                "sampling_standard_error_bytes": raw_estimate["sampling_standard_error_bytes"],
                "interval_status": "uncalibrated; discovery does not certify context/mux error",
            }
        )
    return outputs


def aliasing_diagnostic(output):
    """Sample phases of a half-second interval inside real development noise.

    This is a temporal phase test on real decoded frames, not an additional
    accuracy video. The hypothetical burst is only the active interval [10,10.5).
    Other frames are assigned the source's actual uniform background texture.
    """
    source = ROOT / "artifacts/corpus-v1/brief-burst.mp4"
    command = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-threads",
        "1",
        "-filter_threads",
        "1",
        "-i",
        str(source),
        "-vf",
        "fps=24,scale=96:54:flags=area,format=gray",
        "-an",
        "-f",
        "rawvideo",
        "-pix_fmt",
        "gray",
        "pipe:1",
    ]
    started = time.monotonic()
    raw = subprocess.check_output(command)
    frames = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 54, 96)
    textures = []
    for frame in frames:
        plane = frame.astype(np.int16)
        textures.append(
            float(np.abs(np.diff(plane, axis=0)).mean() + np.abs(np.diff(plane, axis=1)).mean())
        )
    background = statistics.median(textures[:24])
    threshold = background + 0.25 * (max(textures[240:252]) - background)
    rows = []
    for fps in [1, 2, 4, 8]:
        detected = []
        for phase in range(24):
            offset = phase / 24 / fps
            times = np.arange(offset, len(frames) / 24, 1 / fps)
            sampled = [
                textures[min(len(textures) - 1, int(t * 24))] if 10 <= t < 10.5 else background
                for t in times
            ]
            detected.append(max(sampled) > threshold)
        rows.append(
            {
                "fps": fps,
                "phase_count": 24,
                "detected_phases": sum(detected),
                "missed_phases": len(detected) - sum(detected),
            }
        )
    result = {
        "scope": "phase aliasing diagnostic using real decoded development pixels; not video accuracy",
        "source": str(source),
        "source_sha256": digest(source),
        "command": command,
        "active_interval": [10, 10.5],
        "background_texture": background,
        "threshold_texture": threshold,
        "diagnostic_prepare_wall_seconds": time.monotonic() - started,
        "rows": rows,
        "limitation": "Any fixed finite FPS can miss shorter events; maxima cannot recover absent frames.",
    }
    (output / "half-second-phase-diagnostic.json").write_text(json.dumps(result, indent=2))
    return result


def study(args):
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "predictions.jsonl").write_text("")
    dataset = json.loads(MANIFEST.read_text())
    if dataset["split"] != "development":
        raise ValueError("only frozen development cases are allowed")
    chosen = set(args.cases.split(",")) if args.cases else {c["id"] for c in dataset["cases"]}
    cases = [c for c in dataset["cases"] if c["id"] in chosen]
    if len(cases) != len(chosen):
        raise ValueError("all requested cases must be in the frozen development manifest")
    prediction_rows = []
    metadata = {
        "schema_version": 1,
        "scope": "development_only_costed_replay",
        "manifest_sha256": digest(MANIFEST),
        "script_sha256": digest(Path(__file__)),
        "threads": 1,
        "crf": 23,
        "preset": "medium",
        "audio": "dropped; corpus has no audio",
        "rates": [1, 2, 4, 8],
        "preview_resolution": [96, 54],
        "block_seconds": BLOCK_SECONDS,
        "pilots": 3,
        "audits": args.audits,
        "seed": SEED,
        "cost_accounting": "per-method cold probe times are charged even if experiments reuse measurements",
        "ffmpeg": subprocess.check_output(["ffmpeg", "-version"], text=True).splitlines()[0],
    }
    (args.output / "metadata.json").write_text(json.dumps(metadata, indent=2))
    for case in cases:
        if digest(Path(case["source"])) != case["sha256"]:
            raise ValueError("development source changed")
        initialized = time.monotonic()
        deadline = PreviewDeadline(1200)
        media = Media(
            EncodeSpec(Path(case["source"]), drop_audio=True),
            1,
            Cache(args.output / ".vsize-cache" / case["id"]),
            deadline,
        )
        original = media.metadata_blocks(BLOCK_SECONDS)
        initialization_wall = time.monotonic() - initialized
        previews = {fps: preview(media, fps, original) for fps in [1, 2, 4, 8]}
        samplers = {"uniform3": (Sampler(copy.deepcopy(original), SEED, count=3, uniform=True), 0)}
        for fps in [4, 8]:
            samplers[f"mean-{fps}fps"] = (
                padded_three_sampler(previews[fps]["mean_blocks"]),
                previews[fps]["preview_prepare_wall_seconds"],
            )
        for fps in [1, 2, 4, 8]:
            samplers[f"mean-max-{fps}fps"] = (
                padded_three_sampler(previews[fps]["max_blocks"]),
                previews[fps]["preview_prepare_wall_seconds"],
            )
        required = sorted(
            {i for sampler, _ in samplers.values() for i in sampler.order[: 3 + args.audits]}
        )
        measured = {}
        for index in required:
            path = args.output / "measurements" / f"{case['id']}-{index:03}.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists():
                record = json.loads(path.read_text())
                if record["source_hash"] != media.source_hash:
                    raise ValueError("cached development measurement has wrong source")
                if record["policy_fingerprint"] != policy_fingerprint(media):
                    record = None
                elif media.cache.lookup(record["warmup_record_key"], media.deadline) is None:
                    record = None
            else:
                record = None
            if record is None:
                block = original[index]
                cold = time.monotonic()
                record = probe(media, 23, (block["start"], block["duration"]))
                record["cold_measurement_wall_seconds"] = time.monotonic() - cold
                record["block_index"] = index
                path.write_text(json.dumps(record, indent=2))
            measured[index] = record
            print(json.dumps({"case": case["id"], "measured_block": index}), flush=True)
        rows = []
        for name, (sampler, preparation) in samplers.items():
            rows += strategy_estimates(
                name, sampler, measured, initialization_wall, preparation, args.audits
            )
        rows = [{"case": case["id"], "group_id": case["group_id"], **row} for row in rows]
        prediction_rows += rows
        (args.output / f"{case['id']}-previews.json").write_text(json.dumps(previews, indent=2))
        with (args.output / "predictions.jsonl").open("a") as stream:
            for row in rows:
                stream.write(json.dumps(row) + "\n")
        media.verify_source()
    # Ground truth is only opened after all candidate predictions are persisted.
    references = json.loads(REFERENCE_SOURCE.read_text())
    for case in cases:
        reference = references[case["id"]]
        if reference["source_hash"] != case["sha256"]:
            raise ValueError("reference source mismatch")
        if digest(Path(reference["artifact"])) != reference["sha256"]:
            raise ValueError("reference artifact changed")
    evaluated = []
    for row in prediction_rows:
        reference = references[row["case"]]
        evaluated.append(
            {
                **row,
                "reference_payload_bytes": reference["payload_bytes"],
                "reference_file_bytes": reference["file_bytes"],
                "reference_encode_wall_seconds": reference["encode_wall_seconds"],
                "reference_transcode_wall_seconds": reference["transcode_wall_seconds"],
                "raw_payload_relative_error": row["raw_payload_estimate"]
                / reference["payload_bytes"]
                - 1,
                "periodic_payload_relative_error": row["periodic_payload_estimate"]
                / reference["payload_bytes"]
                - 1,
                "raw_file_relative_error_legacy_mux": row["raw_file_estimate_legacy_mux"]
                / reference["file_bytes"]
                - 1,
                "periodic_file_relative_error_legacy_mux": row["periodic_file_estimate_legacy_mux"]
                / reference["file_bytes"]
                - 1,
            }
        )
    (args.output / "evaluated.json").write_text(json.dumps(evaluated, indent=2))
    summary = []
    for method in samplers:
        for count in range(3, 4 + args.audits):
            subset = [r for r in evaluated if r["method"] == method and r["sample_count"] == count]
            if not subset:
                continue
            summary.append(
                {
                    "method": method,
                    "sample_count": count,
                    "cases": len(subset),
                    "raw_payload_median_absolute_relative_error": statistics.median(
                        abs(r["raw_payload_relative_error"]) for r in subset
                    ),
                    "raw_payload_max_absolute_relative_error": max(
                        abs(r["raw_payload_relative_error"]) for r in subset
                    ),
                    "periodic_payload_median_absolute_relative_error": statistics.median(
                        abs(r["periodic_payload_relative_error"]) for r in subset
                    ),
                    "median_total_wall_seconds": statistics.median(
                        r["costed_total_wall_seconds"] for r in subset
                    ),
                    "median_encoded_fraction": statistics.median(
                        r["encoded_fraction"] for r in subset
                    ),
                }
            )
    diagnostic = aliasing_diagnostic(args.output)
    (args.output / "summary.json").write_text(
        json.dumps(
            {"metadata": metadata, "methods": summary, "phase_diagnostic": diagnostic}, indent=2
        )
    )
    print(json.dumps(summary), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "results/discovery-study-2026-10-03")
    parser.add_argument("--cases")
    parser.add_argument("--audits", type=int, default=3)
    args = parser.parse_args()
    if args.audits < 0:
        parser.error("audits must be nonnegative")
    study(args)


if __name__ == "__main__":
    main()
