"""Where do a session's tokens go? Attribute them to the kind of work.

Every model call re-sends the whole conversation, so a tool output stays in
the bill for every call that follows it. For each tool call we estimate

  output_tokens  ~ characters of the output the model saw / 4
  carried_tokens ~ output_tokens x model calls after it in the same thread

and group both by a category: first by the tool's name (Read, apply_patch,
spawn_agent...), then, for shell tools, by the command it ran. Categories are
regular expressions, checked in order; pass your own dicts to fit another
project. Works for Codex, Claude Code and Copilot; Claude Code outputs also get a
dollar estimate (carried tokens at the model's cache-read price).
"""

from __future__ import annotations

import json
import os
import re
import shlex

from .parse import iter_jsonl, parse_ts, in_window, files_touched_since, Prices, copilot_files, copilot_project

# Tool names that say what the work is, whatever the arguments.
TOOL_CATEGORIES = {
    "reading files": r"^(Read|Grep|Glob|LS|view_image|read_file|list_dir|grep_files|view|grep|glob)$",
    "editing files": r"^(Edit|Write|MultiEdit|NotebookEdit|apply_patch|write_file|edit|create|str_replace_editor)$",
    "subagent coordination": r"^(Task|task|Agent|SendMessage|TaskOutput|TaskStop|spawn_agent|send_message|wait|wait_agent"
                             r"|close_agent|list_agents|send_input)$",
    "planning and todos": r"^(TodoWrite|TaskCreate|TaskUpdate|TaskList|update_plan|ExitPlanMode|EnterPlanMode)$",
    "web": r"^(WebFetch|WebSearch|web_search|web__run|web_fetch)$",
    "polling running commands": r"^(write_stdin|BashOutput|read_bash|write_bash)$",
    "skills": r"^(Skill|skill)$",
    "MCP tools": r"^mcp__",
}

# Shell commands, by what they run.
DEFAULT_CATEGORIES = {
    "kit scripts and gate": r"^\s*(?:(?:bash|sh|zsh)\s+)?\S*scripts/[\w./-]+\.sh|^\s*\S*(run-gate|native-gate|quiet|e2e)\b",
    "skills and agent docs": r"(SKILL|AGENTS|CLAUDE)\.md|/plugins/cache/|/skills/|harness-protocol",
    "reading files": r"^\s*(cat|sed|rg|grep|head|tail|nl|ls|find|wc|tree|less|stat|file|du|jq)\b",
    "simulator / UI tests": r"simctl|[Ss]imulator|booted|UITests?\b|XCUI|-destination",
    "xcodebuild (build/test)": r"xcodebuild|xcrun|xcresult",
    "swift build/test": r"\bswift (build|test)\b",
    "git": r"^\s*(git|gh)\b",
    "package and test runners": r"^\s*(npm|npx|pnpm|yarn|pytest|cargo|go|make|pip3?|uv|bun)\b",
    "inline scripts": r"^\s*(python3?|node|ruby|perl|swift)\b",
}

SHELL_TOOLS = r"^(Bash|bash|exec_command|shell|local_shell_call|shell_command)$"
TOP_N = 12  # rows kept per drill-down list


def _text(x) -> str:
    if isinstance(x, str):
        return x
    if isinstance(x, list):
        return "".join(_text(i) for i in x if not (isinstance(i, dict) and i.get("type") == "image"))
    if isinstance(x, dict):
        return _text(x.get("text") or x.get("output") or x.get("content") or "")
    return ""


def categorise(text: str, cats: dict) -> str:
    for name, rx in cats.items():
        if re.search(rx, text, re.M):
            return name
    return "other"


def _unquote(q: str) -> str:
    if q[0] == '"':
        try:
            return json.loads(q)
        except ValueError:
            pass
    return q[1:-1].replace("\\n", "\n")


