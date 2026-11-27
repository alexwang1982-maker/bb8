# -*- coding: utf-8 -*-
"""Full privacy scan for BB-8 evidence: worktree, current tree and Git history.

Usage:
  python tools/privacy_scan.py --mode all --json evidence/privacy-scan.json
  python tools/privacy_scan.py --mode history --ref HEAD

Exit code 1 means at least one sensitive pattern remains.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
from privacy import find_hits_in_bytes  # noqa: E402

# Rule definitions and their synthetic regression fixtures. They intentionally
# contain fake credential/memory shapes, never captured private content.
SKIP_PATHS = {
    "tools/privacy.py", "tools/scrub_evidence.py", "tools/privacy_scan.py",
    "tools/filter_index_privacy.py", "tests/test_privacy_guard.py",
}


def git(*args: str) -> str:
    p = subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True,
                       text=True, encoding="utf-8", errors="replace")
    if p.returncode:
        raise RuntimeError("git %s failed: %s" % (" ".join(args), p.stderr.strip()))
    return p.stdout


def _paths_from_worktree() -> list[str]:
    raw = subprocess.run(["git", "-C", str(ROOT), "ls-files", "-co", "--exclude-standard", "-z"],
                         capture_output=True).stdout
    return [p.decode("utf-8", "replace") for p in raw.split(b"\0") if p]


def _paths_from_tree() -> list[str]:
    return [p for p in git("ls-tree", "-r", "--name-only", "HEAD").splitlines() if p]


def _add(hits: dict, location: str, name: str, data: bytes) -> None:
    for rule, sample in find_hits_in_bytes(name, data):
        key = (location, name, rule)
        hits[key]["count"] += 1
        if sample not in hits[key]["samples"]:
            hits[key]["samples"].append(sample)


def scan_worktree(hits: dict) -> int:
    count = 0
    for rel in _paths_from_worktree():
        if rel.replace("\\", "/") in SKIP_PATHS:
            continue
        p = ROOT / rel
        if not p.is_file():
            continue
        count += 1
        _add(hits, "worktree", rel, p.read_bytes())
    return count


def scan_tree(hits: dict) -> int:
    paths = _paths_from_tree()
    for rel in paths:
        if rel.replace("\\", "/") in SKIP_PATHS:
            continue
        p = ROOT / rel
        if p.is_file():
            _add(hits, "tree:HEAD", rel, p.read_bytes())
        else:
            data = subprocess.run(["git", "-C", str(ROOT), "show", "HEAD:" + rel],
                                  capture_output=True).stdout
            _add(hits, "tree:HEAD", rel, data)
    return len(paths)


def scan_history(hits: dict, ref: str) -> int:
    raw = git("rev-list", "--objects", ref).splitlines()
    objects = []
    seen: set[str] = set()
    for line in raw:
        parts = line.split(" ", 1)
        if not parts or len(parts[0]) != 40:
            continue
        sha = parts[0]
        if sha in seen:
            continue
        seen.add(sha)
        path = parts[1] if len(parts) > 1 else "<no-path>"
        objects.append((sha, path))
    scanned = 0
    for sha, path in objects:
        if path.replace("\\", "/") in SKIP_PATHS:
            continue
        data = subprocess.run(["git", "-C", str(ROOT), "cat-file", "blob", sha],
                              capture_output=True).stdout
        _add(hits, "history:" + ref, path, data)
        scanned += 1
    return scanned


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["all", "worktree", "tree", "history"], default="all")
    ap.add_argument("--ref", default="HEAD")
    ap.add_argument("--json", default="", help="optional report path")
    args = ap.parse_args()

    hits: dict = defaultdict(lambda: {"count": 0, "samples": []})
    scanned = {}
    if args.mode in ("all", "worktree"):
        scanned["worktree_files"] = scan_worktree(hits)
    if args.mode in ("all", "tree"):
        scanned["tree_files"] = scan_tree(hits)
    if args.mode in ("all", "history"):
        scanned["history_objects"] = scan_history(hits, args.ref)

    rows = []
    for (location, path, rule), info in sorted(hits.items()):
        rows.append({"location": location, "path": path, "rule": rule, **info})
    report = {"mode": args.mode, "ref": args.ref, "scanned": scanned,
              "hit_rows": len(rows), "total_hits": sum(r["count"] for r in rows),
              "hits": rows}
    print("privacy_scan mode=%s scanned=%s hit_rows=%d total_hits=%d" %
          (args.mode, json.dumps(scanned, sort_keys=True), report["hit_rows"],
           report["total_hits"]))
    for r in rows[:50]:
        print("HIT %s %s %s x%d" % (r["location"], r["path"], r["rule"], r["count"]))
    if args.json:
        out = Path(args.json)
        if not out.is_absolute():
            out = ROOT / out
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print("report=" + str(out))
    return 1 if rows else 0


if __name__ == "__main__":
    sys.exit(main())
