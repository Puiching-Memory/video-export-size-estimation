"""Reproducible core-development measurements, with explicit target semantics.

Only the nine already-opened development sources are accepted. This runner is
not a calibration or untouched generalization benchmark. Predictions are saved
before their complete reference is generated or read. A padded encode's entire
duration counts as work; a complete block census is never called low budget.
"""

import argparse
import hashlib
import json
import re
import shutil
import statistics
import subprocess
import threading
import time
from dataclasses import asdict
from pathlib import Path

from vsize import ComputeBudget, EncodeSpec, Request, SizeConstraint
from vsize.assets import PreparedAsset
from vsize.context_probe import context_window, probe
from vsize.inference import Sampler
from vsize.media import Media
from vsize.runtime import BudgetExhausted, Cache, Deadline, digest


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "results/development-2026-10-02/manifest.json"
OUTPUT = ROOT / "results/core-validation-2026-10-03"
SEED = 20261003
ALLOWED_CASES = {
    "static",
    "motion",
    "grain",
    "brief-burst",
    "cbr-burst",
    "alternating",
    "screen",
    "pedestrians",
    "megamind",
}


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, allow_nan=False) + "\n")


def append(path, value):
    with path.open("a") as stream:
        stream.write(json.dumps(value, allow_nan=False) + "\n")
    print(json.dumps(value, allow_nan=False), flush=True)


def metadata(stage, cases):
    value = hashlib.sha256()
    for path in sorted((ROOT / "src").rglob("*.py")):
        value.update(path.relative_to(ROOT).as_posix().encode())
        value.update(path.read_bytes())
    return {
        "schema_version": 1,
        "stage": stage,
        "scope": "development_diagnostic_not_independent_validation",
        "source_code_sha256": value.hexdigest(),
        "runner_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "cases": cases,
        "contract": {
            "codec": "libx264",
            "crf": 23,
            "preset": "medium",
            "pixel_format": "yuv420p",
            "threads": 1,
            "filter_threads": 1,
            "audio": "dropped; frozen development corpus has no audio",
            "continuous_target": "ordinary complete MP4 export",
            "segmented_target": "explicit independent closed-GOP fMP4; duration in stage configuration",
        },
        "ffmpeg": subprocess.check_output(["ffmpeg", "-version"], text=True).splitlines()[0],
        "ordering": "predictions persisted before complete labels are generated/read",
        "cpu_max": Path("/sys/fs/cgroup/cpu.max").read_text().strip()
        if Path("/sys/fs/cgroup/cpu.max").is_file()
        else None,
        "latency_scope": "shared cloud cgroup; OS page cache not flushed; one FFmpeg per runner",
    }


def cases_selected(names):
    manifest = json.loads(MANIFEST.read_text())
    if manifest["split"] != "development":
        raise ValueError("only the frozen development split is authorized here")
    cases = [case for case in manifest["cases"] if not names or case["id"] in names]
    if names and {case["id"] for case in cases} != set(names):
        raise ValueError("unknown requested development case")
    for case in cases:
        if case["id"] not in ALLOWED_CASES or "industrial-v1" in case["source"]:
            raise ValueError("reserved industrial accuracy cannot be used for development")
        if digest(Path(case["source"])) != case["sha256"]:
            raise ValueError(f"source checksum changed: {case['id']}")
    return cases


