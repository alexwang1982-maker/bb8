# -*- coding: utf-8 -*-
"""Redact credential-shaped strings from captured evidence before committing.

Only credential patterns are replaced; request/response structure, task IDs,
timestamps and model replies are preserved. Run before every evidence commit.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EVID = ROOT / "evidence" / "real-env-continuous"

PATTERNS = [
    # full or truncated API keys of the sk- family
    # Negative lookbehind prevents matching the "sk-" inside words like "task-result".
    (re.compile(r"(?<![A-Za-z0-9_\-])sk-[A-Za-z0-9]{6,}(?:…|\.\.\.)?"), "sk-REDACTED"),
    # bearer tokens in any captured header dump
    (re.compile(r"(?i)(Bearer\s+)[A-Za-z0-9_\-\.]{12,}"), r"\1REDACTED"),
]


def main():
    changed = 0
    hits = 0
    for p in EVID.rglob("*"):
        if not p.is_file() or p.suffix.lower() not in (".json", ".txt", ".md"):
            continue
        text = p.read_text(encoding="utf-8", errors="replace")
        new = text
        for pat, repl in PATTERNS:
            new, n = pat.subn(repl, new)
            hits += n
        if new != text:
            p.write_text(new, encoding="utf-8")
            changed += 1
    print("files_changed=%d replacements=%d" % (changed, hits))


if __name__ == "__main__":
    main()
