"""Cloud-executable integration checks for the actual reusable export target."""

from array import array
from dataclasses import replace
from fractions import Fraction
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from vsize.assets import PreparedAsset
from vsize.contracts import ComputeBudget, EncodeSpec, Request
from vsize.engine import Engine
from vsize.runtime import Deadline


def _ffmpeg(arguments):
    return subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-v",
            "error",
            "-nostdin",
            "-xerror",
            "-y",
            "-threads",
            "1",
            "-filter_threads",
            "1",
            *arguments,
        ],
        check=True,
        capture_output=True,
    ).stdout


def _probe(path, *, packets=False):
    return json.loads(
        subprocess.check_output(
            [
                "ffprobe",
                "-v",
                "error",
                "-count_frames",
                "-show_streams",
                "-show_format",
                *(["-show_packets"] if packets else []),
                "-of",
                "json",
                str(path),
            ]
        )
    )


def _video(path, video_filter=None):
    return _ffmpeg(
        [
            "-i",
            str(path),
            "-map",
            "0:v:0",
            "-an",
            *(["-vf", video_filter] if video_filter else []),
            "-fps_mode",
            "passthrough",
            "-pix_fmt",
            "yuv420p",
            "-f",
            "rawvideo",
            "-",
        ]
    )


def _pcm(path):
    return _ffmpeg(["-i", str(path), "-map", "0:a:0", "-vn", "-f", "s16le", "-"])


