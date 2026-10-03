#!/usr/bin/env python3
"""Development study of random access that preserves a full FPS filter's phase.

This does not modify or replace the frozen exporter.  Every window is compared
with a complete, unseeked FFmpeg fps-filter run using pixel SHA256 and integer
PTS.  Input packet timestamps are used only to choose conservative preroll.
"""

import argparse
import bisect
import hashlib
import json
import math
import random
import re
import statistics
import subprocess
import sys
import time
from fractions import Fraction
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from vsize.runtime import Deadline, digest  # noqa: E402


def save(path, value):
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n")


def source_hashes():
    return {
        str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted((ROOT / "src").rglob("*.py"))
    }


def ffmpeg_prefix():
    return [
        "ffmpeg", "-hide_banner", "-loglevel", "verbose", "-nostdin", "-xerror",
        "-benchmark", "-threads", "1", "-filter_threads", "1",
        "-filter_complex_threads", "1",
    ]


def run(command, output, stem, *, timeout=120):
    started = time.monotonic()
    result = Deadline(timeout).run(command)
    elapsed = time.monotonic() - started
    stderr = result.stderr.decode(errors="replace")
    (output / f"{stem}.stderr.log").write_text(stderr)
    diagnostic = re.search(
        r"Input stream #0:\d+ \(video\): (\d+) packets read.*?; (\d+) frames decoded",
        stderr,
    )
    cpu = re.search(r"bench: utime=([\d.]+)s stime=([\d.]+)s rtime=([\d.]+)s", stderr)
    return result.stdout, {
        "command": command,
        "wall_seconds": elapsed,
        "input_packets_read": int(diagnostic[1]) if diagnostic else None,
        "input_frames_decoded": int(diagnostic[2]) if diagnostic else None,
        "ffmpeg_user_seconds": float(cpu[1]) if cpu else None,
        "ffmpeg_system_seconds": float(cpu[2]) if cpu else None,
        "ffmpeg_rtime_seconds": float(cpu[3]) if cpu else None,
    }


def framehash(raw):
    text = raw.decode()
    match = re.search(r"#tb 0: (\d+)/(\d+)", text)
    if not match:
        raise ValueError("framehash did not include an integer output time base")
    rows = []
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        fields = [field.strip() for field in line.split(",")]
        rows.append({
            "dts": int(fields[1]), "pts": int(fields[2]),
            "duration": int(fields[3]), "bytes": int(fields[4]), "sha256": fields[5],
        })
    return Fraction(int(match[1]), int(match[2])), rows


def inspect(source):
    result = Deadline(120).run([
        "ffprobe", "-v", "error", "-threads", "1", "-select_streams", "v:0",
        "-show_packets", "-show_streams", "-show_format", "-show_entries",
        "stream=codec_name,width,height,time_base,start_pts,start_time,avg_frame_rate:"
        "packet=pts,dts,duration,flags:format=start_time,duration", "-of", "json", str(source),
    ])
    info = json.loads(result.stdout)
    stream = info["streams"][0]
    time_base = Fraction(stream["time_base"])
    start = Fraction(info["format"]["start_time"])
    ordered = sorted(info["packets"], key=lambda packet: int(packet["pts"]))
    timestamps = [int(packet["pts"]) * time_base - start for packet in ordered]
    if any(right <= left for left, right in zip(timestamps, timestamps[1:])):
        raise ValueError("strictly increasing presentation timestamps required")
    return info, timestamps


def near(value):
    """AV_ROUND_NEAR_INF for finite rational timestamps."""
    return math.floor(value + Fraction(1, 2)) if value >= 0 else math.ceil(value - Fraction(1, 2))


def seek_text(value):
    # FFmpeg's CLI input -ss has microsecond precision.  Round down so the
    # selected guard sample cannot be accidentally skipped by decimal rounding.
    microseconds = max(0, math.floor(value * 1_000_000))
    return f"{microseconds // 1_000_000}.{microseconds % 1_000_000:06d}"


def output_options(rate, video_filter, frames=None):
    options = [
        "-map", "0:v:0", "-an", "-sn", "-dn", "-vf", video_filter,
        "-fps_mode", "passthrough", "-c:v", "rawvideo", "-threads", "1",
        "-pix_fmt", "yuv420p", "-enc_time_base", str(1 / rate),
    ]
    if frames is not None:
        options += ["-frames:v", str(frames)]
    return options + ["-f", "framehash", "-hash", "sha256", "pipe:1"]


