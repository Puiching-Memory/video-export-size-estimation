"""One frozen holdout evaluation with separate native and temporal contracts.

Inventory and structural checks do not score predictions. The `run` command
requires an explicit source-policy freeze, persists all cold-cache predictions,
then creates one completed reference per case and target. A completed segmented
reference is compared only with predictions of that segmented export target.
The supplied 130-frame AOM sequences remain at native dimensions and frame rate.
Meridian's optional long workflow is explicitly 640x360, never native-4K evidence.
"""

import argparse
from datetime import datetime, timezone
from fractions import Fraction
import hashlib
import json
import os
from pathlib import Path
import shutil
import statistics
import subprocess
import time

from vsize import ComputeBudget, EncodeSpec, Request
from vsize.assets import PreparedAsset
from vsize.engine import Engine
from vsize.runtime import BudgetExhausted, Deadline, digest


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "artifacts/industrial-v1/manifest-test_reserved-sdr_8bit.json"
VALIDATION = ROOT / "results/corpus-2026-10-03/validation.json"
OUTPUT = ROOT / "results/industrial-core-2026-10-03"
SEED = 20261003
SHORT_CASES = {
    "artistic-intro",
    "mobile-sharing",
    "noise-ocean-game",
    "scene-composition",
    "shaky-baseball-4k",
    "shaky-fireworks-4k",
    "shaky-walk",
    "trees-grass",
    "walking-street",
}


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".partial")
    with temporary.open("w") as stream:
        json.dump(value, stream, indent=2, allow_nan=False)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def append(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as stream:
        stream.write(json.dumps(value, allow_nan=False) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    print(json.dumps(value, allow_nan=False), flush=True)


def source_code_sha256():
    value = hashlib.sha256()
    for path in sorted((ROOT / "src").rglob("*.py")):
        value.update(path.relative_to(ROOT).as_posix().encode())
        value.update(path.read_bytes())
    return value.hexdigest()


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def manifest_cases(manifest=MANIFEST, names=None):
    dataset = json.loads(manifest.read_text())
    if dataset.get("split") != "test_reserved" or dataset.get("scope") != "sdr_8bit":
        raise ValueError("industrial final evaluation requires the reserved SDR holdout manifest")
    metadata = {row["id"]: row for row in json.loads(VALIDATION.read_text())["assets"]}
    cases = []
    for row in dataset["cases"]:
        if names is not None and row["id"] not in names:
            continue
        known = metadata[row["id"]]
        if known["sha256"] != row["sha256"] or known["split"] != "test_reserved":
            raise ValueError(f"source metadata differs from reserved manifest: {row['id']}")
        if Path(row["source"]).stat().st_size != known["bytes"]:
            raise ValueError(f"source size differs from verified metadata: {row['id']}")
        cases.append(
            {
                **row,
                "bytes": known["bytes"],
                "video": known["video"],
                "transport_cleanup": known.get("transport_cleanup"),
                "source_representation": "verified_original_or_declared_byte_cleanup",
            }
        )
    if names is not None and {row["id"] for row in cases} != set(names):
        raise ValueError("requested case is absent from the reserved manifest")
    return cases


def inventory(output, manifest):
    cases = manifest_cases(manifest)
    short = [case for case in cases if case["id"] in SHORT_CASES]
    value = {
        "schema_version": 1,
        "stage": "metadata_only_unscored",
        "created_at_utc": utc_now(),
        "manifest_sha256": digest(manifest),
        "native_short_cases": len(short),
        "native_short_group_count": len({case["group_id"] for case in short}),
        "native_short_source_bytes": sum(case["bytes"] for case in short),
        "max_individual_bytes": max(case["bytes"] for case in cases),
        "prepared_asset_limit_bytes": 2 * 1024**3,
        "cpu_quota": Path("/sys/fs/cgroup/cpu.max").read_text().strip(),
        "memory_limit_bytes": int(Path("/sys/fs/cgroup/memory.max").read_text()),
        "cases": cases,
        "note": "Existing verified source metadata only; no encoder outputs or prediction labels read.",
    }
    save(output / "inventory.json", value)
    print(json.dumps({key: val for key, val in value.items() if key != "cases"}), flush=True)


def freeze_metadata(path):
    frozen = json.loads(path.read_text())
    expected = frozen.get("source_code_sha256")
    if not isinstance(expected, str) or expected != source_code_sha256():
        raise ValueError("source policy does not match the explicit final holdout freeze")
    return {
        "freeze_file": str(path.resolve()),
        "freeze_file_sha256": digest(path),
        "source_code_sha256": expected,
        "freeze": frozen,
    }


def check_frozen(metadata):
    if source_code_sha256() != metadata["source_code_sha256"]:
        raise RuntimeError("source changed after holdout freeze; evaluation stopped")


def specification(case, mode, *, long_workflow=False):
    return EncodeSpec(
        Path(case["source"]),
        crf=23,
        preset="veryfast" if long_workflow else "medium",
        video_filter="scale=640:360" if long_workflow else None,
        export_mode=mode,
        segment_seconds=4 if long_workflow else 0.5,
    )


def request(spec, budget, fraction, *, reference=False, sampling_plan="content"):
    return Request(
        spec,
        compute=ComputeBudget(
            wall_seconds=budget,
            max_probes=0 if reference else 6,
            threads=1,
            max_encode_fraction=1.0 if reference else fraction,
        ),
        sample_seconds=4 if spec.video_filter else 0.5,
        seed=SEED,
        probe_mode="auto",
        sampling_plan=sampling_plan,
    )


def check_asset(case, asset):
    if asset.sha256 != case["sha256"] or asset.bytes != case["bytes"]:
        raise ValueError(f"captured source differs from locked holdout: {case['id']}")


def run_answer(engine, asked, asset):
    started = time.monotonic()
    try:
        result = engine.run(asked, asset=asset)
    except (ValueError, RuntimeError, OSError, BudgetExhausted) as error:
        result = {
            "status": "failed",
            "estimate": None,
            "error": str(error),
            "elapsed_seconds": time.monotonic() - started,
            "attempted_encode_seconds": 0,
            "attempted_audio_seconds": 0,
            "total_probe_count": 0,
            "first_estimate_seconds": None,
        }
    return result


def prediction_row(case, mode, budget, fraction, answer, asset, job_id):
    estimate = answer.get("estimate")
    duration = float(Fraction(case["video"]["frames"], 1) / Fraction(case["video"]["frame_rate"]))
    return {
        "job_id": job_id,
        "case": case["id"],
        "group_id": case["group_id"],
        "mode": mode,
        "target": f"{mode}_mp4",
        "native_dimensions": True,
        "budget_seconds": budget,
        "max_encode_fraction": fraction,
        "prediction_bytes": estimate["estimated_bytes"] if estimate else None,
        "state": answer["status"],
        "uncertainty_kind": estimate["uncertainty"]["kind"] if estimate else None,
        "uncertainty": estimate["uncertainty"] if estimate else None,
        "wall_seconds": answer["elapsed_seconds"],
        "prepared_ingest_seconds": asset.ingest_seconds,
        "ingest_plus_request_seconds": asset.ingest_seconds + answer["elapsed_seconds"],
        "ingest_in_request_budget": False,
        "first_estimate_seconds": answer.get("first_estimate_seconds"),
        "encoded_video_seconds": answer["attempted_encode_seconds"],
        "encoded_video_fraction": answer["attempted_encode_seconds"] / duration,
        "encoded_audio_seconds": answer.get("attempted_audio_seconds", 0),
        "probe_count": answer["total_probe_count"],
        "probe_mode": estimate.get("probe_mode") if estimate else None,
        "source_duration_seconds": duration,
        "source_bytes": asset.bytes,
        "source_sha256": asset.sha256,
        "error": answer.get("error"),
    }


def inspect_artifact(path, case, mode, accounting=None, *, long_workflow=False):
    started = time.monotonic()
    deadline = Deadline(900 if long_workflow else 120)
    info = json.loads(
        deadline.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-threads",
                "1",
                "-count_frames",
                "-show_streams",
                "-show_format",
                "-of",
                "json",
                str(path),
            ]
        ).stdout
    )
    video = next(stream for stream in info["streams"] if stream["codec_type"] == "video")
    width, height = (
        (640, 360) if long_workflow else (case["video"]["width"], case["video"]["height"])
    )
    if (video["width"], video["height"]) != (width, height):
        raise AssertionError("export changed declared dimensions")
    if int(video["nb_read_frames"]) != case["video"]["frames"]:
        raise AssertionError("export frame count differs from the full source timeline")
    if Fraction(video["avg_frame_rate"]) != Fraction(case["video"]["frame_rate"]):
        raise AssertionError("export changed declared frame rate")
    if accounting:
        total = sum(
            accounting[name]
            for name in (
                "init_bytes",
                "video_payload_bytes",
                "audio_payload_bytes",
                "fragment_bytes",
            )
        )
        if total != path.stat().st_size:
            raise AssertionError("fixed fragmented-container byte accounting differs from file")
    deadline.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-v",
            "error",
            "-nostdin",
            "-xerror",
            "-threads",
            "1",
            "-filter_threads",
            "1",
            "-i",
            str(path),
            "-map",
            "0:v:0",
            "-map",
            "0:a?",
            "-f",
            "null",
            "-",
        ]
    )
    return {
        "passed": True,
        "mode": mode,
        "frames": int(video["nb_read_frames"]),
        "width": width,
        "height": height,
        "frame_rate": video["avg_frame_rate"],
        "video_duration": video.get("duration"),
        "format_duration": info["format"]["duration"],
        "audio_streams": sum(s["codec_type"] == "audio" for s in info["streams"]),
        "file_bytes": path.stat().st_size,
        "inspection_seconds": time.monotonic() - started,
        "byte_accounting": accounting,
        "native_dimensions": not long_workflow,
        "validation_kind": "full_decode_metadata_and_exact_container_identity_not_prediction_accuracy",
    }