@unittest.skipUnless(
    shutil.which("ffmpeg") and shutil.which("ffprobe") and hasattr(os, "memfd_create"),
    "FFmpeg and Linux immutable prepared assets required",
)
class SegmentedEngineIntegration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temporary.name)
        cls.av = cls.root / "video-with-longer-audio.mp4"
        _ffmpeg(
            [
                "-f",
                "lavfi",
                "-i",
                "testsrc2=s=128x96:r=24:d=4.125",
                "-f",
                "lavfi",
                "-i",
                "aevalsrc=sin(2*PI*523*t)|sin(2*PI*700*t):s=48000:d=4.75",
                "-c:v",
                "libx264",
                "-preset",
                "medium",
                "-crf",
                "18",
                "-threads",
                "1",
                "-c:a",
                "aac",
                "-b:a",
                "128k",
                str(cls.av),
            ]
        )
        cls.fractional = cls.root / "fractional-123-frames.mp4"
        _ffmpeg(
            [
                "-f",
                "lavfi",
                "-i",
                "testsrc2=s=128x96:r=30000/1001",
                "-frames:v",
                "123",
                "-an",
                "-c:v",
                "libx264",
                "-preset",
                "medium",
                "-crf",
                "18",
                "-threads",
                "1",
                str(cls.fractional),
            ]
        )
        cls.vfr = cls.root / "vfr.mp4"
        _ffmpeg(
            [
                "-i",
                str(cls.av),
                "-an",
                "-vf",
                "select='if(lt(n,48),1,not(mod(n,2)))'",
                "-fps_mode",
                "vfr",
                "-c:v",
                "libx264",
                "-preset",
                "medium",
                "-crf",
                "0",
                "-threads",
                "1",
                str(cls.vfr),
            ]
        )

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def setUp(self):
        self.work = self.root / self._testMethodName
        self.work.mkdir()
        self.cache_dir = self.work / "cache"
        self.engine = Engine(self.cache_dir)

    def request(self, source=None, **kwargs):
        return Request(
            EncodeSpec(source or self.av, export_mode="segmented", segment_seconds=1),
            compute=ComputeBudget(
                wall_seconds=30, max_probes=0, threads=1, max_encode_fraction=1.0
            ),
            **kwargs,
        )

    def assert_exact_byte_identity(self, result):
        self.assertEqual(result["status"], "satisfied", result)
        estimate = result["estimate"]
        self.assertEqual(estimate["uncertainty"]["kind"], "exact")
        artifact = Path(estimate["artifact"])
        self.assertEqual(artifact.stat().st_size, estimate["estimated_bytes"])
        accounting = estimate["byte_accounting"]
        total = sum(
            accounting[name]
            for name in (
                "init_bytes",
                "video_payload_bytes",
                "audio_payload_bytes",
                "fragment_bytes",
            )
        )
        self.assertEqual(total, estimate["estimated_bytes"])
        return artifact

    def test_partial_cached_probes_then_exact_census_reuses_actual_samples(self):
        with PreparedAsset.from_path(self.av) as asset:
            partial_request = replace(
                self.request(),
                compute=ComputeBudget(
                    wall_seconds=30, max_probes=1, threads=1, max_encode_fraction=0.25
                ),
            )
            partial = self.engine.run(partial_request, asset=asset)
            self.assertEqual(partial["estimate"]["uncertainty"]["kind"], "uncalibrated")
            self.assertEqual(partial["estimate"]["export_mode"], "segmented")
            self.assertEqual(partial["total_probe_count"], 1)
            self.assertAlmostEqual(partial["attempted_encode_seconds"], 1.0)
            known = partial["estimate"]["known_byte_accounting"]
            self.assertEqual(known["video_payload_bytes"], 0)
            self.assertGreater(known["audio_payload_bytes"], 0)

            only_cached = self.engine.run(
                replace(
                    partial_request,
                    compute=ComputeBudget(
                        wall_seconds=30,
                        max_probes=0,
                        threads=1,
                        max_encode_fraction=0,
                    ),
                ),
                asset=asset,
            )
            self.assertEqual(only_cached["attempted_encode_seconds"], 0)
            self.assertEqual(only_cached["total_probe_count"], 0)
            self.assertEqual(
                only_cached["estimate"]["estimated_bytes"], partial["estimate"]["estimated_bytes"]
            )

            exact = self.engine.run(self.request(), asset=asset)
            self.assert_exact_byte_identity(exact)
            self.assertGreaterEqual(exact["estimate"]["reused_segments"], 1)
            self.assertAlmostEqual(exact["attempted_encode_seconds"], 4.125 - 1.0)
            self.assertEqual(exact["total_probe_count"], 0)
            self.assertEqual(
                exact["estimate"]["byte_accounting"]["init_bytes"], known["init_bytes"]
            )
            self.assertEqual(
                exact["estimate"]["byte_accounting"]["fragment_bytes"], known["fragment_bytes"]
            )

            cached_exact = self.engine.run(
                replace(
                    self.request(),
                    compute=ComputeBudget(
                        wall_seconds=30,
                        max_probes=0,
                        threads=1,
                        max_encode_fraction=0,
                    ),
                ),
                asset=asset,
            )
            self.assertTrue(cached_exact["estimate"]["cache_hit"])
            self.assertEqual(cached_exact["attempted_encode_seconds"], 0)
            self.assertEqual(cached_exact["attempted_audio_seconds"], 0)
            self.assertEqual(cached_exact["estimate"]["sha256"], exact["estimate"]["sha256"])

    def test_fractional_rate_b_frames_and_three_frame_tail_decode_completely(self):
        with PreparedAsset.from_path(self.fractional) as asset:
            result = self.engine.run(self.request(self.fractional), asset=asset)
        artifact = self.assert_exact_byte_identity(result)
        info = _probe(artifact, packets=True)
        video = info["streams"][0]
        self.assertEqual(int(video["nb_read_frames"]), 123)
        self.assertGreater(video["has_b_frames"], 0)
        self.assertEqual(result["segment_plan"]["segments"][-1]["frame_count"], 3)
        self.assertEqual(Fraction(video["avg_frame_rate"]), Fraction(30000, 1001))
        self.assertAlmostEqual(float(video["duration"]), 123 * 1001 / 30000, delta=0.000002)
        ticks = sorted(p["pts"] for p in info["packets"])
        self.assertEqual(ticks, [1001 * n for n in range(123)])
        self.assertEqual(len(_video(artifact)), 123 * 128 * 96 * 3 // 2)

    def test_entire_audio_tail_and_decoded_pcm_match_single_full_aac_encode(self):
        with PreparedAsset.from_path(self.av) as asset:
            result = self.engine.run(self.request(), asset=asset)
        artifact = self.assert_exact_byte_identity(result)
        audio_metadata = []
        for path in self.cache_dir.glob("*.json"):
            record = json.loads(path.read_text())
            if record.get("kind") == "full-audio":
                audio_metadata.append((path, record))
        self.assertEqual(len(audio_metadata), 1)
        audio_path = audio_metadata[0][0].with_suffix(".mp4")
        actual, expected = _pcm(artifact), _pcm(audio_path)
        self.assertEqual(actual, expected)
        # The source's final 0.625 seconds occur after the video ends.
        self.assertGreater(len(actual) / (48000 * 2 * 2), 4.74)
        samples = array("h", actual[-int(0.2 * 48000 * 2 * 2) :])
        self.assertGreater(sum(abs(value) for value in samples) / len(samples), 500)
        info = _probe(artifact)
        self.assertEqual({s["codec_type"] for s in info["streams"]}, {"video", "audio"})
        self.assertAlmostEqual(float(info["streams"][0]["duration"]), 4.125, delta=0.000002)

    def test_tampered_metadata_is_rejected_then_rebuilt_from_valid_cached_samples(self):
        with PreparedAsset.from_path(self.av) as asset:
            first = self.engine.run(self.request(), asset=asset)
            artifact = self.assert_exact_byte_identity(first)
            metadata = artifact.with_suffix(".json")
            record = json.loads(metadata.read_text())
            record["file_bytes"] += 100
            record["crf"] = 50
            metadata.write_text(json.dumps(record))
            self.assertIsNone(self.engine.cache.lookup(record["key"], Deadline(10)))
            rebuilt = self.engine.run(
                replace(
                    self.request(),
                    compute=ComputeBudget(
                        wall_seconds=30,
                        max_probes=0,
                        threads=1,
                        max_encode_fraction=0,
                    ),
                ),
                asset=asset,
            )
        self.assert_exact_byte_identity(rebuilt)
        self.assertFalse(rebuilt["estimate"]["cache_hit"])
        self.assertEqual(rebuilt["estimate"]["selected_crf"], 23)
        self.assertEqual(
            rebuilt["estimate"]["estimated_bytes"], first["estimate"]["estimated_bytes"]
        )
        self.assertEqual(rebuilt["attempted_encode_seconds"], 0)
        self.assertEqual(rebuilt["total_probe_count"], 0)

    def test_continuous_and_segmented_results_never_share_artifact_cache(self):
        segmented_request = self.request(self.fractional)
        continuous_request = replace(
            segmented_request,
            encode=replace(segmented_request.encode, export_mode="continuous"),
        )
        with PreparedAsset.from_path(self.fractional) as asset:
            segmented = self.engine.run(segmented_request, asset=asset)
            continuous = self.engine.run(continuous_request, asset=asset)
            self.assertEqual(continuous["status"], "satisfied", continuous)
            self.assertNotEqual(
                segmented["estimate"]["artifact"], continuous["estimate"]["artifact"]
            )
            self.assertEqual(segmented["estimate"]["export_mode"], "segmented")
            self.assertEqual(continuous["estimate"]["export_mode"], "continuous")
            for request, expected in [
                (segmented_request, segmented),
                (continuous_request, continuous),
            ]:
                cached = self.engine.run(
                    replace(
                        request,
                        compute=ComputeBudget(
                            wall_seconds=30,
                            max_probes=0,
                            threads=1,
                            max_encode_fraction=0,
                        ),
                    ),
                    asset=asset,
                )
                self.assertTrue(cached["estimate"]["cache_hit"])
                self.assertEqual(cached["estimate"]["artifact"], expected["estimate"]["artifact"])
                self.assertEqual(cached["attempted_encode_seconds"], 0)

    def test_pipeline_version_invalidates_complete_artifact_cache(self):
        request = self.request(self.fractional)
        with PreparedAsset.from_path(self.fractional) as asset:
            first = self.engine.run(request, asset=asset)
            first_artifact = self.assert_exact_byte_identity(first)
            cached = Engine(self.cache_dir).run(request, asset=asset)
            self.assertTrue(cached["estimate"]["cache_hit"])
            self.assertEqual(cached["estimate"]["artifact"], str(first_artifact))
            with patch(
                "vsize.segmented_media.SEGMENTED_PIPELINE_VERSION",
                "independent-cfr-x264-next-regression-version",
            ):
                # Same source, configuration and cache directory; only the
                # pipeline version changes.  A fresh engine must rebuild.
                changed = Engine(self.cache_dir).run(request, asset=asset)
                changed_artifact = self.assert_exact_byte_identity(changed)
                self.assertFalse(changed["estimate"]["cache_hit"])
                self.assertNotEqual(changed_artifact, first_artifact)
                self.assertGreater(changed["attempted_encode_seconds"], 0)
                again = Engine(self.cache_dir).run(request, asset=asset)
                self.assertTrue(again["estimate"]["cache_hit"])
                self.assertEqual(again["estimate"]["artifact"], str(changed_artifact))
        self.assertEqual(int(_probe(changed_artifact)["streams"][0]["nb_read_frames"]), 123)
        self.assertEqual(len(_video(changed_artifact)), 123 * 128 * 96 * 3 // 2)

    def test_vfr_requires_explicit_fps_and_converted_export_preserves_all_frames(self):
        request = self.request(self.vfr)
        with PreparedAsset.from_path(self.vfr) as asset:
            with self.assertRaisesRegex(ValueError, "VFR input requires an explicit fps"):
                self.engine.run(request, asset=asset)
            converted_request = replace(
                request,
                encode=replace(
                    request.encode,
                    video_filter="fps=24",
                    crf=0,
                ),
            )
            result = self.engine.run(converted_request, asset=asset)
        artifact = self.assert_exact_byte_identity(result)
        expected = _video(self.vfr, "fps=24")
        actual = _video(artifact)
        self.assertEqual(actual, expected)
        expected_frames = len(expected) // (128 * 96 * 3 // 2)
        self.assertEqual(result["segment_plan"]["total_frames"], expected_frames)
        self.assertEqual(result["estimate"]["byte_accounting"]["video_samples"], expected_frames)


if __name__ == "__main__":
    unittest.main()
