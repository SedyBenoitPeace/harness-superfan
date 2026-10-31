import json
import os
import tempfile
import threading
import unittest
import unittest.mock
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer

from superfan import rules, serve

PRICES = json.load(open(os.path.join(os.path.dirname(serve.__file__), "prices.json")))
FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
REPORT = """# Run
- Start: abc1234 ({start})
- End: abc1235 ({end})
- Cost by model: x $-1.00
"""


def _report(folder, name, start):
    runs = os.path.join(folder, "proj", "docs", "runs")
    os.makedirs(runs, exist_ok=True)
    with open(os.path.join(runs, name), "w") as fh:
        fh.write(REPORT.format(start=start.strftime("%Y-%m-%d %H:%M"), end=(start + timedelta(hours=1)).strftime("%Y-%m-%d %H:%M")))
    return runs


class ReportWindow(unittest.TestCase):
    def test_only_reports_in_the_window_are_checked(self):
        with tempfile.TemporaryDirectory() as d:
            runs = _report(d, "old.md", datetime(2026, 1, 1, 9, 0))
            _report(d, "new.md", datetime(2026, 1, 20, 9, 0))
            since, until = datetime(2026, 1, 15, tzinfo=timezone.utc), datetime(2026, 1, 22, tzinfo=timezone.utc)
            got = rules.report_vs_logs([runs], since=since, until=until)
            self.assertEqual([f["report"] for f in got], ["new.md"])
            self.assertEqual(len(rules.report_vs_logs([runs])), 2)  # no window: every report

    def test_checks_are_kept_on_disk(self):
        with tempfile.TemporaryDirectory() as d:
            runs = _report(d, "r.md", datetime(2026, 1, 20, 9, 0))
            cache = os.path.join(d, "cache", "checks.json")
            first = rules.report_vs_logs([runs], cache_file=cache)
            self.assertTrue(os.path.exists(cache))
            rules._report_cache.clear()  # as after a restart
            with unittest.mock.patch.object(rules, "check_report", side_effect=AssertionError("re-checked")):
                self.assertEqual(rules.report_vs_logs([runs], cache_file=cache), first)


class Serve(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        cfg = {"cache": os.path.join(self.tmp.name, "checks.json"), "claude": os.path.join(FIX, "claude"), "codex": os.path.join(FIX, "codex"), "copilot": None,
               "reports": [], "tz": timezone.utc, "prices": PRICES, "orchestrator_limit": 0.4, "output_limit": 8000}
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), serve.make_handler({**cfg, "port": 0}))
        port = self.httpd.server_address[1]
        self.httpd.RequestHandlerClass = serve.make_handler({**cfg, "port": port})
        self.base = f"http://127.0.0.1:{port}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.tmp.cleanup()

    def _get(self, path):
        with urllib.request.urlopen(self.base + path) as r:
            return json.loads(r.read())

    def test_run_reports_progress_per_source(self):
        self.assertEqual(self._get("/api/progress"), {"running": False})
        req = urllib.request.Request(self.base + "/api/run", data=json.dumps({"days": 90}).encode(),
                                     headers={"X-Superfan": "1", "Content-Type": "application/json"})
        with urllib.request.urlopen(req) as r:
            last = json.loads(r.read())
        self.assertIn("sessions", last)
        p = self._get("/api/progress")
        self.assertFalse(p["running"])
        self.assertEqual([s["key"] for s in p["steps"]], ["claude", "codex", "reports", "outputs"])
        self.assertTrue(all(s["status"] == "done" for s in p["steps"]))

    def test_progress_while_running(self):
        pr = serve.Progress({"codex": "x"})
        gate = threading.Event()
        t = threading.Thread(target=pr.run, args=("codex", gate.wait))
        t.start()
        snap = pr.snapshot()
        self.assertEqual([s["status"] for s in snap["steps"]], ["running", "waiting", "waiting"])
        gate.set()
        t.join()
        self.assertEqual(pr.snapshot()["steps"][0]["status"], "done")



class BornAfter(unittest.TestCase):
    def test_files_created_after_the_window_are_skipped(self):
        from unittest import mock
        from superfan import parse
        with tempfile.TemporaryDirectory() as d:
            for n in ("a.jsonl", "b.jsonl"):
                open(os.path.join(d, n), "w").close()
            real = os.stat
            t0 = datetime(2026, 1, 10, tzinfo=timezone.utc).timestamp()

            def fake(p, *a, **k):
                st = real(p, *a, **k)
                born = t0 if p.endswith("a.jsonl") else t0 + 86400 * 5
                return mock.Mock(st_mtime=st.st_mtime, st_birthtime=born)
            with mock.patch.object(parse.os, "stat", fake):
                got = parse.files_touched_since(d, None, datetime(2026, 1, 12, tzinfo=timezone.utc))
            self.assertEqual([os.path.basename(p) for p in got], ["a.jsonl"])


if __name__ == "__main__":
    unittest.main()
