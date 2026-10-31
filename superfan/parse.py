"""Read Claude Code, Codex and GitHub Copilot CLI session logs and summarise each session.

Standard library only. Nothing here knows about a particular machine: log
folders, the time window and the price table are all passed in.

A session is one top-level conversation plus every subagent it started.
Token fields are normalised the same way for every harness:

  input        fresh input tokens (not served from cache)
  cache_read   input tokens served from cache
  cache_write  input tokens written to cache (Claude Code, Copilot; Codex reports 0)
  output       output tokens (Codex: includes reasoning tokens)
  total        input + cache_read + cache_write + output

Dollars are computed for Claude Code only. Codex and Copilot stay tokens only.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Iterable

TOKEN_KEYS = ("input", "cache_read", "cache_write", "output")
IDLE_GAP_S = 600  # gaps longer than this do not count as active time


def active_seconds(stamps: list) -> int:
    stamps = sorted(stamps)
    return int(sum(min((b - a).total_seconds(), IDLE_GAP_S) if (b - a).total_seconds() <= IDLE_GAP_S else 0
                   for a, b in zip(stamps, stamps[1:])))


def parse_ts(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def iter_jsonl(path: str) -> Iterable[dict]:
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


@dataclass
class Agent:
    agent_id: str
    role: str  # orchestrator | worker | reviewer
    label: str = ""
    models: set = field(default_factory=set)
    tokens: dict = field(default_factory=lambda: {k: 0 for k in TOKEN_KEYS})
    cost_usd: float | None = None
    model_calls: int = 0
    tool_calls: int = 0
    tools: dict = field(default_factory=dict)
    first: datetime | None = None
    last: datetime | None = None
    max_context: int = 0  # largest single-call prompt, a proxy for context size
    stamps: list = field(default_factory=list)
    by_model: dict = field(default_factory=dict)  # model -> {"calls", "tokens", "cost_usd"}
    cost_parts: dict = field(default_factory=lambda: {k: 0.0 for k in TOKEN_KEYS})
    compactions: int = 0       # Copilot: context compactions this agent ran
    compaction_tokens: int = 0  # Copilot: tokens those compactions used (already in `tokens`)
    tool_def_tokens: int = 0    # Copilot: largest tool-definition block in its prompt

    def add_usage(self, model: str, tokens: int, parts: dict | None = None) -> None:
        m = self.by_model.setdefault(model or "?", {"calls": 0, "tokens": 0, "cost_usd": None})
        m["calls"] += 1
        m["tokens"] += tokens
        if parts is not None:
            m["cost_usd"] = (m["cost_usd"] or 0) + sum(parts.values())
            for k, v in parts.items():
                self.cost_parts[k] += v

    def see(self, ts: datetime | None) -> None:
        if ts is None:
            return
        self.stamps.append(ts)
        if self.first is None or ts < self.first:
            self.first = ts
        if self.last is None or ts > self.last:
            self.last = ts

    def add_tool(self, name: str) -> None:
        self.tool_calls += 1
        self.tools[name] = self.tools.get(name, 0) + 1


@dataclass
class Session:
    harness: str
    session_id: str
    project: str
    agents: dict = field(default_factory=dict)

    def agent(self, agent_id: str, role: str, label: str = "") -> Agent:
        a = self.agents.get(agent_id)
        if a is None:
            a = self.agents[agent_id] = Agent(agent_id, role, label)
        elif label and not a.label:
            a.label = label
        return a

    def summary(self) -> dict:
        agents = [a for a in self.agents.values() if a.model_calls or a.tool_calls]
        tokens = {k: sum(a.tokens[k] for a in agents) for k in TOKEN_KEYS}
        tokens["total"] = sum(tokens.values())
        firsts = [a.first for a in agents if a.first]
        lasts = [a.last for a in agents if a.last]
        start = min(firsts) if firsts else None
        end = max(lasts) if lasts else None
        priced = self.harness == "claude-code"
        cost = round(sum(a.cost_usd or 0 for a in agents), 4) if priced else None
        tools: dict = {}
        by_model: dict = {}
        parts = {k: 0.0 for k in TOKEN_KEYS}
        for a in agents:
            for k, v in a.tools.items():
                tools[k] = tools.get(k, 0) + v
            for m, v in a.by_model.items():
                bm = by_model.setdefault(m, {"calls": 0, "tokens": 0, "cost_usd": None})
                bm["calls"] += v["calls"]
                bm["tokens"] += v["tokens"]
                if priced and v["cost_usd"] is not None:
                    bm["cost_usd"] = round((bm["cost_usd"] or 0) + v["cost_usd"], 4)
            for k in TOKEN_KEYS:
                parts[k] += a.cost_parts[k]
        rows = []
        for a in sorted(agents, key=lambda a: (a.role != "orchestrator", -sum(a.tokens.values()))):
            t = dict(a.tokens)
            t["total"] = sum(a.tokens.values())
            rows.append({
                "agent_id": a.agent_id,
                "role": a.role,
                "label": a.label,
                "models": sorted(a.models),
                "tokens": t,
                "share": round(t["total"] / tokens["total"], 4) if tokens["total"] else 0,
                "cost_usd": round(a.cost_usd, 4) if priced and a.cost_usd is not None else None,
                "model_calls": a.model_calls,
                "tool_calls": a.tool_calls,
                "max_context": a.max_context,
                "duration_s": int((a.last - a.first).total_seconds()) if a.first and a.last else 0,
                "active_s": active_seconds(a.stamps),
                "compactions": a.compactions,
                "compaction_tokens": a.compaction_tokens,
                "tool_def_tokens": a.tool_def_tokens,
            })
        orch = sum(r["tokens"]["total"] for r in rows if r["role"] != "worker")
        return {
            "harness": self.harness,
            "session_id": self.session_id,
            "project": self.project,
            "start": start.isoformat() if start else None,
            "end": end.isoformat() if end else None,
            "duration_s": int((end - start).total_seconds()) if start and end else 0,
            "active_s": active_seconds([t for a in agents for t in a.stamps]),
            "models": sorted({m for a in agents for m in a.models}),
            "tokens": tokens,
            "cost_usd": cost,
            "model_calls": sum(a.model_calls for a in agents),
            "tool_calls": sum(a.tool_calls for a in agents),
            "tools": dict(sorted(tools.items(), key=lambda kv: -kv[1])),
            "by_model": dict(sorted(by_model.items(), key=lambda kv: -kv[1]["tokens"])),
            "cost_parts": {k: round(v, 4) for k, v in parts.items()} if priced else None,
            "orchestration_share": round(orch / tokens["total"], 4) if tokens["total"] else 0,
            "subagents": sum(1 for r in rows if r["role"] == "worker"),
            "compactions": sum(a.compactions for a in agents),
            "compaction_tokens": sum(a.compaction_tokens for a in agents),
            "agents": rows,
        }


class Prices:
    def __init__(self, table: dict):
        self.models = table.get("models", {})
        self.mult = table.get("cache_write_multiplier", {"5m": 1.25, "1h": 2.0})
        self.unknown: set = set()

    def lookup(self, model: str) -> dict | None:
        if model in self.models:
            return self.models[model]
        # tolerate date or context suffixes such as claude-opus-5-5[1m]
        for name in sorted(self.models, key=len, reverse=True):
            if model.startswith(name):
                return self.models[name]
        self.unknown.add(model)
        return None

    def parts(self, model: str, fresh: int, read: int, write_5m: int, write_1h: int, out: int) -> dict:
        """Dollars per token kind for one call."""
        p = self.lookup(model)
        if p is None:
            return {k: 0.0 for k in TOKEN_KEYS}
        i = p["input"]
        return {"input": fresh * i / 1e6, "cache_read": read * p["cache_read"] / 1e6,
                "cache_write": (write_5m * self.mult["5m"] + write_1h * self.mult["1h"]) * i / 1e6,
                "output": out * p["output"] / 1e6}

    def cost(self, model: str, fresh: int, read: int, write_5m: int, write_1h: int, out: int) -> float:
        return sum(self.parts(model, fresh, read, write_5m, write_1h, out).values())


def in_window(ts: datetime | None, since: datetime | None, until: datetime | None) -> bool:
    if ts is None:
        return False
    if since and ts < since:
        return False
    if until and ts >= until:
        return False
    return True


def files_touched_since(root: str, since: datetime | None, until: datetime | None = None) -> list[str]:
    """Log files written to at or after `since`. `until` is accepted for symmetry but not used:
    a file's creation time says nothing about its events when logs were copied or restored."""
    out = []
    for dirpath, _dirs, names in os.walk(root):
        for n in names:
            if not n.endswith(".jsonl"):
                continue
            p = os.path.join(dirpath, n)
            if since is None or datetime.fromtimestamp(os.path.getmtime(p), timezone.utc) >= since:
                out.append(p)
    return sorted(out)


