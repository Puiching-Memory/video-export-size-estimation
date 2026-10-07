#!/usr/bin/env python3
"""Development-only libx264 equivalence and no-flush context experiments."""

import argparse
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
from fractions import Fraction
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SOURCE_ROOT = Path(
    os.environ.get("VSIZE_FROZEN_SOURCE_ROOT", ROOT / "artifacts/stream-context-f39/src")
).resolve()
if not SOURCE_ROOT.is_dir():
    raise RuntimeError("prepare the frozen src snapshot or set VSIZE_FROZEN_SOURCE_ROOT explicitly")
sys.path.insert(0, str(SOURCE_ROOT))

from vsize.assets import PreparedAsset  # noqa: E402
from vsize.contracts import EncodeSpec  # noqa: E402
from vsize.media import Media  # noqa: E402
from vsize.runtime import Cache, Deadline, digest  # noqa: E402


def save_json(path, value):
    path.write_text(json.dumps(value, indent=2))


def code_hashes():
    return {
        f"src/{p.relative_to(SOURCE_ROOT)}": digest(p) for p in sorted(SOURCE_ROOT.rglob("*.py"))
    }


def run(command, *, stdin=None, env=None, pass_fds=(), timeout=120):
    started = time.monotonic()
    process = subprocess.Popen(
        command,
        stdin=stdin,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        pass_fds=pass_fds,
        start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except BaseException:
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        process.communicate()
        raise
    if process.returncode:
        raise RuntimeError(stderr.decode(errors="replace")[-3000:])
    return stdout, stderr.decode(errors="replace"), time.monotonic() - started


def avcc_nals(data):
    result, position = [], 0
    while position < len(data):
        if position + 4 > len(data):
            raise ValueError("incomplete AVCC length")
        size = int.from_bytes(data[position : position + 4], "big")
        position += 4
        if not size or position + size > len(data):
            raise ValueError("invalid AVCC payload")
        result.append(data[position : position + size])
        position += size
    return result


def extradata_bytes(text):
    result = bytearray()
    for line in text.splitlines():
        if ":" in line:
            result.extend(
                bytes.fromhex("".join(line.split(":", 1)[1].split("  ", 1)[0].strip().split()))
            )
    return bytes(result)


def sps_pps(data):
    if not data or data[0] != 1:
        raise ValueError("AVC decoder configuration record expected")
    position, nals = 6, []
    for count in (data[5] & 31,):
        for _ in range(count):
            size = int.from_bytes(data[position : position + 2], "big")
            position += 2
            nals.append(data[position : position + size])
            position += size
    count = data[position]
    position += 1
    for _ in range(count):
        size = int.from_bytes(data[position : position + 2], "big")
        position += 2
        nals.append(data[position : position + size])
        position += size
    return nals


def mp4_packets(path, fps):
    stdout, _, _ = run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_packets",
            "-show_streams",
            "-show_data",
            "-show_entries",
            "stream=time_base,extradata:packet=pts,dts,pos,size,flags",
            "-of",
            "json",
            str(path),
        ]
    )
    info = json.loads(stdout)
    time_base = Fraction(info["streams"][0]["time_base"])
    packets = []
    with path.open("rb") as stream:
        for record in info["packets"]:
            pts = int(record["pts"]) * time_base * fps
            dts = int(record["dts"]) * time_base * fps
            if pts.denominator != 1 or dts.denominator != 1:
                raise ValueError("noninteger presentation time on declared frame clock")
            stream.seek(int(record["pos"]))
            payload = stream.read(int(record["size"]))
            packets.append({"pts": int(pts), "dts": int(dts), "payload": payload})
    return packets, sps_pps(extradata_bytes(info["streams"][0]["extradata"]))


def helper_packets(base):
    data = base.with_suffix(".packets.avcc").read_bytes()
    packets = []
    for line in base.with_suffix(".packets.jsonl").read_text().splitlines():
        record = json.loads(line)
        record["payload"] = data[record["offset"] : record["offset"] + record["size"]]
        packets.append(record)
    headers = avcc_nals(base.with_suffix(".headers.avcc").read_bytes())
    return packets, [nal for nal in headers if nal[0] & 31 in (7, 8)]


