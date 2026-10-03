"""Frozen maintenance checks; never rerun old estimator accuracy experiments."""

import argparse
import hashlib
import json
import statistics
import struct
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from vsize.runtime import Deadline, canonical_key, digest
from vsize.toolchain import native_pipeline_fingerprint


ROOT = Path(__file__).resolve().parents[3]
DEST = Path(__file__).resolve().parent
EXPECTED = "b4440e98a293e38ffb1356a51068e1a75677ac0ba7e2e7417ec469d286f0ca3a"


def save(name, value):
    path = DEST / name
    if path.exists():
        raise FileExistsError(f"refusing to overwrite maintenance observation: {path}")
    path.write_text(json.dumps(value, indent=2, allow_nan=False) + "\n")


def source_sha():
    value = hashlib.sha256()
    for path in sorted((ROOT / "src").rglob("*.py")):
        value.update(path.relative_to(ROOT).as_posix().encode())
        value.update(path.read_bytes())
    result = value.hexdigest()
    if result != EXPECTED:
        raise RuntimeError("maintenance source changed from the final freeze")
    return result


def media_processes():
    result = []
    for path in Path("/proc").iterdir():
        if not path.name.isdigit():
            continue
        try:
            args = [p.decode() for p in (path / "cmdline").read_bytes().split(b"\0") if p]
            if args and Path(args[0]).name in {"ffmpeg", "ffprobe"}:
                result.append({"pid": int(path.name), "command": args})
        except (OSError, UnicodeDecodeError):
            pass
    return result


def boxes(path):
    data = path.read_bytes()
    result = []
    offset = 0
    while offset < len(data):
        size, kind = struct.unpack_from(">I4s", data, offset)
        header = 8
        if size == 1:
            size = struct.unpack_from(">Q", data, offset + 8)[0]
            header = 16
        if size == 0:
            size = len(data) - offset
        if size < header or offset + size > len(data):
            raise RuntimeError("invalid published MP4 top-level box")
        result.append(
            {"type": kind.decode("ascii"), "offset": offset, "bytes": size, "header_bytes": header}
        )
        offset += size
    return result


