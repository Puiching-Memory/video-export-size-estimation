"""Development-only experiment measuring short-encode startup/context bias.

This deliberately expensive census isolates context error from block-selection
error. The complete reference is used only for scoring, never for correcting a
candidate. Re-running the script reuses saved encodes and their inspections.
"""

import argparse
import hashlib
import json
import math
import statistics
import subprocess
import tempfile
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "results/development-2026-10-02/manifest.json"
CONTRACT = {
    "codec": "libx264",
    "crf": 23,
    "preset": "medium",
    "pixel_format": "yuv420p",
    "threads": 1,
    "filter_threads": 1,
    "audio": "dropped",
    "keyint": "x264 default 250 (verified in encoder log)",
}


def sha256(path):
    h = hashlib.sha256()
    with path.open("rb") as source:
        while data := source.read(1024 * 1024):
            h.update(data)
    return h.hexdigest()


def run_json(command):
    return json.loads(subprocess.check_output(command, text=True))


def inspect(path):
    """Join packets to presentation-order frames by file position, not order."""
    info = run_json(
        ["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)]
    )
    packets = run_json(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_packets",
            "-show_entries",
            "packet=stream_index,pos,size,pts_time,dts_time,duration_time,flags",
            "-of",
            "json",
            str(path),
        ]
    )["packets"]
    frames = run_json(
        [
            "ffprobe",
            "-v",
            "error",
            "-threads",
            "1",
            "-select_streams",
            "v:0",
            "-show_frames",
            "-show_entries",
            "frame=pts_time,pkt_pos,pkt_size,pict_type,key_frame",
            "-of",
            "json",
            str(path),
        ]
    )["frames"]
    by_position = {f["pkt_pos"]: f for f in frames if "pkt_pos" in f}
    video = next(s for s in info["streams"] if s["codec_type"] == "video")
    for p in packets:
        frame = by_position.get(p.get("pos"))
        if p["stream_index"] == video["index"]:
            if frame is None:
                raise ValueError(f"video packet missing frame: {p}")
            p["pict_type"] = frame["pict_type"]
    if len([p for p in packets if p["stream_index"] == video["index"]]) != len(frames):
        raise ValueError("video packet/frame count mismatch")
    payload_by_stream = {
        str(s["index"]): sum(int(p["size"]) for p in packets if p["stream_index"] == s["index"])
        for s in info["streams"]
    }
    video_packets = [p for p in packets if p["stream_index"] == video["index"]]
    return {
        "info": info,
        "packets": packets,
        "video_packets": video_packets,
        "file_bytes": path.stat().st_size,
        "payload_by_stream": payload_by_stream,
        "payload_bytes": sum(payload_by_stream.values()),
        "container_bytes": path.stat().st_size - sum(payload_by_stream.values()),
        "duration": float(info["format"]["duration"]),
        "video_frames": len(frames),
        "by_picture_type": picture_types(video_packets),
        "sha256": sha256(path),
    }


def picture_types(packets):
    return {
        kind: {
            "frames": sum(p["pict_type"] == kind for p in packets),
            "bytes": sum(int(p["size"]) for p in packets if p["pict_type"] == kind),
        }
        for kind in ["I", "P", "B"]
    }


def first_packet_nals(path, record):
    """Inspect AVCC NAL lengths without decoding or altering the packet."""
    packet = min(record["video_packets"], key=lambda p: float(p["pts_time"]))
    stream = next(s for s in record["info"]["streams"] if s["codec_type"] == "video")
    length_bytes = int(stream["nal_length_size"])
    with path.open("rb") as source:
        source.seek(int(packet["pos"]))
        data = source.read(int(packet["size"]))
    offset = 0
    nals = []
    while offset < len(data):
        if len(data) - offset < length_bytes:
            raise ValueError("truncated AVCC NAL length")
        size = int.from_bytes(data[offset : offset + length_bytes], "big")
        offset += length_bytes
        if size <= 0 or size > len(data) - offset:
            raise ValueError("invalid AVCC NAL size")
        nal = data[offset : offset + size]
        offset += size
        kind = nal[0] & 31
        # This experiment's x264 initialization SEI contains payload type 5:
        # user_data_unregistered. Do not subtract other kinds of SEI metadata.
        initialization_sei = kind == 6 and nal[1] == 5
        nals.append(
            {
                "nal_type": kind,
                "bytes": size + length_bytes,
                "initialization_sei": initialization_sei,
            }
        )
    return nals


