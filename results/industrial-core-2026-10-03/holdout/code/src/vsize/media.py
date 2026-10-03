"""FFmpeg adapter for the declared main-video/first-audio MP4 export contract."""

import json
import math
import os
import re
import tempfile
import time
from bisect import bisect_right
from dataclasses import replace
from fractions import Fraction
from pathlib import Path

import numpy as np

from .contracts import EncodeSpec
from .assets import PreparedAsset
from .runtime import Cache, Deadline, canonical_key, digest


PIPELINE_VERSION = "x264-mp4-v3-passthrough"


class Media:
    def __init__(
        self, spec: EncodeSpec, threads: int, cache: Cache, deadline: Deadline, asset=None
    ):
        if asset is None and not spec.source.is_file():
            raise ValueError(f"input is not a regular file: {spec.source}")
        self.spec, self.threads, self.cache, self.deadline = spec, threads, cache, deadline
        self.asset = asset
        if asset is not None:
            if not isinstance(asset, PreparedAsset):
                raise ValueError("asset must be a sealed PreparedAsset")
            if asset.source != spec.source:
                raise ValueError("prepared asset does not match the requested source path")
            deadline.bind_fd(asset.fileno())
            self.input_path = asset.input_path
            self.source_hash = asset.sha256
        else:
            self.input_path = str(spec.source)
            self.source_hash = digest(spec.source, deadline)
        self.toolchain = deadline.run(["ffmpeg", "-version"]).stdout.decode()
        self.toolchain_key = canonical_key({"ffmpeg": self.toolchain})
        raw = deadline.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_streams",
                "-show_format",
                "-of",
                "json",
                self.input_path,
            ]
        ).stdout
        self.info = json.loads(raw)
        videos = [
            s
            for s in self.info["streams"]
            if s["codec_type"] == "video"
            and not s.get("disposition", {}).get("attached_pic", False)
        ]
        if not videos:
            raise ValueError("input has no video stream")
        self.video = videos[0]
        audios = [s for s in self.info["streams"] if s["codec_type"] == "audio"]
        self.audio = audios[0] if audios and not spec.drop_audio else None
        self.duration = float(self.video.get("duration") or self.info["format"]["duration"])
        if not math.isfinite(self.duration) or self.duration <= 0:
            raise ValueError("finite positive input duration required")
        rate = self.video.get("avg_frame_rate", "0/1")
        self.fps = float(Fraction(rate)) if rate != "0/0" else 0
        self.blocks = []

    def metadata_blocks(self, block_seconds: float):
        """Build a time plan without decoding the full source before a probe."""
        count = max(1, math.ceil(self.duration / block_seconds - 1e-7))
        self.blocks = [
            {
                "start": i * block_seconds,
                "duration": min(block_seconds, self.duration - i * block_seconds),
                "input_bytes": 0,
            }
            for i in range(count)
        ]
        if len(self.blocks) > 1 and self.blocks[-1]["duration"] < block_seconds / 2:
            self.blocks[-2]["duration"] += self.blocks.pop()["duration"]
        return self.blocks

    def verify_source(self):
        if self.asset is not None:
            # Linux seals prohibit writes, growth, and truncation of these bytes.
            self.asset.fileno()
        elif digest(self.spec.source, self.deadline) != self.source_hash:
            raise RuntimeError("input changed during analysis; result was discarded")

    def key(self, crf: float, window: tuple[float, float] | None):
        return canonical_key(
            {
                "pipeline": PIPELINE_VERSION,
                "source": self.source_hash,
                "toolchain": self.toolchain_key,
                "threads": self.threads,
                "configuration": replace(self.spec, crf=crf).configuration(),
                "window": window,
            }
        )

    def scan(self, block_seconds: float, visual: bool = True):
        started = time.monotonic()
        data = json.loads(
            self.deadline.run(
                [
                    "ffprobe",
                    "-v",
                    "error",
                    "-select_streams",
                    str(self.video["index"]),
                    "-show_packets",
                    "-show_entries",
                    "packet=pts_time,duration_time,size",
                    "-of",
                    "json",
                    self.input_path,
                ]
            ).stdout
        )["packets"]
        data = [p for p in data if "pts_time" in p]
        if not data:
            raise ValueError("video packet timestamps are required")
        origin = min(float(p["pts_time"]) for p in data)
        end = max(float(p["pts_time"]) + float(p.get("duration_time", 0)) for p in data)
        duration = end - origin
        count = max(1, math.ceil(duration / block_seconds - 1e-7))
        blocks = [
            {
                "start": i * block_seconds,
                "duration": min(block_seconds, duration - i * block_seconds),
                "input_bytes": 0,
            }
            for i in range(count)
        ]
        if len(blocks) > 1 and blocks[-1]["duration"] < block_seconds / 2:
            blocks[-2]["duration"] += blocks.pop()["duration"]
        for packet in data:
            index = min(len(blocks) - 1, int((float(packet["pts_time"]) - origin) / block_seconds))
            blocks[index]["input_bytes"] += int(packet["size"])
        self.blocks, self.duration = blocks, duration
        if visual:
            self._visual_features(block_seconds)
        self.scan_seconds = time.monotonic() - started
        return blocks

    def preview_features(self, blocks, preview_fps=4):
        """Costed whole-source preview, cached independently of CRF and probes.

        A feature vector selects content; it is not a size bound. Low preview
        rates can miss short events, so calibration or an exact file is needed.
        """
        started = time.monotonic()
        self.blocks = blocks
        key = canonical_key(
            {
                "version": "output-space-mean-preview-v1",
                "source": self.source_hash,
                "toolchain": self.toolchain_key,
                "threads": self.threads,
                "video_filter": self.spec.video_filter,
                "preview_fps": preview_fps,
                "resolution": [96, 54],
                "windows": [[b["start"], b["duration"]] for b in blocks],
            }
        )
        path = self.cache.root / f"{key}.features.json"
        hit = False
        try:
            cached = json.loads(path.read_text())
            if not isinstance(cached, dict):
                raise ValueError("preview metadata must be an object")
            values = cached["blocks"]
            if not isinstance(values, list) or not all(isinstance(b, dict) for b in values):
                raise ValueError("preview blocks must be a list of objects")
            if cached["key"] != key or cached["checksum"] != canonical_key({"blocks": values}):
                raise ValueError("preview metadata checksum mismatch")
            if len(values) != len(blocks) or any(
                not isinstance(b.get("features"), list)
                or len(b["features"]) != 3
                or not all(math.isfinite(x) for x in b["features"])
                or b["start"] != original["start"]
                or b["duration"] != original["duration"]
                for b, original in zip(values, blocks)
            ):
                raise ValueError("preview metadata does not match the frozen plan")
            self.blocks = values
            hit = True
        except (OSError, ValueError, TypeError, KeyError):
            self._visual_features(1, preview_fps=preview_fps)
            self.verify_source()
            descriptor, name = tempfile.mkstemp(
                prefix="partial-features-", suffix=".json", dir=self.cache.root
            )
            try:
                with os.fdopen(descriptor, "w") as stream:
                    json.dump(
                        {
                            "key": key,
                            "blocks": self.blocks,
                            "checksum": canonical_key({"blocks": self.blocks}),
                        },
                        stream,
                    )
                    stream.flush()
                    os.fsync(stream.fileno())
                self.deadline.check()
                os.replace(name, path)
            finally:
                Path(name).unlink(missing_ok=True)
        return self.blocks, {
            "cache_hit": hit,
            "elapsed_seconds": time.monotonic() - started,
            "key": key,
            "preview_fps": preview_fps,
            "resolution": [96, 54],
        }

    def _visual_features(self, block_seconds, *, preview_fps=8):
        # These features come from decoded output-space content, so constant
        # input bitrate and export filters need not erase the complexity signal.
        filters = [self.spec.video_filter] if self.spec.video_filter else []
        filters += [f"fps={preview_fps}", "scale=96:54:flags=area", "format=gray"]
        raw = self.deadline.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-nostdin",
                "-xerror",
                "-threads",
                str(self.threads),
                "-filter_threads",
                str(self.threads),
                "-i",
                self.input_path,
                "-map",
                f"0:{self.video['index']}",
                "-vf",
                ",".join(filters),
                "-an",
                "-sn",
                "-f",
                "rawvideo",
                "-pix_fmt",
                "gray",
                "pipe:1",
            ]
        ).stdout
        if len(raw) % (96 * 54):
            raise RuntimeError("incomplete raw preview frame")
        frames = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 54, 96)
        if not len(frames):
            raise ValueError("preview returned no frames")
        starts = [b["start"] for b in self.blocks]
        spatial = [[] for _ in self.blocks]
        temporal = [[] for _ in self.blocks]
        previous = None
        for n, frame in enumerate(frames):
            if n % 64 == 0:
                self.deadline.check()
            index = min(len(self.blocks) - 1, max(0, bisect_right(starts, n / preview_fps) - 1))
            plane = frame.astype(np.int16)
            gradient = float(
                np.abs(np.diff(plane, axis=0)).mean() + np.abs(np.diff(plane, axis=1)).mean()
            )
            delta = float(np.abs(plane - previous).mean()) if previous is not None else 0.0
            spatial[index].append(gradient)
            temporal[index].append(delta)
            previous = plane
        matrix = []
        for i, block in enumerate(self.blocks):
            texture = float(np.mean(spatial[i])) if spatial[i] else 0.0
            motion = float(np.mean(temporal[i])) if temporal[i] else 0.0
            block["visual_texture"] = texture
            block["visual_change"] = motion
            matrix.append(
                [
                    math.log1p(block["input_bytes"] / block["duration"]),
                    math.log1p(texture),
                    math.log1p(motion),
                ]
            )
        features = np.array(matrix)
        spread = features.std(axis=0)
        normalized = (features - features.mean(axis=0)) / np.maximum(spread, 1e-3)
        normalized *= np.array([0.25, 1.0, 1.0])
        for block, vector in zip(self.blocks, normalized):
            block["features"] = vector.tolist()

    def lookup(self, crf: float, window=None):
        return self.cache.lookup(self.key(crf, window), self.deadline)

    def encode(self, crf: float, window=None):
        key = self.key(crf, window)
        if record := self.cache.lookup(key, self.deadline):
            return record
        temporary = self.cache.temporary()
        started = time.monotonic()
        command = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "info",
            "-nostdin",
            "-y",
            "-xerror",
            "-benchmark",
            "-nostats",
        ]
        if window:
            command += ["-ss", str(window[0])]
        command += [
            "-threads",
            str(self.threads),
            "-filter_threads",
            str(self.threads),
            "-i",
            self.input_path,
        ]
        if window:
            command += ["-t", str(window[1])]
        command += ["-map", f"0:{self.video['index']}"]
        if self.audio:
            command += [
                "-map",
                f"0:{self.audio['index']}",
                "-c:a",
                "aac",
                "-b:a",
                str(self.spec.audio_bitrate),
            ]
        else:
            command += ["-an"]
        command += [
            "-sn",
            "-dn",
            "-map_metadata",
            "-1",
            "-c:v",
            "libx264",
            "-crf",
            str(crf),
            "-preset",
            self.spec.preset,
            "-threads",
            str(self.threads),
            "-pix_fmt",
            "yuv420p",
            "-fps_mode",
            "passthrough",
        ]
        if self.spec.video_filter:
            command += ["-vf", self.spec.video_filter]
        command += ["-movflags", "+faststart", str(temporary)]
        try:
            encoded = self.deadline.run(command)
            process_seconds = time.monotonic() - started
            measured = re.search(
                rb"bench: utime=[\d.]+s stime=[\d.]+s rtime=([\d.]+)s",
                encoded.stderr,
            )
            # FFmpeg's internal real-time measurement excludes process startup
            # and output inspection. Extrapolating these fixed costs by duration
            # made cheap full exports look more expensive than repeated probes.
            transcode_seconds = float(measured[1]) if measured else process_seconds
            output = json.loads(
                self.deadline.run(
                    [
                        "ffprobe",
                        "-v",
                        "error",
                        "-show_streams",
                        "-show_format",
                        "-show_packets",
                        "-show_entries",
                        "packet=stream_index,size:stream=codec_type,duration,nb_frames,width,height:format=duration,size",
                        "-of",
                        "json",
                        str(temporary),
                    ]
                ).stdout
            )
            video = next(s for s in output["streams"] if s["codec_type"] == "video")
            if int(video.get("nb_frames", 0)) < 1:
                raise RuntimeError("encoder returned no video frames")
            payload_bytes = sum(int(p["size"]) for p in output["packets"])
            record = {
                "key": key,
                "crf": crf,
                "window": window,
                "duration": float(output["format"]["duration"]),
                "video_frames": int(video["nb_frames"]),
                "width": video["width"],
                "height": video["height"],
                "payload_bytes": payload_bytes,
                "encode_wall_seconds": time.monotonic() - started,
                "transcode_wall_seconds": transcode_seconds,
                "source_hash": self.source_hash,
                "toolchain_key": self.toolchain_key,
                "configuration": replace(self.spec, crf=crf).configuration(),
            }
            self.verify_source()
            return self.cache.publish(key, temporary, record, self.deadline)
        finally:
            temporary.unlink(missing_ok=True)
