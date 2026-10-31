"""A local web page for the Superfan: `python3 -m superfan serve`.

Listens on 127.0.0.1 only and reads log folders as the user running it.
The only files it writes or deletes are reviews, in the reviews folder
(~/.superfan/reviews by default, outside any repository), and a cache of
run-report checks (~/.superfan/report-checks.json: numbers and file names only). The only thing that
leaves the machine is the optional "Ask" or "Run review" request, which sends
numbers and command names (no transcript) to the local `claude` or `codex` CLI,
only when you press it.
"""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .parse import scan, Prices
from .breakdown import codex_outputs, claude_outputs, copilot_outputs, summarise
from . import rules, advice, review

def default_cache() -> str:
    """Report checks already done, so a restart doesn't redo them. Outside any repository."""
    return os.path.expanduser(os.environ.get("SUPERFAN_CACHE") or "~/.superfan/report-checks.json")


HERE = os.path.dirname(os.path.abspath(__file__))
PAGE = os.path.join(HERE, "web", "index.html")


class Progress:
    """What a run is doing right now, for the page to show while it waits."""

    STEPS = [("claude", "Claude Code logs"), ("codex", "Codex logs"), ("copilot", "Copilot logs"),
             ("reports", "Run reports"), ("outputs", "Tool outputs")]

    def __init__(self, cfg: dict | None = None):
        cfg = cfg or {}
        self.lock = threading.Lock()
        self.started = time.time()
        self.steps = [{"key": k, "label": label, "status": "waiting", "seconds": None, "note": ""}
                      for k, label in self.STEPS
                      if k in ("reports", "outputs") or cfg.get(k)]

    def run(self, key: str, fn, note=None):
        step = next((s for s in self.steps if s["key"] == key), None)
        if step is None:
            return fn()
        with self.lock:
            step["status"], step["since"] = "running", time.time()
        try:
            out = fn()
        except Exception:
            with self.lock:
                step["status"] = "failed"
            raise
        with self.lock:
            step["status"], step["seconds"] = "done", round(time.time() - step.pop("since"), 1)
            step["note"] = note(out) if note else ""
        return out

    def snapshot(self) -> dict:
        with self.lock:
            now = time.time()
            steps = [{k: v for k, v in s.items() if k != "since"} | (
                {"seconds": round(now - s["since"], 1)} if s["status"] == "running" else {}) for s in self.steps]
        return {"elapsed": round(time.time() - self.started, 1), "steps": steps}


def _n(word: str):
    return lambda xs: f"{len(xs)} {word}{'' if len(xs) == 1 else 's'}"


def run_once(cfg: dict, days: float, until: datetime | None = None,
             progress: Progress | None = None) -> tuple[dict, dict]:
    """Returns (result for the page, tool outputs per session for drill-downs)."""
    progress = progress or Progress(cfg)
    until = until or datetime.now(timezone.utc)
    since = until - timedelta(days=days)
    started = time.time()
    missing = [k for k in ("claude", "codex", "copilot") if cfg.get(k) and not os.path.isdir(cfg[k])]
    roots = {k: cfg.get(k) if cfg.get(k) and k not in missing else None for k in ("claude", "codex", "copilot")}
    sessions = []
    for k in ("claude", "codex", "copilot"):
        if roots[k]:
            sessions += progress.run(k, lambda k=k: scan(roots["claude"] if k == "claude" else None,
                                                         roots["codex"] if k == "codex" else None, cfg["prices"],
                                                         since, until, roots["copilot"] if k == "copilot" else None),
                                     _n("session"))
    sessions.sort(key=lambda r: -r["tokens"]["total"])
    findings = rules.orchestrator_share(sessions, cfg["orchestrator_limit"])
    findings += rules.frequent_compaction(sessions) + rules.tool_definitions(sessions)
    findings += progress.run("reports", lambda: rules.report_vs_logs(
        cfg["reports"], roots["codex"], roots["claude"], cfg["prices"], cfg["tz"], copilot_root=roots["copilot"],
        since=since, until=until, cache_file=cfg.get("cache") or default_cache()), _n("finding"))

    def tool_outputs():
        outs, breakdown = [], {}
        if roots["codex"]:
            o, secs = codex_outputs(roots["codex"], since, until)
            outs += o
            breakdown["codex"] = summarise(o, secs)
        if roots["claude"]:
            o, secs = claude_outputs(roots["claude"], Prices(cfg["prices"]), since, until)
            outs += o
            breakdown["claude-code"] = summarise(o, secs)
        if roots["copilot"]:
            o, secs = copilot_outputs(roots["copilot"], since, until)
            outs += o
            breakdown["copilot"] = summarise(o, secs)
        return outs, breakdown
    outs, breakdown = progress.run("outputs", tool_outputs, lambda r: _n("tool call")(r[0]))
    for b in breakdown.values():
        b.pop("by_session", None)
    findings += rules.large_outputs([(o["session"], o["agent"], o["label"], o["tokens"], o["calls_after"]) for o in outs],
                                    cfg["output_limit"])
    per_session: dict = {}
    for o in outs:
        per_session.setdefault(o["session"], []).append(o)
    result = {
        "since": since.isoformat(), "until": until.isoformat(), "ran_at": datetime.now(timezone.utc).isoformat(),
        "seconds": round(time.time() - started, 1), "days": days,
        "sources": {"claude": bool(cfg["claude"]) and "claude" not in missing,
                    "codex": bool(cfg["codex"]) and "codex" not in missing,
                    "copilot": bool(cfg.get("copilot")) and "copilot" not in missing,
                    "reports": len([d for d in cfg["reports"] if os.path.isdir(d)])},
        "missing": missing,
        "thresholds": {"orchestrator": cfg["orchestrator_limit"], "output": cfg["output_limit"]},
        "ask": {"claude": {"model": cfg.get("ask_model") or "", "available": bool(advice.shutil.which("claude"))},
                "codex": {"model": cfg.get("ask_codex_model") or "", "available": bool(advice.shutil.which("codex"))}},
        "sessions": sessions, "findings": findings, "breakdown": breakdown,
    }
    return result, per_session


