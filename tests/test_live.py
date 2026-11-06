import json
import os
import shutil
import tempfile
import threading
import time
import unittest
import urllib.request
from http.server import ThreadingHTTPServer

from superfan import live, serve
from superfan.parse import Prices, scan_claude

PRICES = json.load(open(os.path.join(os.path.dirname(serve.__file__), "prices.json")))
FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


class Classify(unittest.TestCase):
    def test_cli_processes(self):
        cases = [
            (["/Users/u/.local/bin/claude"], "", False, "claude-code"),
            (["claude", "--resume"], "", False, "claude-code"),
            (["node", "/opt/homebrew/bin/claude"], "", False, "claude-code"),
            (["/Users/u/Library/Application Support/Claude/claude-code/2.1.0/claude"], "", False, "claude-code"),
            (["/Applications/Claude.app/Contents/MacOS/Claude"], "", False, None),  # the desktop app itself
            (["/Applications/Claude.app/Contents/Frameworks/Claude Helper.app/x", "--type=renderer"], "", False, None),
            (["codex"], "/usr/local/bin/codex", False, "codex"),
            (["codex", "app-server"], "", False, "codex"),
            (["node", "/usr/local/lib/node_modules/@openai/codex/bin/codex.js"], "", False, "codex"),
            (["node", "/usr/local/bin/copilot"], "", False, "copilot"),
            (["vim", "claude.md"], "", False, None),
            (["/Applications/Visual Studio Code.app/Contents/Frameworks/Code Helper (Plugin).app/x", "/Users/u/.vscode/extensions/github.copilot-chat/node_modules/@github/copilot-language-server/dist/language-server.js", "--stdio"], "", False, None),
            (["node", "/usr/local/lib/node_modules/@github/copilot/index.js"], "", False, "copilot"),
            (["grep", "codex"], "", False, None),
            (["python3", "-m", "superfan", "serve"], "", False, None),
        ]
        for argv, exe, win, want in cases:
            self.assertEqual(live.classify(argv, exe, windows=win), want, argv)

    def test_windows_command_lines(self):
        cases = [
            (r'"C:\Users\u\.local\bin\claude.exe"', r"C:\Users\u\.local\bin\claude.exe", "claude-code"),
            (r'"C:\Program Files\nodejs\node.exe" "C:\Users\u\AppData\Roaming\npm\node_modules\@anthropic-ai\claude-code\cli.js"',
             r"C:\Program Files\nodejs\node.exe", "claude-code"),
            (r'"C:\Users\u\AppData\Local\AnthropicClaude\app-1.0.0\claude.exe"', r"C:\Users\u\AppData\Local\AnthropicClaude\app-1.0.0\claude.exe", None),
            (r'C:\Users\u\AppData\Roaming\npm\node_modules\@openai\codex\vendor\codex.exe', "", "codex"),
            (r'"C:\Program Files\nodejs\node.exe" C:\Users\u\AppData\Roaming\npm\node_modules\@github\copilot\index.js', "", "copilot"),
            (r'notepad.exe C:\notes\codex.txt', r"C:\Windows\notepad.exe", None),
        ]
        for cmd, exe, want in cases:
            self.assertEqual(live.classify(live.split_command(cmd), exe, windows=True), want, cmd)

    def test_parse_windows_output_and_wrappers(self):
        rows = [
            {"ProcessId": 10, "ParentProcessId": 1, "ExecutablePath": r"C:\nodejs\node.exe",
             "CommandLine": r'"C:\nodejs\node.exe" C:\npm\node_modules\@openai\codex\bin\codex.js'},
            {"ProcessId": 11, "ParentProcessId": 10, "ExecutablePath": r"C:\npm\node_modules\@openai\codex\vendor\codex.exe",
             "CommandLine": r"C:\npm\node_modules\@openai\codex\vendor\codex.exe"},
            {"ProcessId": 20, "ParentProcessId": 1, "ExecutablePath": r"C:\Users\u\.local\bin\claude.exe", "CommandLine": "claude"},
            {"ProcessId": 30, "ParentProcessId": 1, "ExecutablePath": r"C:\Windows\explorer.exe", "CommandLine": "explorer.exe"},
        ]
        procs = live._dedupe(live.parse_win(json.dumps(rows)))
        self.assertEqual(sorted((p["harness"], p["pid"]) for p in procs), [("claude-code", 20), ("codex", 10)])
        self.assertTrue(all(p["cwd"] is None for p in procs))  # Windows gives no working folder
        self.assertEqual(live.parse_win(json.dumps(rows[2]))[0]["pid"], 20)  # one process: an object, not a list

    def test_parse_mac_output(self):
        comm = ("  101     1 Wed Nov  5 10:00:00 2026 /Users/u/Library/Application Support/Claude/claude-code/2.1.0/claude\n"
                "  202     1 Wed Nov  5 09:00:00 2026 /bin/zsh\n  303     1 Tue Nov  4 08:00:00 2026 /Applications/Claude.app/Contents/MacOS/Claude\n")
        args = "  101 /Users/u/Library/Application Support/Claude/claude-code/2.1.0/claude --resume\n  202 -zsh\n  303 /Applications/Claude.app/Contents/MacOS/Claude\n"
        parents = {}
        procs = live.parse_ps(comm, args, parents)
        self.assertEqual([(p["harness"], p["pid"]) for p in procs], [("claude-code", 101)])
        self.assertEqual(procs[0]["start"], live.datetime(2026, 11, 5, 10, 0, 0).timestamp())
        self.assertEqual(parents, {101: 1, 202: 1, 303: 1})
        out = "p101\nfcwd\nn/Users/u/Source/app\nf12\nn/Users/u/.codex/sessions/2026/11/05/rollout-x.jsonl\nf13\nn/dev/null\n"
        self.assertEqual(live.parse_lsof(out), ({101: "/Users/u/Source/app"}, {101: ["/Users/u/.codex/sessions/2026/11/05/rollout-x.jsonl"]}))

    def test_helpers_and_grandchildren_count_once(self):
        procs = [{"harness": "codex", "pid": 10, "ppid": 1}, {"harness": "codex", "pid": 30, "ppid": 20},
                 {"harness": "codex", "pid": 40, "ppid": 1}]
        parents = {10: 1, 20: 10, 30: 20, 40: 1}  # 30 is a grandchild of 10, through a shell (20)
        self.assertEqual([p["pid"] for p in live._dedupe(procs, parents)], [10, 40])


