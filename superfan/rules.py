"""Health rules. Each returns findings: dicts with rule, session, agent, tokens,
evidence and why it matters. Thresholds are arguments so they can be tuned."""

from __future__ import annotations

import json
import os
import re
import subprocess
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


def _commit_time(repo: str, ref: str) -> datetime | None:
    """Commit time of `ref` in `repo`, read-only; None when git or the commit isn't there."""
    if not re.fullmatch(r"[0-9a-f]{4,40}", ref) or not os.path.isdir(os.path.join(repo, ".git")):
        return None
    try:
        r = subprocess.run(["git", "-C", repo, "show", "-s", "--format=%cI", ref], capture_output=True, text=True, timeout=10)
        return datetime.fromisoformat(r.stdout.strip()).replace(second=0) if r.returncode == 0 else None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


_parsed: dict = {}  # (path, mtime, tz) -> parsed report; parsing asks git for two commit times


def parse_run_report(path: str, tz: timezone) -> dict:
    key = (path, os.path.getmtime(path), tz)
    if key not in _parsed:
        _parsed[key] = _parse_run_report(path, tz)
    return {**_parsed[key], "problems": list(_parsed[key]["problems"])}


def _parse_run_report(path: str, tz: timezone) -> dict:
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    rep = {"path": os.path.basename(path), "problems": []}
    m = re.search(r"^- Start: (\S+) \((\d{4}-\d\d-\d\d \d\d:\d\d)\)", text, re.M)
    n = re.search(r"^- End: (\S+) \((\d{4}-\d\d-\d\d \d\d:\d\d)\)", text, re.M)
    if m and n:
        # The commits carry their own UTC offset; the printed local time is only right in the
        # offset the run had, which differs after a clock change or a trip.
        repo = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(path))))
        rep["start"] = _commit_time(repo, m.group(1)) or datetime.strptime(m.group(2), "%Y-%m-%d %H:%M").replace(tzinfo=tz)
        rep["end"] = (_commit_time(repo, n.group(1)) or datetime.strptime(n.group(2), "%Y-%m-%d %H:%M").replace(tzinfo=tz)) \
            + timedelta(minutes=1)
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


def report_dirs(folders: list) -> list:
    """Each folder may be a docs/runs folder, a project that has one, or a folder
    of projects (every <project>/docs/runs one level down)."""
    out = []
    for d in folders:
        d = os.path.expanduser(d)
        runs = os.path.join(d, "docs", "runs")
        if os.path.isdir(runs):
            out.append(runs)
        elif os.path.isdir(d) and not any(n.endswith(".md") for n in os.listdir(d)):
            subs = sorted(os.path.join(d, n, "docs", "runs") for n in os.listdir(d)
                          if os.path.isdir(os.path.join(d, n, "docs", "runs")))
            out += subs or [d]
        else:
            out.append(d)
    return list(dict.fromkeys(out))


def report_project(path: str) -> str:
    """A report lives in <project>/docs/runs/<name>.md."""
    return os.path.basename(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(path)))))


def same_project(session_project: str, project: str) -> bool:
    """Parallel lanes run in <project>-<feature id> worktrees next to the repo."""
    return session_project == project or bool(re.fullmatch(re.escape(project) + r"-[A-Za-z]+\d+-\d+", session_project or ""))


_report_cache: dict = {}  # key -> findings; a finished run's logs don't change


def _load_cache(cache_file: str | None) -> None:
    if not cache_file or _report_cache.get(("loaded", cache_file)):
        return
    _report_cache[("loaded", cache_file)] = True
    try:
        with open(cache_file, encoding="utf-8") as fh:
            _report_cache.update(json.load(fh))
    except (OSError, ValueError):
        pass


def _save_cache(cache_file: str | None) -> None:
    if not cache_file:
        return
    try:
        os.makedirs(os.path.dirname(cache_file), exist_ok=True)
        with open(cache_file + ".tmp", "w", encoding="utf-8") as fh:
            json.dump({k: v for k, v in _report_cache.items() if isinstance(k, str)}, fh)
        os.replace(cache_file + ".tmp", cache_file)
    except OSError:
        pass


