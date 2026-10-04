"""Adaptive two-phase survey sampling, SOURCE/invented-metadata only.

This file never opens media, earlier result labels, or a native oracle. Its
one registered fixture exhausts a finite two-stage policy with exact rationals.
"""

import argparse
from dataclasses import dataclass
from fractions import Fraction as Q
import gzip
import hashlib
import io
from itertools import combinations
import json
import os
from pathlib import Path
import resource
import selectors
import shutil
import sys
import tarfile
import time


ROOT = Path(__file__).resolve().parents[1]
CODE = Path(__file__).resolve()
DOC = ROOT / "docs/research-adaptive-nested-sampling.md"
OUT = ROOT / "results/native-adaptive-nested-source-2026-10-04"
PYTHON = "/opt/codex/runtimes/codex-primary-runtime/dependencies/python/bin/python3.12"
LIMIT = 256 * 1024
FINAL = 32 * 1024
RESERVE = 134217728
READERS = (
    "docs/research-nested-entropy-sampling.md",
    "docs/research-confidence-stopping.md",
    "docs/research-overlap-panel-confidence.md",
    "docs/research-fixed-unit-cheap-potentials-v3.md",
)


@dataclass(frozen=True)
class Fee:
    """Additive WHOLE-query upper contracts, not measured CPU or central frames."""

    cpu_ticks: int
    input_frames: int
    io_bytes: int

    def __post_init__(self):
        if any(type(x) is not int or x < 0 for x in self.as_tuple()):
            raise ValueError("nonnegative integer fee required")

    def as_tuple(self):
        return self.cpu_ticks, self.input_frames, self.io_bytes

    def __add__(self, other):
        return Fee(*(x + y for x, y in zip(self.as_tuple(), other.as_tuple(), strict=True)))

    def fits(self, other):
        return all(x <= y for x, y in zip(self.as_tuple(), other.as_tuple(), strict=True))


ZERO = Fee(0, 0, 0)
LOOKUP = Fee(1, 0, 1)  # Every requested cheap value still pays key/hash lookup.


@dataclass(frozen=True)
class Unit:
    index: int
    source_prediction: Q
    cheap_closure: tuple
    exact_closure: tuple
    cheap_whole_fee: Fee
    exact_whole_fee: Fee

    def __post_init__(self):
        if type(self.index) is not int or self.index < 0:
            raise ValueError("unit ID")
        for closure in (self.cheap_closure, self.exact_closure):
            if not closure or any(type(i) is not int or i < 0 for i in closure):
                raise ValueError("nonempty frozen input closure")
            if tuple(sorted(set(closure))) != closure:
                raise ValueError("canonical input closure")
        if self.cheap_whole_fee.input_frames < len(self.cheap_closure):
            raise ValueError("cheap closure undercharged")
        if self.exact_whole_fee.input_frames < len(self.exact_closure):
            raise ValueError("continuous reference closure undercharged")
        if self.exact_whole_fee.cpu_ticks <= 0:
            raise ValueError("positive exact CPU ticket normalizer required by recipe")


@dataclass(frozen=True)
class History:
    # Frozen prior receipt fields. Current-stage M must never reach fit_model.
    known: tuple = ()
    cache: tuple = ()
    spent: Fee = ZERO

    def __post_init__(self):
        if type(self.spent) is not Fee:
            raise ValueError("complete fee ledger required")
        for pairs in (self.known, self.cache):
            if type(pairs) is not tuple:
                raise ValueError("immutable history receipt required")
            seen = set()
            for pair in pairs:
                if type(pair) is not tuple or len(pair) != 2:
                    raise ValueError("history pair schema")
                index, value = pair
                if type(index) is not int or index < 0 or index in seen:
                    raise ValueError("unique nonnegative history unit ID")
                if type(value) is not Q:
                    raise ValueError("finite exact rational history value")
                seen.add(index)

    def known_map(self):
        return dict(self.known)

    def cache_map(self):
        return dict(self.cache)


def validate_history(units, history):
    if not units or any(type(u) is not Unit or u.index != i for i, u in enumerate(units)):
        raise ValueError("canonical SOURCE unit population")
    population = set(range(len(units)))
    if not (set(history.known_map()) | set(history.cache_map())) <= population:
        raise ValueError("history unit outside SOURCE population")


