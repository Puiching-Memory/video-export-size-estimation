"""Acquire and validate the reserved corpus; never evaluate predictor accuracy.

No third-party Python dependencies. Media stays outside Git, with publisher MD5
where available and a checked-in SHA-256 lock for every downloaded object.
"""

import argparse
import concurrent.futures
import hashlib
import json
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from fractions import Fraction
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / "benchmarks/corpora/industrial-v1/catalog.json"
LOCK = CATALOG.with_name("sources.lock.json")
MEDIA = ROOT / "artifacts/industrial-v1"


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n")
    temporary.replace(path)


def hashes(path):
    sha = hashlib.sha256()
    md5 = hashlib.md5(usedforsecurity=False)
    with path.open("rb") as stream:
        while chunk := stream.read(4 * 1024 * 1024):
            sha.update(chunk)
            md5.update(chunk)
    return {"sha256": sha.hexdigest(), "md5": md5.hexdigest(), "bytes": path.stat().st_size}


def load_catalog(path):
    catalog = json.loads(path.read_text())
    ids, groups = set(), {}
    for asset in catalog["assets"]:
        name = asset["id"]
        if name in ids or not re.fullmatch(r"[a-z0-9-]+", name):
            raise ValueError(f"invalid/duplicate id: {name}")
        ids.add(name)
        if Path(asset["filename"]).name != asset["filename"]:
            raise ValueError(f"filename must be a basename: {name}")
        if asset["split"] not in {"calibration_reserved", "test_reserved"}:
            raise ValueError(f"invalid reserved split: {name}")
        group = asset["group_id"]
        if groups.setdefault(group, asset["split"]) != asset["split"]:
            raise ValueError(f"source group crosses splits: {group}")
        notice = (path.parent / asset["license"]["notice_file"]).resolve()
        if not notice.is_relative_to(path.parent.resolve()):
            raise ValueError(f"notice escapes catalog directory: {name}")
        if hashlib.sha256(notice.read_bytes()).hexdigest() != asset["license"]["notice_sha256"]:
            raise ValueError(f"license notice changed: {name}")
    return catalog


def load_lock(path, assets):
    if not path.exists():
        return {}
    records = json.loads(path.read_text())["assets"]
    for asset in assets:
        record = records.get(asset["id"])
        if not record or record["url"] != asset["url"]:
            raise ValueError(f"lock missing or URL changed: {asset['id']}")
        if record["bytes"] != asset["expected_bytes"]:
            raise ValueError(f"lock size changed: {asset['id']}")
    return records


def check_bytes(path, asset, locked=None):
    if path.stat().st_size != asset["expected_bytes"]:
        raise ValueError(f"size mismatch: {asset['id']}")
    result = hashes(path)
    if asset.get("publisher_md5") and result["md5"] != asset["publisher_md5"]:
        raise ValueError(f"publisher MD5 mismatch: {asset['id']}")
    if locked and result["sha256"] != locked["sha256"]:
        raise ValueError(f"locked SHA-256 mismatch: {asset['id']}")
    return result


def media_path(root, asset):
    return root / asset["id"] / asset["filename"]


