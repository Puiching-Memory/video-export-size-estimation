"""Conformal envelopes for a frozen prediction trajectory, grouped by source.

Each calibration score is the maximum error over the entire registered
trajectory. One envelope therefore covers candidate selection and optional
stopping within that family. Coverage is marginal under exchangeability of
source groups; identifiers and provenance records do not prove that assumption.
"""

import json
import math
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal, localcontext
from fractions import Fraction
from pathlib import Path

from .runtime import canonical_key


CALIBRATION_SPLITS = {"calibration", "calibration_reserved"}
ASSUMPTION = (
    "New source groups and calibration source groups are exchangeable within the declared domain. "
    "The complete family and prediction model were frozen before calibration labels were used. "
    "Group identifiers and provenance document grouping; they do not establish independence "
    "or exchangeability."
)


def _string(value, name):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a nonempty string")
    return value


def _integer(value, name, minimum=1):
    if type(value) is not int or value < minimum:
        raise ValueError(f"{name} must be an integer >= {minimum}")
    return value


def _number(value, name, minimum=None, maximum=None):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be finite")
    try:
        finite = math.isfinite(value)
    except OverflowError:
        finite = False
    if not finite or (minimum is not None and value < minimum):
        raise ValueError(f"{name} must be finite and >= {minimum}")
    if maximum is not None and value > maximum:
        raise ValueError(f"{name} must be <= {maximum}")
    return value


def _json_copy(value, name):
    """Reject noncanonical objects and nonfinite numbers before hashing."""

    def check(item):
        if isinstance(item, dict):
            if not all(isinstance(key, str) for key in item):
                raise ValueError(f"{name} requires string dictionary keys")
            for child in item.values():
                check(child)
        elif isinstance(item, (list, tuple)):
            for child in item:
                check(child)
        elif isinstance(item, float):
            _number(item, name)
        elif item is not None and not isinstance(item, (str, int, bool)):
            raise ValueError(f"{name} must contain JSON values")

    check(value)
    return json.loads(json.dumps(value, allow_nan=False))


def make_family(
    configuration: dict,
    crf_grid: list[float],
    sample_seconds: float,
    probe_policy: dict,
    seed_strategy: dict,
    toolchain_key: str,
    threads: int,
    *,
    variants=("original",),
    model_policy: str = "visual-stratified-residual-v4",
    prefix_policy: dict | None = None,
):
    """Describe every setting to which a trajectory envelope may apply.

    ``configuration`` is the full export configuration, excluding source. Its
    current CRF may be present, but must belong to ``crf_grid``. The CRF grid,
    rather than its currently selected member, is hashed into the family.
    ``probe_policy`` must include the actual context/padding parameters; this
    module compares their full JSON representation, rather than interpreting
    their encoder semantics.
    """
    if not isinstance(configuration, dict) or not configuration:
        raise ValueError("configuration must be a nonempty dictionary")
    configuration = _json_copy(configuration, "configuration")
    if "source" in configuration:
        raise ValueError("source belongs to provenance, not the export configuration")
    if not isinstance(crf_grid, (list, tuple)) or not crf_grid:
        raise ValueError("crf_grid must be nonempty")
    grid = [float(_number(crf, "crf", 0, 51)) for crf in crf_grid]
    if len(set(grid)) != len(grid):
        raise ValueError("crf_grid must not contain duplicate candidates")
    grid.sort()
    if "crf" in configuration:
        current = float(_number(configuration.pop("crf"), "crf", 0, 51))
        if current not in grid:
            raise ValueError("configuration CRF is outside the registered family")
    seconds = float(_number(sample_seconds, "sample_seconds", 0))
    if not seconds:
        raise ValueError("sample_seconds must be positive")
    for value, name in [(probe_policy, "probe_policy"), (seed_strategy, "seed_strategy")]:
        if not isinstance(value, dict) or not value:
            raise ValueError(f"{name} must be a nonempty dictionary")
    if not isinstance(variants, (list, tuple)) or not variants:
        raise ValueError("variants must be nonempty")
    variant_ids = [_string(variant, "variant") for variant in variants]
    if len(set(variant_ids)) != len(variant_ids):
        raise ValueError("variants must not contain duplicates")
    if prefix_policy is None:
        prefix_policy = {
            "kind": "all_prefixes_up_to_maximum",
            "minimum_prefix": 1,
            "maximum_prefix": 1,
        }
    if not isinstance(prefix_policy, dict) or not prefix_policy:
        raise ValueError("prefix_policy must be a nonempty dictionary")
    if set(prefix_policy) != {"kind", "minimum_prefix", "maximum_prefix"}:
        raise ValueError("prefix_policy requires an explicit finite registered prefix range")
    if prefix_policy["kind"] != "all_prefixes_up_to_maximum":
        raise ValueError("unsupported prefix registration policy")
    minimum = _integer(prefix_policy["minimum_prefix"], "minimum_prefix")
    maximum = _integer(prefix_policy["maximum_prefix"], "maximum_prefix", 0)
    if maximum and maximum < minimum:
        raise ValueError("maximum_prefix must reach minimum_prefix or be zero")
    return {
        "family_schema": 1,
        "model_policy": _string(model_policy, "model_policy"),
        "configuration": configuration,
        "crf_grid": grid,
        "sample_seconds": seconds,
        "probe_policy": _json_copy(probe_policy, "probe_policy"),
        "seed_strategy": _json_copy(seed_strategy, "seed_strategy"),
        "toolchain_key": _string(toolchain_key, "toolchain_key"),
        "threads": _integer(threads, "threads"),
        "variants": sorted(variant_ids),
        "prefix_policy": _json_copy(prefix_policy, "prefix_policy"),
    }


