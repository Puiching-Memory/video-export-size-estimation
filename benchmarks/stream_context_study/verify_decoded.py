#!/usr/bin/env python3
"""Independently decode already saved full/stop development study artifacts."""

import argparse
import json
from pathlib import Path

from study import ROOT, digest, frame_hashes, helper_packets, save_json


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("batches", nargs="+", type=Path)
    args = parser.parse_args()
    results = []
    for batch in args.batches:
        for record in json.loads(batch.read_text()):
            if "api_stop" not in record:
                continue
            base = Path(record["api_stop"]["command"][-1])
            full_base = Path(record["api_full"]["command"][-1])
            stop, _ = helper_packets(base)
            full_hashes, _ = frame_hashes(full_base.with_suffix(".h264"))
            stop_hashes, _ = frame_hashes(base.with_suffix(".h264"))
            presentation_pts = sorted(packet["pts"] for packet in stop)
            if len(stop_hashes) != len(presentation_pts):
                raise RuntimeError("one decoded picture per emitted packet was not observed")
            by_pts = dict(zip(presentation_pts, stop_hashes, strict=True))
            start, count = record["leading_frames"], record["central_frames"]
            measured = [by_pts[pts] for pts in range(start, start + count)]
            expected = full_hashes[start : start + count]
            result = {
                "id": record["id"],
                "batch": str(batch.resolve().relative_to(ROOT)),
                "stop_annexb_sha256": digest(base.with_suffix(".h264")),
                "full_annexb_sha256": digest(full_base.with_suffix(".h264")),
                "emitted_packets": len(stop),
                "decoded_stop_frames": len(stop_hashes),
                "presentation_pts": presentation_pts,
                "central_frame_md5": measured,
                "full_central_frame_md5": expected,
                "decoded_central_equal": measured == expected and len(expected) == count,
            }
            if not result["decoded_central_equal"]:
                raise RuntimeError(f"decoded central pixels changed: {record['id']}")
            results.append(result)
    save_json(args.output, {"count": len(results), "all_equal": True, "results": results})
    print(json.dumps({"decoded_pairs": len(results), "all_equal": True}))


if __name__ == "__main__":
    main()
