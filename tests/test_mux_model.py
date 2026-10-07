import io
import json
import shutil
import struct
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from vsize.mux_model import estimate_video_container, inspect_video_container
from vsize.runtime import BudgetExhausted, Deadline


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg required")
class VideoContainerCensus(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temporary.name)
        cls.files = {}
        for name, source in (
            ("static", "color=c=gray:s=160x96:r=24"),
            ("motion", "testsrc2=s=160x96:r=24"),
        ):
            for duration in (2, 4):
                target = cls.root / f"{name}-{duration}.mp4"
                cls.ffmpeg(
                    [
                        "-f",
                        "lavfi",
                        "-i",
                        source,
                        "-t",
                        str(duration),
                        "-c:v",
                        "libx264",
                        "-preset",
                        "medium",
                        "-crf",
                        "23",
                        "-threads",
                        "1",
                        "-movflags",
                        "+faststart",
                        str(target),
                    ]
                )
                cls.files[name, duration] = target

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    @staticmethod
    def ffmpeg(arguments):
        subprocess.run(
            [
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
                *arguments,
            ],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    @staticmethod
    def packets(path):
        return json.loads(
            subprocess.check_output(
                [
                    "ffprobe",
                    "-v",
                    "error",
                    "-select_streams",
                    "v:0",
                    "-show_packets",
                    "-show_entries",
                    "packet=size,flags",
                    "-of",
                    "json",
                    str(path),
                ]
            )
        )["packets"]

    def test_real_medium_static_motion_have_exact_table_census_and_identity_prediction(self):
        for (name, duration), path in self.files.items():
            with self.subTest(name=name, duration=duration):
                info = inspect_video_container(path, Deadline(2))
                self.assertIsNotNone(info)
                packets = self.packets(path)
                payload = sum(int(packet["size"]) for packet in packets)
                keyframes = sum("K" in packet["flags"] for packet in packets)
                self.assertEqual(info["video_frames"], len(packets))
                self.assertEqual(info["video_frames"], 24 * duration)
                self.assertEqual(info["payload_bytes"], payload)
                self.assertEqual(info["container_bytes"], path.stat().st_size - payload)
                self.assertEqual(
                    info["container_bytes"], info["constant_bytes"] + info["dynamic_bytes"]
                )
                self.assertEqual(info["dynamic_bytes"], sum(info["dynamic_table_bytes"].values()))
                self.assertEqual(
                    estimate_video_container(info, len(packets), keyframes),
                    path.stat().st_size - payload,
                )
        for name in ("static", "motion"):
            probe = inspect_video_container(self.files[name, 2])
            full = inspect_video_container(self.files[name, 4])
            self.assertEqual(probe["constant_bytes"], full["constant_bytes"])
            self.assertGreater(estimate_video_container(probe, full["video_frames"]), 0)
            self.assertIn("no bound", probe["estimation_kind"])

    def test_census_never_reads_encoded_mdat_payload(self):
        data = self.files["motion", 4].read_bytes()
        mdat = data.index(b"mdat") - 4
        mdat_size = struct.unpack_from(">I", data, mdat)[0]
        payload_start, payload_end = mdat + 8, mdat + mdat_size

        class GuardedReader(io.BytesIO):
            def read(self, size=-1):
                start = self.tell()
                end = len(data) if size < 0 else start + size
                if start < payload_end and end > payload_start:
                    raise AssertionError("encoded payload must be skipped")
                return super().read(size)

        with patch.object(Path, "open", return_value=GuardedReader(data)):
            info = inspect_video_container(self.files["motion", 4])
        self.assertEqual(info["file_bytes"], len(data))

    def test_real_multichunk_video_has_exact_sample_offsets_and_identity(self):
        target = self.root / "noisy-multichunk.mp4"
        self.ffmpeg(
            [
                "-f",
                "lavfi",
                "-i",
                "testsrc2=s=320x180:r=24:d=2",
                "-vf",
                "noise=alls=100:allf=t+u",
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                "-crf",
                "0",
                "-threads",
                "1",
                "-movflags",
                "+faststart",
                str(target),
            ]
        )
        info = inspect_video_container(target)
        self.assertIsNotNone(info)
        self.assertGreater(info["chunks"], 1)
        packets = self.packets(target)
        self.assertEqual(info["payload_bytes"], sum(int(packet["size"]) for packet in packets))
        self.assertEqual(info["video_frames"], len(packets))
        self.assertEqual(
            estimate_video_container(info, len(packets), info["keyframe_count"]),
            target.stat().st_size - info["payload_bytes"],
        )
        modified = bytearray(target.read_bytes())
        offset_position = modified.index(b"stco") + 12
        offset = struct.unpack_from(">I", modified, offset_position)[0]
        struct.pack_into(">I", modified, offset_position, offset + 1)
        corrupt = self.root / "chunk-offset-mismatch.mp4"
        corrupt.write_bytes(modified)
        self.assertIsNone(inspect_video_container(corrupt))

    def test_audio_and_fragmented_outputs_fall_back(self):
        audio = self.root / "with-audio.mp4"
        self.ffmpeg(
            [
                "-i",
                str(self.files["motion", 2]),
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=523:sample_rate=48000:duration=2",
                "-c:v",
                "copy",
                "-c:a",
                "aac",
                "-threads",
                "1",
                str(audio),
            ]
        )
        self.assertIsNone(inspect_video_container(audio))
        fragmented = self.root / "fragmented.mp4"
        self.ffmpeg(
            [
                "-i",
                str(self.files["motion", 2]),
                "-c",
                "copy",
                "-movflags",
                "+frag_keyframe+empty_moov",
                str(fragmented),
            ]
        )
        self.assertIsNone(inspect_video_container(fragmented))

    def test_unknown_wide_and_inconsistent_tables_fall_back(self):
        original = self.files["motion", 2].read_bytes()
        for old, new in ((b"stco", b"co64"), (b"stsz", b"stz2"), (b"stts", b"uuid")):
            with self.subTest(box=new):
                modified = bytearray(original)
                position = modified.index(old)
                modified[position : position + 4] = new
                target = self.root / f"unsupported-{new.decode()}.mp4"
                target.write_bytes(modified)
                self.assertIsNone(inspect_video_container(target))
        modified = bytearray(original)
        sample_count = modified.index(b"stsz") + 12
        struct.pack_into(">I", modified, sample_count, 1)
        target = self.root / "count-mismatch.mp4"
        target.write_bytes(modified)
        self.assertIsNone(inspect_video_container(target))
        modified = bytearray(original)
        mdat_header = modified.index(b"mdat") - 4
        struct.pack_into(">I", modified, mdat_header, 1)
        target = self.root / "wide-mdat.mp4"
        target.write_bytes(modified)
        self.assertIsNone(inspect_video_container(target))
        target = self.root / "truncated.mp4"
        target.write_bytes(original[:15])
        self.assertIsNone(inspect_video_container(target))

    def test_deadline_exhaustion_propagates_instead_of_selecting_fallback(self):
        deadline = Deadline(1)
        deadline.ends = deadline.started
        with self.assertRaises(BudgetExhausted):
            inspect_video_container(self.files["static", 2], deadline)

    def test_estimator_rejects_invalid_requests(self):
        info = inspect_video_container(self.files["static", 2])
        for frames in (0, -1, 1.0, True):
            with self.subTest(frames=frames), self.assertRaises(ValueError):
                estimate_video_container(info, frames)
        for keys in (0, -1, 100, True, 1.5):
            with self.subTest(keys=keys), self.assertRaises(ValueError):
                estimate_video_container(info, 48, keys)
        with self.assertRaises(ValueError):
            estimate_video_container(None, 48)


if __name__ == "__main__":
    unittest.main()
