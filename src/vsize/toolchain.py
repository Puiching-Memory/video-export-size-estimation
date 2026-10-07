"""Fingerprint the native programs and libraries actually loaded on Linux.

FFmpeg's version banner omits dynamically linked x264 revisions. A zero-frame
raw-input process waits on stdin while its actual mappings are inspected. No
source video or encoder probe is needed; all startup and hashing costs belong
to the caller's shared deadline.
"""

import hashlib
import os
import select
import signal
import subprocess
from pathlib import Path

from .runtime import BudgetExhausted


def _wait_for_banner(process, program, deadline):
    captured = bytearray()
    marker = f"{program} version ".encode()
    while marker not in captured:
        deadline.check()
        ready, _, _ = select.select([process.stderr], [], [], deadline.remaining)
        if not ready:
            raise BudgetExhausted("native toolchain inspection exceeded wall-clock budget")
        block = os.read(process.stderr.fileno(), 65536)
        if not block:
            raise RuntimeError(f"{program} exited before native toolchain inspection")
        captured.extend(block)
        if len(captured) > 1024 * 1024:
            raise RuntimeError(f"unexpected {program} startup output")
    return bytes(captured)


def _mapped_files(process):
    entries = {}
    for line in Path(f"/proc/{process.pid}/maps").read_text().splitlines():
        fields = line.split(maxsplit=5)
        if len(fields) != 6 or "x" not in fields[1] or fields[4] == "0":
            continue
        path = fields[5]
        if not path.startswith("/"):
            continue
        if path.endswith(" (deleted)"):
            raise RuntimeError("a loaded native toolchain file was removed during inspection")
        device = tuple(int(part, 16) for part in fields[3].split(":"))
        identity = (*device, int(fields[4]))
        previous = entries.setdefault(path, identity)
        if previous != identity:
            raise RuntimeError("native toolchain mappings changed during inspection")
    executable = Path(f"/proc/{process.pid}/exe").resolve(strict=True)
    if str(executable) not in entries:
        raise RuntimeError("native executable is missing from its process mappings")
    return entries


def _identity(stat):
    return os.major(stat.st_dev), os.minor(stat.st_dev), stat.st_ino


def _signature(stat):
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns


def native_pipeline_fingerprint(deadline):
    """Return content identities for FFmpeg, FFprobe and their mapped code.

    Paths, mtimes and ELF Build-IDs alone do not identify bytes. They only guard
    against changes while each actual mapped file is fully hashed. A relocated
    copy with the same component names and bytes has the same fingerprint.
    """
    processes = []
    try:
        for program in ("ffmpeg", "ffprobe"):
            deadline.check()
            command = [
                program,
                "-loglevel",
                "info",
                "-threads",
                "1",
                "-f",
                "rawvideo",
                "-pixel_format",
                "yuv420p",
                "-video_size",
                "2x2",
                "-framerate",
                "1",
                "-i",
                "pipe:0",
            ]
            if program == "ffmpeg":
                command += [
                    "-nostdin",
                    "-filter_threads",
                    "1",
                    "-threads",
                    "1",
                    "-an",
                    "-f",
                    "null",
                    "-",
                ]
            else:
                command += ["-show_streams", "-of", "json"]
            processes.append(
                (
                    program,
                    subprocess.Popen(
                        command,
                        stdin=subprocess.PIPE,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        start_new_session=True,
                    ),
                )
            )
        mappings = {}
        for program, process in processes:
            _wait_for_banner(process, program, deadline)
            for path, identity in _mapped_files(process).items():
                previous = mappings.setdefault(path, identity)
                if previous != identity:
                    raise RuntimeError("FFmpeg and FFprobe loaded different native file versions")
        components = set()
        for path, expected in sorted(mappings.items()):
            deadline.check()
            value = hashlib.sha256()
            with Path(path).open("rb") as stream:
                before = os.fstat(stream.fileno())
                if _identity(before) != expected:
                    raise RuntimeError("loaded native toolchain identity changed before hashing")
                while block := stream.read(1024 * 1024):
                    deadline.check()
                    value.update(block)
                after = os.fstat(stream.fileno())
            if _signature(before) != _signature(after) or _signature(after) != _signature(
                Path(path).stat()
            ):
                raise RuntimeError("native toolchain bytes changed during hashing")
            components.add((Path(path).name, after.st_size, value.hexdigest()))
        for program, process in processes:
            deadline.check()
            _, error = process.communicate(input=b"", timeout=deadline.remaining)
            if process.returncode:
                raise RuntimeError(
                    f"{program} zero-frame inspection failed: {error.decode()[-1000:]}"
                )
        deadline.check()
        return {
            "version": "linux-loaded-native-sha256-v1",
            "components": [
                {"name": name, "bytes": size, "sha256": digest}
                for name, size, digest in sorted(components)
            ],
        }
    except subprocess.TimeoutExpired:
        raise BudgetExhausted("native toolchain inspection exceeded wall-clock budget") from None
    finally:
        for _, process in processes:
            if process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            process.communicate()
