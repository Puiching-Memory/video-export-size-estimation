"""One controller for compute budget, reliability target, and output-size limit."""

import math
import os
import shutil
import tempfile
from dataclasses import asdict, replace
from pathlib import Path

from .calibration import Calibration, policy_key, relative_error_bound, risk_multiplier
from .contracts import Request
from .inference import Sampler
from .media import Media
from .runtime import BudgetExhausted, Cache, Deadline, digest


class Engine:
    def __init__(self, cache_dir: Path, calibration: Path | None = None):
        self.cache = Cache(cache_dir)
        self.calibration = Calibration(calibration)

    def run(self, request: Request, on_update=None):
        deadline = Deadline(request.compute.wall_seconds)
        crf_grid = [request.encode.crf]
        if request.size.max_bytes is not None:
            crf_grid += [
                float(q)
                for q in range(
                    math.floor(request.encode.crf) + 1, math.floor(request.size.max_crf) + 1
                )
            ]
            if request.size.max_crf not in crf_grid:
                crf_grid.append(request.size.max_crf)
        result = {
            "schema_version": 1,
            "requested": {
                "compute": asdict(request.compute),
                "reliability": asdict(request.reliability),
                "size": asdict(request.size),
                "configuration": request.encode.configuration(),
                "sample_seconds": request.sample_seconds,
                "seed": request.seed,
            },
            "status": "in_progress",
            "estimate": None,
            "candidates": [],
            "unmet": [],
            "elapsed_seconds": 0.0,
            "attempted_encode_seconds": 0.0,
        }
        media = None
        probe_count = 0
        tried = []
        crf = request.encode.crf
        last_full = None

        def can_encode(seconds):
            fraction = request.compute.max_encode_fraction
            return (
                fraction is None
                or result["attempted_encode_seconds"] + seconds <= fraction * media.duration + 1e-7
            )

        def perform(crf, window=None):
            seconds = window[1] if window else media.duration
            if not can_encode(seconds):
                raise BudgetExhausted("encoded-media budget exhausted")
            result["attempted_encode_seconds"] += seconds
            return media.encode(crf, window)

        def emit(snapshot):
            result["estimate"] = snapshot
            result["elapsed_seconds"] = deadline.elapsed
            if on_update:
                on_update({"type": "estimate", **snapshot, "elapsed_seconds": deadline.elapsed})

        def qualifies(snapshot):
            interval = snapshot["uncertainty"].get("interval_bytes")
            precise = (
                interval is not None
                and relative_error_bound(snapshot["estimated_bytes"], interval)
                <= request.reliability.relative_error
            )
            # A file-size hard limit is satisfied only by an actual completed file.
            size_ok = request.size.max_bytes is None or (
                snapshot["uncertainty"]["kind"] == "exact"
                and snapshot["estimated_bytes"] <= request.size.max_bytes
            )
            return precise and size_ok

        def exact(record):
            nonlocal last_full
            last_full = record
            return {
                "estimated_bytes": record["file_bytes"],
                "selected_crf": record["crf"],
                "uncertainty": {
                    "kind": "exact",
                    "interval_bytes": [record["file_bytes"]] * 2,
                    "coverage": 1.0,
                },
                "artifact": record["artifact"],
                "sha256": record["sha256"],
                "cache_hit": record["cache_hit"],
                "sample_count": probe_count,
                "source_hash": record["source_hash"],
            }

        try:
            media = Media(request.encode, request.compute.threads, self.cache, deadline)
            result["toolchain_key"] = media.toolchain_key
            while len(tried) < request.size.max_candidates:
                deadline.check()
                tried.append(crf)
                full = media.lookup(crf)
                if (
                    full is None
                    and last_full is not None
                    and last_full["encode_wall_seconds"] * 1.5 < deadline.remaining
                    and can_encode(media.duration)
                ):
                    # Same source/preset/filter, a different CRF: reuse the
                    # measured whole-job cost instead of spending another probe.
                    # Cost is a proposal; the deadline remains authoritative.
                    full = perform(crf)
                if full is None and request.compute.max_probes == 0 and can_encode(media.duration):
                    # A zero probe limit does not prohibit an actual export.
                    full = perform(crf)
                if full:
                    snapshot = exact(full)
                    emit(snapshot)
                else:
                    if not media.blocks:
                        media.scan(request.sample_seconds)
                    sampler = Sampler(media.blocks, request.seed)
                    recent = []
                    snapshot = None
                    for index in sampler.order:
                        block = media.blocks[index]
                        window = (block["start"], block["duration"])
                        cached = media.lookup(crf, window)
                        if cached is None and probe_count >= request.compute.max_probes:
                            break
                        if cached is None and not can_encode(window[1]):
                            break
                        deadline.check()
                        record = cached or perform(crf, window)
                        if not cached:
                            probe_count += 1
                        sampler.add(index, record)
                        recent.append(record)
                        estimate = sampler.estimate()
                        configuration = replace(request.encode, crf=crf).configuration()
                        key = policy_key(
                            configuration,
                            request.sample_seconds,
                            estimate["sample_count"],
                            request.seed,
                            media.toolchain_key,
                            request.compute.threads,
                        )
                        envelope = self.calibration.interval(
                            key,
                            estimate["estimated_bytes"],
                            request.reliability.coverage,
                            risk_multiplier(estimate["sample_count"], len(crf_grid)),
                        )
                        if envelope:
                            estimate["uncertainty"] = envelope
                        snapshot = {
                            **estimate,
                            "selected_crf": crf,
                            "artifact": None,
                            "source_hash": media.source_hash,
                        }
                        emit(snapshot)
                        if qualifies(snapshot):
                            result["status"] = "satisfied"
                            return self._finish(result, request, deadline)
                        if (
                            request.size.max_bytes is not None
                            and all(i in sampler.observed for i in sampler.pilots)
                            and estimate["estimated_bytes"] > request.size.max_bytes
                            and any(q not in tried for q in crf_grid)
                        ):
                            # The requested size can guide candidate selection
                            # before spending a complete export on an apparently
                            # oversized setting. This never certifies feasibility
                            # and does not claim globally best quality.
                            break
                        # Reusable continuous export is the high-fidelity action.
                        # Admission is an estimate; the shared deadline still enforces cancellation.
                        rate = sum(
                            r.get("transcode_wall_seconds", r["encode_wall_seconds"])
                            for r in recent
                        ) / sum(media.blocks[i]["duration"] for i in sampler.observed)
                        fixed_cost = max(
                            r["encode_wall_seconds"]
                            - r.get("transcode_wall_seconds", r["encode_wall_seconds"])
                            for r in recent
                        )
                        expected_full_seconds = max(0.1, (fixed_cost + rate * media.duration) * 1.5)
                        if expected_full_seconds < deadline.remaining and can_encode(
                            media.duration
                        ):
                            full = perform(crf)
                            snapshot = exact(full)
                            emit(snapshot)
                            break
                    if snapshot is None:
                        result["status"] = "constraints_unmet"
                        break
                result["candidates"].append(
                    {
                        "crf": crf,
                        "estimated_bytes": snapshot["estimated_bytes"],
                        "evidence": snapshot["uncertainty"]["kind"],
                    }
                )
                if qualifies(snapshot):
                    result["status"] = "satisfied"
                    break
                if request.size.max_bytes is None:
                    result["status"] = "constraints_unmet"
                    break
                remaining_crfs = [q for q in crf_grid if q not in tried]
                if not remaining_crfs:
                    result["status"] = "constraints_unmet"
                    break
                # Log-rate/CRF relation proposes the next candidate. The decision
                # is verified by encoding; monotonicity is never used as a proof.
                ratio = snapshot["estimated_bytes"] / request.size.max_bytes
                proposed = crf + 6 * math.log2(max(ratio, 1.01)) + 0.5
                crf = min(remaining_crfs, key=lambda q: abs(q - proposed))
            if result["status"] == "in_progress":
                result["status"] = "constraints_unmet"
        except BudgetExhausted:
            result["status"] = "budget_exhausted"
        return self._finish(result, request, deadline)

    @staticmethod
    def _finish(result, request, deadline):
        result["elapsed_seconds"] = deadline.elapsed
        estimate = result["estimate"]
        unmet = []
        if estimate is None:
            unmet.append("no_estimate_within_budget")
        else:
            interval = estimate["uncertainty"].get("interval_bytes")
            if interval is None:
                unmet.append("reliability_not_calibrated")
            elif (
                relative_error_bound(estimate["estimated_bytes"], interval)
                > request.reliability.relative_error
            ):
                unmet.append("relative_error_target_not_met")
            if request.size.max_bytes is not None:
                if estimate["uncertainty"]["kind"] != "exact":
                    unmet.append("size_limit_not_verified")
                elif estimate["estimated_bytes"] > request.size.max_bytes:
                    unmet.append("size_limit_exceeded_at_tested_setting")
        result["unmet"] = unmet
        result["alternatives"] = []
        if unmet:
            result["alternatives"].append(
                "Increase the compute budget to permit further probes or a verified export."
            )
            if request.size.max_bytes is not None:
                result["alternatives"].append(
                    "Explicitly relax the size limit or expand the allowed CRF range."
                )
        return result

    @staticmethod
    def materialize(result: dict, destination: Path):
        """Publish a verified result without overwriting an existing destination."""
        estimate = result.get("estimate")
        if (
            result.get("status") != "satisfied"
            or not estimate
            or estimate["uncertainty"]["kind"] != "exact"
        ):
            raise ValueError("only a satisfied, exact result can be exported")
        source = Path(estimate["artifact"])
        limit = result["requested"]["size"]["max_bytes"]
        actual = source.stat().st_size
        if actual != estimate["estimated_bytes"] or (limit is not None and actual > limit):
            raise ValueError("artifact size no longer satisfies the result")
        destination = Path(destination).resolve()
        destination.parent.mkdir(parents=True, exist_ok=True)
        descriptor, name = tempfile.mkstemp(prefix=".vsize-", suffix=".mp4", dir=destination.parent)
        os.close(descriptor)
        temporary = Path(name)
        try:
            shutil.copyfile(source, temporary)
            if digest(temporary) != estimate["sha256"]:
                raise ValueError("artifact checksum mismatch")
            with temporary.open("rb") as stream:
                os.fsync(stream.fileno())
            os.link(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)
        return str(destination)
