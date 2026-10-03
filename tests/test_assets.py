import errno
import hashlib
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from vsize.assets import (
    F_ADD_SEALS,
    F_GET_SEALS,
    F_SEAL_GROW,
    F_SEAL_SEAL,
    F_SEAL_SHRINK,
    F_SEAL_WRITE,
    AssetTooLargeError,
    PreparedAsset,
    SourceChangedError,
)

try:
    import fcntl
except ImportError:
    fcntl = None


@unittest.skipUnless(hasattr(os, "memfd_create") and fcntl is not None, "Linux memfd required")
class ImmutablePreparedAssets(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.source = Path(self.temporary.name) / "upload.bin"
        self.content = bytes(range(256)) * 1000
        self.source.write_bytes(self.content)

    def capture_created_fds(self):
        original = os.memfd_create
        descriptors = []

        def capture(*args, **kwargs):
            fd = original(*args, **kwargs)
            descriptors.append(fd)
            return fd

        return descriptors, capture

    def assert_descriptors_closed(self, descriptors):
        self.assertTrue(descriptors)
        for fd in descriptors:
            with self.assertRaises(OSError) as caught:
                os.fstat(fd)
            self.assertEqual(caught.exception.errno, errno.EBADF)

    def test_full_hash_content_and_ingestion_metadata(self):
        with PreparedAsset.from_path(self.source) as asset:
            self.assertEqual(asset.source, self.source.resolve())
            self.assertEqual(asset.source_path, asset.source)
            self.assertEqual(asset.bytes, len(self.content))
            self.assertEqual(asset.size_bytes, asset.bytes)
            self.assertEqual(asset.sha256, hashlib.sha256(self.content).hexdigest())
            self.assertEqual(os.pread(asset.fd, asset.bytes, 0), self.content)
            self.assertEqual(Path(asset.input_path).read_bytes(), self.content)
            self.assertEqual(asset.fileno(), asset.fd)
            self.assertGreaterEqual(asset.ingest_seconds, 0)

    def test_kernel_refuses_writes_growth_shrink_and_further_seals(self):
        with PreparedAsset.from_path(self.source) as asset:
            required = F_SEAL_WRITE | F_SEAL_GROW | F_SEAL_SHRINK | F_SEAL_SEAL
            self.assertEqual(fcntl.fcntl(asset.fd, F_GET_SEALS) & required, required)
            for operation in [
                lambda: os.write(asset.fd, b"changed"),
                lambda: os.ftruncate(asset.fd, asset.bytes + 1),
                lambda: os.ftruncate(asset.fd, asset.bytes - 1),
                lambda: fcntl.fcntl(asset.fd, F_ADD_SEALS, F_SEAL_SEAL),
            ]:
                with self.subTest(operation=operation), self.assertRaises(OSError) as caught:
                    operation()
                self.assertEqual(caught.exception.errno, errno.EPERM)

    def test_original_mutation_and_replacement_do_not_change_snapshot(self):
        with PreparedAsset.from_path(self.source) as asset:
            expected_hash = asset.sha256
            self.source.write_bytes(b"new upload version")
            self.source.unlink()
            self.source.write_bytes(b"replacement inode")
            self.assertEqual(os.pread(asset.fd, asset.bytes, 0), self.content)
            self.assertEqual(asset.sha256, expected_hash)

    def test_close_invalidates_handles_but_preserves_metadata(self):
        asset = PreparedAsset.from_path(self.source)
        fd = asset.fd
        expected_hash = asset.sha256
        asset.close()
        asset.close()
        self.assertTrue(asset.closed)
        self.assertEqual(asset.sha256, expected_hash)
        for accessor in [lambda: asset.fd, lambda: asset.input_path, asset.fileno, asset.__enter__]:
            with self.subTest(accessor=accessor), self.assertRaises(ValueError):
                accessor()
        with self.assertRaises(OSError) as caught:
            os.fstat(fd)
        self.assertEqual(caught.exception.errno, errno.EBADF)

    def test_empty_file_is_a_valid_captured_asset(self):
        self.source.write_bytes(b"")
        with PreparedAsset.from_path(self.source, max_bytes=1) as asset:
            self.assertEqual(asset.bytes, 0)
            self.assertEqual(asset.sha256, hashlib.sha256(b"").hexdigest())

    def test_size_limit_rejects_before_allocating_snapshot(self):
        with patch("vsize.assets.os.memfd_create") as allocate:
            with self.assertRaises(AssetTooLargeError):
                PreparedAsset.from_path(self.source, max_bytes=len(self.content) - 1)
            allocate.assert_not_called()
        for limit in [0, -1, True, 1.5]:
            with self.subTest(limit=limit), self.assertRaises(ValueError):
                PreparedAsset.from_path(self.source, max_bytes=limit)

    def test_growing_upload_is_bounded_during_copy_and_releases_fd(self):
        self.source.write_bytes(b"four")
        original_read = os.read
        changed = False

        def grow(fd, count):
            nonlocal changed
            block = original_read(fd, count)
            if not changed:
                with self.source.open("ab") as stream:
                    stream.write(b"extra")
                changed = True
            return block

        descriptors, capture = self.capture_created_fds()
        with patch("vsize.assets.os.memfd_create", side_effect=capture):
            with patch("vsize.assets.os.read", side_effect=grow):
                with self.assertRaises(AssetTooLargeError):
                    PreparedAsset.from_path(self.source, max_bytes=4)
        self.assert_descriptors_closed(descriptors)

    def test_mutation_during_copy_is_rejected_and_releases_fd(self):
        original_read = os.read
        changed = False

        def mutate(fd, count):
            nonlocal changed
            block = original_read(fd, count)
            if not changed:
                initial_mtime = self.source.stat().st_mtime_ns
                self.source.write_bytes(bytes([1]) * len(self.content))
                os.utime(self.source, ns=(initial_mtime, initial_mtime + 1))
                changed = True
            return block

        descriptors, capture = self.capture_created_fds()
        with patch("vsize.assets.os.memfd_create", side_effect=capture):
            with patch("vsize.assets.os.read", side_effect=mutate):
                with self.assertRaises(SourceChangedError):
                    PreparedAsset.from_path(self.source)
        self.assert_descriptors_closed(descriptors)

    def test_read_failure_releases_snapshot_descriptor(self):
        descriptors, capture = self.capture_created_fds()
        original_open = os.open
        opened_sources = []

        def capture_source(*args, **kwargs):
            fd = original_open(*args, **kwargs)
            opened_sources.append(fd)
            return fd

        with patch("vsize.assets.os.memfd_create", side_effect=capture):
            with patch("vsize.assets.os.open", side_effect=capture_source):
                with patch("vsize.assets.os.read", side_effect=OSError(errno.EIO, "read failed")):
                    with self.assertRaises(OSError):
                        PreparedAsset.from_path(self.source)
        self.assert_descriptors_closed(descriptors)
        self.assert_descriptors_closed(opened_sources)

    def test_sealing_failure_releases_snapshot_descriptor(self):
        descriptors, capture = self.capture_created_fds()
        original_fcntl = fcntl.fcntl

        def reject_seal(fd, operation, *args):
            if operation == F_ADD_SEALS:
                raise OSError(errno.EPERM, "seal failed")
            return original_fcntl(fd, operation, *args)

        with patch("vsize.assets.os.memfd_create", side_effect=capture):
            with patch("vsize.assets.fcntl.fcntl", side_effect=reject_seal):
                with self.assertRaises(OSError):
                    PreparedAsset.from_path(self.source)
        self.assert_descriptors_closed(descriptors)

    def test_partial_writes_copy_every_byte(self):
        original_write = os.write

        def partial_write(fd, data):
            return original_write(fd, data[:4096])

        with patch("vsize.assets.os.write", side_effect=partial_write):
            with PreparedAsset.from_path(self.source) as asset:
                self.assertEqual(os.pread(asset.fd, asset.bytes, 0), self.content)
                self.assertEqual(asset.sha256, hashlib.sha256(self.content).hexdigest())

    def test_nonregular_input_is_rejected(self):
        with self.assertRaises(ValueError):
            PreparedAsset.from_path(self.source.parent)

    def test_manual_construction_cannot_claim_an_unverified_hash(self):
        with self.assertRaises(TypeError):
            PreparedAsset(0, self.source, "invented", 0, 0)

    def test_child_process_reads_snapshot_with_explicit_fd_inheritance(self):
        with PreparedAsset.from_path(self.source) as asset:
            self.assertFalse(os.get_inheritable(asset.fd))
            output = subprocess.check_output(
                [
                    sys.executable,
                    "-c",
                    "import hashlib,sys; print(hashlib.sha256(open(sys.argv[1],'rb').read()).hexdigest())",
                    asset.input_path,
                ],
                pass_fds=(asset.fd,),
                text=True,
                timeout=5,
            )
            self.assertEqual(output.strip(), asset.sha256)

    def test_external_fd_reuse_does_not_close_an_unrelated_file(self):
        asset = PreparedAsset.from_path(self.source)
        previous_fd = asset.fd
        os.close(previous_fd)
        replacement = os.open(self.source, os.O_RDONLY)
        try:
            os.dup2(replacement, previous_fd)
            with self.assertRaises(ValueError):
                asset.fileno()
            asset.close()
            self.assertTrue(asset.closed)
            self.assertEqual(os.pread(previous_fd, len(self.content), 0), self.content)
        finally:
            os.close(previous_fd)
            if replacement != previous_fd:
                os.close(replacement)


if __name__ == "__main__":
    unittest.main()