def _s(h, sid, cwd, age, ended=False, now=1_000_000.0):
    return (h, sid), {"harness": h, "session_id": sid, "cwd": cwd, "slug": live.claude_slug(cwd), "files": [],
                      "mtime": now - age, "ended": ended, "project": os.path.basename(cwd)}


class States(unittest.TestCase):
    NOW = 1_000_000.0

    def sessions(self, *rows):
        return dict(_s(*r, now=self.NOW) for r in rows)

    def test_by_process_working_folder(self):
        ss = self.sessions(("claude-code", "a", "/w/app", 30), ("claude-code", "b", "/w/app", 900),
                           ("claude-code", "c", "/w/lib", 900), ("codex", "d", "/w/app", 3000))
        procs = [{"harness": "claude-code", "pid": 1, "cwd": "/w/app"}, {"harness": "codex", "pid": 2, "cwd": "/w/app"}]
        live.assign_states(ss, procs, self.NOW)
        got = {k[1]: v["state"] for k, v in ss.items()}
        # a: just written; b: same folder but the one process there is a's; c: no process in /w/lib; d: idle for 50 min but its CLI is open
        self.assertEqual(got, {"a": "working", "b": "ended", "c": "ended", "d": "waiting"})

    def test_two_processes_in_one_folder(self):
        ss = self.sessions(("claude-code", "a", "/w/app", 600), ("claude-code", "b", "/w/app", 900), ("claude-code", "c", "/w/app", 1200))
        procs = [{"harness": "claude-code", "pid": i, "cwd": "/w/app"} for i in (1, 2)]
        live.assign_states(ss, procs, self.NOW)
        self.assertEqual({k[1]: v["state"] for k, v in ss.items()}, {"a": "waiting", "b": "waiting", "c": "ended"})

    def test_without_working_folder_newest_sessions_win(self):  # Windows
        ss = self.sessions(("codex", "a", "/w/x", 500), ("codex", "b", "/w/y", 400), ("codex", "c", "/w/z", 200))
        live.assign_states(ss, [{"harness": "codex", "pid": 1, "cwd": None, "no_cwd": True}], self.NOW)
        self.assertEqual({k[1]: (v["state"], v["by"]) for k, v in ss.items() if v["state"] != "ended"}, {"c": ("waiting", "process count")})

    def test_without_process_information_file_time_decides(self):
        ss = self.sessions(("codex", "a", "/w/x", 60), ("codex", "b", "/w/y", 600), ("codex", "c", "/w/z", 4000),
                           ("copilot", "d", "/w/x", 10, True))
        live.assign_states(ss, None, self.NOW)
        self.assertEqual({k[1]: v["state"] for k, v in ss.items()}, {"a": "working", "b": "idle", "c": "ended", "d": "ended"})

    def test_a_process_cannot_own_a_log_last_written_before_it_started(self):
        # The reported case: two sessions working now, older ones in the same folder last
        # written 17-19 hours ago, and CLI processes in that folder that started this morning.
        ss = self.sessions(("codex", "now1", "/w/app", 4), ("codex", "now2", "/w/app", 11),
                           ("codex", "old1", "/w/app", 16.9 * 3600), ("codex", "old2", "/w/app", 18 * 3600),
                           ("codex", "old3", "/w/app", 18.9 * 3600))
        morning = self.NOW - 3 * 3600
        procs = [{"harness": "codex", "pid": i, "cwd": "/w/app", "start": morning + i} for i in range(1, 6)]
        live.assign_states(ss, procs, self.NOW)
        self.assertEqual({k[1]: v["state"] for k, v in ss.items()},
                         {"now1": "working", "now2": "working", "old1": "ended", "old2": "ended", "old3": "ended"})
        # a process started before the old session was last written can still own it (a long-lived app)
        procs = [{"harness": "codex", "pid": 9, "cwd": "/w/app", "start": self.NOW - 30 * 3600}]
        ss = self.sessions(("codex", "old1", "/w/app", 16.9 * 3600))
        live.assign_states(ss, procs, self.NOW)
        self.assertEqual(ss[("codex", "old1")]["state"], "waiting")

    def test_open_log_file_is_exact(self):
        ss = self.sessions(("copilot", "x", "/w/p", 18 * 3600), ("copilot", "y", "/w/p", 600))
        ss[("copilot", "x")]["files"] = ["/logs/x/events.jsonl"]
        ss[("copilot", "y")]["files"] = ["/logs/y/events.jsonl"]
        # one process, started long ago, holding x's log: x is open, y is not (even though y is newer)
        procs = [{"harness": "copilot", "pid": 1, "cwd": "/w/p", "start": self.NOW - 40 * 3600, "files": ["/logs/x/events.jsonl"]}]
        live.assign_states(ss, procs, self.NOW)
        self.assertEqual({k[1]: (v["state"], v["by"]) for k, v in ss.items()},
                         {"x": ("waiting", "log file open"), "y": ("ended", "no process")})

    def test_no_folder_means_no_guess_where_folders_exist(self):
        # The reported case on macOS: Codex app servers with cwd "/" and no open log were
        # each paired with an old physio session by count.
        ss = self.sessions(("codex", "a", "/w/physio", 3.4 * 3600), ("codex", "b", "/w/physio", 20.6 * 3600))
        early = self.NOW - 48 * 3600
        procs = [{"harness": "codex", "pid": 1, "cwd": "/", "start": early, "files": []},
                 {"harness": "codex", "pid": 2, "cwd": None, "start": early, "files": []}]
        live.assign_states(ss, procs, self.NOW)
        self.assertEqual({k[1]: v["state"] for k, v in ss.items()}, {"a": "ended", "b": "ended"})
        # Windows never gives folders, so there the count still decides
        for p in procs:
            p["no_cwd"] = True
        live.assign_states(ss, procs, self.NOW)
        self.assertEqual({k[1]: v["state"] for k, v in ss.items()}, {"a": "waiting", "b": "waiting"})

    def test_superfan_ask_folders_are_not_sessions(self):
        ss = self.sessions(("claude-code", "a", "/w/app", 900))
        live.assign_states(ss, [{"harness": "claude-code", "pid": 1, "cwd": "/tmp/superfan-ask-x1"}], self.NOW)
        self.assertEqual(ss[("claude-code", "a")]["state"], "ended")


