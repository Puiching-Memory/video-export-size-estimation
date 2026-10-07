"""Read Git refs and checkpoint filenames; never inspect research result bodies."""

import argparse
from collections import Counter
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
REPOSITORY = "Puiching-Memory/video-export-size-estimation"
MARKER = re.compile(
    r"^(completion|completed|final[-_]freeze|source[-_]freeze)"
    r"(?:[-_].*)?\.json(?:\.gz)?$"
)


def command(arguments, allow_failure=False):
    result = subprocess.run(arguments, cwd=ROOT, capture_output=True, check=False, timeout=30)
    if result.returncode and not allow_failure:
        raise RuntimeError(result.stderr.decode(errors="replace").strip())
    return result


def git(*arguments, allow_failure=False):
    return command(["git", *arguments], allow_failure=allow_failure)


def names(*arguments):
    return set(git(*arguments).stdout.decode().rstrip("\0").split("\0")) - {""}


def commit_names(sha):
    if not sha or git("cat-file", "-e", sha + "^{commit}", allow_failure=True).returncode:
        return None
    return names("ls-tree", "-rz", "--name-only", sha, "--", "results")


def staged_markers():
    entries = names("ls-files", "--stage", "-z", "--", "results")
    head_entries = names("ls-tree", "-rz", "HEAD", "--", "results")
    head_blobs = {}
    for entry in head_entries:
        metadata, path = entry.split("\t", 1)
        head_blobs[path] = metadata.split()[2]
    paths = set()
    for entry in entries:
        metadata, path = entry.split("\t", 1)
        if marker_kind(path) and metadata.split()[1] != head_blobs.get(path):
            paths.add(path)
    return sorted(paths)


def marker_kind(path):
    match = MARKER.fullmatch(PurePosixPath(path).name)
    return match.group(1).replace("_", "-") if match else None


def status(arguments):
    branch = git("branch", "--show-current").stdout.decode().strip()
    head = git("rev-parse", "HEAD").stdout.decode().strip()
    ref = arguments.ref or ("origin/" + branch if branch else "")
    comparison = {
        "ref": ref or None,
        "verification": "live_github_api" if arguments.remote else "local_cached_ref_only",
        "head": None,
        "relationship": "unknown",
        "error": None,
    }
    if arguments.remote:
        if not ref.startswith("origin/"):
            raise ValueError("--remote requires an origin/branch comparison ref")
        remote_branch = ref.removeprefix("origin/")
        response = command(
            [
                "gh",
                "api",
                f"repos/{arguments.repository}/git/ref/heads/{remote_branch}",
                "--jq",
                ".object.sha",
            ],
            allow_failure=True,
        )
    else:
        response = git("rev-parse", "--verify", ref, allow_failure=True) if ref else None
    if response is not None and response.returncode == 0:
        candidate = response.stdout.decode().strip()
        if re.fullmatch(r"[0-9a-f]{40}", candidate):
            comparison["head"] = candidate
        else:
            comparison["error"] = "Comparison ref did not return a commit SHA"
    else:
        comparison["error"] = "Comparison ref unavailable; synchronization is unverified"
    compared = comparison["head"]
    if compared == head:
        comparison["relationship"] = "equal"
        comparison.update(ahead=0, behind=0)
    elif compared and commit_names(compared) is not None:
        counts = git("rev-list", "--left-right", "--count", head + "..." + compared)
        ahead, behind = map(int, counts.stdout.split())
        comparison.update(ahead=ahead, behind=behind)
        comparison["relationship"] = (
            "diverged" if ahead and behind else "local_ahead" if ahead else "local_behind"
        )
    indexed = names("ls-files", "--cached", "-z", "--", "results")
    untracked = names("ls-files", "--others", "--exclude-standard", "-z", "--", "results")
    in_head = commit_names(head)
    in_comparison = commit_names(compared)
    paths = sorted(path for path in indexed | untracked if marker_kind(path))
    files = []
    for path in paths:
        files.append(
            {
                "path": path,
                "kind": marker_kind(path),
                "tracking": "untracked" if path in untracked else "tracked_in_index",
                "in_head": path in in_head,
                "in_comparison_commit": (
                    path in in_comparison if in_comparison is not None else None
                ),
            }
        )
    tracking = Counter(item["tracking"] for item in files)
    missing = (
        sum(not item["in_comparison_commit"] for item in files)
        if in_comparison is not None
        else None
    )
    dirty_markers = staged_markers()
    return {
        "repository": arguments.repository,
        "branch": branch or None,
        "head": head,
        "comparison": comparison,
        "sealed_checkpoint_files": {
            "status_not_inferred": True,
            "total": len(files),
            "tracked_in_index": tracking["tracked_in_index"],
            "untracked": tracking["untracked"],
            "absent_from_head": sum(not item["in_head"] for item in files),
            "absent_from_comparison_commit": missing,
            "staged_marker_paths": dirty_markers,
            "unstaged_body_changes_not_checked": True,
            "files": files[: arguments.max_files],
            "omitted_files": max(0, len(files) - arguments.max_files),
        },
        "pending_publication": (
            comparison["relationship"] != "equal" or missing != 0 or bool(dirty_markers)
        ),
        "scope": (
            "Git metadata and filenames only. Marker presence does not establish PASS, "
            "scientific validation, or complete publication of a marker's family. "
            "No media, gzip body, result values, or risk labels are opened. "
            "Cached refs are not live remote evidence. No Git or GitHub writes occur."
        ),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ref", help="Comparison ref; defaults to origin/current-branch")
    parser.add_argument("--repository", default=REPOSITORY)
    parser.add_argument("--remote", action="store_true", help="Read the actual GitHub ref via gh")
    parser.add_argument(
        "--max-files", type=int, default=20, help="Marker paths to show; 0 hides paths"
    )
    parser.add_argument(
        "--require-synced", action="store_true", help="Exit 1 unless HEAD equals the comparison ref"
    )
    arguments = parser.parse_args()
    if arguments.max_files < 0:
        parser.error("--max-files must be nonnegative")
    report = status(arguments)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if arguments.require_synced and report["comparison"]["relationship"] != "equal":
        return 1
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as error:
        print(json.dumps({"error": str(error), "synchronization_verified": False}))
        sys.exit(2)
