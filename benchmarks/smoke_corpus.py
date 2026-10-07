"""Check native-size two-second exports; produce no predictor accuracy labels."""

import argparse
import json
import subprocess
from pathlib import Path

from corpus import MEDIA, write_json

from vsize import EncodeSpec
from vsize.media import Media
from vsize.runtime import Cache, Deadline, digest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=MEDIA)
    args = parser.parse_args()
    # Include actual screen capture, unusual portrait dimensions, 4K camera
    # content, audio-bearing long-form distribution, and the VFR derivative.
    names = [
        "debugging",
        "mobile-sharing",
        "shaky-fireworks-4k",
        "cosmos-sdr-long",
        "cosmos-vfr-90s",
    ]
    records = []
    for name in names:
        derived = name == "cosmos-vfr-90s"
        metadata = args.root / (
            "derived/cosmos-vfr-90s.json" if derived else f"validation/{name}.json"
        )
        source_record = json.loads(metadata.read_text())
        source = args.root / source_record["source"]
        if digest(source) != source_record["sha256"]:
            raise ValueError(f"source changed: {name}")
        # Use the middle (12 fps) section of the VFR file, not its CFR opening.
        start = 40 if derived else 0
        media = Media(EncodeSpec(source), 2, Cache(args.root / "smoke-cache" / name), Deadline(180))
        record = media.encode(23, (start, 2))
        artifact = record["artifact"]
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
                artifact,
                "-map",
                "0:v:0",
                "-map",
                "0:a?",
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
                    artifact,
                ],
                text=True,
            )
        )
        video = next(s for s in info["streams"] if s["codec_type"] == "video")
        has_audio = any(s["codec_type"] == "audio" for s in info["streams"])
        if (video["width"], video["height"]) != (media.video["width"], media.video["height"]):
            raise ValueError("native geometry was not preserved")
        if has_audio != bool(media.audio) or not 1.9 <= float(info["format"]["duration"]) <= 2.2:
            raise ValueError("audio or export duration did not match the contract")
        evidence = {
            "id": name,
            "source_sha256": source_record["sha256"],
            "window_seconds": [start, 2],
            "passed": True,
            "width": video["width"],
            "height": video["height"],
            "video_frames": int(video["nb_frames"]),
            "audio_preserved": has_audio,
            "output_sha256": record["sha256"],
            "output_bytes": record["file_bytes"],
            "output_duration": float(info["format"]["duration"]),
            "predictor_accuracy_evaluated": False,
        }
        records.append(evidence)
        print(json.dumps(evidence), flush=True)
    write_json(
        args.root / "export-smoke.json",
        {
            "note": "Functional two-second native-resolution Media.encode exports only. No size prediction, confidence, HDR fidelity, or full-length export performance evaluation.",
            "ffmpeg": subprocess.check_output(["ffmpeg", "-version"], text=True).splitlines()[0],
            "configuration": {"codec": "libx264", "crf": 23, "preset": "medium", "threads": 2},
            "cases": records,
        },
    )


if __name__ == "__main__":
    main()