def directory(output, stage, cases):
    dest = output / stage
    dest.mkdir(parents=True, exist_ok=False)
    save(dest / "metadata.json", metadata(stage, cases))
    shutil.copytree(
        ROOT / "src",
        dest / ".vsize-cache" / "code_snapshot" / "src",
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    shutil.copyfile(Path(__file__), dest / ".vsize-cache" / "code_snapshot" / "core_validation.py")
    shutil.copyfile(
        ROOT / "pyproject.toml", dest / ".vsize-cache" / "code_snapshot" / "pyproject.toml"
    )
    return dest


def specification(case, **kwargs):
    return EncodeSpec(Path(case["source"]), drop_audio=True, **kwargs)


def continuous_reference(case, dest):
    deadline = Deadline(180)
    media = Media(specification(case), 1, Cache(dest / ".vsize-cache" / case["id"]), deadline)
    record = media.encode(23)
    return {**record, "reference_wall_seconds": deadline.elapsed, "target": "continuous_mp4"}


def components(cases, output, prefixes):
    dest = directory(output, "components", cases)
    rows = []
    timing = {}
    for case in cases:
        spec = specification(case)
        # This isolated metadata measurement includes a real full input digest.
        cold = Deadline(60)
        cold_media = Media(spec, 1, Cache(dest / ".vsize-cache" / "cold" / case["id"]), cold)
        cold_metadata_seconds = cold.elapsed
        del cold_media
        with PreparedAsset.from_path(spec.source) as asset:
            timing[case["id"]] = {
                "source_bytes": asset.size_bytes,
                "prepared_ingest_seconds": asset.ingest_seconds,
                "cold_metadata_with_digest_seconds": cold_metadata_seconds,
            }
            for kind in ["bare", "context"]:
                deadline = Deadline(180)
                media = Media(
                    spec,
                    1,
                    Cache(dest / ".vsize-cache" / kind / case["id"]),
                    deadline,
                    asset=asset,
                )
                metadata_seconds = deadline.elapsed
                blocks = media.metadata_blocks(2)
                sampler = Sampler(blocks, SEED, uniform=True)
                adjusted_sampler = Sampler(blocks, SEED, uniform=True)
                measurements = []
                file_rates = []
                encoded_seconds = 0.0
                requested_seconds = 0.0
                for position, index in enumerate(sampler.order[: max(prefixes)], 1):
                    block = blocks[index]
                    window = (block["start"], block["duration"])
                    if kind == "context":
                        plan = context_window(media, window)
                        requested_seconds += plan["encoded_media_seconds"]
                        measured = probe(media, 23, window)
                        encoded_seconds += measured["encoded_media_seconds"]
                        adjusted_sampler.add(
                            index,
                            {
                                **measured,
                                "payload_bytes": measured["periodic_adjusted_payload_bytes"],
                            },
                        )
                    else:
                        requested_seconds += window[1]
                        measured = media.encode(23, window)
                        encoded_seconds += measured["duration"]
                        file_rates.append(measured["file_bytes"] / measured["duration"])
                    sampler.add(index, measured)
                    measurements.append({"block_index": index, **measured})
                    if position not in prefixes:
                        continue
                    candidate = sampler.estimate()
                    common = {
                        "case": case["id"],
                        "prefix": position,
                        "duration": media.duration,
                        "warm_request_wall_seconds": deadline.elapsed,
                        "cold_ingest_plus_request_seconds": asset.ingest_seconds + deadline.elapsed,
                        "metadata_seconds": metadata_seconds,
                        "encoded_media_seconds": encoded_seconds,
                        "encoded_media_fraction": encoded_seconds / media.duration,
                        "requested_encode_seconds": requested_seconds,
                        "sampled_central_seconds": candidate["sampled_seconds"],
                        "sampling_census": position == len(blocks),
                        "source_blocks": len(blocks),
                        "uncertainty_kind": "uncalibrated",
                        "target": "continuous_mp4",
                    }
                    fixed_sei = measured.get("global_init_sei_bytes", 0)
                    ordinary_overhead = (
                        2048
                        + 6
                        * sum(r["video_frames"] for r in sampler.observed.values())
                        / candidate["sampled_seconds"]
                        * media.duration
                    )
                    results = [
                        (
                            "bare-payload-model" if kind == "bare" else "context-raw",
                            candidate["estimated_bytes"] + fixed_sei,
                            candidate["estimated_bytes"] - ordinary_overhead + fixed_sei,
                        )
                    ]
                    if kind == "bare":
                        results.append(
                            (
                                "bare-file-rate",
                                round(statistics.fmean(file_rates) * media.duration),
                                None,
                            )
                        )
                    else:
                        periodic = adjusted_sampler.estimate()["estimated_bytes"]
                        results.append(
                            (
                                "context-periodic-hypothesis",
                                periodic + fixed_sei,
                                periodic - ordinary_overhead + fixed_sei,
                            )
                        )
                    for method, prediction, payload in results:
                        row = {
                            **common,
                            "method": method,
                            "prediction_bytes": prediction,
                            "prediction_payload_bytes": payload,
                        }
                        append(dest / "predictions.jsonl", row)
                        rows.append(row)
                save(dest / "measurements" / f"{case['id']}-{kind}.json", measurements)
    save(dest / "timing.json", timing)
    # No full labels are consumed by the prediction loop above.
    references = {case["id"]: continuous_reference(case, dest) for case in cases}
    save(dest / "references.json", references)
    evaluate(rows, references, dest)


def evaluate(rows, references, dest):
    for row in rows:
        reference = references[row["case"]]
        row["actual_bytes"] = reference["file_bytes"]
        prediction = row.get("prediction_bytes")
        if prediction is not None:
            signed = 100 * (prediction / reference["file_bytes"] - 1)
            row["signed_error_percent"] = signed
            row["absolute_error_percent"] = abs(signed)
        if row.get("prediction_payload_bytes") is not None:
            row["signed_payload_error_percent"] = 100 * (
                row["prediction_payload_bytes"] / reference["payload_bytes"] - 1
            )
        row["wall_vs_full_reference"] = (
            row.get("warm_request_wall_seconds", row.get("wall_seconds", 0))
            / reference["reference_wall_seconds"]
        )
    save(dest / "evaluated.json", rows)
    groups = {}
    for row in rows:
        key = (
            row["method"],
            row.get("prefix"),
            row.get("budget_seconds"),
            row.get("max_encode_fraction"),
            row.get("lifecycle"),
        )
        groups.setdefault(key, []).append(row)
    summary = []
    for key, group in groups.items():
        successful = [row for row in group if row.get("prediction_bytes") is not None]
        errors = [row["absolute_error_percent"] for row in successful]
        summary.append(
            {
                "method": key[0],
                "prefix": key[1],
                "budget_seconds": key[2],
                "max_encode_fraction": key[3],
                "lifecycle": key[4],
                "cases": len(group),
                "estimates": len(successful),
                "no_estimate_rate": 1 - len(successful) / len(group),
                "within_10_percent_of_all_cases": sum(error <= 10 for error in errors) / len(group),
                "median_ape_percent": statistics.median(errors) if errors else None,
                "worst_ape_percent": max(errors) if errors else None,
                "median_encoded_media_fraction": statistics.median(
                    row["encoded_media_fraction"] for row in group
                ),
                "median_wall_seconds": statistics.median(
                    row.get("warm_request_wall_seconds", row.get("wall_seconds", 0))
                    for row in group
                ),
                "exact_estimates": sum(
                    row.get("uncertainty_kind") == "exact" for row in successful
                ),
                "certified_satisfied": sum(row.get("state") == "satisfied" for row in group),
            }
        )
    save(dest / "summary.json", summary)


def segmented_reference(case, dest, segment_seconds):
    from vsize.fmp4 import write_fragmented_mp4
    from vsize.segmented_media import SegmentedMedia

    deadline = Deadline(180)
    media = Media(
        specification(case, export_mode="segmented", segment_seconds=segment_seconds),
        1,
        Cache(dest / ".vsize-cache" / case["id"]),
        deadline,
    )
    adapter = SegmentedMedia(media, segment_seconds=segment_seconds)
    adapter.prepare()
    segments = []
    for segment in adapter.segments:
        record = adapter.encode_segment(segment.index, 23)
        track, packets = adapter.read_segment(record)
        segments.append(packets)
    path = dest / ".vsize-cache" / case["id"] / "complete-segmented-reference.mp4"
    accounting = write_fragmented_mp4(path, track, segments, deadline=deadline)
    adapter.verify_source()
    deadline.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-xerror",
            "-threads",
            "1",
            "-i",
            str(path),
            "-an",
            "-f",
            "null",
            "-",
        ]
    )
    if path.stat().st_size != accounting.total_bytes:
        raise AssertionError("reference byte accounting mismatch")
    return {
        "file_bytes": path.stat().st_size,
        "payload_bytes": accounting.video_payload_bytes,
        "artifact": str(path),
        "sha256": digest(path),
        "accounting": asdict(accounting),
        "reference_wall_seconds": deadline.elapsed,
        "target": "segmented_mp4",
        "segment_seconds": segment_seconds,
        "total_segments": len(segments),
    }


