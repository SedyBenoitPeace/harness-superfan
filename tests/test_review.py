"""`superfan review`: numbers, saving and loading, on the synthetic fixtures."""

import json
import os
import tempfile
import unittest
from datetime import date, datetime, timezone

from superfan import review

HERE = os.path.dirname(os.path.abspath(__file__))
FIX = os.path.join(HERE, "fixtures")
PRICES = json.load(open(os.path.join(HERE, "..", "superfan", "prices.json")))
CFG = {"claude": os.path.join(FIX, "claude"), "codex": os.path.join(FIX, "codex"), "reports": [],
       "tz": timezone.utc, "prices": PRICES, "orchestrator_limit": 0.40, "output_limit": 8000,
       "ask_model": "", "ask_codex_model": ""}


class Review(unittest.TestCase):
    def test_day_numbers_only(self):
        since, until = review.day_window(date(2026, 1, 5), timezone.utc)
        with tempfile.TemporaryDirectory() as d:
            r = review.run(CFG, "day", since, until, engines=[], out_dir=d, compare_days=2)
            self.assertIn("codex", r["data"]["totals"])
            self.assertEqual([x["day"] for x in r["data"]["previous_days"]], ["2026-01-03", "2026-01-04"])
            self.assertIn("# Daily review: 2026-01-05", r["markdown"])
            self.assertNotIn("## Summary", r["markdown"])
            [row] = review.list_reviews(d)
            self.assertEqual(row["scope"], "day")
            self.assertEqual(review.load(row["name"], d), r["markdown"])
            self.assertTrue(os.path.exists(os.path.join(d, row["name"][:-3] + ".json")))

    def test_load_refuses_paths(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertIsNone(review.load("../etc/passwd", d))
            self.assertIsNone(review.load("2026-01-05-0900-day.md/../../x", d))

    def test_prompt_has_numbers_not_transcripts(self):
        data = review.build(CFG, "window", datetime(2026, 1, 1, tzinfo=timezone.utc),
                            datetime(2026, 1, 6, tzinfo=timezone.utc))
        p = review.prompt(data)
        self.assertIn("Do not run commands or read files", p)
        self.assertIn('"top_sessions"', p)

    def test_failed_model_keeps_numbers(self):
        old = review.advice.ask
        review.advice.ask = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("not installed"))
        try:
            with tempfile.TemporaryDirectory() as d:
                r = review.run(CFG, "window", datetime(2026, 1, 1, tzinfo=timezone.utc),
                               datetime(2026, 1, 6, tzinfo=timezone.utc), engines=["claude"], out_dir=d)
        finally:
            review.advice.ask = old
        self.assertIn("claude", r["errors"])
        self.assertIn("## Numbers", r["markdown"])

    def test_delete_and_prune_stay_in_folder(self):
        since, until = review.day_window(date(2026, 1, 5), timezone.utc)
        with tempfile.TemporaryDirectory() as d:
            keep = os.path.join(d, "notes.md")
            open(keep, "w").close()
            a = review.run(CFG, "day", since, until, engines=[], out_dir=d)
            self.assertFalse(review.delete("../notes.md", d))
            self.assertEqual(review.prune(30, d), [])  # just made
            js = os.path.join(d, a["name"][:-3] + ".json")
            data = json.load(open(js))
            data["made_at"] = "2025-01-01T00:00:00+00:00"
            json.dump(data, open(js, "w"))
            self.assertEqual(review.prune(30, d), [a["name"]])
            self.assertEqual(sorted(os.listdir(d)), ["notes.md"])


def _report(root, project, name, cost=False):
    d = os.path.join(root, project, "docs", "runs")
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, name)
    with open(p, "w") as fh:
        fh.write("# Run report\n\n## Summary\n\n- Start: aaa (2026-01-05 09:00)\n- End: bbb (2026-01-05 09:30)\n")
        if cost:
            fh.write("- Cost: $1.00 — feature agents $0.70, orchestration $0.30 (30%)\n")
    return p


class RunReports(unittest.TestCase):
    def test_folders_of_projects_expand(self):
        with tempfile.TemporaryDirectory() as d:
            a = _report(d, "repo", "r1.md")
            _report(d, "other", "r2.md")
            os.makedirs(os.path.join(d, "no-kit"))
            runs = os.path.dirname(a)
            self.assertEqual(review.rules.report_dirs([d]), [os.path.join(d, "other", "docs", "runs"), runs])
            self.assertEqual(review.rules.report_dirs([os.path.join(d, "repo")]), [runs])
            self.assertEqual(review.rules.report_dirs([runs, runs]), [runs])

    def test_lane_worktrees_count_as_the_project(self):
        same = review.rules.same_project
        self.assertTrue(same("repo", "repo"))
        self.assertTrue(same("repo-M32-002", "repo"))
        self.assertFalse(same("repo-api", "repo"))
        self.assertFalse(same("other", "repo"))

    def test_harness_comes_from_the_logs(self):
        with tempfile.TemporaryDirectory() as d:
            a = _report(d, "repo", "r1.md")
            b = _report(d, "elsewhere", "r2.md")
            c = _report(d, "elsewhere", "r3.md", cost=True)
            who = review.report_harnesses(CFG, [a, b, c])
            self.assertIn("Codex", who[a])
            self.assertEqual(who[b], "")  # no sessions in that project, no dollars
            self.assertEqual(who[c], "Claude Code")  # only the Claude Code edition writes dollars

    def test_run_review_keeps_to_the_report_project(self):
        with tempfile.TemporaryDirectory() as d:
            here = review.build(CFG, "run", datetime(2026, 1, 5, 9, tzinfo=timezone.utc),
                                datetime(2026, 1, 5, 9, 31, tzinfo=timezone.utc), _report(d, "repo", "r1.md"))
            away = review.build(CFG, "run", datetime(2026, 1, 5, 9, tzinfo=timezone.utc),
                                datetime(2026, 1, 5, 9, 31, tzinfo=timezone.utc), _report(d, "elsewhere", "r2.md"))
        self.assertTrue(here["top_sessions"])
        self.assertTrue(all(s["project"] == "repo" for s in here["top_sessions"]))
        self.assertEqual(away["top_sessions"], [])
        self.assertFalse(any(v for v in away["where_tokens_went"].values()))


if __name__ == "__main__":
    unittest.main()
