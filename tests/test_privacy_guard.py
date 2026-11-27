# -*- coding: utf-8 -*-
"""Regression tests for evidence privacy scrubbing (no real private data here)."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from privacy import find_hits, scrub, scrub_bytes  # noqa: E402


class PrivacyGuardTest(unittest.TestCase):
    def test_scrub_removes_captured_memory_and_credentials(self):
        sample = json.dumps({
            "raw": "团队动态 2026：用户希望被称呼为大哥；测试口令是 ABC123-DEF456；"
                   "sk-abcdef123456；Bearer abcdefghijklmnop；"
                   "00000000-1111-2222-3333-444444444444；ses_abcdefghijklmnop",
        }, ensure_ascii=False)
        self.assertTrue(find_hits(sample))
        redacted = scrub(sample)
        self.assertEqual(find_hits(redacted), [])
        self.assertIn("PRIVATE MEMORY REDACTED", redacted)

    def test_scrub_is_idempotent(self):
        sample = "sk-abcdef123456 00000000-1111-2222-3333-444444444444"
        once = scrub(sample)
        self.assertEqual(scrub(once), once)
        self.assertEqual(find_hits(once), [])

    def test_zip_members_are_scrubbed(self):
        import io
        import zipfile
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("dump.json", '{"content":"团队动态 2026 sk-abcdef123456"}')
        new, changed = scrub_bytes("probe.zip", buf.getvalue())
        self.assertTrue(changed)
        with zipfile.ZipFile(io.BytesIO(new)) as z:
            text = z.read("dump.json").decode("utf-8")
        self.assertEqual(find_hits(text), [])


if __name__ == "__main__":
    unittest.main()
