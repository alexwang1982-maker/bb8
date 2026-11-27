# -*- coding: utf-8 -*-
"""Redact private/credential content from all repository evidence before commit.

Runs across the whole repository (not only evidence/real-env-continuous), skips
.git internals and binary databases, and rewrites ZIP members in place.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
from privacy import scrub_bytes  # noqa: E402

SKIP_DIRS = {".git", "_isolated", "_snapshot", "__pycache__"}
SKIP_SUFFIXES = {".db", ".db-wal", ".db-shm", ".pyc", ".exe", ".dll", ".png", ".jpg", ".jpeg"}
# These files DEFINE the privacy rules/tests. They contain generic marker phrases
# and fake samples, never captured private content; scrubbing them would break logic.
SKIP_PATHS = {
    "tools/privacy.py", "tools/scrub_evidence.py", "tools/privacy_scan.py",
    "tools/filter_index_privacy.py", "tests/test_privacy_guard.py",
}


def candidate_paths() -> list[Path]:
    out: list[Path] = []
    for p in ROOT.rglob("*"):
        if not p.is_file():
            continue
        rel = p.relative_to(ROOT)
        if any(part in SKIP_DIRS for part in rel.parts):
            continue
        if p.suffix.lower() in SKIP_SUFFIXES:
            continue
        if rel.as_posix() in SKIP_PATHS:
            continue
        out.append(p)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--path", default="", help="limit to one relative path (used by history rewrite)")
    args = ap.parse_args()

    if args.path:
        paths = [ROOT / args.path] if (ROOT / args.path).is_file() else []
    else:
        paths = candidate_paths()
    changed = 0
    replacements = 0
    for p in paths:
        data = p.read_bytes()
        new, n = scrub_bytes(p.name, data)
        # scrub_bytes reports boolean changed; approximate replacement count from size delta
        # is not meaningful, so count one per changed file plus ZIP members by size check.
        if new != data:
            changed += 1
            replacements += 1
            if not args.dry_run:
                p.write_bytes(new)
    print("files_changed=%d replacements=%d paths=%d" % (changed, replacements, len(paths)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
