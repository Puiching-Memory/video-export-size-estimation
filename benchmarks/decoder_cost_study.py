#!/usr/bin/env python3
"""Check whether an explicit segment frame limit reduces source decoding.

Development-only command replay. The frozen exporter is not modified. FFmpeg
diagnostic logging is measured separately from unchanged info-level timing runs.
"""

import argparse
import hashlib
import json
import random
import re
import statistics
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from vsize.contracts import EncodeSpec  # noqa: E402
from vsize.media import Media  # noqa: E402
from vsize.runtime import Cache, Deadline, digest  # noqa: E402
from vsize.segmented_media import SegmentedMedia  # noqa: E402


class CapturingDeadline(Deadline):
    def __init__(self):
        super().__init__(1200)
        self.commands = []
        self.logs = []

    def run(self, command):
        result = super().run(command)
        if command[0] == "ffmpeg" and "-x264-params" in command:
            self.commands.append(list(command))
            self.logs.append(result.stderr.decode(errors="replace"))
        return result


def source_code_hashes():
    return {
        str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted((ROOT / "src").rglob("*.py"))
    }


def profiles():
    manifest = json.loads((ROOT / "artifacts/corpus-v1/manifest.json").read_text())
    if manifest["split"] != "development":
        raise ValueError("decoder study requires the existing development manifest")
    cases = {case["id"]: case for case in manifest["cases"]}
    selected = []
    for name in ("motion", "grain"):
        source = Path(cases[name]["source"])
        if digest(source) != cases[name]["sha256"]:
            raise ValueError(f"development source changed: {name}")
        selected.append({"id": name, "source": source, "video_filter": None})
    for name, filename, video_filter in (
        ("bunny-cfr", "bunny-full.mkv", None),
        ("bunny-fps24", "bunny.mp4", "fps=24"),
    ):
        source = Path("/tmp/video-estimation-research") / filename
        if source.is_file():
            selected.append({"id": name, "source": source, "video_filter": video_filter})
    return selected


def execute(command, logfile):
    started = time.monotonic()
    completed = Deadline(300).run(command)
    wall = time.monotonic() - started
    stderr = completed.stderr.decode(errors="replace")
    logfile.write_text(stderr)
    match = re.search(r"bench: utime=([\d.]+)s stime=([\d.]+)s rtime=([\d.]+)s", stderr)
    return {
        "wall_seconds": wall,
        "ffmpeg_user_seconds": float(match[1]) if match else None,
        "ffmpeg_system_seconds": float(match[2]) if match else None,
        "ffmpeg_rtime_seconds": float(match[3]) if match else None,
    }, stderr


