"""Health rules. Each returns findings: dicts with rule, session, agent, tokens,
evidence and why it matters. Thresholds are arguments so they can be tuned."""

from __future__ import annotations

import os
import re
from datetime import datetime, timedelta, timezone

from .parse import scan

# R1 ------------------------------------------------------------------------

def orchestrator_share(sessions: list, limit: float = 0.40, min_tokens: int = 1_000_000) -> list:
    """The orchestrator (plus approval reviewers) should coordinate, not do the
    work. Flag sessions with subagents where it takes more than `limit`."""
    out = []
    for s in sessions:
        if not s["subagents"] or s["tokens"]["total"] < min_tokens or s["orchestration_share"] <= limit:
            continue
        orch = [a for a in s["agents"] if a["role"] != "worker"]
        main = next((a for a in orch if a["role"] == "orchestrator"), orch[0])
        out.append({
            "rule": "R1 orchestrator share",
            "harness": s["harness"], "session": s["session_id"], "project": s["project"],
            "agent": "orchestrator",
            "tokens": sum(a["tokens"]["total"] for a in orch),
            "cost_usd": sum(a["cost_usd"] or 0 for a in orch) if s["cost_usd"] is not None else None,
            "evidence": f"{s['orchestration_share']:.0%} of {s['tokens']['total']:,} tokens; "
                        f"orchestrator made {main['model_calls']} model calls, largest prompt {main['max_context']:,} tokens",
            "why": "every orchestrator call re-sends its whole context; tokens spent coordinating build nothing",
        })
    return out


# R2 ------------------------------------------------------------------------

_NUM = r"\$?-?[\d.]+[kM]?"


def _num(s: str) -> float | None:
    s = s.strip().lstrip("$")
    if not s:
        return None
    mult = {"k": 1e3, "M": 1e6}.get(s[-1], 1)
    try:
        return float(s.rstrip("kM")) * mult
    except ValueError:
        return None


def parse_run_report(path: str, tz: timezone) -> dict:
    text = open(path, encoding="utf-8").read()
    rep = {"path": os.path.basename(path), "problems": []}
    m = re.search(r"^- Start: \S+ \((\d{4}-\d\d-\d\d \d\d:\d\d)\)", text, re.M)
    n = re.search(r"^- End: \S+ \((\d{4}-\d\d-\d\d \d\d:\d\d)\)", text, re.M)
    if m and n:
        rep["start"] = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M").replace(tzinfo=tz)
        rep["end"] = datetime.strptime(n.group(1), "%Y-%m-%d %H:%M").replace(tzinfo=tz) + timedelta(minutes=1)
    t = re.search(r"^- Tokens: (\S+) — feature agents ([^,]*), orchestration (\S+)", text, re.M)
    if t:
        rep["tokens"] = {"total": _num(t.group(1)), "agents": _num(t.group(2)), "orchestration": _num(t.group(3))}
        if rep["tokens"]["agents"] is None:
            rep["problems"].append("feature-agent tokens are blank")
    c = re.search(r"^- Cost: \$(\S+) — feature agents \$(\S+), orchestration \$(\S+)", text, re.M)
    if c:
        rep["cost"] = {"total": _num(c.group(1)), "agents": _num(c.group(2)), "orchestration": _num(c.group(3))}
    neg = re.findall(r"\$-[\d.]+", text)
    if neg:
        rep["problems"].append(f"{len(neg)} negative dollar amounts ({', '.join(neg)})")
    if re.search(r"^- (Cost|Tokens) by model:.*\bunknown\b", text, re.M):
        rep["problems"].append("a model is reported as 'unknown'")
    return rep


def report_vs_logs(report_dirs: list, codex_root=None, claude_root=None, prices=None,
                   tz: timezone = timezone.utc, tolerance: float = 0.10) -> list:
    """A run report should agree with the session logs for the same window."""
    out = []
    for d in report_dirs:
        if not os.path.isdir(d):
            continue
        for name in sorted(os.listdir(d)):
            if name.endswith(".md"):
                out += check_report(os.path.join(d, name), codex_root, claude_root, prices, tz, tolerance)
    return out


def check_report(path: str, codex_root=None, claude_root=None, prices=None,
                 tz: timezone = timezone.utc, tolerance: float = 0.10) -> list:
    """R2 for one run report (docs/runs/<name>.md); its project is the folder above docs/."""
    rep = parse_run_report(path, tz)
    problems = list(rep["problems"])
    # windows come from commit times to the minute; too short to compare reliably
    if "tokens" in rep and "start" in rep and codex_root and rep["end"] - rep["start"] >= timedelta(minutes=5):
        project = os.path.basename(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(path)))))
        logs = [s for s in scan(None, codex_root, prices, rep["start"], rep["end"]) if s["project"] == project]
        total = sum(s["tokens"]["total"] for s in logs)
        orch = sum(a["tokens"]["total"] for s in logs for a in s["agents"] if a["role"] != "worker")
        said = rep["tokens"]
        if total and said["total"] is not None and abs(said["total"] - total) / total > tolerance:
            problems.append(f"report says {said['total']:,.0f} tokens, logs say {total:,} for the same window")
        if orch and said["orchestration"] is not None and abs(said["orchestration"] - orch) / orch > tolerance:
            problems.append(f"report says orchestration {said['orchestration']:,.0f}, logs say {orch:,}")
    if not problems:
        return []
    return [{"rule": "R2 report matches logs", "report": rep["path"], "agent": "run report",
             "tokens": None, "evidence": "; ".join(problems),
             "why": "budgets, the orchestration split and model choice are tuned from these numbers"}]


# R3 ------------------------------------------------------------------------

def large_outputs(breakdown_events: list, limit: int = 8000) -> list:
    """A single tool output this big stays in context for every later call.
    `breakdown_events` are (session, role, script, tokens, calls_after)."""
    out = []
    for sess, role, script, tok, after in breakdown_events:
        if tok >= limit:
            out.append({"rule": "R3 large tool output", "session": sess, "agent": role, "script": script,
                        "tokens": tok, "carried": tok * after,
                        "evidence": f"{script} printed ~{tok:,} tokens, re-read by {after} later calls",
                        "why": "output that size is near the CLI's truncation cap and is paid for on every following call"})
    return out
