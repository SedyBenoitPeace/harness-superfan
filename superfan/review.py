"""`superfan review`: one review of a run, a day or a custom window.

Python computes every number (sessions, tokens, Claude Code dollars, rule
findings, the biggest problems and how the day compares with the days before).
A model only writes the summary, from those findings: `claude -p` with tools
off, and optionally `codex exec` as a second opinion. It never sees transcripts.

Each review is saved as <out_dir>/<YYYY-MM-DD-HHMM>-<scope>.md plus a .json of
the findings. out_dir defaults to $SUPERFAN_REVIEWS or ~/.superfan/reviews and
should stay outside any repository: reviews describe your own sessions.
"""

from __future__ import annotations

import json
import os
import re
from datetime import date, datetime, time, timedelta, timezone

from .parse import scan, Prices, parse_ts
from .breakdown import codex_outputs, claude_outputs, summarise
from . import rules, advice

ENGINE_NAME = {"claude": "Claude", "codex": "ChatGPT"}
NAME_RE = re.compile(r"^\d{4}-\d\d-\d\d-\d{4,6}-[a-z0-9-]+\.md$")


def default_dir() -> str:
    return os.path.expanduser(os.environ.get("SUPERFAN_REVIEWS") or "~/.superfan/reviews")


def _t(n) -> str:
    return advice._t(n or 0)


def day_window(d: date, tz) -> tuple[datetime, datetime]:
    start = datetime.combine(d, time.min).replace(tzinfo=tz)
    return start, start + timedelta(days=1)


def _totals(sessions: list) -> dict:
    out: dict = {}
    for s in sessions:
        t = out.setdefault(s["harness"], {"sessions": 0, "tokens": 0, "orchestrator_tokens": 0, "cost_usd": None})
        t["sessions"] += 1
        t["tokens"] += s["tokens"]["total"]
        t["orchestrator_tokens"] += sum(a["tokens"]["total"] for a in s["agents"] if a["role"] != "worker")
        if s["cost_usd"] is not None:
            t["cost_usd"] = round((t["cost_usd"] or 0) + s["cost_usd"], 2)
    for t in out.values():
        t["orchestration_share"] = round(t["orchestrator_tokens"] / t["tokens"], 3) if t["tokens"] else 0
    return out


def _previous_days(cfg: dict, d: date, n: int) -> list:
    """Totals per harness for each of the n days before d, by session start (local day)."""
    tz = cfg["tz"]
    since, _ = day_window(d - timedelta(days=n), tz)
    until, _ = day_window(d, tz)
    sessions = scan(cfg["claude"], cfg["codex"], cfg["prices"], since, until)
    days = []
    for i in range(n, 0, -1):
        day = d - timedelta(days=i)
        ss = [s for s in sessions if s["start"] and parse_ts(s["start"]).astimezone(tz).date() == day]
        days.append({"day": day.isoformat(), "totals": _totals(ss)})
    return days


def _problems(sessions: list, per_session: dict, cfg: dict, top: int = 3) -> list:
    """The same rule-based tips the page shows per session, grouped across sessions
    and ordered by the tokens at stake."""
    groups: dict = {}
    for s in sessions:
        for tip in advice.advise(s, per_session.get(s["session_id"], []), cfg["orchestrator_limit"], cfg["output_limit"]):
            g = groups.setdefault(tip["title"], {"title": tip["title"], "sessions": 0, "tokens": 0, "worst": None,
                                                 "why": tip["why"], "try": tip["try"]})
            g["sessions"] += 1
            g["tokens"] += tip.get("tokens") or 0
            if not g["worst"] or (tip.get("tokens") or 0) > g["worst"]["tokens"]:
                g["worst"] = {"session": s["session_id"][:8], "harness": s["harness"], "project": s["project"],
                              "evidence": tip["evidence"], "tokens": tip.get("tokens") or 0}
    return sorted(groups.values(), key=lambda g: -g["tokens"])[:top]