def engine(
    cases,
    output,
    budgets,
    fractions,
    sampling_plan="temporal",
    sample_seconds=2,
    segment_seconds=4,
    *,
    stage="engine",
    probe_mode="auto",
):
    from vsize.engine import Engine

    dest = directory(output, stage, cases)
    save(
        dest / "experiment.json",
        {
            "sampling_plan": sampling_plan,
            "sample_seconds": sample_seconds,
            "segment_seconds": segment_seconds,
            "probe_mode": probe_mode,
            "budgets": budgets,
            "fractions": fractions,
        },
    )
    rows = []
    references = {}
    for case in cases:
        spec = specification(case)
        with PreparedAsset.from_path(spec.source) as asset:
            outside_metadata = Media(
                spec,
                1,
                Cache(dest / ".vsize-cache" / "metadata" / case["id"]),
                Deadline(30),
                asset=asset,
            )
            source_duration = outside_metadata.duration
            for mode in ["continuous", "segmented"]:
                mode_spec = specification(case, export_mode=mode, segment_seconds=segment_seconds)
                lifecycles = ["prepared"] if stage == "fallback" else ["cold-original", "prepared"]
                for lifecycle in lifecycles:
                    for budget in budgets:
                        for fraction in fractions:
                            job = f"{case['id']}-{mode}-{lifecycle}-{budget}-{fraction}"
                            updates = []
                            request = Request(
                                mode_spec,
                                compute=ComputeBudget(
                                    wall_seconds=budget,
                                    max_probes=6,
                                    threads=1,
                                    max_encode_fraction=fraction,
                                ),
                                sample_seconds=sample_seconds
                                if mode == "continuous"
                                else segment_seconds,
                                seed=SEED,
                                sampling_plan=sampling_plan,
                                probe_mode=probe_mode,
                            )
                            controller = Engine(dest / ".vsize-cache" / job)
                            started = time.monotonic()
                            answer = controller.run(
                                request,
                                on_update=updates.append,
                                asset=asset if lifecycle == "prepared" else None,
                            )
                            snapshot = answer["estimate"]
                            row = {
                                "case": case["id"],
                                "method": mode,
                                "lifecycle": lifecycle,
                                "budget_seconds": budget,
                                "max_encode_fraction": fraction,
                                "state": answer["status"],
                                "unmet": answer["unmet"],
                                "prediction_bytes": snapshot["estimated_bytes"]
                                if snapshot
                                else None,
                                "uncertainty_kind": snapshot["uncertainty"]["kind"]
                                if snapshot
                                else None,
                                "wall_seconds": time.monotonic() - started,
                                "first_estimate_seconds": updates[0]["elapsed_seconds"]
                                if updates
                                else None,
                                "first_estimate_reported_seconds": answer["first_estimate_seconds"],
                                "prepared_ingest_seconds": asset.ingest_seconds,
                                "cold_ingest_plus_request_seconds": asset.ingest_seconds
                                + answer["elapsed_seconds"]
                                if lifecycle == "prepared"
                                else None,
                                "encoded_media_seconds": answer["attempted_encode_seconds"],
                                "encoded_media_fraction": answer["attempted_encode_seconds"]
                                / source_duration,
                                "attempted_audio_seconds": answer["attempted_audio_seconds"],
                                "selected_probe_mode": snapshot.get("probe_mode")
                                if snapshot
                                else None,
                                "total_probe_count": answer["total_probe_count"],
                                "pilot_count": answer.get("pilot_count"),
                                "content_discovery": answer.get("content_discovery"),
                                "sampling_plan": sampling_plan,
                                "sample_seconds": sample_seconds
                                if mode == "continuous"
                                else segment_seconds,
                                "target": f"{mode}_mp4",
                            }
                            save(dest / "answers" / f"{job}.json", answer)
                            save(dest / "updates" / f"{job}.json", updates)
                            append(dest / "predictions.jsonl", row)
                            rows.append(row)
    saved_continuous = output / "engine" / "continuous-references.json"
    if stage == "fallback" and saved_continuous.is_file():
        references = json.loads(saved_continuous.read_text())
        for case in cases:
            reference = references[case["id"]]
            if digest(Path(reference["artifact"])) != reference["sha256"]:
                raise RuntimeError("continuous reference changed before reuse")
    else:
        for case in cases:
            references[case["id"]] = continuous_reference(case, dest)
            print(
                json.dumps({"type": "reference", "case": case["id"], "mode": "continuous"}),
                flush=True,
            )
    save(dest / "continuous-references.json", references)
    continuous_rows = [row for row in rows if row["method"] == "continuous"]
    evaluate(continuous_rows, references, dest / "continuous")
    segmented_rows = [row for row in rows if row["method"] == "segmented"]
    segmented_reference_path = (
        output / "engine" / "segmented-references.json"
        if stage == "fallback"
        else output / "segmented" / "references.json"
    )
    if segmented_reference_path.is_file():
        segmented_references = json.loads(segmented_reference_path.read_text())
        for case in cases:
            reference = segmented_references[case["id"]]
            if reference.get("segment_seconds", 4) != segment_seconds:
                raise ValueError("saved segmented references target another segment duration")
            if digest(Path(reference["artifact"])) != reference["sha256"]:
                raise RuntimeError("segmented reference changed before reuse")
    else:
        segmented_references = {}
        for case in cases:
            segmented_references[case["id"]] = segmented_reference(
                case,
                dest / "segmented-references",
                segment_seconds,
            )
            save(dest / "segmented-references.json", segmented_references)
            print(
                json.dumps({"type": "reference", "case": case["id"], "mode": "segmented"}),
                flush=True,
            )
    evaluate(segmented_rows, segmented_references, dest / "segmented")
    save(dest / "evaluated.json", continuous_rows + segmented_rows)


