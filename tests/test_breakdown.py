"""Run from the code folder: python3 -m unittest discover tests"""

import os
import unittest
from datetime import datetime, timezone

from superfan.breakdown import classify, _label, claude_outputs, codex_breakdown, copilot_outputs
from superfan.parse import scan
from superfan.advice import advise

HERE = os.path.dirname(os.path.abspath(__file__))
FIX = os.path.join(HERE, "fixtures")
SINCE = datetime(2026, 1, 1, tzinfo=timezone.utc)
UNTIL = datetime(2026, 1, 6, tzinfo=timezone.utc)


class Classify(unittest.TestCase):
    def test_tool_names(self):
        self.assertEqual(classify("Read", {"file_path": "/x/a.py"}), "reading files")
        self.assertEqual(classify("apply_patch", "*** Begin Patch"), "editing files")
        self.assertEqual(classify("wait_agent", "{}"), "subagent coordination")
        self.assertEqual(classify("mcp__notes__save", {}), "MCP tools")

    def test_shell_commands(self):
        self.assertEqual(classify("Bash", {"command": "cd app && bash scripts/run-gate.sh"}), "kit scripts and gate")
        self.assertEqual(classify("Bash", {"command": "FOO=1 bash ~/kit/scripts/e2e.sh"}), "kit scripts and gate")
        self.assertEqual(classify("Bash", {"command": "cat AGENTS.md; xcrun simctl list"}), "skills and agent docs")
        self.assertEqual(classify("Bash", {"command": "xcrun simctl boot X"}), "simulator / UI tests")
        self.assertEqual(classify("Bash", {"command": "git status --short"}), "git")

    def test_codex_exec_cell(self):
        cell = 'text(await tools.exec_command({cmd:"git diff --stat","yield_time_ms":1000}))'
        self.assertEqual(classify("exec", cell), "git")
        self.assertEqual(_label("exec", cell), "git diff")
        self.assertEqual(classify("exec", 'text(await tools.write_stdin({session_id:1,chars:""}))'),
                         "polling running commands")

    def test_labels(self):
        self.assertEqual(_label("Read", {"file_path": "/repo/src/app.ts"}), "Read app.ts")
        self.assertEqual(_label("Bash", {"command": "bash /p/scripts/context.sh --x"}), "scripts/context.sh")


class Outputs(unittest.TestCase):
    def test_claude_outputs_follow_threads(self):
        outs, _ = claude_outputs(os.path.join(FIX, "claude"), since=SINCE, until=UNTIL)
        main = [o for o in outs if o["agent_id"] == "main"]
        self.assertEqual([o["label"] for o in main], ["Agent harness-builder"])
        self.assertEqual(main[0]["calls_after"], 1)  # one more orchestrator call after the result

    def test_codex_breakdown_still_feeds_r3(self):
        b = codex_breakdown(os.path.join(FIX, "codex"), SINCE, UNTIL)
        self.assertTrue(all(len(t) == 5 for t in b["outputs"]))

    def test_copilot_outputs_follow_agents(self):
        outs, secs = copilot_outputs(os.path.join(FIX, "copilot"), since=SINCE, until=UNTIL)
        by = {o["label"]: o for o in outs}
        self.assertEqual(by["scripts/status.sh"]["category"], "kit scripts and gate")
        self.assertEqual(by["scripts/status.sh"]["tokens"], 1000)  # result.content, not detailedContent
        self.assertEqual(by["scripts/status.sh"]["calls_after"], 2)  # orchestrator calls only
        self.assertEqual(by["view AGENTS.md"]["agent"], "harness-builder")
        self.assertEqual(by["view AGENTS.md"]["calls_after"], 2)
        self.assertIsNone(by["view AGENTS.md"]["carried_usd"])
        self.assertEqual(secs["kit scripts and gate"], 2)
        self.assertEqual(by["git log"]["calls_after"], 1)  # older CLI: assistant messages count as calls


class Advice(unittest.TestCase):
    def test_orchestrator_tip(self):
        [s] = scan(codex_root=os.path.join(FIX, "codex"), since=SINCE, until=UNTIL)
        titles = [t["title"] for t in advise(s, [], orchestrator_limit=0.30)]
        self.assertIn("The orchestrator is doing work its subagents could do", titles)

    def test_copilot_tips(self):
        [s] = [s for s in scan(copilot_root=os.path.join(FIX, "copilot"), since=SINCE, until=UNTIL) if s["project"] == "repo"]
        tips = {t["title"]: t for t in advise(s, [])}
        self.assertIn("harness-builder ran 1 of them", tips["Context compaction is eating tokens"]["evidence"])
        self.assertEqual(tips["Tool definitions fill the prompt"]["tokens"], (120000 - 9000) * 3)


if __name__ == "__main__":
    unittest.main()