def main():
    plan = json.loads((DEST / "prepared-plan.json").read_text())
    save(
        "started.json",
        {
            "started_utc": datetime.now(timezone.utc).isoformat(),
            "source_sha256": source_sha(),
            "runner_sha256": digest(Path(__file__)),
            "source_snapshot_zip_sha256": digest(DEST.parent / "source-snapshot.zip"),
            "cpu_max": Path("/sys/fs/cgroup/cpu.max").read_text().strip(),
            "scope": "maintenance native-identity/runtime and publication, not prediction accuracy",
            "latency_scope": "shared machine with concurrent full suite; OS cache not cleared",
            "processes": media_processes(),
        },
    )
    fingerprints = []
    for number in range(1, 4):
        concurrent = media_processes()
        deadline = Deadline(60)
        started = time.monotonic()
        fingerprint = native_pipeline_fingerprint(deadline)
        elapsed = time.monotonic() - started
        record = {
            "trial": number,
            "elapsed_seconds": elapsed,
            "native_pipeline_sha256": canonical_key(fingerprint),
            "components_sha256": canonical_key({"components": fingerprint["components"]}),
            "component_count": len(fingerprint["components"]),
            "component_bytes_hashed": sum(c["bytes"] for c in fingerprint["components"]),
            "actual_fingerprint": fingerprint,
            "concurrent_media_processes": concurrent,
            "source_sha256": source_sha(),
            "cache_scope": "no application memoization; OS cache not cleared",
        }
        save(f"native-fingerprint-{number}.json", record)
        fingerprints.append(record)
        print(
            json.dumps(
                {
                    k: v
                    for k, v in record.items()
                    if k not in {"actual_fingerprint", "concurrent_media_processes"}
                }
            ),
            flush=True,
        )
    if len({r["native_pipeline_sha256"] for r in fingerprints}) != 1:
        raise RuntimeError("native components changed between trials")
    command = plan["cli_command"]
    save(
        "cli-command.json",
        {
            "command": command,
            "concurrent_media_processes": media_processes(),
            "source_sha256": source_sha(),
        },
    )
    if digest(Path(plan["source"])) != plan["expected_source_sha256"]:
        raise RuntimeError("original static fixture changed")
    started = time.monotonic()
    process = subprocess.run(command, capture_output=True, cwd=ROOT, timeout=60)
    outer_seconds = time.monotonic() - started
    (DEST / "cli-stdout.json").write_bytes(process.stdout)
    (DEST / "cli-stderr.txt").write_bytes(process.stderr)
    if process.returncode != 0:
        raise RuntimeError(f"CLI export failed with return code {process.returncode}")
    answer = json.loads(process.stdout)
    if answer["status"] != "satisfied" or answer["estimate"]["uncertainty"]["kind"] != "exact":
        raise RuntimeError("CLI did not publish a complete exact export")
    published = Path(answer["published_file"])
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
                str(published),
            ]
        )
        .stdout
    )
    save("published-metadata.json", inspection)
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
        str(published),
        "-map",
        "0:v:0",
        "-an",
        "-fps_mode",
        "passthrough",
        "-threads",
        "1",
        "-progress",
        str(DEST / "decode-progress.txt"),
        "-f",
        "null",
        "-",
    ]
    save("decode-command.json", decode_command)
    decode_started = time.monotonic()
    decoded = Deadline(30).run(decode_command)
    decode_seconds = time.monotonic() - decode_started
    (DEST / "decode-stderr.txt").write_bytes(decoded.stderr)
    fields = {}
    for line in (DEST / "decode-progress.txt").read_text().splitlines():
        if "=" in line:
            name, value = line.split("=", 1)
            fields[name] = value
    layout = boxes(published)
    payload = sum(b["bytes"] - b["header_bytes"] for b in layout if b["type"] == "mdat")
    init = sum(b["bytes"] for b in layout if b["type"] in {"ftyp", "moov"})
    fragments = sum(b["bytes"] for b in layout if b["type"] == "moof") + sum(
        b["header_bytes"] for b in layout if b["type"] == "mdat"
    )
    observed = {
        "init_bytes": init,
        "payload_bytes": payload,
        "fragment_bytes": fragments,
        "file_bytes": published.stat().st_size,
        "moof_count": sum(b["type"] == "moof" for b in layout),
        "mdat_count": sum(b["type"] == "mdat" for b in layout),
        "top_level_boxes": layout,
    }
    save("independent-box-accounting.json", observed)
    finish(
        plan,
        fingerprints,
        answer,
        inspection,
        fields,
        observed,
        outer_seconds,
        decode_seconds,
        process.returncode,
    )


