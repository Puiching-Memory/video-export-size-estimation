"""A fixed-layout H.264/AAC fragmented MP4 writer with exact byte accounting.

The encoder owns compressed samples; this module only changes their container.
Video segments must be closed GOPs with the same decoder configuration. Their
presentation timelines are joined without re-encoding. Signed composition
offsets and an initial edit preserve B-frame reordering. AAC is encoded once,
including its priming packet, and its original presentation edit is preserved.
This deliberately narrow container contract is distinct from continuous x264.
"""

from dataclasses import dataclass
from fractions import Fraction
import json
from pathlib import Path
import re
import struct
import subprocess
from typing import Sequence


MOVIE_TIMESCALE = 1_000_000


@dataclass(frozen=True)
class Packet:
    data: bytes
    duration: int
    dts: int
    pts: int
    keyframe: bool
    skip_samples: int = 0
    discard_padding: int = 0


@dataclass(frozen=True)
class Track:
    codec: str
    time_base: Fraction
    extradata: bytes
    width: int = 0
    height: int = 0
    sample_rate: int = 0
    channels: int = 0
    presentation_start: int = 0
    presentation_duration: int | None = None

    @property
    def timescale(self) -> int:
        if self.time_base.numerator != 1 or self.time_base.denominator >= 2**32:
            raise ValueError("MP4 contract requires a positive integer track timescale")
        return self.time_base.denominator


@dataclass(frozen=True)
class ByteAccounting:
    init_bytes: int
    video_payload_bytes: int
    audio_payload_bytes: int
    fragment_bytes: int
    video_samples: int
    audio_samples: int
    video_duration_ticks: int = 0
    audio_duration_ticks: int = 0
    audio_priming_samples: int = 0

    @property
    def total_bytes(self) -> int:
        return (
            self.init_bytes
            + self.video_payload_bytes
            + self.audio_payload_bytes
            + self.fragment_bytes
        )

    @property
    def container_bytes(self) -> int:
        return self.init_bytes + self.fragment_bytes


def _box(name: bytes, data: bytes) -> bytes:
    size = len(data) + 8
    if size >= 2**32:
        raise ValueError("individual boxes must be smaller than 4 GiB")
    return struct.pack(">I4s", size, name) + data


def _full(name: bytes, version: int, flags: int, data: bytes) -> bytes:
    return _box(name, struct.pack(">I", (version << 24) | flags) + data)


def _descriptor(tag: int, data: bytes) -> bytes:
    length = len(data)
    encoded = bytes([length & 0x7F])
    while length := length >> 7:
        encoded = bytes([(length & 0x7F) | 0x80]) + encoded
    return bytes([tag]) + encoded + data


def _movie_ticks(ticks: int, timescale: int) -> int:
    return round(Fraction(ticks * MOVIE_TIMESCALE, timescale))


def _sample_entry(track: Track) -> bytes:
    if track.codec == "h264":
        if not track.extradata or track.extradata[0] != 1:
            raise ValueError("H.264 track requires an AVCDecoderConfigurationRecord")
        if not (0 < track.width < 2**16 and 0 < track.height < 2**16):
            raise ValueError("video dimensions must fit the AVC sample entry")
        visual = (
            b"\0" * 6
            + struct.pack(">H", 1)
            + b"\0" * 16
            + struct.pack(">HHIIIH", track.width, track.height, 0x480000, 0x480000, 0, 1)
            + b"\0" * 32
            + struct.pack(">Hh", 24, -1)
        )
        return _box(b"avc1", visual + _box(b"avcC", track.extradata))
    if track.codec == "aac":
        # AAC-LC only: HE-AAC needs additional decoder/sample-rate semantics.
        if not track.extradata or track.extradata[0] >> 3 != 2:
            raise ValueError("only AAC-LC AudioSpecificConfig is supported")
        if not (0 < track.channels <= 2 and 0 < track.sample_rate < 2**16):
            raise ValueError("only mono/stereo AAC below 65536 Hz is supported")
        if track.timescale != track.sample_rate:
            raise ValueError("AAC timescale must match its sample rate")
        audio = b"\0" * 6 + struct.pack(
            ">HHHIHHHHI", 1, 0, 0, 0, track.channels, 16, 0, 0, track.sample_rate << 16
        )
        decoder = _descriptor(4, b"\x40\x15" + b"\0" * 11 + _descriptor(5, track.extradata))
        es = _descriptor(3, b"\0\x01\0" + decoder + _descriptor(6, b"\x02"))
        return _box(b"mp4a", audio + _full(b"esds", 0, 0, es))
    raise ValueError("only H.264 and AAC tracks are supported")


