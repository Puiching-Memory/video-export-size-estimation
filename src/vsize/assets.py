"""Explicit, one-time ingestion into Linux sealed, immutable memory files.

Preparation reads and hashes every captured byte. Its cost is reported separately
from prediction. Subsequent users read the sealed snapshot, never a stat-based
cache of the original file. Callers must keep the asset alive and pass its file
descriptor to child processes that open ``input_path``.
"""

import errno
import hashlib
import os
import stat
import sys
import time
from pathlib import Path

try:
    import fcntl
except ImportError:  # Fail at preparation, rather than at module import.
    fcntl = None


DEFAULT_MAX_BYTES = 2 * 1024**3
_COPY_CHUNK_BYTES = 1024**2
_PREPARED_TOKEN = object()

# Linux UAPI values are stable even when Python was compiled with libc headers
# that do not expose the sealing constants. Actual kernel sealing is mandatory.
F_ADD_SEALS = getattr(fcntl, "F_ADD_SEALS", 1033)
F_GET_SEALS = getattr(fcntl, "F_GET_SEALS", 1034)
F_SEAL_SEAL = getattr(fcntl, "F_SEAL_SEAL", 0x0001)
F_SEAL_SHRINK = getattr(fcntl, "F_SEAL_SHRINK", 0x0002)
F_SEAL_GROW = getattr(fcntl, "F_SEAL_GROW", 0x0004)
F_SEAL_WRITE = getattr(fcntl, "F_SEAL_WRITE", 0x0008)


class AssetTooLargeError(ValueError):
    """The original or captured upload exceeds the configured ingestion limit."""


class SourceChangedError(RuntimeError):
    """Ordinary source mutation was detected while the upload was being copied."""


def _fingerprint(info: os.stat_result) -> tuple[int, ...]:
    # atime may change because of our own reads. It is deliberately excluded.
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _required_seals() -> int:
    if (
        fcntl is None
        or not sys.platform.startswith("linux")
        or not hasattr(os, "memfd_create")
        or not hasattr(os, "MFD_ALLOW_SEALING")
    ):
        raise NotImplementedError("PreparedAsset requires Linux memfd sealing support")
    return F_SEAL_WRITE | F_SEAL_GROW | F_SEAL_SHRINK | F_SEAL_SEAL


