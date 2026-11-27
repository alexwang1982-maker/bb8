# -*- coding: utf-8 -*-
"""Privacy scan for every ref in a fetched remote repository mirror.

Usage:
  python tools/privacy_scan_remote.py --repo D:\\Temp\\opencode\\bb8-remote-audit.git \
      --json D:\\Temp\\opencode\\remote-privacy-scan-before.json

Only counts are recorded. Raw matches/samples are never written to disk.
Exit code 1 means at least one ref still reaches sensitive content.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from privacy import find_hits_in_bytes  # noqa: E402

# Rule definitions and their synthetic fixtures are not captured evidence.
SKIP_PATHS = {
    "tools/privacy.py", "tools/scrub_evidence.py", "tools/privacy_scan.py",
    "tools/privacy_scan_remote.py", "tools/filter_index_privacy.py",
    "tests/test_privacy_guard.py",
}


def git_text(repo: Path, *args: str) -> str:
    p = subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                       text=True, encoding="utf-8", errors="replace")
    if p.returncode:
        raise RuntimeError("git %s failed: %s" % (" ".join(args), p.stderr.strip()))
    return p.stdout


def git_bytes(repo: Path, *args: str) -> bytes:
    p = subprocess.run(["git", "-C", str(repo), *args], capture_output=True)
    if p.returncode:
        raise RuntimeError("git %s failed: %s" %
                           (" ".join(args), p.stderr.decode("utf-8", "replace").strip()))
    return p.stdout


def list_refs(repo: Path) -> list[dict]:
    rows = []
    for line in git_text(repo, "for-each-ref", "--format=%(refname)%09%(objectname)").splitlines():
        name, sha = line.split("\t", 1)
        if name.startswith("refs/pull/"):
            kind = "pull"
        elif name.startswith("refs/heads/"):
            kind = "branch"
        elif name.startswith("refs/tags/"):
            kind = "tag"
        else:
            kind = "other"
        rows.append({"name": name, "sha": sha, "kind": kind})
    return rows


def commits_for_path(repo: Path, ref: str, path: str) -> list[str]:
    raw = git_text(repo, "rev-list", "--reverse", ref, "--", path).splitlines()
    return raw


def scan_ref(repo: Path, ref: dict) -> dict:
    name = ref["name"]
    raw = git_text(repo, "rev-list", "--objects", name).splitlines()
    seen: set[str] = set()
    counts: dict[tuple[str, str], int] = defaultdict(int)
    scanned = 0
    for line in raw:
        parts = line.split(" ", 1)
        if len(parts) != 2 or len(parts[0]) != 40:
            continue
        sha, path = parts
        if sha in seen:
            continue
        seen.add(sha)
        if path.replace("\\", "/") in SKIP_PATHS:
            continue
        if git_text(repo, "cat-file", "-t", sha).strip() != "blob":
            continue
        data = git_bytes(repo, "cat-file", "blob", sha)
        for rule, _sample in find_hits_in_bytes(path, data):
            counts[(path, rule)] += 1
        scanned += 1

    rows = []
    for (path, rule), count in sorted(counts.items()):
        commits = commits_for_path(repo, name, path)
        rows.append({
            "ref": name, "ref_sha": ref["sha"], "path": path, "rule": rule,
            "count": count, "introduced_commit": commits[0] if commits else ref["sha"],
            "touching_commits": len(commits),
        })
    return {
        "ref": name, "ref_sha": ref["sha"], "kind": ref["kind"],
        "objects_scanned": scanned, "hit_rows": len(rows),
        "total_hits": sum(r["count"] for r in rows), "hits": rows,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--json", required=True)
    ap.add_argument("--refs", default="", help="comma-separated ref names; default all")
    args = ap.parse_args()

    repo = Path(args.repo).resolve()
    refs = list_refs(repo)
    if args.refs:
        wanted = {s.strip() for s in args.refs.split(",") if s.strip()}
        refs = [r for r in refs if r["name"] in wanted]
    results = [scan_ref(repo, r) for r in refs]
    report = {
        "repo": str(repo),
        "refs_scanned": len(results),
        "objects_scanned": sum(r["objects_scanned"] for r in results),
        "hit_rows": sum(r["hit_rows"] for r in results),
        "total_hits": sum(r["total_hits"] for r in results),
        "refs": results,
    }
    out = Path(args.json)
    if not out.is_absolute():
        out = HERE.parent / out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print("remote_scan refs=%d objects=%d hit_rows=%d total_hits=%d report=%s" %
          (report["refs_scanned"], report["objects_scanned"], report["hit_rows"],
           report["total_hits"], out))
    for r in results:
        print("REF %s %s objects=%d hits=%d" %
              (r["ref"], r["ref_sha"][:12], r["objects_scanned"], r["total_hits"]))
    return 1 if report["hit_rows"] else 0


if __name__ == "__main__":
    sys.exit(main())
