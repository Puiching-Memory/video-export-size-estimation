import json
import shutil
import subprocess
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path

from vsize import ComputeBudget, EncodeSpec, Request, SizeConstraint
from vsize.engine import Engine
from vsize.media import Media
from vsize.runtime import BudgetExhausted, Cache, Deadline


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg required")
class RealEncoding(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temporary.name)
        cls.source = cls.root / "source.mp4"
        subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "testsrc2=s=320x180:r=24:d=8",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=523:sample_rate=48000:duration=8",
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                "-crf",
                "16",
                "-threads",
                "2",
                "-c:a",
                "aac",
                "-b:a",
                "128k",
                "-shortest",
                str(cls.source),
            ],
            check=True,
        )

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def engine(self, name):
        return Engine(self.root / name)

    def request(self, **kwargs):
        return Request(
            EncodeSpec(self.source),
            compute=ComputeBudget(wall_seconds=20),
            sample_seconds=1,
            **kwargs,
        )

    def test_budgeted_prediction_never_claims_unvalidated_confidence(self):
        request = replace(
            self.request(),
            compute=ComputeBudget(wall_seconds=10, max_probes=2, max_encode_fraction=0.26),
        )
        result = self.engine("limited").run(request)
        self.assertIsNotNone(result["estimate"])
        self.assertEqual(result["estimate"]["uncertainty"]["kind"], "uncalibrated")
        self.assertIn("reliability_not_calibrated", result["unmet"])
        self.assertLessEqual(result["attempted_encode_seconds"], 8 * 0.26)
        with self.assertRaises(ValueError):
            Engine.materialize(result, self.root / "not-certified.mp4")

    def test_exact_export_cache_and_materialization(self):
        engine = self.engine("exact")
        result = engine.run(self.request())
        self.assertEqual(result["status"], "satisfied", result)
        record = result["estimate"]
        self.assertEqual(record["uncertainty"]["kind"], "exact")
        cached = engine.run(
            replace(
                self.request(),
                compute=ComputeBudget(wall_seconds=5, max_probes=0, max_encode_fraction=0),
            )
        )
        self.assertTrue(cached["estimate"]["cache_hit"])
        self.assertEqual(cached["attempted_encode_seconds"], 0)
        target = self.root / "export.mp4"
        Engine.materialize(cached, target)
        self.assertEqual(target.stat().st_size, record["estimated_bytes"])
        with self.assertRaises(FileExistsError):
            Engine.materialize(cached, target)
        subprocess.run(
            ["ffmpeg", "-v", "error", "-xerror", "-i", str(target), "-f", "null", "-"], check=True
        )
        info = json.loads(
            subprocess.check_output(
                [
                    "ffprobe",
                    "-v",
                    "error",
                    "-show_entries",
                    "stream=codec_type,width,height,nb_frames",
                    "-of",
                    "json",
                    str(target),
                ]
            )
        )
        self.assertEqual({s["codec_type"] for s in info["streams"]}, {"video", "audio"})
        video = next(s for s in info["streams"] if s["codec_type"] == "video")
        self.assertEqual(int(video["nb_frames"]), 192)

    def test_strict_limit_search_and_failure(self):
        engine = self.engine("size-control")
        reference = engine.run(self.request())["estimate"]["estimated_bytes"]
        limit = int(reference * 0.70)
        request = replace(
            self.request(),
            size=SizeConstraint(max_bytes=limit, max_crf=40, max_candidates=5),
            compute=ComputeBudget(wall_seconds=35, max_probes=6),
        )
        result = engine.run(request)
        self.assertEqual(result["status"], "satisfied", result)
        self.assertLessEqual(result["estimate"]["estimated_bytes"], limit)
        self.assertGreater(result["estimate"]["selected_crf"], 23)
        tiny = engine.run(replace(request, size=SizeConstraint(max_bytes=100, max_crf=23)))
        self.assertIn("size_limit_exceeded_at_tested_setting", tiny["unmet"])
        with self.assertRaises(ValueError):
            Engine.materialize(tiny, self.root / "too-big.mp4")

    def test_filter_change_invalidates_artifact(self):
        engine = self.engine("filters")
        plain = engine.run(self.request())
        changed = engine.run(
            replace(self.request(), encode=EncodeSpec(self.source, video_filter="scale=160:90"))
        )
        self.assertNotEqual(plain["estimate"]["artifact"], changed["estimate"]["artifact"])
        artifact = changed["estimate"]["artifact"]
        info = json.loads(
            subprocess.check_output(
                [
                    "ffprobe",
                    "-v",
                    "error",
                    "-select_streams",
                    "v:0",
                    "-show_entries",
                    "stream=width,height",
                    "-of",
                    "json",
                    artifact,
                ]
            )
        )["streams"][0]
        self.assertEqual((info["width"], info["height"]), (160, 90))

    def test_zero_probe_limit_allows_complete_export(self):
        result = self.engine("direct").run(
            replace(self.request(), compute=ComputeBudget(wall_seconds=15, max_probes=0))
        )
        self.assertEqual(result["status"], "satisfied", result)
        self.assertEqual(result["estimate"]["sample_count"], 0)
        self.assertAlmostEqual(result["attempted_encode_seconds"], 8, delta=0.1)

    def test_size_search_reuses_full_cost_after_probe_budget_is_spent(self):
        reference = self.engine("cap-reference").run(self.request())["estimate"]["estimated_bytes"]
        result = self.engine("cap-one-probe").run(
            replace(
                self.request(),
                compute=ComputeBudget(wall_seconds=25, max_probes=1),
                size=SizeConstraint(max_bytes=int(reference * 0.7), max_crf=40),
            )
        )
        self.assertEqual(result["status"], "satisfied", result)
        self.assertLessEqual(result["estimate"]["sample_count"], 1)
        self.assertLessEqual(result["estimate"]["estimated_bytes"], int(reference * 0.7))

    def test_cache_corruption_is_rejected(self):
        engine = self.engine("corrupt")
        first = engine.run(self.request())
        artifact = Path(first["estimate"]["artifact"])
        data = bytearray(artifact.read_bytes())
        data[len(data) // 2] ^= 1
        artifact.write_bytes(data)
        second = engine.run(self.request())
        self.assertEqual(second["status"], "satisfied", second)
        self.assertFalse(second["estimate"]["cache_hit"])

    def test_deadline_terminates_work_and_drops_partial_artifacts(self):
        deadline = Deadline(0.08)
        started = time.monotonic()
        with self.assertRaises(BudgetExhausted):
            deadline.run(["python", "-c", "import time; time.sleep(30)"])
        self.assertLess(time.monotonic() - started, 1.0)
        result = self.engine("timeout").run(
            replace(self.request(), compute=ComputeBudget(wall_seconds=0.01))
        )
        self.assertEqual(result["status"], "budget_exhausted")
        self.assertEqual(list((self.root / "timeout").glob("partial-*")), [])

    def test_actual_encode_interruption_leaves_no_publishable_artifact(self):
        cache = Cache(self.root / "encode-interruption")
        deadline = Deadline(10)
        media = Media(EncodeSpec(self.source, preset="veryslow"), 1, cache, deadline)
        deadline.ends = time.monotonic() + 0.1
        with self.assertRaises(BudgetExhausted):
            media.encode(23)
        self.assertEqual(list(cache.root.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
