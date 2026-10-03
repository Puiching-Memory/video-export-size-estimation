"""Central-packet measurements from padded x264 encodes.

Padding reduces startup and lookahead bias; it does not reproduce the state of a
continuous encode. Raw measurements and a separate GOP correction hypothesis
are returned. Neither is a validated confidence interval or a final export.
"""

import json
import math
import re
import statistics
import time
from fractions import Fraction
from pathlib import Path

from .runtime import canonical_key


CONTEXT_PROBE_VERSION = "x264-central-packet-v1"
LEADING_SECONDS = 2.0
TRAILING_GUARD_FRAMES = 2
KEYINT_FRAMES = 250
# x264 preset defaults. These belong to the declared x264 pipeline, not to the
# source codec. Fingerprints version the policy if encoder defaults change.
PRESET_CONTEXT = {
    "ultrafast": (0, 0),
    "superfast": (0, 3),
    "veryfast": (10, 3),
    "faster": (20, 3),
    "fast": (30, 3),
    "medium": (40, 3),
    "slow": (50, 3),
    "slower": (60, 3),
    "veryslow": (60, 8),
}


def output_fps(media):
    rate = media.fps
    basis = "source_average"
    if media.spec.video_filter:
        for part in media.spec.video_filter.split(","):
            if match := re.fullmatch(r"fps=(\d+(?:/\d+)?)", part):
                rate = float(Fraction(match[1]))
                basis = "explicit_fps_filter"
    if not math.isfinite(rate) or rate <= 0:
        raise ValueError("a finite positive output frame rate is required for context padding")
    return rate, basis


def policy(media):
    fps, basis = output_fps(media)
    lookahead, b_delay = PRESET_CONTEXT[media.spec.preset]
    return {
        "version": CONTEXT_PROBE_VERSION,
        "preset": media.spec.preset,
        "output_fps": fps,
        "fps_basis": basis,
        "leading_seconds": LEADING_SECONDS,
        "lookahead_frames": lookahead,
        "b_delay_frames": b_delay,
        "trailing_guard_frames": TRAILING_GUARD_FRAMES,
        "keyint_frames": KEYINT_FRAMES,
        "audio_assignment": "packet_pts_in_half_open_interval",
        "initialization_sei": "exclude_from_content_add_once_to_global_total",
        "periodic_adjustment": "optional_max_zero_periodic_minus_observed_i_rate",
        "frame_rate_limitation": "average source fps does not guarantee VFR frame coverage",
    }


def policy_fingerprint(media):
    return canonical_key(policy(media))


def context_window(media, window):
    """Return the padded input span and preflight cost before encoding.

    ``encoded_media_seconds`` here is the requested padded span. ``probe``
    replaces it with the observed output-video duration; timestamps/FPS filters
    can round that duration. Leading context is an explicit two-second policy.
    """
    start, duration = map(float, window)
    if not all(math.isfinite(v) for v in [start, duration]):
        raise ValueError("finite context-window timestamps required")
    if (
        start < 0
        or start >= media.duration
        or duration <= 0
        or start + duration > media.duration + 1e-6
    ):
        raise ValueError("context window must be within the video timeline")
    end = min(media.duration, start + duration)
    configuration = policy(media)
    fps = configuration["output_fps"]
    future = (
        configuration["lookahead_frames"]
        + configuration["b_delay_frames"]
        + configuration["trailing_guard_frames"]
    ) / fps
    padded_start = max(0.0, start - LEADING_SECONDS)
    padded_end = min(media.duration, end + future)
    return {
        "window": (start, end - start),
        "encoding_window": (padded_start, padded_end - padded_start),
        "central_offset": start - padded_start,
        "central_duration": end - start,
        "encoded_media_seconds": padded_end - padded_start,
        "requested_future_seconds": future,
        "available_future_seconds": padded_end - end,
        "at_source_end": end >= media.duration - 1e-6,
        "policy": configuration,
        "policy_fingerprint": canonical_key(configuration),
    }


def _packets_in_window(packets, start, end):
    return [
        packet
        for packet in packets
        if "pts_time" in packet and start - 1e-7 <= float(packet["pts_time"]) < end - 1e-7
    ]