def quality_metrics(source, artifact):
    command = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "info",
        "-nostdin",
        "-xerror",
        "-threads",
        "1",
        "-i",
        str(source),
        "-threads",
        "1",
        "-i",
        str(artifact),
        "-filter_complex_threads",
        "1",
        "-filter_complex",
        "[0:v]setpts=PTS-STARTPTS,split=2[rp][rs];"
        "[1:v]setpts=PTS-STARTPTS,split=2[dp][ds];"
        "[dp][rp]psnr=shortest=1[p];[ds][rs]ssim=shortest=1[s]",
        "-map",
        "[p]",
        "-map",
        "[s]",
        "-an",
        "-threads",
        "1",
        "-f",
        "null",
        "-",
    ]
    result = Deadline(180).run(command)
    output = result.stderr.decode()
    psnr = re.search(r"PSNR .*average:([\d.]+|inf)", output)
    ssim = re.search(r"SSIM .*All:([\d.]+)", output)
    if not psnr or not ssim:
        raise RuntimeError("FFmpeg did not report complete PSNR/SSIM")
    return {
        "psnr_db": None if psnr[1] == "inf" else float(psnr[1]),
        "psnr_infinite": psnr[1] == "inf",
        "ssim": float(ssim[1]),
        "command": command,
    }