def report_vs_logs(report_dirs: list, codex_root=None, claude_root=None, prices=None,
                   tz: timezone = timezone.utc, tolerance: float = 0.10, copilot_root=None,
                   since: datetime | None = None, until: datetime | None = None,
                   cache_file: str | None = None) -> list:
    """A run report should agree with the session logs for the same window.
    With since/until, only reports whose run overlaps that window are checked
    (a report with no Start/End counts by its file time). Each check re-reads the
    logs for the report's window, so results are kept per report file and mtime,
    in memory and, with cache_file, on disk across restarts."""
    _load_cache(cache_file)
    out, added = [], False
    for d in report_dirs:
        if not os.path.isdir(d):
            continue
        for name in sorted(os.listdir(d)):
            if not name.endswith(".md"):
                continue
            path = os.path.join(d, name)
            st = os.stat(path)
            key = json.dumps([path, st.st_mtime, st.st_size, codex_root, copilot_root, tolerance])
            if key not in _report_cache:
                rep = parse_run_report(path, tz)
                if since or until:
                    mtime = datetime.fromtimestamp(st.st_mtime, timezone.utc)
                    start, end = rep.get("start", mtime), rep.get("end", mtime)
                    if (until and start > until) or (since and end < since):
                        continue
                _report_cache[key] = check_report(path, codex_root, claude_root, prices, tz, tolerance, copilot_root, rep)
                added = True
            out += _report_cache[key]
    if added:
        _save_cache(cache_file)
    return out


def check_report(path: str, codex_root=None, claude_root=None, prices=None,
                 tz: timezone = timezone.utc, tolerance: float = 0.10, copilot_root=None, rep=None) -> list:
    """R2 for one run report (docs/runs/<name>.md); its project is the folder above docs/."""
    rep = rep or parse_run_report(path, tz)
    problems = list(rep["problems"])
    # windows come from commit times to the minute; too short to compare reliably
    if "tokens" in rep and "start" in rep and (codex_root or copilot_root) and rep["end"] - rep["start"] >= timedelta(minutes=5):
        project = report_project(path)
        logs = [s for s in scan(None, codex_root, prices, rep["start"], rep["end"], copilot_root) if same_project(s["project"], project)]
        total = sum(s["tokens"]["total"] for s in logs)
        orch = sum(a["tokens"]["total"] for s in logs for a in s["agents"] if a["role"] != "worker")
        comp = sum(s.get("compaction_tokens") or 0 for s in logs)
        said = rep["tokens"]
        if total and said["total"] is not None and abs(said["total"] - total) / total > tolerance:
            if comp and abs(said["total"] - (total - comp)) / total <= tolerance:
                problems.append(f"report says {said['total']:,.0f} tokens, logs say {total:,}: the report leaves out "
                                f"{comp:,} context-compaction tokens")
            else:
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


def _who(a: dict) -> str:
    role = {"orchestrator": "orchestrator", "worker": "subagent", "reviewer": "approval reviewer"}.get(a["role"], a["role"])
    return f"{role} {a['label']}".strip() if a.get("label") else role


# R4 ------------------------------------------------------------------------

def frequent_compaction(sessions: list, limit: int = 5) -> list:
    """An agent that compacts its context again and again is working in a
    window that is too full (logged by Copilot; other harnesses report 0)."""
    out = []
    for s in sessions:
        for a in s["agents"]:
            n = a.get("compactions") or 0
            if n < limit:
                continue
            out.append({
                "rule": "R4 frequent compaction",
                "harness": s["harness"], "session": s["session_id"], "project": s["project"],
                "agent": _who(a), "tokens": a.get("compaction_tokens") or 0,
                "evidence": f"{_who(a)} compacted {n} time{'' if n == 1 else 's'} over {a['model_calls']} calls, using "
                            f"{a.get('compaction_tokens') or 0:,} tokens; largest prompt {a['max_context']:,} tokens",
                "why": "each compaction re-reads the whole context to summarise it, and the agent loses detail "
                       "every time; it usually means the prompt is mostly fixed overhead",
            })
    return out


# R5 ------------------------------------------------------------------------

def tool_definitions(sessions: list, limit: int = 50_000) -> list:
    """Tool schemas are sent with every call. An agent carrying this many
    tokens of them loads every tool up front instead of searching for the few
    it uses (logged by Copilot; other harnesses report 0)."""
    out = []
    for s in sessions:
        for a in s["agents"]:
            per = a.get("tool_def_tokens") or 0
            if per < limit:
                continue
            out.append({
                "rule": "R5 tool definitions",
                "harness": s["harness"], "session": s["session_id"], "project": s["project"],
                "agent": _who(a), "tokens": per * a["model_calls"],
                "evidence": f"{_who(a)} sent ~{per:,} tokens of tool definitions with each of {a['model_calls']} calls "
                            f"(~{per * a['model_calls']:,} in total)",
                "why": "schemas for tools the agent never calls fill the window, which drives compaction; "
                       "deferred tool loading sends only the tools it searches for",
            })
    return out

