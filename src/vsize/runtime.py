"""A shared monotonic deadline, subprocess cleanup, and immutable content cache."""

import hashlib
import json
import os
import signal
import subprocess
import tempfile
import time
from pathlib import Path


class BudgetExhausted(Exception):
    pass


class Deadline:
    def __init__(self, seconds: float):
        self.started = time.monotonic()
        self.ends = self.started + seconds

    @property
    def remaining(self):
        return max(0.0, self.ends - time.monotonic())

    @property
    def elapsed(self):
        return time.monotonic() - self.started

    def check(self):
        if self.remaining <= 0:
            raise BudgetExhausted("wall-clock budget exhausted")

    def run(self, command: list[str]) -> subprocess.CompletedProcess:
        self.check()
        process = subprocess.Popen(
            command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True
        )
        try:
            out, err = process.communicate(timeout=self.remaining)
        except (subprocess.TimeoutExpired, KeyboardInterrupt):
            # FFmpeg may start helpers; terminate the complete process group.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.communicate()
            if self.remaining <= 0:
                raise BudgetExhausted("subprocess exceeded wall-clock budget") from None
            raise
        if process.returncode:
            raise RuntimeError(err.decode(errors="replace")[-2000:])
        self.check()
        return subprocess.CompletedProcess(command, process.returncode, out, err)


def digest(path: Path, deadline: Deadline | None = None):
    value = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            if deadline:
                deadline.check()
            value.update(block)
    return value.hexdigest()


def canonical_key(payload: dict):
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


class Cache:
    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def lookup(self, key: str, deadline: Deadline):
        metadata = self.root / f"{key}.json"
        artifact = self.root / f"{key}.mp4"
        try:
            record = json.loads(metadata.read_text())
            if artifact.stat().st_size != record["file_bytes"]:
                return None
            if digest(artifact, deadline) != record["sha256"]:
                return None
            return {**record, "artifact": str(artifact), "cache_hit": True}
        except (OSError, ValueError, KeyError):
            return None

    def temporary(self):
        descriptor, path = tempfile.mkstemp(prefix="partial-", suffix=".mp4", dir=self.root)
        os.close(descriptor)
        return Path(path)

    def publish(self, key: str, temporary: Path, record: dict, deadline: Deadline):
        record = {
            **record,
            "file_bytes": temporary.stat().st_size,
            "sha256": digest(temporary, deadline),
        }
        artifact = self.root / f"{key}.mp4"
        metadata = self.root / f"{key}.json"
        descriptor, meta_path = tempfile.mkstemp(prefix="partial-", suffix=".json", dir=self.root)
        try:
            with os.fdopen(descriptor, "w") as stream:
                json.dump(record, stream, sort_keys=True)
                stream.flush()
                os.fsync(stream.fileno())
            with temporary.open("rb") as stream:
                os.fsync(stream.fileno())
            deadline.check()
            os.replace(temporary, artifact)
            os.replace(meta_path, metadata)
        finally:
            Path(meta_path).unlink(missing_ok=True)
        return {**record, "artifact": str(artifact), "cache_hit": False}
