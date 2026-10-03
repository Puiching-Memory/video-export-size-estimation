"""An explicit, independently encoded CFR segment export.

These segments are the actual final video samples, not approximations of a
continuous x264 encode.  A caller must invoke ``verify_source`` before claiming
that a complete export or estimate refers to the initially hashed input.
"""

import json
import math
import os
import re
import tempfile
import time
from dataclasses import dataclass, replace
from fractions import Fraction
from pathlib import Path

from .media import Media
from .runtime import canonical_key


SEGMENTED_PIPELINE_VERSION = "independent-cfr-x264-v1"

# Static, conservative 8-bit component formats.  Storage size (rgb24/rgba) is
# not component precision; formats outside this list need an explicit review.
EIGHT_BIT_PIXEL_FORMATS = frozenset(
    {
        "yuv420p",
        "yuv422p",
        "yuv444p",
        "yuv410p",
        "yuv411p",
        "yuv440p",
        "yuvj420p",
        "yuvj422p",
        "yuvj444p",
        "yuvj440p",
        "yuvj411p",
        "yuva420p",
        "yuva422p",
        "yuva444p",
        "nv12",
        "nv21",
        "nv16",
        "nv24",
        "nv42",
        "yuyv422",
        "uyvy422",
        "yvyu422",
        "rgb24",
        "bgr24",
        "rgba",
        "argb",
        "bgra",
        "abgr",
        "rgb0",
        "bgr0",
        "0rgb",
        "0bgr",
        "gbrp",
        "gbrap",
        "gray",
        "gray8",
        "pal8",
    }
)


@dataclass(frozen=True)
class Segment:
    index: int
    start_frame: int
    frame_count: int
    start_tick: int | None = None
    end_tick: int | None = None

    def configuration(self):
        return {
            "index": self.index,
            "start_frame": self.start_frame,
            "frame_count": self.frame_count,
            "start_tick": self.start_tick,
            "end_tick": self.end_tick,
        }


def _fraction(value):
    try:
        rate = Fraction(value)
    except (ValueError, ZeroDivisionError, TypeError):
        raise ValueError("finite positive rational frame rate required") from None
    if rate <= 0:
        raise ValueError("finite positive rational frame rate required")
    return rate


def _extradata(value):
    # FFprobe's -show_data dump has a hex offset, hex words, and an ASCII column.
    result = bytearray()
    for line in value.splitlines():
        if ":" not in line:
            continue
        words = line.split(":", 1)[1].split("  ", 1)[0].strip().split()
        result.extend(bytes.fromhex("".join(words)))
    return bytes(result)


def _stream_start(stream):
    start_pts = _optional_integer(stream.get("start_pts"), "start_pts")
    if start_pts is not None and stream.get("time_base") not in (None, "N/A", ""):
        return start_pts * _fraction(stream["time_base"])
    start_time = stream.get("start_time")
    if start_time in (None, "N/A", ""):
        raise ValueError("explicit video/audio presentation starts required for audio alignment")
    try:
        return Fraction(start_time)
    except (ValueError, ZeroDivisionError, TypeError):
        raise ValueError("finite video/audio presentation starts required") from None


def _optional_integer(value, name):
    if value in (None, "N/A", ""):
        return None
    try:
        return int(value)
    except (ValueError, TypeError):
        raise ValueError(f"invalid {name} metadata") from None