def structural(cases, output):
    destination = output / "structural"
    destination.mkdir(parents=True, exist_ok=True)
    for case in cases:
        with PreparedAsset.from_path(case["source"]) as asset:
            check_asset(case, asset)
            for mode in ["continuous", "segmented"]:
                path = destination / "records" / f"{case['id']}-{mode}.json"
                if path.exists():
                    raise FileExistsError("structural result already exists; no silent rerun")
                answer = run_answer(
                    Engine(destination / ".vsize-cache" / f"{case['id']}-{mode}"),
                    request(specification(case, mode), 120, 1, reference=True),
                    asset,
                )
                row = {
                    "case": case["id"],
                    "group_id": case["group_id"],
                    "mode": mode,
                    "source_code_sha256": source_code_sha256(),
                    "scope": "structural_only_unscored",
                    "prediction_accuracy_evaluated": False,
                    "answer": answer,
                }
                estimate = answer.get("estimate")
                if estimate and estimate["uncertainty"]["kind"] == "exact":
                    row["inspection"] = inspect_artifact(
                        Path(estimate["artifact"]), case, mode, estimate.get("byte_accounting")
                    )
                save(path, row)
                print(
                    json.dumps(
                        {
                            "case": case["id"],
                            "mode": mode,
                            "passed": row.get("inspection", {}).get("passed", False),
                            "scope": "structural_only_unscored",
                        }
                    ),
                    flush=True,
                )


