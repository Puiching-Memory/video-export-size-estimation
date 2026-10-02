"""Content strata plus randomized residual correction.

Representatives supply a cheap model. Independent random samples of the remaining
blocks correct its total. Encoder startup/context bias is a separate error source;
sample variance alone is deliberately not presented as a calibrated interval.
"""

import math
import random
import statistics


def representatives(blocks: list[dict], count: int):
    values = [
        tuple(b.get("features", [math.log1p(b["input_bytes"] / b["duration"])])) for b in blocks
    ]

    def distance(a, b):
        return sum((x - y) ** 2 for x, y in zip(a, b))

    def mean(indices):
        return tuple(statistics.fmean(values[i][j] for i in indices) for j in range(len(values[0])))

    middle = mean(range(len(values)))
    centers = [min(values, key=lambda v: distance(v, middle))]
    while len(centers) < min(count, len(set(values))):
        value = max(values, key=lambda v: min(distance(v, c) for c in centers))
        if value in centers:
            break
        centers.append(value)
    for _ in range(50):
        groups = [
            [
                i
                for i, value in enumerate(values)
                if min(range(len(centers)), key=lambda j: distance(value, centers[j])) == k
            ]
            for k in range(len(centers))
        ]
        groups = [g for g in groups if g]
        updated = [mean(group) for group in groups]
        if (
            len(centers) == len(updated)
            and max(distance(a, b) for a, b in zip(centers, updated)) < 1e-12
        ):
            break
        centers = updated
    selected = [min(group, key=lambda i: distance(values[i], mean(group))) for group in groups]
    return groups, selected


class Sampler:
    def __init__(self, blocks: list[dict], seed: int, count: int = 3):
        self.blocks = blocks
        self.groups, self.pilots = representatives(blocks, min(count, len(blocks)))
        remaining = [i for i in range(len(blocks)) if i not in self.pilots]
        random.Random(seed).shuffle(remaining)
        self.audit_order = remaining
        self.order = self.pilots + remaining
        self.observed = {}

    def add(self, index: int, record: dict):
        self.observed[index] = record

    def estimate(self):
        if not self.observed:
            return None
        observed_rates = [
            r["payload_bytes"] / self.blocks[i]["duration"] for i, r in self.observed.items()
        ]
        fallback = statistics.fmean(observed_rates)
        predicted = [fallback * b["duration"] for b in self.blocks]
        for group, pilot in zip(self.groups, self.pilots):
            if pilot in self.observed:
                rate = self.observed[pilot]["payload_bytes"] / self.blocks[pilot]["duration"]
                for index in group:
                    predicted[index] = rate * self.blocks[index]["duration"]
        # Once pilot construction is finished, the model is frozen. Audit blocks
        # are a simple random sample without replacement of the remaining blocks.
        audits = [i for i in self.audit_order if i in self.observed]
        residuals = [self.observed[i]["payload_bytes"] - predicted[i] for i in audits]
        pilot_total = sum(
            self.observed[i]["payload_bytes"] for i in self.pilots if i in self.observed
        )
        remaining = [i for i in range(len(self.blocks)) if i not in self.pilots]
        if all(i in self.observed for i in self.pilots):
            total = pilot_total + sum(predicted[i] for i in remaining)
            if residuals:
                total += len(remaining) * statistics.fmean(residuals)
        else:
            total = sum(predicted)
        # Ordinary MP4 muxing overhead model; final continuous encode is authoritative.
        sampled_duration = sum(self.blocks[i]["duration"] for i in self.observed)
        sampled_frames = sum(r["video_frames"] for r in self.observed.values())
        duration = sum(b["duration"] for b in self.blocks)
        overhead = 2048 + 6 * sampled_frames / sampled_duration * duration
        variance = None
        if len(residuals) >= 2:
            variance = (
                len(remaining) ** 2
                * (1 - len(residuals) / len(remaining))
                * statistics.variance(residuals)
                / len(residuals)
            )
        return {
            "estimated_bytes": max(1, round(total + overhead)),
            "sampled_seconds": sampled_duration,
            "sample_count": len(self.observed),
            "audit_count": len(audits),
            "sampling_standard_error_bytes": math.sqrt(max(0, variance))
            if variance is not None
            else None,
            "uncertainty": {
                "kind": "uncalibrated",
                "interval_bytes": None,
                "coverage": None,
                "reason": "Sampling variance excludes encoder-context and muxing-model bias.",
            },
        }