def quality(cases, output):
    dest = directory(output, "quality", cases)
    engine_output = output / "engine"
    continuous = json.loads((engine_output / "continuous-references.json").read_text())
    segmented_path = engine_output / "segmented-references.json"
    if not segmented_path.is_file():
        segmented_path = output / "segmented/references.json"
    segmented = json.loads(segmented_path.read_text())
    rows = []
    for case in cases:
        cr, sr = continuous[case["id"]], segmented[case["id"]]
        for record in [cr, sr]:
            if digest(Path(record["artifact"])) != record["sha256"]:
                raise RuntimeError("quality reference changed")
        cq = quality_metrics(case["source"], cr["artifact"])
        sq = quality_metrics(case["source"], sr["artifact"])
        row = {
            "case": case["id"],
            "crf": 23,
            "segment_seconds": sr.get("segment_seconds", 4),
            "continuous_bytes": cr["file_bytes"],
            "segmented_bytes": sr["file_bytes"],
            "segmented_size_change_percent": 100 * (sr["file_bytes"] / cr["file_bytes"] - 1),
            "continuous_quality": cq,
            "segmented_quality": sq,
            "delta_psnr_db": sq["psnr_db"] - cq["psnr_db"]
            if sq["psnr_db"] is not None and cq["psnr_db"] is not None
            else None,
            "delta_ssim": sq["ssim"] - cq["ssim"],
        }
        append(dest / "results.jsonl", row)
        rows.append(row)
    save(dest / "results.json", rows)


def ab_av1(cases, output, binary, budget, sample_seconds=2):
    """Preserve the official raw estimate; do not invent a target normalization."""
    dest = directory(output, "ab-av1", cases)
    rows = []
    for case in cases:
        source = Path(case["source"])
        metadata_deadline = Deadline(30)
        media = Media(
            specification(case), 1, Cache(dest / ".vsize-cache" / "metadata"), metadata_deadline
        )
        temp = dest / ".vsize-cache" / case["id"]
        temp.mkdir(parents=True)
        command = [
            str(binary),
            "sample-encode",
            "-i",
            str(source),
            "--encoder",
            "libx264",
            "--preset",
            "medium",
            "--crf",
            "23",
            "--keyint",
            "250",
            "--pix-format",
            "yuv420p",
            "--enc",
            "threads=1",
            "--enc",
            "filter_threads=1",
            "--enc",
            "fps_mode=passthrough",
            "--enc-input",
            "threads=1",
            "--samples",
            "3",
            "--sample-duration",
            f"{sample_seconds}s",
            "--and-vmaf",
            "false",
            "--vmaf",
            "n_threads=1",
            "--vmaf-scale",
            "none",
            "--cache",
            "false",
            "--keep",
            "--temp-dir",
            str(temp),
            "--stdout-format",
            "json",
        ]
        deadline = Deadline(budget)
        raw, prediction, state = None, None, "completed"
        try:
            if digest(source, deadline) != case["sha256"]:
                raise RuntimeError("source changed before ab-av1")
            result = deadline.run(command)
            raw = json.loads(result.stdout)
            prediction = raw["predicted_encode_size"]
            if digest(source, deadline) != case["sha256"]:
                raise RuntimeError("source changed during ab-av1")
            (dest / f"{case['id']}.log").write_bytes(result.stderr)
        except BudgetExhausted:
            state = "budget_exhausted"
        measured_wall = deadline.elapsed
        samples = []
        for artifact in temp.rglob("*.mp4"):
            inspection = json.loads(
                Deadline(30)
                .run(
                    [
                        "ffprobe",
                        "-v",
                        "error",
                        "-show_streams",
                        "-show_format",
                        "-of",
                        "json",
                        str(artifact),
                    ]
                )
                .stdout
            )
            video = next(
                stream for stream in inspection["streams"] if stream["codec_type"] == "video"
            )
            samples.append(
                {
                    "artifact": str(artifact),
                    "duration": float(video["duration"]),
                    "frames": int(video["nb_frames"]),
                    "file_bytes": artifact.stat().st_size,
                }
            )
        encoded_seconds = sum(sample["duration"] for sample in samples)
        row = {
            "case": case["id"],
            "method": "ab-av1-official-estimate",
            "prefix": 3,
            "budget_seconds": budget,
            "lifecycle": "cold-original",
            "prediction_bytes": prediction,
            "prediction_payload_bytes": prediction,
            "wall_seconds": measured_wall,
            "encoded_media_seconds": encoded_seconds,
            "encoded_media_fraction": encoded_seconds / media.duration,
            "duration": media.duration,
            "sample_seconds": sample_seconds,
            "state": state,
            "uncertainty_kind": "uncalibrated",
            "target": "raw_official_size_estimate_scored_against_both_file_and_payload",
            "first_estimate_seconds": measured_wall if prediction is not None else None,
            "samples": samples,
            "command": command,
            "official_output": raw,
            "target_note": (
                "Log calls this video stream size; kept sample files show container bytes affect "
                "the rate ratio. Main file comparison uses the unmodified official output."
            ),
        }
        append(dest / "predictions.jsonl", row)
        rows.append(row)
    references = {case["id"]: continuous_reference(case, dest) for case in cases}
    save(dest / "references.json", references)
    evaluate(rows, references, dest)


