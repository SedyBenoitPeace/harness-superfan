"""Drill-down for one session: which files and commands sit behind each kind
of work, which agents ran them, how often the same thing was read again, and
a concrete hint for each.

Works on the output records of breakdown.py. Paths are shortened to their last
two parts everywhere they are shown, so a screenshot of the page doesn't carry
your home folder or project layout.
"""

from __future__ import annotations

import os
import re
import shlex

from .breakdown import _first_segment

TOP_ITEMS = 12
READERS = {"cat", "head", "tail", "nl", "sed", "less", "more", "bat", "wc", "awk"}
SEARCHERS = {"rg", "grep", "ag", "ack"}
LISTERS = {"ls", "find", "tree", "du", "fd"}
READ_TOOLS = r"^(Read|view|read_file|view_image|NotebookRead)$"


def _t(n: float) -> str:
    n = int(n)  # rounds halves up, as the page does
    return f"{n / 1e6:.1f}M" if n >= 1e6 else f"{int(n / 1e3 + 0.5)}k" if n >= 1e3 else str(n)


def short_path(p: str) -> str:
    """'/Users/me/src/app/db/drive.ts' -> '…/db/drive.ts'. Relative paths with
    up to two parts are kept as they are."""
    parts = [x for x in p.strip().split("/") if x]
    if p.startswith(("/", "~")) and len(parts) > 2 or len(parts) > 3:
        return "…/" + "/".join(parts[-2:])
    return p


def short_paths(text: str) -> str:
    """Shorten every absolute or home path in a command line, quoted or not."""
    text = re.sub(r"""(['"])([~/][^'"]*)\1""", lambda m: m.group(1) + short_path(m.group(2)) + m.group(1), text)
    return re.sub(r"(?<![\w.:'\"/…])([~/][^\s'\";|&)]*)", lambda m: short_path(m.group(1)), text)


def _words(cmd: str) -> list:
    seg = _first_segment(cmd)
    try:
        return shlex.split(seg)
    except ValueError:
        return seg.split()


def _range(words: list) -> int | None:
    """Lines printed by `sed -n 'a,bp'` or `head/tail -n N`, when it says."""
    for w in words:
        m = re.fullmatch(r"(\d+),(\d+)p", w)
        if m:
            return int(m.group(2)) - int(m.group(1)) + 1
    for i, w in enumerate(words[:-1]):
        if w in ("-n", "--lines") and words[i + 1].lstrip("+-").isdigit():
            return int(words[i + 1].lstrip("+-"))
    return None


def what(o: dict) -> dict:
    """What one call touched: {key, verb, file, lines, context}. `key` groups
    calls on the same file (reads) or the same command (everything else)."""
    tool, sample = o.get("tool") or "", o.get("sample") or ""
    if re.search(READ_TOOLS, tool) and sample:
        f = short_path(sample)
        return {"key": f, "verb": "Read", "file": f, "lines": None, "context": 0}
    words = _words(sample) if sample else []
    verb = os.path.basename(words[0]) if words else ""
    if verb in READERS or verb in SEARCHERS or verb in LISTERS:
        args = [w for w in words[1:] if not w.startswith("-")]
        if verb == "sed" and args and not any(w in ("-e", "-f") for w in words):
            args = args[1:]  # the script
        if verb in SEARCHERS and args:
            args = args[1:]  # the pattern
        if verb == "awk" and args:
            args = args[1:]
        files = [a for a in args if not re.fullmatch(r"\d+", a)]
        ctx = max([int(m) for w in words for m in re.findall(r"^-[ABC](\d+)$", w)] or [0])
        if verb in READERS and files:
            f = short_path(files[0])
            return {"key": f, "verb": verb, "file": f, "lines": _range(words), "context": ctx}
        return {"key": short_paths(o.get("label") or verb), "verb": verb, "file": short_path(files[0]) if files else "",
                "lines": None, "context": ctx}
    return {"key": short_paths(o.get("label") or tool or "?"), "verb": verb or tool, "file": "", "lines": None, "context": 0}


