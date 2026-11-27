# -*- coding: utf-8 -*-
"""git filter-branch --index-filter helper: scrub every blob in the temp index.

This script is used only when rewriting the NEW branch. It never touches refs.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from privacy import scrub_bytes  # noqa: E402

# Rule/test definitions contain generic marker phrases, not captured secrets.
SKIP_PATHS = {
    "tools/privacy.py", "tools/scrub_evidence.py", "tools/privacy_scan.py",
    "tools/privacy_scan_remote.py", "tools/filter_index_privacy.py",
    "tests/test_privacy_guard.py",
}


def git_bytes(*args: str, input_data: bytes | None = None) -> bytes:
    p = subprocess.run(["git", *args], input=input_data, capture_output=True)
    if p.returncode:
        raise RuntimeError("git %s failed: %s" % (" ".join(args), p.stderr.decode("utf-8", "replace")))
    return p.stdout


def main() -> int:
    raw = git_bytes("ls-files", "-s", "-z")
    changed = 0
    for record in raw.split(b"\0"):
        if not record:
            continue
        head, path_b = record.split(b"\t", 1)
        mode, sha, stage = head.split()
        if stage != b"0":
            continue
        data = git_bytes("cat-file", "blob", sha.decode("ascii"))
        path = path_b.decode("utf-8", "replace")
        if path in SKIP_PATHS:
            continue
        new_data, did_change = scrub_bytes(path, data)
        if not did_change:
            continue
        new_sha = git_bytes("hash-object", "-w", "--stdin", input_data=new_data).decode("ascii").strip()
        git_bytes("update-index", "--cacheinfo", mode.decode("ascii"), new_sha, path)
        changed += 1
    if changed:
        print("privacy-filter: %d blob(s) scrubbed" % changed, file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