class Watch(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        for k in ("claude", "codex", "copilot"):
            shutil.copytree(os.path.join(FIX, k), os.path.join(self.tmp, k))
            for dp, _, names in os.walk(os.path.join(self.tmp, k)):
                for n in names:
                    os.utime(os.path.join(dp, n))  # written just now
        self.cfg = {k: os.path.join(self.tmp, k) for k in ("claude", "codex", "copilot")}
        self.cfg.update(prices=PRICES, orchestrator_limit=0.4, process_check=False)

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_index_and_states(self):
        ov = live.Live(self.cfg).overview()
        got = sorted((s["harness"], s["state"]) for s in ov["sessions"])
        # Copilot fixtures end with session.shutdown, so they are not listed
        self.assertEqual(got, [("claude-code", "working"), ("codex", "working")])
        self.assertEqual(ov["process_check"], "off")
        claude = next(s for s in ov["sessions"] if s["harness"] == "claude-code")
        self.assertEqual((claude["session_id"], claude["project"]), ("s1", "repo"))

    def test_view_matches_scan_and_follows_new_lines(self):
        lv = live.Live(self.cfg)
        lv.overview()
        w = lv.watcher("claude-code", "s1")
        v = w.snapshot()
        full = scan_claude(self.cfg["claude"], Prices(PRICES))["s1"].summary()
        self.assertEqual(v["summary"]["tokens"], full["tokens"])
        self.assertAlmostEqual(v["summary"]["cost_usd"], full["cost_usd"])
        self.assertTrue(v["recent"])
        self.assertTrue(all(set(r) == {"ts", "role", "tool", "target"} for r in v["recent"]))
        before = v["summary"]["tokens"]["total"]
        now = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())
        line = {"type": "assistant", "sessionId": "s1", "cwd": "/repo", "timestamp": now, "requestId": "r-live",
                "message": {"id": "m-live", "model": "claude-opus-5-5",
                            "content": [{"type": "tool_use", "id": "t-live", "name": "Read", "input": {"file_path": "/repo/src/deep/app.py"}}],
                            "usage": {"input_tokens": 100, "cache_read_input_tokens": 900, "cache_creation_input_tokens": 0, "output_tokens": 50}}}
        with open(os.path.join(self.cfg["claude"], "-repo", "s1.jsonl"), "a") as fh:
            fh.write(json.dumps(line) + "\n")
        lv.index.built = 0
        w.next_parse = 0
        v2 = w.snapshot()
        self.assertEqual(v2["summary"]["tokens"]["total"], before + 1050)
        self.assertEqual(v2["burn"]["last5_tokens"], 1050)
        self.assertEqual(v2["context"]["last"], 1000)
        self.assertEqual(v2["recent"][-1], {"ts": now, "role": "orchestrator", "tool": "Read", "target": "deep/app.py"})
        self.assertEqual(v2["state"]["state"], "working")
        self.assertIn("categories", v2["detail"])
        self.assertTrue(v2["detail"]["advice"])

    def test_burn_moves_on_without_new_lines(self):
        lv = live.Live(self.cfg)
        w = lv.watcher("claude-code", "s1")
        v = w.snapshot()
        w._agents[0].timeline.append((live.datetime.now(live.timezone.utc), 777, 0.01))  # as if read earlier
        w.burn_at = 0
        version = w.version
        v = w.snapshot()
        self.assertEqual(v["burn"]["last5_tokens"], 777)
        self.assertGreater(w.version, version)

    def test_tool_calls_survive_huge_lines(self):
        # Copilot can write one multi-megabyte line after the tool calls; they must still show.
        idx = live.Live(self.cfg).index.refresh(force=True)
        path = next(f for (h, _), s in idx.items() if h == "copilot" for f in s["files"])
        feed = live.ToolFeed("copilot")
        first = feed.update([path], {})
        self.assertTrue(first)
        with open(path, "a") as fh:
            fh.write(json.dumps({"type": "session.usage_checkpoint", "timestamp": "2026-01-05T09:59:00.000Z",
                                 "data": {"blob": "x" * 2_000_000}}) + "\n")
            fh.write('{"type": "tool.execution_start", "timestamp": "2026-01-05T09:59:01.000Z", "data": {"toolName": "view", "arguments": {"path": "/a/b/c.py"}}}')  # still being written
        self.assertEqual(live.ToolFeed("copilot").update([path], {})[-1], first[-1])
        with open(path, "a") as fh:
            fh.write("\n")
        got = feed.update([path], {})
        self.assertEqual(got[-1], {"ts": "2026-01-05T09:59:01.000Z", "role": "orchestrator", "tool": "view", "target": "b/c.py"})

    def test_codex_and_copilot_views(self):
        lv = live.Live(self.cfg)
        idx, _ = lv.sessions()
        for h in ("codex", "copilot"):
            for (hh, sid) in idx:
                if hh == h:
                    v = lv.watcher(h, sid).snapshot()
                    self.assertGreater(v["summary"]["tokens"]["total"], 0)
                    self.assertIsNone(v["summary"]["cost_usd"])  # tokens only
                    self.assertIsNone(v["burn"]["cost_usd"])