def fingerprint_packets(packets):
    return [
        {
            "pts": packet["pts"],
            "dts": packet["dts"],
            "bytes": len(packet["payload"]),
            "sha256": hashlib.sha256(packet["payload"]).hexdigest(),
            "nal_types": [nal[0] & 31 for nal in avcc_nals(packet["payload"])],
            "nal_reference_idc": [nal[0] >> 5 & 3 for nal in avcc_nals(packet["payload"])],
            **({"x264_picture_type": packet["type"]} if "type" in packet else {}),
        }
        for packet in packets
    ]


def packet_equivalence(left, right):
    return {
        "packet_count": len(left) == len(right),
        "pts_dts": [(p["pts"], p["dts"]) for p in left] == [(p["pts"], p["dts"]) for p in right],
        "avcc_bytes": [p["payload"] for p in left] == [p["payload"] for p in right],
    }


def frame_hashes(path):
    stdout, _, wall = run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
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
            "-fps_mode",
            "passthrough",
            "-pix_fmt",
            "yuv420p",
            "-f",
            "framemd5",
            "pipe:1",
        ]
    )
    rows = [line for line in stdout.decode().splitlines() if line and not line.startswith("#")]
    return [line.split(",")[-1].strip() for line in rows], wall


def ffmpeg_decode_command(media, start, duration, output, *, debug=False):
    return [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "debug" if debug else "info",
        "-nostdin",
        "-y",
        "-xerror",
        "-nostats",
        "-benchmark",
        "-threads",
        "1",
        "-filter_threads",
        "1",
        "-ss",
        str(start),
        "-i",
        media.input_path,
        "-t",
        str(duration),
        "-map",
        f"0:{media.video['index']}",
        "-an",
        "-sn",
        "-dn",
        "-pix_fmt",
        "yuv420p",
        "-fps_mode",
        "passthrough",
        "-f",
        "rawvideo",
        str(output),
    ]


def color_parameters(video):
    codes = {
        "color_primaries": {
            "bt709": 1,
            "bt470m": 4,
            "bt470bg": 5,
            "smpte170m": 6,
            "smpte240m": 7,
            "film": 8,
            "bt2020": 9,
        },
        "color_transfer": {
            "bt709": 1,
            "gamma22": 4,
            "gamma28": 5,
            "smpte170m": 6,
            "smpte240m": 7,
            "linear": 8,
            "log100": 9,
            "log316": 10,
            "iec61966-2-4": 11,
            "bt1361e": 12,
            "iec61966-2-1": 13,
            "bt2020-10": 14,
            "bt2020-12": 15,
        },
        "color_space": {
            "gbr": 0,
            "bt709": 1,
            "fcc": 4,
            "bt470bg": 5,
            "smpte170m": 6,
            "smpte240m": 7,
            "ycgco": 8,
            "bt2020nc": 9,
            "bt2020c": 10,
        },
    }
    env, flags, filter_parameters = {}, [], []
    for key, flag, parameter in (
        ("color_primaries", "-color_primaries", "VSIZE_VUI_COLOR_PRIMARIES"),
        ("color_transfer", "-color_trc", "VSIZE_VUI_COLOR_TRANSFER"),
        ("color_space", "-colorspace", "VSIZE_VUI_COLOR_MATRIX"),
    ):
        if key in video and video[key] not in ("unknown", "unspecified"):
            if video[key] not in codes[key]:
                raise ValueError(f"unmapped VUI code for {key}: {video[key]}")
            env[parameter] = str(codes[key][video[key]])
            flags.extend([flag, video[key]])
            filter_parameters.append(f"{flag.lstrip('-')}={video[key]}")
    if video.get("color_range") in ("tv", "pc"):
        env["VSIZE_VUI_FULL_RANGE"] = str(int(video["color_range"] == "pc"))
        flags.extend(["-color_range", video["color_range"]])
        filter_parameters.append(f"range={'full' if video['color_range'] == 'pc' else 'limited'}")
    chroma_codes = {"left": 0, "center": 1, "topleft": 2, "top": 3, "bottomleft": 4, "bottom": 5}
    if video.get("chroma_location") in chroma_codes:
        env["VSIZE_VUI_CHROMA_LOCATION"] = str(chroma_codes[video["chroma_location"]])
        flags.extend(["-chroma_sample_location", video["chroma_location"]])
    return {
        "env": env,
        "ffmpeg_flags": flags,
        "frame_filter": "setparams=" + ":".join(filter_parameters) if filter_parameters else None,
    }


