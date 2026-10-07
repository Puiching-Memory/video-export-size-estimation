"""One independent exact reconstruction; never import or execute author code."""

import argparse
from fractions import Fraction as F
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
OUT = ROOT / "results/native-adaptive-nested-independent-review-2026-10-04"
PY = "/opt/codex/runtimes/codex-primary-runtime/dependencies/python/bin/python3.12"
CAP, FINAL, DISK = 131072, 16384, 134217728
BINDINGS = {
    "benchmarks/native_adaptive_nested_sampling.py": "ad5f482920e0d7d472b3add7c59e37d395e3bef12be40ef3f07788ca37b9d6a1",
    "docs/research-adaptive-nested-sampling.md": "71b4d766fc6fe00055a3058d9b79f005619ae928e335c4a23bb97c84b97ab935",
    "results/native-adaptive-nested-source-2026-10-04/registration.json.gz": "0d2eadc844a5c35f96f95be233318ea6efe70c544c72115f173af7d11200aad7",
    "results/native-adaptive-nested-source-2026-10-04/completion.json.gz": "855aec76681fe0a1ef22f096e6af6165b9b9f8bc7d9c4e58c89c901136b5ad31",
}


def digest(data):
    return hashlib.sha256(data).hexdigest()


def wire(value):
    if isinstance(value, F):
        return [str(value.numerator), str(value.denominator)]
    if isinstance(value, dict):
        return {str(k): wire(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [wire(v) for v in value]
    return value


def encoded(value):
    return json.dumps(wire(value), sort_keys=True, separators=(",", ":")).encode()


def family_bytes():
    paths = [CODE]
    if OUT.exists():
        paths.extend(OUT.rglob("*"))
        paths.append(OUT)
    return sum(p.stat().st_blocks * 512 for p in paths)


def persist(name, raw, final=False):
    allocation = ((len(raw) + 4095) // 4096) * 4096
    if family_bytes() + allocation > CAP - (0 if final else FINAL):
        raise ValueError("canonical/snapshots family allocation exceeds registered bound")
    if shutil.disk_usage(ROOT).free - allocation < DISK:
        raise ValueError("128MiB disk reserve unavailable")
    path = OUT / name
    with path.open("xb") as handle:
        handle.write(raw)
        handle.flush()
        os.fsync(handle.fileno())
    return {"path": str(path.relative_to(ROOT)), "sha256": digest(raw), "bytes": len(raw)}


def store(name, value, final=False):
    return persist(name, gzip.compress(encoded(value), mtime=0), final)


def identity():
    if sys.executable != PY or sys.version_info[:2] != (3, 12):
        raise ValueError("CODEX Python3.12 required")
    if not sys.dont_write_bytecode or not sys.flags.no_site or os.environ.get("LANG") != "C":
        raise ValueError("registered -B -S LANG=C runtime required")
    for relative, expected in BINDINGS.items():
        if digest((ROOT / relative).read_bytes()) != expected:
            raise ValueError("frozen source identity changed: " + relative)


def register():
    identity()
    if OUT.exists():
        raise ValueError("single immutable family already exists")
    memory = io.BytesIO()
    snapshot_paths = [CODE, *(ROOT / p for p in BINDINGS)]
    with tarfile.open(fileobj=memory, mode="w", format=tarfile.USTAR_FORMAT) as archive:
        for path in snapshot_paths:
            raw = path.read_bytes()
            item = tarfile.TarInfo(str(path.relative_to(ROOT)))
            item.size, item.mtime, item.mode = len(raw), 0, 0o644
            archive.addfile(item, io.BytesIO(raw))
    snapshot = gzip.compress(memory.getvalue(), mtime=0)
    worst_new = ((len(snapshot) + 4095) // 4096) * 4096 + 65536 + FINAL
    if family_bytes() + worst_new > CAP or shutil.disk_usage(ROOT).free - worst_new < DISK:
        raise ValueError("complete-family preflight refused")
    OUT.mkdir()
    receipt = persist("source-snapshots.tar.gz", snapshot)
    registration = store(
        "registration.json.gz",
        {
            "kind": "INDEPENDENT_EXACT_RECONSTRUCTION_NOT_AUTHOR_EXECUTION",
            "reviewer_sha256": digest(CODE.read_bytes()),
            "bindings": BINDINGS,
            "snapshots": receipt,
            "runtime": {"executable": sys.executable, "version": sys.version},
            "family_limit": CAP,
            "final_reserve": FINAL,
            "disk_reserve": DISK,
            "worker_cpu_limit_seconds": 5,
            "plan": [
                "reconstruct author numeric policy from independent Fraction functions",
                "12 first histories, six final branches per history, exact probabilities",
                "each-history mean, total-variance decomposition, all whole fee vectors",
                "wrong marginal-q and current-Y stop counterexamples on same tree",
                "new current-M-fit and fee-selective counterexamples",
                "same-history fixed-b/r and cheap-free fixed final comparisons",
            ],
            "forbidden": "author imports/proof runs, media/codec/compiler, actual a/Y, DEV/holdout riskscores",
            "scope": "invented finite point-estimation proof; no native cost or CI evidence",
        },
    )
    print(json.dumps(registration))


def guard():
    identity()
    registration = json.loads(gzip.decompress((OUT / "registration.json.gz").read_bytes()))
    if registration["reviewer_sha256"] != digest(CODE.read_bytes()):
        raise ValueError("independent reconstruction changed after freeze")
    snapshot = registration["snapshots"]
    if digest((ROOT / snapshot["path"]).read_bytes()) != snapshot["sha256"]:
        raise ValueError("frozen snapshot changed")


def add(*vectors):
    return tuple(sum(v[j] for v in vectors) for j in range(3))


def fits(vector, bound):
    return all(x <= y for x, y in zip(vector, bound, strict=True))


def average(rows, key="z"):
    return sum((row["prob"] * row[key] for row in rows), F())


def variance(rows):
    mean = average(rows)
    return sum((row["prob"] * (row["z"] - mean) ** 2 for row in rows), F())


def reconstruct():
    # These are the published INVENTED metadata, not actual export observations.
    source = tuple(map(F, (1, 3, 2, 4)))
    auxiliary = tuple(map(F, (0, 2, 6, 3)))
    target = tuple(map(F, (2, 5, 1, 8)))
    cheap = tuple((3 + i, 3 + i, 9 + 3 * i) for i in range(4))
    exact = tuple((9 + 2 * i, 6 + 2 * i, 18 + 6 * i) for i in range(4))
    source_fee, stage_cap, whole_cap = (5, 3, 7), (28, 24, 80), (61, 51, 167)
    truth = sum(target, F())

    def cheap_charge(panel, cache):
        return add(*((1, 0, 1) if i in cache else add((1, 0, 1), cheap[i]) for i in panel))

    def panels(known, cache, cap):
        unknown = set(range(4)) - set(known)
        law_support = [
            f
            for f in combinations(sorted(unknown), 2)
            if all(fits(add(cheap_charge(f, cache), exact[i]), cap) for i in f)
        ]
        covered = set().union(*(set(f) for f in law_support))
        return law_support, unknown - covered

    def model(known, cache):
        matched = [(cache[i], y) for i, y in known.items() if i in cache]
        coefficient = (1 + sum((a * y for a, y in matched), F())) / (
            1 + sum((a * a for a, _ in matched), F())
        )
        prediction = {i: coefficient * source[i] for i in range(4) if i not in known}
        return coefficient, prediction

    def stage(known, cache, spent, fixed=False):
        coefficient, prediction = model(known, cache)
        support, missing = panels(known, cache, stage_cap)
        assert not missing
        score = {i: 1 + abs(cache.get(i, source[i]) - prediction[i]) for i in prediction}
        masses = [1 + sum((score[i] for i in f), F()) for f in support]
        normalizer = sum(masses, F())
        first = {f: mass / normalizer for f, mass in zip(support, masses, strict=True)}
        marginal = {i: sum((pf for f, pf in first.items() if i in f), F()) for i in prediction}
        assert sum(first.values(), F()) == 1 and all(p > 0 for p in marginal.values())
        rows = []
        first_variance = second_variance = F()
        for panel, pf in first.items():
            # The ONLY current observation given to b/r is the complete a_F slice.
            a_paid = {i: auxiliary[i] for i in panel}
            intercept = F() if fixed else sum((a_paid[i] - prediction[i] for i in panel), F()) / 4
            b = {i: coefficient * a_paid[i] + intercept for i in panel}
            weight = {i: (1 + abs(b[i] - prediction[i])) / exact[i][0] for i in panel}
            total_weight = sum(weight.values(), F())
            conditional = {
                i: F(1, 2) if fixed else F(1, 8) + F(3, 4) * weight[i] / total_weight for i in panel
            }
            assert sum(conditional.values(), F()) == 1 and min(conditional.values()) > 0
            predictable_part = sum(known.values(), F()) + sum(prediction.values(), F())
            predictable_part += sum(((b[i] - prediction[i]) / marginal[i] for i in panel), F())
            conditional_mean = sum(known.values(), F()) + sum(prediction.values(), F())
            conditional_mean += sum(((target[i] - prediction[i]) / marginal[i] for i in panel), F())
            first_variance += pf * (conditional_mean - truth) ** 2
            conditional_variance = F()
            for selected, rm in conditional.items():
                # Open current Y only AFTER b and conditional tickets are frozen.
                z = predictable_part + (target[selected] - b[selected]) / (marginal[selected] * rm)
                stage_fee = add(cheap_charge(panel, cache), exact[selected])
                assert fits(stage_fee, stage_cap)
                updated_known = {**known, selected: target[selected]}
                updated_cache = {**cache, **a_paid}
                row = {
                    "prob": pf * rm,
                    "z": z,
                    "F": panel,
                    "M": selected,
                    "p": marginal,
                    "r": conditional,
                    "b": b,
                    "mu": prediction,
                    "known": updated_known,
                    "cache": updated_cache,
                    "fee": add(spent, stage_fee),
                    "stage_fee": stage_fee,
                }
                rows.append(row)
                conditional_variance += rm * (z - conditional_mean) ** 2
            second_variance += pf * conditional_variance
        assert sum((row["prob"] for row in rows), F()) == 1
        assert average(rows) == truth
        assert variance(rows) == first_variance + second_variance
        return rows, (first_variance, second_variance)

    first, first_decomposition = stage({}, {}, source_fee)
    assert len(first) == 12
    q = {i: sum((row["prob"] for row in first if row["M"] == i), F()) for i in range(4)}
    wrong_q = sum(
        (
            row["prob"]
            * (
                sum(row["mu"].values(), F())
                + sum(((row["b"][i] - row["mu"][i]) / row["p"][i] for i in row["F"]), F())
                + (target[row["M"]] - row["b"][row["M"]]) / q[row["M"]]
            )
            for row in first
        ),
        F(),
    )
    assert wrong_q != truth
    leaves, history_rows, fixed_leaves, cheap_free_leaves = [], [], [], []
    for index, h in enumerate(first):
        final, decomposition = stage(h["known"], h["cache"], h["fee"])
        fixed_final, fixed_decomposition = stage(h["known"], h["cache"], h["fee"], fixed=True)
        assert len(final) == len(fixed_final) == 6
        unknown = set(range(4)) - set(h["known"])
        _, mu = model(h["known"], h["cache"])
        base = sum(h["known"].values(), F()) + sum(mu.values(), F())
        cheap_free = [
            {
                "prob": F(1, len(unknown)),
                "z": base + len(unknown) * (target[i] - mu[i]),
                "fee": add(h["fee"], exact[i]),
                "stage_fee": exact[i],
            }
            for i in sorted(unknown)
        ]
        assert average(cheap_free) == truth
        history_rows.append(
            {
                "history": index,
                "first_F_M": (h["F"], h["M"]),
                "history_probability": h["prob"],
                "final_mean": average(final),
                "final_variance_components": decomposition,
                "fixed_b_uniform_r_variance_components": fixed_decomposition,
                "cheap_free_exact_singleton_variance": variance(cheap_free),
                "final_min_path_p_times_r": min(
                    row["p"][row["M"]] * row["r"][row["M"]] for row in final
                ),
                "final_reserve_vector": tuple(
                    max(row["stage_fee"][j] for row in final) for j in range(3)
                ),
                "cheap_free_reserve_vector": tuple(
                    max(exact[i][j] for i in unknown) for j in range(3)
                ),
            }
        )
        for row in final:
            assert fits(row["fee"], whole_cap)
            selected = (h["M"], row["M"])
            separate_exact_frames = sum(exact[i][1] for i in selected)
            union_exact_frames = max(exact[i][1] for i in selected)
            assert separate_exact_frames > union_exact_frames
            leaves.append(
                {
                    "prob": h["prob"] * row["prob"],
                    "z": row["z"],
                    "first_z": h["z"],
                    "history": index,
                    "final_F_M": (row["F"], row["M"]),
                    "fee": row["fee"],
                    "duplicated_exact_frames_charged": separate_exact_frames - union_exact_frames,
                }
            )
        for row in fixed_final:
            fixed_leaves.append({**row, "prob": h["prob"] * row["prob"]})
        for row in cheap_free:
            assert fits(row["fee"], whole_cap)
            cheap_free_leaves.append({**row, "prob": h["prob"] * row["prob"]})
    assert len(leaves) == 72 and sum((row["prob"] for row in leaves), F()) == 1
    assert average(leaves) == average(fixed_leaves) == average(cheap_free_leaves) == truth
    threshold = sum(source, F())
    latest = sum(
        (
            row["prob"] * (row["first_z"] if row["first_z"] > threshold else row["z"])
            for row in leaves
        ),
        F(),
    )
    random_average = sum(
        (
            row["prob"]
            * (row["first_z"] if row["first_z"] > threshold else (row["first_z"] + row["z"]) / 2)
            for row in leaves
        ),
        F(),
    )
    centered = sum(
        (
            row["prob"]
            * (row["first_z"] - truth + (row["z"] - truth if row["first_z"] <= threshold else 0))
            for row in leaves
        ),
        F(),
    )
    assert latest != truth and random_average != truth and centered == 0
    # Different invented counterexamples, not a rerun of author fixtures.
    fit_points = (F(4), F(16, 3))  # Y=(1,4), a=(1,3), b=a*Y_M/a_M, F=U.
    assert sum(fit_points, F()) / 2 == F(14, 3) != 5
    old_draw = (F(6), F(18))  # Y=(3,9), uniform singleton HT, fees=(1,2), B=1.
    assert sum(old_draw, F()) / 2 == 12 and old_draw[0] != 12
    refusal_support, missing = panels({}, {}, (22, 16, 66))
    assert refusal_support == [(0, 1)] and missing == {2, 3}
    assert min(row["r"][row["M"]] for row in first) >= F(1, 8)
    stats = {}
    for name, rows in (
        ("adaptive_final", leaves),
        ("fixed_b_uniform_r_final", fixed_leaves),
        ("cheap_free_fixed_final", cheap_free_leaves),
    ):
        stats[name] = {
            "mean": average(rows),
            "variance": variance(rows),
            "variance_approx": float(variance(rows)),
            "expected_whole_fee": tuple(
                sum((row["prob"] * row["fee"][j] for row in rows), F()) for j in range(3)
            ),
            "whole_fee_max": tuple(max(row["fee"][j] for row in rows) for j in range(3)),
        }
    return {
        "status": "PASS_INDEPENDENT_EXACT_INVENTED_ONLY",
        "truth": truth,
        "first_mean": average(first),
        "first_variance_components": first_decomposition,
        "probability_mass": sum((row["prob"] for row in leaves), F()),
        "history_count": 12,
        "leaf_count": 72,
        "all_final_histories_feasible_and_unbiased": True,
        "history_checks": history_rows,
        "leaves": leaves,
        "comparison": stats,
        "wrong_q": {"q": q, "mean": wrong_q, "bias": wrong_q - truth},
        "current_Y_stop": {
            "threshold": threshold,
            "latest_mean": latest,
            "random_average_mean": random_average,
            "stopped_centered_sum_mean": centered,
            "fresh_final_mean": average(leaves),
        },
        "current_M_fit": {
            "Y": (1, 4),
            "a": (1, 3),
            "points": fit_points,
            "mean": F(14, 3),
            "truth": 5,
        },
        "fee_selected_publication": {
            "Y": (3, 9),
            "costs": (1, 2),
            "budget": 1,
            "unfiltered_mean": 12,
            "successful_mean": 6,
        },
        "refusal": {
            "stage_cap": (22, 16, 66),
            "legal_F": refusal_support,
            "missing_exact_coverage": sorted(missing),
        },
        "whole_cap_invented": whole_cap,
        "whole_fee_min": tuple(min(row["fee"][j] for row in leaves) for j in range(3)),
        "whole_fee_max": tuple(max(row["fee"][j] for row in leaves) for j in range(3)),
        "interpretation": "Current-F b and accessible conditional r broaden usable policies; no universal variance or speed gain, no CI.",
        "native_success_issue": "Toy completes every branch. Real all-history successful final execution remains unproved; selective refusal/failure changes published conditional means.",
        "variance_identity": "Var(Z|H)=Var_F(K+sum_U(mu)+sum_F((Y-mu)/p))+E_F Var_M(sum_M((Y-b)/(p*r))|F).",
        "singleton_optimal_r_if_Y_known": "r_i proportional to abs(Y_i-b_i)/p_i; unavailable target residuals prevent treating recipe weights as an optimum.",
        "author_module_imports_or_proof_runs": 0,
        "actual_a_Y_DEV_holdout_media_reads": 0,
        "decoder_encoder_compiler_calls": 0,
    }


def worker():
    guard()
    print(encoded(reconstruct()).decode(), flush=True)


def run():
    guard()
    store("one-attempt.json.gz", {"started_unix": time.time(), "retry": "NEVER"})
    pipes = [os.pipe(), os.pipe()]
    started = time.monotonic()
    pid = os.fork()
    if pid == 0:
        resource.setrlimit(resource.RLIMIT_CPU, (5, 5))
        resource.setrlimit(resource.RLIMIT_AS, (512 << 20, 512 << 20))
        for index, (_, fd) in enumerate(pipes):
            os.dup2(fd, index + 1)
        for pair in pipes:
            for fd in pair:
                os.close(fd)
        os.execv(PY, [PY, "-B", "-S", str(CODE), "_worker"])
    selector = selectors.DefaultSelector()
    buffers = [bytearray(), bytearray()]
    for index, (reader, writer) in enumerate(pipes):
        os.close(writer)
        os.set_blocking(reader, False)
        selector.register(reader, selectors.EVENT_READ, index)
    failure = None
    try:
        while selector.get_map():
            if time.monotonic() - started > 10:
                raise ValueError("independent metadata deadline")
            for key, _ in selector.select(0.1):
                chunk = os.read(key.fd, 4096)
                if not chunk:
                    selector.unregister(key.fd)
                    os.close(key.fd)
                else:
                    buffers[key.data].extend(chunk)
                    if len(buffers[key.data]) > (65536 if key.data == 0 else 4096):
                        raise ValueError("registered output cap")
    except BaseException as error:
        failure = str(error)
        try:
            os.kill(pid, 9)
        except ProcessLookupError:
            pass
    finally:
        for key in tuple(selector.get_map().values()):
            os.close(key.fd)
        selector.close()
        _, status, usage = os.wait4(pid, 0)
    stdout, stderr = map(bytes, buffers)
    cpu = usage.ru_utime + usage.ru_stime
    passed = failure is None and os.waitstatus_to_exitcode(status) == 0 and cpu <= 5
    result = json.loads(stdout) if passed else None
    evidence = store("exact-reconstruction.json.gz", result) if passed else None
    receipt = store(
        "completion.json.gz",
        {
            "status": "PASS_INDEPENDENT_SOURCE_ONLY" if passed else "FAIL_RETAIN_FEES_NO_RETRY",
            "evidence": evidence,
            "failure": failure,
            "exit_code": os.waitstatus_to_exitcode(status),
            "whole_worker_wait4": {
                "user_cpu_seconds": usage.ru_utime,
                "system_cpu_seconds": usage.ru_stime,
                "cpu_seconds": cpu,
                "wall_seconds": time.monotonic() - started,
                "maxrss_kib": usage.ru_maxrss,
            },
            "worker_contains": "startup/imports/binding reads/independent reconstruction/serialization/exit",
            "stdout": {"bytes": len(stdout), "sha256": digest(stdout), "complete": passed},
            "stderr": {
                "bytes": len(stderr),
                "sha256": digest(stderr),
                "prefix": stderr.decode(errors="replace"),
            },
            "supervisor_CPU_prefix_seconds": resource.getrusage(resource.RUSAGE_SELF).ru_utime
            + resource.getrusage(resource.RUSAGE_SELF).ru_stime,
            "registration_planning_Ruff_supervisor_final_write_exit_CPU": "UNKNOWN_NOT_FREE",
            "allocated_before_final": family_bytes(),
            "disk_free_before_final": shutil.disk_usage(ROOT).free,
            "actual_native_cost_or_accuracy_certificate": False,
        },
        final=True,
    )
    print(json.dumps(receipt))
    if not passed:
        raise SystemExit(1)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("register", "run", "_worker"))
    args = parser.parse_args()
    {"register": register, "run": run, "_worker": worker}[args.action]()


if __name__ == "__main__":
    main()
