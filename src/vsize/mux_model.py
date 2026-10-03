"""Read video-only MP4 tables and model their container overhead.

The census is exact for the deliberately narrow accepted layout.  Extrapolated
composition-offset and keyframe table counts are empirical point estimates;
they are neither byte bounds nor confidence intervals.
"""

import math
import struct
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path


MODEL_VERSION = "single-video-mp4-tables-v1"


class _Unsupported(ValueError):
    pass


@dataclass(frozen=True)
class _Box:
    kind: bytes
    start: int
    size: int

    @property
    def payload(self):
        return self.start + 8

    @property
    def end(self):
        return self.start + self.size


class _Reader:
    def __init__(self, stream, deadline):
        self.stream, self.deadline = stream, deadline

    def check(self):
        if self.deadline is not None:
            self.deadline.check()

    def read(self, offset, size):
        self.check()
        self.stream.seek(offset)
        data = self.stream.read(size)
        if len(data) != size:
            raise _Unsupported("truncated MP4 box")
        return data

    def children(self, start, end):
        boxes = []
        offset = start
        while offset < end:
            self.check()
            if end - offset < 8:
                raise _Unsupported("incomplete MP4 box header")
            size, kind = struct.unpack(">I4s", self.read(offset, 8))
            # A narrow standard 32-bit box layout is enough for the encoder's
            # regular MP4s.  Extended/implicit sizes require another model.
            if size < 8 or size > end - offset:
                raise _Unsupported("unsupported MP4 box size")
            boxes.append(_Box(kind, offset, size))
            if len(boxes) > 4096:
                raise _Unsupported("unreasonable structural box count")
            offset += size
        return boxes

    def named(self, parent, allowed, required):
        boxes = self.children(parent.payload, parent.end)
        result = {}
        for box in boxes:
            if box.kind not in allowed or box.kind in result:
                raise _Unsupported("unknown or repeated MP4 structural box")
            result[box.kind] = box
        if not set(required) <= result.keys():
            raise _Unsupported("missing MP4 structural box")
        return result

    def full(self, box, versions=(0,)):
        if box.size < 12:
            raise _Unsupported("truncated full box")
        flags = self.read(box.payload, 4)
        if flags[0] not in versions or flags[1:] != b"\0\0\0":
            raise _Unsupported("unsupported full-box version or flags")
        return flags[0]

    def table(self, box, width, versions=(0,)):
        version = self.full(box, versions)
        if box.size < 16:
            raise _Unsupported("truncated sample table")
        count = struct.unpack(">I", self.read(box.payload + 4, 4))[0]
        if box.size != 16 + count * width:
            raise _Unsupported("sample-table size/count mismatch")
        return version, count

    def values(self, offset, count, width):
        # Work in bounded pieces, even when the file has millions of samples.
        format_string = ">" + "I" * (width // 4)
        remaining = count
        while remaining:
            batch = min(remaining, max(1, 65536 // width))
            data = self.read(offset, batch * width)
            yield from struct.iter_unpack(format_string, data)
            remaining -= batch
            offset += batch * width


def _census(reader, file_bytes):
    top = reader.children(0, file_bytes)
    if any(box.kind not in {b"ftyp", b"free", b"skip", b"wide", b"moov", b"mdat"} for box in top):
        raise _Unsupported("unsupported top-level box")
    moovs = [box for box in top if box.kind == b"moov"]
    mdats = [box for box in top if box.kind == b"mdat"]
    if len(moovs) != 1 or len(mdats) != 1 or sum(box.kind == b"ftyp" for box in top) != 1:
        raise _Unsupported("one moov and one mdat are required")
    moov = reader.named(moovs[0], {b"mvhd", b"trak", b"udta", b"free"}, {b"mvhd", b"trak"})
    # A repeated trak is rejected by named(), excluding audio/subtitle tracks.
    trak = reader.named(moov[b"trak"], {b"tkhd", b"edts", b"mdia"}, {b"tkhd", b"mdia"})
    mdia = reader.named(trak[b"mdia"], {b"mdhd", b"hdlr", b"minf"}, {b"mdhd", b"hdlr", b"minf"})
    handler = mdia[b"hdlr"]
    reader.full(handler)
    if handler.size < 24 or reader.read(handler.payload + 8, 4) != b"vide":
        raise _Unsupported("single video track required")
    minf = reader.named(mdia[b"minf"], {b"vmhd", b"dinf", b"stbl"}, {b"vmhd", b"dinf", b"stbl"})
    dinf = reader.named(minf[b"dinf"], {b"dref"}, {b"dref"})
    dref = dinf[b"dref"]
    reader.full(dref)
    if dref.size < 16 or reader.read(dref.payload + 4, 4) != b"\0\0\0\1":
        raise _Unsupported("one local data reference required")
    urls = reader.children(dref.payload + 8, dref.end)
    if len(urls) != 1 or urls[0].kind != b"url " or urls[0].size != 12:
        raise _Unsupported("self-contained data reference required")
    if reader.read(urls[0].payload, 4) != b"\0\0\0\1":
        raise _Unsupported("external data reference")
    tables = reader.named(
        minf[b"stbl"],
        {b"stsd", b"stts", b"ctts", b"stss", b"stsc", b"stsz", b"stco"},
        {b"stsd", b"stts", b"stsc", b"stsz", b"stco"},
    )
    stsd = tables[b"stsd"]
    reader.full(stsd)
    if stsd.size < 16 or reader.read(stsd.payload + 4, 4) != b"\0\0\0\1":
        raise _Unsupported("one AVC sample entry required")
    entries = reader.children(stsd.payload + 8, stsd.end)
    if len(entries) != 1 or entries[0].kind != b"avc1" or entries[0].size < 86:
        raise _Unsupported("AVC sample entry required")
    avc = entries[0]
    if reader.read(avc.payload + 6, 2) != b"\0\1":
        raise _Unsupported("unsupported AVC data reference")
    avc_children = reader.children(avc.start + 86, avc.end)
    if sum(box.kind == b"avcC" for box in avc_children) != 1 or any(
        box.kind not in {b"avcC", b"pasp", b"btrt", b"colr", b"clap", b"fiel"}
        for box in avc_children
    ):
        raise _Unsupported("unsupported AVC configuration boxes")

    stsz = tables[b"stsz"]
    reader.full(stsz)
    if stsz.size < 20:
        raise _Unsupported("truncated sample-size table")
    sample_size, frames = struct.unpack(">II", reader.read(stsz.payload + 4, 8))
    stsz_dynamic = 0 if sample_size else frames * 4
    if frames < 1 or stsz.size != 20 + stsz_dynamic:
        raise _Unsupported("sample-size table count mismatch")
    if sample_size:
        payload_bytes = sample_size * frames
    else:
        payload_bytes = 0
        for (value,) in reader.values(stsz.payload + 12, frames, 4):
            if value < 1:
                raise _Unsupported("empty video sample")
            payload_bytes += value
    if payload_bytes != mdats[0].size - 8:
        raise _Unsupported("mdat contains bytes beyond the one video track")

    versions = {"stsz": 0}
    table_counts = {}
    table_bytes = {"stsz": stsz.size}
    for kind, width in ((b"stts", 8), (b"stsc", 12), (b"stco", 4)):
        version, count = reader.table(tables[kind], width)
        versions[kind.decode()] = version
        table_counts[kind.decode()] = count
        table_bytes[kind.decode()] = tables[kind].size
        if count < 1 or count > frames or (kind == b"stts" and count != 1):
            raise _Unsupported("only CFR timing and nonempty local chunks are modeled")
    timing_count, sample_ticks = next(reader.values(tables[b"stts"].payload + 8, 1, 8))
    if timing_count != frames or sample_ticks < 1:
        raise _Unsupported("sample/chunk/timing tables are inconsistent")
    chunk_count = table_counts["stco"]
    if table_counts["stsc"] > chunk_count:
        raise _Unsupported("too many sample-to-chunk runs")
    chunk_runs = iter(reader.values(tables[b"stsc"].payload + 8, table_counts["stsc"], 12))
    current = next(chunk_runs)
    following = next(chunk_runs, None)
    if current[0] != 1:
        raise _Unsupported("sample-to-chunk table must begin at chunk one")
    sample_sizes = iter(reader.values(stsz.payload + 12, frames, 4)) if not sample_size else None
    expected_offset = mdats[0].payload
    counted_frames = 0
    for chunk, (offset,) in enumerate(
        reader.values(tables[b"stco"].payload + 8, chunk_count, 4), 1
    ):
        if following is not None:
            if not current[0] < following[0] <= chunk_count:
                raise _Unsupported("invalid sample-to-chunk run order")
            if following[0] == chunk:
                current, following = following, next(chunk_runs, None)
        _, samples, description = current
        if (
            description != 1
            or samples < 1
            or counted_frames + samples > frames
            or offset != expected_offset
        ):
            raise _Unsupported("sample/chunk byte ranges are inconsistent")
        if sample_size:
            expected_offset += sample_size * samples
        else:
            expected_offset += sum(next(sample_sizes)[0] for _ in range(samples))
        counted_frames += samples
    if counted_frames != frames or expected_offset != mdats[0].end or following is not None:
        raise _Unsupported("chunk samples do not exactly cover the video payload")

    ctts_count = 0
    ctts_dynamic = 0
    if b"ctts" in tables:
        versions["ctts"], ctts_count = reader.table(tables[b"ctts"], 8, (0, 1))
        if ctts_count < 1:
            raise _Unsupported("empty composition-offset table")
        total = 0
        for count, _offset in reader.values(tables[b"ctts"].payload + 8, ctts_count, 8):
            if count < 1:
                raise _Unsupported("empty composition-offset run")
            total += count
        if total != frames:
            raise _Unsupported("composition-offset sample count mismatch")
        ctts_dynamic = ctts_count * 8
        table_bytes["ctts"] = tables[b"ctts"].size
    stss_count = frames
    stss_dynamic = 0
    if b"stss" in tables:
        versions["stss"], stss_count = reader.table(tables[b"stss"], 4)
        previous = 0
        for (sample,) in reader.values(tables[b"stss"].payload + 8, stss_count, 4):
            if not previous < sample <= frames:
                raise _Unsupported("invalid sync-sample number")
            previous = sample
        if stss_count < 1 or reader.read(tables[b"stss"].payload + 8, 4) != b"\0\0\0\1":
            raise _Unsupported("video must begin with a sync sample")
        stss_dynamic = stss_count * 4
        table_bytes["stss"] = tables[b"stss"].size
    dynamic_bytes = stsz_dynamic + ctts_dynamic + stss_dynamic
    container_bytes = file_bytes - payload_bytes
    constant_bytes = container_bytes - dynamic_bytes
    if constant_bytes < 0:
        raise _Unsupported("negative container census")
    return {
        "model_version": MODEL_VERSION,
        "kind": "exact_single_video_census",
        "file_bytes": file_bytes,
        "payload_bytes": payload_bytes,
        "container_bytes": container_bytes,
        "video_frames": frames,
        "sample_ticks": sample_ticks,
        "constant_bytes": constant_bytes,
        "dynamic_bytes": dynamic_bytes,
        "dynamic_table_bytes": {"stsz": stsz_dynamic, "ctts": ctts_dynamic, "stss": stss_dynamic},
        "table_bytes": table_bytes,
        "table_versions": versions,
        "table_counts": {**table_counts, "stsz": frames, "ctts": ctts_count, "stss": stss_count},
        "stsz_constant_sample_size": sample_size,
        "ctts_present": b"ctts" in tables,
        "stss_present": b"stss" in tables,
        "keyframe_count": stss_count,
        "ctts_run_count": ctts_count,
        "video_only": True,
        "chunks": chunk_count,
        "estimation_kind": "empirical_point; no bound or confidence interval",
    }


def inspect_video_container(path, deadline=None):
    """Return an exact supported video-only MP4 table census or None.

    Only headers/sample tables are read.  Encoded mdat payload is skipped.  AAC,
    fragmented MP4, compact/64-bit tables, and unfamiliar sample
    structures return None so a caller can use its general fallback.  A deadline
    exhaustion propagates rather than being disguised as an unsupported format.
    """
    try:
        path = Path(path)
        before = path.stat()
        with path.open("rb") as stream:
            result = _census(_Reader(stream, deadline), before.st_size)
        after = path.stat()
        if (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        ) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            return None
        return result
    except (OSError, _Unsupported, struct.error):
        return None


def estimate_video_container(info, video_frames, keyframe_count=None):
    """Extrapolate a point estimate from a supported probe's measured tables.

    Composition runs scale empirically with frame count.  The default sync count
    combines observed sync density with the encoder's usual 250-frame maximum
    GOP.  A caller with a better keyframe model can pass its predicted count.
    """
    if not isinstance(info, dict) or info.get("model_version") != MODEL_VERSION:
        raise ValueError("a supported video-container census is required")
    if type(video_frames) is not int or video_frames < 1:
        raise ValueError("video_frames must be a positive integer")
    observed_frames = info["video_frames"]
    if type(observed_frames) is not int or observed_frames < 1:
        raise ValueError("invalid observed frame count")
    if keyframe_count is None:
        keyframe_count = max(
            math.ceil(video_frames / 250),
            round(Fraction(info["keyframe_count"] * video_frames, observed_frames)),
        )
        keyframe_count = min(video_frames, max(1, keyframe_count))
    if type(keyframe_count) is not int or not 1 <= keyframe_count <= video_frames:
        raise ValueError("keyframe_count must be an integer between one and video_frames")
    size_bytes = 0 if info["stsz_constant_sample_size"] else 4 * video_frames
    composition_bytes = (
        8 * max(1, round(Fraction(info["ctts_run_count"] * video_frames, observed_frames)))
        if info["ctts_present"]
        else 0
    )
    sync_bytes = 4 * keyframe_count if info["stss_present"] else 0
    return int(info["constant_bytes"] + size_bytes + composition_bytes + sync_bytes)