_MATRIX = struct.pack(">9I", 0x10000, 0, 0, 0, 0x10000, 0, 0, 0, 0x40000000)


def _track_box(
    track: Track, track_id: int, media_duration: int, presentation_duration: int, edit_start: int
) -> bytes:
    duration = _movie_ticks(presentation_duration, track.timescale)
    volume = 0x100 if track.codec == "aac" else 0
    tkhd = _full(
        b"tkhd",
        1,
        7,
        struct.pack(">QQIIQ", 0, 0, track_id, 0, duration)
        + b"\0" * 8
        + struct.pack(">hhhh", 0, 0, volume, 0)
        + _MATRIX
        + struct.pack(">II", track.width << 16, track.height << 16),
    )
    edit = _box(b"edts", _full(b"elst", 1, 0, struct.pack(">IQqhh", 1, duration, edit_start, 1, 0)))
    mdhd = _full(
        b"mdhd",
        1,
        0,
        struct.pack(">QQIQHH", 0, 0, track.timescale, media_duration, 0x55C4, 0),
    )
    handler = b"vide" if track.codec == "h264" else b"soun"
    hdlr = _full(b"hdlr", 0, 0, b"\0" * 4 + handler + b"\0" * 12 + b"\0")
    header = (
        _full(b"vmhd", 0, 1, b"\0" * 8)
        if track.codec == "h264"
        else _full(b"smhd", 0, 0, b"\0" * 4)
    )
    dinf = _box(b"dinf", _full(b"dref", 0, 0, struct.pack(">I", 1) + _full(b"url ", 0, 1, b"")))
    stbl = _box(
        b"stbl",
        _full(b"stsd", 0, 0, struct.pack(">I", 1) + _sample_entry(track))
        + _full(b"stts", 0, 0, b"\0" * 4)
        + _full(b"stsc", 0, 0, b"\0" * 4)
        + _full(b"stsz", 0, 0, b"\0" * 8)
        + _full(b"stco", 0, 0, b"\0" * 4),
    )
    return _box(
        b"trak", tkhd + edit + _box(b"mdia", mdhd + hdlr + _box(b"minf", header + dinf + stbl))
    )


def _init(
    video: Track,
    video_duration: int,
    video_edit: int,
    audio: Track | None = None,
    audio_duration: int = 0,
    audio_media_duration: int = 0,
    audio_edit: int = 0,
) -> bytes:
    duration = _movie_ticks(video_duration, video.timescale)
    if audio:
        duration = max(duration, _movie_ticks(audio_duration, audio.timescale))
    mvhd = _full(
        b"mvhd",
        1,
        0,
        struct.pack(">QQIQIh", 0, 0, MOVIE_TIMESCALE, duration, 0x10000, 0x100)
        + b"\0" * 10
        + _MATRIX
        + b"\0" * 24
        + struct.pack(">I", 3 if audio else 2),
    )
    traks = _track_box(video, 1, video_duration, video_duration, video_edit)
    trex = _full(b"trex", 0, 0, struct.pack(">5I", 1, 1, 0, 0, 0))
    if audio:
        traks += _track_box(audio, 2, audio_media_duration, audio_duration, audio_edit)
        trex += _full(b"trex", 0, 0, struct.pack(">5I", 2, 1, 0, 0, 0))
    return _box(b"ftyp", b"isom\0\0\x02\0isomiso6mp41") + _box(
        b"moov", mvhd + traks + _box(b"mvex", trex)
    )


