"""Cold-cache, common-deadline comparison against real completed exports.

This small development corpus is a diagnostic, not an industrial/SOTA benchmark.
Predictions are persisted before references are encoded or read by evaluation.
"""

import argparse
import hashlib
import json
import math
import random
import shutil
import statistics
import subprocess
import time
from pathlib import Path

from vsize import ComputeBudget, EncodeSpec, Request
from vsize.engine import Engine
from vsize.inference import Sampler
from vsize.media import Media
from vsize.runtime import BudgetExhausted, Cache, Deadline, digest


def ffmpeg(arguments):
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y", *arguments], check=True
    )


def prepare(root):
    root.mkdir(parents=True, exist_ok=True)
    definitions = {
        "static": "color=c=0x316a8c:s=384x216:r=24:d=24",
        "motion": "testsrc2=s=384x216:r=24:d=24",
        "grain": "testsrc2=s=384x216:r=24:d=24,noise=alls=22:allf=t+u:all_seed=2701",
        "brief-burst": "color=c=0x316a8c:s=384x216:r=24:d=24,noise=alls=50:allf=t+u:all_seed=2729:enable='between(t,9.5,11.5)'",
        "cbr-burst": "color=c=0x316a8c:s=384x216:r=24:d=24,noise=alls=50:allf=t+u:all_seed=2753:enable='between(t,16.5,19.5)'",
        "alternating": "testsrc2=s=384x216:r=24:d=24,noise=alls=40:allf=t+u:all_seed=2767:enable='lt(mod(t,8),2)'",
        "screen": "testsrc=s=384x216:r=24:d=24",
    }
    cases = []
    for name, source in definitions.items():
        path = root / f"{name}.mp4"
        options = [
            "-f",
            "lavfi",
            "-i",
            source,
            "-c:v",
            "libx264",
            "-preset",
            "fast",
            "-threads",
            "2",
            "-pix_fmt",
            "yuv420p",
            "-g",
            "48",
            "-an",
        ]
        if name == "cbr-burst":
            options += [
                "-b:v",
                "700k",
                "-minrate",
                "700k",
                "-maxrate",
                "700k",
                "-bufsize",
                "1400k",
                "-x264-params",
                "nal-hrd=cbr:force-cfr=1",
            ]
        else:
            options += ["-crf", "18"]
        ffmpeg([*options, str(path)])
        cases.append(
            {
                "id": name,
                "group_id": f"synthetic-{name}",
                "source": str(path.resolve()),
                "generator": source,
                "sha256": digest(path),
                "kind": "synthetic",
            }
        )
    source_root = root.parent / "sources"
    for name, start, duration in [("pedestrians", 8, 40), ("megamind", 0, 11)]:
        original = source_root / f"{name}.avi"
        if not original.exists():
            raise FileNotFoundError(f"Download documented source first: {original}")
        path = root / f"{name}.mp4"
        ffmpeg(
            [
                "-ss",
                str(start),
                "-i",
                str(original),
                "-t",
                str(duration),
                "-vf",
                "scale=384:-2",
                "-c:v",
                "libx264",
                "-preset",
                "fast",
                "-crf",
                "18",
                "-threads",
                "2",
                "-an",
                str(path),
            ]
        )
        cases.append(
            {
                "id": name,
                "group_id": f"opencv-{name}",
                "source": str(path.resolve()),
                "sha256": digest(path),
                "kind": "public-video",
                "original_sha256": digest(original),
            }
        )
    manifest = {
        "schema_version": 1,
        "split": "development",
        "note": "Nine diagnostic cases; no generalization or SOTA claim.",
        "cases": cases,
    }
    (root / "manifest.json").write_text(json.dumps(manifest, indent=2))
    print(json.dumps({"prepared": len(cases), "manifest": str(root / "manifest.json")}), flush=True)


