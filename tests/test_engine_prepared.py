"""Real small-video coverage of sealed uploads entering the FFmpeg adapter."""

import json
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from vsize.assets import PreparedAsset
from vsize.contracts import ComputeBudget, EncodeSpec, Request, SizeConstraint
from vsize.engine import Engine
from vsize.media import Media
from vsize.runtime import BudgetExhausted, Cache, Deadline
from vsize.search import CRFSearch
from vsize.segmented_media import SegmentedMedia


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg required")
class PreparedMediaEncoding(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temporary.name)
        cls.original = cls.root / "fixture.mp4"
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
                "testsrc2=s=128x96:r=24:d=4",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=523:sample_rate=48000:duration=4",
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                "-crf",
                "18",
                "-threads",
                "1",
                "-c:a",
                "aac",
                "-b:a",
                "128k",
                "-shortest",
                str(cls.original),
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
        shutil.copyfile(self.original, self.source)
        self.asset = PreparedAsset.from_path(self.source)
        self.addCleanup(self.asset.close)

    def media(self, cache_name):
        return Media(
            EncodeSpec(self.source),
            1,
            Cache(self.work / cache_name),
            Deadline(10),
            asset=self.asset,
        )

    def probe_output(self, artifact):
        return json.loads(
            subprocess.check_output(
                [
                    "ffprobe",
                    "-v",
                    "error",
                    "-show_streams",
                    "-show_format",
                    "-of",
                    "json",
                    str(artifact),
                ]
            )
        )

    def test_media_full_export_uses_inherited_sealed_fd_without_source_hash_reads(self):
        self.source.write_bytes(b"original path is no longer a valid video")
        with patch("vsize.media.digest", side_effect=AssertionError("prepared source rehashed")):
            media = self.media("full")
            record = media.encode(23)
        self.assertEqual(record["source_hash"], self.asset.sha256)
        self.assertEqual(record["video_frames"], 96)
        info = self.probe_output(record["artifact"])
        self.assertEqual({stream["codec_type"] for stream in info["streams"]}, {"video", "audio"})
        self.assertAlmostEqual(float(info["format"]["duration"]), 4, delta=0.05)

    def test_original_mutation_does_not_change_uncached_output_of_the_upload_version(self):
        with patch("vsize.media.digest", side_effect=AssertionError("prepared source rehashed")):
            first = self.media("before-mutation").encode(23)
            self.source.unlink()
            self.source.write_bytes(b"replacement upload")
            second = self.media("after-mutation").encode(23)
        self.assertFalse(first["cache_hit"])
        self.assertFalse(second["cache_hit"])
        self.assertEqual(first["sha256"], second["sha256"])
        self.assertEqual(first["file_bytes"], second["file_bytes"])
        self.assertEqual(first["source_hash"], self.asset.sha256)

    def test_media_rejects_asset_for_another_source_path(self):
        other = self.work / "other-upload.mp4"
        shutil.copyfile(self.original, other)
        with self.assertRaises(ValueError):
            Media(
                EncodeSpec(other), 1, Cache(self.work / "mismatch"), Deadline(10), asset=self.asset
            )

    def test_media_rejects_closed_asset_before_starting_an_encoder(self):
        self.asset.close()
        with patch("vsize.runtime.subprocess.Popen") as launch:
            with self.assertRaises(ValueError):
                self.media("closed")
            launch.assert_not_called()

    def request(self, *, wall=10, probes=0, fraction=None, mode="continuous"):
        return Request(
            EncodeSpec(self.source, export_mode=mode, segment_seconds=1),
            compute=ComputeBudget(
                wall_seconds=wall, max_probes=probes, max_encode_fraction=fraction, threads=1
            ),
            sample_seconds=1,
            probe_mode="bare",
        )

    def test_engine_reports_preparation_cost_separately_and_exports_snapshot(self):
        request = self.request()
        self.source.write_bytes(b"the source path has changed after ingestion")
        with patch("vsize.media.digest", side_effect=AssertionError("prepared source rehashed")):
            result = Engine(self.work / "engine-full").run(request, asset=self.asset)
        self.assertEqual(result["status"], "satisfied", result)
        self.assertEqual(result["estimate"]["uncertainty"]["kind"], "exact")
        self.assertEqual(result["estimate"]["source_hash"], self.asset.sha256)
        self.assertEqual(result["prepared_asset"]["ingest_seconds"], self.asset.ingest_seconds)
        self.assertFalse(result["prepared_asset"]["ingest_in_request_budget"])
        self.assertEqual(result["total_probe_count"], 0)
        self.assertAlmostEqual(result["attempted_encode_seconds"], 4, delta=0.05)
        video = next(
            stream
            for stream in self.probe_output(result["estimate"]["artifact"])["streams"]
            if stream["codec_type"] == "video"
        )
        self.assertEqual(int(video["nb_frames"]), 96)

    def test_engine_limited_probe_avoids_whole_source_scan_and_accounts_video_work(self):
        encode = Media.encode
        attempted = []

        def track_encode(media, crf, window=None):
            attempted.append(window[1] if window else media.duration)
            return encode(media, crf, window)

        request = self.request(wall=4, probes=2, fraction=0.26)
        with patch("vsize.media.digest", side_effect=AssertionError("prepared source rehashed")):
            with patch.object(Media, "scan", side_effect=AssertionError("whole-source scan")):
                with patch.object(Media, "encode", new=track_encode):
                    result = Engine(self.work / "engine-limited").run(request, asset=self.asset)
        self.assertIsNotNone(result["estimate"], result)
        self.assertEqual(result["estimate"]["uncertainty"]["kind"], "uncalibrated")
        self.assertEqual(result["total_probe_count"], 1)
        self.assertAlmostEqual(sum(attempted), result["attempted_encode_seconds"])
        self.assertLessEqual(sum(attempted), 4 * 0.26 + 1e-7)
        self.assertLessEqual(result["elapsed_seconds"], request.compute.wall_seconds + 0.5)
        self.assertIn("reliability_not_calibrated", result["unmet"])

    def test_engine_verified_cache_is_available_with_zero_new_video_budget(self):
        engine = Engine(self.work / "engine-cache")
        with patch("vsize.media.digest", side_effect=AssertionError("prepared source rehashed")):
            first = engine.run(self.request(), asset=self.asset)
            self.assertEqual(first["status"], "satisfied", first)
            self.source.write_bytes(b"new source version")
            cached = engine.run(self.request(wall=2, fraction=0), asset=self.asset)
        self.assertEqual(cached["status"], "satisfied", cached)
        self.assertTrue(cached["estimate"]["cache_hit"])
        self.assertEqual(cached["estimate"]["sha256"], first["estimate"]["sha256"])
        self.assertEqual(cached["attempted_encode_seconds"], 0)
        self.assertEqual(cached["total_probe_count"], 0)
        self.assertLessEqual(cached["elapsed_seconds"], 2.5)

    def test_engine_empty_cache_and_zero_video_budget_does_not_start_encode_work(self):
        engine = Engine(self.work / "engine-no-budget")
        with patch("vsize.media.digest", side_effect=AssertionError("prepared source rehashed")):
            with patch.object(Media, "encode", side_effect=AssertionError("unbudgeted encode")):
                result = engine.run(self.request(wall=2, probes=2, fraction=0), asset=self.asset)
        self.assertIsNone(result["estimate"])
        self.assertEqual(result["attempted_encode_seconds"], 0)
        self.assertEqual(result["total_probe_count"], 0)
        self.assertIn("no_estimate_within_budget", result["unmet"])
        self.assertEqual(list(engine.cache.root.glob("*.mp4")), [])

    def test_engine_tiny_deadline_returns_without_publishable_partials(self):
        engine = Engine(self.work / "engine-timeout")
        started = time.monotonic()
        result = engine.run(self.request(wall=0.001), asset=self.asset)
        self.assertEqual(result["status"], "budget_exhausted", result)
        self.assertIsNone(result["estimate"])
        self.assertLess(time.monotonic() - started, 0.75)
        self.assertEqual(list(engine.cache.root.glob("partial-*")), [])
        self.assertEqual(list(engine.cache.root.glob("*.mp4")), [])

    def test_engine_upload_identity_survives_original_path_becoming_a_symlink(self):
        request = self.request()
        self.source.unlink()
        self.source.symlink_to(self.original)
        with patch("vsize.media.digest", side_effect=AssertionError("prepared source rehashed")):
            result = Engine(self.work / "engine-replaced-symlink").run(request, asset=self.asset)
        self.assertEqual(result["status"], "satisfied", result)
        self.assertEqual(result["estimate"]["source_hash"], self.asset.sha256)

    def test_segmented_audio_cost_is_explicit_and_video_fraction_is_enforced(self):
        request = self.request(wall=5, probes=1, fraction=0.26, mode="segmented")
        with patch("vsize.media.digest", side_effect=AssertionError("prepared source rehashed")):
            result = Engine(self.work / "engine-segmented-budget").run(request, asset=self.asset)
        self.assertIsNotNone(result["estimate"], result)
        self.assertEqual(result["estimate"]["export_mode"], "segmented")
        self.assertLessEqual(result["attempted_encode_seconds"], 4 * 0.26 + 1e-7)
        self.assertGreaterEqual(result["attempted_audio_seconds"], 3.9)
        self.assertLess(result["attempted_audio_seconds"], 4.1)
        self.assertIn("requested_output_video_seconds", result["encode_fraction_basis"])
        self.assertIn("audio charged to wall budget", result["encode_fraction_basis"])
        self.assertLessEqual(result["elapsed_seconds"], request.compute.wall_seconds + 0.5)

    def test_callback_budget_stop_keeps_the_lowest_newly_verified_feasible_crf(self):
        # Only scheduling and interruption are controlled here. Every accepted
        # file size and checksum comes from an actual complete FFmpeg export.
        references = self.media("callback-references")
        sizes = {crf: references.encode(crf)["file_bytes"] for crf in (23, 29, 30)}
        limit = max(sizes[29], sizes[30])
        self.assertLess(limit, sizes[23], "fixture must make the starting CRF oversized")
        request = Request(
            EncodeSpec(self.source),
            compute=ComputeBudget(wall_seconds=10, max_probes=0, threads=1),
            size=SizeConstraint(max_bytes=limit, max_crf=30, max_candidates=4),
            probe_mode="bare",
        )
        feasible_events = []

        def schedule_refinement(search):
            for crf in (30.0, 29.0):
                if crf not in search.attempted:
                    return crf
            return None

        def interrupt_after_better_file(event):
            if event["uncertainty"]["kind"] == "exact" and event["estimated_bytes"] <= limit:
                feasible_events.append(dict(event))
                if event["selected_crf"] == 29:
                    raise BudgetExhausted("forced stop after verified CRF29")

        with patch.object(CRFSearch, "propose", new=schedule_refinement):
            with patch(
                "vsize.media.digest", side_effect=AssertionError("prepared source rehashed")
            ):
                result = Engine(self.work / "engine-callback-refinement").run(
                    request, interrupt_after_better_file, asset=self.asset
                )
        self.assertEqual([event["selected_crf"] for event in feasible_events], [30, 29])
        self.assertEqual(result["status"], "satisfied", result)
        self.assertEqual(result["estimate"]["selected_crf"], 29)
        self.assertEqual(result["quality_search"]["best_crf"], 29)
        self.assertEqual(result["estimate"]["sha256"], feasible_events[-1]["sha256"])
        destination = self.work / "best-verified.mp4"
        Engine.materialize(result, destination)
        self.assertEqual(destination.stat().st_size, result["estimate"]["estimated_bytes"])

    def test_cancelled_uncached_audio_work_is_recorded_before_completion(self):
        request = self.request(wall=5, probes=1, fraction=0.26, mode="segmented")
        with patch.object(
            SegmentedMedia, "encode_audio", side_effect=BudgetExhausted("cancelled audio encode")
        ):
            result = Engine(self.work / "engine-audio-cancel").run(request, asset=self.asset)
        self.assertEqual(result["status"], "budget_exhausted", result)
        self.assertGreaterEqual(result["attempted_audio_seconds"], 3.9)
        self.assertLess(result["attempted_audio_seconds"], 4.1)
        self.assertLessEqual(result["attempted_encode_seconds"], 4 * 0.26 + 1e-7)
        self.assertIsNone(result["estimate"])


if __name__ == "__main__":
    unittest.main()