# ---------------------------------------------------------------- Claude Code

def scan_claude(root: str, prices: Prices, since=None, until=None) -> dict[str, Session]:
    """~/.claude/projects/<project>/<session>.jsonl, plus subagent transcripts in
    <project>/<session>/subagents/agent-<id>.jsonl (newer versions) or inline
    lines with isSidechain=true (older versions)."""
    sessions: dict[str, Session] = {}
    seen_msgs: set = set()
    seen_tools: set = set()
    labels: dict[str, str] = {}  # agentId -> subagent type, from the parent's tool results

    for path in files_touched_since(root, since, until):
        file_agent = None
        base = os.path.basename(path)
        if os.sep + "subagents" + os.sep in path and base.startswith("agent-"):
            file_agent = base[len("agent-"):-len(".jsonl")]
            meta = path[:-len(".jsonl")] + ".meta.json"
            if os.path.exists(meta):
                try:
                    with open(meta) as fh:
                        m = json.load(fh)
                    labels.setdefault(file_agent, m.get("agentType") or m.get("subagent_type") or "")
                except (OSError, ValueError):
                    pass
        for r in iter_jsonl(path):
            sid = r.get("sessionId")
            if not sid:
                continue
            # parent side: remember which subagent type each agentId was
            tur = r.get("toolUseResult")
            if isinstance(tur, dict) and tur.get("agentId"):
                labels.setdefault(tur["agentId"], tur.get("agentType") or tur.get("subagent_type") or "")
            if r.get("type") != "assistant":
                continue
            ts = parse_ts(r.get("timestamp"))
            if not in_window(ts, since, until):
                continue
            msg = r.get("message") or {}
            model = msg.get("model") or ""
            if model == "<synthetic>":
                continue
            sidechain = bool(r.get("isSidechain"))
            agent_id = r.get("agentId") or file_agent
            if sidechain or file_agent:
                key, role = agent_id or "subagent", "worker"
            else:
                key, role = "main", "orchestrator"
            s = sessions.get(sid)
            if s is None:
                s = sessions[sid] = Session("claude-code", sid, os.path.basename(r.get("cwd") or "") or "?")
            a = s.agent(key, role)
            a.see(ts)
            for block in msg.get("content") or []:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    tid = block.get("id")
                    if tid in seen_tools:
                        continue
                    seen_tools.add(tid)
                    a.add_tool(block.get("name") or "?")
            mid = (msg.get("id"), r.get("requestId"))
            usage = msg.get("usage")
            if not usage or mid in seen_msgs:
                continue  # one API response is split over several lines with the same usage
            seen_msgs.add(mid)
            fresh = usage.get("input_tokens") or 0
            read = usage.get("cache_read_input_tokens") or 0
            write = usage.get("cache_creation_input_tokens") or 0
            cc = usage.get("cache_creation") or {}
            w1h = cc.get("ephemeral_1h_input_tokens") or 0
            w5m = write - w1h
            out = usage.get("output_tokens") or 0
            a.models.add(model)
            a.model_calls += 1
            a.tokens["input"] += fresh
            a.tokens["cache_read"] += read
            a.tokens["cache_write"] += write
            a.tokens["output"] += out
            a.max_context = max(a.max_context, fresh + read + write)
            parts = prices.parts(model, fresh, read, w5m, w1h, out)
            a.cost_usd = (a.cost_usd or 0) + sum(parts.values())
            a.add_usage(model, fresh + read + write + out, parts)

    for s in sessions.values():
        for a in s.agents.values():
            if a.role == "worker" and not a.label:
                a.label = labels.get(a.agent_id, "")
    return sessions