def _validated_family(family):
    if not isinstance(family, dict) or family.get("family_schema") != 1:
        raise ValueError("unsupported trajectory family")
    required = {
        "family_schema",
        "configuration",
        "crf_grid",
        "sample_seconds",
        "probe_policy",
        "seed_strategy",
        "toolchain_key",
        "threads",
        "variants",
        "model_policy",
        "prefix_policy",
    }
    if (
        set(family) != required
        or not isinstance(family["configuration"], dict)
        or "crf" in family["configuration"]
    ):
        raise ValueError("family must have exactly the registered fields and CRF grid")
    canonical = make_family(
        family["configuration"],
        family["crf_grid"],
        family["sample_seconds"],
        family["probe_policy"],
        family["seed_strategy"],
        family["toolchain_key"],
        family["threads"],
        variants=family["variants"],
        model_policy=family["model_policy"],
        prefix_policy=family["prefix_policy"],
    )
    if canonical != family:
        raise ValueError("family must use the canonical sorted representation")
    return canonical


def family_key(family: dict):
    """Hash the entire frozen family, including its candidate grid."""
    return canonical_key(_validated_family(family))


def matches_family(family: dict, configuration: dict):
    """Check a full runtime export configuration, including its selected CRF."""
    family = _validated_family(family)
    if not isinstance(configuration, dict) or "crf" not in configuration:
        return False
    try:
        candidate = _json_copy(configuration, "configuration")
        crf = float(_number(candidate.pop("crf"), "crf", 0, 51))
    except ValueError:
        return False
    return crf in family["crf_grid"] and candidate == family["configuration"]


def _provenance(value, sources, splits):
    if not isinstance(value, dict):
        raise ValueError("each group requires recorded provenance")
    value = _json_copy(value, "group provenance")
    _string(value.get("grouping_basis"), "grouping_basis")
    recorded_sources = value.get("source_ids")
    if (
        not isinstance(recorded_sources, list)
        or not all(isinstance(source, str) and source for source in recorded_sources)
        or len(set(recorded_sources)) != len(recorded_sources)
        or set(recorded_sources) != set(sources)
    ):
        raise ValueError("group provenance must identify exactly its registered sources")
    if not splits or not splits <= CALIBRATION_SPLITS:
        raise ValueError("only calibration splits may supply calibration scores")
    if "split" in value and value["split"] not in splits:
        raise ValueError("group provenance split does not match calibration rows")
    value["source_ids"] = sorted(recorded_sources)
    return value


def _registered_prefixes(family, source_blocks):
    source_blocks = _integer(source_blocks, "source_blocks")
    policy = family["prefix_policy"]
    return tuple(range(policy["minimum_prefix"], min(policy["maximum_prefix"], source_blocks) + 1))