def fetch_one(root, asset, locked=None):
    target = media_path(root, asset)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        return check_bytes(target, asset, locked)
    part = target.with_suffix(target.suffix + ".part")
    receipt = target.with_suffix(target.suffix + ".http.json")
    total = asset["expected_bytes"]
    for attempt in range(3):
        offset = part.stat().st_size if part.exists() else 0
        if offset > total:
            raise ValueError(f"partial file exceeds expected size: {asset['id']}")
        if offset == total:
            break
        headers = {
            "User-Agent": "video-export-size-estimation-corpus/1",
            "Accept-Encoding": "identity",
        }
        previous = json.loads(receipt.read_text()) if receipt.exists() else {}
        validator = previous.get("etag") or previous.get("last_modified")
        if offset and previous.get("url") == asset["url"] and validator:
            headers.update({"Range": f"bytes={offset}-", "If-Range": validator})
        else:
            offset = 0
        try:
            request = urllib.request.Request(asset["url"], headers=headers)
            with urllib.request.urlopen(request, timeout=60) as response:
                if response.status == 206:
                    match = re.fullmatch(
                        r"bytes (\d+)-(\d+)/(\d+)", response.headers.get("Content-Range", "")
                    )
                    if not match or int(match[1]) != offset or int(match[3]) != total:
                        raise ValueError(f"invalid resume range: {asset['id']}")
                elif response.status == 200:
                    offset = 0
                else:
                    raise ValueError(f"unexpected HTTP status: {response.status}")
                write_json(
                    receipt,
                    {
                        "url": asset["url"],
                        "etag": response.headers.get("ETag"),
                        "last_modified": response.headers.get("Last-Modified"),
                        "note": "HTTP validators are for resuming, not publisher checksums.",
                    },
                )
                with part.open("ab" if offset else "wb") as output:
                    while chunk := response.read(4 * 1024 * 1024):
                        offset += len(chunk)
                        if offset > total:
                            raise ValueError(f"response exceeds expected size: {asset['id']}")
                        output.write(chunk)
            if offset != total:
                raise OSError(f"incomplete download: {asset['id']} ({offset}/{total})")
            break
        except (OSError, urllib.error.URLError):
            if attempt == 2:
                raise
            time.sleep(attempt + 1)
    result = check_bytes(part, asset, locked)
    part.replace(target)
    return result


def fetch(catalog, root, lock, workers):
    root.mkdir(parents=True, exist_ok=True)
    remaining = 0
    for asset in catalog["assets"]:
        path = media_path(root, asset)
        part = path.with_suffix(path.suffix + ".part")
        if not path.exists():
            remaining += max(
                0, asset["expected_bytes"] - (part.stat().st_size if part.exists() else 0)
            )
    if shutil.disk_usage(root).free < remaining + 2 * 1024**3:
        raise ValueError("insufficient disk space (downloads plus 2 GiB reserve required)")
    records = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(fetch_one, root, a, lock.get(a["id"])): a for a in catalog["assets"]}
        for future in concurrent.futures.as_completed(futures):
            asset = futures[future]
            result = future.result()
            records[asset["id"]] = {"url": asset["url"], **result}
            print(
                json.dumps({"download_verified": asset["id"], "bytes": result["bytes"]}), flush=True
            )
    write_json(
        root / "acquisition.json",
        {
            "schema_version": 1,
            "acquired_at_utc": datetime.now(timezone.utc).isoformat(),
            "assets": records,
        },
    )


def bit_depth(video):
    if video.get("bits_per_raw_sample", "0") not in {"0", "N/A", ""}:
        return int(video["bits_per_raw_sample"])
    pixel_format = video.get("pix_fmt", "")
    match = re.search(r"(?:p|gray|gbrp)(9|10|12|14|16)(?:le|be)?$", pixel_format)
    if match:
        return int(match[1])
    if pixel_format in {"yuv420p", "yuv422p", "yuv444p", "yuvj420p", "rgb24", "bgr24", "gray"}:
        return 8
    return None


def check_video(asset, video):
    actual = {
        "width": video["width"],
        "height": video["height"],
        "frame_rate": video["avg_frame_rate"],
        "frames": int(video["nb_read_frames"]),
        "bit_depth": bit_depth(video),
    }
    for key, expected in asset["expected_video"].items():
        observed = actual[key]
        equal = (
            Fraction(observed) == Fraction(expected)
            if key == "frame_rate"
            else observed == expected
        )
        if not equal:
            raise ValueError(
                f"video metadata mismatch: {asset['id']}: {key}={observed}, expected {expected}"
            )
    actual["scope"] = (
        "sdr_8bit"
        if asset["signal"] == "SDR"
        and actual["bit_depth"] == 8
        and video.get("color_transfer") not in {"smpte2084", "arib-std-b67"}
        else "precision_or_hdr_boundary"
    )
    return actual


