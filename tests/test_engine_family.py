"""Actual-media regression for strategy parameters entering calibration keys."""

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from vsize import ComputeBudget, EncodeSpec, Request
from vsize.engine import Engine


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg required")
class EngineFamilyIntegration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        directory = tempfile.TemporaryDirectory()
        cls.addClassCleanup(directory.cleanup)
        cls.root = Path(directory.name)
        cls.source = cls.root / "source.mp4"
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
                "testsrc2=size=64x48:rate=8:duration=4",
                "-an",
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                "-threads",
                "1",
                "-pix_fmt",
                "yuv420p",
                str(cls.source),
            ],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    def test_segmented_fraction_changes_family_when_it_changes_pilot_strategy(self):
        engine = Engine(self.root / "cache")
        results = []
        for fraction in [0.02, 0.5]:
            results.append(
                engine.run(
                    Request(
                        EncodeSpec(
                            self.source,
                            preset="ultrafast",
                            drop_audio=True,
                            export_mode="segmented",
                            segment_seconds=1,
                        ),
                        ComputeBudget(
                            wall_seconds=10,
                            max_probes=3,
                            threads=1,
                            max_encode_fraction=fraction,
                        ),
                    )
                )
            )
        low, high = results
        self.assertNotEqual(low["prediction_family_key"], high["prediction_family_key"])
        self.assertEqual(low["prediction_family"]["probe_policy"]["max_encode_fraction"], 0.02)
        self.assertEqual(high["prediction_family"]["probe_policy"]["max_encode_fraction"], 0.5)
        self.assertNotEqual(low["pilot_count"], high["pilot_count"])
        self.assertEqual(low["attempted_encode_seconds"], 0)
        self.assertIsNone(low["estimate"])
        self.assertGreater(high["attempted_encode_seconds"], 0)
        self.assertEqual(high["estimate"]["uncertainty"]["kind"], "uncalibrated")
        self.assertIn("reliability_not_calibrated", high["unmet"])
        self.assertLessEqual(high["attempted_encode_seconds"], 2)


if __name__ == "__main__":
    unittest.main()
