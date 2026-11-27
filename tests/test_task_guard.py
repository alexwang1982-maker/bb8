# -*- coding: utf-8 -*-
"""Regression tests: a task is never verified without a valid expected marker."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_TMP_DIR = tempfile.mkdtemp(prefix="bb8-task-guard-")
os.environ["OPENMUSE_TASK_DB"] = str(Path(_TMP_DIR) / "tasks.db")
sys.path.insert(0, str(ROOT / "bridge"))

import task_engine as te  # noqa: E402

te.init_schema()


class TaskGuardTest(unittest.TestCase):
    def _success(self, marker="MARKER-OK", stdout=None):
        r = te.submit("key-" + marker, "hermes_cli",
                      {"query": "real skill", "_expected_marker": marker},
                      step_id="step-" + marker)
        tid = r["task"]["id"]
        with te._tx() as c:
            c.execute("UPDATE tasks SET status=?,result_json=?,error=NULL WHERE id=?",
                      (te.SUCCEEDED,
                       json.dumps({"exit_code": 0, "stdout": stdout if stdout is not None else marker}),
                       tid))
        return tid

    def test_submit_rejects_missing_or_blank_marker(self):
        with self.assertRaises(ValueError):
            te.submit("k-no-marker", "hermes_cli", {"query": "x"}, step_id="s1")
        with self.assertRaises(ValueError):
            te.submit("k-blank-marker", "hermes_cli",
                      {"query": "x", "_expected_marker": "   "}, step_id="s2")

    def test_submit_requires_step_id(self):
        with self.assertRaises(ValueError):
            te.submit("k-no-step", "hermes_cli",
                      {"query": "x", "_expected_marker": "MARKER-OK"})

    def test_verified_requires_stored_marker(self):
        tid = self._success("MARKER-STORED")
        v = te.verified_status(tid)
        self.assertTrue(v["verified"], v["reason"])
        self.assertEqual(v["status"], te.SUCCEEDED)
        self.assertFalse(te.verified_status(tid, "WRONG-MARKER")["verified"])
        self.assertFalse(te.verified_status(tid, "   ")["verified"])

    def test_legacy_success_without_marker_is_not_verified(self):
        with te._tx() as c:
            c.execute("""INSERT INTO tasks(id,idempotency_key,kind,payload_json,status,
                         max_attempts,retry_policy,next_run_at,created_at,updated_at,step_id)
                         VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                      ("tsk_legacy", "k-legacy", "hermes_cli", json.dumps({"query": "x"}),
                       te.SUCCEEDED, 1, "safe", 0, 0, 0, "s-legacy"))
            c.execute("UPDATE tasks SET result_json=? WHERE id=?",
                      (json.dumps({"exit_code": 0, "stdout": "anything"}), "tsk_legacy"))
        self.assertFalse(te.verified_status("tsk_legacy")["verified"])

    def test_marker_missing_helper_is_strict(self):
        self.assertTrue(te.marker_missing("", "anything"))
        self.assertTrue(te.marker_missing("   ", "anything"))
        self.assertTrue(te.marker_missing("EXPECTED", "other output"))
        self.assertFalse(te.marker_missing("EXPECTED", "prefix EXPECTED suffix"))

    def test_find_step_success_ignores_wrong_marker(self):
        tid = self._success("MARKER-STEP")
        self.assertIsNone(te.find_step_success("step-MARKER-STEP", "WRONG"))
        found = te.find_step_success("step-MARKER-STEP", "MARKER-STEP")
        self.assertTrue(found and found["verified"])


if __name__ == "__main__":
    unittest.main()