# ---------------------------------------------------------------------- Codex

def _codex_role(meta: dict) -> tuple[str, str]:
    src = meta.get("thread_source") or ""
    if src == "subagent":
        return "worker", meta.get("agent_role") or meta.get("agent_nickname") or "subagent"
    if src == "guardian_review" or (isinstance(meta.get("source"), dict) and "guardian" in json.dumps(meta["source"])):
        return "reviewer", "guardian_review"
    if meta.get("parent_thread_id"):
        return "worker", meta.get("agent_role") or src or "subagent"
    return "orchestrator", ""


def scan_codex(root: str, since=None, until=None) -> dict[str, Session]:
    """~/.codex/sessions/YYYY/MM/DD/rollout-<time>-<thread id>.jsonl, one file per
    thread. A subagent file starts with a copy of its parent's history; only
    lines from subagent_history_start_ordinal on belong to the subagent."""
    sessions: dict[str, Session] = {}
    for path in files_touched_since(root, since, until):
        meta = None
        start_ord = 0
        thread_id = None
        a = None
        last_total = None  # fallback when token_usage_record is absent
        got_records = False
        model = ""
        for r in iter_jsonl(path):
            t = r.get("type")
            p = r.get("payload") or {}
            if meta is None:
                if t != "session_meta":
                    break
                meta = p
                thread_id = p.get("id")
                start_ord = p.get("subagent_history_start_ordinal") or 0
                root_id = p.get("session_id") or p.get("parent_thread_id") or thread_id
                role, label = _codex_role(p)
                cwd = (p.get("runtime_workspace_roots") or [p.get("cwd") or ""])[0]
                if os.path.basename(cwd.rstrip("/")).startswith("superfan-ask-"):
                    break  # a question the Superfan page itself asked; not your work
                s = sessions.get(root_id)
                if s is None:
                    s = sessions[root_id] = Session("codex", root_id, os.path.basename(cwd.rstrip("/")) or "?")
                a = s.agent(thread_id, role, label)
                continue
            if (r.get("ordinal") or 0) < start_ord:
                continue  # inherited parent history
            ts = parse_ts(r.get("timestamp"))
            if t == "turn_context" and p.get("model"):
                model = p["model"]
            elif t == "event_msg" and p.get("type") == "thread_settings_applied":
                model = (p.get("thread_settings") or {}).get("model") or model
            elif t == "token_usage_record" and p.get("thread_id") == thread_id:
                got_records = True
                if not in_window(ts, since, until):
                    continue
                u = p.get("usage") or {}
                cached = u.get("cached_input_tokens") or 0
                a.tokens["input"] += (u.get("input_tokens") or 0) - cached
                a.tokens["cache_read"] += cached
                a.tokens["cache_write"] += u.get("cache_write_input_tokens") or 0
                a.tokens["output"] += u.get("output_tokens") or 0
                a.model_calls += 1
                a.max_context = max(a.max_context, u.get("input_tokens") or 0)
                if model:
                    a.models.add(model)
                a.add_usage(model, (u.get("input_tokens") or 0) + (u.get("cache_write_input_tokens") or 0)
                            + (u.get("output_tokens") or 0))
                a.see(ts)
            elif t == "event_msg" and p.get("type") == "token_count" and in_window(ts, since, until):
                info = p.get("info") or {}
                if info.get("total_token_usage"):
                    last_total = info
                    a.see(ts)
            elif t == "response_item" and p.get("type") in ("custom_tool_call", "function_call", "local_shell_call"):
                if in_window(ts, since, until):
                    a.add_tool(p.get("name") or p.get("type"))
                    a.see(ts)
            elif t == "event_msg" and p.get("type") == "item_completed":
                item = p.get("item") or {}
                if item.get("type") == "CommandExecution" and p.get("thread_id") == thread_id and in_window(ts, since, until):
                    # shell commands run inside an exec tool call; counted separately
                    a.tools["shell_command"] = a.tools.get("shell_command", 0) + 1
        if a is not None and not got_records and last_total:
            # older CLI: no per-call records, take the cumulative thread total
            u = last_total["total_token_usage"]
            cached = u.get("cached_input_tokens") or 0
            a.tokens["input"] += (u.get("input_tokens") or 0) - cached
            a.tokens["cache_read"] += cached
            a.tokens["output"] += u.get("output_tokens") or 0
            a.model_calls = max(a.model_calls, 1)
            if model:
                a.models.add(model)
    return sessions