def finish(
    plan,
    fingerprints,
    answer,
    inspection,
    fields,
    observed,
    outer_seconds,
    decode_seconds,
    exit_code,
):
    published = Path(answer["published_file"])
    artifact = Path(answer["estimate"]["artifact"])
    init, payload, fragments = (
        observed[k] for k in ("init_bytes", "payload_bytes", "fragment_bytes")
    )
    account = answer["estimate"]["byte_accounting"]
    video = next(s for s in inspection["streams"] if s["codec_type"] == "video")
    version = subprocess.check_output(["ffmpeg", "-version"]).decode()
    expected_toolchain = canonical_key(
        {"ffmpeg": version, "native_pipeline": fingerprints[0]["actual_fingerprint"]}
    )
    valid = (
        digest(published) == digest(artifact) == answer["estimate"]["sha256"]
        and published.stat().st_size == answer["estimate"]["estimated_bytes"]
        and fields["progress"] == "end"
        and int(fields["frame"]) == 576
        and ("nb_frames" not in video or int(video["nb_frames"]) == 576)
        and float(video["duration"]) == 24
        and answer["estimate"]["segment_count"] == 6
        and observed["moof_count"] == 6
        and init + payload + fragments == published.stat().st_size
        and init == account["init_bytes"]
        and fragments == account["fragment_bytes"]
        and payload == account["video_payload_bytes"] + account["audio_payload_bytes"]
        and account["video_samples"] == 576
        and account["audio_samples"] == 0
        and answer["toolchain_key"] == expected_toolchain
        and source_sha() == EXPECTED
    )
    summary = {
        "valid": valid,
        "source_sha256_before_after": source_sha(),
        "native_fingerprint_cost_seconds": [r["elapsed_seconds"] for r in fingerprints],
        "native_fingerprint_median_seconds": statistics.median(
            r["elapsed_seconds"] for r in fingerprints
        ),
        "native_pipeline_sha256": fingerprints[0]["native_pipeline_sha256"],
        "components_sha256": fingerprints[0]["components_sha256"],
        "component_count": fingerprints[0]["component_count"],
        "component_bytes_hashed_each_trial": fingerprints[0]["component_bytes_hashed"],
        "source_fixture_sha256": digest(Path(plan["source"])),
        "published_sha256": digest(published),
        "published_bytes": published.stat().st_size,
        "decoded_frames": int(fields["frame"]),
        "video_seconds": float(video["duration"]),
        "segment_count": answer["estimate"]["segment_count"],
        "byte_accounting": account,
        "independent_box_accounting": {k: v for k, v in observed.items() if k != "top_level_boxes"},
        "cli_exit_code": exit_code,
        "ingest_seconds": answer["prepared_asset"]["ingest_seconds"],
        "prediction_seconds": answer["elapsed_seconds"],
        "prediction_seconds_basis": "Engine request wall, including full six-segment encode and inspection; not low-budget sampled estimation",
        "first_estimate_seconds": answer["first_estimate_seconds"],
        "publication_seconds": answer["publication_seconds"],
        "cli_reported_end_to_end_seconds": answer["end_to_end_seconds"],
        "outer_subprocess_wall_seconds": outer_seconds,
        "complete_output_decode_seconds": decode_seconds,
        "timing_recovery_note": "CLI internal phase timings preserved; outer process/decode wall timings not retained when the initial validator failed"
        if outer_seconds is None
        else None,
        "metadata_reported_nb_frames": video.get("nb_frames"),
        "frame_count_verification_basis": "actual complete ffmpeg decode progress=end/frame576",
        "postprocessor_runner_sha256": digest(Path(__file__)),
        "native_components_match_cli_toolchain": answer["toolchain_key"] == expected_toolchain,
        "charged_output_video_seconds": answer["attempted_encode_seconds"],
        "scope": "maintenance runtime acceptance only; no old accuracy claim rerun or upgraded",
        "latency_scope": "shared machine, concurrent full suite; OS cache not cleared",
    }
    save("summary.json", summary)
    print(json.dumps(summary), flush=True)
    if not valid:
        raise RuntimeError("published export failed verification")


def finish_saved():
    source_sha()
    plan = json.loads((DEST / "prepared-plan.json").read_text())
    fingerprints = [
        json.loads((DEST / f"native-fingerprint-{n}.json").read_text()) for n in (1, 2, 3)
    ]
    answer = json.loads((DEST / "cli-stdout.json").read_text())
    inspection = json.loads((DEST / "published-metadata.json").read_text())
    observed = json.loads((DEST / "independent-box-accounting.json").read_text())
    fields = {}
    for line in (DEST / "decode-progress.txt").read_text().splitlines():
        if "=" in line:
            name, value = line.split("=", 1)
            fields[name] = value
    finish(plan, fingerprints, answer, inspection, fields, observed, None, None, 0)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--finish-saved",
        action="store_true",
        help="verify saved successful export without another encoding",
    )
    args = parser.parse_args()
    if args.finish_saved:
        finish_saved()
    else:
        main()