class Endpoints(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        shutil.copytree(os.path.join(FIX, "claude"), os.path.join(self.tmp, "claude"))
        for dp, _, names in os.walk(self.tmp):
            for n in names:
                os.utime(os.path.join(dp, n))
        cfg = {"cache": os.path.join(self.tmp, "c.json"), "claude": os.path.join(self.tmp, "claude"), "codex": None, "copilot": None,
               "reports": [], "tz": None, "prices": PRICES, "orchestrator_limit": 0.4, "output_limit": 8000, "process_check": False}
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), serve.make_handler({**cfg, "port": 0}))
        port = self.httpd.server_address[1]
        self.httpd.RequestHandlerClass = serve.make_handler({**cfg, "port": port})
        self.httpd.daemon_threads = True
        self.base = f"http://127.0.0.1:{port}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        shutil.rmtree(self.tmp)

    def get(self, path):
        return urllib.request.urlopen(self.base + path, timeout=5)

    def test_live_list_page_and_stream(self):
        j = json.load(self.get("/api/live"))
        self.assertEqual([s["session_id"] for s in j["sessions"]], ["s1"])
        self.assertIn(b"EventSource", self.get("/live").read())
        r = self.get("/api/live/stream?h=claude-code&id=s1")
        self.assertEqual(r.headers["Content-Type"], "text/event-stream")
        self.assertEqual(r.readline(), b"event: view\n")
        data = json.loads(r.readline()[len(b"data: "):])
        self.assertEqual(data["summary"]["session_id"], "s1")
        r.close()

    def test_bad_requests(self):
        with self.assertRaises(urllib.error.HTTPError) as e:
            self.get("/api/live/session?h=nope&id=x")
        self.assertEqual(e.exception.code, 400)
        with self.assertRaises(urllib.error.HTTPError) as e:
            self.get("/api/live/session?h=codex&id=missing")
        self.assertEqual(e.exception.code, 404)


if __name__ == "__main__":
    unittest.main()