def baseline_predictions(cases, destination, metadata, binary, budget, *, resume=False):
    """Unmodified official ab-av1 estimates, before any completed labels exist."""
    prediction_path = destination / "baseline-predictions.jsonl"
    rows = (
        [json.loads(line) for line in prediction_path.read_text().splitlines()]
        if prediction_path.exists()
        else []
    )
    seen = {row["case"] for row in rows}
    if rows and not resume:
        raise FileExistsError("baseline predictions already exist")
    for case in cases:
        if case["id"] in seen:
            continue
        check_frozen(metadata)
        cache = destination / ".vsize-cache" / "baseline" / case["id"]
        if cache.exists():
            shutil.rmtree(cache)
        cache.mkdir(parents=True)
        with PreparedAsset.from_path(case["source"]) as asset:
            check_asset(case, asset)
            command = [
                str(binary),
                "sample-encode",
                "-i",
                asset.input_path,
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
                "500ms",
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
                str(cache),
                "--stdout-format",
                "json",
            ]
            deadline = Deadline(budget)
            deadline.bind_fd(asset.fileno())
            prediction, official, state, error = None, None, "completed", None
            try:
                result = deadline.run(command)
                official = json.loads(result.stdout)
                prediction = official["predicted_encode_size"]
                (destination / "baseline-logs").mkdir(exist_ok=True)
                (destination / "baseline-logs" / f"{case['id']}.txt").write_bytes(result.stderr)
            except (BudgetExhausted, RuntimeError, ValueError, KeyError) as exc:
                state, error = "failed", str(exc)
            row = {
                "case": case["id"],
                "group_id": case["group_id"],
                "method": "ab-av1-official-unmodified-estimate",
                "mode": "continuous",
                "prediction_bytes": prediction,
                "state": state,
                "error": error,
                "wall_seconds": deadline.elapsed,
                "budget_seconds": budget,
                "native_dimensions": True,
                "prepared_ingest_seconds": asset.ingest_seconds,
                "ingest_in_request_budget": False,
                "samples_requested": 3,
                "sample_seconds_requested": 0.5,
                "encoded_fraction_cap": None,
                "command": command,
                "official_output": official,
                "uncertainty_kind": "uncalibrated",
                "target_note": "Official size estimate is kept unchanged and compared to completed "
                "continuous MP4 file bytes; no normalization is invented.",
            }
            check_frozen(metadata)
            append(prediction_path, row)
            rows.append(row)
    return rows


