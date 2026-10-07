"""Post-score native export-quality characterization, without model tuning.

Compare previously completed continuous and independent-segment exports against
their identical 130-frame Y4M source. PSNR/SSIM are signal metrics; unavailable
VMAF and subjective viewing are not silently replaced by these measurements.
Quality analysis is separate from the earlier controller compute budget.
"""

import argparse
import csv
from datetime import datetime, timezone
from fractions import Fraction
import hashlib
import json
import math
from pathlib import Path
import re
import statistics
import subprocess
import time


ROOT = Path(__file__).resolve().parents[1]
REFERENCES = ROOT / "results/industrial-core-2026-10-03/holdout/references.json"
MANIFEST = ROOT / "artifacts/industrial-v1/manifest-test_reserved-sdr_8bit.json"
OUTPUT = ROOT / "results/industrial-core-2026-10-03/quality"
CASES = (
    "scene-composition",
    "mobile-sharing",
    "artistic-intro",
    "noise-ocean-game",
    "shaky-baseball-4k",
    "shaky-fireworks-4k",
    "shaky-walk",
    "trees-grass",
    "walking-street",
)
POLICY = {
    "version": "post-score-native-130-frame-psnr-ssim-v1",
    "purpose": "characterize export semantics after scoring; no prediction or tuning",
    "reference": "same verified source, native dimensions, original frame rate, yuv420p 8 bit",
    "pairing": "all source and output frames in presentation order, normalized equal frame-index clocks",
    "frames": 130,
    "metrics": ["FFmpeg_psnr_full_yuv", "FFmpeg_ssim_full_yuv"],
    "threads_per_decoder": 1,
    "filter_complex_threads": 1,
    "filter_threads": 1,
    "vmaf_policy": "record filter availability; do not substitute PSNR/SSIM for perceptual acceptance",
    "quality_analysis_in_controller_budget": False,
    "resource_policy": "serial metric jobs, nice=10, wait for other active FFmpeg processes",
    "parameter_policy": "read completed CRF23 medium native references; no encoding or parameter changes",
}


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".partial")
    temporary.write_text(json.dumps(value, indent=2, allow_nan=False) + "\n")
    temporary.replace(path)


def sha256(path):
    value = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            value.update(block)
    return value.hexdigest()


