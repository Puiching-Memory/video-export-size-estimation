"""JSON request/response CLI; progress is opt-in newline-delimited JSON."""

import argparse
import json
import sys
import time
from contextlib import ExitStack
from pathlib import Path

from .contracts import Request
from .engine import Engine
from .assets import PreparedAsset


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("request", type=Path, help="JSON request file")
    parser.add_argument("--cache", type=Path, default=Path(".vsize-cache"))
    parser.add_argument("--calibration", type=Path)
    parser.add_argument("--events", action="store_true")
    parser.add_argument(
        "--prepare", action="store_true", help="ingest a sealed Linux input snapshot"
    )
    parser.add_argument("--max-prepared-bytes", type=int, default=2 * 1024**3)
    parser.add_argument("--output", type=Path, help="publish a satisfied exact export atomically")
    args = parser.parse_args()
    started = time.monotonic()
    try:
        request = Request.from_dict(json.loads(args.request.read_text()))
        engine = Engine(args.cache, args.calibration)
        callback = (lambda event: print(json.dumps(event), flush=True)) if args.events else None
        with ExitStack() as assets:
            asset = (
                assets.enter_context(
                    PreparedAsset.from_path(
                        request.encode.source, max_bytes=args.max_prepared_bytes
                    )
                )
                if args.prepare
                else None
            )
            result = engine.run(request, callback, asset=asset)
            if args.output and result["status"] == "satisfied":
                publication_started = time.monotonic()
                result["published_file"] = engine.materialize(result, args.output)
                result["publication_seconds"] = time.monotonic() - publication_started
        result["end_to_end_seconds"] = time.monotonic() - started
        print(json.dumps({"type": "result", **result}, indent=None if args.events else 2))
        return 0 if result["status"] == "satisfied" else 2
    except (OSError, ValueError, RuntimeError, KeyError, TypeError) as error:
        print(json.dumps({"type": "error", "message": str(error)}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
