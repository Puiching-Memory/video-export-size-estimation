import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from benchmarks.corpus import (
    CATALOG,
    canonical_y4m,
    check_bytes,
    check_video,
    fetch_one,
    load_catalog,
    media_path,
    write_json,
)


class Response(io.BytesIO):
    def __init__(self, payload, status=200, headers=None):
        super().__init__(payload)
        self.status = status
        self.headers = headers or {"ETag": '"unchanged-object"'}


class CorpusIntegrity(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.payload = b"an immutable public video object"
        self.asset = {
            "id": "sample",
            "filename": "sample.mp4",
            "url": "https://example.org/sample.mp4",
            "expected_bytes": len(self.payload),
            "publisher_md5": hashlib.md5(self.payload, usedforsecurity=False).hexdigest(),
        }

    def partial(self):
        target = media_path(self.root, self.asset)
        target.parent.mkdir(parents=True)
        target.with_suffix(".mp4.part").write_bytes(self.payload[:7])
        write_json(
            target.with_suffix(".mp4.http.json"),
            {
                "url": self.asset["url"],
                "etag": '"unchanged-object"',
            },
        )
        return target

    def test_resumes_only_the_requested_range_and_verifies_complete_bytes(self):
        target = self.partial()
        response = Response(
            self.payload[7:],
            206,
            {
                "Content-Range": f"bytes 7-{len(self.payload) - 1}/{len(self.payload)}",
                "ETag": '"unchanged-object"',
            },
        )
        with patch("urllib.request.urlopen", return_value=response) as opened:
            result = fetch_one(self.root, self.asset)
        self.assertEqual(opened.call_args.args[0].get_header("Range"), "bytes=7-")
        self.assertEqual(target.read_bytes(), self.payload)
        self.assertEqual(result["sha256"], hashlib.sha256(self.payload).hexdigest())

    def test_server_ignoring_range_restarts_instead_of_appending(self):
        target = self.partial()
        with patch("urllib.request.urlopen", return_value=Response(self.payload)):
            fetch_one(self.root, self.asset)
        self.assertEqual(target.read_bytes(), self.payload)

    def test_wrong_resume_offset_is_not_published(self):
        target = self.partial()
        response = Response(self.payload, 206, {"Content-Range": f"bytes 0-9/{len(self.payload)}"})
        with patch("urllib.request.urlopen", return_value=response):
            with self.assertRaisesRegex(ValueError, "invalid resume"):
                fetch_one(self.root, self.asset)
        self.assertFalse(target.exists())
        self.assertEqual(target.with_suffix(".mp4.part").read_bytes(), self.payload[:7])

    def test_truncation_never_becomes_a_video_or_lock(self):
        with patch(
            "urllib.request.urlopen", side_effect=lambda *a, **k: Response(self.payload[:7])
        ):
            with patch("time.sleep"):
                with self.assertRaises(OSError):
                    fetch_one(self.root, self.asset)
        self.assertFalse(media_path(self.root, self.asset).exists())

    def test_same_size_corruption_is_rejected_without_overwriting(self):
        target = self.partial()
        corrupted = b"!" + self.payload[1:]
        target.write_bytes(corrupted)
        with self.assertRaisesRegex(ValueError, "publisher MD5"):
            fetch_one(self.root, self.asset)
        self.assertEqual(target.read_bytes(), corrupted)

    def test_sha_lock_catches_changed_object_without_publisher_checksum(self):
        self.asset["publisher_md5"] = None
        target = self.partial()
        target.write_bytes(b"!" + self.payload[1:])
        lock = {"sha256": hashlib.sha256(self.payload).hexdigest()}
        with self.assertRaisesRegex(ValueError, "locked SHA-256"):
            check_bytes(target, self.asset, lock)

    def test_checked_in_catalog_preserves_group_splits_and_notices(self):
        catalog = load_catalog(CATALOG)
        self.assertGreaterEqual(len(catalog["assets"]), 20)
        # A real same-film SDR/HDR pair must share one split and one source group.
        variants = [a for a in catalog["assets"] if a["id"].startswith("meridian-")]
        self.assertEqual(len({a["group_id"] for a in variants}), 1)
        self.assertEqual(len({a["split"] for a in variants}), 1)

    def test_related_sources_cannot_cross_splits(self):
        notice = self.root / "notice.txt"
        notice.write_text("attribution")
        asset = {
            **self.asset,
            "split": "calibration_reserved",
            "group_id": "one-film",
            "license": {
                "notice_file": "notice.txt",
                "notice_sha256": hashlib.sha256(notice.read_bytes()).hexdigest(),
            },
        }
        other = {**asset, "id": "sample-hdr", "split": "test_reserved"}
        path = self.root / "catalog.json"
        path.write_text(json.dumps({"assets": [asset, other]}))
        with self.assertRaisesRegex(ValueError, "crosses splits"):
            load_catalog(path)

    def test_pixel_precision_hdr_and_missing_frames_are_not_silently_accepted(self):
        asset = {"id": "ctc", "signal": "SDR", "expected_video": {"frames": 130}}
        video = {
            "width": 3840,
            "height": 2160,
            "avg_frame_rate": "30000/1001",
            "nb_read_frames": "130",
            "pix_fmt": "yuv420p10le",
        }
        self.assertEqual(check_video(asset, video)["scope"], "precision_or_hdr_boundary")
        self.assertEqual(check_video(asset, {**video, "pix_fmt": "yuv420p"})["scope"], "sdr_8bit")
        self.assertEqual(
            check_video(asset, {**video, "pix_fmt": "yuv420p", "color_transfer": "smpte2084"})[
                "scope"
            ],
            "precision_or_hdr_boundary",
        )
        with self.assertRaisesRegex(ValueError, "frames=129"):
            check_video(asset, {**video, "nb_read_frames": "129"})

    def test_declared_terminal_cleanup_preserves_all_pixel_bytes(self):
        clean = b"YUV4MPEG2 W2 H2 F24:1 Ip C420jpeg\nFRAME\n" + bytes(range(6))
        original = self.root / "tiny.y4m"
        original.write_bytes(clean + b"\n")
        asset = {
            "expected_video": {"width": 2, "height": 2, "bit_depth": 8, "frames": 1},
            "transport_cleanup": {
                "operation": "remove-one-terminal-newline",
                "sha256": hashlib.sha256(clean).hexdigest(),
            },
        }
        canonical = canonical_y4m(original, asset)
        self.assertEqual(canonical.read_bytes(), clean)
        self.assertEqual(original.read_bytes(), clean + b"\n")
        original.write_bytes(clean[:-1] + b"\n")
        with self.assertRaisesRegex(ValueError, "exactly one newline"):
            canonical_y4m(original, asset)


if __name__ == "__main__":
    unittest.main()
