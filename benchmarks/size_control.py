"""Cold-cache size-cap sweep; quality is measured after control has finished."""

import argparse
import hashlib
import json
import re
import shutil
import subprocess
from pathlib import Path

from vsize import ComputeBudget, EncodeSpec, Request, SizeConstraint
from vsize.engine import Engine
from vsize.runtime import digest


def quality(source, artifact):
    measured = subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-nostdin",
            "-nostats",
            "-threads",
            "2",
            "-i",
            str(source),
            "-threads",
            "2",
            "-i",
            str(artifact),
            "-filter_complex_threads",
            "1",
            "-filter_complex",
            "[0:v]setpts=PTS-STARTPTS,format=yuv420p[ref];"
            "[1:v]setpts=PTS-STARTPTS,format=yuv420p[out];[out][ref]psnr",
            "-an",
            "-f",
            "null",
            "-",
        ],
        capture_output=True,
        text=True,
        timeout=120,
        check=True,
    )
    lines = [line for line in measured.stderr.splitlines() if "PSNR" in line]
    match = re.search(r"average:([\d.]+)", lines[-1]) if lines else None
    return {
        "psnr_average_db": float(match[1]) if match else None,
        "ffmpeg_summary": lines[-1] if lines else "No PSNR summary",
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("manifest", type=Path)
    parser.add_argument("references", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--cases", nargs="+", default=["motion", "pedestrians", "grain"])
    parser.add_argument("--fractions", nargs="+", type=float, default=[0.9, 0.7, 0.5])
    parser.add_argument("--budget", type=float, default=8)
    parser.add_argument("--quality-only", action="store_true", help="evaluate persisted controls")
    args = parser.parse_args()
    if not args.quality_only:
        args.output.mkdir(parents=True, exist_ok=False)
        code_root = Path(__file__).resolve().parents[1]
        code_hash = hashlib.sha256()
        for path in sorted((code_root / "src").rglob("*.py")):
            code_hash.update(path.relative_to(code_root).as_posix().encode())
            code_hash.update(path.read_bytes())
        shutil.copytree(
            code_root / "src",
            args.output / "code" / "src",
            ignore=shutil.ignore_patterns("__pycache__"),
        )
        (args.output / "metadata.json").write_text(
            json.dumps(
                {
                    "source_code_sha256": code_hash.hexdigest(),
                    "budget_seconds": args.budget,
                    "fractions": args.fractions,
                    "cases": args.cases,
                    "cold_cache": True,
                    "split": "development",
                },
                indent=2,
            )
        )
    references = json.loads(args.references.read_text())
    cases = {case["id"]: case for case in json.loads(args.manifest.read_text())["cases"]}
    predictions = (
        [json.loads(line) for line in (args.output / "predictions.jsonl").read_text().splitlines()]
        if args.quality_only
        else []
    )
    pending_cases = [] if args.quality_only else args.cases
    for name in pending_cases:
        case = cases[name]
        source = Path(case["source"])
        if digest(source) != case["sha256"]:
            raise ValueError(f"source changed: {name}")
        for fraction in args.fractions:
            limit = int(references[name]["bytes"] * fraction)
            engine = Engine(args.output / "work" / f"{name}-{fraction}")
            result = engine.run(
                Request(
                    EncodeSpec(source, drop_audio=True),
                    compute=ComputeBudget(wall_seconds=args.budget, max_probes=6),
                    size=SizeConstraint(max_bytes=limit, max_crf=35, max_candidates=4),
                    sample_seconds=2,
                    seed=20261002,
                )
            )
            prediction = {
                "case": name,
                "relative_cap": fraction,
                "max_bytes": limit,
                "reference_bytes": references[name]["bytes"],
                "result": result,
            }
            if result["status"] == "satisfied":
                destination = args.output / f"{name}-{fraction}.mp4"
                engine.materialize(result, destination)
                prediction["published_file"] = str(destination.resolve())
                prediction["actual_bytes"] = destination.stat().st_size
                if destination.stat().st_size > limit:
                    raise AssertionError("size contract violated")
            predictions.append(prediction)
            with (args.output / "predictions.jsonl").open("a") as stream:
                stream.write(json.dumps(prediction) + "\n")
            print(
                json.dumps(
                    {
                        "case": name,
                        "cap": fraction,
                        "status": result["status"],
                        "unmet": result["unmet"],
                    }
                ),
                flush=True,
            )

    # Quality measurement does not influence selection and is excluded from the
    # controller's time budget, like reference encoding in the size benchmark.
    reference_quality = {}
    for name in args.cases:
        reference_quality[name] = quality(cases[name]["source"], references[name]["artifact"])
    for prediction in predictions:
        if "published_file" in prediction:
            prediction["quality"] = quality(
                cases[prediction["case"]]["source"], prediction["published_file"]
            )
    report = {
        "split": "development",
        "budget_seconds": args.budget,
        "max_crf": 35,
        "max_candidates": 4,
        "quality_metric": "PSNR average; pixel error only, not a perceptual quality guarantee",
        "reference_quality": reference_quality,
        "rows": predictions,
    }
    (args.output / "results.json").write_text(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