class MemoryMonitor:
    """Sample Linux RSS of this Python process and its subprocess descendants."""

    def __init__(self):
        import os

        self.pid = os.getpid()
        self.peak_bytes = 0
        self.samples = 0
        self.done = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _rss_tree(self, pid):
        import os

        # The managed /proc view does not expose task/<pid>/children. Derive
        # descendants from PPid in the supported stat files instead.
        processes = {}
        for path in Path("/proc").iterdir():
            if not path.name.isdigit():
                continue
            try:
                fields = (path / "stat").read_text().rsplit(")", 1)[1].split()
                processes[int(path.name)] = (int(fields[1]), int(fields[21]))
            except (OSError, IndexError, ValueError):
                continue
        descendants = {pid}
        pending = {pid}
        while pending:
            parent = pending.pop()
            children = {child for child, info in processes.items() if info[0] == parent}
            new = children - descendants
            descendants.update(new)
            pending.update(new)
        return os.sysconf("SC_PAGE_SIZE") * sum(
            processes.get(child, (0, 0))[1] for child in descendants
        )

    def _run(self):
        while not self.done.is_set():
            self.peak_bytes = max(self.peak_bytes, self._rss_tree(self.pid))
            self.samples += 1
            self.done.wait(0.05)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.done.set()
        self.thread.join()


def hardcap(cases, output, cap_ratio):
    from vsize.engine import Engine

    dest = directory(output, "hardcap", cases)
    rows = []
    references = {
        "continuous": json.loads((output / "components/references.json").read_text()),
        "segmented": json.loads((output / "segmented/references.json").read_text()),
    }
    for case in cases:
        with PreparedAsset.from_path(case["source"]) as asset:
            for mode in ["continuous", "segmented"]:
                cap = round(references[mode][case["id"]]["file_bytes"] * cap_ratio)
                job = f"{case['id']}-{mode}"
                request = Request(
                    specification(case, export_mode=mode, segment_seconds=4),
                    compute=ComputeBudget(wall_seconds=15, max_probes=0, threads=1),
                    size=SizeConstraint(max_bytes=cap, max_crf=35, max_candidates=5),
                    sample_seconds=2 if mode == "continuous" else 4,
                    seed=SEED,
                )
                controller = Engine(dest / ".vsize-cache" / job)
                updates = []
                with MemoryMonitor() as memory:
                    started = time.monotonic()
                    answer = controller.run(request, on_update=updates.append, asset=asset)
                    observed_wall = time.monotonic() - started
                estimate = answer["estimate"]
                row = {
                    "case": case["id"],
                    "method": mode,
                    "cap_ratio": cap_ratio,
                    "cap_bytes": cap,
                    "budget_seconds": 15,
                    "max_candidates": 5,
                    "state": answer["status"],
                    "unmet": answer["unmet"],
                    "selected_crf": estimate["selected_crf"] if estimate else None,
                    "selected_bytes": estimate["estimated_bytes"] if estimate else None,
                    "selected_evidence": estimate["uncertainty"]["kind"] if estimate else None,
                    "wall_seconds": observed_wall,
                    "reported_seconds": answer["elapsed_seconds"],
                    "first_estimate_seconds": answer["first_estimate_seconds"],
                    "attempted_encode_seconds": answer["attempted_encode_seconds"],
                    "prepared_ingest_seconds": asset.ingest_seconds,
                    "sampled_process_tree_peak_rss_bytes": memory.peak_bytes or None,
                    "rss_sampling_period_seconds": 0.05,
                    "rss_samples": memory.samples,
                    "quality_search": answer["quality_search"],
                    "stop_reason": answer["stop_reason"],
                    "verification_outside_controller_budget": True,
                }
                if answer["status"] == "satisfied":
                    path = Path(
                        controller.materialize(
                            answer, dest / ".vsize-cache" / "published" / f"{job}.mp4"
                        )
                    )
                    if (
                        path.stat().st_size != estimate["estimated_bytes"]
                        or path.stat().st_size > cap
                    ):
                        raise AssertionError("satisfied publication violated the hard cap")
                    if digest(path) != estimate["sha256"]:
                        raise AssertionError("published content differs from the verified artifact")
                    verification = Deadline(180)
                    verification.run(
                        [
                            "ffmpeg",
                            "-v",
                            "error",
                            "-xerror",
                            "-threads",
                            "1",
                            "-i",
                            str(path),
                            "-an",
                            "-f",
                            "null",
                            "-",
                        ]
                    )
                    row["verified_decode_seconds"] = verification.elapsed
                    row["published_artifact"] = str(path)
                    row["hardcap_actual_verified"] = True
                else:
                    row["hardcap_actual_verified"] = False
                save(dest / "answers" / f"{job}.json", answer)
                save(dest / "updates" / f"{job}.json", updates)
                append(dest / "results.jsonl", row)
                rows.append(row)
    save(dest / "results.json", rows)