def canonical_y4m(original, asset):
    """Remove only a declared byte after all complete frames, never image data."""
    policy = asset.get("transport_cleanup")
    if not policy:
        return original
    if policy["operation"] != "remove-one-terminal-newline":
        raise ValueError(f"unknown cleanup: {asset['id']}")
    video = asset["expected_video"]
    frame_bytes = video["width"] * video["height"] * 3 // 2
    frame_bytes *= 2 if video["bit_depth"] > 8 else 1
    with original.open("rb") as stream:
        header = stream.readline()
        if not header.startswith(b"YUV4MPEG2 ") or b" C420" not in header:
            raise ValueError("cleanup supports only the declared planar 4:2:0 Y4M files")
        for _ in range(video["frames"]):
            if stream.read(6) != b"FRAME\n":
                raise ValueError("unexpected Y4M frame boundary; refusing cleanup")
            stream.seek(frame_bytes, 1)
        clean_bytes = stream.tell()
        if stream.read() != b"\n":
            raise ValueError("expected exactly one newline after the final complete frame")
    target = original.with_name(original.stem + ".canonical.y4m")
    if target.exists():
        if target.stat().st_size != clean_bytes or hashes(target)["sha256"] != policy["sha256"]:
            raise ValueError("canonical copy changed")
        return target
    part = target.with_suffix(".y4m.part")
    with original.open("rb") as source, part.open("wb") as output:
        remaining = clean_bytes
        while remaining:
            chunk = source.read(min(4 * 1024 * 1024, remaining))
            if not chunk:
                raise ValueError("source truncated while creating canonical copy")
            output.write(chunk)
            remaining -= len(chunk)
    if hashes(part)["sha256"] != policy["sha256"]:
        raise ValueError("canonical SHA-256 mismatch")
    part.replace(target)
    return target


def verify_one(root, asset, locked):
    original = media_path(root, asset)
    upstream = check_bytes(original, asset, locked)
    path = canonical_y4m(original, asset)
    record = upstream if path == original else hashes(path)
    info = json.loads(
        subprocess.check_output(
            [
                "ffprobe",
                "-v",
                "error",
                "-threads",
                "2",
                "-show_streams",
                "-show_format",
                "-of",
                "json",
                str(path),
            ],
            text=True,
        )
    )
    video = next(s for s in info["streams"] if s["codec_type"] == "video")
    started = time.monotonic()
    decoded = subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-v",
            "error",
            "-xerror",
            "-nostdin",
            "-nostats",
            "-progress",
            "pipe:1",
            "-threads",
            "2",
            "-i",
            str(path),
            "-map",
            "0:v:0",
            "-map",
            "0:a?",
            "-fps_mode",
            "passthrough",
            "-f",
            "null",
            "-",
        ],
        text=True,
        capture_output=True,
        check=True,
    )
    # Count the actual decoded frames in the same full pass that checks errors.
    frames = re.findall(r"^frame=(\d+)\s*$", decoded.stdout, flags=re.MULTILINE)
    if not frames or "progress=end" not in decoded.stdout:
        raise ValueError(f"decode did not reach EOF: {asset['id']}")
    video["nb_read_frames"] = frames[-1]
    summary = check_video(asset, video)
    return {
        "id": asset["id"],
        "group_id": asset["group_id"],
        "split": asset["split"],
        "source": str(path.relative_to(root)),
        **record,
        "upstream": {"source": str(original.relative_to(root)), **upstream},
        "transport_cleanup": asset.get("transport_cleanup"),
        "video": summary,
        "streams": info["streams"],
        "format": {k: v for k, v in info["format"].items() if k != "filename"},
        "full_decode": {
            "passed": True,
            "wall_seconds": time.monotonic() - started,
            "stderr": decoded.stderr,
        },
        "predictor_accuracy_evaluated": False,
    }