def planned_overhead(
    video_track: Track,
    sample_counts: Sequence[int],
    *,
    audio_track: Track | None = None,
    audio_packets: Sequence[Packet] | None = None,
) -> int:
    """Exact bytes excluding compressed payload, independent of video sizes.

    Every fragment has one video traf and, when configured, one audio traf.
    Empty audio trafs are included, making overhead independent of interleaving.
    All duration fields use fixed-width v1 boxes, so placeholder durations do
    not change initialization size. AAC packet count is known after one encode.
    """
    if not sample_counts or any(n <= 0 for n in sample_counts):
        raise ValueError("every video segment must contain at least one sample")
    if (audio_track is None) != (audio_packets is None):
        raise ValueError("audio track and audio packets must be provided together")
    audio_count = len(audio_packets) if audio_packets is not None else 0
    tracks = 2 if audio_track else 1
    return (
        len(_init(video_track, 0, 0, audio_track))
        + len(sample_counts) * (32 + 64 * tracks)
        + 16 * (sum(sample_counts) + audio_count)
    )


def planned_accounting(
    video_track: Track,
    video_samples: int,
    video_duration_ticks: int,
    video_fragments: int,
    *,
    audio_track: Track | None = None,
    audio_packets: Sequence[Packet] | None = None,
) -> ByteAccounting:
    """Exact known contribution before unobserved video samples are encoded.

    `video_payload_bytes` is zero, deliberately excluded until samples exist.
    All other byte components equal the eventual writer's components, provided
    the closed-GOP plan and decoder configuration are unchanged. Fragment layout
    has no variable-size duration fields or payload-dependent padding.
    """
    if video_track.codec != "h264":
        raise ValueError("video track must be H.264")
    if not (0 < video_fragments <= video_samples) or video_duration_ticks < 0:
        raise ValueError("sample count and fragment count require a nonempty video plan")
    if (audio_track is None) != (audio_packets is None):
        raise ValueError("audio track and packets must be provided together")
    audio_edit = audio_duration = audio_media_duration = 0
    audio_count = audio_payload = 0
    if audio_track is not None:
        if audio_track.codec != "aac" or audio_track.presentation_start != 0:
            raise ValueError("audio plan requires AAC with zero presentation start")
        _validate_packets(audio_packets)
        audio_count = len(audio_packets)
        audio_payload = sum(len(packet.data) for packet in audio_packets)
        audio_edit = -audio_packets[0].dts
        audio_media_duration = sum(packet.duration for packet in audio_packets)
        audio_duration = (
            audio_track.presentation_duration
            if audio_track.presentation_duration is not None
            else audio_media_duration - audio_edit - audio_packets[-1].discard_padding
        )
        if audio_edit < 0 or not (0 < audio_duration <= audio_media_duration - audio_edit):
            raise ValueError("audio presentation edit exceeds the packet timeline")
    init = _init(
        video_track,
        video_duration_ticks,
        0,
        audio_track,
        audio_duration,
        audio_media_duration,
        audio_edit,
    )
    track_count = 2 if audio_track is not None else 1
    return ByteAccounting(
        len(init),
        0,
        audio_payload,
        video_fragments * (32 + 64 * track_count) + 16 * (video_samples + audio_count),
        video_samples,
        audio_count,
        video_duration_ticks,
        audio_duration,
        audio_edit,
    )