def generate_profile(rows, domain: str, family: dict, *, group_provenance: dict):
    """Reduce complete, group-labelled calibration trajectories to one score each.

    Each row requires ``group_id``, ``source_id``, ``split``, ``variant_id``,
    ``crf``, ``prefix``, ``source_blocks``, ``expected_prefixes``, ``predicted_bytes``, and
    ``actual_bytes``. ``expected_prefixes`` records the preregistered complete
    prefix set for that source/variant/CRF. ``source_blocks`` must come from its
    frozen source plan or manifest, not from the observed stopping point.
    All family variants and CRFs must
    appear for each source, and missing or duplicate prefixes are rejected.

    Provenance requires ``grouping_basis`` and the exact ``source_ids`` per
    group. These are recorded assertions, not a proof of independent sampling.
    """
    family = _validated_family(family)
    domain = _string(domain, "domain")
    if not isinstance(group_provenance, dict):
        raise ValueError("group_provenance must be a dictionary")
    trajectories = {}
    groups = {}
    source_groups = {}
    source_variant_blocks = {}
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("calibration rows must be dictionaries")
        group = _string(row.get("group_id"), "group_id")
        source = _string(row.get("source_id"), "source_id")
        split = row.get("split")
        if not isinstance(split, str) or split not in CALIBRATION_SPLITS:
            raise ValueError("test/development rows cannot calibrate a trajectory envelope")
        if source in source_groups and source_groups[source] != group:
            raise ValueError("the same source cannot be assigned to multiple calibration groups")
        source_groups[source] = group
        variant = row.get("variant_id")
        if variant not in family["variants"]:
            raise ValueError("row variant is outside the registered family")
        crf = float(_number(row.get("crf"), "row crf", 0, 51))
        if crf not in family["crf_grid"]:
            raise ValueError("row CRF is outside the registered family")
        prefix = _integer(row.get("prefix"), "prefix")
        source_blocks = _integer(row.get("source_blocks"), "source_blocks")
        source_variant = (group, source, variant)
        prior_blocks = source_variant_blocks.setdefault(source_variant, source_blocks)
        if prior_blocks != source_blocks:
            raise ValueError("source block count must remain fixed across prefixes and CRFs")
        expected = row.get("expected_prefixes")
        if not isinstance(expected, (list, tuple)) or not expected:
            raise ValueError("expected_prefixes must register the complete trajectory")
        expected = tuple(_integer(value, "expected prefix") for value in expected)
        if expected != _registered_prefixes(family, source_blocks):
            raise ValueError("expected_prefixes must include every registered sampling prefix")
        if prefix not in expected:
            raise ValueError("observed prefix is outside the registered trajectory")
        predicted = _number(row.get("predicted_bytes"), "predicted_bytes", 0)
        if not predicted:
            raise ValueError("predicted_bytes must be positive")
        actual = _integer(row.get("actual_bytes"), "actual_bytes")
        with localcontext() as context:
            # Avoid cancellation of two large floating-point logarithms and
            # conservatively round nonzero stored scores toward infinity.
            context.prec = max(50, len(Decimal(actual).as_tuple().digits) + 30)
            decimal_score = abs((Decimal(predicted) / Decimal(actual)).ln())
        score = float(decimal_score)
        if decimal_score:
            score = math.nextafter(score, math.inf)
        if "family_key" in row and row["family_key"] != family_key(family):
            raise ValueError("calibration row belongs to a different family")
        if "configuration" in row:
            if (
                not matches_family(family, row["configuration"])
                or row["configuration"].get("crf") != crf
            ):
                raise ValueError("row configuration does not match its registered candidate")
        trajectory_key = (group, source, variant, crf)
        record = trajectories.setdefault(
            trajectory_key,
            {
                "expected": expected,
                "source_blocks": source_blocks,
                "actual": actual,
                "prefixes": set(),
            },
        )
        if record["expected"] != expected or record["actual"] != actual:
            raise ValueError("trajectory registration and reference bytes must remain fixed")
        if prefix in record["prefixes"]:
            raise ValueError("duplicate calibration trajectory prefix")
        record["prefixes"].add(prefix)
        group_record = groups.setdefault(
            group, {"score": 0.0, "sources": set(), "splits": set(), "rows": 0}
        )
        group_record["score"] = max(group_record["score"], score)
        group_record["sources"].add(source)
        group_record["splits"].add(split)
        group_record["rows"] += 1
    if not groups:
        raise ValueError("calibration requires at least one registered source group")
    for record in trajectories.values():
        if record["prefixes"] != set(record["expected"]):
            raise ValueError("calibration trajectory is missing registered prefixes")
    for source, group in source_groups.items():
        for variant in family["variants"]:
            for crf in family["crf_grid"]:
                if (group, source, variant, crf) not in trajectories:
                    raise ValueError(
                        "calibration is missing a registered variant or CRF trajectory"
                    )
    records = []
    for group, record in sorted(groups.items()):
        provenance = _provenance(group_provenance.get(group), record["sources"], record["splits"])
        records.append(
            {
                "group_id": group,
                "absolute_log_error": record["score"],
                "source_ids": sorted(record["sources"]),
                "splits": sorted(record["splits"]),
                "row_count": record["rows"],
                "trajectory_count": sum(key[0] == group for key in trajectories),
                "prefix_registration": [
                    {
                        "source_id": key[1],
                        "variant_id": key[2],
                        "crf": key[3],
                        "source_blocks": trajectory["source_blocks"],
                        "expected_prefixes": list(trajectory["expected"]),
                    }
                    for key, trajectory in sorted(trajectories.items())
                    if key[0] == group
                ],
                "provenance": provenance,
            }
        )
    return {
        "family_key": family_key(family),
        "family": family,
        "domain": domain,
        "score_definition": "group_max_absolute_log_error_over_registered_trajectories",
        "assumption": ASSUMPTION,
        "independence_status": "provenance_declared_not_verified",
        "groups": records,
    }


