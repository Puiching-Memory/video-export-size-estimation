"""Split-conformal envelopes from independent source groups.

Coverage is marginal under exchangeability with the declared calibration domain.
Repeated/adaptive looks use a union bound; insufficient calibration produces no
finite interval. A point estimate or a small sample spread is never a certificate.
"""

import json
import math
from pathlib import Path

from .runtime import canonical_key


def policy_key(
    configuration: dict,
    sample_seconds: float,
    sample_count: int,
    seed: int,
    toolchain_key: str,
    threads: int,
):
    return canonical_key(
        {
            "policy": "visual-stratified-residual-v4",
            "configuration": configuration,
            "sample_seconds": sample_seconds,
            "sample_count": sample_count,
            "seed": seed,
            "toolchain_key": toolchain_key,
            "threads": threads,
        }
    )


class Calibration:
    def __init__(self, path: Path | None = None):
        self.profiles = {}
        if path:
            payload = json.loads(Path(path).read_text())
            if payload.get("schema_version") != 1:
                raise ValueError("unsupported calibration schema")
            for profile in payload["profiles"]:
                groups = profile["groups"]
                if len({g["group_id"] for g in groups}) != len(groups):
                    raise ValueError("calibration groups must be independent and unique")
                for group in groups:
                    score = group["absolute_log_error"]
                    if not math.isfinite(score) or score < 0:
                        raise ValueError("calibration scores must be finite and nonnegative")
                self.profiles[profile["policy_key"]] = profile

    def interval(self, key: str, prediction: int, coverage: float, risk_multiplier: int):
        profile = self.profiles.get(key)
        if profile is None:
            return None
        scores = sorted(g["absolute_log_error"] for g in profile["groups"])
        rank = math.ceil((len(scores) + 1) * (1 - (1 - coverage) / risk_multiplier))
        if rank > len(scores):
            return None
        radius = scores[rank - 1]
        if radius > 700:
            return None
        return {
            "kind": "split_conformal",
            "coverage": coverage,
            "interval_bytes": [
                max(1, math.floor(prediction * math.exp(-radius))),
                math.ceil(prediction * math.exp(radius)),
            ],
            "calibration_groups": len(scores),
            "risk_multiplier": risk_multiplier,
            "domain": profile["domain"],
            "assumption": "New source groups are exchangeable with the declared calibration domain.",
        }


def risk_multiplier(sample_count: int, configurations: int):
    """Spend delta / (Q * j * (j + 1)) at prefix j, over Q fixed CRFs.

    The sum over all j >= 1 is delta / Q. This protects optional stopping
    without making the number of looks depend on the new video's block count.
    """
    return configurations * sample_count * (sample_count + 1)


def relative_error_bound(prediction: int, interval: list[int]):
    low, high = interval
    return max((prediction - low) / low, (high - prediction) / high)
