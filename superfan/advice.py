"""What could one session have done cheaper? Rule-based suggestions from the
session summary and its tool outputs, plus an optional question to a local
model CLI: `claude -p` with every tool turned off, or `codex exec` in a
read-only sandbox in an empty folder (ChatGPT, through your Codex login).

Each suggestion: {title, evidence, why, try, tokens} where `tokens` is a rough
size of what is at stake, used only to order them.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from collections import Counter

ROLE = {"orchestrator": "orchestrator", "worker": "subagent", "reviewer": "approval reviewer"}


def _t(n: float) -> str:
    n = int(n)
    return f"{n / 1e6:.1f}M" if n >= 1e6 else f"{n / 1e3:.0f}k" if n >= 1e3 else str(n)


def advise(s: dict, outputs: list, orchestrator_limit: float = 0.40, output_limit: int = 8000) -> list:
    tips = []
    tok = s["tokens"]
    total = tok["total"] or 1
    agents = s["agents"]
    orch = [a for a in agents if a["role"] == "orchestrator"]
    main = orch[0] if orch else None
    claude = s["harness"] == "claude-code"

    # 1. The orchestrator doing the work itself
    if s["subagents"] and s["orchestration_share"] > orchestrator_limit and main:
        tips.append({
            "title": "The orchestrator is doing work its subagents could do",
            "evidence": f"Orchestrator and approval reviewers took {s['orchestration_share']:.0%} of {_t(total)} tokens "
                        f"over {main['model_calls']} calls; its largest prompt was {_t(main['max_context'])} tokens.",
            "why": "Each orchestrator call re-sends its whole conversation, which only grows. Work done there is paid "
                   "for again on every later coordination step.",
            "try": "Send reading, building and testing to subagents and have them return a short summary. Keep the "
                   "orchestrator to planning, spawning and checking results.",
            "tokens": sum(a["tokens"]["total"] for a in agents if a["role"] != "worker") - total * orchestrator_limit,
        })

    # 2. One long thread with no subagents
    if not s["subagents"] and main and main["model_calls"] >= 150 and main["max_context"] >= 120_000:
        tips.append({
            "title": "One long conversation carried everything",
            "evidence": f"{main['model_calls']} calls in a single thread, with prompts up to {_t(main['max_context'])} tokens.",
            "why": "Late calls pay for everything earlier in the thread, even when it no longer matters.",
            "try": "Start a fresh session per feature or milestone, compact earlier, or hand self-contained "
                   "pieces (exploration, test runs) to a subagent.",
            "tokens": tok["cache_read"] * 0.3,
        })

    # 3. Large tool outputs
    big = sorted([o for o in outputs if o["tokens"] >= output_limit], key=lambda o: -o["carried"])
    if big:
        top = big[:3]
        tips.append({
            "title": f"{len(big)} tool output{'s' if len(big) > 1 else ''} over {_t(output_limit)} tokens",
            "evidence": "; ".join(f"{o['label']} printed ~{_t(o['tokens'])} and was re-read by {o['calls_after']} "
                                  f"later calls ({ROLE.get(o['role'], o['role'])})" for o in top) + ".",
            "why": "A big output stays in the prompt for every call after it.",
            "try": "Print summaries instead of full logs (tail, grep for failures, a quiet mode in the script), and "
                   "read file ranges rather than whole files.",
            "tokens": sum(o["carried"] for o in big),
        })

    # 4. The same thing read again and again
    reads = Counter(o["label"] for o in outputs if o["category"] in ("reading files", "skills and agent docs"))
    again = [(k, n) for k, n in reads.most_common(5) if n >= 3 and k not in ("Read", "Grep", "Glob", "ls", "rg")]
    if again:
        cost = sum(o["carried"] for o in outputs if o["label"] in dict(again))
        tips.append({
            "title": "The same files were read several times",
            "evidence": ", ".join(f"{k} ×{n}" for k, n in again) + f"; together ~{_t(cost)} tokens carried.",
            "why": "Every re-read adds a fresh copy to the conversation; the earlier copies are still there.",
            "try": "Read once and keep notes, read only the section you need, or let one subagent read and summarise.",
            "tokens": cost * 0.5,
        })

    # 5. Instructions and skills
    docs = [o for o in outputs if o["category"] == "skills and agent docs"]
    docs_carried = sum(o["carried"] for o in docs)
    if docs and docs_carried >= 0.08 * total:
        top = Counter(o["label"] for o in docs).most_common(3)
        tips.append({
            "title": "Instructions and skill files take a large share",
            "evidence": f"{len(docs)} reads of skill or agent docs carried ~{_t(docs_carried)} tokens "
                        f"({docs_carried / total:.0%} of the session): " + ", ".join(f"{k} ×{n}" for k, n in top) + ".",
            "why": "Long instruction files are loaded by every agent that needs them and stay for the whole thread.",
            "try": "Trim the files, split them so agents load only the section they need, or pass subagents a short "
                   "brief instead of the full protocol.",
            "tokens": docs_carried * 0.5,
        })

    # 6. Polling
    polls = [o for o in outputs if o["category"] == "polling running commands"
             or o["label"] in ("wait", "wait_agent", "TaskOutput", "BashOutput")]
    if len(polls) >= 20:
        who = Counter(ROLE.get(o["role"], o["role"]) for o in polls).most_common(1)[0][0]
        tips.append({
            "title": "Lots of polling",
            "evidence": f"{len(polls)} calls just checked on a running command or agent, mostly by the {who}.",
            "why": "Each check is a full model call that re-sends the whole conversation, even when nothing changed.",
            "try": "Wait with a longer timeout, or block on the command until it finishes instead of checking in.",
            "tokens": len(polls) * (main["max_context"] if main else 0) * 0.5,
        })

    # 7. Cache behaviour
    prompt = tok["input"] + tok["cache_read"] + tok["cache_write"]
    if prompt and tok["cache_read"] / prompt < 0.7:
        tips.append({
            "title": "Low cache reuse",
            "evidence": f"Only {tok['cache_read'] / prompt:.0%} of prompt tokens came from cache "
                        f"({_t(tok['input'])} fresh, {_t(tok['cache_write'])} written).",
            "why": "Uncached input costs several times more than a cache read.",
            "try": "Avoid changing early context (system prompt, tool list, first messages) mid-session, and keep "
                   "pauses shorter than the cache lifetime.",
            "tokens": tok["input"],
        })
    if claude and s.get("cost_parts") and s["cost_usd"]:
        cw = s["cost_parts"]["cache_write"]
        if cw / s["cost_usd"] >= 0.35:
            tips.append({
                "title": "Cache writes are a big part of the bill",
                "evidence": f"${cw:.2f} of ${s['cost_usd']:.2f} went to writing the cache "
                            f"({_t(tok['cache_write'])} tokens).",
                "why": "The cache is rewritten when it expires (long tool runs, breaks) or when early context changes.",
                "try": "Keep pauses under the cache lifetime, or use the 1-hour cache for sessions with long builds.",
                "tokens": tok["cache_write"],
            })

    # 8. Model choice for subagents (Claude Code)
    if claude:
        heavy = [a for a in agents if a["role"] == "worker" and (a["cost_usd"] or 0) >= 0.5
                 and any(k in m for m in a["models"] for k in ("opus", "fable"))]
        if heavy:
            spend = sum(a["cost_usd"] for a in heavy)
            tips.append({
                "title": "Subagents ran on the most expensive models",
                "evidence": f"{len(heavy)} subagent{'s' if len(heavy) > 1 else ''} on "
                            f"{', '.join(sorted({m for a in heavy for m in a['models']}))} cost ${spend:.2f}.",
                "why": "Exploring, reading and running tests rarely needs the top model.",
                "try": "Set a cheaper model (Sonnet or Haiku) on explorer and test-runner agents, and keep the top "
                       "model for planning and hard edits.",
                "tokens": sum(a["tokens"]["total"] for a in heavy) * 0.5,
            })

    # 9. Output-heavy
    if tok["output"] / total >= 0.08 and tok["output"] >= 200_000:
        tips.append({
            "title": "A lot of generated text",
            "evidence": f"{_t(tok['output'])} output tokens, {tok['output'] / total:.0%} of the session"
                        + (" (Codex counts reasoning here)." if s["harness"] == "codex" else "."),
            "why": "Output is the most expensive kind of token.",
            "try": "Lower the reasoning effort for routine steps and ask for shorter reports between agents.",
            "tokens": tok["output"] * 4,
        })

    # 10. Context compaction (Copilot logs what each compaction used)
    comp = s.get("compaction_tokens") or 0
    if comp and comp / total >= 0.10:
        worst = max(agents, key=lambda a: a.get("compaction_tokens") or 0)
        who = worst["label"] or ROLE.get(worst["role"], worst["role"])
        tips.append({
            "title": "Context compaction is eating tokens",
            "evidence": f"{s['compactions']} compaction{'s' if s['compactions'] != 1 else ''} used {_t(comp)} tokens ({comp / total:.0%} of the session); "
                        f"{who} ran {worst['compactions']} of them for {_t(worst['compaction_tokens'])}.",
            "why": "Each compaction re-reads the whole context to summarise it. When the prompt is mostly fixed "
                   "context (tool definitions, instructions), it frees little and runs again a few calls later.",
            "try": "Shrink the fixed context first (fewer tools for that agent, shorter instructions), give the agent "
                   "a smaller task, or start a fresh agent per feature instead of compacting one long one.",
            "tokens": comp,
        })

    # 11. Tool definitions taking the prompt (Copilot reports their size)
    heavy_tools = [a for a in agents if (a.get("tool_def_tokens") or 0) >= 20_000 and a["max_context"]
                   and a["tool_def_tokens"] / a["max_context"] >= 0.25]
    if heavy_tools:
        w = max(heavy_tools, key=lambda a: a["tool_def_tokens"] * a["model_calls"])
        least = min((a["tool_def_tokens"] for a in agents if a.get("tool_def_tokens")), default=0)
        tips.append({
            "title": "Tool definitions fill the prompt",
            "evidence": f"{w['label'] or ROLE.get(w['role'], w['role'])} carried {_t(w['tool_def_tokens'])} tokens of tool "
                        f"definitions on each of {w['model_calls']} calls (largest prompt {_t(w['max_context'])})"
                        + (f"; the lightest agent here carried {_t(least)}." if least < w["tool_def_tokens"] else "."),
            "why": "Tool definitions are re-sent on every call and leave less room for the work, which brings "
                   "compaction sooner.",
            "try": "Give the agent an explicit tool allowlist (only the tools it uses), or turn off MCP servers it "
                   "doesn't need.",
            "tokens": (w["tool_def_tokens"] - least) * w["model_calls"],
        })

    # 12. Approval reviewers
    rev = sum(a["tokens"]["total"] for a in agents if a["role"] == "reviewer")
    if rev / total >= 0.10:
        tips.append({
            "title": "Approval reviews are costly",
            "evidence": f"Approval reviewers used {_t(rev)} tokens ({rev / total:.0%}).",
            "why": "Each approval review re-reads context to judge one command.",
            "try": "Pre-approve the safe commands the kit runs all the time (its scripts, git status, reads).",
            "tokens": rev,
        })

    tips.sort(key=lambda t: -t["tokens"])
    for t in tips:
        t["tokens"] = int(max(t["tokens"], 0))
    return tips


def session_detail(s: dict, outputs: list, orchestrator_limit: float, output_limit: int) -> dict:
    cats: dict = {}
    for o in outputs:
        c = cats.setdefault(o["category"], {"calls": 0, "carried_tokens": 0, "output_tokens": 0})
        c["calls"] += 1
        c["carried_tokens"] += o["carried"]
        c["output_tokens"] += o["tokens"]
    top = sorted(outputs, key=lambda o: -o["carried"])[:15]
    return {
        "categories": dict(sorted(cats.items(), key=lambda kv: -kv[1]["carried_tokens"])),
        "largest": [{k: o[k] for k in ("agent", "role", "category", "label", "sample", "tokens", "calls_after", "carried",
                                       "carried_usd")} for o in top],
        "advice": advise(s, outputs, orchestrator_limit, output_limit),
    }


def ask_prompt(s: dict, detail: dict, question: str) -> str:
    """Numbers and labels only: no transcript, no prompts, no file contents."""
    brief = {k: s[k] for k in ("harness", "project", "models", "tokens", "cost_usd", "duration_s", "active_s",
                               "model_calls", "tool_calls", "tools", "orchestration_share", "subagents")}
    brief["agents"] = [{k: a[k] for k in ("role", "label", "models", "tokens", "share", "cost_usd", "model_calls",
                                          "tool_calls", "max_context")} for a in s["agents"]]
    if s.get("compaction_tokens") or any(a.get("tool_def_tokens") for a in s["agents"]):
        brief["compaction_tokens"] = s.get("compaction_tokens") or 0
        for row, a in zip(brief["agents"], s["agents"]):
            row.update({k: a.get(k) or 0 for k in ("compactions", "compaction_tokens", "tool_def_tokens")})
    brief["where_tokens_went"] = detail["categories"]
    brief["largest_outputs"] = [{k: o[k] for k in ("agent", "category", "label", "tokens", "calls_after", "carried")}
                                for o in detail["largest"]]
    brief["rule_based_suggestions"] = [{k: t[k] for k in ("title", "evidence")} for t in detail["advice"]]
    return (
        "You review one coding-agent session for cost and efficiency. Below is a JSON summary of it: token counts "
        "per agent (orchestrator, subagents, approval reviewers), where tool output tokens went (\"carried\" = "
        "output size x later model calls that re-read it), and rule-based suggestions already shown to the user.\n"
        "Do not run commands or read files: answer the user's question using only these numbers. Be concrete: name the agent, the command or file, "
        "and the tokens involved. Add what the rule-based suggestions missed rather than repeating them. Use short "
        "Markdown: a few bullets, most valuable first. Codex and Copilot sessions have no dollar prices; do not invent any.\n\n"
        f"Question: {question}\n\nSession:\n```json\n{json.dumps(brief, indent=1, default=str)}\n```\n"
    )


ASK_DIR_PREFIX = "superfan-ask-"  # the parser skips Codex sessions started here


def overview_prompt(result: dict, per_session: dict, harness: str, question: str,
                    orchestrator_limit: float = 0.40, output_limit: int = 8000, top: int = 40) -> str:
    """Every session in the window (the largest `top` in detail), the breakdown
    and the rule findings. Numbers and labels only, as for one session."""
    ss = [s for s in result["sessions"] if harness in ("all", s["harness"])]
    totals: dict = {}
    for s in ss:
        t = totals.setdefault(s["harness"], {"sessions": 0, "tokens": 0, "cost_usd": None, "orchestrator_tokens": 0})
        t["sessions"] += 1
        t["tokens"] += s["tokens"]["total"]
        t["orchestrator_tokens"] += sum(a["tokens"]["total"] for a in s["agents"] if a["role"] != "worker")
        if s["cost_usd"] is not None:
            t["cost_usd"] = round((t["cost_usd"] or 0) + s["cost_usd"], 2)
    rows = []
    for s in ss[:top]:
        d = session_detail(s, per_session.get(s["session_id"], []), orchestrator_limit, output_limit)
        rows.append({
            "session": s["session_id"][:8], "harness": s["harness"], "project": s["project"], "start": s["start"],
            "models": s["models"], "tokens": s["tokens"], "cost_usd": s["cost_usd"], "active_s": s["active_s"],
            "model_calls": s["model_calls"], "orchestration_share": s["orchestration_share"], "subagents": s["subagents"],
            **({"compaction_tokens": s["compaction_tokens"]} if s.get("compaction_tokens") else {}),
            "where_tokens_went": {k: v["carried_tokens"] for k, v in list(d["categories"].items())[:5]},
            "largest_outputs": [f"{o['label']} ({o['agent']}): {o['tokens']} x {o['calls_after']}" for o in d["largest"][:3]],
            "suggestions": [t["title"] for t in d["advice"]],
        })
    breakdown = {h: {k: {"calls": v["tool_calls"], "carried_tokens": v["carried_tokens"],
                         "top": [f"{l['label']} x{l['calls']}: {l['carried_tokens']}" for l in v["labels"][:4]]}
                     for k, v in list(b["by_category"].items())[:8]}
                 for h, b in result.get("breakdown", {}).items() if harness in ("all", h)}
    findings: dict = {}
    for f in result.get("findings", []):
        findings[f["rule"]] = findings.get(f["rule"], 0) + 1
    brief = {"window": [result["since"], result["until"]], "totals_by_harness": totals,
             "sessions": rows, "sessions_not_detailed": max(len(ss) - top, 0),
             "where_tokens_went": breakdown, "health_rule_findings": findings}
    return (
        "You review a developer's coding-agent sessions (Claude Code, Codex and/or GitHub Copilot CLI) for cost and efficiency. Below is "
        "a JSON summary of a time window: totals per harness, the largest sessions with per-session numbers, where "
        "tool-output tokens went (\"carried\" = output size x later model calls that re-read it) and rule findings.\n"
        "Do not run commands or read files: answer only from these numbers. Look for patterns across sessions and "
        "projects, name the sessions, projects, commands and token counts involved, and order points by how much "
        "they would save. Use short Markdown. Codex and Copilot have no dollar prices; do not invent any.\n\n"
        f"Question: {question}\n\nData:\n```json\n{json.dumps(brief, indent=1, default=str)}\n```\n"
    )


REVIEW_QUESTION = (
    "Write this week's Harness Superfan report in Markdown with these sections:\n"
    "1. Headline: sessions, tokens per harness, Claude Code dollars, orchestration share, in three lines.\n"
    "2. Top 5 sessions by tokens, one line each on why they were expensive (agent, command or file, tokens).\n"
    "3. Health rules: what fired, with the strongest evidence for each.\n"
    "4. Three changes to make first, each with the session or command it comes from and the tokens at stake.\n"
    "5. Kit issues worth filing: for each, a title and two lines quoting numbers and short paraphrases only "
    "(no paths, prompts or project names), or say none.\n"
    "Keep it under 400 words."
)


def ask(prompt: str, model: str = "", timeout: int = 240, engine: str = "claude") -> str:
    return ask_codex(prompt, model, timeout) if engine == "codex" else ask_claude(prompt, model, timeout)


def ask_codex(prompt: str, model: str = "", timeout: int = 240) -> str:
    """Run `codex exec` (ChatGPT via your Codex login) in a read-only sandbox,
    from an empty temporary folder, without saving the session if the CLI
    supports --ephemeral. The answer is its last message."""
    exe = shutil.which("codex")
    if not exe:
        raise RuntimeError("The `codex` command is not on PATH, so the question can't be asked from here.")
    with tempfile.TemporaryDirectory(prefix=ASK_DIR_PREFIX) as d:
        out = os.path.join(d, "answer.md")
        base = [exe, "exec", "--sandbox", "read-only", "--skip-git-repo-check", "--cd", d,
                "--color", "never", "--output-last-message", out]
        if model:
            base += ["--model", model]
        r = None
        for extra in (["--ephemeral"], []):  # older CLIs don't know --ephemeral
            r = subprocess.run(base + extra + ["-"], input=prompt, capture_output=True, text=True, timeout=timeout, cwd=d)
            if r.returncode == 0 or "ephemeral" not in (r.stderr or ""):
                break
        if r.returncode != 0:
            raise RuntimeError((r.stderr or r.stdout or f"codex exited with {r.returncode}").strip()[-500:])
        if os.path.exists(out):
            with open(out) as fh:
                text = fh.read().strip()
            if text:
                return text
        return r.stdout.strip()


def ask_claude(prompt: str, model: str = "sonnet", timeout: int = 240) -> str:
    """Run `claude -p` with no tools and no saved session; returns its answer."""
    exe = shutil.which("claude")
    if not exe:
        raise RuntimeError("The `claude` command is not on PATH, so the question can't be asked from here.")
    cmd = [exe, "-p", "--tools", "", "--no-session-persistence", "--output-format", "text"]
    if model:
        cmd += ["--model", model]
    r = subprocess.run(cmd, input=prompt, capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout or f"claude exited with {r.returncode}").strip()[:500])
    return r.stdout.strip()
