"""Health rules R4 and R5 on the synthetic Copilot session."""

import os
import unittest
from datetime import datetime, timezone

from superfan import rules
from superfan.parse import scan

FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


class CopilotRules(unittest.TestCase):
    def setUp(self):
        self.ss = scan(copilot_root=os.path.join(FIX, "copilot"), since=datetime(2026, 1, 1, tzinfo=timezone.utc),
                       until=datetime(2026, 1, 6, tzinfo=timezone.utc))

    def test_compaction(self):
        self.assertEqual(rules.frequent_compaction(self.ss), [])  # one compaction is fine
        fs = rules.frequent_compaction(self.ss, limit=1)
        self.assertTrue(fs)
        for f in fs:
            self.assertEqual(f["rule"], "R4 frequent compaction")
        sub = next(f for f in fs if "subagent" in f["agent"])
        self.assertGreater(sub["tokens"], 0)

    def test_tool_definitions(self):
        biggest = max(a.get("tool_def_tokens") or 0 for s in self.ss for a in s["agents"])
        self.assertGreater(biggest, 0)
        self.assertEqual(rules.tool_definitions(self.ss, limit=biggest + 1), [])
        fs = rules.tool_definitions(self.ss, limit=biggest)
        self.assertTrue(fs and all(f["rule"] == "R5 tool definitions" for f in fs))
        self.assertIn("tool definitions", fs[0]["evidence"])

    def test_other_harnesses_never_fire(self):
        cc = scan(claude_root=os.path.join(FIX, "claude"), codex_root=os.path.join(FIX, "codex"),
                  prices_table={}, since=datetime(2026, 1, 1, tzinfo=timezone.utc),
                  until=datetime(2026, 1, 6, tzinfo=timezone.utc))
        self.assertEqual(rules.frequent_compaction(cc, limit=1) + rules.tool_definitions(cc, limit=1), [])


if __name__ == "__main__":
    unittest.main()