def add_nal_measurement(path, record):
    record["first_packet_nals"] = first_packet_nals(path, record)
    record["startup_sei_bytes"] = sum(
        n["bytes"] for n in record["first_packet_nals"] if n["initialization_sei"]
    )
    return record


def encode(source, destination, start=0.0, duration=None):
    saved = destination.with_suffix(".json")
    destination.parent.mkdir(parents=True, exist_ok=True)
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
        "1",
        "-filter_threads",
        "1",
    ]
    if duration is not None:
        command += ["-ss", str(start)]
    command += ["-i", str(source)]
    if duration is not None:
        command += ["-t", str(duration)]
    command += [
        "-map",
        "0:v:0",
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
        "-movflags",
        "+faststart",
        str(destination),
    ]
    if destination.exists() and saved.exists():
        record = json.loads(saved.read_text())
        if record["command"] != command:
            raise ValueError(f"saved encode has different parameters: {destination}")
        if sha256(destination) != record["sha256"]:
            raise ValueError(f"saved encode changed: {destination}")
        if "startup_sei_bytes" not in record:
            add_nal_measurement(destination, record)
            saved.write_text(json.dumps(record, indent=2))
        return record
    started = time.monotonic()
    result = subprocess.run(command, check=True, capture_output=True)
    encoding_seconds = time.monotonic() - started
    destination.with_suffix(".log").write_bytes(result.stderr)
    record = inspect(destination)
    add_nal_measurement(destination, record)
    record.update({"command": command, "encoding_seconds": encoding_seconds})
    saved.write_text(json.dumps(record, indent=2))
    return record


def window_packets(record, start, end):
    return [p for p in record["video_packets"] if start - 1e-7 <= float(p["pts_time"]) < end - 1e-7]


def startup_corrected_bytes(record, measured_duration, fps, keyint=250):
    """Replace the forced first I frame by a local non-I frame estimate.

    Retain all later scene-cut I frames, then add the guessed periodic I excess.
    This hypothesis intentionally does not borrow I-frame statistics from the
    complete reference. It is scored as a candidate, not asserted to be correct.
    """
    packets = sorted(record["video_packets"], key=lambda p: float(p["pts_time"]))
    if not packets or packets[0]["pict_type"] != "I":
        raise ValueError("first presentation frame must be I")
    non_i = [int(p["size"]) for p in packets if p["pict_type"] != "I"]
    if not non_i:
        return None
    typical = statistics.mean(non_i)
    excess = int(packets[0]["size"]) - typical
    return record["payload_bytes"] - excess + excess * measured_duration * fps / keyint


def startup_i_excess(record):
    packets = sorted(record["video_packets"], key=lambda p: float(p["pts_time"]))
    non_i = [int(p["size"]) for p in packets if p["pict_type"] != "I"]
    return int(packets[0]["size"]) - statistics.mean(non_i) if non_i else None