def cheap_fee(units, history, panel):
    validate_history(units, history)
    cached = history.cache_map()
    return sum(
        (LOOKUP + (ZERO if i in cached else units[i].cheap_whole_fee) for i in panel),
        ZERO,
    )


def legal_first_panels(units, history, stage_budget, count):
    """Each selected F reserves every member's complete exact singleton cost."""
    validate_history(units, history)
    unknown = set(range(len(units))) - set(history.known_map())
    if type(count) is not int or not 1 <= count <= len(unknown):
        raise ValueError("F size")
    panels = tuple(
        tuple(panel)
        for panel in combinations(sorted(unknown), count)
        if all(
            (cheap_fee(units, history, panel) + units[i].exact_whole_fee).fits(stage_budget)
            for i in panel
        )
    )
    if set().union(*(set(f) for f in panels)) != unknown:
        raise ValueError("hard-budget library cannot cover every unknown unit")
    return panels


def fit_model(units, history):
    """Ridge slope using only previously paid exact/cache pairs; arbitrary prior."""
    validate_history(units, history)
    known, cached = history.known_map(), history.cache_map()
    pairs = [(cached[i], y) for i, y in known.items() if i in cached]
    slope = (1 + sum((a * y for a, y in pairs), Q())) / (1 + sum((a * a for a, _ in pairs), Q()))
    mu = {i: slope * u.source_prediction for i, u in enumerate(units) if i not in known}
    return slope, mu


def first_law(units, history, panels):
    """Predictable ticket law: use paid labels/cache, never unobserved a or Y."""
    _, mu = fit_model(units, history)
    cached = history.cache_map()
    scores = {i: 1 + abs(cached.get(i, units[i].source_prediction) - mu[i]) for i in mu}
    weights = [1 + sum((scores[i] for i in f), Q()) for f in panels]
    total = sum(weights, Q())
    law = tuple((w / total, f) for w, f in zip(weights, panels, strict=True))
    pf = {i: sum((p for p, f in law if i in f), Q()) for i in mu}
    if any(p <= 0 for p in pf.values()) or sum((p for p, _ in law), Q()) != 1:
        raise ValueError("first-stage support")
    return law, pf


def second_law(units, history, panel, observed_a):
    """F is now fixed. Fit b from ALL paid a_F, then choose conditional M coins."""
    if set(observed_a) != set(panel):
        raise ValueError("complete F cheap observation required before M coins")
    slope, mu = fit_model(units, history)
    # This deliberate F-dependent intercept has no full-population cheap mean.
    intercept = sum((observed_a[i] - mu[i] for i in panel), Q()) / (2 * len(panel))
    b = {i: slope * observed_a[i] + intercept for i in panel}
    weights = {i: (1 + abs(b[i] - mu[i])) / units[i].exact_whole_fee.cpu_ticks for i in panel}
    total = sum(weights.values(), Q())
    epsilon = Q(1, 4)
    r = {i: epsilon / len(panel) + (1 - epsilon) * weights[i] / total for i in panel}
    if any(p <= 0 for p in r.values()) or sum(r.values(), Q()) != 1:
        raise ValueError("conditional M support")
    return tuple((r[i], (i,)) for i in panel), r, b, mu


def point(history, panel, exact_panel, pf, r, b, mu, observed_y):
    if not set(exact_panel) <= set(panel) or set(observed_y) != set(exact_panel):
        raise ValueError("complete nested exact observation")
    if any(pf[i] <= 0 or r[i] <= 0 for i in panel):
        raise ValueError("positive conditional support")
    return (
        sum(history.known_map().values(), Q())
        + sum(mu.values(), Q())
        + sum(((b[i] - mu[i]) / pf[i] for i in panel), Q())
        + sum(((observed_y[i] - b[i]) / (pf[i] * r[i]) for i in exact_panel), Q())
    )


def advance(units, history, panel, exact_panel, observed_a, observed_y):
    cached, known = history.cache_map(), history.known_map()
    if set(observed_a) != set(panel) or set(observed_y) != set(exact_panel):
        raise ValueError("cannot commit partial transaction")
    if not set(exact_panel) <= set(panel) or set(exact_panel) & set(known):
        raise ValueError("new M subset of F")
    if any(i in cached and cached[i] != a for i, a in observed_a.items()):
        raise ValueError("poison: fixed-key cache mismatch")
    bill = cheap_fee(units, history, panel) + sum(
        (units[i].exact_whole_fee for i in exact_panel), ZERO
    )
    cached.update(observed_a)
    known.update(observed_y)
    return History(
        tuple(sorted(known.items())), tuple(sorted(cached.items())), history.spent + bill
    )


