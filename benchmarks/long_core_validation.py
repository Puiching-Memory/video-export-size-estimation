"""One frozen, label-free long-source final test followed by a full reference.

The source is the original 719-second Meridian SDR file. The declared output is
640x360 at 12 fps, video only; this is not a native 4K output benchmark. Each
prediction uses a separate empty cache. No reference is generated until all
four requested predictions have been persisted. Existing stages are refused.
"""

import argparse
import hashlib
import json
import shutil
import subprocess
import time
from dataclasses import asdict
from datetime import datetime, timezone
from fractions import Fraction
from pathlib import Path
from unittest.mock import patch

from vsize import ComputeBudget, EncodeSpec, Request
from vsize.assets import PreparedAsset
from vsize.engine import Engine
from vsize.media import Media
from vsize.runtime import Cache, Deadline, digest


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "artifacts/industrial-v1/meridian-sdr-long/Meridian_3840x2160_5994fps_SDR.mp4"
OUTPUT = ROOT / "results/industrial-long-2026-10-03"
FREEZE = ROOT / "artifacts/core-validation/v2-freeze.json"
SOURCE_SHA = "526b5dad800cdbd7f208bd35d717928bda0ee712a2dd21a6326a43a80717e496"
FROZEN_SHA = "f39dca1c9f04afec908f1171daa898e2bf4c3fa82073cdb5908d8b9eec562ee2"
SEED = 20261003
JOBS = [(30, "temporal"), (30, "content"), (60, "temporal"), (60, "content")]


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, allow_nan=False) + "\n")


def emit(value):
    print(json.dumps(value, allow_nan=False), flush=True)


def utc():
    return datetime.now(timezone.utc).isoformat()


def source_code_sha():
    value = hashlib.sha256()
    for path in sorted((ROOT / "src").rglob("*.py")):
        value.update(path.relative_to(ROOT).as_posix().encode())
        value.update(path.read_bytes())
    return value.hexdigest()


def check_freeze():
    value = source_code_sha()
    if value != FROZEN_SHA or json.loads(FREEZE.read_text())["source_code_sha256"] != value:
        raise RuntimeError("the frozen v2 library changed; refusing this final test")
    return value


def spec():
    return EncodeSpec(
        SOURCE,
        crf=23,
        preset="veryfast",
        video_filter="fps=12,scale=640:360",
        drop_audio=True,
        export_mode="continuous",
    )


def request(budget, plan):
    return Request(
        spec(),
        compute=ComputeBudget(
            wall_seconds=budget,
            max_probes=6,
            threads=1,
            max_encode_fraction=0.2,
        ),
        sample_seconds=4,
        seed=SEED,
        probe_mode="auto",
        sampling_plan=plan,
    )


def serial_request(value):
    result = asdict(value)
    result["encode"]["source"] = str(result["encode"]["source"])
    return result


def processes():
    """Observe competing codecs without claiming isolated host timings."""
    records = []
    for path in Path("/proc").iterdir():
        if not path.name.isdigit():
            continue
        try:
            command = (path / "cmdline").read_bytes().split(b"\0")
            executable = Path(command[0].decode()).name if command[0] else ""
            if executable not in {"ffmpeg", "ffprobe"}:
                continue
            records.append({"pid": int(path.name), "command": [a.decode() for a in command if a]})
        except (OSError, UnicodeDecodeError):
            pass
    return records


def stage_metadata(stage):
    return {
        "stage": stage,
        "started_utc": utc(),
        "source_code_sha256": check_freeze(),
        "runner_sha256": digest(Path(__file__)),
        "ffmpeg": subprocess.check_output(["ffmpeg", "-version"], text=True).splitlines()[0],
        "cpu_max": Path("/sys/fs/cgroup/cpu.max").read_text().strip(),
        "memory_max": Path("/sys/fs/cgroup/memory.max").read_text().strip(),
        "concurrent_media_processes_at_start": processes(),
        "latency_scope": "shared cloud CPU quota; page cache not flushed; one FFmpeg per runner",
    }