# -------------------------------------------------------------------- Copilot

def copilot_workspace(folder: str) -> dict:
    """workspace.yaml next to events.jsonl: flat `key: value` lines."""
    out: dict = {}
    try:
        with open(os.path.join(folder, "workspace.yaml"), encoding="utf-8") as fh:
            for line in fh:
                k, sep, v = line.partition(":")
                if sep and not line[:1].isspace():
                    out[k.strip()] = v.strip().strip("'\"")
    except OSError:
        pass
    return out


def copilot_files(root: str, since: datetime | None, until: datetime | None = None) -> list[str]:
    """<root>/<session uuid>/events.jsonl, or root itself when it is one session folder."""
    return [p for p in files_touched_since(root, since, until) if os.path.basename(p) == "events.jsonl"]


def copilot_project(folder: str, first: dict | None) -> str:
    ws = copilot_workspace(folder)
    ctx = ((first or {}).get("data") or {}).get("context") or {}
    where = ws.get("git_root") or ws.get("cwd") or ctx.get("gitRoot") or ctx.get("cwd") or ""
    return os.path.basename(where.rstrip("/")) or "?"


def _copilot_tokens(u: dict) -> dict:
    """inputTokens is the whole prompt, cache reads and writes included."""
    total_in = u.get("inputTokens") or 0
    read = u.get("cacheReadTokens") or 0
    write = u.get("cacheWriteTokens") or 0
    return {"input": max(total_in - read - write, 0), "cache_read": read, "cache_write": write,
            "output": u.get("outputTokens") or 0}