def raw_ffmpeg_command(raw, output, width, height, fps, sar, vui):
    raw_filter = f"setsar={sar}:max=65535"
    if vui["frame_filter"]:
        raw_filter += "," + vui["frame_filter"]
    command = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "info",
        "-nostdin",
        "-y",
        "-nostats",
        "-benchmark",
        "-threads",
        "1",
        "-filter_threads",
        "1",
        "-f",
        "rawvideo",
        "-pixel_format",
        "yuv420p",
        "-video_size",
        f"{width}x{height}",
        "-framerate",
        str(fps),
        *vui["ffmpeg_flags"],
        "-i",
        str(raw),
        "-vf",
        raw_filter,
        "-an",
        "-sn",
        "-dn",
        "-map_metadata",
        "-1",
        "-c:v",
        "libx264",
        "-crf",
        "23",
        "-preset",
        "medium",
        "-threads",
        "1",
        "-pix_fmt",
        "yuv420p",
        "-fps_mode",
        "passthrough",
        "-movflags",
        "+faststart",
    ]
    return command + vui["ffmpeg_flags"] + [str(output)]


def helper_command(helper, base, width, height, fps, total, central_start, central_frames, sar):
    return [
        str(helper),
        str(width),
        str(height),
        str(fps.numerator),
        str(fps.denominator),
        str(total),
        str(central_start),
        str(central_frames),
        str(sar.numerator),
        str(sar.denominator),
        str(base),
    ]


def encode_helper(
    helper, raw, base, width, height, fps, total, central_start, central_frames, sar, mode, vui
):
    command = helper_command(
        helper, base, width, height, fps, total, central_start, central_frames, sar
    )
    with raw.open("rb") as stream:
        stdout, stderr, wall = run(
            command,
            stdin=stream,
            env={
                **os.environ,
                **vui["env"],
                "VSIZE_CONTEXT_MODE": mode,
            },
        )
    result = json.loads(stdout)
    result.update(command=command, wall_seconds=wall)
    base.with_suffix(".stderr.log").write_text(stderr)
    return result


def decode_counts(stderr):
    match = re.search(
        r"Input stream #0:\d+ \(video\): (\d+) packets read.*?; (\d+) frames decoded", stderr
    )
    output_match = re.search(r"Output stream #0:\d+ \(video\): (\d+) frames encoded", stderr)
    bench = re.search(r"bench: utime=([\d.]+)s stime=([\d.]+)s rtime=([\d.]+)s", stderr)
    return {
        "source_packets_parsed": int(match[1]) if match else None,
        "source_frames_decoded": int(match[2]) if match else None,
        "raw_frames_emitted": int(output_match[1]) if output_match else None,
        "decoder_cpu_seconds": float(bench[1]) + float(bench[2]) if bench else None,
        "decoder_internal_wall_seconds": float(bench[3]) if bench else None,
    }


def content_bytes(packets):
    identification_uuid = bytes.fromhex("dc45e9bde6d948b7962cd820d923eeef")
    return sum(
        len(nal) + 4
        for packet in packets
        for nal in avcc_nals(packet["payload"])
        if not (
            nal[0] & 31 == 6
            and len(nal) > 1
            and nal[1] == 5
            and identification_uuid in nal[:40]
            and b"x264 - core " in nal
        )
    )