def _parts(tool: str, body) -> tuple[str, str]:
    """(shell command, inner tool) of a call. Codex's `exec` tool takes a
    JavaScript cell that calls tools.exec_command({cmd: "..."}) or other tools."""
    if isinstance(body, dict):
        c = body.get("command") or body.get("cmd") or ""
        return (" ".join(map(str, c)) if isinstance(c, list) else str(c)), ""
    if not isinstance(body, str) or not body:
        return "", ""
    m = re.search(r'"(?:command|cmd)"\s*:\s*(\[[^\]]*\]|"(?:[^"\\]|\\.)*")', body)
    if m:
        try:
            v = json.loads(m.group(1))
            return (" ".join(map(str, v)) if isinstance(v, list) else str(v)), ""
        except ValueError:
            pass
    cmds = re.findall(r"""\bcmd\s*:\s*("(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*'|`[^`]*`)""", body)
    if cmds:
        return "\n".join(_unquote(c) for c in cmds), ""
    inner = [t for t in re.findall(r"tools\.(\w+)\s*\(", body) if t != "exec_command"]
    if inner:
        return "", inner[0]
    return (body, "") if re.search(SHELL_TOOLS, tool) else ("", "")


def _command(body) -> str:
    return _parts("", body)[0]


def _first_segment(cmd: str) -> str:
    """The first command of a cell or pipeline, without a leading `cd x &&`."""
    cmd = re.sub(r"^\s*cd\s+\S+\s*(?:&&|;)\s*", "", cmd.strip())
    seg = re.split(r"\s*(?:&&|;|\||\n)\s*", cmd)[0]
    return re.sub(r"^(?:\w+=(?:'[^']*'|\"[^\"]*\"|\S*)\s+)+", "", seg)  # VAR=value prefixes


def _first_words(cmd: str) -> str:
    seg = _first_segment(cmd)
    s = re.search(r"scripts/[\w.-]+\.sh", seg)
    if s:
        return s.group(0)
    try:
        words = shlex.split(seg)
    except ValueError:
        words = seg.split()
    while words and re.fullmatch(r"\w+=\S*", words[0]):
        words = words[1:]  # VAR=value prefixes
    keep = [w for w in words[:4] if not w.startswith("-")][:2] or words[:1]
    return " ".join(os.path.basename(w) if "/" in w else w for w in keep)[:60]


def _label(tool: str, body, cat: str = "") -> str:
    """A short name to group similar calls: the kit script, the first words of
    a command, the file a Read opened, the inner tool, or the tool name."""
    cmd, inner = _parts(tool, body)
    if cmd:
        return _first_words(cmd) or tool
    if isinstance(body, dict):
        for k in ("file_path", "path", "notebook_path"):
            if body.get(k):
                return f"{tool} {os.path.basename(str(body[k]))}"
        if body.get("subagent_type"):
            return f"{tool} {body['subagent_type']}"
        if tool == "Skill" and body.get("skill"):
            return f"Skill {body['skill']}"
    return inner or tool


def _sample(tool: str, body) -> str:
    """A one-line example of the call, for the drill-down."""
    cmd, inner = _parts(tool, body)
    s = cmd
    if not s and isinstance(body, dict):
        s = (body.get("file_path") or body.get("pattern") or body.get("description") or body.get("url")
             or body.get("query") or "")
    if not s and inner:
        s = inner
    return re.sub(r"\s+", " ", str(s))[:160]


def classify(tool: str, body, cats: dict | None = None) -> str:
    cat = categorise(tool, TOOL_CATEGORIES)
    if cat != "other":
        return cat
    cmd, inner = _parts(tool, body)
    if cmd:  # by the first command, the same one the label names
        return categorise(_first_segment(cmd), cats or DEFAULT_CATEGORIES)
    if inner:
        return categorise(inner, TOOL_CATEGORIES)
    return "other"


# ---------------------------------------------------------------------- Codex