def build(cfg: dict, scope: str, since: datetime, until: datetime, report: str | None = None,
          compare_days: int = 0) -> dict:
    """All the numbers for one review. `cfg` has the same keys as `serve`'s config."""
    sessions = scan(cfg["claude"], cfg["codex"], cfg["prices"], since, until)
    sessions.sort(key=lambda r: -r["tokens"]["total"])
    outs, breakdown = [], {}
    if cfg["codex"] and os.path.isdir(cfg["codex"]):
        o, secs = codex_outputs(cfg["codex"], since, until)
        outs += o
        breakdown["codex"] = summarise(o, secs)
    if cfg["claude"] and os.path.isdir(cfg["claude"]):
        o, secs = claude_outputs(cfg["claude"], Prices(cfg["prices"]), since, until)
        outs += o
        breakdown["claude-code"] = summarise(o, secs)
    per_session: dict = {}
    for o in outs:
        per_session.setdefault(o["session"], []).append(o)
    findings = rules.orchestrator_share(sessions, cfg["orchestrator_limit"])
    findings += rules.large_outputs([(o["session"], o["agent"], o["label"], o["tokens"], o["calls_after"]) for o in outs],
                                    cfg["output_limit"])
    r2 = rules.check_report(report, cfg["codex"], cfg["claude"], cfg["prices"], cfg["tz"]) if report else []
    data = {
        "scope": scope, "since": since.isoformat(), "until": until.isoformat(),
        "made_at": datetime.now(timezone.utc).isoformat(),
        "report": os.path.basename(report) if report else None,
        "totals": _totals(sessions),
        "top_sessions": [{
            "session": s["session_id"][:8], "harness": s["harness"], "project": s["project"], "start": s["start"],
            "models": s["models"], "tokens": s["tokens"]["total"], "cost_usd": s["cost_usd"],
            "active_s": s["active_s"], "orchestration_share": s["orchestration_share"], "subagents": s["subagents"],
        } for s in sessions[:5]],
        "problems": _problems(sessions, per_session, cfg),
        "rules": [{k: f.get(k) for k in ("rule", "session", "report", "agent", "tokens", "evidence", "why")}
                  for f in r2 + sorted(findings, key=lambda f: -(f.get("carried") or f.get("tokens") or 0))[:12]],
        "rules_fired": {r: sum(1 for f in r2 + findings if f["rule"] == r) for r in sorted({f["rule"] for f in r2 + findings})},
        "where_tokens_went": {h: [{"category": k, "calls": v["tool_calls"], "carried_tokens": v["carried_tokens"],
                                   "top": [f"{l['label']} x{l['calls']}" for l in v["labels"][:3]]}
                                  for k, v in list(b["by_category"].items())[:5]]
                              for h, b in breakdown.items()},
    }
    if compare_days:
        data["previous_days"] = _previous_days(cfg, since.astimezone(cfg["tz"]).date(), compare_days)
    return data


QUESTION = {
    "run": "Review this one run: did the run report agree with the logs, which agent or command cost the most, "
           "and the three changes that would have made it cheaper.",
    "day": "Review today's sessions: the top 3 problems with the sessions and tokens behind each, how today "
           "compares with the previous days, and the first change to make tomorrow.",
    "window": "Review this window: the top 3 problems with the sessions and tokens behind each, what the health "
              "rules found, and the three changes to make first.",
}


def prompt(data: dict) -> str:
    return (
        "You review a developer's coding-agent sessions (Claude Code and/or Codex) for cost and efficiency. Below is "
        "a JSON summary that a script computed from the session logs: totals per harness, the largest sessions, the "
        "biggest problems found by fixed rules, health-rule findings and where tool-output tokens went (\"carried\" = "
        "output size x later model calls that re-read it).\n"
        "Do not run commands or read files: answer only from these numbers, and do not recompute or invent any. "
        "Name the sessions, commands and token counts involved. Codex has no dollar prices. Short Markdown, under "
        "250 words, no headline restating the totals (they are printed above your answer).\n\n"
        f"Task: {QUESTION.get(data['scope'], QUESTION['window'])}\n\nData:\n```json\n"
        f"{json.dumps(data, indent=1, default=str)}\n```\n"
    )


def _harness_line(h: str, t: dict) -> str:
    money = f", ${t['cost_usd']:,.2f}" if h == "claude-code" and t["cost_usd"] is not None else ""
    return f"- {h}: {t['sessions']} session{'' if t['sessions'] == 1 else 's'}, {_t(t['tokens'])} tokens{money}, orchestration {t['orchestration_share']:.0%}"