def _validate_packets(
    packets: Sequence[Packet], *, closed_gop: bool = False, deadline=None
) -> None:
    if not packets:
        raise ValueError("track or segment cannot be empty")
    if closed_gop and not packets[0].keyframe:
        raise ValueError("video segment must start with a keyframe")
    expected = packets[0].dts
    for index, packet in enumerate(packets):
        if deadline is not None and index % 64 == 0:
            deadline.check()
        if packet.duration <= 0 or packet.duration >= 2**32 or not packet.data:
            raise ValueError("samples require positive duration and nonempty payload")
        if packet.dts != expected:
            raise ValueError("decode timestamps must be contiguous in track ticks")
        expected += packet.duration


def _starts_with_idr(packet: Packet, track: Track) -> bool:
    """A keyframe flag alone can describe an open-GOP non-IDR I picture."""
    if len(track.extradata) < 5:
        raise ValueError("AVC decoder configuration is truncated")
    length_size = (track.extradata[4] & 3) + 1
    cursor = 0
    found_idr = False
    while cursor < len(packet.data):
        if cursor + length_size > len(packet.data):
            raise ValueError("truncated AVCC NAL length")
        size = int.from_bytes(packet.data[cursor : cursor + length_size], "big")
        cursor += length_size
        if size <= 0 or cursor + size > len(packet.data):
            raise ValueError("truncated AVCC NAL payload")
        found_idr |= packet.data[cursor] & 0x1F == 5
        cursor += size
    return found_idr


def _traf(track_id: int, packets: Sequence[Packet], dts: int, data_offset: int) -> bytes:
    entries = bytearray()
    for packet in packets:
        flags = 0x02000000 if packet.keyframe else 0x01010000
        offset = packet.pts - packet.dts
        if not (-(2**31) <= offset < 2**31):
            raise ValueError("composition offset exceeds signed 32-bit range")
        entries.extend(struct.pack(">IIIi", packet.duration, len(packet.data), flags, offset))
    return _box(
        b"traf",
        _full(b"tfhd", 0, 0x020000, struct.pack(">I", track_id))
        + _full(b"tfdt", 1, 0, struct.pack(">Q", dts))
        + _full(b"trun", 1, 0xF01, struct.pack(">Ii", len(packets), data_offset) + entries),
    )


def _fragment(
    sequence: int, video: Sequence[Packet], audio: Sequence[Packet] | None, audio_dts: int
) -> tuple[bytes, bytes]:
    tracks = 2 if audio is not None else 1
    moof_size = 24 + 64 * tracks + 16 * (len(video) + (len(audio) if audio else 0))
    data_offset = moof_size + 8
    body = _full(b"mfhd", 0, 0, struct.pack(">I", sequence))
    body += _traf(1, video, video[0].dts, data_offset)
    video_data = b"".join(p.data for p in video)
    audio_data = b""
    if audio is not None:
        body += _traf(2, audio, audio_dts, data_offset + len(video_data))
        audio_data = b"".join(p.data for p in audio)
    moof = _box(b"moof", body)
    if len(moof) != moof_size:
        raise RuntimeError("fixed fragment layout changed")
    return moof, _box(b"mdat", video_data + audio_data)