def raw_frame_hashes(raw, bytes_per_frame):
    hashes = []
    with raw.open("rb") as stream:
        while frame := stream.read(bytes_per_frame):
            if len(frame) != bytes_per_frame:
                raise ValueError("partial raw frame")
            hashes.append(hashlib.md5(frame).hexdigest())
    return hashes


def continuous_source_hashes(media, start, count):
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
        media.input_path,
        "-map",
        f"0:{media.video['index']}",
        "-an",
        "-sn",
        "-dn",
        "-vf",
        f"trim=start_frame={start}:end_frame={start + count}",
        "-frames:v",
        str(count),
        "-threads",
        "1",
        "-fps_mode",
        "passthrough",
        "-pix_fmt",
        "yuv420p",
        "-f",
        "framemd5",
        "pipe:1",
    ]
    stdout, _, wall = run(command, pass_fds=(media.asset.fd,))
    hashes = [
        line.split(",")[-1].strip()
        for line in stdout.decode().splitlines()
        if line and not line.startswith("#")
    ]
    return hashes, command, wall


def process_alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def stop_process(process):
    if process.poll() is None:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=2)


def reap_decoder(process, timeout):
    """Use wait4 directly so FFmpeg's CPU is available even after EPIPE exit."""
    deadline = time.monotonic() + timeout
    while True:
        pid, status, usage = os.wait4(process.pid, os.WNOHANG)
        if pid:
            process.returncode = os.waitstatus_to_exitcode(status)
            return {
                "decoder_cpu_seconds": usage.ru_utime + usage.ru_stime,
                "decoder_user_cpu_seconds": usage.ru_utime,
                "decoder_system_cpu_seconds": usage.ru_stime,
            }
        if time.monotonic() >= deadline:
            raise subprocess.TimeoutExpired(process.args, timeout)
        time.sleep(0.001)


def encode_pipeline(
    helper,
    media,
    base,
    width,
    height,
    fps,
    total,
    central_start,
    central_frames,
    sar,
    start,
    duration,
    mode,
    vui,
    *,
    timeout=120,
):
    """Own/reap both process groups, including an early-closing rawvideo reader."""
    command = ffmpeg_decode_command(media, start, duration, "pipe:1", debug=True)
    command.insert(command.index("-benchmark") + 1, "-benchmark_all")
    helper_args = helper_command(
        helper, base, width, height, fps, total, central_start, central_frames, sar
    )
    started = time.monotonic()
    decoder = encoder = None
    decoder_usage = {}
    cleanup_errors = []
    timed_out = False
    with tempfile.TemporaryFile() as decoder_log, tempfile.TemporaryFile() as encoder_log:
        try:
            decoder = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=decoder_log,
                pass_fds=(media.asset.fd,),
                start_new_session=True,
            )
            encoder = subprocess.Popen(
                helper_args,
                stdin=decoder.stdout,
                stdout=subprocess.PIPE,
                stderr=encoder_log,
                env={**os.environ, **vui["env"], "VSIZE_CONTEXT_MODE": mode},
                start_new_session=True,
            )
            decoder.stdout.close()
            stdout, _ = encoder.communicate(timeout=timeout)
            encoder_finished = time.monotonic()
            try:
                decoder_usage = reap_decoder(decoder, timeout=2)
            except subprocess.TimeoutExpired:
                stop_process(decoder)
            decoder_finished = time.monotonic()
        except subprocess.TimeoutExpired:
            timed_out = True
            stdout = b""
            encoder_finished = time.monotonic()
            decoder_finished = encoder_finished
        finally:
            if decoder is not None and decoder.stdout is not None:
                decoder.stdout.close()
            for process in (encoder, decoder):
                if process is not None:
                    try:
                        stop_process(process)
                    except Exception as error:
                        cleanup_errors.append(repr(error))
            if encoder is not None and encoder.stdout is not None:
                encoder.stdout.close()
        decoder_log.seek(0)
        decoder_stderr = decoder_log.read().decode(errors="replace")
        encoder_log.seek(0)
        encoder_stderr = encoder_log.read().decode(errors="replace")
    base.with_suffix(".decoder.stderr.log").write_text(decoder_stderr)
    base.with_suffix(".stderr.log").write_text(encoder_stderr)
    cleanup = {
        "timed_out": timed_out,
        "encoder_returncode": encoder.returncode,
        "decoder_returncode": decoder.returncode,
        "encoder_pid": encoder.pid,
        "decoder_pid": decoder.pid,
        "encoder_reaped": not process_alive(encoder.pid),
        "decoder_reaped": not process_alive(decoder.pid),
        "cleanup_errors": cleanup_errors,
        "total_wall_seconds": time.monotonic() - started,
    }
    if timed_out:
        return cleanup
    if cleanup_errors or not cleanup["encoder_reaped"] or not cleanup["decoder_reaped"]:
        raise RuntimeError(f"pipeline process cleanup failed: {cleanup}")
    if encoder.returncode:
        raise RuntimeError(encoder_stderr[-3000:])
    if decoder.returncode and (
        mode != "stop"
        or decoder.returncode != 224
        or "Broken pipe" not in decoder_stderr
        or re.search(r"Error (?:while decoding|decoding)|[1-9]\d* decode errors", decoder_stderr)
    ):
        raise RuntimeError(decoder_stderr[-3000:])
    result = json.loads(stdout)
    result.update(cleanup)
    result.update(decode_counts(decoder_stderr))
    result.update(decoder_usage)
    result.update(
        command=helper_args,
        decoder_command=command,
        helper_completion_observed_wall_seconds=encoder_finished - started,
        decoder_reap_observed_wall_seconds=decoder_finished - started,
    )
    return result