def memory_measurements(cases, output):
    from vsize.engine import Engine

    dest = directory(output, "memory", cases)
    rows = []
    for case in cases:
        with PreparedAsset.from_path(case["source"]) as asset:
            for mode in ["continuous", "segmented"]:
                job = f"{case['id']}-{mode}"
                request = Request(
                    specification(case, export_mode=mode, segment_seconds=4),
                    compute=ComputeBudget(wall_seconds=30, max_probes=0, threads=1),
                    seed=SEED,
                )
                controller = Engine(dest / ".vsize-cache" / job)
                with MemoryMonitor() as monitor:
                    answer = controller.run(request, asset=asset)
                row = {
                    "case": case["id"],
                    "method": mode,
                    "state": answer["status"],
                    "wall_seconds": answer["elapsed_seconds"],
                    "sampled_process_tree_peak_rss_bytes": monitor.peak_bytes or None,
                    "rss_sampling_period_seconds": 0.05,
                    "rss_samples": monitor.samples,
                    "prepared_snapshot_storage_bytes": asset.size_bytes,
                    "snapshot_pages_are_not_process_rss": True,
                    "host_and_cgroup_memory_not_measured": True,
                }
                append(dest / "results.jsonl", row)
                rows.append(row)
    save(dest / "results.json", rows)