def temporal_predictions(cases, destination, metadata, *, resume=False):
    """Preregistered same-target and same-fraction temporal controller baseline."""
    path = destination / "temporal-predictions.jsonl"
    rows = [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
    if rows and not resume:
        raise FileExistsError("temporal baseline already started")
    seen = {row["job_id"] for row in rows}
    for case in cases:
        with PreparedAsset.from_path(case["source"]) as asset:
            check_asset(case, asset)
            for mode in ["continuous", "segmented"]:
                job_id = f"{case['id']}-{mode}-temporal-15-0.2"
                if job_id in seen:
                    continue
                check_frozen(metadata)
                cache = destination / ".vsize-cache" / "predictions-temporal" / job_id
                if cache.exists():
                    shutil.rmtree(cache)
                asked = request(specification(case, mode), 15, 0.2, sampling_plan="temporal")
                answer = run_answer(Engine(cache), asked, asset)
                check_frozen(metadata)
                save(destination / "temporal-answers" / f"{job_id}.json", answer)
                row = prediction_row(case, mode, 15, 0.2, answer, asset, job_id)
                row["sampling_plan"] = "temporal"
                append(path, row)
                rows.append(row)
    if len(rows) != 2 * len(cases):
        raise ValueError("temporal baseline lacks a frozen prediction job")
    return rows


def run(
    cases,
    output,
    frozen,
    budgets,
    fractions,
    reference_seconds,
    *,
    resume=False,
    ab_av1=None,
    baseline_budget=15,
    temporal_baseline=False,
):
    destination = output / "holdout"
    if destination.exists() and not resume:
        raise FileExistsError(
            "final holdout already started; use --resume only for the same freeze"
        )
    destination.mkdir(parents=True, exist_ok=True)
    metadata = {
        "schema_version": 1,
        "created_at_utc": utc_now(),
        **frozen,
        "benchmark_script_sha256": digest(Path(__file__)),
        "split": "test_reserved",
        "scope": "native_8bit_sdr_short_sequences",
        "cases": cases,
        "groups": len({case["group_id"] for case in cases}),
        "budgets_seconds": budgets,
        "fractions": fractions,
        "max_probes": 6,
        "threads": 1,
        "filter_threads": 1,
        "crf": 23,
        "preset": "medium",
        "sample_seconds": 0.5,
        "segment_seconds": 0.5,
        "probe_mode": "auto",
        "sampling_plan": "content",
        "seed": SEED,
        "cold_cache_per_prediction": True,
        "preparation": "one sealed original input per case, separately timed, reused across requests",
        "audio": "first audio preserved when present; public AOM raw sources have no audio",
        "targets": {
            "continuous": "complete continuous x264 ordinary MP4",
            "segmented": "independent 0.5-second integer-frame closed-GOP fixed fMP4",
        },
        "reference_deadline_seconds": reference_seconds,
        "baseline": {
            "binary": str(ab_av1.resolve()),
            "binary_sha256": digest(ab_av1),
            "version": subprocess.check_output([str(ab_av1), "--version"], text=True).strip(),
            "wall_seconds": baseline_budget,
            "samples": 3,
            "sample_seconds": 0.5,
            "native_dimensions": True,
            "max_encode_fraction": None,
        }
        if ab_av1 is not None
        else None,
        "ordering": "all predictions fsync-persisted before references are generated or read",
        "ffmpeg": subprocess.check_output(["ffmpeg", "-version"], text=True).splitlines()[0],
    }
    previous_metadata = destination / "metadata.json"
    if resume:
        previous = json.loads(previous_metadata.read_text())
        if previous["benchmark_script_sha256"] != metadata["benchmark_script_sha256"]:
            amendment = json.loads(
                (destination / "preregistration-temporal-baseline.json").read_text()
            )
            if not (
                temporal_baseline
                and amendment["references_started"] is False
                and amendment["complete_label_marker_exists"] is False
                and amendment["source_code_sha256"] == previous["source_code_sha256"]
                and amendment["original_benchmark_script_sha256"]
                == previous["benchmark_script_sha256"]
                and amendment["amended_benchmark_script_sha256"]
                == metadata["benchmark_script_sha256"]
            ):
                raise ValueError("unregistered benchmark change cannot resume the final holdout")
            previous["active_benchmark_script_sha256"] = metadata["benchmark_script_sha256"]
            previous["temporal_baseline"] = amendment
            save(previous_metadata, previous)
        for key in [
            "source_code_sha256",
            "budgets_seconds",
            "fractions",
            "cases",
            "reference_deadline_seconds",
            "baseline",
        ]:
            if previous[key] != metadata[key]:
                raise ValueError(f"resume would change frozen evaluation field: {key}")
        metadata = previous
    else:
        save(previous_metadata, metadata)
    prediction_path = destination / "predictions.jsonl"
    rows = (
        [json.loads(line) for line in prediction_path.read_text().splitlines()]
        if prediction_path.exists()
        else []
    )
    seen = {row["job_id"] for row in rows}
    if len(seen) != len(rows):
        raise ValueError("duplicate persisted prediction job")
    for case in cases:
        with PreparedAsset.from_path(case["source"]) as asset:
            check_asset(case, asset)
            for mode in ["continuous", "segmented"]:
                for budget in budgets:
                    for fraction in fractions:
                        job_id = f"{case['id']}-{mode}-{budget:g}-{fraction:g}"
                        if job_id in seen:
                            continue
                        check_frozen(metadata)
                        updates = []
                        job_cache = destination / ".vsize-cache" / "predictions" / job_id
                        # Resume never turns an interrupted unpersisted job into
                        # a warm-cache measurement. Only this runner's own cache
                        # directory is removed; completed jobs remain untouched.
                        if job_cache.exists():
                            shutil.rmtree(job_cache)
                        engine = Engine(job_cache)
                        asked = request(specification(case, mode), budget, fraction)
                        started = time.monotonic()
                        try:
                            answer = engine.run(asked, updates.append, asset=asset)
                        except (ValueError, RuntimeError, OSError, BudgetExhausted) as error:
                            answer = {
                                "status": "failed",
                                "estimate": None,
                                "error": str(error),
                                "elapsed_seconds": time.monotonic() - started,
                                "attempted_encode_seconds": 0,
                                "attempted_audio_seconds": 0,
                                "total_probe_count": 0,
                                "first_estimate_seconds": None,
                            }
                        check_frozen(metadata)
                        save(destination / "answers" / f"{job_id}.json", answer)
                        save(destination / "updates" / f"{job_id}.json", updates)
                        row = prediction_row(case, mode, budget, fraction, answer, asset, job_id)
                        append(prediction_path, row)
                        rows.append(row)
                        seen.add(job_id)
    expected_jobs = len(cases) * 2 * len(budgets) * len(fractions)
    if len(rows) != expected_jobs:
        raise ValueError("not all frozen prediction jobs were persisted")
    temporal_rows = (
        temporal_predictions(cases, destination, metadata, resume=resume)
        if temporal_baseline
        else []
    )
    baseline_rows = (
        baseline_predictions(cases, destination, metadata, ab_av1, baseline_budget, resume=resume)
        if ab_av1 is not None
        else []
    )
    save(
        destination / "predictions.complete.json",
        {
            "rows": len(rows),
            "sha256": digest(prediction_path),
            "completed_at_utc": utc_now(),
            "reference_labels_read": False,
            "baseline_rows": len(baseline_rows),
            "temporal_rows": len(temporal_rows),
            "temporal_predictions_sha256": digest(destination / "temporal-predictions.jsonl")
            if temporal_rows
            else None,
            "baseline_predictions_sha256": digest(destination / "baseline-predictions.jsonl")
            if baseline_rows
            else None,
        },
    )
    references = {}
    for case in cases:
        with PreparedAsset.from_path(case["source"]) as asset:
            check_asset(case, asset)
            for mode in ["continuous", "segmented"]:
                check_frozen(metadata)
                key = f"{case['id']}-{mode}"
                persisted = destination / "references" / f"{key}.json"
                if persisted.exists() and resume:
                    row = json.loads(persisted.read_text())
                else:
                    answer = run_answer(
                        Engine(destination / ".vsize-cache" / "references" / key),
                        request(specification(case, mode), reference_seconds, 1, reference=True),
                        asset,
                    )
                    row = {
                        "case": case["id"],
                        "mode": mode,
                        "answer": answer,
                        "reference_wall_seconds": answer["elapsed_seconds"],
                        "source_ingest_seconds": asset.ingest_seconds,
                        "file_bytes": None,
                        "inspection": None,
                    }
                    estimate = answer.get("estimate")
                    if estimate and estimate["uncertainty"]["kind"] == "exact":
                        row["file_bytes"] = estimate["estimated_bytes"]
                        row["inspection"] = inspect_artifact(
                            Path(estimate["artifact"]), case, mode, estimate.get("byte_accounting")
                        )
                    save(persisted, row)
                    print(
                        json.dumps(
                            {
                                "reference": key,
                                "completed": row["file_bytes"] is not None,
                                "full_decode": bool(row["inspection"]),
                            }
                        ),
                        flush=True,
                    )
                references[key] = row
    check_frozen(metadata)
    save(destination / "references.json", references)
    evaluate(rows, references, destination)
    if temporal_rows:
        evaluate(temporal_rows, references, destination, prefix="temporal-")
        content_summary = json.loads((destination / "summary.json").read_text())
        temporal_summary = json.loads((destination / "temporal-summary.json").read_text())
        comparison = []
        for mode in ["continuous", "segmented"]:
            content = next(
                row
                for row in content_summary
                if row["mode"] == mode
                and row["budget_seconds"] == 15
                and row["max_encode_fraction"] == 0.2
            )
            temporal = next(row for row in temporal_summary if row["mode"] == mode)
            comparison.append(
                {
                    "mode": mode,
                    "same_target": True,
                    "wall_budget_seconds": 15,
                    "max_encode_fraction": 0.2,
                    "content": content,
                    "temporal": temporal,
                    "note": "Both use the identical completed target per case; "
                    "the content policy was frozen before any full labels.",
                }
            )
        save(destination / "same-target-comparison.json", comparison)
    if baseline_rows:
        evaluated = []
        for row in baseline_rows:
            reference = references[f"{row['case']}-continuous"]
            value = {**row, "actual_bytes": reference["file_bytes"]}
            if row["prediction_bytes"] is not None and reference["file_bytes"] is not None:
                value["absolute_error_percent"] = abs(
                    100 * (row["prediction_bytes"] / reference["file_bytes"] - 1)
                )
            evaluated.append(value)
        save(destination / "baseline-evaluated.json", evaluated)
        errors = [
            row["absolute_error_percent"] for row in evaluated if "absolute_error_percent" in row
        ]
        save(
            destination / "baseline-summary.json",
            {
                "method": "ab-av1-0.11.7-official-native-three-500ms-samples",
                "cases": len(evaluated),
                "estimates": len(errors),
                "within_10_percent_all_cases": sum(error <= 10 for error in errors)
                / len(evaluated),
                "median_ape_percent": statistics.median(errors) if errors else None,
                "worst_ape_percent": max(errors) if errors else None,
                "median_wall_seconds": statistics.median(row["wall_seconds"] for row in evaluated),
                "encoded_fraction_cap": None,
                "fairness_note": "Shared wall and thread budget; official fixed sample count has no "
                "encode-fraction cap, unlike the controller requests.",
            },
        )


def evaluate(rows, references, destination, *, prefix=""):
    evaluated = []
    for row in rows:
        reference = references[f"{row['case']}-{row['mode']}"]
        result = {
            **row,
            "actual_bytes": reference["file_bytes"],
            "reference_completed": reference["file_bytes"] is not None,
        }
        if row["prediction_bytes"] is not None and reference["file_bytes"] is not None:
            result["signed_error_percent"] = 100 * (
                row["prediction_bytes"] / reference["file_bytes"] - 1
            )
            result["absolute_error_percent"] = abs(result["signed_error_percent"])
            result["request_wall_vs_complete_reference"] = (
                row["wall_seconds"] / reference["reference_wall_seconds"]
            )
        evaluated.append(result)
    save(destination / f"{prefix}evaluated.json", evaluated)
    summary = []
    for mode, budget, fraction in sorted(
        {(r["mode"], r["budget_seconds"], r["max_encode_fraction"]) for r in rows}
    ):
        group = [
            row
            for row in evaluated
            if (row["mode"], row["budget_seconds"], row["max_encode_fraction"])
            == (mode, budget, fraction)
        ]
        completed = [row for row in group if "absolute_error_percent" in row]
        errors = [row["absolute_error_percent"] for row in completed]
        summary.append(
            {
                "mode": mode,
                "budget_seconds": budget,
                "max_encode_fraction": fraction,
                "cases": len(group),
                "source_groups": len({row["group_id"] for row in group}),
                "reference_complete_cases": sum(row["reference_completed"] for row in group),
                "estimates": sum(row["prediction_bytes"] is not None for row in group),
                "scored_estimates": len(completed),
                "no_estimate_rate": sum(row["prediction_bytes"] is None for row in group)
                / len(group),
                "within_10_percent_all_cases": sum(error <= 10 for error in errors) / len(group),
                "median_ape_percent": statistics.median(errors) if errors else None,
                "worst_ape_percent": max(errors) if errors else None,
                "median_wall_seconds": statistics.median(row["wall_seconds"] for row in group),
                "median_encoded_video_fraction": statistics.median(
                    row["encoded_video_fraction"] for row in group
                ),
                "fraction_violations": sum(
                    row["encoded_video_fraction"] > fraction + 1e-6 for row in group
                ),
                "exact_outputs": sum(row["uncertainty_kind"] == "exact" for row in group),
                "certified_satisfied": sum(row["state"] == "satisfied" for row in group),
            }
        )
    save(destination / f"{prefix}summary.json", summary)
    if not prefix:
        write_report(summary, evaluated, references, destination)


def write_report(summary, rows, references, destination):
    lines = [
        "# Frozen industrial-related holdout",
        "",
        "Nine public AOM CTC sources, eight conservative source groups; native dimensions, "
        "original frame rate and all 130 frames. This is a CTC-source subset, not official "
        "CTC codec testing, SOTA certification, or broad industrial generalization.",
        "",
        "Continuous and segmented predictions are scored against their own actual export targets. "
        "Every prediction was persisted before completed reference labels were generated/read. "
        "No holdout scores were used to adjust the frozen implementation.",
        "",
        "Request time excludes explicitly reported immutable-upload preparation. Each prediction "
        "has its own cold result cache. Structural success alone is not prediction accuracy.",
        "",
        "| Target | Wall budget | Encode fraction | Estimates/cases | Within 10% of all cases | "
        "Median APE | Worst APE | Median wall |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for item in summary:
        median = (
            f"{item['median_ape_percent']:.2f}%"
            if item["median_ape_percent"] is not None
            else "n/a"
        )
        worst = (
            f"{item['worst_ape_percent']:.2f}%" if item["worst_ape_percent"] is not None else "n/a"
        )
        lines.append(
            f"| {item['mode']} | {item['budget_seconds']:g}s | "
            f"{item['max_encode_fraction']:.0%} | {item['estimates']}/{item['cases']} | "
            f"{item['within_10_percent_all_cases']:.0%} | {median} | {worst} | "
            f"{item['median_wall_seconds']:.2f}s |"
        )
    lines += [
        "",
        "Failed/no-estimate cases remain in the denominator. Missing completed references "
        "are reported separately. Without an independent calibration profile, sampling estimates "
        "retain `uncalibrated` evidence; this benchmark does not invent coverage certificates.",
        "",
        f"Full reference targets: {sum(r['file_bytes'] is not None for r in references.values())}"
        f"/{len(references)}. Full-decode checks are recorded per reference.",
        "",
        "See `metadata.json`, `predictions.jsonl`, `predictions.complete.json`, `references.json`, "
        "`evaluated.json`, `summary.json`, and per-request `answers/` for the complete evidence.",
    ]
    (destination / "README.md").write_text("\n".join(lines) + "\n")


def long_preflight(case, output):
    """Measure three ten-second windows without making size predictions."""
    destination = output / "long-preflight"
    destination.mkdir(parents=True, exist_ok=False)
    rows = []
    for start in [60, 300, 600]:
        path = destination / ".vsize-cache" / f"window-{start}.mp4"
        path.parent.mkdir(parents=True, exist_ok=True)
        command = [
            "ffmpeg",
            "-hide_banner",
            "-v",
            "error",
            "-nostdin",
            "-xerror",
            "-y",
            "-threads",
            "1",
            "-filter_threads",
            "1",
            "-ss",
            str(start),
            "-i",
            case["source"],
            "-t",
            "10",
            "-map",
            "0:v:0",
            "-map",
            "0:a:0?",
            "-vf",
            "scale=640:360,fps=12",
            "-fps_mode",
            "passthrough",
            "-c:v",
            "libx264",
            "-crf",
            "23",
            "-preset",
            "veryfast",
            "-threads",
            "1",
            "-c:a",
            "aac",
            "-b:a",
            "128000",
            str(path),
        ]
        started = time.monotonic()
        state, error = "completed", None
        try:
            Deadline(120).run(command)
        except (RuntimeError, BudgetExhausted) as exc:
            state, error = "failed", str(exc)
        wall = time.monotonic() - started
        duration = float(
            Fraction(case["video"]["frames"], 1) / Fraction(case["video"]["frame_rate"])
        )
        row = {
            "window_start_seconds": start,
            "window_seconds": 10,
            "wall_seconds": wall,
            "state": state,
            "error": error,
            "extrapolated_full_timeline_seconds": wall / 10 * duration
            if state == "completed"
            else None,
            "scale": "640:360",
            "output_fps": 12,
            "preset": "veryfast",
            "threads": 1,
            "native_4k_validated": False,
            "prediction_accuracy_evaluated": False,
            "warning": "Sparse throughput windows are an estimate, not a full-length completion proof.",
        }
        rows.append(row)
        save(destination / "timing.json", rows)
        print(json.dumps(row), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=["inventory", "structural", "run", "long-preflight"])
    parser.add_argument("--manifest", type=Path, default=MANIFEST)
    parser.add_argument("--output", type=Path, default=OUTPUT)
    parser.add_argument("--cases", help="comma-separated reserved case IDs")
    parser.add_argument("--freeze", type=Path)
    parser.add_argument("--budgets", nargs="+", type=float, default=[15])
    parser.add_argument("--fractions", nargs="+", type=float, default=[0.2, 0.5])
    parser.add_argument("--reference-seconds", type=float, default=120)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--ab-av1", type=Path)
    parser.add_argument("--baseline-budget", type=float, default=15)
    parser.add_argument("--temporal-baseline", action="store_true")
    args = parser.parse_args()
    if args.stage == "inventory":
        inventory(args.output, args.manifest)
        return
    names = (
        args.cases.split(",")
        if args.cases
        else (["meridian-sdr-long"] if args.stage == "long-preflight" else sorted(SHORT_CASES))
    )
    cases = manifest_cases(args.manifest, names)
    if args.stage == "structural":
        structural(cases, args.output)
    elif args.stage == "long-preflight":
        if len(cases) != 1 or cases[0]["id"] != "meridian-sdr-long":
            raise ValueError("long preflight requires the original Meridian SDR source")
        long_preflight(cases[0], args.output)
    else:
        if args.freeze is None:
            parser.error("final holdout run requires --freeze from the finalized policy")
        if any(case["id"] not in SHORT_CASES for case in cases):
            raise ValueError("short native holdout runner must not mix the long workflow")
        run(
            cases,
            args.output,
            freeze_metadata(args.freeze),
            args.budgets,
            args.fractions,
            args.reference_seconds,
            resume=args.resume,
            ab_av1=args.ab_av1,
            baseline_budget=args.baseline_budget,
            temporal_baseline=args.temporal_baseline,
        )


if __name__ == "__main__":
    main()
