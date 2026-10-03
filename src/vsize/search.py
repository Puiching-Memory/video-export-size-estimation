"""Measured-file CRF search with explicit limits on its optimality claim.

Log-rate interpolation proposes work; it never certifies file-size feasibility.
Every measured observation is retained, including nonmonotonic ones. A caller
may stop at any budget boundary and still retrieve the best verified artifact.
"""

import math
from dataclasses import dataclass
from typing import Any, Sequence


@dataclass(frozen=True)
class SizeObservation:
    crf: float
    file_bytes: int | float
    actual: bool
    record: dict[str, Any] | None = None


class CRFSearch:
    """Search a finite CRF grid, without assuming file sizes are monotonic.

    ``propose`` is idempotent until the caller marks work attempted or records
    an observation. Estimates guide proposals but do not certify feasibility.
    A measured feasible point triggers refinement toward lower CRFs. Previously
    infeasible points help interpolation; they do not prune unmeasured points.

    By default the grid contains the starting CRF, all greater integer CRFs,
    and the maximum CRF. A supplied grid still includes both boundary settings.
    """

    def __init__(
        self,
        initial_crf: float,
        max_crf: float,
        max_bytes: int,
        *,
        allowed_crfs: Sequence[float] | None = None,
    ):
        for value in (initial_crf, max_crf):
            if not math.isfinite(value) or not 0 <= value <= 51:
                raise ValueError("CRFs must be finite and between zero and 51")
        if max_crf < initial_crf:
            raise ValueError("max_crf cannot be lower than initial_crf")
        if type(max_bytes) is not int or max_bytes <= 0:
            raise ValueError("max_bytes must be a positive integer")
        if allowed_crfs is None:
            allowed_crfs = range(math.floor(initial_crf) + 1, math.floor(max_crf) + 1)
        grid = {float(initial_crf), float(max_crf)}
        for value in allowed_crfs:
            if not math.isfinite(value) or not initial_crf <= value <= max_crf:
                raise ValueError("allowed CRFs must be finite and within the search bounds")
            grid.add(float(value))
        self.grid = tuple(sorted(grid))
        self.max_bytes = max_bytes
        self.observations: list[SizeObservation] = []
        self.attempted: set[float] = set()

    def _crf(self, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("CRF must be finite and belong to the search grid")
        for allowed in self.grid:
            if math.isclose(value, allowed, rel_tol=0, abs_tol=1e-9):
                return allowed
        raise ValueError("CRF must belong to the search grid")

    def mark_attempted(self, crf: float) -> None:
        """Exclude started work from future proposals, even if it was cancelled."""
        self.attempted.add(self._crf(crf))

    def observe(
        self,
        crf: float,
        file_bytes: int | float,
        *,
        actual: bool = True,
        record: dict[str, Any] | None = None,
    ) -> SizeObservation:
        crf = self._crf(crf)
        if type(actual) is not bool:
            raise ValueError("actual must be a boolean")
        if not math.isfinite(file_bytes) or file_bytes <= 0:
            raise ValueError("file_bytes must be finite and positive")
        if actual and type(file_bytes) is not int:
            raise ValueError("actual file_bytes must be an integer")
        observation = SizeObservation(
            crf, file_bytes, actual, dict(record) if record is not None else None
        )
        self.observations.append(observation)
        self.attempted.add(crf)
        return observation

    def best_actual(self) -> SizeObservation | None:
        """Return the lowest measured feasible CRF; estimates never qualify."""
        feasible = [
            point
            for point in self.observations
            if point.actual and point.file_bytes <= self.max_bytes
        ]
        return min(feasible, key=lambda point: (point.crf, -point.file_bytes)) if feasible else None

    def _points(self) -> dict[float, SizeObservation]:
        # Later estimates must never replace actual evidence at the same CRF.
        points = {}
        for point in self.observations:
            if point.actual or point.crf not in points or not points[point.crf].actual:
                points[point.crf] = point
        return points

    def _crossing(self, left: SizeObservation, right: SizeObservation) -> float | None:
        if left.crf >= right.crf or left.file_bytes <= right.file_bytes:
            return None
        log_left = math.log(left.file_bytes)
        slope = (math.log(right.file_bytes) - log_left) / (right.crf - left.crf)
        return left.crf + (math.log(self.max_bytes) - log_left) / slope

    @staticmethod
    def _nearest(choices: list[float], target: float) -> float:
        # At equal distance buy the lower-CRF candidate first.
        return min(choices, key=lambda crf: (abs(crf - target), crf))

    def propose(self) -> float | None:
        available = [crf for crf in self.grid if crf not in self.attempted]
        if not available:
            return None
        if self.grid[0] not in self.attempted:
            return self.grid[0]
        points = self._points()
        actuals = {crf: point for crf, point in points.items() if point.actual}
        best = self.best_actual()
        if best is not None:
            lower = [crf for crf in available if crf < best.crf]
            if not lower:
                return None
            oversized = [
                point
                for point in actuals.values()
                if point.crf < best.crf and point.file_bytes > self.max_bytes
            ]
            if oversized:
                left = max(oversized, key=lambda point: point.crf)
                interior = [crf for crf in lower if left.crf < crf < best.crf]
                if interior:
                    crossing = self._crossing(left, best)
                    target = crossing if crossing is not None else (left.crf + best.crf) / 2
                    return self._nearest(interior, target)
            # No interpolation bracket remains. Audit the still-unmeasured
            # lower grid, including settings below an oversized observation:
            # an unexpected feasible island must not be pruned away.
            return max(lower)

        # Actual measurements outrank estimates when they provide a secant.
        fitting = actuals if len(actuals) >= 2 else points
        ordered = sorted(fitting.values(), key=lambda point: point.crf)
        if len(ordered) >= 2:
            # Prefer a bracketing pair. Otherwise extrapolate the most recent
            # high-CRF pair; each new measured point refits the slope.
            pairs = list(zip(ordered, ordered[1:]))
            brackets = [
                pair for pair in pairs if pair[0].file_bytes > self.max_bytes >= pair[1].file_bytes
            ]
            left, right = brackets[0] if brackets else pairs[-1]
            crossing = self._crossing(left, right)
            if crossing is not None:
                return self._nearest(available, crossing)
            # A flat or increasing observed rate offers no useful inverse.
            # Explore the unmeasured upper range instead of inventing a slope.
            above = [crf for crf in available if crf > ordered[-1].crf]
            if above:
                return self._nearest(above, (ordered[-1].crf + self.grid[-1]) / 2)
        # The first oversized point cannot identify a rate/CRF slope. Buy the
        # adjacent setting to measure it, rather than assuming six CRF per octave.
        if ordered:
            above = [crf for crf in available if crf > ordered[-1].crf]
            if above:
                return min(above)
        return min(available)

    def summary(self) -> dict:
        actuals = [point for point in self.observations if point.actual]
        measured_crfs = {point.crf for point in actuals}
        latest = sorted(self._points().values(), key=lambda point: point.crf)
        latest_actuals = [point for point in latest if point.actual]
        warnings = []
        if any(
            right.file_bytes > left.file_bytes
            for left, right in zip(latest_actuals, latest_actuals[1:])
        ):
            warnings.append("actual_size_is_nonmonotonic")
        if any(
            len({point.file_bytes for point in actuals if point.crf == crf}) > 1
            for crf in measured_crfs
        ):
            warnings.append("actual_size_changed_at_same_crf")
        best = self.best_actual()
        unverified_lower = (
            [crf for crf in self.grid if crf < best.crf and crf not in measured_crfs]
            if best is not None
            else []
        )
        if best is not None:
            feasibility = "verified_feasible"
            optimality = "best_verified" if unverified_lower else "lowest_verified_feasible_on_grid"
        else:
            feasibility = (
                "verified_infeasible_on_grid"
                if len(measured_crfs) == len(self.grid)
                else "no_verified_feasible"
            )
            optimality = "unknown"
        return {
            "objective": "lowest_verified_feasible_crf",
            "allowed_crfs": list(self.grid),
            "attempted_crfs": sorted(self.attempted),
            "actual_crfs": sorted(measured_crfs),
            "unverified_lower_crfs": unverified_lower,
            "best_crf": best.crf if best is not None else None,
            "best_bytes": best.file_bytes if best is not None else None,
            "feasibility": feasibility,
            "optimality": optimality,
            "warnings": warnings,
        }
