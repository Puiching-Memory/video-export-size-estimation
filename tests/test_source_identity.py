"""Real FFmpeg change/restore races and sealed-source isolation."""

import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from vsize.assets import PreparedAsset
from vsize.contracts import ComputeBudget, EncodeSpec, Request
from vsize.engine import Engine
from vsize.media import Media
from vsize.runtime import Cache, Deadline


@unittest.skipUnless(
    sys.platform.startswith("linux") and shutil.which("ffmpeg") and shutil.which("ffprobe"),
    "Linux and FFmpeg/FFprobe required",
)
class SourceIdentityIntegration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        directory = tempfile.TemporaryDirectory()
        cls.addClassCleanup(directory.cleanup)
        cls.root = Path(directory.name)
        cls.original = cls.root / "original.mp4"
        cls.alternate = cls.root / "alternate.mp4"
        for path, content in [
            (cls.original, "testsrc2=size=64x48:rate=8:duration=1"),
            (cls.alternate, "color=red:size=64x48:rate=8:duration=1"),
        ]:
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
                    content,
                    "-an",
                    "-c:v",
                    "libx264",
                    "-preset",
                    "ultrafast",
                    "-threads",
                    "1",
                    "-pix_fmt",
                    "yuv420p",
                    str(path),
                ],
                check=True,
                capture_output=True,
            )
        cls.original_bytes = cls.original.read_bytes()
        cls.alternate_bytes = cls.alternate.read_bytes()
        cls.reference_cache = cls.root / "reference-cache"
        cls.reference = Engine(cls.reference_cache).run(
            Request(
                EncodeSpec(cls.original),
                compute=ComputeBudget(wall_seconds=30, max_probes=0, threads=1),
                probe_mode="bare",
            )
        )
        if cls.reference["status"] != "satisfied":
            raise AssertionError(f"tiny real baseline export failed: {cls.reference}")

    def setUp(self):
        self.work = self.root / self._testMethodName
        self.work.mkdir()
        self.source = self.work / "source.mp4"
        self.source.write_bytes(self.original_bytes)
        self.cache = self.work / "cache"
        self.request = Request(
            EncodeSpec(self.source),
            compute=ComputeBudget(wall_seconds=30, max_probes=0, threads=1),
            probe_mode="bare",
        )

    def timed_mutation(self):
        run = Deadline.run
        attempts = []

        def change_restore(deadline, command):
            if command[0] == "ffmpeg" and "libx264" in command and "-crf" in command:
                before = self.source.stat()
                self.source.write_bytes(self.alternate_bytes)
                attempts.append(command)
                try:
                    return run(deadline, command)
                finally:
                    self.source.write_bytes(self.original_bytes)
                    # Restore the visible mtime too. The original content
                    # digest and length match again; ctime still detects ABA.
                    os.utime(self.source, ns=(before.st_atime_ns, before.st_mtime_ns))
            return run(deadline, command)

        return attempts, change_restore

    def test_raw_change_restore_during_real_encode_is_rejected_without_publication(self):
        before = self.source.stat()
        attempts, change_restore = self.timed_mutation()
        with patch.object(Deadline, "run", new=change_restore):
            with self.assertRaisesRegex(RuntimeError, "input changed during analysis"):
                Engine(self.cache).run(self.request)
        self.assertEqual(len(attempts), 1)
        after = self.source.stat()
        self.assertEqual(after.st_ino, before.st_ino)
        self.assertEqual(after.st_size, before.st_size)
        self.assertEqual(after.st_mtime_ns, before.st_mtime_ns)
        self.assertNotEqual(after.st_ctime_ns, before.st_ctime_ns)
        self.assertEqual(self.source.read_bytes(), self.original_bytes)
        self.assertEqual(list(self.cache.glob("*.mp4")), [])
        self.assertEqual(list(self.cache.glob("*.json")), [])
        self.assertEqual(list(self.cache.glob("partial-*")), [])

    @unittest.skipUnless(hasattr(os, "memfd_create"), "Linux sealed memfd required")
    def test_prepared_asset_real_encode_uses_A_despite_original_A_B_A_mutation(self):
        attempts, change_restore = self.timed_mutation()
        with PreparedAsset.from_path(self.source) as asset:
            with patch.object(Deadline, "run", new=change_restore):
                result = Engine(self.cache).run(self.request, asset=asset)
            self.assertEqual(result["source_hash"], asset.sha256)
        self.assertEqual(len(attempts), 1)
        self.assertEqual(result["status"], "satisfied", result)
        self.assertEqual(result["estimate"]["uncertainty"]["kind"], "exact")
        self.assertFalse(result["estimate"]["cache_hit"])
        self.assertEqual(result["estimate"]["sha256"], self.reference["estimate"]["sha256"])
        self.assertEqual(
            Path(result["estimate"]["artifact"]).read_bytes(),
            Path(self.reference["estimate"]["artifact"]).read_bytes(),
        )

    def test_cached_lookup_rejects_same_content_change_restore(self):
        media = Media(EncodeSpec(self.source), 1, Cache(self.reference_cache), Deadline(30))
        self.assertIsNotNone(media.lookup(media.spec.crf))
        before = self.source.stat()
        self.source.write_bytes(self.alternate_bytes)
        self.source.write_bytes(self.original_bytes)
        os.utime(self.source, ns=(before.st_atime_ns, before.st_mtime_ns))
        self.assertEqual(hashlib.sha256(self.source.read_bytes()).hexdigest(), media.source_hash)
        with self.assertRaisesRegex(RuntimeError, "input changed during analysis"):
            media.lookup(media.spec.crf)
        with self.assertRaisesRegex(RuntimeError, "input changed during analysis"):
            media.verify_source()


if __name__ == "__main__":
    unittest.main()
