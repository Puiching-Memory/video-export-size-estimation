import json
import os
import shutil
import subprocess
import tempfile
import unittest
from fractions import Fraction
from pathlib import Path
from unittest.mock import patch

from vsize import EncodeSpec
from vsize.assets import PreparedAsset
from vsize.fmp4 import planned_overhead, write_fragmented_mp4
from vsize.media import Media
from vsize.runtime import Cache, Deadline, digest
from vsize.segmented_media import SegmentedMedia


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg required")
class IndependentSegments(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temporary.name)
        cls.source = cls.root / "fractional-rate.mp4"
        cls.run_ffmpeg(
            [
                "-f",
                "lavfi",
                "-i",
                "testsrc2=s=160x96:r=30000/1001",
                "-frames:v",
                "143",
                "-c:v",
                "libx264",
                "-crf",
                "0",
                "-preset",
                "ultrafast",
                "-threads",
                "1",
                str(cls.source),
            ]
        )
        cls.vfr = cls.root / "vfr.mp4"
        cls.run_ffmpeg(
            [
                "-i",
                str(cls.source),
                "-vf",
                "select='if(lt(n,60),1,not(mod(n,2)))'",
                "-fps_mode",
                "vfr",
                "-c:v",
                "libx264",
                "-crf",
                "0",
                "-preset",
                "ultrafast",
                "-threads",
                "1",
                str(cls.vfr),
            ]
        )
        cls.aligned_audio = cls.root / "aligned-with-aac-priming.mp4"
        cls.run_ffmpeg(
            [
                "-f",
                "lavfi",
                "-i",
                "testsrc2=s=160x96:r=24:d=2",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=523:sample_rate=48000:duration=2",
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                "-threads",
                "1",
                "-c:a",
                "aac",
                str(cls.aligned_audio),
            ]
        )

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    @staticmethod
    def run_ffmpeg(arguments):
        return subprocess.run(
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
        ).stdout

    def adapter(self, name, *, source=None, video_filter=None, preset="veryfast", segment=0.7):
        media = Media(
            EncodeSpec(source or self.source, preset=preset, video_filter=video_filter),
            1,
            Cache(self.root / name),
            Deadline(60),
        )
        return SegmentedMedia(media, segment)

    def raw(self, path, video_filter=None):
        args = ["-i", str(path), "-map", "0:v:0", "-an"]
        if video_filter:
            args += ["-vf", video_filter]
        args += ["-fps_mode", "passthrough", "-pix_fmt", "yuv420p", "-f", "rawvideo", "pipe:1"]
        return self.run_ffmpeg(args)

    def test_fractional_rate_random_access_has_no_missing_or_repeated_frames(self):
        adapter = self.adapter("pixel-exact")
        plan = adapter.prepare()
        self.assertEqual(plan["output_rate"], "30000/1001")
        self.assertEqual(plan["total_frames"], 143)
        self.assertEqual([s.frame_count for s in adapter.segments], [21] * 6 + [17])
        # Deliberately encode out of temporal order; these are reusable final
        # segments, and decoder warmup must not change their selected pixels.
        records = {i: adapter.encode_segment(i, 0) for i in (6, 0, 3, 1, 5, 2, 4)}
        combined = b"".join(self.raw(records[i]["artifact"]) for i in range(7))
        self.assertEqual(combined, self.raw(self.source))
        self.assertEqual(len({r["extradata_hex"] for r in records.values()}), 1)
        self.assertEqual(sum(r["video_frames"] for r in records.values()), 143)
        self.assertEqual(adapter.verify_source(), digest(self.source))
        self.assertTrue(adapter.plan()["source_identity_verified_at_publication"])

    def test_b_frames_and_short_tail_share_one_sample_entry(self):
        adapter = self.adapter("b-frames", segment=1.4)
        adapter.prepare()
        records = [adapter.encode_segment(i, 23) for i in range(len(adapter.segments))]
        self.assertTrue(any(r["first_dts"] < 0 for r in records))
        self.assertEqual(len({r["extradata_hex"] for r in records}), 1)
        self.assertEqual(sum(r["duration_ticks"] for r in records), 143143)
        for record in records:
            self.assertTrue(record["first_keyframe"])
            self.assertEqual(record["first_pts"], 0)
            self.assertEqual(record["duration_ticks"], record["video_frames"] * 1001)

    def test_nonzero_source_origin_survives_timestamp_seek(self):
        offset = self.root / "offset.mp4"
        self.run_ffmpeg(
            ["-i", str(self.source), "-c", "copy", "-output_ts_offset", "5", str(offset)]
        )
        adapter = self.adapter("offset-cache", source=offset)
        adapter.prepare()
        records = [adapter.encode_segment(i, 0) for i in range(len(adapter.segments))]
        self.assertEqual(b"".join(self.raw(r["artifact"]) for r in records), self.raw(offset))

    def test_vfr_requires_explicit_conversion_and_preserves_whole_filter_phase(self):
        with self.assertRaisesRegex(ValueError, "VFR input requires an explicit fps"):
            self.adapter("vfr-rejected", source=self.vfr).prepare()
        video_filter = "fps=24,scale=80:48"
        adapter = self.adapter("vfr-converted", source=self.vfr, video_filter=video_filter)
        with patch.object(adapter.deadline, "run", wraps=adapter.deadline.run) as processes:
            adapter.prepare()
            records = [adapter.encode_segment(i, 0) for i in range(len(adapter.segments))]
        # Both the full FPS counting pass and every encode must honor the same
        # filter-thread budget; FFmpeg otherwise chooses its own worker count.
        commands = [
            call.args[0] for call in processes.call_args_list if call.args[0][0] == "ffmpeg"
        ]
        self.assertGreater(len(commands), 1)
        for command in commands:
            self.assertEqual(command[command.index("-filter_threads") + 1], "1")
            self.assertEqual(command[command.index("-filter_complex_threads") + 1], "1")
        expected = self.raw(self.vfr, video_filter)
        self.assertEqual(b"".join(self.raw(r["artifact"]) for r in records), expected)
        self.assertEqual(adapter.total_frames, len(expected) // (80 * 48 * 3 // 2))
        self.assertEqual({(r["width"], r["height"]) for r in records}, {(80, 48)})

    def test_source_identity_hash_once_at_publication_and_cache_reuse(self):
        adapter = self.adapter("identity-cache")
        adapter.prepare()
        with patch("vsize.media.digest", wraps=digest) as source_digest:
            first = adapter.encode_segment(0, 23)
            adapter.encode_segment(1, 23)
            self.assertEqual(source_digest.call_count, 0)
            adapter.verify_source()
            self.assertEqual(source_digest.call_count, 1)
        another = self.adapter("identity-cache")
        self.assertTrue(another.prepare()["prepare_cache_hit"])
        cached = another.encode_segment(0, 23)
        self.assertTrue(cached["cache_hit"])
        self.assertEqual(cached["artifact"], first["artifact"])
        corrupt = Path(cached["artifact"])
        content = bytearray(corrupt.read_bytes())
        content[len(content) // 2] ^= 1
        corrupt.write_bytes(content)
        self.assertFalse(another.encode_segment(0, 23)["cache_hit"])
        metadata_path = another.cache.root / f"{cached['key']}.json"
        metadata = json.loads(metadata_path.read_text())
        metadata["payload_bytes"] += 1
        metadata_path.write_text(json.dumps(metadata))
        self.assertFalse(another.encode_segment(0, 23)["cache_hit"])

    def test_source_mutation_during_session_is_rejected(self):
        source = self.root / "mutable.mp4"
        shutil.copyfile(self.source, source)
        adapter = self.adapter("mutation", source=source)
        adapter.prepare()
        with source.open("ab") as stream:
            stream.write(b"changed")
        with self.assertRaisesRegex(RuntimeError, "input changed"):
            adapter.encode_segment(0, 23)

    def test_mutation_between_initial_hash_and_adapter_is_caught_at_publication(self):
        source = self.root / "pre-adapter-mutation.mp4"
        shutil.copyfile(self.source, source)
        media = Media(EncodeSpec(source), 1, Cache(self.root / "pre-mutation"), Deadline(60))
        with source.open("ab") as stream:
            stream.write(b"changed before adapter stat snapshot")
        adapter = SegmentedMedia(media)
        adapter.prepare()
        with self.assertRaisesRegex(RuntimeError, "input changed"):
            adapter.verify_source()

    def test_whole_audio_tail_and_priming_are_preserved(self):
        source = self.root / "long-audio.mp4"
        self.run_ffmpeg(
            [
                "-f",
                "lavfi",
                "-i",
                "testsrc2=s=160x96:r=24:d=2",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=523:sample_rate=48000:duration=3.5",
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                "-crf",
                "16",
                "-threads",
                "1",
                "-c:a",
                "aac",
                str(source),
            ]
        )
        adapter = self.adapter("audio-tail", source=source)
        adapter.prepare()
        self.assertIsNone(adapter.lookup_audio())
        audio = adapter.encode_audio()
        self.assertEqual(audio["key"], adapter.audio_key())
        self.assertTrue(adapter.lookup_audio()["cache_hit"])
        self.assertEqual(audio["codec"], "aac")
        self.assertLess(audio["first_dts"], 0)
        self.assertTrue(audio["priming_side_data"])
        self.assertGreater(audio["presentation_duration"] / audio["sample_rate"], 3.45)
        self.assertGreater(audio["duration_ticks"] / audio["sample_rate"], adapter.duration)
        self.assertTrue(adapter.encode_audio()["cache_hit"])
        records = [adapter.encode_segment(i, 23) for i in range(len(adapter.segments))]
        tracks = [adapter.read_segment(record) for record in records]
        audio_track, audio_packets = adapter.read_audio(audio)
        target = self.root / "joined-with-audio-tail.mp4"
        accounting = write_fragmented_mp4(
            target,
            tracks[0][0],
            [packets for _, packets in tracks],
            audio_track=audio_track,
            audio_packets=audio_packets,
            deadline=adapter.deadline,
        )
        self.assertEqual(accounting.total_bytes, target.stat().st_size)
        self.assertEqual(accounting.video_samples, adapter.total_frames)
        self.assertEqual(
            accounting.container_bytes,
            planned_overhead(
                tracks[0][0],
                [len(packets) for _, packets in tracks],
                audio_track=audio_track,
                audio_packets=audio_packets,
            ),
        )
        actual_audio = self.run_ffmpeg(
            ["-i", str(target), "-map", "0:a:0", "-f", "s16le", "pipe:1"]
        )
        original_audio = self.run_ffmpeg(
            ["-i", audio["artifact"], "-map", "0:a:0", "-f", "s16le", "pipe:1"]
        )
        self.assertEqual(actual_audio, original_audio)

    def test_corrupt_cached_plan_is_rebuilt_without_full_decode(self):
        adapter = self.adapter("plan-rebuild")
        adapter.prepare()
        path = next(adapter.cache.root.glob("*.plan.json"))
        plan = json.loads(path.read_text())
        plan["values"]["total_frames"] += 1
        path.write_text(json.dumps(plan))
        rebuilt = self.adapter("plan-rebuild")
        with patch.object(rebuilt.deadline, "run", wraps=rebuilt.deadline.run) as process:
            self.assertFalse(rebuilt.prepare()["prepare_cache_hit"])
        commands = [call.args[0] for call in process.call_args_list]
        self.assertTrue(any("-show_packets" in command for command in commands))
        self.assertFalse(any("-show_frames" in command for command in commands))

    def test_delayed_audio_is_rejected_instead_of_losing_sync(self):
        source = self.root / "delayed-audio.mp4"
        self.run_ffmpeg(
            [
                "-f",
                "lavfi",
                "-i",
                "testsrc2=s=160x96:r=24:d=2",
                "-itsoffset",
                "0.5",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=523:sample_rate=48000:duration=2",
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                "-threads",
                "1",
                "-c:a",
                "aac",
                str(source),
            ]
        )
        with self.assertRaisesRegex(ValueError, "aligned video/audio starts"):
            self.adapter("delayed-audio-cache", source=source)

    def test_matroska_delayed_video_is_rejected_despite_matching_stream_starts(self):
        # Reproduce the actual Bunny-derived Matroska failure using a tiny
        # source whose rawvideo format is known from its header before the
        # first picture: stream.start_pts equals the format start for BOTH
        # streams, although video actually starts over five seconds later.
        source = self.root / "misleading-start-delayed-video.mkv"
        self.run_ffmpeg(
            [
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
                str(source),
            ]
        )
        media = Media(
            EncodeSpec(source, video_filter="fps=12"),
            1,
            Cache(self.root / "misleading-start"),
            Deadline(60),
        )
        self.assertEqual(media.video["pix_fmt"], "bgr24")
        self.assertEqual(media.video["start_pts"], media.audio["start_pts"])
        first_packets = json.loads(
            media.deadline.run(
                [
                    "ffprobe",
                    "-v",
                    "error",
                    "-threads",
                    "1",
                    "-select_streams",
                    "v:0",
                    "-read_intervals",
                    "%+#1",
                    "-show_packets",
                    "-show_entries",
                    "packet=pts",
                    "-of",
                    "json",
                    str(source),
                ]
            ).stdout
        )["packets"]
        self.assertEqual(
            int(first_packets[0]["pts"]) * Fraction(media.video["time_base"]), Fraction(41, 8)
        )
        with self.assertRaisesRegex(ValueError, "aligned video/audio starts.*decoded"):
            SegmentedMedia(media)
        self.assertFalse(list(media.cache.root.glob("*.mp4")))

    def test_actual_audio_start_applies_priming_once_and_checks_before_cached_plan(self):
        adapter = self.adapter("actual-aac-start", source=self.aligned_audio)
        plan = adapter.prepare()
        self.assertEqual(
            plan["source_presentation_starts"],
            {
                "video": "0",
                "audio": "0",
                "method": "first_decoded_frame_after_priming",
            },
        )
        # First AAC packet is priming.  Decoder has already skipped it, so the
        # start check must not add the skip count to the first decoded frame.
        packets = json.loads(
            adapter.deadline.run(
                [
                    "ffprobe",
                    "-v",
                    "error",
                    "-threads",
                    "1",
                    "-select_streams",
                    "a:0",
                    "-read_intervals",
                    "%+#1",
                    "-show_packets",
                    "-show_entries",
                    "packet=pts,side_data_list",
                    "-of",
                    "json",
                    str(self.aligned_audio),
                ]
            ).stdout
        )["packets"]
        self.assertLess(int(packets[0]["pts"]), 0)
        self.assertTrue(
            any(side.get("skip_samples", 0) > 0 for side in packets[0]["side_data_list"])
        )
        media = Media(
            EncodeSpec(self.aligned_audio, preset="veryfast"),
            1,
            Cache(self.root / "actual-aac-start"),
            Deadline(60),
        )
        with patch.object(media.deadline, "run", wraps=media.deadline.run) as processes:
            another = SegmentedMedia(media, 0.7)
            self.assertTrue(another.prepare()["prepare_cache_hit"])
        commands = [call.args[0] for call in processes.call_args_list]
        self.assertEqual(sum("-show_frames" in command for command in commands), 2)

    def test_aligned_nonzero_origin_uses_actual_presentation_frames(self):
        source = self.root / "aligned-common-origin.mkv"
        self.run_ffmpeg(
            [
                "-copyts",
                "-itsoffset",
                "5.125",
                "-i",
                str(self.source),
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=523:sample_rate=48000:duration=4.8",
                "-map",
                "0:v:0",
                "-map",
                "1:a:0",
                "-af",
                "asetpts=PTS+5.125/TB",
                "-c:v",
                "copy",
                "-c:a",
                "pcm_s16le",
                str(source),
            ]
        )
        adapter = self.adapter("aligned-common-origin", source=source)
        self.assertEqual(adapter.video_presentation_start, Fraction(41, 8))
        self.assertEqual(adapter.audio_presentation_start, Fraction(41, 8))
        self.assertEqual(adapter.prepare()["total_frames"], 143)
        self.assertEqual(adapter.encode_audio()["presentation_start"], 0)

    def test_missing_decoded_start_fails_closed_without_encoding(self):
        media = Media(
            EncodeSpec(self.aligned_audio),
            1,
            Cache(self.root / "empty-decoded-start"),
            Deadline(60),
        )
        missing = subprocess.CompletedProcess([], 0, b'{"frames": []}', b"")
        with patch.object(media.deadline, "run", return_value=missing) as processes:
            with self.assertRaisesRegex(
                ValueError, "explicit decoded video/audio presentation starts"
            ):
                SegmentedMedia(media)
        self.assertEqual(processes.call_count, 1)
        self.assertFalse(list(media.cache.root.glob("*.mp4")))
        # A valid timestamp on a later frame must never substitute for an
        # unknown first presentation timestamp, on either video or audio.
        for selector in ("video", "audio"):
            with self.subTest(selector=selector):
                unknown_first = subprocess.CompletedProcess(
                    [],
                    0,
                    b'{"frames": [{"nb_samples": 1024}, {"pts": 0, "nb_samples": 1024}]}',
                    b"",
                )
                video_ok = subprocess.CompletedProcess([], 0, b'{"frames": [{"pts": 0}]}', b"")
                responses = [unknown_first] if selector == "video" else [video_ok, unknown_first]
                with patch.object(media.deadline, "run", side_effect=responses):
                    with self.assertRaisesRegex(
                        ValueError, "explicit decoded video/audio presentation starts"
                    ):
                        SegmentedMedia(media)

    @unittest.skipUnless(hasattr(os, "memfd_create"), "Linux sealed assets required")
    def test_prepared_asset_uses_immutable_fd_after_original_changes(self):
        source = self.root / "mutable-prepared.mp4"
        shutil.copyfile(self.source, source)
        asset = PreparedAsset.from_path(source)
        try:
            source.write_bytes(b"original upload path has been replaced")
            media = Media(
                EncodeSpec(source, preset="veryfast"),
                1,
                Cache(self.root / "prepared-cache"),
                Deadline(60),
                asset=asset,
            )
            adapter = SegmentedMedia(media, 0.7)
            self.assertEqual(adapter.prepare()["total_frames"], 143)
            record = adapter.encode_segment(0, 0)
            self.assertEqual(
                self.raw(record["artifact"]), self.raw(self.source)[: 21 * 160 * 96 * 3 // 2]
            )
            with patch("vsize.media.digest", wraps=digest) as source_digest:
                adapter.verify_source()
            self.assertEqual(source_digest.call_count, 0)
            self.assertIn("sealed prepared asset", record["source_identity_check"])
        finally:
            asset.close()

    def test_high_precision_and_unknown_formats_are_rejected_without_depth_metadata(self):
        media = Media(
            EncodeSpec(self.source), 1, Cache(self.root / "format-admission"), Deadline(60)
        )
        for pixel_format in (
            "gray16le",
            "rgb48le",
            "rgba64le",
            "yuv420p10le",
            "gbrpf32le",
            "unknown8",
            "N/A",
            None,
        ):
            for depth in (None, "N/A", "0", "16"):
                with self.subTest(pixel_format=pixel_format, depth=depth):
                    media.video["pix_fmt"] = pixel_format
                    media.video["bits_per_raw_sample"] = depth
                    with self.assertRaisesRegex(ValueError, "known 8-bit SDR pixel format"):
                        SegmentedMedia(media)
        # Known formats remain admissible when optional bit-depth metadata is
        # absent, as happens with ordinary H.264/y4m inputs.
        for pixel_format in ("yuv420p", "rgb24", "rgba", "gray", "gbrp"):
            for depth in (None, "N/A", "0", "8"):
                with self.subTest(pixel_format=pixel_format, depth=depth):
                    media.video["pix_fmt"] = pixel_format
                    media.video["bits_per_raw_sample"] = depth
                    self.assertIsInstance(SegmentedMedia(media), SegmentedMedia)
        media.video["bits_per_raw_sample"] = "malformed"
        with self.assertRaisesRegex(ValueError, "invalid bits_per_raw_sample metadata"):
            SegmentedMedia(media)

    def test_real_high_precision_rawvideo_is_rejected_even_without_declared_depth(self):
        for pixel_format in ("gray16le", "rgb48le", "rgba64le"):
            with self.subTest(pixel_format=pixel_format):
                source = self.root / f"{pixel_format}.nut"
                self.run_ffmpeg(
                    [
                        "-f",
                        "lavfi",
                        "-i",
                        "testsrc2=s=32x32:r=24:d=0.1",
                        "-vf",
                        f"format={pixel_format}",
                        "-c:v",
                        "rawvideo",
                        "-pix_fmt",
                        pixel_format,
                        "-threads",
                        "1",
                        "-f",
                        "nut",
                        str(source),
                    ]
                )
                media = Media(
                    EncodeSpec(source), 1, Cache(self.root / f"{pixel_format}-cache"), Deadline(60)
                )
                self.assertEqual(media.video["pix_fmt"], pixel_format)
                media.video["bits_per_raw_sample"] = "N/A"
                with self.assertRaisesRegex(ValueError, "known 8-bit SDR pixel format"):
                    SegmentedMedia(media)

    def test_optional_na_metadata_never_implies_an_audio_alignment(self):
        media = Media(
            EncodeSpec(self.aligned_audio), 1, Cache(self.root / "na-metadata"), Deadline(60)
        )
        media.video["bits_per_raw_sample"] = "N/A"
        media.video["start_pts"] = "N/A"
        media.video["start_time"] = "0.0"
        media.audio["start_pts"] = "N/A"
        media.audio["start_time"] = "0.0"
        self.assertIsInstance(SegmentedMedia(media), SegmentedMedia)
        media.audio["start_time"] = "N/A"
        with self.assertRaisesRegex(ValueError, "explicit video/audio presentation starts"):
            SegmentedMedia(media)
        media.audio["sample_rate"] = "N/A"
        with self.assertRaisesRegex(ValueError, "mono/stereo AAC below 65536 Hz"):
            SegmentedMedia(media)


if __name__ == "__main__":
    unittest.main()
