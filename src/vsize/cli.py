"""JSON request/response CLI; progress is opt-in newline-delimited JSON."""

import argparse
import json
import sys
from pathlib import Path

from .contracts import Request
from .engine import Engine


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("request", type=Path, help="JSON request file")
    parser.add_argument("--cache", type=Path, default=Path(".vsize-cache"))
    parser.add_argument("--calibration", type=Path)
    parser.add_argument("--events", action="store_true")
    parser.add_argument("--output", type=Path, help="publish a satisfied exact export atomically")
    args = parser.parse_args()
    try:
        request = Request.from_dict(json.loads(args.request.read_text()))
        engine = Engine(args.cache, args.calibration)
        callback = (lambda event: print(json.dumps(event), flush=True)) if args.events else None
        result = engine.run(request, callback)
        if args.output and result["status"] == "satisfied":
            result["published_file"] = engine.materialize(result, args.output)
        print(json.dumps({"type": "result", **result}, indent=None if args.events else 2))
        return 0 if result["status"] == "satisfied" else 2
    except (OSError, ValueError, RuntimeError, KeyError, TypeError) as error:
        print(json.dumps({"type": "error", "message": str(error)}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
