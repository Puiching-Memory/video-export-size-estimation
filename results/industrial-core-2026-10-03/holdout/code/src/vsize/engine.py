"""Budgeted prediction, reusable segmented export, and verified CRF refinement."""

import json
import os
import shutil
import tempfile
from dataclasses import asdict, replace
from pathlib import Path

from .calibration import relative_error_bound
from .contracts import Request
from .inference import Sampler
from .media import Media
from .runtime import BudgetExhausted, Cache, Deadline, digest
from .search import CRFSearch
from .sessions import ContinuousSession, SegmentedSession
from .trajectory import TrajectoryCalibration, family_key, harmonic_point, make_family


class Engine:
    def __init__(self, cache_dir: Path, calibration: Path | None = None):
        self.cache = Cache(cache_dir)
        self.calibration = TrajectoryCalibration()
        self.calibration_note = None
        if calibration:
            schema = json.loads(Path(calibration).read_text()).get("schema_version")
            if schema == 2:
                self.calibration = TrajectoryCalibration(calibration)
            elif schema == 1:
                self.calibration_note = (
                    "Legacy profiles do not calibrate the current prediction policy."
                )
            else:
                raise ValueError("unsupported calibration schema")

    def run(self, request: Request, on_update=None, *, asset=None):
        deadline = Deadline(request.compute.wall_seconds)
        search = (
            CRFSearch(request.encode.crf, request.size.max_crf, request.size.max_bytes)
            if request.size.max_bytes is not None
            else None
        )
        crf_grid = list(search.grid) if search else [request.encode.crf]
        result = {
            "schema_version": 2,
            "requested": {
                "compute": asdict(request.compute),
                "reliability": asdict(request.reliability),
                "size": asdict(request.size),
                "configuration": request.encode.configuration(),
                "sample_seconds": request.sample_seconds,
                "seed": request.seed,
                "probe_mode": request.probe_mode,
                "sampling_plan": request.sampling_plan,
            },
            "status": "in_progress",
            "estimate": None,
            "candidates": [],
            "unmet": [],
            "elapsed_seconds": 0.0,
            "first_estimate_seconds": None,
            "attempted_encode_seconds": 0.0,
            "attempted_audio_seconds": 0.0,
            "audio_preparation_seconds": 0.0,
            "total_probe_count": 0,
            "encode_fraction_basis": "requested_output_video_seconds; audio charged to wall budget",
            "prepared_asset": None,
            "calibration_note": self.calibration_note,
            "content_discovery": None,
            "stop_reason": None,
        }
        if asset is not None:
            result["prepared_asset"] = {
                "source_hash": asset.sha256,
                "bytes": asset.bytes,
                "ingest_seconds": asset.ingest_seconds,
                "ingest_in_request_budget": False,
                "identity": "linux_sealed_memfd",
            }
        media = None
        session = None
        probe_count = 0
        last_full = None
        best = None
        crf = request.encode.crf

        def can_encode(seconds):
            fraction = request.compute.max_encode_fraction
            duration = session.duration if session is not None else media.duration
            return fraction is None or (
                result["attempted_encode_seconds"] + seconds <= fraction * duration + 1e-7
            )

        def charge(seconds):
            deadline.check()
            if not can_encode(seconds):
                raise BudgetExhausted("encoded-video budget exhausted")
            result["attempted_encode_seconds"] += seconds

        def emit(snapshot):
            result["estimate"] = snapshot
            result["elapsed_seconds"] = deadline.elapsed
            if result["first_estimate_seconds"] is None:
                result["first_estimate_seconds"] = deadline.elapsed
            if on_update:
                on_update({"type": "estimate", **snapshot, "elapsed_seconds": deadline.elapsed})

        def qualifies(snapshot):
            interval = snapshot["uncertainty"].get("interval_bytes")
            precise = interval is not None and (
                relative_error_bound(snapshot["estimated_bytes"], interval)
                <= request.reliability.relative_error
            )
            size_ok = request.size.max_bytes is None or (
                snapshot["uncertainty"]["kind"] == "exact"
                and snapshot["estimated_bytes"] <= request.size.max_bytes
            )
            return precise and size_ok

        def exact(record):
            nonlocal last_full, best
            if record["cache_hit"]:
                media.verify_source()
                if isinstance(session, SegmentedSession):
                    session.adapter.source_verified = True
                    result["segment_plan"] = session.adapter.plan()
            last_full = record
            snapshot = {
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
                "export_mode": request.encode.export_mode,
            }
            for name in ("byte_accounting", "reused_segments", "segment_count"):
                if name in record:
                    snapshot[name] = record[name]
            # Register the verified artifact before progress callbacks or
            # further work can stop the controller.
            if search:
                search.observe(record["crf"], record["file_bytes"], actual=True, record=snapshot)
                verified = search.best_actual()
                if verified is not None:
                    best = verified.record
            return snapshot

        try:
            media = Media(
                request.encode, request.compute.threads, self.cache, deadline, asset=asset
            )
            result["source_hash"] = media.source_hash
            result["toolchain_key"] = media.toolchain_key
            session = (
                SegmentedSession(media, request, charge, result)
                if request.encode.export_mode == "segmented"
                else ContinuousSession(media, request, charge, result)
            )
            pilot_count = min(3, len(session.blocks), max(1, request.compute.max_probes))
            if request.compute.max_encode_fraction is not None:
                nominal = sum(b["duration"] for b in session.blocks) / len(session.blocks)
                pilot_count = min(
                    pilot_count,
                    max(1, int(request.compute.max_encode_fraction * session.duration / nominal)),
                )
            result["pilot_count"] = pilot_count
            initial_sampler = Sampler(session.blocks, request.seed, count=pilot_count, uniform=True)
            session.choose_probe_mode(initial_sampler.order[0], can_encode)
            for _ in range(request.size.max_candidates):
                deadline.check()
                if search:
                    search.mark_attempted(crf)
                full = session.lookup_full(crf)
                if full is None and last_full is not None:
                    if can_encode(session.remaining_seconds(crf)) and (
                        last_full["encode_wall_seconds"] * 1.5 + 0.2 < deadline.remaining
                    ):
                        full = session.complete(crf)
                if full is None and request.compute.max_probes == 0:
                    if can_encode(session.remaining_seconds(crf)):
                        full = session.complete(crf)
                snapshot = None
                if full is not None:
                    snapshot = exact(full)
                    emit(snapshot)
                else:
                    if request.sampling_plan == "content" and result["content_discovery"] is None:
                        session.blocks, result["content_discovery"] = media.preview_features(
                            session.blocks, 4
                        )
                    sampler = Sampler(
                        session.blocks,
                        request.seed,
                        count=pilot_count,
                        uniform=request.sampling_plan == "temporal",
                    )
                    session.choose_probe_mode(sampler.order[0], can_encode)
                    family = make_family(
                        request.encode.configuration(),
                        crf_grid,
                        request.encode.segment_seconds
                        if request.encode.export_mode == "segmented"
                        else request.sample_seconds,
                        {
                            **session.probe_policy(),
                            "sampling_plan": request.sampling_plan,
                            "preview_rule": "4fps_96x54_mean_gradient_and_frame_difference",
                            "pilot_count_rule": "min_3_blocks_probe_limit_and_video_fraction",
                            "max_encode_fraction": request.compute.max_encode_fraction,
                        },
                        {"kind": "fixed", "seed": request.seed},
                        media.toolchain_key,
                        request.compute.threads,
                        model_policy="budgeted-content-or-temporal-residual-v2",
                        prefix_policy={
                            "kind": "all_prefixes_up_to_maximum",
                            "minimum_prefix": 1,
                            "maximum_prefix": request.compute.max_probes,
                        },
                    )
                    key = family_key(family)
                    result["prediction_family"] = family
                    result["prediction_family_key"] = key
                    for index in sampler.order:
                        deadline.check()
                        cost, cached = session.probe_plan(index, crf)
                        if cached is None:
                            if probe_count >= request.compute.max_probes or not can_encode(cost):
                                break
                            charge(cost)
                            probe_count += 1
                            result["total_probe_count"] = probe_count
                        record = session.probe(index, crf, cached)
                        sampler.add(index, record)
                        estimate = session.estimate(sampler)
                        configuration = replace(request.encode, crf=crf).configuration()
                        envelope = self.calibration.interval(
                            key,
                            estimate["estimated_bytes"],
                            request.reliability.coverage,
                            configuration=configuration,
                            sample_count=estimate["sample_count"],
                        )
                        if envelope:
                            estimate["uncertainty"] = envelope
                            estimate["raw_prediction_bytes"] = estimate["estimated_bytes"]
                            estimate["estimated_bytes"] = round(
                                harmonic_point(envelope["interval_bytes"])
                            )
                        snapshot = {
                            **estimate,
                            "selected_crf": crf,
                            "artifact": None,
                            "source_hash": media.source_hash,
                            "observed_blocks": sorted(sampler.observed),
                            "pilot_indices": sampler.pilots,
                        }
                        emit(snapshot)
                        if qualifies(snapshot) and search is None:
                            media.verify_source()
                            if isinstance(session, SegmentedSession):
                                session.adapter.source_verified = True
                                result["segment_plan"] = session.adapter.plan()
                            result["status"] = "satisfied"
                            result["stop_reason"] = "reliability_target_met"
                            return self._finish(result, request, deadline)
                        remaining = session.remaining_seconds(crf)
                        if can_encode(remaining) and (
                            session.expected_complete_wall(sampler) < deadline.remaining
                        ):
                            full = session.complete(crf)
                            snapshot = exact(full)
                            emit(snapshot)
                            break
                        if (
                            search is not None
                            and all(i in sampler.observed for i in sampler.pilots)
                            and estimate["estimated_bytes"] > request.size.max_bytes
                        ):
                            break
                if snapshot is None:
                    result["stop_reason"] = "no_more_admissible_work"
                    break
                actual = snapshot["uncertainty"]["kind"] == "exact"
                result["candidates"].append(
                    {
                        "crf": crf,
                        "estimated_bytes": snapshot["estimated_bytes"],
                        "evidence": snapshot["uncertainty"]["kind"],
                    }
                )
                if search:
                    if not actual:
                        search.observe(
                            crf, snapshot["estimated_bytes"], actual=False, record=snapshot
                        )
                    verified = search.best_actual()
                    if verified is not None:
                        best = verified.record
                    next_crf = search.propose()
                    if next_crf is None:
                        result["stop_reason"] = "allowed_grid_search_finished"
                        break
                    crf = next_crf
                else:
                    result["stop_reason"] = (
                        "exact_export_complete" if actual else "no_more_admissible_work"
                    )
                    break
            else:
                result["stop_reason"] = "candidate_limit_reached"
            if (
                result["estimate"] is not None
                and result["estimate"]["uncertainty"]["kind"] != "exact"
            ):
                media.verify_source()
                if isinstance(session, SegmentedSession):
                    session.adapter.source_verified = True
            if isinstance(session, SegmentedSession):
                result["segment_plan"] = session.adapter.plan()
            result["status"] = "constraints_unmet"
        except BudgetExhausted as error:
            result["status"] = "budget_exhausted"
            result["stop_reason"] = str(error)
            if result["estimate"] is not None and (
                result["estimate"]["uncertainty"]["kind"] != "exact" and asset is None
            ):
                result["estimate"]["uncertainty"] = {
                    "kind": "uncalibrated",
                    "interval_bytes": None,
                    "coverage": None,
                    "reason": "Final source identity verification did not finish within budget.",
                }
        finally:
            if search:
                result["quality_search"] = search.summary()
            if best is not None:
                result["estimate"] = best
        return self._finish(result, request, deadline)

    @staticmethod
    def _finish(result, request, deadline):
        result["elapsed_seconds"] = deadline.elapsed
        result["unmet"] = []
        estimate = result["estimate"]
        if estimate is None:
            result["unmet"].append("no_estimate_within_budget")
        else:
            interval = estimate["uncertainty"].get("interval_bytes")
            if interval is None:
                result["unmet"].append("reliability_not_calibrated")
            elif (
                relative_error_bound(estimate["estimated_bytes"], interval)
                > request.reliability.relative_error
            ):
                result["unmet"].append("relative_error_target_not_met")
            if request.size.max_bytes is not None:
                if estimate["uncertainty"]["kind"] != "exact":
                    result["unmet"].append("size_limit_not_verified")
                elif estimate["estimated_bytes"] > request.size.max_bytes:
                    result["unmet"].append("size_limit_exceeded_at_tested_setting")
        if not result["unmet"]:
            result["status"] = "satisfied"
        result["alternatives"] = []
        if result["unmet"]:
            result["alternatives"].append(
                "Increase the compute budget to permit further probes or a verified export."
            )
            if request.size.max_bytes is not None:
                result["alternatives"].append(
                    "Relax the size limit or expand the allowed CRF range."
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