def _initialization_sei_bytes(path, packet, length_bytes, deadline):
    """Count whole AVCC SEI NALs containing only unregistered user data."""
    deadline.check()
    with path.open("rb") as stream:
        stream.seek(int(packet["pos"]))
        data = stream.read(int(packet["size"]))
    offset = 0
    result = 0
    while offset < len(data):
        if len(data) - offset < length_bytes:
            raise RuntimeError("incomplete AVCC NAL length")
        size = int.from_bytes(data[offset : offset + length_bytes], "big")
        offset += length_bytes
        if size <= 0 or size > len(data) - offset:
            raise RuntimeError("invalid AVCC NAL size")
        nal = data[offset : offset + size]
        offset += size
        if nal[0] & 31 != 6:
            continue
        # Remove emulation-prevention bytes before parsing the SEI payload list.
        rbsp = nal[1:].replace(b"\x00\x00\x03", b"\x00\x00")
        position = 0
        payload_types = []
        while position < len(rbsp) and rbsp[position:] != b"\x80":
            payload_type = 0
            while position < len(rbsp) and rbsp[position] == 255:
                payload_type += 255
                position += 1
            if position >= len(rbsp):
                raise RuntimeError("incomplete SEI payload type")
            payload_type += rbsp[position]
            position += 1
            payload_size = 0
            while position < len(rbsp) and rbsp[position] == 255:
                payload_size += 255
                position += 1
            if position >= len(rbsp):
                raise RuntimeError("incomplete SEI payload size")
            payload_size += rbsp[position]
            position += 1
            if payload_size > len(rbsp) - position:
                raise RuntimeError("incomplete SEI payload")
            position += payload_size
            payload_types.append(payload_type)
        if payload_types and all(kind == 5 for kind in payload_types):
            result += size + length_bytes
    deadline.check()
    return result