def codex_outputs(root: str, since=None, until=None, categories=None, session_filter=None) -> tuple[list, dict]:
    """One dict per tool output, plus command run time per category."""
    out: list = []
    cmd_seconds: dict = {}
    for path in files_touched_since(root, since):
        meta = None
        start_ord = 0
        calls: dict = {}
        events = []
        project = "?"
        for r in iter_jsonl(path):
            t, p = r.get("type"), r.get("payload") or {}
            if meta is None:
                if t != "session_meta":
                    break
                meta = p
                start_ord = p.get("subagent_history_start_ordinal") or 0
                root_id = p.get("session_id") or p.get("parent_thread_id") or p.get("id")
                cwd = (p.get("runtime_workspace_roots") or [p.get("cwd") or ""])[0]
                project = os.path.basename(cwd.rstrip("/")) or "?"
                if project.startswith("superfan-ask-"):
                    break  # a question the Superfan page itself asked
                if session_filter and not session_filter(root_id, p):
                    break
                continue
            if (r.get("ordinal") or 0) < start_ord:
                continue
            ts = parse_ts(r.get("timestamp"))
            if not in_window(ts, since, until):
                continue
            if t == "token_usage_record" and p.get("thread_id") == meta.get("id"):
                events.append(("call",))
            elif t == "response_item" and p.get("type") in ("custom_tool_call", "function_call"):
                name = p.get("name") or ""
                body = p.get("input") or p.get("arguments") or ""
                cat = classify(name, body, categories)
                calls[p.get("call_id")] = (cat, _label(name, body, cat), name, _sample(name, body))
            elif t == "response_item" and p.get("type") in ("custom_tool_call_output", "function_call_output"):
                cat, label, name, sample = calls.get(p.get("call_id"), ("other", "?", "?", ""))
                events.append(("out", cat, len(_text(p.get("output"))) // 4, label, name, sample))
            elif t == "event_msg" and p.get("type") == "item_completed":
                it = p.get("item") or {}
                if it.get("type") == "CommandExecution" and p.get("thread_id") == meta.get("id"):
                    d = it.get("duration") or {}
                    secs = (d.get("secs") or 0) + (d.get("nanos") or 0) / 1e9
                    c = it.get("command") or []
                    if isinstance(c, list) and len(c) >= 3 and c[-2] in ("-c", "-lc"):
                        c = c[-1]  # ["/bin/zsh", "-lc", "<command>"]
                    cat = classify("Bash", {"command": c}, categories)
                    cmd_seconds[cat] = cmd_seconds.get(cat, 0) + secs
        if meta is None:
            continue
        src = meta.get("thread_source")
        role = "orchestrator" if src in (None, "user") else ("reviewer" if src == "guardian_review" else "worker")
        agent = meta.get("agent_role") or role
        total_calls = sum(1 for e in events if e[0] == "call")
        seen = 0
        for e in events:
            if e[0] == "call":
                seen += 1
                continue
            _, cat, tok, label, name, sample = e
            after = total_calls - seen
            out.append({"harness": "codex", "session": root_id, "project": project, "agent_id": meta.get("id"),
                        "role": role, "agent": agent, "category": cat, "label": label, "tool": name,
                        "sample": sample, "tokens": tok, "calls_after": after, "carried": tok * after,
                        "carried_usd": None})
    return out, cmd_seconds


def codex_breakdown(root: str, since=None, until=None, categories=None, session_filter=None) -> dict:
    """Codex only, as the CLI and rule R3 use it: `outputs` holds
    (session, agent, label, tokens, calls_after) tuples."""
    outs, secs = codex_outputs(root, since, until, categories, session_filter)
    b = summarise(outs, secs)
    b["outputs"] = [(o["session"], o["agent"], o["label"], o["tokens"], o["calls_after"]) for o in outs]
    return b


# ---------------------------------------------------------------- Claude Code

def claude_outputs(root: str, prices: Prices | None = None, since=None, until=None, categories=None) -> tuple[list, dict]:
    """Same records for ~/.claude/projects. Each agent (main conversation or a
    subagent) is its own thread; its model calls are its assistant responses.
    Run time is the gap between a tool call and its result, capped at 10 min."""
    prices = prices or Prices({})
    out: list = []
    seconds: dict = {}
    seen_msgs: set = set()
    for path in files_touched_since(root, since):
        base = os.path.basename(path)
        in_sub = os.sep + "subagents" + os.sep in path and base.startswith("agent-")
        file_agent = base[len("agent-"):-len(".jsonl")] if in_sub else None
        threads: dict = {}
        for r in iter_jsonl(path):
            sid = r.get("sessionId")
            if not sid or r.get("type") not in ("assistant", "user"):
                continue
            ts = parse_ts(r.get("timestamp"))
            if not in_window(ts, since, until):
                continue
            sidechain = bool(r.get("isSidechain")) or bool(file_agent)
            key = (r.get("agentId") or file_agent or "subagent") if sidechain else "main"
            th = threads.setdefault(key, {"events": [], "calls": {}, "model": "", "session": sid,
                                          "project": os.path.basename(r.get("cwd") or "") or "?",
                                          "role": "worker" if sidechain else "orchestrator"})
            msg = r.get("message") or {}
            content = msg.get("content") if isinstance(msg.get("content"), list) else []
            if r.get("type") == "assistant":
                model = msg.get("model") or ""
                if model == "<synthetic>":
                    continue
                th["model"] = model or th["model"]
                for b in content:
                    if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("id") not in th["calls"]:
                        name, body = b.get("name") or "?", b.get("input") or {}
                        cat = classify(name, body, categories)
                        th["calls"][b.get("id")] = (cat, _label(name, body, cat), name, _sample(name, body), ts)
                mid = (msg.get("id"), r.get("requestId"))
                if msg.get("usage") and mid not in seen_msgs:
                    seen_msgs.add(mid)
                    th["events"].append(("call", th["model"]))
            else:
                for b in content:
                    if isinstance(b, dict) and b.get("type") == "tool_result":
                        cat, label, name, sample, t0 = th["calls"].get(b.get("tool_use_id"), ("other", "?", "?", "", None))
                        if t0 and ts:
                            seconds[cat] = seconds.get(cat, 0) + min(max((ts - t0).total_seconds(), 0), 600)
                        th["events"].append(("out", cat, len(_text(b.get("content"))) // 4, label, name, sample))
        for key, th in threads.items():
            n_calls = sum(1 for e in th["events"] if e[0] == "call")
            # cache-read price of every later call, summed from the end
            price_after, acc = [], 0.0
            for e in reversed(th["events"]):
                if e[0] == "call":
                    acc += (prices.lookup(e[1]) or {}).get("cache_read", 0) if e[1] else 0
                price_after.append(acc)
            price_after.reverse()
            seen = 0
            for i, e in enumerate(th["events"]):
                if e[0] == "call":
                    seen += 1
                    continue
                _, cat, tok, label, name, sample = e
                after = n_calls - seen
                out.append({"harness": "claude-code", "session": th["session"], "project": th["project"],
                            "agent_id": key, "role": th["role"], "agent": th["role"], "category": cat, "label": label,
                            "tool": name, "sample": sample, "tokens": tok, "calls_after": after, "carried": tok * after,
                            "carried_usd": round(tok * price_after[i] / 1_000_000, 6)})
    return out, seconds


# -------------------------------------------------------------------- Copilot

def copilot_outputs(root: str, since=None, until=None, categories=None) -> tuple[list, dict]:
    """Same records for ~/.copilot/session-state. Each agent (orchestrator, or a
    subagent by agentId) is its own thread; its model calls are its
    session.usage_record lines. Run time is tool start to tool complete."""
    out: list = []
    seconds: dict = {}
    for path in copilot_files(root, since):
        folder = os.path.dirname(path)
        sid, project = os.path.basename(folder), None
        threads: dict = {}
        names: dict = {}
        for r in iter_jsonl(path):
            t, d, aid = r.get("type") or "", r.get("data") or {}, r.get("agentId")
            if project is None:
                project = copilot_project(folder, r)
                if t == "session.start":
                    sid = d.get("sessionId") or sid
            if t == "subagent.started" and aid:
                names[aid] = d.get("agentName") or "subagent"
            ts = parse_ts(r.get("timestamp"))
            if not in_window(ts, since, until):
                continue
            th = threads.setdefault(aid or "main", {"events": [], "calls": {}})
            if t == "session.usage_record":
                th["events"].append(("call",))
            elif t == "tool.execution_start":
                name, body = d.get("toolName") or "?", d.get("arguments") or {}
                cat = classify(name, body, categories)
                th["calls"][d.get("toolCallId")] = (cat, _label(name, body, cat), name, _sample(name, body), ts)
            elif t == "tool.execution_complete":
                cat, label, name, sample, t0 = th["calls"].get(d.get("toolCallId"), ("other", "?", "?", "", None))
                if t0 and ts:
                    seconds[cat] = seconds.get(cat, 0) + min(max((ts - t0).total_seconds(), 0), 600)
                res = d.get("result")
                text = _text(res.get("content") if isinstance(res, dict) else res) or _text(d.get("error"))
                th["events"].append(("out", cat, len(text) // 4, label, name, sample))
        for key, th in threads.items():
            n_calls = sum(1 for e in th["events"] if e[0] == "call")
            role = "orchestrator" if key == "main" else "worker"
            seen = 0
            for e in th["events"]:
                if e[0] == "call":
                    seen += 1
                    continue
                _, cat, tok, label, name, sample = e
                after = n_calls - seen
                out.append({"harness": "copilot", "session": sid, "project": project or "?", "agent_id": key,
                            "role": role, "agent": names.get(key, role), "category": cat, "label": label,
                            "tool": name, "sample": sample, "tokens": tok, "calls_after": after,
                            "carried": tok * after, "carried_usd": None})
    return out, seconds


# ------------------------------------------------------------------ summary

def summarise(outs: list, seconds: dict | None = None) -> dict:
    """Group output records by category, with drill-downs: by role, by label
    (command, script or file), by session, and the single largest outputs."""
    seconds = seconds or {}
    cats: dict = {}
    by_session: dict = {}
    for o in outs:
        c = cats.setdefault(o["category"], {"tool_calls": 0, "output_tokens": 0, "carried_tokens": 0, "carried_usd": None,
                                            "by_role": {}, "labels": {}, "sessions": {}, "largest": []})
        c["tool_calls"] += 1
        c["output_tokens"] += o["tokens"]
        c["carried_tokens"] += o["carried"]
        if o["carried_usd"] is not None:
            c["carried_usd"] = (c["carried_usd"] or 0) + o["carried_usd"]
        c["by_role"][o["role"]] = c["by_role"].get(o["role"], 0) + o["carried"]
        lb = c["labels"].setdefault(o["label"], {"label": o["label"], "calls": 0, "output_tokens": 0, "carried_tokens": 0,
                                                 "max_output": 0, "carried_usd": None, "sample": o["sample"]})
        lb["calls"] += 1
        lb["output_tokens"] += o["tokens"]
        lb["carried_tokens"] += o["carried"]
        lb["max_output"] = max(lb["max_output"], o["tokens"])
        if o["carried_usd"] is not None:
            lb["carried_usd"] = (lb["carried_usd"] or 0) + o["carried_usd"]
        ss = c["sessions"].setdefault(o["session"], {"session": o["session"], "project": o["project"], "calls": 0,
                                                     "carried_tokens": 0})
        ss["calls"] += 1
        ss["carried_tokens"] += o["carried"]
        c["largest"].append(o)
        per = by_session.setdefault(o["session"], {})
        per[o["category"]] = per.get(o["category"], 0) + o["carried"]
    for c in cats.values():
        c["labels"] = sorted(c["labels"].values(), key=lambda x: -x["carried_tokens"])[:TOP_N]
        c["sessions"] = sorted(c["sessions"].values(), key=lambda x: -x["carried_tokens"])[:TOP_N]
        c["largest"] = [{k: o[k] for k in ("session", "project", "agent", "label", "sample", "tokens", "calls_after",
                                           "carried", "carried_usd")}
                        for o in sorted(c["largest"], key=lambda o: -o["carried"])[:TOP_N]]
        if c["carried_usd"] is not None:
            c["carried_usd"] = round(c["carried_usd"], 4)
    return {"by_category": dict(sorted(cats.items(), key=lambda kv: -kv[1]["carried_tokens"])),
            "command_seconds": {k: round(v) for k, v in sorted(seconds.items(), key=lambda kv: -kv[1])},
            "by_session": by_session}
