import json
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile
import unittest
from dataclasses import replace
from fractions import Fraction

from vsize.fmp4 import (
    Packet,
    Track,
    planned_accounting,
    planned_overhead,
    read_track,
    write_fragmented_mp4,
)
from vsize.runtime import BudgetExhausted, Deadline


def _run(command):
    return subprocess.run(command, check=True, capture_output=True).stdout


def _probe(path):
    return json.loads(
        _run(
            [
                "ffprobe",
                "-v",
                "error",
                "-count_frames",
                "-show_streams",
                "-show_format",
                "-show_packets",
                "-of",
                "json",
                str(path),
            ]
        )
    )


def _frame_hashes(path):
    raw = _run(
        [
            "ffmpeg",
            "-hide_banner",
            "-v",
            "error",
            "-nostdin",
            "-xerror",
            "-threads",
            "1",
            "-i",
            str(path),
            "-map",
            "0:v:0",
            "-an",
            "-f",
            "framemd5",
            "-",
        ]
    ).decode()
    return [
        line.rsplit(",", 1)[1].strip()
        for line in raw.splitlines()
        if line and not line.startswith("#")
    ]


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg required")
class FragmentedMP4(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temporary.name)
        cls.encoded = {}
        for rate, counts in [("24", [24, 3, 1]), ("30000/1001", [30, 2])]:
            for count in counts:
                path = cls.root / f"video-{rate.replace('/', '-')}-{count}.mp4"
                _run(
                    [
                        "ffmpeg",
                        "-hide_banner",
                        "-v",
                        "error",
                        "-nostdin",
                        "-y",
                        "-filter_threads",
                        "1",
                        "-f",
                        "lavfi",
                        "-i",
                        f"testsrc2=s=320x180:r={rate}",
                        "-frames:v",
                        str(count),
                        "-an",
                        "-c:v",
                        "libx264",
                        "-preset",
                        "medium",
                        "-crf",
                        "23",
                        "-threads",
                        "1",
                        "-g",
                        "120",
                        "-x264-params",
                        "open-gop=0",
                        "-pix_fmt",
                        "yuv420p",
                        "-level:v",
                        "3.0",
                        str(path),
                    ]
                )
                cls.encoded[(rate, count)] = path
        cls.audio = cls.root / "audio.m4a"
        _run(
            [
                "ffmpeg",
                "-hide_banner",
                "-v",
                "error",
                "-nostdin",
                "-y",
                "-filter_threads",
                "1",
                "-f",
                "lavfi",
                "-i",
                "aevalsrc=sin(2*PI*523*t)|sin(2*PI*700*t):s=48000:d=2.125",
                "-vn",
                "-c:a",
                "aac",
                "-b:a",
                "128k",
                "-threads",
                "1",
                str(cls.audio),
            ]
        )

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def assemble(self, rate, counts, *, audio=False):
        tracks_and_packets = [read_track(self.encoded[(rate, count)]) for count in counts]
        track = tracks_and_packets[0][0]
        for other, _ in tracks_and_packets:
            self.assertEqual(other.extradata, track.extradata)
            self.assertEqual(other.time_base, track.time_base)
        segments = [packets for _, packets in tracks_and_packets]
        kwargs = {}
        if audio:
            audio_track, audio_packets = read_track(self.audio, "a:0")
            kwargs = {"audio_track": audio_track, "audio_packets": audio_packets}
        output = self.root / f"out-{rate.replace('/', '-')}-{audio}.mp4"
        accounting = write_fragmented_mp4(output, track, segments, **kwargs)
        self.assertEqual(accounting.total_bytes, output.stat().st_size)
        self.assertEqual(accounting.container_bytes, planned_overhead(track, counts, **kwargs))
        self.assertEqual(accounting.video_samples, sum(counts))
        plan = planned_accounting(
            track, sum(counts), accounting.video_duration_ticks, len(counts), **kwargs
        )
        self.assertEqual(plan.container_bytes, accounting.container_bytes)
        self.assertEqual(plan.audio_payload_bytes, accounting.audio_payload_bytes)
        self.assertEqual(plan.audio_samples, accounting.audio_samples)
        self.assertEqual(plan.video_payload_bytes, 0)
        self.assertEqual(
            accounting.video_payload_bytes,
            sum(len(p.data) for segment in segments for p in segment),
        )
        return output, accounting, track, segments

    def assert_video_integrity(self, rate, counts, output, track):
        info = _probe(output)
        video = next(s for s in info["streams"] if s["codec_type"] == "video")
        self.assertEqual(int(video["nb_read_frames"]), sum(counts))
        self.assertEqual((video["width"], video["height"]), (320, 180))
        duration = Fraction(sum(counts), 1) / Fraction(rate)
        self.assertAlmostEqual(float(video["duration"]), float(duration), delta=0.000002)
        packets = [p for p in info["packets"] if p["stream_index"] == video["index"]]
        pts = sorted(int(p["pts"]) for p in packets)
        tick = track.timescale / Fraction(rate)
        self.assertEqual(pts, [int(n * tick) for n in range(sum(counts))])
        dts = [int(p["dts"]) for p in packets]
        self.assertEqual([b - a for a, b in zip(dts, dts[1:])], [int(tick)] * (sum(counts) - 1))
        expected = []
        for count in counts:
            expected.extend(_frame_hashes(self.encoded[(rate, count)]))
        self.assertEqual(_frame_hashes(output), expected)

    def test_cfr24_b_frames_and_three_frame_tail(self):
        output, _, track, segments = self.assemble("24", [24, 24, 3])
        self.assertLess(segments[0][0].dts, 0)
        self.assert_video_integrity("24", [24, 24, 3], output, track)

    def test_fractional_cfr_and_two_frame_tail(self):
        output, _, track, _ = self.assemble("30000/1001", [30, 30, 2])
        self.assert_video_integrity("30000/1001", [30, 30, 2], output, track)

    def test_single_frame_tail_preserves_presentation_with_different_reordering(self):
        track, segment = read_track(self.encoded[("24", 24)])
        tail_track, tail = read_track(self.encoded[("24", 1)])
        self.assertEqual(track.extradata, tail_track.extradata)
        output = self.root / "one-frame-tail.mp4"
        accounting = write_fragmented_mp4(output, track, [segment, tail])
        self.assertEqual(accounting.total_bytes, output.stat().st_size)
        self.assert_video_integrity("24", [24, 1], output, track)

    def test_aac_priming_and_exact_decoded_audio_are_preserved(self):
        output, accounting, track, _ = self.assemble("24", [24, 24, 3], audio=True)
        self.assert_video_integrity("24", [24, 24, 3], output, track)
        audio_track, packets = read_track(self.audio, "a:0")
        self.assertEqual(packets[0].skip_samples, 1024)
        self.assertEqual(accounting.audio_priming_samples, 1024)
        self.assertEqual(accounting.audio_duration_ticks, 102000)
        self.assertEqual(accounting.audio_samples, len(packets))
        self.assertEqual(accounting.audio_payload_bytes, sum(len(p.data) for p in packets))

        def pcm(path):
            return _run(
                [
                    "ffmpeg",
                    "-v",
                    "error",
                    "-nostdin",
                    "-xerror",
                    "-threads",
                    "1",
                    "-i",
                    str(path),
                    "-map",
                    "0:a:0",
                    "-f",
                    "s16le",
                    "-",
                ]
            )

        self.assertEqual(pcm(output), pcm(self.audio))
        info = _probe(output)
        audio = next(s for s in info["streams"] if s["codec_type"] == "audio")
        self.assertEqual(audio["channels"], 2)
        self.assertEqual(audio["start_time"], "0.000000")
        # FFmpeg's fMP4 audio duration includes the one-time priming interval.
        # This is bounded; it does not accumulate at each video boundary.
        intended = accounting.audio_duration_ticks / audio_track.timescale
        self.assertLessEqual(abs(float(audio["duration"]) - intended), 1024 / 48000 + 0.000002)

    def test_empty_audio_trafs_do_not_add_audio_duration(self):
        track, segment = read_track(self.encoded[("24", 24)])
        audio_track, audio_packets = read_track(self.audio, "a:0")
        target = self.root / "audio-shorter-than-video.mp4"
        accounting = write_fragmented_mp4(
            target, track, [segment] * 5, audio_track=audio_track, audio_packets=audio_packets
        )
        self.assertEqual(accounting.total_bytes, target.stat().st_size)
        info = _probe(target)
        self.assertEqual(info["format"]["duration"], "5.000000")
        self.assertEqual(int(info["streams"][0]["nb_read_frames"]), 120)
        self.assertAlmostEqual(
            float(info["streams"][1]["duration"]), 2.125 + 1024 / 48000, delta=0.000002
        )

    def test_invalid_timeline_never_publishes_file(self):
        track, packets = read_track(self.encoded[("24", 24)])
        target = self.root / "invalid.mp4"
        packets[4] = replace(packets[4], dts=packets[4].dts + 1)
        with self.assertRaisesRegex(ValueError, "contiguous"):
            write_fragmented_mp4(target, track, [packets])
        self.assertFalse(target.exists())

    def test_existing_output_is_preserved_and_deadline_removes_partial(self):
        track, packets = read_track(self.encoded[("24", 24)])
        target = self.root / "existing.mp4"
        target.write_bytes(b"keep me")
        with self.assertRaises(FileExistsError):
            write_fragmented_mp4(target, track, [packets])
        self.assertEqual(target.read_bytes(), b"keep me")
        expired = Deadline(0)
        with self.assertRaises(BudgetExhausted):
            read_track(self.encoded[("24", 24)], deadline=expired)
        with self.assertRaises(BudgetExhausted):
            write_fragmented_mp4(self.root / "expired.mp4", track, [packets], deadline=expired)
        self.assertFalse((self.root / "expired.mp4").exists())

    def test_rejects_unrepresented_aac_priming_and_midstream_skip(self):
        track, video = read_track(self.encoded[("24", 24)])
        audio_track, packets = read_track(self.audio, "a:0")
        with self.assertRaisesRegex(ValueError, "priming"):
            write_fragmented_mp4(
                self.root / "invalid-priming.mp4",
                track,
                [video],
                audio_track=replace(audio_track, presentation_start=-1024),
                audio_packets=packets,
            )
        packets[1] = replace(packets[1], skip_samples=1)
        with self.assertRaisesRegex(ValueError, "first packet"):
            write_fragmented_mp4(
                self.root / "midstream-skip.mp4",
                track,
                [video],
                audio_track=audio_track,
                audio_packets=packets,
            )

    def test_keyframe_flag_without_idr_is_rejected(self):
        track, packets = read_track(self.encoded[("24", 24)])
        non_idr = b"\x00\x00\x00\x01\x61"
        packets[0] = replace(packets[0], data=non_idr, keyframe=True)
        with self.assertRaisesRegex(ValueError, "IDR"):
            write_fragmented_mp4(self.root / "open-gop.mp4", track, [packets])


class ContainerLayout(unittest.TestCase):
    def test_structural_overhead_does_not_depend_on_payload_size(self):
        # A syntactically valid AVC config is enough to inspect fixed box sizes.
        track = Track("h264", Fraction(1, 24000), b"\x01\x64\0\x1f\xff\xe0\0", 320, 180)
        short = Packet(b"\x00\x00\x00\x01\x65", 1000, 0, 0, True)
        long = replace(short, data=struct.pack(">I", 896) + b"\x65" + b"x" * 895)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            a = write_fragmented_mp4(root / "short.mp4", track, [[short], [short]])
            b = write_fragmented_mp4(root / "long.mp4", track, [[long], [long]])
            self.assertEqual(a.container_bytes, b.container_bytes)
            self.assertEqual(b.total_bytes - a.total_bytes, 2 * (900 - 5))
            raw = (root / "short.mp4").read_bytes()
            boxes = []
            pos = 0
            while pos < len(raw):
                size, name = struct.unpack_from(">I4s", raw, pos)
                boxes.append(name)
                pos += size
            self.assertEqual(pos, len(raw))
            self.assertEqual(boxes, [b"ftyp", b"moov", b"moof", b"mdat", b"moof", b"mdat"])


if __name__ == "__main__":
    unittest.main()