class TrajectoryCalibration:
    """Read schema-v2 bundles without relaxing their registered family."""

    def __init__(self, path: Path | None = None):
        self.profiles = {}
        if path is None:
            return
        payload = json.loads(Path(path).read_text())
        if payload.get("schema_version") != 2 or not isinstance(payload.get("profiles"), list):
            raise ValueError("unsupported trajectory calibration schema")
        for profile in payload["profiles"]:
            if not isinstance(profile, dict):
                raise ValueError("trajectory profile must be a dictionary")
            family = _validated_family(profile.get("family"))
            key = family_key(family)
            if profile.get("family_key") != key or key in self.profiles:
                raise ValueError("family key mismatch or duplicate trajectory profile")
            _string(profile.get("domain"), "domain")
            if profile.get("score_definition") != (
                "group_max_absolute_log_error_over_registered_trajectories"
            ):
                raise ValueError("unsupported trajectory score definition")
            if profile.get("assumption") != ASSUMPTION:
                raise ValueError("trajectory profile must declare its exchangeability assumptions")
            if profile.get("independence_status") != "provenance_declared_not_verified":
                raise ValueError("identifiers cannot establish source-group independence")
            records = profile.get("groups")
            if not isinstance(records, list) or not records:
                raise ValueError("trajectory calibration needs recorded source groups")
            identifiers = set()
            sources = set()
            for record in records:
                if not isinstance(record, dict):
                    raise ValueError("calibration group must be a dictionary")
                group = _string(record.get("group_id"), "group_id")
                if group in identifiers:
                    raise ValueError("duplicate calibration group")
                identifiers.add(group)
                _number(record.get("absolute_log_error"), "absolute_log_error", 0)
                _integer(record.get("row_count"), "row_count")
                _integer(record.get("trajectory_count"), "trajectory_count")
                group_sources = record.get("source_ids")
                splits = record.get("splits")
                if (
                    not isinstance(group_sources, list)
                    or not group_sources
                    or not all(isinstance(source, str) and source for source in group_sources)
                    or len(set(group_sources)) != len(group_sources)
                    or not isinstance(splits, list)
                    or not all(isinstance(split, str) for split in splits)
                ):
                    raise ValueError("source IDs and splits must be recorded per group")
                _provenance(record.get("provenance"), group_sources, set(splits))
                expected_trajectories = (
                    len(group_sources) * len(family["variants"]) * len(family["crf_grid"])
                )
                if (
                    record["trajectory_count"] != expected_trajectories
                    or record["row_count"] < expected_trajectories
                ):
                    raise ValueError("group counts do not cover the complete registered family")
                registrations = record.get("prefix_registration")
                if (
                    not isinstance(registrations, list)
                    or len(registrations) != expected_trajectories
                ):
                    raise ValueError("group must record every registered source trajectory")
                registration_keys = set()
                registered_rows = 0
                block_counts = {}
                for registration in registrations:
                    if not isinstance(registration, dict):
                        raise ValueError("prefix registration must be a dictionary")
                    source = registration.get("source_id")
                    variant = registration.get("variant_id")
                    crf = registration.get("crf")
                    if (
                        source not in group_sources
                        or variant not in family["variants"]
                        or crf not in family["crf_grid"]
                    ):
                        raise ValueError("prefix registration is outside the family")
                    registration_key = (source, variant, crf)
                    if registration_key in registration_keys:
                        raise ValueError("duplicate registered source trajectory")
                    registration_keys.add(registration_key)
                    blocks = _integer(registration.get("source_blocks"), "source_blocks")
                    prior_blocks = block_counts.setdefault((source, variant), blocks)
                    if prior_blocks != blocks:
                        raise ValueError("source block counts differ across candidate CRFs")
                    prefixes = _registered_prefixes(family, blocks)
                    if not prefixes or registration.get("expected_prefixes") != list(prefixes):
                        raise ValueError("group omits or changes registered prefixes")
                    registered_rows += len(prefixes)
                if record["row_count"] != registered_rows:
                    raise ValueError("group row count differs from complete prefix registration")
                if sources.intersection(group_sources):
                    raise ValueError("the same source cannot calibrate multiple groups")
                sources.update(group_sources)
            self.profiles[key] = profile

    def interval(
        self,
        key: str,
        prediction: int | float,
        coverage: float,
        *,
        sample_count: int | None = None,
        configuration: dict | None = None,
    ):
        """Return an envelope for the family, or None when evidence is insufficient.

        ``sample_count`` is required to issue a certificate; omitting it or
        observing outside the registered range returns None. A caller selecting
        a runtime candidate should supply its complete
        ``configuration`` as an additional membership check. The family key
        itself must be recomputed when policy, build, seed strategy, or any
        registered family field changes.
        """
        _number(coverage, "coverage", 0, 1)
        if not 0 < coverage < 1:
            raise ValueError("coverage must be between zero and one")
        _number(prediction, "prediction", 0)
        if not prediction:
            raise ValueError("prediction must be positive")
        profile = self.profiles.get(key)
        if profile is None:
            return None
        policy = profile["family"]["prefix_policy"]
        if (
            type(sample_count) is not int
            or not policy["minimum_prefix"] <= sample_count <= policy["maximum_prefix"]
        ):
            return None
        if configuration is not None and not matches_family(profile["family"], configuration):
            return None
        scores = sorted(group["absolute_log_error"] for group in profile["groups"])
        rank = math.ceil((len(scores) + 1) * coverage)
        if rank > len(scores):
            return None
        radius = scores[rank - 1]
        if radius > 700:
            return None
        try:
            with localcontext() as context:
                decimal_prediction = Decimal(prediction)
                context.prec = max(50, len(decimal_prediction.as_tuple().digits) + 340)
                decimal_radius = Decimal(radius)
                low = decimal_prediction * (-decimal_radius).exp()
                high = decimal_prediction * decimal_radius.exp()
                if radius:
                    low, high = low.next_minus(), high.next_plus()
                interval = [
                    max(1, int(low.to_integral_value(rounding=ROUND_FLOOR))),
                    int(high.to_integral_value(rounding=ROUND_CEILING)),
                ]
        except (OverflowError, ValueError):
            return None
        return {
            "kind": "trajectory_split_conformal",
            "coverage": coverage,
            "interval_bytes": interval,
            "calibration_groups": len(scores),
            "conformal_rank": rank,
            "absolute_log_error_radius": radius,
            "family_key": key,
            "sample_count": sample_count,
            "prefix_policy": dict(policy),
            "domain": profile["domain"],
            "scope": "all_registered_variants_crfs_and_sample_prefixes",
            "assumption": ASSUMPTION,
            "independence_status": "provenance_declared_not_verified",
        }


def harmonic_point(interval: list[int]):
    """Choose the integer point minimizing worst relative error on [low, high].

    This is a separate presentation choice and never overwrites the calibrated
    model prediction. Integer rounding is evaluated against both endpoints.
    """
    if not isinstance(interval, (list, tuple)) or len(interval) != 2:
        raise ValueError("interval must contain two positive integer endpoints")
    low = _integer(interval[0], "interval low")
    high = _integer(interval[1], "interval high")
    if high < low:
        raise ValueError("interval endpoints are reversed")
    value = Fraction(2 * low * high, low + high)
    floor = value.numerator // value.denominator
    candidates = {floor, min(high, floor + 1)}
    return min(
        candidates,
        key=lambda point: (max(Fraction(point - low, low), Fraction(high - point, high)), point),
    )