def command(source, rate, window, variant, timestamps):
    start, count, first_tick = window["start_frame"], window["frame_count"], window["first_pts"]
    end_tick = first_tick + count
    fps = f"fps={rate}"
    seek = None
    extra = []
    if variant == "prefix_frames":
        video_filter = f"{fps},trim=start_frame={start}:end_frame={start + count}"
    elif variant == "local_reset":
        seek = Fraction(first_tick, 1) / rate
        extra = ["-ss", seek_text(seek)]
        video_filter = f"{fps},setpts=PTS-STARTPTS+{first_tick}"
    else:
        if variant == "fixed_guard":
            seek = max(Fraction(0), Fraction(first_tick, 1) / rate - Fraction(1, 2))
        else:
            quantized = [near(timestamp * rate) for timestamp in timestamps]
            last_eligible = bisect.bisect_right(quantized, first_tick) - 1
            # Two predecessors handle duplicate rounded ticks and CLI seek
            # precision.  Seek is still relative to the *format* start time.
            guard = max(0, last_eligible - 2)
            seek = timestamps[guard]
        extra = ["-ss", seek_text(seek), "-copyts"]
        if variant != "packet_guard_without_origin":
            extra += ["-start_at_zero"]
        if variant == "packet_guard_noaccurate":
            extra += ["-noaccurate_seek"]
        video_filter = f"{fps},trim=start_pts={first_tick}:end_pts={end_tick}"
    cmd = ffmpeg_prefix() + extra + ["-i", str(source)]
    return cmd + output_options(rate, video_filter, count), seek


def prepare_profiles(output):
    sources = output / "sources"
    sources.mkdir()
    bunny = Path("/tmp/video-estimation-research/bunny.mp4")
    bunny_cfr = Path("/tmp/video-estimation-research/bunny-full.mkv")
    if not bunny.is_file() or not bunny_cfr.is_file():
        raise FileNotFoundError("previously inspected Bunny development inputs are required")
    derived = []
    definitions = [
        ("bunny-derived-cfr2997", "fps=30000/1001,scale=256:144"),
        (
            "bunny-derived-vfr-gap",
            "scale=256:144,select='not(between(t,4.2,6.4))*"
            "not(between(t,9.8,12.5))*not(eq(mod(n,7),3))'",
        ),
    ]
    for name, video_filter in definitions:
        destination = sources / f"{name}.mkv"
        cmd = ffmpeg_prefix() + [
            "-i", str(bunny), "-t", "19.375", "-map", "0:v:0", "-an",
            "-vf", video_filter, "-fps_mode", "passthrough", "-c:v", "libx264",
            "-preset", "veryfast", "-crf", "0", "-threads", "1", "-g", "48",
            "-pix_fmt", "yuv420p", "-y", str(destination),
        ]
        _, detail = run(cmd, output, f"generate-{name}")
        derived.append({"id": name, "source": str(destination), "generation": detail})
    gap = sources / "bunny-derived-vfr-gap.mkv"
    for name, delayed_audio in (
        ("bunny-derived-vfr-offset", False),
        ("bunny-derived-vfr-delayed-video", True),
    ):
        destination = sources / f"{name}.mkv"
        cmd = ffmpeg_prefix() + ["-copyts", "-itsoffset", "5.125", "-i", str(gap)]
        if delayed_audio:
            cmd += [
                "-f", "lavfi", "-i", "anullsrc=r=48000:cl=mono",
                "-map", "0:v:0", "-map", "1:a:0", "-c:a", "aac", "-t", "25",
            ]
        else:
            cmd += ["-map", "0:v:0", "-an"]
        cmd += ["-c:v", "copy", "-y", str(destination)]
        _, detail = run(cmd, output, f"generate-{name}")
        derived.append({"id": name, "source": str(destination), "generation": detail})
    existing = [
        {"id": "bunny-existing-cfr", "source": str(bunny_cfr)},
        {"id": "bunny-existing-vfr", "source": str(bunny)},
    ]
    return existing + derived