def make_handler(cfg: dict):
    state = {"last": None, "outputs": {}, "lock": threading.Lock(), "job": None, "jobs": threading.Lock()}
    allowed_hosts = {f"127.0.0.1:{cfg['port']}", f"localhost:{cfg['port']}"}

    def find(sid: str):
        last = state["last"] or {}
        return next((s for s in last.get("sessions", []) if s["session_id"] == sid), None)

    def start_run(days: float, until: datetime | None, day: str | None) -> dict:
        """Starts a run in the background, or joins the one already running for the same window."""
        with state["jobs"]:
            job = state["job"]
            if job and not job["done"].is_set() and job["key"] == (days, day):
                return job
            job = {"key": (days, day), "progress": Progress(cfg), "done": threading.Event(), "error": None}
            state["job"] = job

        def work():
            with state["lock"]:  # one run at a time; a newer request waits for the older one
                job["progress"].started = time.time()
                try:
                    last, outputs = run_once(cfg, days, until, job["progress"])
                    last["day"] = day
                    state["last"], state["outputs"] = last, outputs
                except Exception as e:  # show the error on the page instead of a dead request
                    job["error"] = repr(e)
                finally:
                    job["done"].set()
        threading.Thread(target=work, daemon=True).start()
        return job

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):  # keep the terminal quiet
            pass

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass  # the browser left (reload, closed tab) before the answer was ready

        def _json(self, code: int, obj) -> None:
            self._send(code, json.dumps(obj, default=str).encode(), "application/json")

        def _host_ok(self) -> bool:
            if self.headers.get("Host") not in allowed_hosts:  # refuse DNS-rebinding requests
                self._send(403, b"forbidden", "text/plain")
                return False
            return True

        def do_GET(self):
            if not self._host_ok():
                return
            url = urlparse(self.path)
            q = parse_qs(url.query)
            if url.path == "/":
                with open(PAGE, "rb") as fh:
                    return self._send(200, fh.read(), "text/html; charset=utf-8")
            if url.path == "/api/last":
                return self._json(200, state["last"] or {})
            if url.path == "/api/progress":
                job = state["job"]
                if not job:
                    return self._json(200, {"running": False})
                return self._json(200, {"running": not job["done"].is_set(), "error": job["error"],
                                        **job["progress"].snapshot()})
            if url.path == "/api/session":
                sid = q.get("id", [""])[0]
                s = find(sid)
                if not s:
                    return self._json(404, {"error": "Session not in the last run. Press Refresh."})
                return self._json(200, advice.session_detail(s, state["outputs"].get(sid, []),
                                                             cfg["orchestrator_limit"], cfg["output_limit"]))
            if url.path == "/api/reviews":
                reports = review.latest_reports(cfg["reports"])
                who = review.report_harnesses(cfg, [p for _, p in reports])
                return self._json(200, {
                    "dir": cfg.get("reviews") or review.default_dir(),
                    "reviews": review.list_reviews(cfg.get("reviews")),
                    "reports": [{"id": rid, "name": os.path.basename(p), "project": rules.report_project(p),
                                 "harness": who.get(p, "")} for rid, p in reports],
                    "today": datetime.now(cfg["tz"]).date().isoformat(), "keep_days": cfg.get("keep_days") or 0})
            if url.path == "/api/review":
                text = review.load(q.get("name", [""])[0], cfg.get("reviews"))
                if text is None:
                    return self._json(404, {"error": "No such review."})
                return self._json(200, {"name": q["name"][0], "markdown": text})
            self._send(404, b"not found", "text/plain")

        def do_POST(self):
            # Anything that does work is POST with a custom header, so another
            # website can't trigger it from your browser (no CORS preflight answer).
            if not self._host_ok():
                return
            if self.headers.get("X-Superfan") != "1":
                return self._send(403, b"forbidden", "text/plain")
            try:
                body = json.loads(self.rfile.read(min(int(self.headers.get("Content-Length") or 0), 65536)) or b"{}")
            except ValueError:
                body = {}
            url = urlparse(self.path)
            if url.path == "/api/run":
                try:
                    days = min(max(float(body.get("days", 7)), 0.1), 90)
                except (TypeError, ValueError):
                    days = 7.0
                day, until = body.get("day"), None
                if day:  # one calendar day in local time
                    try:
                        _, until = review.day_window(datetime.strptime(str(day), "%Y-%m-%d").date(), cfg["tz"])
                    except ValueError:
                        return self._json(400, {"error": "Dates must look like 2026-10-16."})
                    days = 1.0
                job = start_run(days, until, str(day) if day else None)
                job["done"].wait()
                if job["error"]:
                    return self._json(500, {"error": job["error"]})
                return self._json(200, state["last"])
            if url.path == "/api/review":
                # The same review as `superfan review`: numbers computed here, summary by a model, saved to disk.
                engines = {"claude": ["claude"], "codex": ["codex"], "both": ["claude", "codex"], "none": []}.get(
                    body.get("with"), ["claude"])
                scope, tz, report, compare = body.get("scope"), cfg["tz"], None, 0
                try:
                    if scope == "run":
                        report = dict(review.latest_reports(cfg["reports"], 200)).get(str(body.get("report") or ""))
                        w = report and review.report_window(report, tz)
                        if not w:
                            return self._json(400, {"error": "Pick a run report with Start and End lines."})
                        since, until = w
                    elif scope == "window":
                        since, until = (datetime.fromisoformat(str(body[k])) for k in ("since", "until"))
                        since, until = (d if d.tzinfo else d.replace(tzinfo=tz) for d in (since, until))
                        if not since < until:
                            return self._json(400, {"error": "The window's start must be before its end."})
                    else:
                        scope, compare = "day", 6
                        d = datetime.strptime(str(body.get("day") or datetime.now(tz).date()), "%Y-%m-%d").date()
                        since, until = review.day_window(d, tz)
                except (KeyError, ValueError):
                    return self._json(400, {"error": "Dates must look like 2026-10-16 or 2026-10-16T09:00."})
                try:
                    r = review.run(cfg, scope, since, until, report, engines, cfg.get("reviews"), compare)
                except Exception as e:
                    return self._json(500, {"error": repr(e)})
                return self._json(200, {"name": r["name"], "markdown": r["markdown"], "errors": r["errors"]})
            if url.path == "/api/reviews/delete":
                # Only ever touches the reviews folder, and only files named like a review.
                if body.get("name"):
                    ok = review.delete(str(body["name"]), cfg.get("reviews"))
                    return self._json(200 if ok else 404, {"deleted": [body["name"]] if ok else [], **({} if ok else {"error": "No such review."})})
                try:
                    days = float(body.get("older_than_days"))
                except (TypeError, ValueError):
                    return self._json(400, {"error": "Say how many days to keep."})
                if days < 1:
                    return self._json(400, {"error": "Keep at least 1 day."})
                return self._json(200, {"deleted": review.prune(days, cfg.get("reviews"))})
            if url.path == "/api/ask":
                sid = str(body.get("session") or "")
                question = str(body.get("question") or "What can I improve in this session?")[:1000]
                engine = "codex" if body.get("with") == "codex" else "claude"
                model = cfg.get("ask_codex_model" if engine == "codex" else "ask_model") or ""
                if not sid:  # the whole window
                    if not state["last"]:
                        return self._json(404, {"error": "Nothing read yet. Press Run now."})
                    h = body.get("harness") if body.get("harness") in ("claude-code", "codex", "copilot") else "all"
                    prompt = advice.overview_prompt(state["last"], state["outputs"], h, question,
                                                    cfg["orchestrator_limit"], cfg["output_limit"])
                    try:
                        return self._json(200, {"answer": advice.ask(prompt, model, engine=engine)})
                    except Exception as e:
                        return self._json(502, {"error": str(e)})
                s = find(sid)
                if not s:
                    return self._json(404, {"error": "Session not in the last run. Press Refresh."})
                detail = advice.session_detail(s, state["outputs"].get(sid, []),
                                               cfg["orchestrator_limit"], cfg["output_limit"])
                try:
                    answer = advice.ask(advice.ask_prompt(s, detail, question), model, engine=engine)
                except Exception as e:
                    return self._json(502, {"error": str(e)})
                return self._json(200, {"answer": answer})
            self._send(404, b"not found", "text/plain")

    Handler.start_run = staticmethod(start_run)
    return Handler


def serve(cfg: dict) -> None:
    handler = make_handler(cfg)
    httpd = ThreadingHTTPServer(("127.0.0.1", cfg["port"]), handler)
    httpd.daemon_threads = True
    handler.start_run(7.0, None, None)  # start reading now, so the page has something sooner
    print(f"Harness Superfan on http://127.0.0.1:{cfg['port']}  (Ctrl+C to stop)")
    for k in ("claude", "codex", "copilot"):
        print(f"  {k:<7} {cfg.get(k) or '(off)'}")
    for d in cfg["reports"]:
        print(f"  reports {d}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