def write_fragmented_mp4(
    output: Path,
    video_track: Track,
    video_segments: Sequence[Sequence[Packet]],
    *,
    audio_track: Track | None = None,
    audio_packets: Sequence[Packet] | None = None,
    deadline=None,
) -> ByteAccounting:
    """Write a closed-GOP segmented export and return exact byte components.

    The destination must not already exist. Invalid packet timelines are
    rejected before any output is opened. The caller validates decoder config
    equality across video segments; each segment must use video_track's ticks.

    Input AAC must start at presentation zero. A real offset between the audio
    and video tracks requires a leading empty edit, which this contract rejects.
    FFmpeg reports fragmented AAC duration including its one-time priming
    interval; audio_duration_ticks records the intended presentation duration.
    The original decoded AAC PCM, including decoder tail padding, is preserved.
    """
    if video_track.codec != "h264":
        raise ValueError("video track must be H.264")
    if (audio_track is None) != (audio_packets is None):
        raise ValueError("audio track and packets must be provided together")
    if not video_segments:
        raise ValueError("at least one video segment is required")
    normalized = []
    cursor = 0
    first = video_segments[0]
    _validate_packets(first, closed_gop=True, deadline=deadline)
    edit = min(p.pts for p in first) - first[0].dts
    if edit < 0:
        raise ValueError("first video segment must not require a negative edit")
    for segment in video_segments:
        _validate_packets(segment, closed_gop=True, deadline=deadline)
        if not _starts_with_idr(segment[0], video_track):
            raise ValueError("every video segment must start with an IDR picture")
        origin = min(p.pts for p in segment)
        normalized.append(
            [
                Packet(
                    p.data,
                    p.duration,
                    cursor + p.dts - segment[0].dts,
                    cursor + p.pts - origin + edit,
                    p.keyframe,
                )
                for p in segment
            ]
        )
        presentation = sorted((p.pts - origin, p.duration) for p in segment)
        end = 0
        for pts, duration in presentation:
            if pts != end:
                raise ValueError("presentation timestamps must cover the segment without gaps")
            end += duration
        if end != sum(p.duration for p in segment):
            raise ValueError("presentation and decode durations disagree")
        cursor += end
    audio_normalized = []
    audio_edit = audio_duration = audio_media_duration = 0
    if audio_track is not None:
        if audio_track.codec != "aac":
            raise ValueError("audio track must be AAC")
        if audio_track.presentation_start != 0:
            raise ValueError(
                "nonzero AAC presentation start requires unsupported movie offset; "
                "priming is represented by negative initial packet timestamps"
            )
        _validate_packets(audio_packets, deadline=deadline)
        first_dts = audio_packets[0].dts
        audio_edit = audio_track.presentation_start - first_dts
        if audio_edit < 0:
            raise ValueError("AAC timeline cannot require a negative edit")
        if any(p.discard_padding for p in audio_packets[:-1]):
            raise ValueError("AAC padding is only supported on the final packet")
        if any(p.skip_samples for p in audio_packets[1:]):
            raise ValueError("AAC priming is only supported on the first packet")
        if audio_packets[0].skip_samples > audio_edit:
            raise ValueError("AAC edit does not remove declared priming samples")
        audio_normalized = [
            Packet(p.data, p.duration, p.dts - first_dts, p.pts - first_dts, True)
            for p in audio_packets
        ]
        audio_media_duration = sum(p.duration for p in audio_packets)
        audio_duration = (
            audio_track.presentation_duration
            if audio_track.presentation_duration is not None
            else audio_media_duration - audio_edit - audio_packets[-1].discard_padding
        )
        if not (0 < audio_duration <= audio_media_duration - audio_edit):
            raise ValueError("AAC presentation duration exceeds its sample timeline")
    init = _init(
        video_track, cursor, edit, audio_track, audio_duration, audio_media_duration, audio_edit
    )
    accounting = ByteAccounting(
        len(init),
        sum(len(p.data) for s in video_segments for p in s),
        sum(len(p.data) for p in audio_normalized),
        planned_overhead(
            video_track,
            [len(s) for s in video_segments],
            audio_track=audio_track,
            audio_packets=audio_packets,
        )
        - len(init),
        sum(len(s) for s in video_segments),
        len(audio_normalized),
        cursor,
        audio_duration,
        audio_edit,
    )
    audio_index = 0
    audio_cursor = 0
    output = Path(output)
    try:
        with output.open("xb") as target:
            target.write(init)
            for i, segment in enumerate(normalized):
                if deadline is not None:
                    deadline.check()
                end = Fraction(segment[-1].dts + segment[-1].duration, video_track.timescale)
                start_index = audio_index
                while audio_index < len(audio_normalized) and (
                    i == len(normalized) - 1
                    or Fraction(audio_normalized[audio_index].dts, audio_track.timescale) < end
                ):
                    audio_index += 1
                audio_fragment = audio_normalized[start_index:audio_index] if audio_track else None
                if audio_fragment:
                    audio_cursor = audio_fragment[0].dts
                moof, mdat = _fragment(i + 1, segment, audio_fragment, audio_cursor)
                target.write(moof)
                target.write(mdat)
                if deadline is not None:
                    deadline.check()
                if audio_fragment:
                    audio_cursor = audio_fragment[-1].dts + audio_fragment[-1].duration
            if target.tell() != accounting.total_bytes:
                raise RuntimeError("container byte accounting differs from written bytes")
    except FileExistsError:
        raise
    except BaseException:
        output.unlink(missing_ok=True)
        raise
    return accounting


