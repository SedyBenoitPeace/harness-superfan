"""superfan: per-session token and cost summary for coding-agent logs.

  python3 -m superfan scan --claude DIR --codex DIR --days 7 --out sessions.json
  python3 -m superfan top sessions.json --n 5
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone

from .parse import scan
from .breakdown import codex_breakdown, claude_outputs
from .parse import Prices
from . import rules

HERE = os.path.dirname(os.path.abspath(__file__))


def fmt_tok(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.0f}k"
    return str(n)


def fmt_dur(s: int) -> str:
    h, m = divmod(s // 60, 60)
    return f"{h}h{m:02d}" if h else f"{m}m"


def cmd_scan(a: argparse.Namespace) -> int:
    until = datetime.fromisoformat(a.until).replace(tzinfo=timezone.utc) if a.until else datetime.now(timezone.utc)
    since = until - timedelta(days=a.days)
    with open(a.prices or os.path.join(HERE, "prices.json")) as fh:
        prices = json.load(fh)
    rows = scan(a.claude, a.codex, prices, since, until)
    rows.sort(key=lambda r: -r["tokens"]["total"])
    doc = {"since": since.isoformat(), "until": until.isoformat(), "sessions": rows}
    text = json.dumps(doc, indent=1)
    if a.out:
        with open(a.out, "w") as fh:
            fh.write(text)
        print(f"{len(rows)} sessions -> {a.out}", file=sys.stderr)
    else:
        print(text)
    return 0


def cmd_top(a: argparse.Namespace) -> int:
    with open(a.file) as fh:
        doc = json.load(fh)
    rows = doc["sessions"]
    key = (lambda r: -(r["cost_usd"] or 0)) if a.by == "cost" else (lambda r: -r["tokens"]["total"])
    rows = sorted(rows, key=key)[: a.n]
    print("| # | harness | project | session | model | tokens (in / cache read / cache write / out) | $ | active (wall) | tool calls | orchestrator share | subagents |")
    print("|---|---|---|---|---|---|---|---|---|---|---|")
    for i, r in enumerate(rows, 1):
        t = r["tokens"]
        cost = f"${r['cost_usd']:.2f}" if r["cost_usd"] is not None else "—"
        print(f"| {i} | {r['harness']} | {r['project']} | {r['session_id'][:8]} | {', '.join(r['models'])} | "
              f"{fmt_tok(t['total'])} ({fmt_tok(t['input'])} / {fmt_tok(t['cache_read'])} / {fmt_tok(t['cache_write'])} / {fmt_tok(t['output'])}) | "
              f"{cost} | {fmt_dur(r['active_s'])} ({fmt_dur(r['duration_s'])}) | {r['tool_calls']} | {r['orchestration_share']:.0%} | {r['subagents']} |")
    return 0


def _outputs(a, prices, since, until) -> list:
    """Tool outputs of both harnesses as (session, agent, label, tokens, calls_after), for rule R3."""
    out = codex_breakdown(a.codex, since, until)["outputs"] if a.codex else []
    if a.claude:
        out += [(o["session"], o["agent"], o["label"], o["tokens"], o["calls_after"])
                for o in claude_outputs(a.claude, Prices(prices), since, until)[0]]
    return out


def _tz(s: str) -> timezone:
    sign = -1 if s.startswith("-") else 1
    h, m = s.lstrip("+-").split(":")
    return timezone(sign * timedelta(hours=int(h), minutes=int(m)))


def cmd_check(a: argparse.Namespace) -> int:
    until = datetime.fromisoformat(a.until).replace(tzinfo=timezone.utc) if a.until else datetime.now(timezone.utc)
    since = until - timedelta(days=a.days)
    with open(a.prices or os.path.join(HERE, "prices.json")) as fh:
        prices = json.load(fh)
    sessions = scan(a.claude, a.codex, prices, since, until)
    findings = rules.orchestrator_share(sessions, a.orchestrator_limit)
    findings += rules.report_vs_logs(a.reports or [], a.codex, a.claude, prices, _tz(a.tz))
    findings += rules.large_outputs(_outputs(a, prices, since, until), a.output_limit)
    print(json.dumps(findings, indent=1, default=str))
    return 0


def cmd_report(a: argparse.Namespace) -> int:
    """The weekly overview, printed to the terminal."""
    until = datetime.fromisoformat(a.until).replace(tzinfo=timezone.utc) if a.until else datetime.now(timezone.utc)
    since = until - timedelta(days=a.days)
    with open(a.prices or os.path.join(HERE, "prices.json")) as fh:
        prices = json.load(fh)
    rows = scan(a.claude, a.codex, prices, since, until)
    print(f"Harness Superfan: {since:%Y-%m-%d} to {until:%Y-%m-%d}\n")
    for h in sorted({r["harness"] for r in rows}):
        hs = [r for r in rows if r["harness"] == h]
        tot = sum(r["tokens"]["total"] for r in hs)
        orch = sum(a2["tokens"]["total"] for r in hs for a2 in r["agents"] if a2["role"] != "worker")
        cost = sum(r["cost_usd"] or 0 for r in hs)
        money = f", ${cost:,.2f}" if h == "claude-code" else ""
        print(f"{h}: {len(hs)} sessions, {fmt_tok(tot)} tokens{money}, orchestration {orch / tot:.0%}" if tot else f"{h}: no usage")
    print(f"\nTop {a.n}:")
    for i, r in enumerate(sorted(rows, key=lambda r: -r["tokens"]["total"])[: a.n], 1):
        money = f" ${r['cost_usd']:.2f}" if r["cost_usd"] is not None else ""
        print(f" {i}. {fmt_tok(r['tokens']['total']):>6}{money}  {r['harness']:<11} {r['project'][:28]:<28} "
              f"{r['session_id'][:8]}  {fmt_dur(r['active_s'])} active, {r['model_calls']} calls, "
              f"orchestration {r['orchestration_share']:.0%}, {r['subagents']} subagents")
    findings = rules.orchestrator_share(rows, a.orchestrator_limit)
    findings += rules.report_vs_logs(a.reports or [], a.codex, a.claude, prices, _tz(a.tz))
    findings += rules.large_outputs(_outputs(a, prices, since, until), a.output_limit)
    print("\nHealth rules:")
    if not findings:
        print(" none fired")
    for rule in sorted({f["rule"] for f in findings}):
        fs = [f for f in findings if f["rule"] == rule]
        print(f" {rule}: {len(fs)}")
        for f in sorted(fs, key=lambda f: -(f.get("carried") or f.get("tokens") or 0))[:3]:
            where = f.get("session", f.get("report", ""))[:18]
            print(f"   - {where}: {f['evidence']}")
    return 0


def _cfg(a: argparse.Namespace) -> dict:
    """The settings `serve` and `review` share."""
    with open(a.prices or os.path.join(HERE, "prices.json")) as fh:
        prices = json.load(fh)
    return {"claude": os.path.expanduser(a.claude) if a.claude else None,
            "codex": os.path.expanduser(a.codex) if a.codex else None,
            "reports": [os.path.expanduser(d) for d in a.reports or []],
            "tz": _tz(a.tz) if a.tz else datetime.now().astimezone().tzinfo, "prices": prices,
            "orchestrator_limit": a.orchestrator_limit, "output_limit": a.output_limit, "ask_model": a.ask_model,
            "ask_codex_model": a.ask_codex_model, "reviews": os.path.expanduser(a.reviews) if a.reviews else None}


def cmd_review(a: argparse.Namespace) -> int:
    """One review of a run, a day or a window, saved outside the repo."""
    from . import review
    cfg = _cfg(a)
    tz = cfg["tz"]
    report, compare = None, 0
    if a.run == "latest":  # for automatic runs: the newest report, once
        found = review.latest_reports(cfg["reports"], 1)
        if not found:
            print("no run reports in --reports", file=sys.stderr)
            return 0
        a.run = found[0][1]
        if any(r.get("report") == os.path.basename(a.run) for r in review.list_reviews(cfg["reviews"])):
            print(f"{os.path.basename(a.run)} already reviewed", file=sys.stderr)
            return 0
    if a.run:
        report = os.path.expanduser(a.run)
        w = review.report_window(report, tz)
        if not w:
            print(f"{a.run}: no Start/End lines, so the run's window is unknown", file=sys.stderr)
            return 2
        scope, (since, until) = "run", w
    elif a.since:
        scope = "window"
        since = datetime.fromisoformat(a.since)
        until = datetime.fromisoformat(a.until) if a.until else datetime.now(tz)
        since, until = (d if d.tzinfo else d.replace(tzinfo=tz) for d in (since, until))
    else:
        scope, compare = "day", a.compare
        d = datetime.strptime(a.day, "%Y-%m-%d").date() if a.day and a.day != "today" else datetime.now(tz).date()
        since, until = review.day_window(d, tz)
    engines = [] if a.with_ == "none" else ["claude", "codex"] if a.with_ == "both" else [a.with_]
    r = review.run(cfg, scope, since, until, report, engines, cfg["reviews"], compare)
    print(r["markdown"] if not a.quiet else "", end="")
    print(f"Saved {r['path']}", file=sys.stderr)
    return 1 if r["errors"] and len(r["errors"]) == len(engines) else 0


def cmd_serve(a: argparse.Namespace) -> int:
    from .serve import serve
    cfg = _cfg(a)
    cfg["port"] = a.port
    if not a.no_open:
        import threading
        import webbrowser
        threading.Timer(0.8, webbrowser.open, [f"http://127.0.0.1:{a.port}/"]).start()
    serve(cfg)
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="superfan")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("scan", help="summarise sessions in a time window")
    s.add_argument("--claude", help="Claude Code projects folder (usually ~/.claude/projects)")
    s.add_argument("--codex", help="Codex sessions folder (usually ~/.codex/sessions)")
    s.add_argument("--days", type=float, default=7)
    s.add_argument("--until", help="ISO date/time the window ends (default: now, UTC)")
    s.add_argument("--prices", help="price table JSON (default: bundled prices.json)")
    s.add_argument("--out")
    s.set_defaults(fn=cmd_scan)
    t = sub.add_parser("top", help="print the N largest sessions as a Markdown table")
    t.add_argument("file")
    t.add_argument("--n", type=int, default=5)
    t.add_argument("--by", choices=["tokens", "cost"], default="tokens")
    t.set_defaults(fn=cmd_top)
    c = sub.add_parser("check", help="run the health rules and print findings as JSON")
    c.add_argument("--claude")
    c.add_argument("--codex")
    c.add_argument("--reports", nargs="*", help="docs/runs folders of projects that use the kit")
    c.add_argument("--tz", default="+00:00", help="UTC offset the run reports' local times are in")
    c.add_argument("--days", type=float, default=7)
    c.add_argument("--until")
    c.add_argument("--prices")
    c.add_argument("--orchestrator-limit", type=float, default=0.40)
    c.add_argument("--output-limit", type=int, default=8000)
    c.set_defaults(fn=cmd_check)
    r = sub.add_parser("report", help="print the weekly overview: totals, top sessions, health rules")
    for name, kw in [("--claude", {}), ("--codex", {}), ("--reports", {"nargs": "*"}), ("--tz", {"default": "+00:00"}),
                     ("--days", {"type": float, "default": 7}), ("--until", {}), ("--prices", {}),
                     ("--n", {"type": int, "default": 5}), ("--orchestrator-limit", {"type": float, "default": 0.40}),
                     ("--output-limit", {"type": int, "default": 8000})]:
        r.add_argument(name, **kw)
    r.set_defaults(fn=cmd_report)
    w = sub.add_parser("serve", help="open a local web page with a Run now button")
    w.add_argument("--claude", default="~/.claude/projects", help="Claude Code projects folder ('' to skip)")
    w.add_argument("--codex", default="~/.codex/sessions", help="Codex sessions folder ('' to skip)")
    w.add_argument("--reports", nargs="*", default=[], help="docs/runs folders of projects that use the kit")
    w.add_argument("--tz", help="UTC offset of the run reports' local times (default: this computer's)")
    w.add_argument("--port", type=int, default=8765)
    w.add_argument("--prices")
    w.add_argument("--orchestrator-limit", type=float, default=0.40)
    w.add_argument("--output-limit", type=int, default=8000)
    w.add_argument("--ask-model", default="sonnet", help="model for the Ask Claude button ('' for your default)")
    w.add_argument("--ask-codex-model", default="", help="model for the Ask ChatGPT button (default: your Codex default)")
    w.add_argument("--reviews", help="folder for saved reviews (default: $SUPERFAN_REVIEWS or ~/.superfan/reviews)")
    w.add_argument("--no-open", action="store_true", help="do not open the browser")
    w.set_defaults(fn=cmd_serve)
    v = sub.add_parser("review", help="review a run, a day or a window; a model writes the summary, the numbers are computed")
    g = v.add_mutually_exclusive_group()
    g.add_argument("--run", metavar="REPORT", help="a run report (docs/runs/<name>.md): every session in its window, plus the report-vs-logs check; "
                        "'latest' takes the newest report in --reports and skips it if already reviewed")
    g.add_argument("--day", nargs="?", const="today", metavar="YYYY-MM-DD", help="all sessions that day (the default: today)")
    g.add_argument("--since", help="start of a custom window (ISO date/time, local time if no offset)")
    v.add_argument("--until", help="end of the custom window (default: now)")
    v.add_argument("--compare", type=int, default=6, help="with --day: how many previous days to compare with")
    v.add_argument("--with", dest="with_", choices=["claude", "codex", "both", "none"], default="claude",
                   help="who writes the summary: claude (default), codex, both, or none for numbers only")
    v.add_argument("--claude", default="~/.claude/projects", help="Claude Code projects folder ('' to skip)")
    v.add_argument("--codex", default="~/.codex/sessions", help="Codex sessions folder ('' to skip)")
    v.add_argument("--reports", nargs="*", default=[])
    v.add_argument("--tz", help="UTC offset of your local day and of run reports (default: this computer's)")
    v.add_argument("--prices")
    v.add_argument("--orchestrator-limit", type=float, default=0.40)
    v.add_argument("--output-limit", type=int, default=8000)
    v.add_argument("--ask-model", default="sonnet")
    v.add_argument("--ask-codex-model", default="")
    v.add_argument("--reviews", help="folder to save to (default: $SUPERFAN_REVIEWS or ~/.superfan/reviews)")
    v.add_argument("--quiet", action="store_true", help="only print where it was saved")
    v.set_defaults(fn=cmd_review)
    a = p.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