class SegmentedMedia:
    """Reuse a checked Media identity, its cache, threads and shared deadline.

    ``prepare`` scans the original packet timestamps once.  In the absence of
    an explicit fps filter, it rejects variable timestamps even if the stream's
    nominal rates happen to match.  With fps conversion, it counts the complete
    converted sequence before choosing integer frame boundaries.

    Source stat identity is checked at construction and around each subprocess.
    No segment repeats the potentially large source SHA computation.  The final
    ``verify_source`` checks either the immutable prepared asset's seal/identity
    or a complete source digest, and is mandatory at the publication boundary.
    """

    def __init__(self, media: Media, segment_seconds: float = 4.0):
        if not math.isfinite(segment_seconds) or segment_seconds <= 0:
            raise ValueError("segment_seconds must be finite and positive")
        self.media = media
        self.spec, self.cache, self.deadline = media.spec, media.cache, media.deadline
        self.threads = media.threads
        self.segment_seconds = segment_seconds
        self.source_hash = media.source_hash
        self.toolchain_key = media.toolchain_key
        self._source_stat = self._stat()
        self._prepared = False
        self.prepare_cache_hit = False
        self.source_verified = False
        self.identity_check = (
            "sealed prepared asset; identity required before publication"
            if media.asset is not None
            else "stat_guard; full SHA256 required before publication"
        )
        self.segments = ()
        self.video_signature = None
        self.video_signatures = {}
        self.fps_filter = None
        for part in (self.spec.video_filter or "").split(","):
            if part.startswith("fps="):
                if self.fps_filter is not None:
                    raise ValueError("segmented mode supports only one explicit fps filter")
                self.fps_filter = _fraction(part[4:])
        transfer = str(media.video.get("color_transfer", ""))
        bit_depth = _optional_integer(media.video.get("bits_per_raw_sample"), "bits_per_raw_sample")
        pixel_format = str(media.video.get("pix_fmt", ""))
        if pixel_format not in EIGHT_BIT_PIXEL_FORMATS or bit_depth not in (None, 0, 8):
            raise ValueError("independent-segment mode requires a known 8-bit SDR pixel format")
        if transfer in {"smpte2084", "arib-std-b67"}:
            raise ValueError("independent-segment mode currently requires SDR input")
        if media.audio is not None:
            sample_rate = _optional_integer(media.audio.get("sample_rate"), "sample_rate") or 0
            channels = _optional_integer(media.audio.get("channels"), "channels") or 0
            if not 0 < sample_rate < 65536 or not 0 < channels <= 2:
                raise ValueError("independent-segment MP4 supports mono/stereo AAC below 65536 Hz")
            if abs(_stream_start(media.audio) - _stream_start(media.video)) > Fraction(
                1, sample_rate
            ):
                raise ValueError(
                    "independent-segment MP4 currently requires aligned video/audio starts"
                )

    def _stat(self):
        status = (
            os.fstat(self.media.asset.fileno())
            if self.media.asset is not None
            else self.spec.source.stat()
        )
        return (
            status.st_dev,
            status.st_ino,
            status.st_size,
            status.st_mtime_ns,
            status.st_ctime_ns,
        )

    def _guard_source(self):
        self.deadline.check()
        if self._stat() != self._source_stat:
            raise RuntimeError("input changed during segmented analysis; result was discarded")

    def _run(self, command):
        self._guard_source()
        result = self.deadline.run(command)
        self._guard_source()
        return result

    def verify_source(self):
        """Verify the immutable asset or full source at the publication boundary."""
        self._guard_source()
        self.media.verify_source()
        self._guard_source()
        self.source_verified = True
        return self.source_hash

    def prepare(self):
        if self._prepared:
            self._guard_source()
            return self.plan()
        started = time.monotonic()
        plan_cache_key = canonical_key(
            {
                "pipeline": SEGMENTED_PIPELINE_VERSION,
                "kind": "segment-plan",
                "source": self.source_hash,
                "toolchain": self.toolchain_key,
                "filter": self.spec.video_filter,
                "segment_seconds": self.segment_seconds,
            }
        )
        plan_cache = self.cache.root / f"{plan_cache_key}.plan.json"
        try:
            stored = json.loads(plan_cache.read_text())
            values = stored["values"]
            if stored["checksum"] != canonical_key(values) or stored["key"] != plan_cache_key:
                raise ValueError("invalid cached segment plan")
            self.source_frames = values["source_frames"]
            self.source_time_base = _fraction(values["source_time_base"])
            self.source_origin_tick = values["source_origin_tick"]
            self.output_rate = _fraction(values["output_rate"])
            self.total_frames = values["total_frames"]
            self.frames_per_segment = values["segment_frames"]
            self.segments = tuple(Segment(**segment) for segment in values["segments"])
            if (
                sum(s.frame_count for s in self.segments) != self.total_frames
                or not self.segments
                or self.total_frames < 1
            ):
                raise ValueError("invalid cached segment frame plan")
            self.duration = float(Fraction(self.total_frames, 1) / self.output_rate)
            self.plan_hash = values["plan_hash"]
            self.prepare_cache_hit = True
            self.prepare_seconds = time.monotonic() - started
            self._prepared = True
            self._guard_source()
            return self.plan()
        except (OSError, ValueError, KeyError, TypeError):
            pass
        raw = self._run(
            [
                "ffprobe",
                "-v",
                "error",
                "-threads",
                str(self.threads),
                "-select_streams",
                str(self.media.video["index"]),
                "-show_packets",
                "-show_entries",
                "packet=pts,duration",
                "-of",
                "json",
                self.media.input_path,
            ]
        ).stdout
        frames = json.loads(raw).get("packets", [])
        if not frames or any("pts" not in frame for frame in frames):
            raise ValueError("video sample presentation timestamps are required")
        # Packet planning assumes one coded picture per sample.  The actual
        # decoder/encoder frame counts are checked for every complete segment.
        supported = {
            "h264",
            "hevc",
            "av1",
            "vp8",
            "vp9",
            "mpeg4",
            "rawvideo",
            "ffv1",
            "prores",
            "mjpeg",
            "png",
        }
        if self.media.video.get("codec_name") not in supported:
            raise ValueError("source codec lacks the one-picture-per-sample segment contract")
        frames.sort(key=lambda frame: int(frame["pts"]))
        self.source_frames = len(frames)
        declared_frames = self.media.video.get("nb_frames")
        if declared_frames not in (None, "N/A") and int(declared_frames) != self.source_frames:
            raise ValueError("source sample count differs from its declared frame count")
        timestamps = [int(frame["pts"]) for frame in frames]
        if any(right <= left for left, right in zip(timestamps, timestamps[1:])):
            raise ValueError("strictly increasing source frame timestamps required")
        self.source_time_base = _fraction(self.media.video["time_base"])
        self.source_origin_tick = timestamps[0]
        if self.fps_filter is None:
            average = _fraction(self.media.video.get("avg_frame_rate", "0/1"))
            nominal = _fraction(self.media.video.get("r_frame_rate", "0/1"))
            if average != nominal:
                raise ValueError("VFR input requires an explicit fps filter in segmented mode")
            expected_ticks = 1 / (average * self.source_time_base)
            # Integer timestamp rounding may alternate neighboring tick counts.
            if any(
                abs((tick - timestamps[0]) - n * expected_ticks) > 1
                for n, tick in enumerate(timestamps)
            ):
                raise ValueError("VFR input requires an explicit fps filter in segmented mode")
            self.output_rate = average
            self.total_frames = self.source_frames
            final_duration = int(
                frames[-1].get("duration")
                or frames[-1].get("pkt_duration")
                or round(expected_ticks)
            )
            end_tick = timestamps[-1] + final_duration
        else:
            self.output_rate = self.fps_filter
            # Keep the original filter's FPS phase.  Source pixels are not
            # retained; this scan counts only the declared full output sequence.
            converted = self._run(
                [
                    "ffmpeg",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-nostdin",
                    "-xerror",
                    "-threads",
                    str(self.threads),
                    "-filter_threads",
                    str(self.threads),
                    "-filter_complex_threads",
                    str(self.threads),
                    "-i",
                    self.media.input_path,
                    "-map",
                    f"0:{self.media.video['index']}",
                    "-an",
                    "-sn",
                    "-dn",
                    "-vf",
                    self.spec.video_filter,
                    "-fps_mode",
                    "passthrough",
                    "-progress",
                    "pipe:1",
                    "-f",
                    "null",
                    "-",
                ]
            ).stdout
            counts = re.findall(rb"(?:^|\n)frame=(\d+)", converted)
            self.total_frames = int(counts[-1]) if counts else 0
            if self.total_frames < 1:
                raise ValueError("fps conversion returned no output frames")
            end_tick = None
        frames_per_segment = max(1, round(Fraction(str(self.segment_seconds)) * self.output_rate))
        self.segments = tuple(
            Segment(
                index=index,
                start_frame=start,
                frame_count=min(frames_per_segment, self.total_frames - start),
                start_tick=timestamps[start] if self.fps_filter is None else None,
                end_tick=(
                    timestamps[min(start + frames_per_segment, self.total_frames)]
                    if start + frames_per_segment < self.total_frames
                    else end_tick
                )
                if self.fps_filter is None
                else None,
            )
            for index, start in enumerate(range(0, self.total_frames, frames_per_segment))
        )
        self.frames_per_segment = frames_per_segment
        self.duration = float(Fraction(self.total_frames, 1) / self.output_rate)
        self.plan_hash = canonical_key(
            {
                "pipeline": SEGMENTED_PIPELINE_VERSION,
                "source": self.source_hash,
                "output_rate": str(self.output_rate),
                "segments": [segment.configuration() for segment in self.segments],
            }
        )
        self.prepare_seconds = time.monotonic() - started
        self._prepared = True
        values = {
            **self.plan(),
            "source_time_base": str(self.source_time_base),
            "source_origin_tick": self.source_origin_tick,
        }
        descriptor, path = tempfile.mkstemp(
            prefix="partial-plan-", suffix=".json", dir=self.cache.root
        )
        try:
            with os.fdopen(descriptor, "w") as stream:
                json.dump(
                    {"key": plan_cache_key, "values": values, "checksum": canonical_key(values)},
                    stream,
                    sort_keys=True,
                )
                stream.flush()
                os.fsync(stream.fileno())
            self._guard_source()
            os.replace(path, plan_cache)
        finally:
            Path(path).unlink(missing_ok=True)
        return self.plan()

    def plan(self):
        if not self._prepared:
            return self.prepare()
        return {
            "pipeline": SEGMENTED_PIPELINE_VERSION,
            "semantics": "independent_segments",
            "source_hash": self.source_hash,
            "toolchain_key": self.toolchain_key,
            "plan_hash": self.plan_hash,
            "source_frames": self.source_frames,
            "source_geometry": [self.media.video["width"], self.media.video["height"]],
            "source_frame_count_method": "one_picture_per_sample; per-segment encoded count verified",
            "output_rate": str(self.output_rate),
            "total_frames": self.total_frames,
            "duration": self.duration,
            "segment_frames": self.frames_per_segment,
            "segments": [segment.configuration() for segment in self.segments],
            "prepare_seconds": self.prepare_seconds,
            "prepare_cache_hit": self.prepare_cache_hit,
            "source_identity_verified_at_publication": self.source_verified,
            "fps_conversion_access": "decode_from_start" if self.fps_filter else "timestamp_seek",
        }

    def _segment(self, index):
        self.prepare()
        if type(index) is not int or not 0 <= index < len(self.segments):
            raise ValueError("segment index outside the fixed export plan")
        return self.segments[index]

    def key(self, index, crf):
        segment = self._segment(index)
        if not math.isfinite(crf) or not 0 <= crf <= 51:
            raise ValueError("crf must be between zero and 51")
        return canonical_key(
            {
                "pipeline": SEGMENTED_PIPELINE_VERSION,
                "plan": self.plan_hash,
                "source": self.source_hash,
                "toolchain": self.toolchain_key,
                "threads": self.threads,
                "configuration": replace(self.spec, crf=crf).configuration(),
                "segment": segment.configuration(),
            }
        )

    def lookup_segment(self, index, crf):
        self._guard_source()
        record = self.cache.lookup(self.key(index, crf), self.deadline)
        if record:
            self._accept_video_signature(record)
        return record

    def _inspect(self, artifact, selector):
        output = json.loads(
            self._run(
                [
                    "ffprobe",
                    "-v",
                    "error",
                    "-select_streams",
                    selector,
                    "-show_streams",
                    "-show_packets",
                    "-show_data",
                    "-show_entries",
                    "stream=codec_type,codec_name,width,height,time_base,nb_frames,extradata,"
                    "extradata_size,start_pts,duration_ts,sample_rate,channels:"
                    "packet=size,pts,dts,duration,flags,side_data_list",
                    "-of",
                    "json",
                    str(artifact),
                ]
            ).stdout
        )
        stream = output["streams"][0]
        packets = output.get("packets", [])
        if not packets or any(not {"pts", "dts", "duration"} <= set(p) for p in packets):
            raise RuntimeError("encoded segment has incomplete packet timestamps")
        extradata = _extradata(stream.get("extradata", ""))
        if len(extradata) != int(stream.get("extradata_size", 0)) or not extradata:
            raise RuntimeError("encoded segment lacks complete codec extradata")
        return {
            "codec": stream["codec_name"],
            "time_base": stream["time_base"],
            "extradata_hex": extradata.hex(),
            "width": stream.get("width", 0),
            "height": stream.get("height", 0),
            "sample_rate": int(stream.get("sample_rate", 0)),
            "channels": stream.get("channels", 0),
            "packet_count": len(packets),
            "payload_bytes": sum(int(packet["size"]) for packet in packets),
            "first_pts": min(int(packet["pts"]) for packet in packets),
            "last_pts": max(int(packet["pts"]) for packet in packets),
            "first_dts": int(packets[0]["dts"]),
            "last_dts": int(packets[-1]["dts"]),
            "duration_ticks": sum(int(packet["duration"]) for packet in packets),
            "presentation_start": int(stream.get("start_pts", 0)),
            "presentation_duration": int(stream["duration_ts"])
            if "duration_ts" in stream
            else None,
            "first_keyframe": "K" in packets[0].get("flags", ""),
            "priming_side_data": [
                packet["side_data_list"] for packet in packets if packet.get("side_data_list")
            ],
        }

    def _accept_video_signature(self, record):
        signature = {
            name: record[name]
            for name in ("codec", "time_base", "extradata_hex", "width", "height")
        }
        candidate = str(record["crf"])
        prior = self.video_signatures.get(candidate)
        if prior is not None and prior != signature:
            raise RuntimeError("independent segments have incompatible AVC configuration")
        self.video_signatures[candidate] = signature
        self.video_signature = signature

    def encode_segment(self, index, crf):
        segment = self._segment(index)
        key = self.key(index, crf)
        if record := self.lookup_segment(index, crf):
            return record
        temporary = self.cache.temporary()
        started = time.monotonic()
        command = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "info",
            "-nostdin",
            "-y",
            "-xerror",
            "-benchmark",
            "-nostats",
            "-threads",
            str(self.threads),
            "-filter_threads",
            str(self.threads),
            "-filter_complex_threads",
            str(self.threads),
        ]
        if self.fps_filter is None:
            # Seek before the exact integer-PTS boundary, then discard every
            # earlier frame.  Float seek precision never decides inclusion.
            start_seconds = float(segment.start_tick * self.source_time_base)
            seek = max(
                0.0, start_seconds - float(self.source_origin_tick * self.source_time_base) - 2
            )
            command += ["-ss", f"{seek:.6f}", "-copyts"]
        command += [
            "-i",
            self.media.input_path,
            "-map",
            f"0:{self.media.video['index']}",
            "-an",
            "-sn",
            "-dn",
            "-map_metadata",
            "-1",
        ]
        filters = [self.spec.video_filter] if self.spec.video_filter else []
        if self.fps_filter is None:
            filters += [f"trim=start_pts={segment.start_tick}:end_pts={segment.end_tick}"]
        else:
            filters += [
                f"trim=start_frame={segment.start_frame}:"
                f"end_frame={segment.start_frame + segment.frame_count}"
            ]
        filters += ["setpts=PTS-STARTPTS"]
        # Fixed GOP parameters prevent the tail segment's length or content
        # from altering AVC sample-entry configuration.  Each encoder starts
        # independently, and its first SEI remains part of the actual payload.
        # The explicit, verified rate gives even the final sample its complete
        # duration.  FFmpeg passthrough + trim otherwise writes a zero-duration
        # tail sample on some inputs.  Inclusion still uses integer PTS/frame
        # boundaries, and the actual frame count is verified below.
        command += [
            "-vf",
            ",".join(filters),
            "-fps_mode",
            "cfr",
            "-r",
            str(self.output_rate),
            "-c:v",
            "libx264",
            "-crf",
            str(crf),
            "-preset",
            self.spec.preset,
            "-threads",
            str(self.threads),
            "-pix_fmt",
            "yuv420p",
            "-x264-params",
            "open-gop=0:keyint=250:min-keyint=1",
            "-movflags",
            "+faststart",
            str(temporary),
        ]
        try:
            encoded = self._run(command)
            process_seconds = time.monotonic() - started
            measured = re.search(
                rb"bench: utime=[\d.]+s stime=[\d.]+s rtime=([\d.]+)s", encoded.stderr
            )
            summary = self._inspect(temporary, "v:0")
            if summary["packet_count"] != segment.frame_count:
                raise RuntimeError(
                    f"segment {index} frame count mismatch: "
                    f"{summary['packet_count']} != {segment.frame_count}"
                )
            if not summary["first_keyframe"] or summary["first_pts"] != 0:
                raise RuntimeError(
                    "independent segment must start with an IDR at presentation time zero"
                )
            record = {
                **summary,
                "key": key,
                "crf": crf,
                "segment": segment.configuration(),
                "plan_hash": self.plan_hash,
                "source_hash": self.source_hash,
                "toolchain_key": self.toolchain_key,
                "configuration": replace(self.spec, crf=crf).configuration(),
                "video_frames": segment.frame_count,
                "duration": float(segment.frame_count / self.output_rate),
                "global_start_frame": segment.start_frame,
                "encode_wall_seconds": time.monotonic() - started,
                "transcode_wall_seconds": float(measured[1]) if measured else process_seconds,
                "source_identity_check": self.identity_check,
            }
            self._accept_video_signature(record)
            self._guard_source()
            return self.cache.publish(key, temporary, record, self.deadline)
        finally:
            temporary.unlink(missing_ok=True)

    def audio_key(self):
        """The full-audio cache identity, available before attempting an encode."""
        if self.media.audio is None:
            return None
        return canonical_key(
            {
                "pipeline": SEGMENTED_PIPELINE_VERSION,
                "kind": "full-audio",
                "source": self.source_hash,
                "toolchain": self.toolchain_key,
                "threads": self.threads,
                "audio_stream": self.media.audio["index"],
                "audio_bitrate": self.spec.audio_bitrate,
            }
        )

    def lookup_audio(self):
        """Return a verified complete audio artifact without starting an encode."""
        self._guard_source()
        key = self.audio_key()
        return self.cache.lookup(key, self.deadline) if key is not None else None

    def encode_audio(self):
        """Encode the complete first audio stream once, including any video tail overhang."""
        self._guard_source()
        key = self.audio_key()
        if key is None:
            return None
        if record := self.lookup_audio():
            return record
        temporary = self.cache.temporary()
        started = time.monotonic()
        try:
            self._run(
                [
                    "ffmpeg",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-nostdin",
                    "-y",
                    "-xerror",
                    "-threads",
                    str(self.threads),
                    "-filter_threads",
                    str(self.threads),
                    "-filter_complex_threads",
                    str(self.threads),
                    "-i",
                    self.media.input_path,
                    "-map",
                    f"0:{self.media.audio['index']}",
                    "-vn",
                    "-sn",
                    "-dn",
                    "-map_metadata",
                    "-1",
                    "-c:a",
                    "aac",
                    "-b:a",
                    str(self.spec.audio_bitrate),
                    "-threads",
                    str(self.threads),
                    "-movflags",
                    "+faststart",
                    str(temporary),
                ]
            )
            summary = self._inspect(temporary, "a:0")
            record = {
                **summary,
                "key": key,
                "source_hash": self.source_hash,
                "toolchain_key": self.toolchain_key,
                "kind": "full-audio",
                "encode_wall_seconds": time.monotonic() - started,
                "source_identity_check": self.identity_check,
            }
            self._guard_source()
            return self.cache.publish(key, temporary, record, self.deadline)
        finally:
            temporary.unlink(missing_ok=True)

    def read_segment(self, record):
        from .fmp4 import read_track

        self._guard_source()
        track, packets = read_track(Path(record["artifact"]), "v:0", deadline=self.deadline)
        if len(packets) != record["video_frames"]:
            raise RuntimeError("cached segment frame count changed")
        self._guard_source()
        return track, packets

    def read_audio(self, record):
        from .fmp4 import read_track

        self._guard_source()
        track, packets = read_track(Path(record["artifact"]), "a:0", deadline=self.deadline)
        self._guard_source()
        return track, packets
