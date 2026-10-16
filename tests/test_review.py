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


if __name__ == "__main__":
    unittest.main()
