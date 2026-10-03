"""Real content-discovery, cache-integrity and budget regression tests."""

import json
import math
import shutil
import subprocess
import tempfile
import time
import unittest
from fractions import Fraction
from pathlib import Path
from unittest.mock import patch

from vsize import ComputeBudget, EncodeSpec, Reliability, Request
from vsize.assets import PreparedAsset
from vsize.calibration import relative_error_bound
from vsize.engine import Engine
from vsize.media import Media
from vsize.runtime import Cache, Deadline
from vsize.trajectory import harmonic_point


class ContentContracts(unittest.TestCase):
    def test_sampling_plan_round_trip_and_validation(self):
        self.assertEqual(Request(EncodeSpec(Path("fixture.mp4"))).sampling_plan, "temporal")
        request = Request.from_dict(
            {"encode": {"source": "fixture.mp4"}, "sampling_plan": "content"}
        )
        self.assertEqual(request.sampling_plan, "content")
        with self.assertRaises(ValueError):
            Request(EncodeSpec(Path("fixture.mp4")), sampling_plan="unknown")

    def test_large_integer_endpoints_keep_one_byte_relative_error(self):
        low = 2**54
        high = low + 1
        expected = float(Fraction(1, high))
        error = relative_error_bound(low, [low, high])
        self.assertGreater(error, 0)
        self.assertTrue(math.isclose(error, expected, rel_tol=1e-15))
        self.assertEqual(harmonic_point([low, high]), low)
        self.assertGreater(error, Reliability(relative_error=expected / 2).relative_error)
        self.assertLessEqual(error, Reliability(relative_error=expected).relative_error)
        self.assertEqual(relative_error_bound(low, [low, low]), 0)


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg required")
class RealContentDiscovery(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temporary.name)
        cls.burst = cls.root / "burst.mp4"
        cls.quiet = cls.root / "quiet.mp4"
        for source, noise in [(cls.burst, True), (cls.quiet, False)]:
            generator = "color=c=0x316a8c:s=128x96:r=24:d=12"
            if noise:
                generator += ",noise=alls=50:allf=t+u:all_seed=8871:enable='between(t,5,7)'"
            subprocess.run(
                [
                    "ffmpeg",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-nostdin",
                    "-y",
                    "-filter_threads",
                    "1",
                    "-f",
                    "lavfi",
                    "-i",
                    generator,
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
                    "-an",
                    str(source),
                ],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def setUp(self):
        self.work = self.root / self._testMethodName
        self.work.mkdir()
        self.source = self.work / "upload.mp4"
        shutil.copyfile(self.burst, self.source)

    def media(self, name="features", *, source=None, video_filter=None, asset=None):
        return Media(
            EncodeSpec(source or self.source, video_filter=video_filter, drop_audio=True),
            1,
            Cache(self.work / name),
            Deadline(30),
            asset=asset,
        )

    def test_preview_avoids_source_packet_scan_and_hot_cache_avoids_another_decode(self):
        media = self.media()
        blocks = media.metadata_blocks(1)
        with patch.object(Media, "scan", side_effect=AssertionError("source packet scan")):
            with patch.object(media.deadline, "run", wraps=media.deadline.run) as commands:
                first, cold = media.preview_features(blocks)
        calls = [call.args[0] for call in commands.call_args_list]
        self.assertEqual(sum("rawvideo" in command for command in calls), 1)
        self.assertFalse(any("-show_packets" in command for command in calls))
        self.assertFalse(cold["cache_hit"])
        self.assertGreater(cold["elapsed_seconds"], 0)
        self.assertEqual(cold["preview_fps"], 4)
        self.assertGreater(max(b["visual_texture"] for b in first), 0)
        with patch.object(
            Media, "_visual_features", side_effect=AssertionError("duplicate decode")
        ):
            second, warm = media.preview_features(media.metadata_blocks(1))
        self.assertTrue(warm["cache_hit"])
        self.assertEqual(warm["key"], cold["key"])
        self.assertEqual(first, second)

    def test_source_filter_and_block_plan_each_change_feature_identity(self):
        media = self.media()
        original, original_info = media.preview_features(media.metadata_blocks(1))
        _, different_plan = media.preview_features(media.metadata_blocks(1.5))
        filtered = self.media(video_filter="eq=contrast=0.5")
        _, different_filter = filtered.preview_features(filtered.metadata_blocks(1))
        self.source.unlink()
        shutil.copyfile(self.quiet, self.source)
        replacement = self.media()
        quiet, different_source = replacement.preview_features(replacement.metadata_blocks(1))
        keys = {
            item["key"]
            for item in [original_info, different_plan, different_filter, different_source]
        }
        self.assertEqual(len(keys), 4)
        self.assertTrue(
            all(
                not item["cache_hit"]
                for item in [different_plan, different_filter, different_source]
            )
        )
        self.assertGreater(max(b["visual_texture"] for b in original), 1)
        self.assertEqual(max(b["visual_texture"] for b in quiet), 0)

    def test_malformed_json_and_tampered_checksum_are_rebuilt_from_pixels(self):
        media = self.media()
        expected, info = media.preview_features(media.metadata_blocks(1))
        path = media.cache.root / f"{info['key']}.features.json"
        path.write_text("{not valid JSON")
        with patch.object(media.deadline, "run", wraps=media.deadline.run) as commands:
            repaired, info = media.preview_features(media.metadata_blocks(1))
        self.assertFalse(info["cache_hit"])
        self.assertEqual(repaired, expected)
        self.assertEqual(sum("rawvideo" in call.args[0] for call in commands.call_args_list), 1)
        saved = json.loads(path.read_text())
        saved["blocks"][0]["features"][1] += 42
        path.write_text(json.dumps(saved))
        rebuilt, bad_checksum = media.preview_features(media.metadata_blocks(1))
        self.assertFalse(bad_checksum["cache_hit"])
        self.assertEqual(rebuilt, expected)
        _, hot = media.preview_features(media.metadata_blocks(1))
        self.assertTrue(hot["cache_hit"])
        self.assertEqual(list(media.cache.root.glob("partial-features-*")), [])

    def test_prepared_upload_keeps_its_features_when_named_source_is_replaced(self):
        with PreparedAsset.from_path(self.source) as asset:
            prepared = self.media(asset=asset)
            expected, first = prepared.preview_features(prepared.metadata_blocks(1))
            self.source.unlink()
            shutil.copyfile(self.quiet, self.source)
            retained = self.media(asset=asset)
            with patch.object(
                Media, "_visual_features", side_effect=AssertionError("prepared cache decoded")
            ):
                old_version, hot = retained.preview_features(retained.metadata_blocks(1))
            self.assertTrue(hot["cache_hit"])
            self.assertEqual(hot["key"], first["key"])
            self.assertEqual(old_version, expected)
            self.assertEqual(retained.source_hash, asset.sha256)
            # Also verify an uncached preview reads sealed bytes, not the replacement.
            uncached = self.media("fresh-prepared", asset=asset)
            with patch(
                "vsize.media.digest", side_effect=AssertionError("prepared source rehashed")
            ):
                fresh, cold = uncached.preview_features(uncached.metadata_blocks(1))
            self.assertFalse(cold["cache_hit"])
            self.assertEqual(fresh, expected)
        replacement = self.media()
        _, current = replacement.preview_features(replacement.metadata_blocks(1))
        self.assertNotEqual(current["key"], first["key"])

    def test_noninteger_windows_assign_high_texture_to_the_actual_content_interval(self):
        media = self.media()
        blocks = [
            {"start": 0.0, "duration": 5.25, "input_bytes": 0},
            {"start": 5.25, "duration": 1.5, "input_bytes": 0},
            {"start": 6.75, "duration": 5.25, "input_bytes": 0},
        ]
        measured, _ = media.preview_features(blocks)
        self.assertEqual(
            [(b["start"], b["duration"]) for b in measured], [(0, 5.25), (5.25, 1.5), (6.75, 5.25)]
        )
        self.assertGreater(measured[1]["visual_texture"], 5 * measured[0]["visual_texture"])
        self.assertGreater(measured[1]["visual_texture"], 5 * measured[2]["visual_texture"])

    def test_content_bare_budget_selects_rare_and_quiet_content_without_certifying_confidence(self):
        for fraction, expected_count in [(0.2, 2), (0.25, 3)]:
            with self.subTest(fraction=fraction):
                engine = Engine(self.work / f"engine-{fraction}")
                request = Request(
                    EncodeSpec(self.source, drop_audio=True),
                    compute=ComputeBudget(
                        wall_seconds=20, max_probes=3, threads=1, max_encode_fraction=fraction
                    ),
                    sample_seconds=1,
                    probe_mode="bare",
                    sampling_plan="content",
                    seed=17,
                )
                with patch.object(Media, "scan", side_effect=AssertionError("source packet scan")):
                    result = engine.run(request)
                self.assertIsNotNone(result["estimate"], result)
                self.assertIsNotNone(result["content_discovery"], result)
                self.assertFalse(result["content_discovery"]["cache_hit"])
                self.assertEqual(result["pilot_count"], expected_count)
                self.assertEqual(result["total_probe_count"], expected_count)
                estimate = result["estimate"]
                self.assertEqual(estimate["probe_mode"], "bare")
                self.assertEqual(len(estimate["observed_blocks"]), expected_count)
                self.assertEqual(set(estimate["pilot_indices"]), set(estimate["observed_blocks"]))
                features = json.loads(
                    (
                        engine.cache.root / f"{result['content_discovery']['key']}.features.json"
                    ).read_text()
                )["blocks"]
                textures = [features[i]["visual_texture"] for i in estimate["observed_blocks"]]
                peak = max(b["visual_texture"] for b in features)
                self.assertGreaterEqual(max(textures), peak * 0.5)
                self.assertLessEqual(min(textures), peak * 0.1)
                self.assertLessEqual(result["attempted_encode_seconds"], 12 * fraction + 1e-7)
                self.assertGreaterEqual(
                    result["first_estimate_seconds"], result["content_discovery"]["elapsed_seconds"]
                )
                self.assertEqual(estimate["uncertainty"]["kind"], "uncalibrated")
                self.assertIsNone(estimate["uncertainty"]["interval_bytes"])
                self.assertIsNone(estimate["uncertainty"]["coverage"])
                self.assertIn("reliability_not_calibrated", result["unmet"])
                self.assertNotEqual(result["status"], "satisfied")

    def test_actual_preview_subprocess_timeout_leaves_no_publishable_feature_partial(self):
        engine = Engine(self.work / "preview-timeout")
        request = Request(
            EncodeSpec(self.source, drop_audio=True),
            compute=ComputeBudget(
                wall_seconds=20, max_probes=3, threads=1, max_encode_fraction=0.25
            ),
            sample_seconds=1,
            probe_mode="bare",
            sampling_plan="content",
        )
        deadlines = []
        processes = []
        original_launch = subprocess.Popen

        def capture_deadline(seconds):
            deadline = Deadline(seconds)
            deadlines.append(deadline)
            return deadline

        def expire_after_actual_preview_launch(command, **kwargs):
            process = original_launch(command, **kwargs)
            if "rawvideo" in command:
                processes.append(process)
                # Real FFmpeg was started; only interruption scheduling is controlled.
                deadlines[0].ends = time.monotonic() + 0.001
            return process

        with patch("vsize.engine.Deadline", side_effect=capture_deadline):
            with patch(
                "vsize.runtime.subprocess.Popen", side_effect=expire_after_actual_preview_launch
            ):
                result = engine.run(request)
        self.assertEqual(len(processes), 1)
        self.assertIsNotNone(processes[0].poll())
        self.assertEqual(result["status"], "budget_exhausted", result)
        self.assertIsNone(result["estimate"])
        self.assertEqual(result["attempted_encode_seconds"], 0)
        self.assertEqual(list(engine.cache.root.glob("partial-*")), [])
        self.assertEqual(list(engine.cache.root.glob("*.features.json")), [])


if __name__ == "__main__":
    unittest.main()