def _parse_hex_dump(value: str) -> bytes:
    result = bytearray()
    for line in value.splitlines():
        if ":" not in line:
            continue
        data = line.split(":", 1)[1].strip().split("  ", 1)[0]
        hex_string = "".join(data.split())
        if not re.fullmatch(r"[0-9a-fA-F]*", hex_string) or len(hex_string) % 2:
            raise ValueError("invalid ffprobe extradata hex dump")
        result.extend(bytes.fromhex(hex_string))
    return bytes(result)


def read_track(
    path: Path, selector: str = "v:0", *, deadline=None, run=None
) -> tuple[Track, list[Packet]]:
    """Read MP4 sample byte ranges without decoding or copying the whole file.

    `run` may be Deadline.run; the default is subprocess.run(check=True).
    Annex-B elementary streams are excluded: byte offsets must point to AVCC
    or raw AAC payload in an ISO BMFF input file.
    """
    command = [
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        selector,
        "-show_streams",
        "-show_packets",
        "-show_data",
        "-show_entries",
        "stream=codec_name,time_base,extradata,width,height,"
        "sample_rate,channels,start_pts,duration_ts:packet=pts,dts,duration,"
        "pos,size,flags,side_data_list",
        "-of",
        "json",
        str(path),
    ]
    if deadline is not None:
        deadline.check()
        run = deadline.run
    result = run(command) if run else subprocess.run(command, check=True, capture_output=True)
    info = json.loads(result.stdout)
    if len(info.get("streams", [])) != 1:
        raise ValueError("exactly one selected MP4 track is required")
    stream = info["streams"][0]
    track = Track(
        stream["codec_name"],
        Fraction(stream["time_base"]),
        _parse_hex_dump(stream.get("extradata", "")),
        int(stream.get("width", 0)),
        int(stream.get("height", 0)),
        int(stream.get("sample_rate", 0)),
        int(stream.get("channels", 0)),
        int(stream.get("start_pts", 0)),
        int(stream["duration_ts"]) if "duration_ts" in stream else None,
    )
    _sample_entry(track)
    packets = []
    with Path(path).open("rb") as source:
        for index, packet in enumerate(info.get("packets", [])):
            if deadline is not None and index % 64 == 0:
                deadline.check()
            if not all(name in packet for name in ("pts", "dts", "duration", "pos", "size")):
                raise ValueError("MP4 packet requires complete timestamps and byte offsets")
            offset, size = int(packet["pos"]), int(packet["size"])
            if offset < 0 or size <= 0:
                raise ValueError("packet byte range must be positive")
            source.seek(offset)
            data = source.read(size)
            if len(data) != size:
                raise ValueError("truncated MP4 packet byte range")
            side = packet.get("side_data_list", [])
            skip = sum(int(x.get("skip_samples", 0)) for x in side)
            padding = sum(int(x.get("discard_padding", 0)) for x in side)
            packets.append(
                Packet(
                    data,
                    int(packet["duration"]),
                    int(packet["dts"]),
                    int(packet["pts"]),
                    "K" in packet.get("flags", ""),
                    skip,
                    padding,
                )
            )
    if deadline is not None:
        deadline.check()
    return track, packets