class PreparedAsset:
    """An owned immutable upload snapshot; close it after all readers complete.

    ``from_path`` hashes the bytes actually copied and rejects changes detected
    by before/after source metadata. That metadata check detects ordinary upload
    races; it is not a cross-request integrity cache or proof against an adversary
    changing a source invisibly during capture. The resulting captured bytes are
    fully hashed and kernel-sealed regardless of the original file's later life.

    Metadata stays readable after close. ``fd``, ``fileno`` and ``input_path``
    become unusable. Prediction must use the snapshot path and inherit ``fd``
    through subprocess ``pass_fds``; preparation is outside prediction budgets.
    """

    def __init__(
        self,
        fd: int,
        source: Path,
        sha256: str,
        size_bytes: int,
        ingest_seconds: float,
        *,
        _token=None,
    ):
        if _token is not _PREPARED_TOKEN:
            raise TypeError("create prepared assets with PreparedAsset.from_path")
        seals = _required_seals()
        if fcntl.fcntl(fd, F_GET_SEALS) & seals != seals:
            raise ValueError("prepared descriptor is not fully sealed")
        info = os.fstat(fd)
        if info.st_size != size_bytes:
            raise ValueError("prepared descriptor size does not match metadata")
        self._fd = fd
        self._identity = (info.st_dev, info.st_ino, info.st_size)
        self._source = Path(source)
        self._sha256 = sha256
        self._bytes = size_bytes
        self._ingest_seconds = ingest_seconds

    @classmethod
    def from_path(cls, path: Path | str, *, max_bytes: int = DEFAULT_MAX_BYTES):
        if type(max_bytes) is not int or max_bytes <= 0:
            raise ValueError("max_bytes must be a positive integer")
        seals = _required_seals()
        started = time.monotonic()
        source = Path(path).resolve()
        source_fd = None
        snapshot_fd = None
        value = hashlib.sha256()
        captured_bytes = 0
        try:
            # NONBLOCK prevents a concurrently substituted FIFO from blocking
            # before fstat can reject it; regular-file reads are unaffected.
            flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NONBLOCK", 0)
            flags |= getattr(os, "O_NOFOLLOW", 0)
            source_fd = os.open(source, flags)
            before = os.fstat(source_fd)
            if not stat.S_ISREG(before.st_mode):
                raise ValueError(f"input is not a regular file: {source}")
            if before.st_size > max_bytes:
                raise AssetTooLargeError(
                    f"asset is {before.st_size} bytes; ingestion limit is {max_bytes} bytes"
                )
            snapshot_fd = os.memfd_create(
                "vsize-prepared-asset", os.MFD_ALLOW_SEALING | getattr(os, "MFD_CLOEXEC", 0)
            )
            while block := os.read(source_fd, _COPY_CHUNK_BYTES):
                captured_bytes += len(block)
                if captured_bytes > max_bytes:
                    raise AssetTooLargeError(
                        f"captured upload exceeds ingestion limit of {max_bytes} bytes"
                    )
                remaining = memoryview(block)
                while remaining:
                    written = os.write(snapshot_fd, remaining)
                    if written <= 0:
                        raise OSError("snapshot write made no progress")
                    remaining = remaining[written:]
                value.update(block)
            after = os.fstat(source_fd)
            try:
                named_after = source.stat()
            except OSError as error:
                raise SourceChangedError("source disappeared during ingestion") from error
            if (
                _fingerprint(before) != _fingerprint(after)
                or _fingerprint(before) != _fingerprint(named_after)
                or captured_bytes != before.st_size
            ):
                raise SourceChangedError("source changed during ingestion; snapshot discarded")
            os.lseek(snapshot_fd, 0, os.SEEK_SET)
            fcntl.fcntl(snapshot_fd, F_ADD_SEALS, seals)
            if fcntl.fcntl(snapshot_fd, F_GET_SEALS) & seals != seals:
                raise RuntimeError("kernel did not apply all required snapshot seals")
            os.close(source_fd)
            source_fd = None
            asset = cls(
                snapshot_fd,
                source,
                value.hexdigest(),
                captured_bytes,
                time.monotonic() - started,
                _token=_PREPARED_TOKEN,
            )
            snapshot_fd = None  # Ownership transferred only after construction succeeds.
            return asset
        finally:
            if source_fd is not None:
                os.close(source_fd)
            if snapshot_fd is not None:
                os.close(snapshot_fd)

    @property
    def source(self) -> Path:
        return self._source

    @property
    def source_path(self) -> Path:
        return self._source

    @property
    def sha256(self) -> str:
        return self._sha256

    @property
    def bytes(self) -> int:
        return self._bytes

    @property
    def size_bytes(self) -> int:
        return self._bytes

    @property
    def ingest_seconds(self) -> float:
        return self._ingest_seconds

    @property
    def closed(self) -> bool:
        return self._fd is None

    @property
    def fd(self) -> int:
        if self.closed:
            raise ValueError("prepared asset is closed")
        try:
            info = os.fstat(self._fd)
        except OSError as error:
            if error.errno != errno.EBADF:
                raise
            self._fd = None
            raise ValueError("prepared asset descriptor was closed externally") from error
        if (info.st_dev, info.st_ino, info.st_size) != self._identity:
            # Never close an unrelated descriptor that reused the same number.
            self._fd = None
            raise ValueError("prepared asset descriptor no longer refers to its snapshot")
        return self._fd

    def fileno(self) -> int:
        return self.fd

    @property
    def input_path(self) -> str:
        return f"/proc/self/fd/{self.fd}"

    def close(self) -> None:
        if self.closed:
            return
        try:
            fd = self.fd
        except ValueError:
            return
        self._fd = None
        os.close(fd)

    def __enter__(self):
        self.fd  # Validate that this context owns a live snapshot.
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()

    def __del__(self):
        if hasattr(self, "_fd"):
            try:
                self.close()
            except OSError:
                pass