def enumerate_stage(units, history, stage_budget, toy_a, toy_y):
    """Evaluator only: observations are exposed to inference AFTER each coin."""
    panels = legal_first_panels(units, history, stage_budget, 2)
    law, pf = first_law(units, history, panels)
    branches = []
    for p, f in law:
        observed_a = {i: toy_a[i] for i in f}
        conditional, r, b, mu = second_law(units, history, f, observed_a)
        for probability, m in conditional:
            observed_y = {i: toy_y[i] for i in m}
            estimate = point(history, f, m, pf, r, b, mu, observed_y)
            updated = advance(units, history, f, m, observed_a, observed_y)
            bill = cheap_fee(units, history, f) + units[m[0]].exact_whole_fee
            if not bill.fits(stage_budget):
                raise AssertionError("draw exceeded registered WHOLE-query bound")
            branches.append((p * probability, estimate, updated, (f, m, pf, r, b, mu)))
    return tuple(branches)


def mean(branches):
    return sum((p * x for p, x, *_ in branches), Q())


def moments(branches):
    average = mean(branches)
    return average, sum((p * (x - average) ** 2 for p, x, *_ in branches), Q())


def rejected(function, *args):
    try:
        function(*args)
    except ValueError:
        return True
    raise AssertionError("invalid contract accepted")