def render(data: dict, summaries: dict) -> str:
    """The saved Markdown: computed numbers first, then each model's summary."""
    tz_since = data["since"][:16].replace("T", " ")
    tz_until = data["until"][:16].replace("T", " ")
    title = {"run": f"Run review: {data.get('report')}", "day": f"Daily review: {data['since'][:10]}",
             "window": "Review"}.get(data["scope"], "Review")
    lines = [f"# {title}", "", f"Window {tz_since} to {tz_until}.", "", "## Numbers", ""]
    if not data["totals"]:
        lines.append("- No sessions in this window.")
    for h, t in sorted(data["totals"].items()):
        lines.append(_harness_line(h, t))
    if data.get("previous_days"):
        lines += ["", "Previous days (tokens per harness):", ""]
        for d in data["previous_days"]:
            parts = [f"{h} {_t(t['tokens'])}" for h, t in sorted(d["totals"].items())] or ["no sessions"]
            lines.append(f"- {d['day']}: {', '.join(parts)}")
    if data["top_sessions"]:
        lines += ["", "## Largest sessions", ""]
        for s in data["top_sessions"]:
            money = f" ${s['cost_usd']:.2f}" if s["cost_usd"] is not None else ""
            lines.append(f"- {s['session']} ({s['harness']}, {s['project']}): {_t(s['tokens'])} tokens{money}, "
                         f"orchestration {s['orchestration_share']:.0%}, {s['subagents']} subagent{'' if s['subagents'] == 1 else 's'}")
    if data["problems"]:
        lines += ["", "## Top problems", ""]
        for i, p in enumerate(data["problems"], 1):
            w = p["worst"]
            lines.append(f"{i}. **{p['title']}**: {p['sessions']} session(s), about {_t(p['tokens'])} tokens at stake. "
                         f"Worst: {w['session']} ({w['harness']}), {w['evidence']} Try: {p['try']}")
    if data["scope"] == "run":
        r2 = [f for f in data["rules"] if f["rule"].startswith("R2")]
        lines += ["", "## Run report vs logs", "",
                  f"- {r2[0]['evidence']}" if r2 else "- The report's tokens and orchestration split agree with the logs (within 10%)."]
    lines += ["", "## Health rules", ""]
    if not [r for r in data["rules_fired"] if not (data["scope"] == "run" and r.startswith("R2"))]:
        lines.append("- None fired.")
    for r, n in data["rules_fired"].items():
        if data["scope"] == "run" and r.startswith("R2"):
            continue  # shown in its own section above
        ex = next((f for f in data["rules"] if f["rule"] == r), None)
        lines.append(f"- {r}: {n}" + (f". Strongest: {ex['evidence']}" if ex else ""))
    for engine, text in summaries.items():
        lines += ["", f"## Summary ({ENGINE_NAME.get(engine, engine)})", "", text.strip()]
    return "\n".join(lines) + "\n"


def run(cfg: dict, scope: str, since: datetime, until: datetime, report: str | None = None, engines=("claude",),
        out_dir: str | None = None, compare_days: int = 0) -> dict:
    """Build, summarise, save. Returns {name, path, markdown, data, errors}."""
    data = build(cfg, scope, since, until, report, compare_days)
    summaries, errors = {}, {}
    p = prompt(data)
    for e in engines:
        model = cfg.get("ask_codex_model" if e == "codex" else "ask_model") or ""
        try:
            summaries[e] = advice.ask(p, model, timeout=400, engine=e)
        except Exception as ex:  # keep the numbers even when a model is unavailable
            errors[e] = str(ex)
            summaries[e] = f"_{ENGINE_NAME.get(e, e)} could not write a summary: {str(ex)[:300]}_"
    data["engines"] = list(engines)
    text = render(data, summaries)
    out_dir = out_dir or default_dir()
    os.makedirs(out_dir, mode=0o700, exist_ok=True)
    stamp = datetime.now().astimezone().strftime("%Y-%m-%d-%H%M%S")  # this computer's clock, so names sort
    name = f"{stamp}-{scope}.md"
    path = os.path.join(out_dir, name)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    with open(path[:-3] + ".json", "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=1, default=str)
    return {"name": name, "path": path, "markdown": text, "data": data, "errors": errors}


def list_reviews(out_dir: str | None = None, limit: int = 100) -> list:
    out_dir = out_dir or default_dir()
    if not os.path.isdir(out_dir):
        return []
    rows = []
    for name in sorted((n for n in os.listdir(out_dir) if NAME_RE.match(n)), reverse=True)[:limit]:
        row = {"name": name, "scope": name[:-3].split("-", 4)[-1]}
        try:
            with open(os.path.join(out_dir, name[:-3] + ".json"), encoding="utf-8") as fh:
                d = json.load(fh)
            row.update({k: d.get(k) for k in ("scope", "since", "until", "made_at", "report", "engines")})
            row["tokens"] = sum(t["tokens"] for t in d.get("totals", {}).values())
        except (OSError, ValueError):
            pass
        rows.append(row)
    return sorted(rows, key=lambda r: r.get("made_at") or "", reverse=True)


def load(name: str, out_dir: str | None = None) -> str | None:
    if not NAME_RE.match(name or ""):  # only names this module wrote; no paths
        return None
    path = os.path.join(out_dir or default_dir(), name)
    if not os.path.isfile(path):
        return None
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def latest_reports(report_dirs: list, limit: int = 20) -> list:
    """Run reports (docs/runs/*.md) newest first, as (id, path) where id is 'n:name'."""
    found = []
    for i, d in enumerate(report_dirs):
        if os.path.isdir(d):
            for name in os.listdir(d):
                if name.endswith(".md"):
                    p = os.path.join(d, name)
                    found.append((os.path.getmtime(p), f"{i}:{name}", p))
    return [(rid, p) for _, rid, p in sorted(found, reverse=True)[:limit]]


def report_window(path: str, tz) -> tuple[datetime, datetime] | None:
    rep = rules.parse_run_report(path, tz)
    if "start" not in rep:
        return None
    return rep["start"], rep["end"]