def segmented(cases, output, prefixes):
    from vsize.fmp4 import planned_accounting, write_fragmented_mp4
    from vsize.segmented_media import SegmentedMedia

    dest = directory(output, "segmented", cases)
    rows = []
    jobs = {}
    for case in cases:
        deadline = Deadline(180)
        media = Media(specification(case), 1, Cache(dest / ".vsize-cache" / case["id"]), deadline)
        segment_media = SegmentedMedia(media, segment_seconds=4)
        plan = segment_media.prepare()
        blocks = [
            {
                "start": s.start_frame / float(segment_media.output_rate),
                "duration": s.frame_count / float(segment_media.output_rate),
                "input_bytes": 0,
            }
            for s in segment_media.segments
        ]
        sampler = Sampler(blocks, SEED, uniform=True)
        observed = {}
        encoded = 0.0
        for position, index in enumerate(sampler.order[: max(prefixes)], 1):
            record = segment_media.encode_segment(index, 23)
            track, packets = segment_media.read_segment(record)
            observed[index] = (record, track, packets)
            encoded += blocks[index]["duration"]
            sampler.add(index, record)
            if position not in prefixes:
                continue
            base = sampler.estimate(fixed_bytes=0)
            known = planned_accounting(
                track,
                segment_media.total_frames,
                sum(p.duration for p in packets) * segment_media.total_frames // len(packets),
                len(segment_media.segments),
            )
            payload = base["estimated_payload_bytes"]
            row = {
                "case": case["id"],
                "method": "segmented-reusable",
                "prefix": position,
                "prediction_bytes": round(payload + known.total_bytes),
                "prediction_payload_bytes": payload,
                "warm_request_wall_seconds": deadline.elapsed,
                "encoded_media_seconds": encoded,
                "encoded_media_fraction": encoded / segment_media.duration,
                "duration": segment_media.duration,
                "sampling_census": position == len(blocks),
                "source_blocks": len(blocks),
                "uncertainty_kind": "uncalibrated",
                "target": "independent_segmented_fmp4",
                "known_byte_accounting": asdict(known),
            }
            append(dest / "predictions.jsonl", row)
            rows.append(row)
        jobs[case["id"]] = {"plan": plan, "sample_keys": [v[0]["key"] for v in observed.values()]}
        # Release any shared state. Actual assembly begins only after all predictions exist.
    references = {}
    comparisons = []
    for case in cases:
        deadline = Deadline(180)
        media = Media(specification(case), 1, Cache(dest / ".vsize-cache" / case["id"]), deadline)
        segment_media = SegmentedMedia(media, segment_seconds=4)
        segment_media.prepare()
        segments, records = [], []
        for segment in segment_media.segments:
            record = segment_media.encode_segment(segment.index, 23)
            track, packets = segment_media.read_segment(record)
            segments.append(packets)
            records.append(record)
        path = dest / ".vsize-cache" / case["id"] / "final-segmented.mp4"
        accounting = write_fragmented_mp4(path, track, segments, deadline=deadline)
        segment_media.verify_source()
        deadline.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-xerror",
                "-threads",
                "1",
                "-i",
                str(path),
                "-an",
                "-f",
                "null",
                "-",
            ]
        )
        actual = {
            "file_bytes": path.stat().st_size,
            "payload_bytes": accounting.video_payload_bytes,
            "artifact": str(path),
            "sha256": digest(path),
            "accounting": asdict(accounting),
            "reference_wall_seconds": deadline.elapsed,
            "reused_sample_segments": sum(record["cache_hit"] for record in records),
            "total_segments": len(records),
            "sample_keys": jobs[case["id"]]["sample_keys"],
        }
        if actual["file_bytes"] != accounting.total_bytes:
            raise AssertionError("additive accounting mismatch")
        references[case["id"]] = actual
        continuous = continuous_reference(case, dest / "continuous")
        cq = quality_metrics(case["source"], continuous["artifact"])
        sq = quality_metrics(case["source"], path)
        comparisons.append(
            {
                "case": case["id"],
                "crf": 23,
                "continuous_bytes": continuous["file_bytes"],
                "segmented_bytes": actual["file_bytes"],
                "segmented_size_change_percent": 100
                * (actual["file_bytes"] / continuous["file_bytes"] - 1),
                "continuous_quality": cq,
                "segmented_quality": sq,
                "delta_psnr_db": (sq["psnr_db"] - cq["psnr_db"])
                if sq["psnr_db"] is not None and cq["psnr_db"] is not None
                else None,
                "delta_ssim": sq["ssim"] - cq["ssim"],
                "continuous_reference": continuous,
            }
        )
        save(dest / "quality.json", comparisons)
    save(dest / "references.json", references)
    save(dest / "plans.json", jobs)
    evaluate(rows, references, dest)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "stage",
        choices=[
            "components",
            "segmented",
            "engine",
            "ab-av1",
            "hardcap",
            "memory",
            "fallback",
            "quality",
        ],
    )
    parser.add_argument("--cases", help="comma separated frozen development case IDs")
    parser.add_argument("--output", type=Path, default=OUTPUT)
    parser.add_argument("--prefixes", type=int, nargs="+", default=[1, 2, 3, 6])
    parser.add_argument("--budgets", type=float, nargs="+", default=[5, 15])
    parser.add_argument("--fractions", type=float, nargs="+", default=[0.2, 0.5])
    parser.add_argument(
        "--ab-av1", type=Path, default=Path("/tmp/video-estimation-research/ab-av1")
    )
    parser.add_argument("--baseline-budget", type=float, default=5)
    parser.add_argument("--cap-ratio", type=float, default=0.7)
    parser.add_argument("--sampling-plan", choices=["temporal", "content"], default="temporal")
    parser.add_argument("--sample-seconds", type=float, default=2)
    parser.add_argument("--segment-seconds", type=float, default=4)
    parser.add_argument("--probe-mode", choices=["auto", "context", "bare"], default="auto")
    parser.add_argument("--fallback-budget", type=float, default=10)
    args = parser.parse_args()
    cases = cases_selected(args.cases.split(",") if args.cases else None)
    if args.stage == "components":
        components(cases, args.output, args.prefixes)
    elif args.stage == "segmented":
        segmented(cases, args.output, args.prefixes)
    elif args.stage == "ab-av1":
        ab_av1(cases, args.output, args.ab_av1, args.baseline_budget, args.sample_seconds)
    elif args.stage == "hardcap":
        hardcap(cases, args.output, args.cap_ratio)
    elif args.stage == "memory":
        memory_measurements(cases, args.output)
    elif args.stage == "quality":
        quality(cases, args.output)
    elif args.stage == "fallback":
        engine(
            cases,
            args.output,
            [args.fallback_budget],
            [None],
            args.sampling_plan,
            args.sample_seconds,
            args.segment_seconds,
            stage="fallback",
            probe_mode=args.probe_mode,
        )
    else:
        engine(
            cases,
            args.output,
            args.budgets,
            args.fractions,
            args.sampling_plan,
            args.sample_seconds,
            args.segment_seconds,
            probe_mode=args.probe_mode,
        )


if __name__ == "__main__":
    main()