def components(spec, cache, seconds, method):
    deadline = Deadline(seconds)
    prediction = None
    sampled = 0
    try:
        media = Media(spec, 2, Cache(cache), deadline)
        blocks = media.scan(2, visual=method == "visual-residual")
        sampler = Sampler(blocks, seed=20261002)
        if method == "uniform":
            count = min(3, len(blocks))
            selected = []
            for n in range(count):
                start = max(0, (media.duration - count * 2) / (count + 1) * (n + 1) + n * 2)
                record = media.encode(spec.crf, (start, min(2, media.duration - start)))
                selected.append(record)
                sampled += min(2, media.duration - start)
                prediction = round(
                    sum(r["file_bytes"] for r in selected)
                    / sum(r["duration"] for r in selected)
                    * media.duration
                )
        else:
            for index in sampler.order[:6]:
                block = blocks[index]
                record = media.encode(spec.crf, (block["start"], block["duration"]))
                sampled += block["duration"]
                sampler.add(index, record)
                prediction = sampler.estimate()["estimated_bytes"]
        state = "completed"
    except BudgetExhausted:
        state = "deadline"
    return {
        "prediction_bytes": prediction,
        "wall_seconds": deadline.elapsed,
        "sampled_seconds": sampled,
        "state": state,
    }


def run(manifest, output, ab_av1, budgets, methods):
    output.mkdir(parents=True, exist_ok=False)
    cases = json.loads(manifest.read_text())["cases"]
    code_root = Path(__file__).resolve().parents[1]
    for directory in ["src", "benchmarks", "tests"]:
        shutil.copytree(
            code_root / directory,
            output / "code" / directory,
            ignore=shutil.ignore_patterns("__pycache__"),
        )
    shutil.copyfile(code_root / "pyproject.toml", output / "code" / "pyproject.toml")
    shutil.copyfile(manifest, output / "manifest.json")
    for case in cases:
        if digest(Path(case["source"])) != case["sha256"]:
            raise ValueError(f"source changed since manifest: {case['id']}")
    code_hash = hashlib.sha256()
    for path in sorted((code_root / "src").rglob("*.py")):
        code_hash.update(path.relative_to(code_root).as_posix().encode())
        code_hash.update(path.read_bytes())
    metadata = {
        "schema_version": 1,
        "split": "development",
        "source_code_sha256": code_hash.hexdigest(),
        "budgets_seconds": budgets,
        "methods": methods,
        "cold_cache": True,
        "codec": "libx264",
        "crf": 23,
        "preset": "medium",
        "threads": 2,
        "audio": "dropped for comparable video-only baselines",
        "ffmpeg": subprocess.check_output(["ffmpeg", "-version"], text=True).splitlines()[0],
        "ab_av1": subprocess.check_output([str(ab_av1), "--version"], text=True).strip(),
    }
    (output / "metadata.json").write_text(json.dumps(metadata, indent=2))
    jobs = [(case, budget, method) for case in cases for budget in budgets for method in methods]
    random.Random(20261002).shuffle(jobs)
    predictions = []
    for number, (case, budget, method) in enumerate(jobs):
        work = output / "work" / f"{case['id']}-{method}-{budget}"
        spec = EncodeSpec(Path(case["source"]), drop_audio=True)
        started = time.monotonic()
        try:
            if method == "controller":
                answer = Engine(work).run(
                    Request(
                        spec,
                        compute=ComputeBudget(wall_seconds=budget, max_probes=6),
                        sample_seconds=2,
                        seed=20261002,
                    )
                )
                observed = answer["estimate"]
                measurement = {
                    "prediction_bytes": observed["estimated_bytes"] if observed else None,
                    "wall_seconds": answer["elapsed_seconds"],
                    "state": answer["status"],
                    "evidence": observed["uncertainty"]["kind"] if observed else None,
                    "sampled_seconds": answer["attempted_encode_seconds"],
                }
            elif method == "full-encode":
                deadline = Deadline(budget)
                media = Media(spec, 2, Cache(work), deadline)
                exact = media.encode(23)
                measurement = {
                    "prediction_bytes": exact["file_bytes"],
                    "wall_seconds": deadline.elapsed,
                    "state": "completed",
                    "evidence": "exact",
                    "sampled_seconds": media.duration,
                }
            elif method == "ab-av1":
                work.mkdir(parents=True)
                deadline = Deadline(budget)
                raw = deadline.run(
                    [
                        str(ab_av1),
                        "sample-encode",
                        "-i",
                        str(spec.source),
                        "--encoder",
                        "libx264",
                        "--preset",
                        "medium",
                        "--crf",
                        "23",
                        "--pix-format",
                        "yuv420p",
                        "--enc",
                        "threads=2",
                        "--samples",
                        "3",
                        "--sample-duration",
                        "2s",
                        "--and-vmaf",
                        "false",
                        "--cache",
                        "false",
                        "--temp-dir",
                        str(work),
                        "--stdout-format",
                        "json",
                    ]
                )
                answer = json.loads(raw.stdout)
                measurement = {
                    "prediction_bytes": answer["predicted_encode_size"],
                    "wall_seconds": deadline.elapsed,
                    "state": "completed",
                    "sampled_seconds": 6,
                }
            else:
                measurement = components(spec, work, budget, method)
        except BudgetExhausted:
            measurement = {
                "prediction_bytes": None,
                "wall_seconds": time.monotonic() - started,
                "state": "deadline",
            }
        except (RuntimeError, ValueError) as error:
            measurement = {
                "prediction_bytes": None,
                "wall_seconds": time.monotonic() - started,
                "state": "failed",
                "error": str(error)[-500:],
            }
        row = {
            "case": case["id"],
            "group_id": case["group_id"],
            "budget_seconds": budget,
            "method": method,
            **measurement,
        }
        predictions.append(row)
        with (output / "predictions.jsonl").open("a") as stream:
            stream.write(json.dumps(row) + "\n")
        print(json.dumps({"job": number + 1, "total": len(jobs), **row}), flush=True)
    # Reference labels become available only after every prediction was persisted.
    references = {}
    for case in cases:
        media = Media(
            EncodeSpec(Path(case["source"]), drop_audio=True),
            2,
            Cache(output / "references" / case["id"]),
            Deadline(120),
        )
        started = time.monotonic()
        exact = media.encode(23)
        references[case["id"]] = {
            "bytes": exact["file_bytes"],
            "encode_wall_seconds": time.monotonic() - started,
            "artifact": exact["artifact"],
            "sha256": exact["sha256"],
        }
    (output / "references.json").write_text(json.dumps(references, indent=2))
    for row in predictions:
        reference = references[row["case"]]
        row["actual_bytes"] = reference["bytes"]
        row["cost_vs_full_encode"] = row["wall_seconds"] / reference["encode_wall_seconds"]
        if row["prediction_bytes"] is not None:
            row["signed_error_percent"] = 100 * (row["prediction_bytes"] / reference["bytes"] - 1)
            row["absolute_error_percent"] = abs(row["signed_error_percent"])
    (output / "evaluated.json").write_text(json.dumps(predictions, indent=2))
    report(predictions, output)