def verify(catalog, root, lock, workers):
    if not lock:
        raise ValueError("freeze acquisition hashes with --freeze-lock before verification")
    records = []
    failures = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(verify_one, root, a, lock[a["id"]]): a for a in catalog["assets"]}
        for future in concurrent.futures.as_completed(futures):
            try:
                record = future.result()
            except (ValueError, OSError, subprocess.CalledProcessError) as error:
                failure = {
                    "id": futures[future]["id"],
                    "error": str(error),
                    "stderr": getattr(error, "stderr", ""),
                }
                failures.append(failure)
                print(json.dumps({"verification_failed": failure}), flush=True)
                continue
            records.append(record)
            write_json(root / "validation" / (record["id"] + ".json"), record)
            print(json.dumps({"full_decode_verified": record["id"], **record["video"]}), flush=True)
    if failures:
        write_json(root / "validation-failures.json", failures)
        raise ValueError(
            f"{len(failures)} sources failed; successful checks retained in validation/"
        )
    publish_report(catalog, root, records)


def publish_report(catalog, root, records):
    if {r["id"] for r in records} != {a["id"] for a in catalog["assets"]}:
        raise ValueError("validation records do not cover exactly the catalog")
    report = {
        "schema_version": 1,
        "catalog_sha256": hashlib.sha256(json.dumps(catalog, sort_keys=True).encode()).hexdigest(),
        "verification_script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "validated_at_utc": datetime.now(timezone.utc).isoformat(),
        "ffmpeg": subprocess.check_output(["ffmpeg", "-version"], text=True).splitlines()[0],
        "validation": "Exact source hashes, metadata/frame count, and full video/audio decode; no predictor scores.",
        "assets": sorted(records, key=lambda r: r["id"]),
    }
    write_json(root / "validation.json", report)
    for split in ("calibration_reserved", "test_reserved"):
        for scope in ("all", "sdr_8bit"):
            cases = [
                {
                    "id": r["id"],
                    "group_id": r["group_id"],
                    "source": str((root / r["source"]).resolve()),
                    "sha256": r["sha256"],
                    "kind": "public-video",
                    "scope": r["video"]["scope"],
                }
                for r in report["assets"]
                if r["split"] == split and (scope == "all" or r["video"]["scope"] == scope)
            ]
            write_json(
                root / f"manifest-{split}-{scope}.json",
                {
                    "schema_version": 1,
                    "split": split,
                    "scope": scope,
                    "note": "Reserved, unscored sources. Variants are not independent observations.",
                    "cases": cases,
                },
            )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["list", "fetch", "verify"])
    parser.add_argument("--catalog", type=Path, default=CATALOG)
    parser.add_argument("--root", type=Path, default=MEDIA)
    parser.add_argument("--lock", type=Path, default=LOCK)
    parser.add_argument("--workers", type=int, default=2, choices=range(1, 5))
    parser.add_argument(
        "--freeze-lock",
        action="store_true",
        help="Record first acquisition; never overwrite a lock.",
    )
    args = parser.parse_args()
    catalog = load_catalog(args.catalog)
    lock = load_lock(args.lock, catalog["assets"])
    if args.freeze_lock and (args.command != "fetch" or args.lock.exists()):
        parser.error("--freeze-lock is only for a first fetch with no existing lock")
    if args.command == "list":
        print(
            json.dumps(
                {
                    "assets": len(catalog["assets"]),
                    "groups": len({a["group_id"] for a in catalog["assets"]}),
                    "bytes": sum(a["expected_bytes"] for a in catalog["assets"]),
                },
                indent=2,
            )
        )
    elif args.command == "fetch":
        if not lock and not args.freeze_lock:
            parser.error(
                "no source lock; use --freeze-lock only for an intentional first acquisition"
            )
        fetch(catalog, args.root, lock, args.workers)
        if args.freeze_lock:
            write_json(args.lock, json.loads((args.root / "acquisition.json").read_text()))
    else:
        verify(catalog, args.root, lock, args.workers)


if __name__ == "__main__":
    main()
