"""A reproducible VFR + audio fixture from the reserved Cosmos source.

This is an explicit workflow derivative, not another independent film or CTC
sequence. It inherits the parent's split, group, and attribution obligations.
"""

import argparse
import json
import subprocess
from collections import Counter
from pathlib import Path

from corpus import (
    CATALOG,
    LOCK,
    MEDIA,
    check_bytes,
    hashes,
    load_catalog,
    load_lock,
    media_path,
    write_json,
)


def derive(catalog, root, lock):
    asset = next(a for a in catalog["assets"] if a["id"] == "cosmos-sdr-long")
    source = media_path(root, asset)
    parent = check_bytes(source, asset, lock[asset["id"]])
    directory = root / "derived"
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / "cosmos-vfr-90s.mp4"
    receipt = directory / "cosmos-vfr-90s.json"
    # Preserve timestamps; drop alternate frames only in the middle 30 seconds.
    options = [
        "-ss",
        "60",
        "-i",
        str(source),
        "-t",
        "90",
        "-map",
        "0:v:0",
        "-map",
        "0:a:0",
        "-vf",
        "select='not(between(t,30,60))+between(t,30,60)*not(mod(n,2))'",
        "-fps_mode",
        "vfr",
        "-c:v",
        "libx264",
        "-preset",
        "fast",
        "-crf",
        "18",
        "-threads",
        "2",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        "-movflags",
        "+faststart",
    ]
    recipe = [value if value != str(source) else "{parent}" for value in options]
    toolchain = subprocess.check_output(["ffmpeg", "-version"], text=True).splitlines()[0]
    if target.exists():
        previous = json.loads(receipt.read_text())
        if (
            previous["sha256"] != hashes(target)["sha256"]
            or previous["parent_sha256"] != parent["sha256"]
        ):
            raise ValueError("existing derivative or its parent changed")
        if previous["recipe"] != recipe or previous["ffmpeg"] != toolchain:
            raise ValueError("recipe or toolchain changed; choose a fresh output directory")
        print(json.dumps({"reused": str(target)}))
        return
    temporary = directory / "cosmos-vfr-90s.partial.mp4"
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-v", "error", "-nostdin", "-y", *options, str(temporary)],
        check=True,
    )
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-xerror",
            "-nostdin",
            "-threads",
            "2",
            "-i",
            str(temporary),
            "-map",
            "0:v:0",
            "-map",
            "0:a:0",
            "-fps_mode",
            "passthrough",
            "-f",
            "null",
            "-",
        ],
        check=True,
    )
    info = json.loads(
        subprocess.check_output(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_streams",
                "-show_format",
                "-of",
                "json",
                str(temporary),
            ],
            text=True,
        )
    )
    frames = json.loads(
        subprocess.check_output(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_frames",
                "-show_entries",
                "frame=best_effort_timestamp_time",
                "-of",
                "json",
                str(temporary),
            ],
            text=True,
        )
    )["frames"]
    pts = [float(f["best_effort_timestamp_time"]) for f in frames]
    intervals = Counter(round(pts[i + 1] - pts[i], 6) for i in range(len(pts) - 1))
    # Round to milliseconds to merge only the 1-us timestamp representation jitter.
    cadence_ms = {round(interval * 1000) for interval in intervals}
    audio = [s for s in info["streams"] if s["codec_type"] == "audio"]
    if cadence_ms != {42, 83} or len(pts) != 1800 or len(audio) != 1:
        raise ValueError(
            f"VFR/audio recipe not reproduced: {cadence_ms}, {len(pts)} frames, {len(audio)} audio streams"
        )
    target_info = hashes(temporary)
    record = {
        "schema_version": 1,
        "id": "cosmos-vfr-90s",
        "parent_id": asset["id"],
        "parent_sha256": parent["sha256"],
        "group_id": asset["group_id"],
        "split": asset["split"],
        "source": str(target.relative_to(root)),
        **target_info,
        "scope": "sdr_8bit",
        "license": asset["license"],
        "source_url": asset["url"],
        "changes": "Seconds 60–150; alternating 24/12/24 fps while preserving time; x264 CRF18 and AAC128k stereo re-encode. Not CTC, not an independent source.",
        "ffmpeg": toolchain,
        "recipe": recipe,
        "frames": len(pts),
        "frame_interval_seconds": dict(sorted(intervals.items())),
        "streams": info["streams"],
        "duration": info["format"]["duration"],
        "full_decode_passed": True,
        "predictor_accuracy_evaluated": False,
    }
    temporary.replace(target)
    write_json(receipt, record)
    write_json(
        root / "manifest-derived-calibration_reserved.json",
        {
            "schema_version": 1,
            "split": asset["split"],
            "scope": "sdr_8bit",
            "note": "A VFR/audio workflow fixture; shares the Cosmos group with all parent variants.",
            "cases": [
                {
                    "id": record["id"],
                    "group_id": asset["group_id"],
                    "source": str(target.resolve()),
                    "sha256": record["sha256"],
                    "scope": "sdr_8bit",
                    "kind": "derived-public-video",
                }
            ],
        },
    )
    print(
        json.dumps(
            {
                "derived_verified": record["id"],
                "frames": len(pts),
                "cadence_ms": sorted(cadence_ms),
                "bytes": record["bytes"],
            }
        ),
        flush=True,
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, default=CATALOG)
    parser.add_argument("--root", type=Path, default=MEDIA)
    parser.add_argument("--lock", type=Path, default=LOCK)
    args = parser.parse_args()
    catalog = load_catalog(args.catalog)
    derive(catalog, args.root, load_lock(args.lock, catalog["assets"]))


if __name__ == "__main__":
    main()
