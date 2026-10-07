"""Structural development regression; no accuracy or heldout label evaluation."""

import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))

from vsize import EncodeSpec  # noqa: E402
from vsize.fmp4 import write_fragmented_mp4  # noqa: E402
from vsize.media import Media  # noqa: E402
from vsize.runtime import Cache, Deadline, digest  # noqa: E402
from vsize.segmented_media import SegmentedMedia  # noqa: E402

OUTPUT = Path(__file__).resolve().parent
SOURCE = OUTPUT / "rawvideo-delayed-video.mkv"
LEGACY = ROOT / "results/industrial-core-2026-10-03/holdout/code/src/vsize/segmented_media.py"


def frames(deadline, path, selector):
    return json.loads(
        deadline.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-threads",
                "1",
                "-select_streams",
                selector,
                "-read_intervals",
                "%+#8",
                "-show_frames",
                "-show_streams",
                "-show_entries",
                "frame=pts,nb_samples:stream=time_base,start_pts,pix_fmt",
                "-of",
                "json",
                str(path),
            ]
        ).stdout
    )


def main():
    deadline = Deadline(60)
    generation = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-y",
        "-threads",
        "1",
        "-filter_threads",
        "1",
        "-filter_complex_threads",
        "1",
        "-copyts",
        "-f",
        "lavfi",
        "-i",
        "testsrc2=s=128x96:r=24:d=1",
        "-f",
        "lavfi",
        "-i",
        "anullsrc=r=48000:cl=mono",
        "-map",
        "0:v:0",
        "-map",
        "1:a:0",
        "-vf",
        "setpts=PTS+5.125/TB",
        "-c:v",
        "rawvideo",
        "-pix_fmt",
        "bgr24",
        "-allow_raw_vfw",
        "1",
        "-threads",
        "1",
        "-c:a",
        "aac",
        "-t",
        "7",
        str(SOURCE),
    ]
    deadline.run(generation)
    spec = importlib.util.spec_from_file_location("vsize._frozen_v2_segmented", LEGACY)
    legacy = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = legacy
    spec.loader.exec_module(legacy)
    media = Media(
        EncodeSpec(SOURCE, video_filter="fps=12", preset="veryfast"),
        1,
        Cache(OUTPUT / ".vsize-cache"),
        deadline,
    )
    evidence = {
        "development_only": True,
        "not_accuracy_evaluation": True,
        "source_command": generation,
        "source_sha256": digest(SOURCE),
        "frozen_adapter_path": str(LEGACY),
        "frozen_adapter_sha256": digest(LEGACY),
        "source_stream_summary": media.info,
        "input_first_video": frames(deadline, SOURCE, "v:0"),
        "input_first_audio": frames(deadline, SOURCE, "a:0"),
    }
    adapter = legacy.SegmentedMedia(media, 0.5)
    evidence["legacy_plan"] = adapter.prepare()
    records = [adapter.encode_segment(i, 23) for i in range(len(adapter.segments))]
    segments = [adapter.read_segment(record) for record in records]
    audio = adapter.encode_audio()
    audio_track, audio_packets = adapter.read_audio(audio)
    output = OUTPUT / "legacy-lost-delay.mp4"
    output.unlink(missing_ok=True)
    accounting = write_fragmented_mp4(
        output,
        segments[0][0],
        [packets for _, packets in segments],
        audio_track=audio_track,
        audio_packets=audio_packets,
        deadline=deadline,
    )
    evidence["legacy_output"] = {
        "path": str(output),
        "sha256": digest(output),
        "bytes": output.stat().st_size,
        "exact_byte_accounting": accounting.total_bytes == output.stat().st_size,
        "first_video": frames(deadline, output, "v:0"),
        "first_audio": frames(deadline, output, "a:0"),
    }
    deadline.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-xerror",
            "-threads",
            "1",
            "-filter_threads",
            "1",
            "-i",
            str(output),
            "-threads",
            "1",
            "-f",
            "null",
            "-",
        ]
    )
    evidence["legacy_output"]["full_decode_passed"] = True
    try:
        SegmentedMedia(media, 0.5)
    except ValueError as error:
        evidence["maintenance_rejection"] = str(error)
    else:
        raise AssertionError("maintenance adapter must reject actual A/V delay")
    (OUTPUT / "evidence.json").write_text(json.dumps(evidence, indent=2) + "\n")
    print(
        json.dumps(
            {
                "legacy_accepted_and_exported": True,
                "output_bytes": output.stat().st_size,
                "maintenance_rejection": evidence["maintenance_rejection"],
            }
        )
    )


if __name__ == "__main__":
    main()