def _copilot_snapshot(d: dict) -> dict:
    """session.shutdown totals as {agent: {"name", "models": {model: (tokens, requests)}}}.
    agentMetrics splits them by agent ("main" = orchestrator); older files only
    have modelMetrics for the whole session."""
    per_agent = d.get("agentMetrics") or {"main": {"modelMetrics": d.get("modelMetrics") or {}}}
    out: dict = {}
    for key, m in per_agent.items():
        models = {}
        for model, mm in ((m or {}).get("modelMetrics") or {}).items():
            models[model] = (_copilot_tokens((mm or {}).get("usage") or {}),
                             ((mm or {}).get("requests") or {}).get("count") or 0)
        out[key] = {"name": (m or {}).get("agentName") or "", "models": models}
    return out


def scan_copilot(root: str, since=None, until=None) -> dict[str, Session]:
    """~/.copilot/session-state/<session>/events.jsonl, one file per session.
    Lines with an agentId belong to that subagent; the rest to the orchestrator.
    Tokens come from session.usage_record, plus the tokens each context
    compaction used (session.compaction_complete), which Copilot's own
    subagent totals leave out. Older CLIs (before ~1.0.9x) write no usage
    records: their tokens come from the cumulative session.shutdown totals,
    the last one in the window minus the last one before it.
    Tokens only: AI credits are never read."""
    sessions: dict[str, Session] = {}
    for path in copilot_files(root, since, until):
        folder = os.path.dirname(path)
        sid = os.path.basename(folder)
        s = None
        names: dict = {}  # agentId -> agentName
        has_records = False
        compactions = []  # (agent, tokens, ts), added once we know where totals come from
        before, last = None, None  # shutdown snapshots before and inside the window
        for r in iter_jsonl(path):
            if s is None:
                sid = ((r.get("data") or {}).get("sessionId") or sid) if r.get("type") == "session.start" else sid
                s = sessions[sid] = Session("copilot", sid, copilot_project(folder, r))
            t = r.get("type") or ""
            d = r.get("data") or {}
            aid = r.get("agentId")
            if t == "subagent.started" and aid:
                names[aid] = d.get("agentName") or d.get("agentDisplayName") or "subagent"
            ts = parse_ts(r.get("timestamp"))
            if t == "session.usage_record":
                has_records = True
            if t == "session.shutdown":
                if ts and since and ts < since:
                    before = _copilot_snapshot(d)
                elif in_window(ts, since, until):
                    last = (ts, _copilot_snapshot(d))
                continue
            if not in_window(ts, since, until):
                continue
            a = s.agent(aid, "worker", names.get(aid, "")) if aid else s.agent("main", "orchestrator")
            if t == "session.usage_record":
                u = d.get("usage") or {}
                tk = _copilot_tokens(u)
                model = u.get("model") or ""
                for k in TOKEN_KEYS:
                    a.tokens[k] += tk[k]
                a.model_calls += 1
                a.max_context = max(a.max_context, u.get("inputTokens") or 0)
                if model:
                    a.models.add(model)
                a.add_usage(model, sum(tk.values()))
                a.see(ts)
            elif t == "session.compaction_complete":
                tk = _copilot_tokens(d.get("compactionTokensUsed") or {})
                a.compactions += 1
                compactions.append((a, tk, ts))
            elif t == "session.compaction_start":
                a.tool_def_tokens = max(a.tool_def_tokens, d.get("toolDefinitionsTokens") or 0)
                a.max_context = max(a.max_context, d.get("currentTokens") or 0) if not has_records else a.max_context
            elif t == "session.usage_checkpoint":
                st_all = d.get("promptCacheBreakState") or []
                for st in (st_all.values() if isinstance(st_all, dict) else st_all):
                    for m in ((st or {}).get("models") or {}).values():
                        a.tool_def_tokens = max(a.tool_def_tokens, (m or {}).get("tool_tokens") or 0)
            elif t == "tool.execution_start":
                a.add_tool(d.get("toolName") or "?")
                a.see(ts)
        if s is None:
            continue
        from_snapshots = not has_records and last is not None
        for a, tk, ts in compactions:
            a.compaction_tokens += sum(tk.values())
            if not from_snapshots:  # shutdown totals already hold them
                for k in TOKEN_KEYS:
                    a.tokens[k] += tk[k]
                a.add_usage("compaction", sum(tk.values()))
                a.see(ts)
        if from_snapshots:
            ts, snap = last
            for key, cur in snap.items():
                prev = (before or {}).get(key, {"models": {}})["models"]
                a = s.agent("main", "orchestrator") if key == "main" else s.agent(key, "worker", cur["name"] or names.get(key, ""))
                for model, (tk, n) in cur["models"].items():
                    ptk, pn = prev.get(model, ({k: 0 for k in TOKEN_KEYS}, 0))
                    delta = {k: max(tk[k] - ptk[k], 0) for k in TOKEN_KEYS}
                    if not any(delta.values()):
                        continue
                    for k in TOKEN_KEYS:
                        a.tokens[k] += delta[k]
                    a.model_calls += max(n - pn, 0)
                    a.models.add(model)
                    a.add_usage(model, sum(delta.values()))
                a.see(ts)
    return sessions


def scan(claude_root=None, codex_root=None, prices_table=None, since=None, until=None, copilot_root=None) -> list[dict]:
    prices = Prices(prices_table or {})
    out: list[dict] = []
    if claude_root:
        out += [s.summary() for s in scan_claude(claude_root, prices, since, until).values()]
    if codex_root:
        out += [s.summary() for s in scan_codex(codex_root, since, until).values()]
    if copilot_root:
        out += [s.summary() for s in scan_copilot(copilot_root, since, until).values()]
    out = [s for s in out if s["tokens"]["total"] > 0]
    if prices.unknown:
        for s in out:
            s.setdefault("warnings", [])
            missing = sorted(set(s["models"]) & prices.unknown)
            if missing:
                s["warnings"].append("no price for " + ", ".join(missing) + "; cost undercounted")
    return out
