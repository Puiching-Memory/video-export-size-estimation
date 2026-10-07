import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import numpy as np

from vsize import EncodeSpec
from vsize.context_probe import context_window, policy_fingerprint, probe
from vsize.media import Media
from vsize.runtime import Cache, Deadline


class ContextPolicy(unittest.TestCase):
    def media(self, preset="medium", video_filter=None):
        return SimpleNamespace(
            spec=SimpleNamespace(preset=preset, video_filter=video_filter), fps=10.0, duration=30.0
        )

    def test_lookahead_uses_output_frames_and_preserves_encoding_cost(self):
        plan = context_window(self.media(), (5, 1))
        self.assertEqual(plan["encoding_window"][0], 3)
        self.assertGreaterEqual(plan["available_future_seconds"], 4.3)
        self.assertEqual(plan["encoded_media_seconds"], 7.5)
        faster = context_window(self.media(preset="ultrafast"), (5, 1))
        self.assertLess(faster["encoded_media_seconds"], plan["encoded_media_seconds"])
        filtered = context_window(self.media(video_filter="scale=64:36,fps=25"), (5, 1))
        self.assertEqual(filtered["policy"]["output_fps"], 25)
        self.assertLess(filtered["available_future_seconds"], plan["available_future_seconds"])
        self.assertNotEqual(filtered["policy_fingerprint"], plan["policy_fingerprint"])
        self.assertEqual(policy_fingerprint(self.media()), plan["policy_fingerprint"])

    def test_window_bounds_and_tail_truncation_are_explicit(self):
        plan = context_window(self.media(), (29, 1))
        self.assertEqual(plan["available_future_seconds"], 0)
        self.assertTrue(plan["at_source_end"])
        for window in [(-1, 1), (0, 0), (30, 1), (30 + 1e-7, 1e-7), (float("nan"), 1)]:
            with self.assertRaises(ValueError):
                context_window(self.media(), window)


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg required")
class RealContextProbe(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temporary.name)
        cls.source = cls.root / "two-audio-10fps.mp4"
        subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-nostdin",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "testsrc2=s=128x72:r=10:d=8",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=440:sample_rate=48000:duration=8",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=880:sample_rate=48000:duration=8",
                "-map",
                "0:v:0",
                "-map",
                "1:a:0",
                "-map",
                "2:a:0",
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                "-crf",
                "16",
                "-threads",
                "1",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-b:a",
                "96k",
                str(cls.source),
            ],
            check=True,
        )

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def media(self, name, **options):
        return Media(EncodeSpec(self.source, **options), 1, Cache(self.root / name), Deadline(30))

    def test_central_measurement_audio_selection_cost_and_cached_artifact(self):
        media = self.media("central")
        record = probe(media, 23, (2, 1))
        self.assertEqual(record["video_frames"], 10)
        self.assertGreater(record["audio_payload_bytes"], 0)
        self.assertEqual(record["central_init_sei_bytes"], 0)
        self.assertGreater(record["global_init_sei_bytes"], 0)
        self.assertGreaterEqual(record["available_future_frames"], 43)
        self.assertGreater(record["encoded_media_seconds"], 7)
        self.assertEqual(record["duration"], 1)
        self.assertNotIn("artifact", record)
        self.assertEqual(record["bias_status"], "context_approximation_requires_validation")
        self.assertEqual(sum(d["frames"] for d in record["picture_types"].values()), 10)
        self.assertEqual(
            record["payload_bytes"], record["video_payload_bytes"] + record["audio_payload_bytes"]
        )
        again = probe(media, 23, (2, 1))
        self.assertTrue(again["cache_hit"])
        self.assertEqual(again["payload_bytes"], record["payload_bytes"])
        self.assertEqual(again["warmup_artifact"], record["warmup_artifact"])

        info = json.loads(
            subprocess.check_output(
                [
                    "ffprobe",
                    "-v",
                    "error",
                    "-show_streams",
                    "-of",
                    "json",
                    record["warmup_artifact"],
                ]
            )
        )
        self.assertEqual(sum(s["codec_type"] == "audio" for s in info["streams"]), 1)
        # The requested contract retains the first source audio, not the second.
        audio = subprocess.check_output(
            [
                "ffmpeg",
                "-v",
                "error",
                "-threads",
                "1",
                "-i",
                record["warmup_artifact"],
                "-map",
                "0:a:0",
                "-t",
                "1",
                "-ac",
                "1",
                "-ar",
                "8000",
                "-f",
                "f32le",
                "pipe:1",
            ]
        )
        samples = np.frombuffer(audio, dtype=np.float32)
        peak = np.argmax(np.abs(np.fft.rfft(samples))) * 8000 / len(samples)
        self.assertAlmostEqual(peak, 440, delta=2)

    def test_first_window_initialization_sei_is_separate_and_i_is_not_blindly_added(self):
        record = probe(self.media("initialization"), 23, (0, 1))
        self.assertGreater(record["central_init_sei_bytes"], 0)
        self.assertEqual(record["central_init_sei_bytes"], record["global_init_sei_bytes"])
        self.assertEqual(
            sum(values["content_payload_bytes"] for values in record["picture_types"].values()),
            record["video_payload_bytes"],
        )
        self.assertEqual(
            record["raw_payload_bytes"] - record["payload_bytes"],
            record["central_init_sei_bytes"],
        )
        self.assertGreaterEqual(record["picture_types"]["I"]["frames"], 1)
        self.assertEqual(record["periodic_adjustment"]["extra_i_frames"], 0)
        self.assertEqual(record["periodic_adjusted_payload_bytes"], record["payload_bytes"])

    def test_dropped_audio_and_source_tail_are_reported(self):
        record = probe(self.media("no-audio", drop_audio=True), 23, (7, 1))
        self.assertEqual(record["audio_payload_bytes"], 0)
        self.assertEqual(record["audio_packets"], 0)
        self.assertEqual(record["available_future_frames"], 0)
        self.assertTrue(record["future_context_truncated_at_source_end"])
        self.assertAlmostEqual(record["encoded_media_seconds"], 3, delta=0.01)
        self.assertEqual(record["video_frames"], 10)


if __name__ == "__main__":
    unittest.main()