def hint(category: str, it: dict) -> str:
    """One concrete way to make this item cheaper, or '' when nothing stands out.
    `it` has: verb, file, lines, context, calls, agents, re_reads, max_output,
    calls_after (the most later calls any copy was carried by), role."""
    verb, big, calls = it["verb"], it["max_output"], it["calls"]
    if category == "skills and agent docs":
        if calls == 1 and big < 2000:
            return ""
        who = f" by {it['agents']} agents" if it["agents"] > 1 else ""
        times = "once" if calls == 1 else f"{calls}×{who}, ~{_t(big)} each"
        return (f"Loaded {times}{f' (~{_t(big)})' if calls == 1 else ''}. Trim the file, split it so each agent loads "
                "only its section, or give subagents a short brief instead of the full file.")
    if category == "reading files":
        if it["re_reads"] >= 2 and it["file"]:
            return (f"The same agent read this file {it['re_reads'] + 1}× and every copy stays in its prompt. Read it "
                    "once and keep notes, or read only the part you still need.")
        if it["context"] >= 8:
            return (f"-A/-B/-C {it['context']} prints that many lines around every match. List matches first "
                    "(-l or -c), then open only the ones you need.")
        if verb in LISTERS and big >= 2000:
            return "Large listing. Limit the depth (-maxdepth 2), filter by name, or list one folder."
        if verb in LISTERS and calls >= 10:
            return f"Listed {calls}×. Keep a note of the layout after the first look."
        if verb == "sed" and (it["lines"] or 0) >= 150 and big >= 4000:
            return (f"Printed a {it['lines']}-line range (~{_t(big)}). grep -n for the function first, then print "
                    "~60 lines around it.")
        if verb in ("cat", "Read", "nl", "less", "more", "bat") and big >= 4000:
            return (f"Whole file printed (~{_t(big)}). grep -n for what you need, then read that range "
                    "(sed -n 'a,bp', or Read with offset and limit).")
        if it["agents"] > 1 and calls >= 3:
            return (f"{it['agents']} agents each read this. Pass the part they need in their brief instead, or have "
                    "one agent read it and share a summary.")
        if it["calls_after"] >= 50 and big >= 2000:
            return (f"Carried by {it['calls_after']} later calls. Doing this read in a subagent that returns a "
                    "summary keeps it out of this thread.")
        return ""
    if category == "kit scripts and gate":
        return ("" if big < 2000 else f"Script printed ~{_t(big)}. Have it print one pass/fail line and the failures "
                "only, with the full log in a file.")
    if category in ("package and test runners", "xcodebuild (build/test)", "swift build/test", "simulator / UI tests"):
        return ("" if big < 2000 else f"Build or test output ~{_t(big)}. Use a quiet reporter (-q, --silent, "
                "-quiet) and print only errors and failing tests, e.g. | grep -E 'error|FAIL' | tail -40.")
    if category == "git":
        return ("" if big < 2000 else "Use --stat or --name-only first, --oneline -n 20 for logs, and diff one "
                "path at a time.")
    if category == "polling running commands":
        return (f"{calls} check-ins. Block until it finishes, or wait with a longer timeout." if calls >= 5 else "")
    if category == "subagent coordination":
        if it["role"] == "orchestrator" and big >= 2000:
            return (f"Subagent reports of up to ~{_t(big)} land in the orchestrator and stay there. Ask for a short "
                    "summary (what changed, what failed, ≤200 words).")
        return f"{calls} waits or messages. Wait on several agents at once, with a longer timeout." if calls >= 10 else ""
    if category == "MCP tools":
        return ("" if big < 2000 else "Ask the tool for fewer fields or a smaller page, or turn this server off "
                "for agents that don't use it.")
    if category == "web":
        return "" if big < 2000 else "Fetch with a specific question so only the answer comes back."
    if category == "editing files":
        return ("" if big < 2000 else "This edit echoed a lot back. Prefer small targeted edits over rewriting "
                "whole files.")
    if category == "inline scripts":
        return ("" if big < 2000 else "Make the script print a summary (counts, the few rows that matter) "
                "instead of raw data.")
    return "" if big < 8000 else f"Large output (~{_t(big)}). Filter it before printing (grep, head, a summary flag)."


def drill(outputs: list) -> dict:
    """Per category: carried tokens by role, and the files or commands behind
    it, each with agents, re-reads and a hint. Largest first."""
    cats: dict = {}
    for o in outputs:
        w = what(o)
        c = cats.setdefault(o["category"], {"by_role": {}, "items": {}})
        c["by_role"][o["role"]] = c["by_role"].get(o["role"], 0) + o["carried"]
        it = c["items"].setdefault(w["key"], {"what": w["key"], "verb": w["verb"], "file": w["file"], "lines": w["lines"],
                                              "context": 0, "calls": 0, "output_tokens": 0, "max_output": 0,
                                              "carried_tokens": 0, "carried_usd": None, "calls_after": 0,
                                              "by_agent": {}, "sample": short_paths(o.get("sample") or "")})
        it["calls"] += 1
        it["output_tokens"] += o["tokens"]
        it["max_output"] = max(it["max_output"], o["tokens"])
        it["carried_tokens"] += o["carried"]
        it["calls_after"] = max(it["calls_after"], o["calls_after"])
        it["context"] = max(it["context"], w["context"])
        it["lines"] = max(it["lines"] or 0, w["lines"] or 0) or None
        if o.get("carried_usd") is not None:
            it["carried_usd"] = (it["carried_usd"] or 0) + o["carried_usd"]
        a = it["by_agent"].setdefault(o.get("agent_id") or o["agent"], {"agent": o["agent"], "role": o["role"],
                                                                         "calls": 0, "carried_tokens": 0})
        a["calls"] += 1
        a["carried_tokens"] += o["carried"]
    for cat, c in cats.items():
        items = sorted(c["items"].values(), key=lambda x: -x["carried_tokens"])
        for it in items:
            ags = sorted(it.pop("by_agent").values(), key=lambda a: -a["carried_tokens"])
            it["agents"] = ags
            it["re_reads"] = max((a["calls"] for a in ags), default=1) - 1
            it["role"] = ags[0]["role"] if ags else ""
            if it["carried_usd"] is not None:
                it["carried_usd"] = round(it["carried_usd"], 4)
            it["hint"] = hint(cat, {**it, "agents": len(ags)})
        c["items"] = items[:TOP_ITEMS]
        c["more_items"] = max(len(items) - TOP_ITEMS, 0)
    return cats


def output_hint(o: dict) -> str:
    """The hint for one tool output, as for an item read once by one agent."""
    w = what(o)
    return hint(o["category"], {**w, "calls": 1, "agents": 1, "re_reads": 0, "max_output": o["tokens"],
                                "calls_after": o["calls_after"], "role": o["role"]})
