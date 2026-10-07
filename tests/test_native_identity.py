"""Real loaded-library and cancellation regressions for native fingerprints."""

import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from vsize.contracts import ComputeBudget, EncodeSpec, Request
from vsize.engine import Engine
from vsize.runtime import BudgetExhausted, Deadline
from vsize.toolchain import native_pipeline_fingerprint


NATIVE_AVAILABLE = (
    sys.platform.startswith("linux")
    and Path("/proc/self/maps").is_file()
    and shutil.which("ffmpeg")
    and shutil.which("ffprobe")
)


@unittest.skipUnless(NATIVE_AVAILABLE, "Linux /proc and FFmpeg/FFprobe required")
class NativeDeadlineCleanup(unittest.TestCase):
    def test_expiry_after_both_actual_processes_start_reaps_them(self):
        # Observe real Popen calls. Start with room for process creation, then
        # expire the shared budget at the two-live-children cleanup boundary.
        # This avoids making a scheduler-speed assertion on a busy CI machine.
        deadline = Deadline(10)
        processes = []
        popen = subprocess.Popen

        def capture(*args, **kwargs):
            process = popen(*args, **kwargs)
            processes.append(process)
            if len(processes) == 2:
                deadline.ends = time.monotonic() + 0.000001
            return process

        with patch("vsize.toolchain.subprocess.Popen", new=capture):
            with self.assertRaises(BudgetExhausted):
                native_pipeline_fingerprint(deadline)
        self.assertEqual(len(processes), 2)
        for process in processes:
            self.assertIsNotNone(process.returncode)
            with self.assertRaises(ChildProcessError):
                os.waitpid(process.pid, os.WNOHANG)


@unittest.skipUnless(NATIVE_AVAILABLE, "Linux /proc and FFmpeg/FFprobe required")
class DynamicX264Identity(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not shutil.which("ldd"):
            raise unittest.SkipTest("dynamic-library discovery requires ldd")
        linked = subprocess.run(
            ["ldd", shutil.which("ffmpeg")], capture_output=True, text=True, check=False
        )
        match = re.search(r"(libx264\.so[^\s]*)\s*=>\s*(/\S+)", linked.stdout)
        if linked.returncode or match is None:
            raise unittest.SkipTest("FFmpeg has no discoverable dynamic libx264")
        cls.soname = match[1]
        cls.library = Path(match[2]).resolve()
        cls.library_bytes = cls.library.read_bytes()
        revision = re.search(rb" r[0-9]+ [0-9a-f]{7,40}\x00", cls.library_bytes)
        if revision is None or cls.library_bytes.count(revision[0]) != 1:
            raise unittest.SkipTest("dynamic x264 has no unique embedded revision marker")
        cls.original_revision = revision[0]
        parts = cls.original_revision[:-1].split()
        digits = b"9" * (len(parts[0]) - 1)
        if digits == parts[0][1:]:
            digits = b"8" * len(digits)
        commit = b"0" * len(parts[1])
        if commit == parts[1]:
            commit = b"1" * len(commit)
        cls.changed_revision = b" r" + digits + b" " + commit + b"\x00"
        assert len(cls.original_revision) == len(cls.changed_revision)
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
                "testsrc2=size=64x48:rate=8:duration=1",
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
            capture_output=True,
        )
        cls.request = Request(
            EncodeSpec(cls.source),
            compute=ComputeBudget(wall_seconds=30, max_probes=0, threads=1),
            probe_mode="bare",
        )
        cls.cache = cls.root / "cache"
        cls.baseline = Engine(cls.cache).run(cls.request)
        if cls.baseline["status"] != "satisfied":
            raise AssertionError(f"tiny real baseline export failed: {cls.baseline}")
        cls.version = subprocess.check_output(["ffmpeg", "-version"])

    def copied_library(self, name, contents):
        directory = self.root / name
        directory.mkdir()
        target = directory / self.library.name
        target.write_bytes(contents)
        if target.name != self.soname:
            (directory / self.soname).symlink_to(target.name)
        previous = os.environ.get("LD_LIBRARY_PATH", "")
        return str(directory) + (os.pathsep + previous if previous else "")

    def test_same_library_bytes_relocated_preserve_key_and_cache_hit(self):
        directory = self.copied_library("same-library", self.library_bytes)
        with patch.dict(os.environ, {"LD_LIBRARY_PATH": directory}):
            version = subprocess.check_output(["ffmpeg", "-version"])
            result = Engine(self.cache).run(self.request)
        self.assertEqual(version, self.version)
        self.assertEqual(result["status"], "satisfied", result)
        self.assertEqual(result["toolchain_key"], self.baseline["toolchain_key"])
        self.assertTrue(result["estimate"]["cache_hit"])
        self.assertEqual(result["estimate"]["sha256"], self.baseline["estimate"]["sha256"])
        self.assertEqual(result["attempted_encode_seconds"], 0)

    def test_same_abi_different_revision_invalidates_real_export_cache(self):
        # Alter only equal-length build text in a private copied DSO. ABI and
        # encoder instructions stay intact; the actual output SEI changes.
        contents = self.library_bytes.replace(self.original_revision, self.changed_revision)
        directory = self.copied_library("changed-library", contents)
        with patch.dict(os.environ, {"LD_LIBRARY_PATH": directory}):
            version = subprocess.check_output(["ffmpeg", "-version"])
            result = Engine(self.cache).run(self.request)
        self.assertEqual(version, self.version)
        self.assertEqual(result["status"], "satisfied", result)
        self.assertNotEqual(result["toolchain_key"], self.baseline["toolchain_key"])
        self.assertFalse(result["estimate"]["cache_hit"])
        self.assertNotEqual(result["estimate"]["artifact"], self.baseline["estimate"]["artifact"])
        self.assertNotEqual(result["estimate"]["sha256"], self.baseline["estimate"]["sha256"])
        self.assertGreater(result["attempted_encode_seconds"], 0)
        baseline = Path(self.baseline["estimate"]["artifact"]).read_bytes()
        changed = Path(result["estimate"]["artifact"]).read_bytes()
        self.assertIn(self.original_revision[:-1], baseline)
        self.assertIn(self.changed_revision[:-1], changed)


if __name__ == "__main__":
    unittest.main()