def snapshot(dest):
    shutil.copytree(
        ROOT / "src",
        dest / ".vsize-cache/code_snapshot/src",
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    shutil.copyfile(Path(__file__), dest / ".vsize-cache/code_snapshot/long_core_validation.py")
    shutil.copyfile(FREEZE, dest / ".vsize-cache/code_snapshot/v2-freeze.json")


def prepare(asset, dest):
    if asset.sha256 != SOURCE_SHA or asset.size_bytes != 1299914020:
        raise RuntimeError("original full Meridian input checksum/size changed")
    save(
        dest / "prepared-asset.json",
        {
            "source": str(SOURCE),
            "sha256": asset.sha256,
            "bytes": asset.size_bytes,
            "ingest_seconds": asset.ingest_seconds,
            "ingest_scope": "sealed full-file immutable snapshot and SHA; outside prepared request budgets",
        },
    )


def predict(output):
    check_freeze()
    output.mkdir(parents=True, exist_ok=False)
    manifest = {
        "schema_version": 1,
        "created_utc": utc(),
        "source_code_sha256": FROZEN_SHA,
        "source": {
            "id": "meridian-sdr-long",
            "group_id": "meridian-2016",
            "split": "test_reserved",
            "path": str(SOURCE),
            "sha256": SOURCE_SHA,
            "bytes": 1299914020,
            "video_duration_seconds": 718.933333,
            "width": 3840,
            "height": 2160,
            "frame_rate": "60000/1001",
        },
        "target": spec().configuration(),
        "requests": [serial_request(request(budget, plan)) for budget, plan in JOBS],
        "reference_deadline_seconds": 1800,
        "reference_policy": "one full continuous export, no temporal window, after all predictions saved",
        "prior_label_exposure": "three throughput windows only; full target size not generated/read",
        "claim_scope": "one long held-out source, downscaled 640x360 12fps output; uncalibrated estimator",
    }
    save(output / "manifest.json", manifest)
    dest = output / "predictions"
    dest.mkdir()
    save(dest / "metadata.json", stage_metadata("predictions"))
    snapshot(dest)
    emit({"type": "manifest_frozen", "output": str(output), "source_sha256": SOURCE_SHA})
    rows = []
    with PreparedAsset.from_path(SOURCE) as asset:
        prepare(asset, dest)
        media = Media(spec(), 1, Cache(dest / ".vsize-cache/metadata"), Deadline(30), asset=asset)
        save(dest / "source-metadata.json", media.info)
        duration = media.duration
        for budget, plan in JOBS:
            check_freeze()
            job = f"{plan}-{budget}s"
            cache = dest / ".vsize-cache" / job
            cache.mkdir(exist_ok=False)
            updates, previews = [], []
            original_preview = Media.preview_features

            def observe_preview(instance, *args, **kwargs):
                started = time.monotonic()
                observed = {"started_utc": utc(), "status": "running"}
                previews.append(observed)
                try:
                    result = original_preview(instance, *args, **kwargs)
                    observed["status"] = "completed"
                    observed["returned_metadata"] = result[1]
                    return result
                except Exception as error:
                    observed["status"] = "failed"
                    observed["failure_type"] = type(error).__name__
                    observed["failure"] = str(error)
                    raise
                finally:
                    observed["elapsed_seconds"] = time.monotonic() - started

            def update(value):
                updates.append(value)
                emit(
                    {
                        "type": "estimate_update",
                        "job": job,
                        "elapsed_seconds": value.get("elapsed_seconds"),
                        "estimated_bytes": value.get("estimated_bytes"),
                    }
                )

            emit(
                {
                    "type": "request_start",
                    "job": job,
                    "utc": utc(),
                    "concurrent_media_processes": processes(),
                }
            )
            started = time.monotonic()
            with patch.object(Media, "preview_features", observe_preview):
                answer = Engine(cache).run(request(budget, plan), on_update=update, asset=asset)
            wall = time.monotonic() - started
            estimate = answer["estimate"]
            row = {
                "job": job,
                "sampling_plan": plan,
                "wall_budget_seconds": budget,
                "state": answer["status"],
                "stop_reason": answer.get("stop_reason"),
                "unmet": answer["unmet"],
                "prediction_bytes": estimate["estimated_bytes"] if estimate else None,
                "uncertainty_kind": estimate["uncertainty"]["kind"] if estimate else None,
                "wall_seconds": wall,
                "engine_elapsed_seconds": answer["elapsed_seconds"],
                "first_estimate_seconds": answer["first_estimate_seconds"],
                "prepared_ingest_seconds": asset.ingest_seconds,
                "ingest_plus_request_seconds": asset.ingest_seconds + wall,
                "charged_encode_seconds": answer["attempted_encode_seconds"],
                "charged_encode_fraction": answer["attempted_encode_seconds"] / duration,
                "attempted_audio_seconds": answer["attempted_audio_seconds"],
                "total_probe_count": answer["total_probe_count"],
                "pilot_count": answer.get("pilot_count"),
                "selected_probe_mode": estimate.get("probe_mode") if estimate else None,
                "prediction_family": answer.get("prediction_family"),
                "content_discovery": answer.get("content_discovery"),
                "preview_observations": previews,
                "source_duration_seconds": duration,
                "cache_initially_empty": True,
                "source_code_sha256": check_freeze(),
            }
            save(dest / f"{job}-answer.json", answer)
            save(dest / f"{job}-updates.json", updates)
            save(dest / f"{job}.json", row)
            rows.append(row)
            save(dest / "results.json", rows)
            emit({"type": "prediction_saved", **row})
    if len(rows) != len(JOBS):
        raise RuntimeError("not all predictions were saved")
    save(
        dest / "complete.json",
        {
            "completed_utc": utc(),
            "jobs": len(rows),
            "results_sha256": digest(dest / "results.json"),
        },
    )


class ObservedReferenceDeadline(Deadline):
    """Add progress reporting only, preserving Media.encode's full target."""

    def __init__(self, seconds, dest):
        super().__init__(seconds)
        self.dest = dest

    def run(self, command):
        if command[0] == "ffmpeg" and "-benchmark" in command:
            command = [
                *command[:-1],
                "-progress",
                str(self.dest / "encode-progress.txt"),
                "-stats_period",
                "5",
                command[-1],
            ]
            if "-ss" in command or "-t" in command:
                raise RuntimeError("a shortened reference is forbidden")
            save(self.dest / "encode-command.json", command)
        try:
            result = super().run(command)
        except Exception as error:
            save(
                self.dest / "last-process-failure.json",
                {
                    "command": command,
                    "failure_type": type(error).__name__,
                    "failure": str(error),
                    "deadline_elapsed_seconds": self.elapsed,
                },
            )
            raise
        if command[0] == "ffmpeg" and "-benchmark" in command:
            (self.dest / "encode-stderr.txt").write_bytes(result.stderr)
        return result


def reference(output):
    check_freeze()
    predicted = output / "predictions"
    completion = json.loads((predicted / "complete.json").read_text())
    if digest(predicted / "results.json") != completion["results_sha256"]:
        raise RuntimeError("saved predictions changed")
    rows = json.loads((predicted / "results.json").read_text())
    if {row["job"] for row in rows} != {f"{p}-{b}s" for b, p in JOBS}:
        raise RuntimeError("all four predictions must precede the reference")
    dest = output / "reference"
    dest.mkdir(exist_ok=False)
    save(dest / "metadata.json", stage_metadata("full_reference"))
    snapshot(dest)
    deadline = None
    try:
        with PreparedAsset.from_path(SOURCE) as asset:
            prepare(asset, dest)
            deadline = ObservedReferenceDeadline(1800, dest)
            media = Media(spec(), 1, Cache(dest / ".vsize-cache/full"), deadline, asset=asset)
            emit(
                {
                    "type": "full_reference_start",
                    "utc": utc(),
                    "source_duration_seconds": media.duration,
                    "deadline_seconds": 1800,
                    "concurrent_media_processes": processes(),
                }
            )
            record = media.encode(23)
            record["reference_stage_wall_seconds"] = deadline.elapsed
            record["reference_ingest_seconds"] = asset.ingest_seconds
            save(dest / "record.json", record)
        artifact = Path(record["artifact"])
        inspected = json.loads(
            Deadline(60)
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
        save(dest / "output-metadata.json", inspected)
        decode_command = [
            "ffmpeg",
            "-hide_banner",
            "-v",
            "error",
            "-nostdin",
            "-xerror",
            "-threads",
            "1",
            "-i",
            str(artifact),
            "-map",
            "0:v:0",
            "-an",
            "-sn",
            "-dn",
            "-fps_mode",
            "passthrough",
            "-threads",
            "1",
            "-progress",
            str(dest / "decode-progress.txt"),
            "-stats_period",
            "5",
            "-f",
            "null",
            "-",
        ]
        save(dest / "decode-command.json", decode_command)
        decode_deadline = Deadline(180)
        decoded = decode_deadline.run(decode_command)
        (dest / "decode-stderr.txt").write_bytes(decoded.stderr)
        fields = {}
        for line in (dest / "decode-progress.txt").read_text().splitlines():
            if "=" in line:
                key, value = line.split("=", 1)
                fields[key] = value
        video = next(s for s in inspected["streams"] if s["codec_type"] == "video")
        duration_guard = 1 / 12 + 1001 / 60000
        verification = {
            "sha256": digest(artifact),
            "file_bytes": artifact.stat().st_size,
            "full_output_decode_completed": fields.get("progress") == "end",
            "decoded_frames": int(fields["frame"]),
            "reported_frames": record["video_frames"],
            "output_duration_seconds": float(video["duration"]),
            "source_video_duration_seconds": rows[0]["source_duration_seconds"],
            "allowed_duration_rounding_seconds": duration_guard,
            "output_width": video["width"],
            "output_height": video["height"],
            "output_frame_rate": video["avg_frame_rate"],
            "output_audio_streams": sum(s["codec_type"] == "audio" for s in inspected["streams"]),
            "decode_wall_seconds": decode_deadline.elapsed,
            "source_code_sha256": check_freeze(),
        }
        verification["valid"] = (
            verification["sha256"] == record["sha256"]
            and verification["file_bytes"] == record["file_bytes"]
            and verification["full_output_decode_completed"]
            and verification["decoded_frames"] == record["video_frames"]
            and abs(verification["output_duration_seconds"] - rows[0]["source_duration_seconds"])
            <= duration_guard
            and (video["width"], video["height"]) == (640, 360)
            and Fraction(video["avg_frame_rate"]) == 12
            and verification["output_audio_streams"] == 0
            and record["window"] is None
            and not record["cache_hit"]
        )
        save(dest / "verification.json", verification)
        if not verification["valid"]:
            raise RuntimeError("the complete reference failed verification; accuracy is unscored")
        evaluated = []
        for row in rows:
            value = row["prediction_bytes"]
            evaluated.append(
                {
                    **row,
                    "reference_bytes": record["file_bytes"],
                    "signed_error_percent": 100 * (value / record["file_bytes"] - 1)
                    if value is not None
                    else None,
                    "absolute_error_percent": 100 * abs(value / record["file_bytes"] - 1)
                    if value is not None
                    else None,
                    "point_within_ten_percent": abs(value / record["file_bytes"] - 1) <= 0.1
                    if value is not None
                    else False,
                    "scored": value is not None,
                }
            )
        save(output / "evaluated.json", evaluated)
        save(
            dest / "status.json",
            {
                "status": "verified_full_reference",
                "completed_utc": utc(),
                "reference_sha256": record["sha256"],
            },
        )
        emit(
            {
                "type": "full_reference_verified",
                "record": record,
                "verification": verification,
                "evaluated": evaluated,
            }
        )
    except Exception as error:
        failure = {
            "status": "failed_reference_unscored",
            "completed_utc": utc(),
            "failure_type": type(error).__name__,
            "failure": str(error),
            "reference_deadline_elapsed_seconds": deadline.elapsed if deadline else None,
        }
        save(dest / "status.json", failure)
        emit(failure)
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=["predict", "reference", "all"])
    parser.add_argument("--output", type=Path, default=OUTPUT)
    args = parser.parse_args()
    if args.stage in {"predict", "all"}:
        predict(args.output.resolve())
    if args.stage in {"reference", "all"}:
        reference(args.output.resolve())


if __name__ == "__main__":
    main()
