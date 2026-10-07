"""Public contracts. Targets are requests; result fields describe achieved evidence."""

import math
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path


def positive(value: float, name: str) -> None:
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be finite and positive")


def integer(value, name, minimum):
    if type(value) is not int or value < minimum:
        raise ValueError(f"{name} must be an integer >= {minimum}")


@dataclass(frozen=True)
class ComputeBudget:
    wall_seconds: float = 10.0
    max_probes: int = 6
    threads: int = 2
    max_encode_fraction: float | None = None

    def __post_init__(self):
        positive(self.wall_seconds, "wall_seconds")
        integer(self.max_probes, "max_probes", 0)
        integer(self.threads, "threads", 1)
        if self.max_encode_fraction is not None:
            if not math.isfinite(self.max_encode_fraction) or self.max_encode_fraction < 0:
                raise ValueError("max_encode_fraction must be finite and nonnegative")


@dataclass(frozen=True)
class Reliability:
    relative_error: float = 0.1
    coverage: float = 0.95

    def __post_init__(self):
        if not math.isfinite(self.relative_error) or not 0 < self.relative_error < 1:
            raise ValueError("relative_error must be between zero and one")
        if not math.isfinite(self.coverage) or not 0 < self.coverage < 1:
            raise ValueError("coverage must be between zero and one")


@dataclass(frozen=True)
class SizeConstraint:
    max_bytes: int | None = None
    max_crf: float = 35.0
    max_candidates: int = 5

    def __post_init__(self):
        if self.max_bytes is not None:
            integer(self.max_bytes, "max_bytes", 1)
        if not math.isfinite(self.max_crf) or not 0 <= self.max_crf <= 51:
            raise ValueError("max_crf must be between zero and 51")
        integer(self.max_candidates, "max_candidates", 1)


@dataclass(frozen=True)
class EncodeSpec:
    source: Path
    crf: float = 23.0
    preset: str = "medium"
    video_filter: str | None = None
    audio_bitrate: int = 128_000
    drop_audio: bool = False
    export_mode: str = "continuous"
    segment_seconds: float = 4.0

    def __post_init__(self):
        object.__setattr__(self, "source", Path(self.source).resolve())
        if not math.isfinite(self.crf) or not 0 <= self.crf <= 51:
            raise ValueError("crf must be between zero and 51")
        presets = {
            "ultrafast",
            "superfast",
            "veryfast",
            "faster",
            "fast",
            "medium",
            "slow",
            "slower",
            "veryslow",
        }
        if self.preset not in presets:
            raise ValueError(f"unsupported x264 preset: {self.preset}")
        integer(self.audio_bitrate, "audio_bitrate", 1)
        if type(self.drop_audio) is not bool:
            raise ValueError("drop_audio must be a boolean")
        if self.export_mode not in {"continuous", "segmented"}:
            raise ValueError("export_mode must be continuous or segmented")
        positive(self.segment_seconds, "segment_seconds")
        if self.video_filter:
            number = r"-?(?:\d+(?:\.\d*)?|\.\d+)"
            patterns = [
                r"scale=\d+:\d+",
                r"crop=\d+:\d+:\d+:\d+",
                r"fps=\d+(?:/\d+)?",
                r"transpose=[0-3]",
                r"[hv]flip",
                rf"eq=(?:brightness|contrast|saturation)={number}(?::(?:brightness|contrast|saturation)={number})*",
            ]
            if any(
                not any(re.fullmatch(pattern, part) for pattern in patterns)
                for part in self.video_filter.split(",")
            ):
                raise ValueError(
                    "supported filters: constant scale/crop/fps/transpose/hflip/vflip/eq"
                )

    def configuration(self) -> dict:
        result = asdict(self)
        result.pop("source")
        return {"codec": "libx264", "container": "mp4", **result}


@dataclass(frozen=True)
class Request:
    encode: EncodeSpec
    compute: ComputeBudget = field(default_factory=ComputeBudget)
    reliability: Reliability = field(default_factory=Reliability)
    size: SizeConstraint = field(default_factory=SizeConstraint)
    sample_seconds: float = 4.0
    seed: int = 0
    probe_mode: str = "auto"
    sampling_plan: str = "temporal"

    def __post_init__(self):
        positive(self.sample_seconds, "sample_seconds")
        integer(self.seed, "seed", 0)
        if self.probe_mode not in {"auto", "context", "bare"}:
            raise ValueError("probe_mode must be auto, context, or bare")
        if self.sampling_plan not in {"temporal", "content"}:
            raise ValueError("sampling_plan must be temporal or content")
        if self.size.max_bytes is not None and self.size.max_crf < self.encode.crf:
            raise ValueError("max_crf cannot be lower than the starting crf")

    @classmethod
    def from_dict(cls, payload: dict):
        allowed = {
            "encode",
            "compute",
            "reliability",
            "size",
            "sample_seconds",
            "seed",
            "probe_mode",
            "sampling_plan",
        }
        unknown = set(payload) - allowed
        if unknown:
            raise ValueError(f"unknown request fields: {sorted(unknown)}")
        return cls(
            encode=EncodeSpec(**payload["encode"]),
            compute=ComputeBudget(**payload.get("compute", {})),
            reliability=Reliability(**payload.get("reliability", {})),
            size=SizeConstraint(**payload.get("size", {})),
            sample_seconds=payload.get("sample_seconds", 4.0),
            seed=payload.get("seed", 0),
            probe_mode=payload.get("probe_mode", "auto"),
            sampling_plan=payload.get("sampling_plan", "temporal"),
        )