def probe(media, crf, window):
    """Encode/reuse padding, then measure only central presentation packets.

    The returned artifact is deliberately called ``warmup_artifact``. It is a
    probe and must never be passed off as the requested continuous final file.
    ``payload_bytes`` excludes initialization SEI; a final total should add
    ``global_init_sei_bytes`` once. The GOP-adjusted candidate is separate.
    """
    started = time.monotonic()
    plan = context_window(media, window)
    encoded = media.encode(crf, plan["encoding_window"])
    artifact = Path(encoded["artifact"])
    output = json.loads(
        media.deadline.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_streams",
                "-show_format",
                "-show_packets",
                "-show_entries",
                "packet=stream_index,pos,size,pts_time,duration_time:"
                "stream=index,codec_type,duration,width,height,nal_length_size:format=duration,size",
                "-of",
                "json",
                str(artifact),
            ]
        ).stdout
    )
    video = next(s for s in output["streams"] if s["codec_type"] == "video")
    frames = json.loads(
        media.deadline.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-threads",
                str(media.threads),
                "-select_streams",
                str(video["index"]),
                "-show_frames",
                "-show_entries",
                "frame=pkt_pos,pict_type,pts_time",
                "-of",
                "json",
                str(artifact),
            ]
        ).stdout
    )["frames"]
    by_position = {frame["pkt_pos"]: frame for frame in frames if "pkt_pos" in frame}
    video_packets = [p for p in output["packets"] if p["stream_index"] == video["index"]]
    for index, packet in enumerate(video_packets):
        if index % 128 == 0:
            media.deadline.check()
        frame = by_position.get(packet.get("pos"))
        if frame is None:
            raise RuntimeError("encoded video packet has no matching decoded frame")
        packet["pict_type"] = frame["pict_type"]
    if not video_packets or len(video_packets) != len(frames):
        raise RuntimeError("encoded video frame/packet count mismatch")
    origin = min(float(p["pts_time"]) for p in video_packets)
    central_start = origin + plan["central_offset"]
    central_end = central_start + plan["central_duration"]
    central_video = _packets_in_window(video_packets, central_start, central_end)
    if not central_video:
        raise RuntimeError("central context window has no encoded video frames")
    audio_streams = [s["index"] for s in output["streams"] if s["codec_type"] == "audio"]
    audio_packets = [p for p in output["packets"] if p["stream_index"] in audio_streams]
    central_audio = _packets_in_window(audio_packets, central_start, central_end)
    duration = plan["central_duration"]
    first = min(video_packets, key=lambda p: float(p["pts_time"]))
    length_bytes = int(video["nal_length_size"])
    global_sei = _initialization_sei_bytes(artifact, first, length_bytes, media.deadline)
    central_sei = sum(
        _initialization_sei_bytes(artifact, p, length_bytes, media.deadline)
        for p in central_video
        if p["pict_type"] == "I"
    )
    picture_types = {
        kind: {
            "frames": sum(p["pict_type"] == kind for p in central_video),
            "payload_bytes": sum(int(p["size"]) for p in central_video if p["pict_type"] == kind),
        }
        for kind in ["I", "P", "B"]
    }
    for kind, values in picture_types.items():
        values["frames_per_second"] = values["frames"] / duration
        values["bytes_per_second"] = values["payload_bytes"] / duration
        values["content_payload_bytes"] = values["payload_bytes"] - (
            central_sei if kind == "I" else 0
        )
        values["content_bytes_per_second"] = values["content_payload_bytes"] / duration
    raw_video = sum(int(p["size"]) for p in central_video)
    audio_payload = sum(int(p["size"]) for p in central_audio)
    payload = raw_video - central_sei + audio_payload
    non_i = [int(p["size"]) for p in video_packets if p["pict_type"] != "I"]
    first_i_excess = (
        max(0.0, int(first["size"]) - global_sei - statistics.fmean(non_i)) if non_i else 0.0
    )
    expected_i_rate = plan["policy"]["output_fps"] / KEYINT_FRAMES
    observed_i_rate = picture_types["I"]["frames"] / duration
    extra_is = max(0.0, expected_i_rate - observed_i_rate) * duration
    periodic_extra = extra_is * first_i_excess
    video_end = max(float(p["pts_time"]) + float(p.get("duration_time", 0)) for p in video_packets)
    encoded_media_seconds = video_end - origin
    future_frames = len([p for p in video_packets if float(p["pts_time"]) >= central_end - 1e-7])
    return {
        "key": canonical_key(
            {"policy": plan["policy_fingerprint"], "warm_key": encoded["key"], "window": window}
        ),
        "probe_kind": CONTEXT_PROBE_VERSION,
        "policy_fingerprint": plan["policy_fingerprint"],
        "policy": plan["policy"],
        "crf": crf,
        "window": list(plan["window"]),
        "encoding_window": list(plan["encoding_window"]),
        "duration": duration,
        "video_frames": len(central_video),
        "width": video["width"],
        "height": video["height"],
        "raw_payload_bytes": raw_video + audio_payload,
        "payload_bytes": payload,
        "video_payload_bytes": raw_video - central_sei,
        "raw_video_payload_bytes": raw_video,
        "audio_payload_bytes": audio_payload,
        "audio_packets": len(central_audio),
        "global_init_sei_bytes": global_sei,
        "central_init_sei_bytes": central_sei,
        "global_audio_priming_bytes": sum(
            int(p["size"]) for p in audio_packets if float(p.get("pts_time", 0)) < origin
        ),
        "picture_types": picture_types,
        "periodic_adjusted_payload_bytes": payload + periodic_extra,
        "periodic_adjustment": {
            "kind": "unvalidated_model_candidate",
            "extra_i_frames": extra_is,
            "first_i_excess_excluding_sei": first_i_excess,
            "payload_bytes": periodic_extra,
            "observed_i_frames_per_second": observed_i_rate,
            "target_periodic_i_frames_per_second": expected_i_rate,
        },
        "encoded_media_seconds": encoded_media_seconds,
        "requested_encoded_media_seconds": plan["encoded_media_seconds"],
        "available_future_frames": future_frames,
        "future_context_truncated_at_source_end": plan["available_future_seconds"]
        < plan["requested_future_seconds"] - 1e-6,
        "warmup_artifact": str(artifact),
        "warmup_record_key": encoded["key"],
        "cache_hit": encoded["cache_hit"],
        "encode_wall_seconds": encoded["encode_wall_seconds"],
        "transcode_wall_seconds": encoded["transcode_wall_seconds"],
        "measurement_wall_seconds": time.monotonic() - started,
        "source_hash": encoded["source_hash"],
        "toolchain_key": encoded["toolchain_key"],
        "configuration": encoded["configuration"],
        "bias_status": "context_approximation_requires_validation",
    }
