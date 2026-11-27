# -*- coding: utf-8 -*-
"""One-off repair for the over-greedy first scrub pass (task-* filename refs)."""
from pathlib import Path

BASE = Path(__file__).resolve().parents[1] / "evidence" / "real-env-continuous"
for p in sorted(BASE.glob("run*/SUMMARY.json")):
    text = p.read_text(encoding="utf-8")
    text = text.replace("c4-task-REDACTED.raw.json", "c4-task-events.raw.json")
    text = text.replace("task-REDACTED*.json", "task-result-*.json")
    for n in ("001", "002", "003"):
        text = text.replace('"task-REDACTED.json"', '"task-result-%s.json"' % n, 1)
    p.write_text(text, encoding="utf-8")
    print(p.name, "repaired")