def quantile(values, probability):
    values = sorted(values)
    index = (len(values) - 1) * probability
    lower = math.floor(index)
    return values[lower] + (values[min(lower + 1, len(values) - 1)] - values[lower]) * (
        index - lower
    )


def report(rows, output):
    summary = []
    for method, budget in sorted({(r["method"], r["budget_seconds"]) for r in rows}):
        group = [r for r in rows if r["method"] == method and r["budget_seconds"] == budget]
        completed = [r for r in group if r["prediction_bytes"] is not None]
        errors = [r["absolute_error_percent"] for r in completed]
        summary.append(
            {
                "method": method,
                "budget_seconds": budget,
                "outputs": len(completed),
                "cases": len(group),
                "within_10_percent_all_cases": sum(e <= 10 for e in errors) / len(group),
                "median_ape_percent": statistics.median(errors) if errors else None,
                "p90_ape_percent": quantile(errors, 0.9) if errors else None,
                "worst_ape_percent": max(errors) if errors else None,
                "median_wall_seconds": statistics.median(r["wall_seconds"] for r in group),
                "exact_outputs": sum(r.get("evidence") == "exact" for r in completed),
            }
        )
    (output / "summary.json").write_text(json.dumps(summary, indent=2))
    print(json.dumps({"summary": summary}), flush=True)


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    make = sub.add_parser("prepare")
    make.add_argument("directory", type=Path)
    evaluate = sub.add_parser("run")
    evaluate.add_argument("manifest", type=Path)
    evaluate.add_argument("output", type=Path)
    evaluate.add_argument("--ab-av1", type=Path, required=True)
    evaluate.add_argument("--budgets", nargs="+", type=float, default=[1, 3, 8])
    evaluate.add_argument(
        "--methods",
        nargs="+",
        choices=["uniform", "visual-residual", "controller", "ab-av1", "full-encode"],
        default=["uniform", "visual-residual", "controller", "ab-av1", "full-encode"],
    )
    args = parser.parse_args()
    if args.command == "prepare":
        prepare(args.directory)
    else:
        run(args.manifest, args.output, args.ab_av1, args.budgets, args.methods)


if __name__ == "__main__":
    main()
