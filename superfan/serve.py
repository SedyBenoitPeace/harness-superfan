"""A local web page for the Superfan: `python3 -m superfan serve`.

Listens on 127.0.0.1 only and reads log folders as the user running it.
Nothing is written. The only thing that leaves the machine is the optional
"Ask Claude" / "Ask ChatGPT" question, which sends one session's numbers (no
transcript) to the local `claude` or `codex` CLI, only when you press it.
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
from .breakdown import codex_outputs, claude_outputs, summarise
from . import rules, advice

HERE = os.path.dirname(os.path.abspath(__file__))
PAGE = os.path.join(HERE, "web", "index.html")


def run_once(cfg: dict, days: float, until: datetime | None = None) -> tuple[dict, dict]:
    """Returns (result for the page, tool outputs per session for drill-downs)."""
    until = until or datetime.now(timezone.utc)
    since = until - timedelta(days=days)
    started = time.time()
    sessions = scan(cfg["claude"], cfg["codex"], cfg["prices"], since, until)
    sessions.sort(key=lambda r: -r["tokens"]["total"])
    findings = rules.orchestrator_share(sessions, cfg["orchestrator_limit"])
    findings += rules.report_vs_logs(cfg["reports"], cfg["codex"], cfg["claude"], cfg["prices"], cfg["tz"])
    missing = [k for k in ("claude", "codex") if cfg[k] and not os.path.isdir(cfg[k])]
    outs, breakdown = [], {}
    if cfg["codex"] and "codex" not in missing:
        o, secs = codex_outputs(cfg["codex"], since, until)
        outs += o
        breakdown["codex"] = summarise(o, secs)
    if cfg["claude"] and "claude" not in missing:
        o, secs = claude_outputs(cfg["claude"], Prices(cfg["prices"]), since, until)
        outs += o
        breakdown["claude-code"] = summarise(o, secs)
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
                    "reports": len([d for d in cfg["reports"] if os.path.isdir(d)])},
        "missing": missing,
        "thresholds": {"orchestrator": cfg["orchestrator_limit"], "output": cfg["output_limit"]},
        "ask": {"claude": {"model": cfg.get("ask_model") or "", "available": bool(advice.shutil.which("claude"))},
                "codex": {"model": cfg.get("ask_codex_model") or "", "available": bool(advice.shutil.which("codex"))}},
        "sessions": sessions, "findings": findings, "breakdown": breakdown,
    }
    return result, per_session


def make_handler(cfg: dict):
    state = {"last": None, "outputs": {}, "lock": threading.Lock()}
    allowed_hosts = {f"127.0.0.1:{cfg['port']}", f"localhost:{cfg['port']}"}

    def find(sid: str):
        last = state["last"] or {}
        return next((s for s in last.get("sessions", []) if s["session_id"] == sid), None)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):  # keep the terminal quiet
            pass

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

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
            if url.path == "/api/session":
                sid = q.get("id", [""])[0]
                s = find(sid)
                if not s:
                    return self._json(404, {"error": "Session not in the last run. Press Run now."})
                return self._json(200, advice.session_detail(s, state["outputs"].get(sid, []),
                                                             cfg["orchestrator_limit"], cfg["output_limit"]))
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
                with state["lock"]:
                    try:
                        state["last"], state["outputs"] = run_once(cfg, days)
                    except Exception as e:  # show the error on the page instead of a dead request
                        return self._json(500, {"error": repr(e)})
                return self._json(200, state["last"])
            if url.path == "/api/review":
                # The weekly review on demand: a fresh 7-day read, then one model writes the report.
                engine = "codex" if body.get("with") == "codex" else "claude"
                model = cfg.get("ask_codex_model" if engine == "codex" else "ask_model") or ""
                with state["lock"]:
                    try:
                        state["last"], state["outputs"] = run_once(cfg, 7)
                    except Exception as e:
                        return self._json(500, {"error": repr(e)})
                prompt = advice.overview_prompt(state["last"], state["outputs"], "all", advice.REVIEW_QUESTION,
                                                cfg["orchestrator_limit"], cfg["output_limit"])
                try:
                    text = advice.ask(prompt, model, timeout=400, engine=engine)
                except Exception as e:
                    return self._json(502, {"error": str(e), "data": state["last"]})
                state["last"]["review"] = {"with": engine, "at": datetime.now(timezone.utc).isoformat(), "text": text}
                return self._json(200, state["last"])
            if url.path == "/api/ask":
                sid = str(body.get("session") or "")
                question = str(body.get("question") or "What can I improve in this session?")[:1000]
                engine = "codex" if body.get("with") == "codex" else "claude"
                model = cfg.get("ask_codex_model" if engine == "codex" else "ask_model") or ""
                if not sid:  # the whole window
                    if not state["last"]:
                        return self._json(404, {"error": "Nothing read yet. Press Run now."})
                    h = body.get("harness") if body.get("harness") in ("claude-code", "codex") else "all"
                    prompt = advice.overview_prompt(state["last"], state["outputs"], h, question,
                                                    cfg["orchestrator_limit"], cfg["output_limit"])
                    try:
                        return self._json(200, {"answer": advice.ask(prompt, model, engine=engine)})
                    except Exception as e:
                        return self._json(502, {"error": str(e)})
                s = find(sid)
                if not s:
                    return self._json(404, {"error": "Session not in the last run. Press Run now."})
                detail = advice.session_detail(s, state["outputs"].get(sid, []),
                                               cfg["orchestrator_limit"], cfg["output_limit"])
                try:
                    answer = advice.ask(advice.ask_prompt(s, detail, question), model, engine=engine)
                except Exception as e:
                    return self._json(502, {"error": str(e)})
                return self._json(200, {"answer": answer})
            self._send(404, b"not found", "text/plain")

    return Handler


def serve(cfg: dict) -> None:
    httpd = ThreadingHTTPServer(("127.0.0.1", cfg["port"]), make_handler(cfg))
    print(f"Harness Superfan on http://127.0.0.1:{cfg['port']}  (Ctrl+C to stop)")
    for k in ("claude", "codex"):
        print(f"  {k:<7} {cfg[k] or '(off)'}")
    for d in cfg["reports"]:
        print(f"  reports {d}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