def fingerprint(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def active_ffmpeg():
    active = []
    for path in Path("/proc").iterdir():
        if not path.name.isdigit():
            continue
        try:
            args = (path / "cmdline").read_bytes().split(b"\0")
            if args[0] and Path(args[0].decode()).name == "ffmpeg":
                active.append({"pid": int(path.name), "command": [x.decode() for x in args if x]})
        except (OSError, UnicodeDecodeError):
            pass
    return active


def wait_for_slot(output):
    started = time.monotonic()
    last_report = -math.inf
    while active := active_ffmpeg():
        if time.monotonic() - last_report >= 30:
            event = {"utc": utc_now(), "kind": "wait_for_other_ffmpeg", "active": active}
            with (output / "resource-waits.jsonl").open("a") as stream:
                stream.write(json.dumps(event) + "\n")
            print(
                json.dumps({"kind": event["kind"], "active_pids": [p["pid"] for p in active]}),
                flush=True,
            )
            last_report = time.monotonic()
        time.sleep(5)
    return time.monotonic() - started


def inspect(path, *, packets):
    command = ["ffprobe", "-v", "error", "-threads", "1", "-select_streams", "v:0"]
    if packets:
        command += ["-count_packets"]
    command += [
        "-show_entries",
        "stream=codec_name,width,height,pix_fmt,avg_frame_rate,r_frame_rate,nb_frames,nb_read_packets,duration,color_range,color_space,color_transfer,color_primaries",
        "-of",
        "json",
        str(path),
    ]
    result = subprocess.run(command, check=True, capture_output=True, text=True)
    streams = json.loads(result.stdout)["streams"]
    if len(streams) != 1:
        raise ValueError("exactly one main video stream required")
    return {"video": streams[0], "command": command}


def count_y4m_frames(path, video):
    """Count real frame records without decoding a raw Y4M source twice."""
    if video["pix_fmt"] != "yuv420p" or video["width"] % 2 or video["height"] % 2:
        raise ValueError("this characterization requires even-dimension eight-bit YUV420")
    width, height = video["width"], video["height"]
    frame_bytes = width * height + 2 * (width // 2) * (height // 2)
    size = path.stat().st_size
    frames = 0
    with path.open("rb") as stream:
        header = stream.readline(4096)
        if not header.startswith(b"YUV4MPEG2 ") or not header.endswith(b"\n"):
            raise ValueError("regular Y4M source required")
        tokens = header.decode().split()
        if f"W{width}" not in tokens or f"H{height}" not in tokens:
            raise ValueError("Y4M dimensions disagree with ffprobe")
        while line := stream.readline(4096):
            if not line.endswith(b"\n") or not (line == b"FRAME\n" or line.startswith(b"FRAME ")):
                raise ValueError("invalid Y4M frame header")
            if stream.tell() + frame_bytes > size:
                raise ValueError("truncated Y4M frame")
            stream.seek(frame_bytes, 1)
            frames += 1
        if stream.tell() != size:
            raise ValueError("Y4M counting did not consume exact source bytes")
    return {
        "frames": frames,
        "source_header": header.decode().rstrip(),
        "frame_payload_bytes": frame_bytes,
    }


def verify_inputs(case, references):
    started = time.monotonic()
    source = Path(case["source"])
    source_hash = sha256(source)
    if source_hash != case["sha256"]:
        raise ValueError(f"source SHA mismatch: {case['id']}")
    source_info = inspect(source, packets=False)
    source_video = source_info["video"]
    count = count_y4m_frames(source, source_video)
    if count["frames"] != 130:
        raise ValueError("source does not contain exactly 130 frames")
    rate = Fraction(source_video["avg_frame_rate"])
    modes = {}
    for mode in ("continuous", "segmented"):
        reference = references[f"{case['id']}-{mode}"]
        estimate = reference["answer"]["estimate"]
        configuration = reference["answer"]["requested"]["configuration"]
        compute = reference["answer"]["requested"]["compute"]
        if (
            configuration["crf"] != 23
            or configuration["preset"] != "medium"
            or configuration["video_filter"] is not None
        ):
            raise ValueError("reference is not the frozen native CRF23 medium target")
        if compute["threads"] != 1 or configuration["export_mode"] != mode:
            raise ValueError("reference thread or export contract mismatch")
        if estimate["source_hash"] != source_hash:
            raise ValueError("continuous and segmented outputs must use the same source")
        artifact = Path(estimate["artifact"])
        artifact_hash = sha256(artifact)
        if (
            artifact_hash != estimate["sha256"]
            or artifact.stat().st_size != reference["file_bytes"]
        ):
            raise ValueError(f"completed reference SHA/size mismatch: {case['id']} {mode}")
        info = inspect(artifact, packets=True)
        video = info["video"]
        if video["width"] != source_video["width"] or video["height"] != source_video["height"]:
            raise ValueError("quality comparisons must remain at native source dimensions")
        if video["pix_fmt"] != "yuv420p" or Fraction(video["avg_frame_rate"]) != rate:
            raise ValueError("output pixel format or frame rate differs from source target")
        if int(video["nb_read_packets"]) != 130 or reference["inspection"]["frames"] != 130:
            raise ValueError("reference does not have 130 video packets/previously decoded frames")
        modes[mode] = {
            "artifact": str(artifact),
            "sha256": artifact_hash,
            "file_bytes": artifact.stat().st_size,
            "inspection": info,
            "export_configuration": configuration,
        }
    return {
        "source": str(source),
        "source_sha256": source_hash,
        "source_bytes": source.stat().st_size,
        "source_inspection": source_info,
        "source_frame_count": count,
        "frame_rate": str(rate),
        "modes": modes,
        "verification_wall_seconds": time.monotonic() - started,
    }


def number(value):
    result = float(value)
    return result if math.isfinite(result) else value


def parse_metric(stderr, metric, mode):
    prefix = rf"\[{metric}@{mode}\s+@[^\]]*\]"
    if metric == "psnr":
        matches = re.findall(
            prefix + r" PSNR y:(\S+) u:(\S+) v:(\S+) average:(\S+) min:(\S+) max:(\S+)", stderr
        )
        if len(matches) != 1:
            raise ValueError(f"expected one named PSNR summary: {mode}")
        return dict(
            zip(
                ("y_db", "u_db", "v_db", "average_db", "min_frame_db", "max_frame_db"),
                map(number, matches[0]),
            )
        )
    matches = re.findall(
        prefix
        + r" SSIM Y:(\S+) \([^)]*\) U:(\S+) \([^)]*\) V:(\S+) \([^)]*\) All:(\S+) \(([^)]*)\)",
        stderr,
    )
    if len(matches) != 1:
        raise ValueError(f"expected one named SSIM summary: {mode}")
    return dict(zip(("y", "u", "v", "all", "all_db"), map(number, matches[0])))


def stats_frames(path):
    rows = []
    for line in path.read_text().splitlines():
        fields = dict(re.findall(r"(\w+):([^\s]+)", line))
        if "n" in fields:
            rows.append(fields)
    if [int(r["n"]) for r in rows] != list(range(1, 131)):
        raise ValueError(f"metric statistics must contain all 130 frames: {path}")
    return rows


def measure(case_id, verified, output):
    case_output = output / "cases" / case_id
    case_output.mkdir(parents=True, exist_ok=True)
    rate = Fraction(verified["frame_rate"])
    norm = f"format=pix_fmts=yuv420p,settb=AVTB,setpts=N/(({rate.numerator}/{rate.denominator})*TB)"
    graph = ";".join(
        [
            f"[0:v:0]{norm},split=4[src_cp][src_cs][src_sp][src_ss]",
            f"[1:v:0]{norm},split=2[cont_p][cont_s]",
            f"[2:v:0]{norm},split=2[seg_p][seg_s]",
            "[cont_p][src_cp]psnr@continuous=stats_file=continuous-psnr.txt:stats_version=2:output_max=1:shortest=1:repeatlast=0[cp]",
            "[cont_s][src_cs]ssim@continuous=stats_file=continuous-ssim.txt:shortest=1:repeatlast=0[cs]",
            "[seg_p][src_sp]psnr@segmented=stats_file=segmented-psnr.txt:stats_version=2:output_max=1:shortest=1:repeatlast=0[sp]",
            "[seg_s][src_ss]ssim@segmented=stats_file=segmented-ssim.txt:shortest=1:repeatlast=0[ss]",
        ]
    )
    command = [
        "nice",
        "-n",
        "10",
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "info",
        "-nostdin",
        "-xerror",
        "-nostats",
        "-filter_threads",
        "1",
        "-filter_complex_threads",
        "1",
    ]
    for path in (
        verified["source"],
        verified["modes"]["continuous"]["artifact"],
        verified["modes"]["segmented"]["artifact"],
    ):
        command += ["-threads", "1", "-noautorotate", "-i", path]
    command += ["-filter_complex", graph]
    for label in ("cp", "cs", "sp", "ss"):
        command += ["-map", f"[{label}]"]
    command += ["-an", "-sn", "-dn", "-threads", "1", "-fps_mode", "passthrough", "-f", "null", "-"]
    wait = wait_for_slot(output)
    started = time.monotonic()
    process = subprocess.run(command, cwd=case_output, capture_output=True, text=True)
    wall = time.monotonic() - started
    (case_output / "ffmpeg.log").write_text(process.stderr)
    save(
        case_output / "command.json",
        {"command": command, "cwd": str(case_output), "returncode": process.returncode},
    )
    if process.returncode:
        raise RuntimeError(process.stderr[-4000:])
    metrics = {}
    for mode in ("continuous", "segmented"):
        psnr_stats = stats_frames(case_output / f"{mode}-psnr.txt")
        ssim_stats = stats_frames(case_output / f"{mode}-ssim.txt")
        psnr = parse_metric(process.stderr, "psnr", mode)
        ssim = parse_metric(process.stderr, "ssim", mode)
        ssim_frames = [float(row["All"]) for row in ssim_stats]
        psnr["stats_frames"] = len(psnr_stats)
        ssim.update(
            stats_frames=len(ssim_stats),
            min_frame_all=min(ssim_frames),
            max_frame_all=max(ssim_frames),
        )
        metrics[mode] = {"psnr": psnr, "ssim": ssim}
    delta = {
        "file_bytes": verified["modes"]["segmented"]["file_bytes"]
        - verified["modes"]["continuous"]["file_bytes"],
        "file_relative": verified["modes"]["segmented"]["file_bytes"]
        / verified["modes"]["continuous"]["file_bytes"]
        - 1,
        "psnr_average_db": metrics["segmented"]["psnr"]["average_db"]
        - metrics["continuous"]["psnr"]["average_db"],
        "psnr_y_db": metrics["segmented"]["psnr"]["y_db"] - metrics["continuous"]["psnr"]["y_db"],
        "ssim_all": metrics["segmented"]["ssim"]["all"] - metrics["continuous"]["ssim"]["all"],
        "ssim_y": metrics["segmented"]["ssim"]["y"] - metrics["continuous"]["ssim"]["y"],
    }
    record = {
        "case": case_id,
        "analysis_kind": "post-score_quality_not_prediction_tuning",
        "verified_inputs": verified,
        "metrics_against_source": metrics,
        "segmented_minus_continuous": delta,
        "decoded_source_frames": 130,
        "decoded_continuous_frames": 130,
        "decoded_segmented_frames": 130,
        "metrics_wall_seconds": wall,
        "resource_wait_seconds": wait,
        "quality_analysis_in_controller_budget": False,
        "timing_scope": "shared cloud host with other API/development work; not isolated latency",
        "policy_fingerprint": fingerprint(POLICY),
    }
    save(case_output / "result.json", record)
    return record


def summarize(records, metadata, output):
    deltas = [r["segmented_minus_continuous"] for r in records]
    summary = {
        "metadata": metadata,
        "cases": len(records),
        "segmented_file_relative_median": statistics.median(d["file_relative"] for d in deltas),
        "segmented_file_relative_min": min(d["file_relative"] for d in deltas),
        "segmented_file_relative_max": max(d["file_relative"] for d in deltas),
        "segmented_psnr_average_db_delta_median": statistics.median(
            d["psnr_average_db"] for d in deltas
        ),
        "segmented_psnr_average_db_delta_min": min(d["psnr_average_db"] for d in deltas),
        "segmented_psnr_average_db_delta_max": max(d["psnr_average_db"] for d in deltas),
        "segmented_ssim_all_delta_median": statistics.median(d["ssim_all"] for d in deltas),
        "segmented_ssim_all_delta_min": min(d["ssim_all"] for d in deltas),
        "segmented_ssim_all_delta_max": max(d["ssim_all"] for d in deltas),
        "metric_compute_seconds_total": sum(r["metrics_wall_seconds"] for r in records),
        "source_artifact_verification_seconds_total": sum(
            r["verified_inputs"]["verification_wall_seconds"] for r in records
        ),
        "perceptual_acceptance": "not established; PSNR/SSIM are objective signal metrics, not subjective review",
    }
    save(output / "summary.json", summary)
    fields = [
        "case",
        "width",
        "height",
        "fps",
        "frames",
        "continuous_bytes",
        "segmented_bytes",
        "file_delta_relative",
        "continuous_psnr_db",
        "segmented_psnr_db",
        "psnr_delta_db",
        "continuous_ssim_all",
        "segmented_ssim_all",
        "ssim_delta_all",
    ]
    rows = []
    for record in records:
        verified = record["verified_inputs"]
        metrics = record["metrics_against_source"]
        delta = record["segmented_minus_continuous"]
        video = verified["source_inspection"]["video"]
        rows.append(
            {
                "case": record["case"],
                "width": video["width"],
                "height": video["height"],
                "fps": verified["frame_rate"],
                "frames": 130,
                "continuous_bytes": verified["modes"]["continuous"]["file_bytes"],
                "segmented_bytes": verified["modes"]["segmented"]["file_bytes"],
                "file_delta_relative": delta["file_relative"],
                "continuous_psnr_db": metrics["continuous"]["psnr"]["average_db"],
                "segmented_psnr_db": metrics["segmented"]["psnr"]["average_db"],
                "psnr_delta_db": delta["psnr_average_db"],
                "continuous_ssim_all": metrics["continuous"]["ssim"]["all"],
                "segmented_ssim_all": metrics["segmented"]["ssim"]["all"],
                "ssim_delta_all": delta["ssim_all"],
            }
        )
    with (output / "comparison.csv").open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    print(json.dumps({k: v for k, v in summary.items() if k != "metadata"}), flush=True)


def verify_aggregates(records, output):
    """Check logged aggregates against independently parsed per-frame statistics."""
    checks = []
    for record in records:
        for mode in ("continuous", "segmented"):
            folder = output / "cases" / record["case"]
            frames = stats_frames(folder / f"{mode}-psnr.txt")
            mean_mse = statistics.fmean(float(frame["mse_avg"]) for frame in frames)
            # stats_version=2 prints MSE to two decimals, so preserve that
            # quantization interval instead of inventing a quality tolerance.
            lower = 10 * math.log10(255**2 / (mean_mse + 0.005))
            upper = 10 * math.log10(255**2 / (mean_mse - 0.005)) if mean_mse > 0.005 else math.inf
            logged = record["metrics_against_source"][mode]["psnr"]["average_db"]
            if not isinstance(logged, (int, float)) or not lower - 1e-6 <= logged <= upper + 1e-6:
                raise ValueError("logged PSNR is outside per-frame MSE rounding interval")
            ssim_frames = stats_frames(folder / f"{mode}-ssim.txt")
            difference = abs(
                statistics.fmean(float(f["All"]) for f in ssim_frames)
                - record["metrics_against_source"][mode]["ssim"]["all"]
            )
            if difference > 1.000001e-6:
                raise ValueError("logged SSIM disagrees with its per-frame mean")
            checks.append(
                {
                    "case": record["case"],
                    "mode": mode,
                    "metric_frames": len(frames),
                    "logged_psnr_within_frame_mse_quantization_bounds": True,
                    "ssim_frame_mean_difference": difference,
                }
            )
    save(
        output / "verification.json",
        {
            "all_output_sha_verified_before_measurement": True,
            "all_source_sha_verified_before_measurement": True,
            "all_source_frame_records_exactly_130": True,
            "all_metric_frame_numbers_exact_1_to_130": True,
            "aggregation_checks": checks,
            "no_new_encodes_or_parameter_changes": True,
        },
    )


def study(args):
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    references = json.loads(REFERENCES.read_text())
    all_cases = {c["id"]: c for c in json.loads(MANIFEST.read_text())["cases"]}
    names = set(CASES) if not args.cases else set(args.cases.split(","))
    if not names or not names <= set(CASES):
        raise ValueError("only the nine already-scored native short cases are allowed")
    filters = subprocess.run(
        ["ffmpeg", "-hide_banner", "-filters"], check=True, capture_output=True, text=True
    )
    filter_text = filters.stdout + filters.stderr
    if not all(re.search(rf"\b{metric}\b", filter_text) for metric in ("psnr", "ssim")):
        raise RuntimeError("PSNR and SSIM filters are required")
    (output / "ffmpeg-filters.txt").write_text(filter_text)
    metadata = {
        "created_at_utc": utc_now(),
        "policy": POLICY,
        "policy_fingerprint": fingerprint(POLICY),
        "case_ids": [case for case in CASES if case in names],
        "reference_index": str(REFERENCES),
        "reference_index_sha256": sha256(REFERENCES),
        "source_manifest": str(MANIFEST),
        "source_manifest_sha256": sha256(MANIFEST),
        "ffmpeg": subprocess.check_output(["ffmpeg", "-version"], text=True),
        "libvmaf_available": bool(re.search(r"\blibvmaf\b", filter_text)),
        "vmaf_measured": False,
        "vmaf_note": "filter unavailable in this build"
        if not re.search(r"\blibvmaf\b", filter_text)
        else "available, but this requested characterization measures PSNR/SSIM only",
        "quality_analysis_in_controller_budget": False,
        "timing_scope": "shared cloud host with other API/development work; not isolated latency",
        "cpu_quota": Path("/sys/fs/cgroup/cpu.max").read_text().strip(),
        "memory_limit_bytes": int(Path("/sys/fs/cgroup/memory.max").read_text()),
    }
    save(output / "metadata.json", metadata)
    records = []
    for case_id in metadata["case_ids"]:
        verified = verify_inputs(all_cases[case_id], references)
        save(output / "cases" / case_id / "verified-inputs.json", verified)
        record = measure(case_id, verified, output)
        records.append(record)
        save(output / "results.json", records)
        print(json.dumps({"case": case_id, **record["segmented_minus_continuous"]}), flush=True)
    verify_aggregates(records, output)
    summarize(records, metadata, output)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=OUTPUT)
    parser.add_argument("--cases", help="optional subset of the nine fixed post-score native cases")
    study(parser.parse_args())


if __name__ == "__main__":
    main()