def windows(rows, rate, timestamps, seed):
    count = len(rows)
    rng = random.Random(seed)
    choices = {
        (0, min(6, count)): "first",
        (count // 2, min(6, count - count // 2)): "middle",
        (count - 3, 3): "short_tail_three",
        (count - 1, 1): "short_tail_one",
    }
    for i in range(7):
        start = rng.randrange(1, count - 6)
        choices[(start, rng.randrange(1, 7))] = f"random_{i}"
    for left, right in zip(timestamps, timestamps[1:]):
        if right - left > Fraction(1, 2):
            tick = near((left + right) * rate / 2)
            start = tick - rows[0]["pts"]
            if 0 <= start < count - 6:
                choices[(start, 6)] = f"inside_gap_{len(choices)}"
    return [
        {"id": label, "start_frame": start, "frame_count": frames, "first_pts": rows[start]["pts"]}
        for (start, frames), label in sorted(choices.items())
    ]


def compare(expected, actual, expected_base, actual_base):
    equal = expected_base == actual_base and expected == actual
    difference = next((
        {"frame": i, "expected": left, "actual": right}
        for i, (left, right) in enumerate(zip(expected, actual)) if left != right
    ), None)
    return {
        "exact": equal, "frame_count_equal": len(expected) == len(actual),
        "pixels_equal": [row["sha256"] for row in expected] == [row["sha256"] for row in actual],
        "pts_equal": [row["pts"] for row in expected] == [row["pts"] for row in actual],
        "time_base_equal": expected_base == actual_base,
        "expected_frames": len(expected), "actual_frames": len(actual), "first_difference": difference,
    }


def study(args):
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    frozen = source_hashes()
    profiles = prepare_profiles(output)
    metadata = {
        "split": "development only: previously inspected Bunny and derived diagnostics",
        "not_holdout": True, "changes_frozen_exporter": False,
        "source_code_hashes_before": frozen,
        "benchmark_sha256": digest(Path(__file__)),
        "ffmpeg": subprocess.check_output(["ffmpeg", "-version"], text=True),
        "rates": args.rates, "threads": 1, "random_seed": 20261003,
        "repeats": args.repeats,
        "timing_scope": "direct FFmpeg decode + fps + raw framehash, verbose logging; no source scan or encoding",
        "cost_note": "Shared four-core environment; counts are deterministic, timing is descriptive.",
        "limitations": [
            "This verifies local access only; frozen v2 still decodes from start.",
            "The frozen explicit-fps plan still counts by full filtered decode; this study does not remove that scan.",
            "Only fps and spatial scale filters are used; temporal/stateful preceding filters need their own context.",
            "Input packet index assumes one decoded presentation frame per packet and strictly increasing PTS.",
        ],
        "profiles": profiles,
    }
    save(output / "metadata.json", metadata)
    results = []
    for profile in profiles:
        source = Path(profile["source"])
        profile["sha256"] = digest(source)
        info, timestamps = inspect(source)
        profile["ffprobe"] = info
        save(output / "metadata.json", metadata)
        for rate_text in args.rates:
            rate = Fraction(rate_text)
            prefix = profile["id"] + "-fps" + rate_text.replace("/", "_")
            reference_cmd = ffmpeg_prefix() + ["-i", str(source)] + output_options(rate, f"fps={rate}")
            raw, ref_timing = run(reference_cmd, output, prefix + "-full")
            (output / f"{prefix}-full.framehash").write_bytes(raw)
            reference_base, reference_rows = framehash(raw)
            if reference_base != 1 / rate:
                raise RuntimeError("unexpected FPS reference time base")
            if any(row["pts"] != reference_rows[0]["pts"] + n for n, row in enumerate(reference_rows)):
                raise RuntimeError("full FPS reference does not have contiguous integer PTS")
            variants = ["prefix_frames", "local_reset", "fixed_guard", "packet_guard", "packet_guard_noaccurate"]
            if Fraction(info["format"]["start_time"]) != 0:
                variants.append("packet_guard_without_origin")
            selected_windows = windows(reference_rows, rate, timestamps, 20261003)
            for window in selected_windows:
                expected = reference_rows[window["start_frame"]:window["start_frame"] + window["frame_count"]]
                order = list(variants)
                random.Random(20261003 + window["start_frame"]).shuffle(order)
                for variant in order:
                    trials = []
                    comparisons = []
                    for repeat in range(args.repeats):
                        stem = f"{prefix}-{window['id']}-{variant}-{repeat}"
                        cmd, seek = command(source, rate, window, variant, timestamps)
                        raw, timing = run(cmd, output, stem)
                        (output / f"{stem}.framehash").write_bytes(raw)
                        actual_base, actual = framehash(raw)
                        checks = compare(expected, actual, reference_base, actual_base)
                        trials.append(timing)
                        comparisons.append(checks)
                    row = {
                        "profile": profile["id"], "rate": rate_text, "window": window,
                        "variant": variant, "seek_relative_seconds": str(seek) if seek is not None else None,
                        "reference_frames": len(reference_rows), "reference_full_timing": ref_timing,
                        "comparison": comparisons[0], "all_repeats_exact": all(c["exact"] for c in comparisons),
                        "trials": trials,
                    }
                    results.append(row)
                    with (output / "results.jsonl").open("a") as stream:
                        stream.write(json.dumps(row) + "\n")
            print(json.dumps({
                "profile": profile["id"], "rate": rate_text, "reference_frames": len(reference_rows),
                "windows": len(selected_windows), "completed_comparisons": len(results),
                "packet_guard_failures_so_far": sum(
                    not row["all_repeats_exact"] for row in results if row["variant"] == "packet_guard"
                ),
            }), flush=True)
    if source_hashes() != frozen:
        raise RuntimeError("frozen source changed during independent study")
    metadata["source_code_hashes_after"] = source_hashes()
    save(output / "metadata.json", metadata)
    summaries = []
    for variant in sorted({row["variant"] for row in results}):
        selected = [row for row in results if row["variant"] == variant]
        passed = [row for row in selected if row["all_repeats_exact"]]
        trials = [trial for row in passed for trial in row["trials"]]
        summaries.append({
            "variant": variant, "comparisons": len(selected), "exact": len(passed),
            "failures": [
                {key: row[key] for key in ("profile", "rate", "window", "comparison")}
                for row in selected if not row["all_repeats_exact"]
            ],
            "median_wall_seconds_among_exact": statistics.median(t["wall_seconds"] for t in trials) if trials else None,
            "median_frames_decoded_among_exact": statistics.median(t["input_frames_decoded"] for t in trials) if trials else None,
        })
    save(output / "summary.json", {"comparisons": len(results), "variants": summaries})
    print(json.dumps({"done": True, "comparisons": len(results), "source_unchanged": True}), flush=True)


def planning(args):
    """Verify head/true-EOF-tail counting without a complete converted decode."""
    output = args.output.resolve()
    original = json.loads((output / "metadata.json").read_text())
    if source_hashes() != original["source_code_hashes_before"]:
        raise RuntimeError("frozen source differs from the window study")
    destination = output / "planning"
    destination.mkdir(exist_ok=False)
    rows = []
    for profile in original["profiles"]:
        source = Path(profile["source"])
        if digest(source) != profile["sha256"]:
            raise RuntimeError("development input changed")
        scan_started = time.monotonic()
        info, timestamps = inspect(source)
        scan_wall = time.monotonic() - scan_started
        for rate_text in original["rates"]:
            rate = Fraction(rate_text)
            prefix = profile["id"] + "-fps" + rate_text.replace("/", "_")
            reference_base, reference = framehash((output / f"{prefix}-full.framehash").read_bytes())
            head_cmd = ffmpeg_prefix() + ["-i", str(source)] + output_options(rate, f"fps={rate}", 1)
            head_raw, head_timing = run(head_cmd, destination, prefix + "-head")
            (destination / f"{prefix}-head.framehash").write_bytes(head_raw)
            head_base, head = framehash(head_raw)
            seek = timestamps[max(0, len(timestamps) - 4)]
            tail_cmd = ffmpeg_prefix() + [
                "-ss", seek_text(seek), "-copyts", "-start_at_zero", "-i", str(source),
            ] + output_options(rate, f"showinfo,fps={rate}")
            tail_cmd[tail_cmd.index("-loglevel") + 1] = "debug"
            # There is deliberately no input -t, output frame limit or trim:
            # fps must receive the true decoder/filter EOF of this video.
            tail_raw, tail_timing = run(tail_cmd, destination, prefix + "-tail-true-eof")
            (destination / f"{prefix}-tail-true-eof.framehash").write_bytes(tail_raw)
            tail_base, tail = framehash(tail_raw)
            stderr = (destination / f"{prefix}-tail-true-eof.stderr.log").read_text()
            eof_values = re.findall(r"EOF is at pts (-?\d+)", stderr)
            if not head or not tail or not eof_values:
                raise RuntimeError("empty head/tail or missing real fps EOF; must fall back")
            end = int(eof_values[-1])
            inferred_count = end - head[0]["pts"]
            expected_tail = [row for row in reference if row["pts"] >= tail[0]["pts"]]
            source_packets = sorted(info["packets"], key=lambda packet: int(packet["pts"]))
            source_rate = Fraction(info["streams"][0]["avg_frame_rate"])
            cfr_nominal_count = near(Fraction(len(source_packets), 1) * rate / source_rate)
            row = {
                "profile": profile["id"], "rate": rate_text,
                "packet_scan_wall_seconds": scan_wall,
                "source_packets": len(source_packets), "head_timing": head_timing, "tail_timing": tail_timing,
                "first_output_pts": head[0]["pts"], "true_fps_eof_pts": end,
                "inferred_frames": inferred_count, "full_reference_frames": len(reference),
                "head_exact": compare(reference[:1], head, reference_base, head_base),
                "tail_exact": compare(expected_tail, tail, reference_base, tail_base),
                "count_exact": inferred_count == len(reference),
                "nominal_source_count_rescale": cfr_nominal_count,
                "nominal_count_matches": cfr_nominal_count == len(reference),
                "last_output_is_true_eof_minus_one": tail[-1]["pts"] + 1 == end,
                "candidate_total_plan_wall_seconds": scan_wall + head_timing["wall_seconds"] + tail_timing["wall_seconds"],
                "actual_head_plus_tail_frames_decoded": head_timing["input_frames_decoded"] + tail_timing["input_frames_decoded"],
            }
            rows.append(row)
            save(destination / "results.json", rows)
            print(json.dumps({
                "planning_profile": profile["id"], "rate": rate_text,
                "count_exact": row["count_exact"], "head_exact": row["head_exact"]["exact"],
                "tail_exact": row["tail_exact"]["exact"], "frames": inferred_count,
            }), flush=True)
    save(destination / "metadata.json", {
        "benchmark_sha256": digest(Path(__file__)), "source_code_hashes": source_hashes(),
        "source_unchanged": source_hashes() == original["source_code_hashes_before"],
        "development_only": True,
        "no_full_decode_in_candidate": True,
        "comparison_reference": "unseeked full framehash files from preceding window study",
        "tail_debug_logging": "true EOF is read from fps diagnostic status; pixel suffix independently verified",
        "limitations": [
            "Candidate counting requires a monotonic normal-container timeline and contiguous fps output grid.",
            "Decoder-frame presentation metadata, random-access completeness and real EOF require a support contract.",
            "Packet duration or nominal frame count alone is not treated as decoder EOF.",
            "Open GOP, damaged packets, timestamp discontinuities and temporal filters are not proven.",
            "This is an independent development candidate, not an implemented v2 optimization.",
        ],
    })


def planning_cost(args):
    """Measure the current full conversion count with null output, without SHA."""
    output = args.output.resolve()
    original = json.loads((output / "metadata.json").read_text())
    if source_hashes() != original["source_code_hashes_before"]:
        raise RuntimeError("frozen source differs from the window study")
    planned = json.loads((output / "planning/results.json").read_text())
    destination = output / "planning/full-count-cost"
    destination.mkdir(exist_ok=False)
    rows = []
    for row in planned:
        profile = next(p for p in original["profiles"] if p["id"] == row["profile"])
        source = Path(profile["source"])
        prefix = row["profile"] + "-fps" + row["rate"].replace("/", "_")
        cmd = ffmpeg_prefix() + [
            "-i", str(source), "-map", "0:v:0", "-an", "-sn", "-dn",
            "-vf", f"fps={row['rate']}", "-fps_mode", "passthrough", "-threads", "1",
            "-progress", "pipe:1", "-f", "null", "-",
        ]
        raw, timing = run(cmd, destination, prefix)
        (destination / f"{prefix}.progress.log").write_bytes(raw)
        counts = re.findall(rb"(?:^|\n)frame=(\d+)", raw)
        count = int(counts[-1]) if counts else None
        rows.append({
            "profile": row["profile"], "rate": row["rate"], "full_count": count,
            "count_matches_reference": count == row["full_reference_frames"],
            "full_filtered_null_count_timing": timing,
            "packet_scan_plus_full_conversion_wall_seconds": row["packet_scan_wall_seconds"] + timing["wall_seconds"],
            "packet_scan_plus_head_tail_wall_seconds": row["candidate_total_plan_wall_seconds"],
            "full_frames_decoded": timing["input_frames_decoded"],
            "candidate_frames_decoded": row["actual_head_plus_tail_frames_decoded"],
        })
        save(destination / "results.json", rows)
    save(destination / "metadata.json", {
        "benchmark_sha256": digest(Path(__file__)), "source_code_hashes": source_hashes(),
        "source_unchanged": source_hashes() == original["source_code_hashes_before"],
        "note": "Compare with null/count output, not the costlier all-frame pixel SHA reference. Single descriptive timings.",
    })


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "results/fps-seek-study-2026-10-03")
    parser.add_argument("--rates", nargs="+", default=["12", "30000/1001"])
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--stage", choices=["study", "planning", "planning-cost"], default="study")
    args = parser.parse_args()
    if args.repeats < 1:
        parser.error("repeats must be positive")
    if args.stage == "planning":
        planning(args)
    elif args.stage == "planning-cost":
        planning_cost(args)
    else:
        study(args)


if __name__ == "__main__":
    main()