def central(packets, start, count):
    return sorted(
        (packet for packet in packets if start <= packet["pts"] < start + count),
        key=lambda packet: packet["pts"],
    )


def build(helper, include):
    source = Path(__file__).with_name("x264_context.c")
    command = [
        "gcc",
        "-std=c11",
        "-O2",
        "-Wall",
        "-Wextra",
        "-Werror",
        f"-I{include}",
        str(source),
        "/lib/x86_64-linux-gnu/libx264.so.164",
        "-o",
        str(helper),
    ]
    run(command)
    return command


def study(args):
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    artifacts = ROOT / "artifacts" / output.name
    artifacts.mkdir(parents=True, exist_ok=False)
    helper = artifacts / "x264_context"
    build_command = build(helper, args.include)
    metadata = {
        "scope": "previously inspected development sources only; video-only medium CRF23 threads1",
        "frozen_source_root": str(SOURCE_ROOT),
        "source_code_hashes_before": code_hashes(),
        "build_command": build_command,
        "research_driver_sha256": digest(Path(__file__)),
        "research_helper_sha256": digest(Path(__file__).with_name("x264_context.c")),
        "library_sha256": digest(Path("/lib/x86_64-linux-gnu/libx264.so.164")),
        "header_sha256": digest(args.include / "x264.h"),
        "developer_package": "libx264-dev_0.164.3108+git31e19f9-2+b1_amd64.deb",
        "developer_package_url": "https://deb.debian.org/debian/pool/main/x/x264/libx264-dev_0.164.3108+git31e19f9-2+b1_amd64.deb",
        "ffmpeg": subprocess.check_output(["ffmpeg", "-version"], text=True),
        "cost_rule": "fed/analyzed and decoded frames are counted separately from emitted frames; no original fraction-budget claim",
        "artifact_root": str(artifacts),
    }
    save_json(output / "metadata.json", metadata)
    manifest = json.loads((ROOT / "artifacts/corpus-v1/manifest.json").read_text())
    if manifest["split"] != "development":
        raise ValueError("only development manifest is allowed")
    cases = {case["id"]: case for case in manifest["cases"]}
    results = []
    for name in args.cases:
        if name == "bunny":
            source = Path("/tmp/video-estimation-research/bunny-full.mkv")
            source_hash = digest(source)
        else:
            source = Path(cases[name]["source"])
            source_hash = cases[name]["sha256"]
            if digest(source) != source_hash:
                raise ValueError("development source changed")
        with PreparedAsset.from_path(source) as asset:
            media = Media(
                EncodeSpec(source, drop_audio=True),
                1,
                Cache(artifacts / name / "media-cache"),
                Deadline(1200),
                asset=asset,
            )
            fps = Fraction(media.video["r_frame_rate"])
            if fps != Fraction(media.video["avg_frame_rate"]):
                raise ValueError("initial raw API experiment requires a CFR source")
            width, height = media.video["width"], media.video["height"]
            sar_text = media.video.get("sample_aspect_ratio", "0:1")
            sar = (
                Fraction(sar_text.replace(":", "/"))
                if sar_text not in ("N/A", "0:1")
                else Fraction(0, 1)
            )
            vui = color_parameters(media.video)
            reference = media.encode(23)
            reference_packets, _ = mp4_packets(Path(reference["artifact"]), fps)
            global_start = min(int(12 * fps), int((media.duration - 5.5) * float(fps)))
            if global_start < 1:
                raise ValueError("source too short for a one-second guarded context study")
            global_count = max(1, round(float(fps)))
            reference_central = central(reference_packets, global_start, global_count)
            if [p["pts"] for p in reference_central] != list(
                range(global_start, global_start + global_count)
            ):
                raise ValueError("continuous central reference has missing or duplicate PTS")
            max_leading = min(global_start, round(max(args.leading) * float(fps)))
            validation_start = global_start - max_leading
            validation_count = max_leading + global_count + 45
            continuous_raw_hashes, validation_command, validation_wall = continuous_source_hashes(
                media, validation_start, validation_count
            )
            if len(continuous_raw_hashes) != validation_count:
                raise ValueError("continuous raw frame validation did not return the planned range")
            save_json(
                output / f"{name}-source-frame-proof.json",
                {
                    "start_frame": validation_start,
                    "frame_count": validation_count,
                    "command": validation_command,
                    "reference_only_wall_seconds": validation_wall,
                    "raw_frame_md5": continuous_raw_hashes,
                },
            )
            for leading in args.leading:
                leading_frames = min(global_start, round(leading * float(fps)))
                future_frames = 45
                central_start = leading_frames
                total = leading_frames + global_count + future_frames
                start = float(Fraction(global_start - leading_frames, 1) / fps)
                duration = float(Fraction(total, 1) / fps)
                prefix = f"{name}-leading{leading:g}"
                raw = artifacts / f"{prefix}.i420"
                decode_command = ffmpeg_decode_command(media, start, duration, raw, debug=True)
                _, stderr, decoder_wall = run(decode_command, pass_fds=(asset.fd,))
                (output / f"{prefix}-decoder.stderr.log").write_text(stderr)
                bytes_per_frame = width * height * 3 // 2
                if raw.stat().st_size % bytes_per_frame:
                    raise ValueError("decoder returned an incomplete raw frame")
                actual_frames = raw.stat().st_size // bytes_per_frame
                raw_hashes = raw_frame_hashes(raw, bytes_per_frame)
                offset = max_leading - leading_frames
                source_frames_match = (
                    raw_hashes == continuous_raw_hashes[offset : offset + actual_frames]
                    and actual_frames == total
                )
                padded = media.encode(23, window=(start, duration))
                padded_packets, padded_headers = mp4_packets(Path(padded["artifact"]), fps)
                raw_reference = artifacts / f"{prefix}-ffmpeg.mp4"
                raw_command = raw_ffmpeg_command(raw, raw_reference, width, height, fps, sar, vui)
                _, raw_stderr, ffmpeg_wall = run(raw_command)
                (output / f"{prefix}-ffmpeg.stderr.log").write_text(raw_stderr)
                ff_packets, ff_headers = mp4_packets(raw_reference, fps)
                full_base = artifacts / f"{prefix}-full"
                full = encode_helper(
                    helper,
                    raw,
                    full_base,
                    width,
                    height,
                    fps,
                    actual_frames,
                    central_start,
                    global_count,
                    sar,
                    "full",
                    vui,
                )
                full_packets, full_headers = helper_packets(full_base)
                equivalence = packet_equivalence(full_packets, ff_packets)
                equivalence["sps_pps"] = full_headers == ff_headers
                helper_decoded, _ = frame_hashes(full_base.with_suffix(".h264"))
                ff_decoded, _ = frame_hashes(raw_reference)
                equivalence["decoded_frames"] = helper_decoded == ff_decoded
                padded_equivalence = packet_equivalence(full_packets, padded_packets)
                padded_equivalence["sps_pps"] = full_headers == padded_headers
                stop_base = artifacts / f"{prefix}-stop"
                item = {
                    "id": prefix,
                    "source_hash": source_hash,
                    "geometry": [width, height],
                    "fps": str(fps),
                    "sar": str(sar),
                    "vui": vui,
                    "global_start_frame": global_start,
                    "leading_frames": leading_frames,
                    "central_frames": global_count,
                    "planned_future_frames": future_frames,
                    "decoded_raw_frames": actual_frames,
                    "ingest_seconds": asset.ingest_seconds,
                    "decoder_wall_seconds": decoder_wall,
                    "decode_command": decode_command,
                    "raw_ffmpeg_command": raw_command,
                    "raw_ffmpeg_wall_seconds": ffmpeg_wall,
                    "api_full": full,
                    "decoder_counts": decode_counts(stderr),
                    "source_raw_frames_match_continuous": source_frames_match,
                    "full_api_vs_raw_ffmpeg": equivalence,
                    "full_api_vs_native_padded_ffmpeg": padded_equivalence,
                    "native_padded_artifact": padded["artifact"],
                    "native_padded_packets": fingerprint_packets(padded_packets),
                    "raw_frame_md5": raw_hashes,
                    "full_packets": fingerprint_packets(full_packets),
                    "ffmpeg_packets": fingerprint_packets(ff_packets),
                    "hypothesis": "No-flush may avoid future packet encoding while retaining the same central lookahead decisions; warmup history bias may persist.",
                }
                if (
                    all(equivalence.values())
                    and all(padded_equivalence.values())
                    and source_frames_match
                ):
                    stop = encode_helper(
                        helper,
                        raw,
                        stop_base,
                        width,
                        height,
                        fps,
                        actual_frames,
                        central_start,
                        global_count,
                        sar,
                        "stop",
                        vui,
                    )
                    stop_packets, stop_headers = helper_packets(stop_base)
                    full_central = central(full_packets, central_start, global_count)
                    stop_central = central(stop_packets, central_start, global_count)
                    stop_match = packet_equivalence(stop_central, full_central)
                    stop_match["sps_pps"] = stop_headers == full_headers
                    decoded_stop, _ = frame_hashes(stop_base.with_suffix(".h264"))
                    presentation_pts = sorted(p["pts"] for p in stop_packets)
                    decoded_by_pts = dict(zip(presentation_pts, decoded_stop, strict=True))
                    stop_match["decoded_central_frames"] = [
                        decoded_by_pts[pts]
                        for pts in range(central_start, central_start + global_count)
                    ] == helper_decoded[central_start : central_start + global_count]
                    if not all(stop_match.values()) or len(stop_central) != global_count:
                        raise RuntimeError("no-flush changed the proven central packet sequence")
                    pipeline = {}
                    for mode in ("full", "stop"):
                        pipeline_base = artifacts / f"{prefix}-pipe-{mode}"
                        observation = encode_pipeline(
                            helper,
                            media,
                            pipeline_base,
                            width,
                            height,
                            fps,
                            actual_frames,
                            central_start,
                            global_count,
                            sar,
                            start,
                            duration,
                            mode,
                            vui,
                        )
                        pipe_packets, pipe_headers = helper_packets(pipeline_base)
                        expected = full_packets if mode == "full" else stop_packets
                        observation["packets_vs_file_mode"] = packet_equivalence(
                            pipe_packets, expected
                        )
                        observation["packets_vs_file_mode"]["sps_pps"] = (
                            pipe_headers == full_headers
                        )
                        if not all(observation["packets_vs_file_mode"].values()):
                            raise RuntimeError("direct pipe changed packet bytes")
                        pipeline[mode] = observation
                    if leading == args.leading[0]:
                        cancellation = encode_pipeline(
                            helper,
                            media,
                            artifacts / f"{prefix}-pipe-cancel",
                            width,
                            height,
                            fps,
                            actual_frames,
                            central_start,
                            global_count,
                            sar,
                            start,
                            duration,
                            "stop",
                            vui,
                            timeout=0.001,
                        )
                        if not cancellation["timed_out"] or not (
                            cancellation["encoder_reaped"] and cancellation["decoder_reaped"]
                        ):
                            raise RuntimeError("deadline cancellation did not reap both processes")
                        item["deadline_cancellation"] = cancellation
                    item.update(
                        api_stop=stop,
                        stop_central_vs_full=stop_match,
                        pipeline=pipeline,
                        stop_packets=fingerprint_packets(stop_packets),
                        central_content_bytes=content_bytes(stop_central),
                        continuous_central_content_bytes=content_bytes(reference_central),
                        central_error_vs_continuous=(
                            content_bytes(stop_central) / content_bytes(reference_central) - 1
                        ),
                        api_encoder_seconds_ratio=stop["encoder_seconds"] / full["encoder_seconds"],
                        api_encoder_cpu_seconds_ratio=(
                            stop["encoder_cpu_seconds"] / full["encoder_cpu_seconds"]
                        ),
                        pipeline_encoder_cpu_ratio=(
                            pipeline["stop"]["encoder_cpu_seconds"]
                            / pipeline["full"]["encoder_cpu_seconds"]
                        ),
                        pipeline_wall_ratio=(
                            pipeline["stop"]["total_wall_seconds"]
                            / pipeline["full"]["total_wall_seconds"]
                        ),
                        source_total_frames=len(reference_packets),
                        fed_fraction_of_continuous=stop["fed_frames"] / len(reference_packets),
                        emitted_fraction_of_continuous=stop["produced_packets"]
                        / len(reference_packets),
                        total_sequential_wall_ratio=(decoder_wall + stop["wall_seconds"])
                        / (decoder_wall + full["wall_seconds"]),
                    )
                else:
                    item["blocked"] = (
                        "Full API/native FFmpeg/source-frame equivalence is incomplete; stop cost/accuracy not claimed"
                    )
                results.append(item)
                save_json(output / "measurements.json", results)
                print(
                    json.dumps(
                        {
                            "case": prefix,
                            "full_equivalent": all(equivalence.values()),
                            "fed": item.get("api_stop", {}).get("fed_frames"),
                            "emitted": item.get("api_stop", {}).get("produced_packets"),
                            "central_equal": all(
                                item.get("stop_central_vs_full", {"unknown": False}).values()
                            ),
                            "cpu_ratio": item.get("api_encoder_cpu_seconds_ratio"),
                            "pipe_wall_ratio": item.get("pipeline_wall_ratio"),
                            "bias": item.get("central_error_vs_continuous"),
                        }
                    ),
                    flush=True,
                )
    metadata["source_code_hashes_after"] = code_hashes()
    metadata["frozen_source_unchanged"] = (
        metadata["source_code_hashes_after"] == metadata["source_code_hashes_before"]
    )
    save_json(output / "metadata.json", metadata)
    if not metadata["frozen_source_unchanged"]:
        raise RuntimeError("frozen src changed during the experiment")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", type=Path, default=ROOT / "results/stream-context-study-2026-10-03"
    )
    parser.add_argument(
        "--include", type=Path, default=Path("/tmp/vsize-x264-headers/extracted/usr/include")
    )
    parser.add_argument("--cases", nargs="+", default=["pedestrians"])
    parser.add_argument("--leading", type=float, nargs="+", default=[0, 2, 6])
    args = parser.parse_args()
    study(args)


if __name__ == "__main__":
    main()