def inspect(path, frame_log):
    command = [
        "ffprobe",
        "-v",
        "error",
        "-threads",
        "1",
        "-select_streams",
        "v:0",
        "-count_frames",
        "-show_streams",
        "-show_packets",
        "-show_entries",
        "stream=codec_name,width,height,time_base,nb_read_frames,nb_frames,duration:"
        "packet=pts,dts,duration,size,flags",
        "-of",
        "json",
        str(path),
    ]
    info = json.loads(Deadline(60).run(command).stdout)
    decoded = (
        Deadline(60)
        .run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-nostdin",
                "-threads",
                "1",
                "-i",
                str(path),
                "-map",
                "0:v:0",
                "-an",
                "-filter_threads",
                "1",
                "-threads",
                "1",
                "-pix_fmt",
                "yuv420p",
                "-fps_mode",
                "passthrough",
                "-f",
                "framemd5",
                "pipe:1",
            ]
        )
        .stdout.decode()
    )
    frame_log.write_text(decoded)
    decoded_rows = [line for line in decoded.splitlines() if line and not line.startswith("#")]
    frame_hashes = [line.split(",")[-1].strip() for line in decoded_rows]
    stream = info["streams"][0]
    return {
        "sha256": digest(path),
        "file_bytes": path.stat().st_size,
        "packet_count": len(info["packets"]),
        "decoded_frame_count": len(frame_hashes),
        "frame_hashes": frame_hashes,
        "decoded_rows_sha256": hashlib.sha256("\n".join(decoded_rows).encode()).hexdigest(),
        "packets_sha256": hashlib.sha256(
            json.dumps(info["packets"], sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
        "stream": stream,
    }


def diagnostics(stderr):
    input_match = re.search(
        r"Input stream #0:\d+ \(video\): (\d+) packets read.*?; (\d+) frames decoded",
        stderr,
    )
    output_match = re.search(r"Output stream #0:\d+ \(video\): (\d+) frames encoded", stderr)
    decode_times = re.findall(r"bench:.*?([\d.]+) real decode_video", stderr)
    return {
        "input_packets_read": int(input_match[1]) if input_match else None,
        "input_frames_decoded": int(input_match[2]) if input_match else None,
        "output_frames_encoded": int(output_match[1]) if output_match else None,
        "benchmark_decode_events": len(decode_times),
        "benchmark_decode_real_microseconds_sum": sum(map(float, decode_times)),
    }


def comparison(left, right):
    names = (
        "sha256",
        "packet_count",
        "decoded_frame_count",
        "decoded_rows_sha256",
        "packets_sha256",
    )
    return {name: left[name] == right[name] for name in names}


def changed_command(captured, destination, frames=None, debug=False):
    command = list(captured)
    command[-1] = str(destination)
    if frames is not None:
        command[-1:-1] = ["-frames:v", str(frames)]
    if debug:
        command[command.index("-loglevel") + 1] = "debug"
        command[-1:-1] = ["-benchmark_all"]
    return command


def study(args):
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    artifacts = ROOT / "artifacts" / output.name
    artifacts.mkdir(parents=True, exist_ok=False)
    frozen = source_code_hashes()
    metadata = {
        "split": "development-only; Bunny has been inspected in prior work",
        "source_code_hashes_before": frozen,
        "ffmpeg": subprocess.check_output(["ffmpeg", "-version"], text=True),
        "threads": 1,
        "segment_seconds": args.segment_seconds,
        "repeats_per_variant": args.repeats,
        "timing_boundary": "direct FFmpeg replay; source preparation and inspection excluded",
        "concurrency_note": "Study is serial; formal holdout FFmpeg jobs may run concurrently. No strict latency claim.",
        "diagnostic_note": "debug + benchmark_all runs separate from unchanged info-level timing runs",
        "artifact_root": str(artifacts),
    }
    (output / "metadata.json").write_text(json.dumps(metadata, indent=2))
    results = []
    rng = random.Random(20261003)
    for profile in profiles():
        deadline = CapturingDeadline()
        media = Media(
            EncodeSpec(
                profile["source"],
                drop_audio=True,
                video_filter=profile["video_filter"],
                export_mode="segmented",
                segment_seconds=args.segment_seconds,
            ),
            1,
            Cache(artifacts / profile["id"] / "capture-cache"),
            deadline,
        )
        adapter = SegmentedMedia(media, args.segment_seconds)
        plan = adapter.prepare()
        indices = sorted({0, len(adapter.segments) // 2, len(adapter.segments) - 1})
        for index in indices:
            segment = adapter.segments[index]
            prefix = f"{profile['id']}-segment{index}"
            before = len(deadline.commands)
            captured_record = adapter.encode_segment(index, 23)
            if len(deadline.commands) != before + 1:
                raise RuntimeError("expected one uncached exporter command to capture")
            captured = deadline.commands[-1]
            (output / f"{prefix}.captured.stderr.log").write_text(deadline.logs[-1])
            item = {
                "id": prefix,
                "profile": profile["id"],
                "source": str(profile["source"]),
                "source_sha256": media.source_hash,
                "source_frames": adapter.source_frames,
                "source_geometry": plan["source_geometry"],
                "video_filter": profile["video_filter"],
                "segment": segment.configuration(),
                "captured_command": captured,
                "captured_output_sha256": captured_record["sha256"],
                "captured_encode_wall_seconds": captured_record["encode_wall_seconds"],
                "captured_transcode_wall_seconds": captured_record["transcode_wall_seconds"],
                "variants": {"original": {"trials": []}, "frames_limit": {"trials": []}},
            }
            first_order = ["original", "frames_limit"]
            rng.shuffle(first_order)
            checks = {}
            for repeat in range(args.repeats):
                order = first_order if repeat % 2 == 0 else first_order[::-1]
                for variant in order:
                    destination = artifacts / f"{prefix}-{variant}-{repeat}.mp4"
                    frames = segment.frame_count if variant == "frames_limit" else None
                    command = changed_command(captured, destination, frames=frames)
                    timing, _ = execute(command, output / f"{prefix}-{variant}-{repeat}.stderr.log")
                    evidence = inspect(
                        destination, output / f"{prefix}-{variant}-{repeat}.framemd5"
                    )
                    if evidence["packet_count"] != segment.frame_count:
                        raise RuntimeError("replayed output changed actual segment frame count")
                    if repeat == 0:
                        checks[variant] = evidence
                    timing.update(
                        repeat=repeat,
                        command=command,
                        inspection=evidence,
                        matches_captured_sha256=evidence["sha256"] == captured_record["sha256"],
                    )
                    item["variants"][variant]["trials"].append(timing)
            item["output_equivalence"] = comparison(checks["original"], checks["frames_limit"])
            for variant in ("original", "frames_limit"):
                destination = artifacts / f"{prefix}-{variant}-debug.mp4"
                frames = segment.frame_count if variant == "frames_limit" else None
                command = changed_command(captured, destination, frames=frames, debug=True)
                timing, stderr = execute(command, output / f"{prefix}-{variant}-debug.stderr.log")
                evidence = inspect(destination, output / f"{prefix}-{variant}-debug.framemd5")
                timing.update(
                    command=command,
                    **diagnostics(stderr),
                    matches_info_output_sha256=evidence["sha256"] == checks[variant]["sha256"],
                )
                item["variants"][variant]["diagnostic"] = timing
                trials = item["variants"][variant]["trials"]
                item["variants"][variant]["median_wall_seconds"] = statistics.median(
                    trial["wall_seconds"] for trial in trials
                )
            base = item["variants"]["original"]
            limit = item["variants"]["frames_limit"]
            item["frames_limit_wall_ratio"] = (
                limit["median_wall_seconds"] / base["median_wall_seconds"]
            )
            results.append(item)
            (output / "measurements.json").write_text(json.dumps(results, indent=2))
            print(
                json.dumps(
                    {
                        "case": prefix,
                        "source_frames": adapter.source_frames,
                        "decoded_original": base["diagnostic"]["input_frames_decoded"],
                        "decoded_frames_limit": limit["diagnostic"]["input_frames_decoded"],
                        "output_equivalent": all(item["output_equivalence"].values()),
                        "wall_ratio": round(item["frames_limit_wall_ratio"], 3),
                    }
                ),
                flush=True,
            )
    metadata["source_code_hashes_after"] = source_code_hashes()
    metadata["frozen_source_unchanged"] = metadata["source_code_hashes_after"] == frozen
    (output / "metadata.json").write_text(json.dumps(metadata, indent=2))
    if not metadata["frozen_source_unchanged"]:
        raise RuntimeError("frozen src changed during study")
    write_report(output, results)


def write_report(output, results):
    savings = [
        item["variants"]["original"]["diagnostic"]["input_frames_decoded"]
        - item["variants"]["frames_limit"]["diagnostic"]["input_frames_decoded"]
        for item in results
    ]
    profiles_present = sorted({item["profile"] for item in results})
    first_items = {
        profile: min(
            (item for item in results if item["profile"] == profile),
            key=lambda item: item["segment"]["index"],
        )
        for profile in profiles_present
    }
    early_end_observed = all(
        item["variants"]["original"]["diagnostic"]["input_frames_decoded"] < item["source_frames"]
        for item in first_items.values()
    )
    equivalent_pairs = sum(all(item["output_equivalence"].values()) for item in results)
    summary = {
        "paired_cases": len(results),
        "early_segment_decoding_ends_before_source_eof": early_end_observed,
        "all_outputs_equivalent": all(all(item["output_equivalence"].values()) for item in results),
        "all_timing_replays_match_captured_sha256": all(
            trial["matches_captured_sha256"]
            for item in results
            for variant in item["variants"].values()
            for trial in variant["trials"]
        ),
        "all_diagnostic_replays_match_info_sha256": all(
            variant["diagnostic"]["matches_info_output_sha256"]
            for item in results
            for variant in item["variants"].values()
        ),
        "source_frames_saved_by_limit": savings,
        "minimum_wall_ratio": min(item["frames_limit_wall_ratio"] for item in results),
        "maximum_wall_ratio": max(item["frames_limit_wall_ratio"] for item in results),
        "median_wall_ratio": statistics.median(item["frames_limit_wall_ratio"] for item in results),
        "conclusion": (
            "Existing trim already ends source decoding; explicit frame limit saves at most one decoded frame in these cases."
            if early_end_observed and all(0 <= value <= 1 for value in savings)
            else "Inspect observed source decode counts; the default early-termination conclusion was not established."
        ),
        "timing_claim": "No strict speed or latency advantage claimed under concurrent jobs and two short repeats.",
    }
    (output / "summary.json").write_text(json.dumps(summary, indent=2))
    rows = [
        "# 源解码成本诊断（仅开发集）",
        "",
        f"实际检查 {len(results)} 个配对案例；首段均在源 EOF 前停止：{early_end_observed}；额外 frames 限制省下的解码帧数范围为 {min(savings)}–{max(savings)}。",
        f"{equivalent_pairs}/{len(results)} 文件 SHA、包数及包时序、逐帧解码结果相同。所有 timing 重放与捕获产物相同：{summary['all_timing_replays_match_captured_sha256']}；debug 与 info 产物相同：{summary['all_diagnostic_replays_match_info_sha256']}。",
        "",
        "研究使用现有 motion / grain 开发片和以前已查看的 Bunny；未读取 holdout 误差，也未改动 frozen src。",
        "所有比较保持 codec、输入、滤镜、CRF、preset、GOP 和线程一致；frames_limit 仅额外添加 `-frames:v segment.frame_count`。",
        "时间是直接命令重放的墙钟，不含源准备与输出检查；研究为串行单线程，但正式评测在并行运行，因此不宣称严格延迟优势。",
        "debug + benchmark_all 仅用于源输入帧计数，另存诊断日志，不混入主要墙钟比较。",
        "",
        "| 案例 | 源总帧 | 输出帧 | 原命令实际解码帧 | frames限制解码帧 | 文件/逐帧/包相同 | 墙钟比 limit/original |",
        "| --- | ---: | ---: | ---: | ---: | --- | ---: |",
    ]
    for item in results:
        rows.append(
            f"| {item['id']} | {item['source_frames']} | {item['segment']['frame_count']} | "
            f"{item['variants']['original']['diagnostic']['input_frames_decoded']} | "
            f"{item['variants']['frames_limit']['diagnostic']['input_frames_decoded']} | "
            f"{'是' if all(item['output_equivalence'].values()) else '否'} | "
            f"{item['frames_limit_wall_ratio']:.3f} |"
        )
    rows.extend(
        [
            "",
            "输出等价同时检查文件 SHA-256、video packet 数量及 PTS/DTS/duration/size/flags 摘要、逐帧解码 MD5 和逐帧时间戳记录。",
            "[measurements.json](measurements.json) 保存捕获的完整原命令、重放命令、来源指纹、每次计时、包数、帧哈希与 debug 源解码计数；[metadata.json](metadata.json) 保存前后源码 SHA 和 FFmpeg 版本。",
            "诊断 stderr/framemd5 随报告保存；实际小片产物位于 metadata 的 artifact_root，不随源码包分发。",
            "",
            "## 已确认的成本和适用边界",
            "",
            f"- 主要结论：{summary['conclusion']}",
            "- 正常 CFR 的额外输入帧位于所需输出之前，来自 GOP 寻址与 trim 的前导边界；单凭无 frames 参数不能认定输出后的整片尾部被解码。",
            "- 显式 fps 转换为保留全片滤镜相位和按帧选择的语义，当前使用 decode_from_start。该前缀成本随段位置增大，frames 限制无法消除；上表 Bunny fps24 中/尾段展示了该区别。",
            "- 明确 frames 限制在本次版本、输入和滤镜上的等价结论取决于上表实际验证，不能推广为任意编解码器、任意滤镜或 FFmpeg 版本的保证。",
            f"- 短任务重复、并行正式评测、输入缓存预热和进程启动均影响墙钟。limit/original 从约 {summary['minimum_wall_ratio']:.3f} 到 {summary['maximum_wall_ratio']:.3f}；不据此作统计显著或生产 SLO 声明。",
            "- 下一步应分离测量源 seek/preroll、显式 fps 的前缀处理、每段进程启动、ffprobe/摘要/缓存写入、最终 mux 的成本。更激进寻址必须验证整数边界与 fps 相位一致，不能直接把输入 -ss 挪到段起点就宣称等价。",
            "",
            "所有源码文件的前后摘要一致，研究中没有改动 frozen src。原 Bunny MP4 的 avg_frame_rate 与 r_frame_rate 不同，所以它只以明确 fps=24 转换进入研究；CFR 路径使用已查看的 bunny-full.mkv。",
        ]
    )
    (output / "README.md").write_text("\n".join(rows) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", type=Path, default=ROOT / "results/decoder-cost-study-2026-10-03"
    )
    parser.add_argument("--segment-seconds", type=float, default=2)
    parser.add_argument("--repeats", type=int, default=2)
    args = parser.parse_args()
    if args.repeats < 1 or args.segment_seconds <= 0:
        parser.error("repeats and segment-seconds must be positive")
    study(args)


if __name__ == "__main__":
    main()