def summarize_case(case, records, reference, source_info):
    duration = float(source_info["format"]["duration"])
    frames = int(source_info["streams"][0]["nb_frames"])
    fps = frames / duration
    reference_payload = reference["payload_bytes"]
    reference_file = reference["file_bytes"]
    estimates = {}
    for method in ["old_file", "clip_payload", "startup_corrected", "context_payload"]:
        values = [r["candidates"][method] for r in records]
        value = sum(values) if all(v is not None for v in values) else None
        target = reference_file if method == "old_file" else reference_payload
        estimates[method] = {
            "estimate": value,
            "target": target,
            "relative_error": value / target - 1 if value is not None else None,
        }
    excesses = [r["startup_i_excess"] for r in records if r["startup_i_excess"] is not None]
    periodic_is = math.floor((frames - 1) / 250)
    guessed_extra_payload = statistics.mean(excesses) * periodic_is if excesses else 0
    context_gop_corrected = estimates["context_payload"]["estimate"] + guessed_extra_payload
    estimates["context_gop_corrected"] = {
        "estimate": context_gop_corrected,
        "target": reference_payload,
        "relative_error": context_gop_corrected / reference_payload - 1,
        "guessed_periodic_i_count": periodic_is,
        "guessed_periodic_i_excess": guessed_extra_payload,
    }
    genuine_excesses = [
        r["startup_i_excess"] - r["startup_sei_bytes"]
        for r in records
        if r["startup_i_excess"] is not None
    ]
    guessed_no_sei = statistics.mean(genuine_excesses) * periodic_is if genuine_excesses else 0
    corrected_no_sei = estimates["context_payload"]["estimate"] + guessed_no_sei
    estimates["context_gop_no_sei"] = {
        "estimate": corrected_no_sei,
        "target": reference_payload,
        "relative_error": corrected_no_sei / reference_payload - 1,
        "guessed_periodic_i_count": periodic_is,
        "guessed_periodic_i_excess": guessed_no_sei,
    }
    by_i = reference["by_picture_type"]
    return {
        "id": case["id"],
        "group_id": case["group_id"],
        "duration": duration,
        "frames": frames,
        "fps": fps,
        "source_sha256": case["sha256"],
        "reference_file_bytes": reference_file,
        "reference_payload_bytes": reference_payload,
        "reference_container_bytes": reference["container_bytes"],
        "reference_picture_types": by_i,
        "reference_encode_seconds": reference["encoding_seconds"],
        "census_estimates": estimates,
        "windows": records,
        "limitations": [
            "A census of all blocks isolates context bias and spends more than a full encode.",
            "Scene selection and uncertainty coverage are not evaluated.",
            "Startup correction uses guessed keyint 250 and local first-I excess.",
            "Warmup can suppress normal periodic I frames; padding is not a guarantee of equivalence.",
            "Periodic-I correction ignores GOP resets at scene cuts and uses forced-start I costs.",
        ],
    }


