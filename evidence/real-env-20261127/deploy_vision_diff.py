# -*- coding: utf-8 -*-
"""Controlled replay of the delivered vision diff onto the real production file.

The production file was already at the post-change SHA from the prior task.
To satisfy the deployment gate, restore the exact verified baseline blob from
the delivery branch, apply the branch diff in an isolated directory, verify the
post-change SHA, then copy the verified bytes into production.
"""
from __future__ import annotations

import hashlib
import shutil
import subprocess
from pathlib import Path

REPO = Path(r"D:\Temp\opencode\bb8-vision-brain-work")
WORK = Path(r"D:\Temp\opencode\bb8-real-deploy-20261127")
EVIDENCE = WORK / "evidence"
APPLY_DIR = WORK / "apply"
PROD = Path(r"G:\openmuse\xiaozhi-server\bridge\bridge.py")
NORMALIZED_DIFF = REPO / "evidence" / "BB8-vision-brain-context.normalized.diff"

BASELINE_BLOB = "9015bb8"
BASELINE_SHA = "73D0F2036E48DE4024316AC00DD74E04E09D58AB7FF56D573642BD20B20189C9"
TARGET_SHA = "942B7EBF1BEE39AC0A00848DE53EF90FE3110C511A200283F54E19E9BC933E60"

EVIDENCE.mkdir(parents=True, exist_ok=True)
APPLY_DIR.mkdir(parents=True, exist_ok=True)
log_lines: list[str] = []


def log(msg: str) -> None:
    line = str(msg)
    print(line, flush=True)
    log_lines.append(line)


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest().upper()


def blob_bytes(rev: str) -> bytes:
    p = subprocess.run(
        ["git", "-C", str(REPO), "cat-file", "-p", rev],
        capture_output=True,
        check=True,
    )
    return p.stdout


current = PROD.read_bytes()
current_sha = sha256(current)
log("PRODUCTION_BEFORE_SHA=" + current_sha)
log("PRODUCTION_BEFORE_BYTES=" + str(len(current)))

backup = EVIDENCE / ("bridge.py.before-real-deploy-" + current_sha[:12])
shutil.copyfile(PROD, backup)
log("BACKUP=" + str(backup))
log("BACKUP_SHA=" + sha256(backup.read_bytes()))

baseline = blob_bytes(BASELINE_BLOB)
baseline_sha = sha256(baseline)
log("BASELINE_BLOB=" + BASELINE_BLOB)
log("BASELINE_SHA=" + baseline_sha)
log("BASELINE_BYTES=" + str(len(baseline)))

if baseline_sha != BASELINE_SHA:
    raise SystemExit("FATAL: baseline blob hash does not match the required baseline SHA")

apply_file = APPLY_DIR / "bridge.py"
apply_file.write_bytes(baseline)
log("APPLY_INPUT_SHA=" + sha256(apply_file.read_bytes()))

git_dir = APPLY_DIR / ".git"
if not git_dir.exists():
    subprocess.run(["git", "init", "-q"], cwd=str(APPLY_DIR), check=True)

check = subprocess.run(
    ["git", "-c", "core.autocrlf=false", "-c", "core.eol=lf", "apply",
     "--check", "--unsafe-paths", str(NORMALIZED_DIFF)],
    cwd=str(APPLY_DIR), capture_output=True, text=True,
)
log("GIT_APPLY_CHECK_EXIT=" + str(check.returncode))
if check.stdout.strip():
    log("GIT_APPLY_CHECK_STDOUT=" + check.stdout.strip())
if check.stderr.strip():
    log("GIT_APPLY_CHECK_STDERR=" + check.stderr.strip())
if check.returncode != 0:
    raise SystemExit("FATAL: git apply --check failed")

applied = subprocess.run(
    ["git", "-c", "core.autocrlf=false", "-c", "core.eol=lf", "apply",
     "--unsafe-paths", str(NORMALIZED_DIFF)],
    cwd=str(APPLY_DIR), capture_output=True, text=True,
)
log("GIT_APPLY_EXIT=" + str(applied.returncode))
if applied.stdout.strip():
    log("GIT_APPLY_STDOUT=" + applied.stdout.strip())
if applied.stderr.strip():
    log("GIT_APPLY_STDERR=" + applied.stderr.strip())
if applied.returncode != 0:
    raise SystemExit("FATAL: git apply failed")

patched = apply_file.read_bytes()
patched_sha = sha256(patched)
log("APPLY_OUTPUT_SHA=" + patched_sha)
log("APPLY_OUTPUT_BYTES=" + str(len(patched)))

if patched_sha != TARGET_SHA:
    raise SystemExit("FATAL: patched hash does not match the required target SHA")

PROD.write_bytes(patched)
final_sha = sha256(PROD.read_bytes())
log("PRODUCTION_AFTER_SHA=" + final_sha)
log("PRODUCTION_AFTER_BYTES=" + str(len(PROD.read_bytes())))

if final_sha != TARGET_SHA:
    raise SystemExit("FATAL: production hash after copy does not match target SHA")

log("DEPLOY_RESULT=PASS")

(EVIDENCE / "deploy-vision-diff.raw.txt").write_text(
    "\n".join(log_lines) + "\n", encoding="utf-8", newline="\n"
)