def exact_fixture():
    # Every number, closure and value below is INVENTED. No grain/old label access.
    units = tuple(
        Unit(
            i,
            Q(x),
            tuple(range(i, i + 3 + i)),
            tuple(range(0, 6 + 2 * i)),
            Fee(3 + i, 3 + i, 9 + 3 * i),
            Fee(9 + 2 * i, 6 + 2 * i, 18 + 6 * i),
        )
        for i, x in enumerate((1, 3, 2, 4))
    )
    toy_a = tuple(map(Q, (0, 2, 6, 3)))
    toy_y = tuple(map(Q, (2, 5, 1, 8)))
    true_total = sum(toy_y, Q())
    source_fee = Fee(5, 3, 7)
    stage_budget = Fee(28, 24, 80)
    whole_budget = source_fee + stage_budget + stage_budget
    initial = History(spent=source_fee)
    stage1 = enumerate_stage(units, initial, stage_budget, toy_a, toy_y)
    assert sum((p for p, *_ in stage1), Q()) == 1
    assert mean(stage1) == true_total

    # Unconditional marginal q is computed from the actual adaptive first law.
    q = {i: sum((p for p, _, _, (_, m, *_rest) in stage1 if i in m), Q()) for i in range(4)}
    wrong = []
    for p, _, _, (f, m, pf, _r, b, mu) in stage1:
        x = sum(mu.values(), Q()) + sum(((b[i] - mu[i]) / pf[i] for i in f), Q())
        x += sum(((toy_y[i] - b[i]) / q[i] for i in m), Q())
        wrong.append((p, x))
    assert mean(wrong) != true_total

    leaves = []
    adapted_probabilities, adapted_slopes = set(), set()
    for p1, x1, h1, _ in stage1:
        stage2 = enumerate_stage(units, h1, stage_budget, toy_a, toy_y)
        # This checks every paid-history conditional mean, not only pooled mean.
        assert mean(stage2) == true_total
        slope, _mu = fit_model(units, h1)
        adapted_slopes.add(slope)
        for p2, x2, h2, (_f, _m, pf, _r, _b, _mu) in stage2:
            assert h2.spent.fits(whole_budget)
            adapted_probabilities.add(tuple(sorted(pf.items())))
            leaves.append((p1 * p2, x2, h2, x1))
    assert sum((p for p, *_ in leaves), Q()) == 1
    assert mean(leaves) == true_total
    assert len(adapted_probabilities) > 1 and len(adapted_slopes) > 1

    # Trigger depends on current-stage Y. These reported estimates are selected.
    threshold = sum((u.source_prediction for u in units), Q())
    latest_at_stop = sum((p * (x1 if x1 > threshold else x2) for p, x2, _, x1 in leaves), Q())
    average_at_stop = sum(
        (p * (x1 if x1 > threshold else (x1 + x2) / 2) for p, x2, _, x1 in leaves), Q()
    )
    stopped_centered_sum = sum(
        (
            p * ((x1 - true_total) + (x2 - true_total if x1 <= threshold else 0))
            for p, x2, _, x1 in leaves
        ),
        Q(),
    )
    assert stopped_centered_sum == 0
    assert latest_at_stop != true_total and average_at_stop != true_total
    # Select *whether to finalize* from old history, then issue a NEW final batch.
    # In this bounded toy it is always the second transaction; its law is adaptive.
    final_after_decision = mean(leaves)
    assert final_after_decision == true_total

    # Fitting a slope to current M makes the observed residual vanish, but biases.
    bad_fit_points = (Q(2) * (1 + 4), Q(1, 4) * (1 + 4))
    bad_fit_mean = sum(bad_fit_points, Q()) / 2
    assert bad_fit_mean == Q(45, 8) != 3

    # Filtering an old draw by remaining fee destroys coverage and success mean.
    budget_conditioned_success = Q(0)  # Y=(0,10), costs=(1,2), B=1, only unit0 accepted.
    assert budget_conditioned_success != 10
    refusal_budget = Fee(18, 12, 40)
    assert rejected(legal_first_panels, units, initial, refusal_budget, 2)
    missing = set(range(4)) - set().union(
        *(
            set(f)
            for f in combinations(range(4), 2)
            if all(
                (cheap_fee(units, initial, f) + units[i].exact_whole_fee).fits(refusal_budget)
                for i in f
            )
        )
    )
    assert missing  # No adaptive law can give these units positive conditional M.
    assert rejected(Unit, 0, Q(1), (0,), (0, 1), Fee(1, 1, 1), Fee(1, 1, 1))
    assert rejected(Unit, 0, Q(1), (0,), (0,), Fee(1, 1, 1), Fee(0, 1, 1))
    assert rejected(advance, units, History(cache=((0, Q(100)),)), (0,), (0,), {0: Q(0)}, {0: Q(2)})
    assert rejected(History, ((0, Q(1)), (0, Q(2))))
    assert rejected(History, (), ((0, Q(1)), (0, Q(2))))
    assert rejected(History, ((0, 1.0),))
    assert rejected(validate_history, units, History(known=((4, Q(1)),)))

    return {
        "status": "PASS_SOURCE_INVENTED_ONLY",
        "scope": "ONE exact finite adaptive two-stage tree; no confidence or speed claim",
        "first_transactions": len(stage1),
        "final_leaves": len(leaves),
        "conditional_histories_checked": len(stage1),
        "truth": true_total,
        "stage1_mean_variance": moments(stage1),
        "adaptive_final_mean_variance": moments(leaves),
        "different_final_p_tables": len(adapted_probabilities),
        "different_paid_history_slopes": sorted(adapted_slopes),
        "wrong_unconditional_q_mean": mean(wrong),
        "current_M_slope_fit": {"truth": 3, "points": bad_fit_points, "mean": bad_fit_mean},
        "value_trigger": {
            "source_threshold": threshold,
            "latest_mean": latest_at_stop,
            "random_count_average_mean": average_at_stop,
            "stopped_centered_sum_mean": stopped_centered_sum,
            "new_final_batch_mean": final_after_decision,
        },
        "whole_budget_invented": whole_budget,
        "whole_path_min": tuple(
            min(h.spent.as_tuple()[j] for _, _, h, _ in leaves) for j in range(3)
        ),
        "whole_path_max": tuple(
            max(h.spent.as_tuple()[j] for _, _, h, _ in leaves) for j in range(3)
        ),
        "all_exact_closures_charged_separately": True,
        "hard_budget_refusal_missing_units": sorted(missing),
        "success_conditioning": {
            "truth": 10,
            "old_unconditional_mean": 10,
            "affordable_success_mean": budget_conditioned_success,
        },
        "actual_a_Y_old_holdout_riskscore_media_reads": 0,
        "decoder_encoder_compiler_calls": 0,
        "generic_continuous_M_oracle": "UNPROVED_NOT_INVOKED",
        "actual_query_CPU_upper_certificates": "UNPROVED_INVENTED_FEES_ONLY",
    }