def study(args):
    dataset = json.loads(MANIFEST.read_text())
    if dataset["split"] != "development":
        raise ValueError("only the frozen development manifest is allowed")
    selected = set(args.cases.split(","))
    cases = [c for c in dataset["cases"] if c["id"] in selected]
    if len(cases) != len(selected):
        raise ValueError("requested cases are not in the development manifest")
    args.work.mkdir(parents=True, exist_ok=True)
    args.output.mkdir(parents=True, exist_ok=True)
    reports = []
    for case in cases:
        source = Path(case["source"])
        if sha256(source) != case["sha256"]:
            raise ValueError(f"source changed: {source}")
        source_info = run_json(
            ["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(source)]
        )
        duration = float(source_info["format"]["duration"])
        fps = int(source_info["streams"][0]["nb_frames"]) / duration
        records = []
        # References are encoded only after candidate measurements are committed.
        for index in range(math.ceil(duration / args.block - 1e-7)):
            start = index * args.block
            block_duration = min(args.block, duration - start)
            end = start + block_duration
            work = args.work / case["id"]
            clip = encode(source, work / f"clip-{index:03}.mp4", start, block_duration)
            padded_start = max(0.0, start - args.before)
            padded_end = min(duration, end + args.after)
            padded = encode(
                source,
                work / f"context-{args.before:g}-{args.after:g}-{index:03}.mp4",
                padded_start,
                padded_end - padded_start,
            )
            central = window_packets(padded, start - padded_start, end - padded_start)
            central_bytes = sum(int(p["size"]) for p in central)
            record = {
                "index": index,
                "start": start,
                "duration": block_duration,
                "clip_encode_seconds": clip["encoding_seconds"],
                "context_encode_seconds": padded["encoding_seconds"],
                "clip_file_bytes": clip["file_bytes"],
                "clip_payload_bytes": clip["payload_bytes"],
                "clip_container_bytes": clip["container_bytes"],
                "clip_frames": clip["video_frames"],
                "clip_picture_types": clip["by_picture_type"],
                "startup_i_excess": startup_i_excess(clip),
                "startup_sei_bytes": clip["startup_sei_bytes"],
                "context_start": padded_start,
                "context_duration": padded_end - padded_start,
                "central_frames": len(central),
                "central_picture_types": picture_types(central),
                "candidates": {
                    "old_file": clip["file_bytes"] * block_duration / clip["duration"],
                    "clip_payload": clip["payload_bytes"] * block_duration / clip["duration"],
                    "startup_corrected": startup_corrected_bytes(clip, block_duration, fps),
                    "context_payload": central_bytes,
                },
            }
            records.append(record)
            print(
                json.dumps({"case": case["id"], "block": index, "central_frames": len(central)}),
                flush=True,
            )
        work = args.work / case["id"]
        (work / "candidate-measurements.json").write_text(json.dumps(records, indent=2))
        reference = encode(source, work / "reference.mp4")
        for record in records:
            truth_packets = window_packets(
                reference, record["start"], record["start"] + record["duration"]
            )
            record["reference_window_payload"] = sum(int(p["size"]) for p in truth_packets)
            record["reference_window_picture_types"] = picture_types(truth_packets)
        report = summarize_case(case, records, reference, source_info)
        reports.append(report)
        (args.output / f"{case['id']}.json").write_text(json.dumps(report, indent=2))
        print(json.dumps({"case": case["id"], "estimates": report["census_estimates"]}), flush=True)
    summary = {
        "schema_version": 1,
        "split": "development",
        "contract": CONTRACT,
        "ffmpeg": subprocess.check_output(["ffmpeg", "-version"], text=True).splitlines()[0],
        "manifest_sha256": sha256(MANIFEST),
        "script_sha256": sha256(Path(__file__)),
        "padding": {"before": args.before, "after": args.after},
        "block_seconds": args.block,
        "reference_uses": "scoring only; encoded after candidate measurements",
        "cases": [{k: v for k, v in r.items() if k != "windows"} for r in reports],
    }
    (args.output / "summary.json").write_text(json.dumps(summary, indent=2))


def selftest():
    # B-frame decode order differs from presentation order: count by PTS.
    def p(pts, size, kind):
        return {"pts_time": str(pts), "size": str(size), "pict_type": kind}

    packets = [p(0, 100, "I"), p(0.3, 20, "P"), p(0.1, 10, "B"), p(0.2, 12, "B")]
    record = {"video_packets": packets, "payload_bytes": 142}
    assert sum(int(p["size"]) for p in window_packets(record, 0.1, 0.3)) == 22
    assert picture_types(packets)["B"] == {"frames": 2, "bytes": 22}
    corrected = startup_corrected_bytes(record, 0.4, 10, 4)
    assert corrected == 142, "one full keyint needs no artificial startup correction"
    assert (
        startup_corrected_bytes({"video_packets": [p(0, 100, "I")], "payload_bytes": 100}, 0.1, 10)
        is None
    )
    directory = ROOT / "artifacts/context-study"
    directory.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=directory) as name:
        path = Path(name) / "avcc.bin"
        raw = b"\x00\x00\x00\x03\x06\x05\x80\x00\x00\x00\x02\x05\x80"
        path.write_bytes(raw)
        record = {
            "video_packets": [{"pts_time": "0", "pos": "0", "size": str(len(raw))}],
            "info": {"streams": [{"codec_type": "video", "nal_length_size": "4"}]},
        }
        add_nal_measurement(path, record)
        assert record["startup_sei_bytes"] == 7
        assert sum(n["bytes"] for n in record["first_packet_nals"]) == len(raw)
        path.write_bytes(b"\x00\x00\x00\xff\x05")
        record["video_packets"][0]["size"] = "5"
        try:
            first_packet_nals(path, record)
        except ValueError:
            pass
        else:
            raise AssertionError("truncated AVCC packet must fail")
    print("7 context-measurement assertions passed")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", default="pedestrians,grain,screen,brief-burst")
    parser.add_argument("--block", type=float, default=2.0)
    parser.add_argument("--before", type=float, default=2.0)
    parser.add_argument("--after", type=float, default=1.0)
    parser.add_argument("--work", type=Path, default=ROOT / "artifacts/context-study")
    parser.add_argument("--output", type=Path, default=ROOT / "results/context-study-2026-10-03")
    parser.add_argument("--selftest", action="store_true")
    args = parser.parse_args()
    if args.selftest:
        selftest()
        return
    if args.block <= 0 or args.before < 0 or args.after < 0:
        parser.error("positive block and non-negative padding required")
    study(args)


if __name__ == "__main__":
    main()
