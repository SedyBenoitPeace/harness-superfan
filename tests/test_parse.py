"""Run from the code folder: python3 -m unittest discover tests"""

import json
import os
import unittest
from datetime import datetime, timezone

from superfan.parse import scan

HERE = os.path.dirname(os.path.abspath(__file__))
FIX = os.path.join(HERE, "fixtures")
PRICES = json.load(open(os.path.join(HERE, "..", "superfan", "prices.json")))
SINCE = datetime(2026, 1, 1, tzinfo=timezone.utc)
UNTIL = datetime(2026, 1, 6, tzinfo=timezone.utc)


class ClaudeCode(unittest.TestCase):
    def setUp(self):
        [self.s] = scan(claude_root=os.path.join(FIX, "claude"), prices_table=PRICES, since=SINCE, until=UNTIL)
        self.by = {a["agent_id"]: a for a in self.s["agents"]}

    def test_split_by_agent(self):
        self.assertEqual(self.by["main"]["role"], "orchestrator")
        self.assertEqual(self.by["main"]["tokens"]["total"], 42800)  # duplicated usage line counted once
        self.assertEqual(self.by["a1"]["tokens"]["total"], 36400)    # subagents/ file
        self.assertEqual(self.by["a1"]["label"], "harness-builder")  # from the parent's tool result
        self.assertEqual(self.by["a2"]["tokens"]["total"], 6000)     # inline sidechain lines
        self.assertAlmostEqual(self.s["orchestration_share"], 0.5023, places=4)

    def test_cost(self):
        self.assertAlmostEqual(self.by["main"]["cost_usd"], 0.181, places=4)  # 1h cache write at 2x
        self.assertAlmostEqual(self.by["a1"]["cost_usd"], 0.0446, places=4)   # 5m cache write at 1.25x
        self.assertAlmostEqual(self.s["cost_usd"], 0.2266, places=4)

    def test_tools_and_models(self):
        self.assertEqual(self.s["tool_calls"], 5)
        self.assertEqual(self.s["models"], ["claude-haiku-5-5", "claude-opus-5-5", "claude-sonnet-5-5"])


class Codex(unittest.TestCase):
    """Fixtures in the format of Codex CLI 0.162.1 (copied from the kit's
    synthetic test data): one orchestrator, four builders, one guardian."""

    def test_threads(self):
        [s] = scan(codex_root=os.path.join(FIX, "codex"), since=SINCE, until=UNTIL)
        self.assertIsNone(s["cost_usd"])  # Codex is never priced
        self.assertEqual(s["tokens"]["total"], 2330000)
        roles = sorted((a["role"], a["tokens"]["total"]) for a in s["agents"])
        self.assertEqual(roles[0], ("orchestrator", 906000))
        self.assertEqual(roles[1], ("reviewer", 60500))
        self.assertEqual(s["subagents"], 4)
        self.assertAlmostEqual(s["orchestration_share"], 966500 / 2330000, places=4)


class Copilot(unittest.TestCase):
    """Synthetic Copilot CLI session: orchestrator plus one subagent that compacts once."""

    def setUp(self):
        [self.s] = scan(copilot_root=os.path.join(FIX, "copilot"), since=SINCE, until=UNTIL)
        self.by = {a["role"]: a for a in self.s["agents"]}

    def test_tokens_only(self):
        self.assertEqual(self.s["harness"], "copilot")
        self.assertEqual(self.s["project"], "repo")  # from workspace.yaml git_root
        self.assertIsNone(self.s["cost_usd"])
        self.assertIsNone(self.s["cost_parts"])

    def test_split_by_agent(self):
        o, w = self.by["orchestrator"], self.by["worker"]
        # inputTokens includes cache reads and writes: fresh is the rest
        self.assertEqual(o["tokens"], {"input": 2000, "cache_read": 41000, "cache_write": 24000, "output": 900, "total": 67900})
        self.assertEqual(w["label"], "harness-builder")
        self.assertEqual(w["model_calls"], 3)
        self.assertEqual(w["tokens"]["total"], 614300)  # 468,300 in usage records + 146,000 compaction
        self.assertAlmostEqual(self.s["orchestration_share"], 67900 / 682200, places=4)

    def test_compaction_and_tool_definitions(self):
        w = self.by["worker"]
        self.assertEqual((w["compactions"], w["compaction_tokens"]), (1, 146000))
        self.assertEqual(self.s["compaction_tokens"], 146000)
        self.assertEqual(w["tool_def_tokens"], 120000)                    # from compaction_start
        self.assertEqual(self.by["orchestrator"]["tool_def_tokens"], 9000)  # from usage_checkpoint
        self.assertEqual(self.s["tools"], {"bash": 1, "task": 1, "view": 1, "edit": 1})


if __name__ == "__main__":
    unittest.main()