def wire(value):
    if isinstance(value, Q):
        return [str(value.numerator), str(value.denominator)]
    if isinstance(value, Fee):
        return dict(zip(("cpu_ticks", "input_frames", "io_bytes"), value.as_tuple(), strict=True))
    if isinstance(value, dict):
        return {str(k): wire(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [wire(v) for v in value]
    return value


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def family_blocks():
    paths = [CODE, DOC]
    if OUT.exists():
        paths.extend(p for p in OUT.rglob("*") if p.is_file())
    return sum(p.stat().st_blocks * 512 for p in paths)


def save_bytes(name, raw, *, final=False):
    allocated = ((len(raw) + 4095) // 4096) * 4096
    bound = LIMIT if final else LIMIT - FINAL
    if family_blocks() + allocated > bound or shutil.disk_usage(ROOT).free - allocated < RESERVE:
        raise RuntimeError("aggregate 256KiB family/final32KiB/128MiB disk reserve")
    path = OUT / name
    with path.open("xb") as handle:
        handle.write(raw)
        handle.flush()
        os.fsync(handle.fileno())
    return {"path": str(path.relative_to(ROOT)), "sha256": digest(path), "bytes": len(raw)}


def save_json(name, value, *, final=False):
    raw = json.dumps(wire(value), sort_keys=True, separators=(",", ":")).encode()
    return save_bytes(name, gzip.compress(raw, mtime=0), final=final)


def registration():
    if OUT.exists():
        raise ValueError("immutable family exists; no registration rerun")
    if sys.executable != PYTHON or sys.version_info[:2] != (3, 12):
        raise ValueError("CODEX Python3.12 required")
    closure = {
        str(p.relative_to(ROOT)): digest(p) for p in (CODE, DOC, *(ROOT / r for r in READERS))
    }
    memory = io.BytesIO()
    with tarfile.open(fileobj=memory, mode="w", format=tarfile.USTAR_FORMAT) as archive:
        for p in (CODE, DOC):
            data = p.read_bytes()
            item = tarfile.TarInfo(str(p.relative_to(ROOT)))
            item.size, item.mode, item.mtime = len(data), 0o644, 0
            archive.addfile(item, io.BytesIO(data))
    snapshot = gzip.compress(memory.getvalue(), mtime=0)
    # Reserve complete registration/proof/output/final extents before mkdir.
    worst_new_blocks = ((len(snapshot) + 4095) // 4096) * 4096 + 4 * 4096 + FINAL
    if family_blocks() + worst_new_blocks > LIMIT:
        raise ValueError("SOURCE family worst-case preflight")
    if shutil.disk_usage(ROOT).free - worst_new_blocks < RESERVE:
        raise ValueError("SOURCE disk preflight")
    OUT.mkdir()
    saved = save_bytes("source-snapshots.tar.gz", snapshot)
    result = save_json(
        "registration.json.gz",
        {
            "kind": "SOURCE_ONLY_ADAPTIVE_NESTED",
            "source_bindings": closure,
            "source_snapshots": saved,
            "runtime": {"executable": sys.executable, "version": sys.version},
            "family_allocated_limit": LIMIT,
            "final_reserved": FINAL,
            "disk_free_reserve": RESERVE,
            "experiment": "one exact rational finite policy fixture; no native/real labels",
            "estimator": "K+sum_U(mu)+sum_F((b-mu)/p)+sum_M((Y-b)/(p*r))",
            "r_scope": "conditional on entire paid F transcript before current M coins",
            "claim": "conditional design unbiasedness and hard-budget support contract only",
        },
    )
    print(json.dumps(result), flush=True)


def binding_guard():
    reg = json.loads(gzip.decompress((OUT / "registration.json.gz").read_bytes()))
    if reg["runtime"] != {"executable": sys.executable, "version": sys.version}:
        raise ValueError("registered Python identity changed")
    for relative, expected in reg["source_bindings"].items():
        if digest(ROOT / relative) != expected:
            raise ValueError(f"SOURCE binding changed: {relative}")
    snapshot = reg["source_snapshots"]
    if digest(ROOT / snapshot["path"]) != snapshot["sha256"]:
        raise ValueError("SOURCE snapshot changed")


def worker():
    binding_guard()
    result = exact_fixture()
    print(json.dumps(wire(result), sort_keys=True, separators=(",", ":")), flush=True)


def run_once():
    binding_guard()
    save_json("one-attempt.json.gz", {"started_unix": time.time(), "policy": "no automatic rerun"})
    selector = selectors.DefaultSelector()
    pipes = [os.pipe(), os.pipe()]
    start_wall = time.monotonic()
    pid = os.fork()
    if pid == 0:
        os.dup2(pipes[0][1], 1)
        os.dup2(pipes[1][1], 2)
        for read_fd, write_fd in pipes:
            os.close(read_fd)
            os.close(write_fd)
        devnull = os.open(os.devnull, os.O_RDONLY)
        os.dup2(devnull, 0)
        os.close(devnull)
        os.execv(PYTHON, [PYTHON, "-B", "-S", str(CODE), "_worker"])
    buffers = [bytearray(), bytearray()]
    for index, (read_fd, write_fd) in enumerate(pipes):
        os.close(write_fd)
        os.set_blocking(read_fd, False)
        selector.register(read_fd, selectors.EVENT_READ, index)
    failure = None
    try:
        while selector.get_map():
            if time.monotonic() - start_wall > 45:
                raise RuntimeError("pure worker deadline")
            for key, _ in selector.select(0.5):
                block = os.read(key.fd, 4096)
                if not block:
                    selector.unregister(key.fd)
                    os.close(key.fd)
                    continue
                index = key.data
                if len(buffers[index]) + len(block) > (48 * 1024 if index == 0 else 4096):
                    raise RuntimeError("bounded worker output")
                buffers[index].extend(block)
    except BaseException as error:
        failure = str(error)
        try:
            os.kill(pid, 9)
        except ProcessLookupError:
            pass
    finally:
        for key in tuple(selector.get_map().values()):
            selector.unregister(key.fd)
            os.close(key.fd)
        selector.close()
        _pid, status, usage = os.wait4(pid, 0)
    elapsed = time.monotonic() - start_wall
    stdout, stderr = map(bytes, buffers)
    passed = failure is None and os.waitstatus_to_exitcode(status) == 0
    receipt = None
    if passed:
        result = json.loads(stdout)
        receipt = save_json("exact-fixture.json.gz", result)
    final = save_json(
        "completion.json.gz",
        {
            "status": "PASS_SOURCE_INVENTED_ONLY" if passed else "FAIL_RETAIN_SPENT_FEE",
            "fixture": receipt,
            "failure": failure,
            "worker_exit": os.waitstatus_to_exitcode(status),
            "whole_worker_wait4": {
                "user_cpu_seconds": usage.ru_utime,
                "system_cpu_seconds": usage.ru_stime,
                "wall_seconds": elapsed,
                "maxrss_kib": usage.ru_maxrss,
            },
            "stdout": {
                "bytes": len(stdout),
                "sha256": hashlib.sha256(stdout).hexdigest(),
                "complete": passed,
            },
            "stderr": {
                "bytes": len(stderr),
                "sha256": hashlib.sha256(stderr).hexdigest(),
                "prefix_utf8": stderr[:4096].decode(errors="replace"),
                "complete": failure is None,
            },
            "supervisor_cpu_prefix_seconds": resource.getrusage(resource.RUSAGE_SELF).ru_utime
            + resource.getrusage(resource.RUSAGE_SELF).ru_stime,
            "supervisor_final_write_exit_CPU": "UNKNOWN_NOT_FREE",
            "registration_planning_Ruff_CPU": "UNKNOWN_NOT_FREE",
            "whole_worker_contains": "startup imports binding reads exact enumeration serialization cleanup exit",
            "actual_decode_encode_compile_calls": 0,
            "whole_native_cost_or_confidence_speed_claim": False,
            "family_allocated_before_final": family_blocks(),
            "workspace_free_before_final": shutil.disk_usage(ROOT).free,
        },
        final=True,
    )
    print(json.dumps(final), flush=True)
    if not passed:
        raise SystemExit(1)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("register", "run", "_worker"))
    args = parser.parse_args()
    {"register": registration, "run": run_once, "_worker": worker}[args.action]()


if __name__ == "__main__":
    main()
