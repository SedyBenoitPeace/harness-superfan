"""Which sessions are open right now, and a live view of one of them.

Read-only, standard library only (uses psutil if it happens to be installed).
Nothing here knows about a particular machine: log folders come from the
serve settings.

A session's state:

  working   its log was written in the last WORKING_S seconds
  waiting   no recent write, but a matching CLI process is still running
            (open and idle, usually waiting for you)
  idle      no process information on this machine; written in the last IDLE_S
  ended     an end marker (Copilot's session.shutdown as the last line), or no
            matching process, or (without process information) quiet for IDLE_S

Process check, per platform:

  psutil installed  name, command line and working folder everywhere
  Linux             /proc/<pid>/cmdline and /proc/<pid>/cwd
  macOS             `ps` for command lines, `lsof -d cwd` for working folders
  Windows           PowerShell Get-CimInstance Win32_Process: command lines and
                    parent ids, but no working folder (Windows keeps it inside
                    the process's own memory). Without a folder, the N most
                    recently written sessions of a CLI with N processes count
                    as open.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone

from . import rules, advice
from .breakdown import claude_outputs, codex_outputs, copilot_outputs
from .parse import Prices, iter_jsonl, parse_ts, scan_claude, scan_codex, scan_copilot

WORKING_S = 120
IDLE_S = 1800
LOOKBACK_H = 24          # sessions written in the last day are candidates
INDEX_TTL_S = 3.0
DETAIL_EVERY_S = 30.0    # the "where tokens go" breakdown is heavier; refresh it at most this often
PROCESS_TTL_S = 5.0
RECENT_N = 20
HARNESSES = ("claude-code", "codex", "copilot")
ROOT_KEY = {"claude-code": "claude", "codex": "codex", "copilot": "copilot"}


# ------------------------------------------------------------------ processes

_RUNTIMES = {"node", "bun", "deno"}
_PACKAGES = {"claude-code": "@anthropic-ai/claude-code", "codex": "@openai/codex", "copilot": "@github/copilot"}
_NAMES = {"claude": "claude-code", "codex": "codex", "copilot": "copilot"}
_DESKTOP = ("claude.app/contents/macos/claude", "/anthropicclaude/", "/windowsapps/claude")


def _base(tok: str, fold: bool) -> str:
    b = tok.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
    if fold:
        b = b.lower()
    for ext in (".exe", ".cmd", ".mjs", ".cjs", ".js"):
        if b.lower().endswith(ext):
            return b[:-len(ext)]
    return b


def split_command(cmd: str) -> list:
    """A Windows command line as tokens (quotes removed). Good enough to read argv[0] and argv[1]."""
    return [t.strip('"') for t in re.findall(r'"[^"]*"|\S+', cmd or "")]


def classify(argv: list, exe: str = "", windows: bool | None = None) -> str | None:
    """Which CLI a process is, or None. `argv` is the command line as tokens,
    `exe` the executable path when known. The Claude desktop app is "Claude"
    (capital C) on macOS and lives under AnthropicClaude on Windows, so it is
    not taken for Claude Code; the Claude Code it starts is."""
    windows = sys.platform == "win32" if windows is None else windows
    joined = " ".join(argv).replace("\\", "/")
    low = joined.lower()
    if "--type=" in low:  # Electron helper processes
        return None
    if any(d in low for d in _DESKTOP) or any(d in (exe or "").replace("\\", "/").lower() for d in _DESKTOP):
        return None
    for h, pkg in _PACKAGES.items():
        if pkg in low:
            return h
    first = _base(exe or (argv[0] if argv else ""), windows)
    if first in _NAMES:
        return _NAMES[first]
    if first.lower() in _RUNTIMES:
        script = next((a for a in argv[1:] if not a.startswith("-")), "")
        name = _base(script, True)
        return _NAMES.get(name)
    return None


def _norm(p: str | None) -> str | None:
    if not p:
        return None
    try:
        p = os.path.realpath(p)
    except OSError:
        pass
    return os.path.normcase(p.rstrip("/\\")) or None


def _dedupe(procs: list, parents: dict | None = None) -> list:
    """A CLI and every process it starts (npm shim -> native codex, helpers, a
    subagent run as a child process) count once: drop a process when any of its
    ancestors is a process of the same CLI. `parents` maps every pid to its
    parent pid; without it only the direct parent is checked."""
    parents = dict(parents or {})
    for p in procs:
        parents.setdefault(p["pid"], p.get("ppid"))
    mine = {(p["harness"], p["pid"]) for p in procs}
    out = []
    for p in procs:
        q, seen, nested = parents.get(p["pid"]), set(), False
        while q and q not in seen:
            if (p["harness"], q) in mine:
                nested = True
                break
            seen.add(q)
            q = parents.get(q)
        if not nested:
            out.append(p)
    return out


def _session_files(paths) -> list:
    return [x for x in paths if x and x.endswith(".jsonl")]


def _psutil_procs():
    import psutil  # optional
    out, parents = [], {}
    for pr in psutil.process_iter(["pid", "ppid", "cmdline", "exe", "create_time"]):
        info = pr.info
        parents[info["pid"]] = info.get("ppid")
        h = classify(info.get("cmdline") or [], info.get("exe") or "")
        if not h:
            continue
        try:
            cwd = pr.cwd()
        except Exception:
            cwd = None
        try:
            files = _session_files(f.path for f in pr.open_files())
        except Exception:
            files = []
        out.append({"harness": h, "pid": info["pid"], "ppid": info.get("ppid"), "cwd": cwd,
                    "start": info.get("create_time"), "files": files})
    return out, parents


def _linux_procs():
    out, parents = [], {}
    try:
        with open("/proc/stat") as fh:
            btime = next(int(x.split()[1]) for x in fh if x.startswith("btime"))
        ticks = os.sysconf("SC_CLK_TCK")
    except (OSError, StopIteration, ValueError):
        btime, ticks = None, None
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            with open(f"/proc/{pid}/stat") as fh:
                fields = fh.read().rsplit(")", 1)[1].split()
            ppid = int(fields[1])
            parents[int(pid)] = ppid
            with open(f"/proc/{pid}/cmdline", "rb") as fh:
                argv = [a.decode(errors="replace") for a in fh.read().split(b"\0") if a]
            h = classify(argv)
            if not h:
                continue
            start = btime + int(fields[19]) / ticks if btime and ticks else None
            try:
                cwd = os.readlink(f"/proc/{pid}/cwd")
            except OSError:
                cwd = None
            files = []
            try:
                for fd in os.listdir(f"/proc/{pid}/fd"):
                    try:
                        files.append(os.readlink(f"/proc/{pid}/fd/{fd}"))
                    except OSError:
                        pass
            except OSError:
                pass
            out.append({"harness": h, "pid": int(pid), "ppid": ppid, "cwd": cwd, "start": start,
                        "files": _session_files(files)})
        except (OSError, ValueError, IndexError):
            continue
    return out, parents


def _run(cmd: list, timeout: float = 5.0, env: dict | None = None) -> str:
    kw = {"creationflags": 0x08000000} if sys.platform == "win32" else {}  # CREATE_NO_WINDOW
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env, **kw).stdout


def _lstart(words: list) -> float | None:
    """`ps -o lstart` in the C locale: 'Wed Nov  5 10:00:00 2026', local time."""
    try:
        return datetime.strptime(" ".join(words), "%a %b %d %H:%M:%S %Y").timestamp()
    except ValueError:
        return None


def parse_ps(comm_out: str, args_out: str, parents: dict | None = None) -> list:
    """`ps -axo pid=,ppid=,lstart=,comm=` and `ps -axo pid=,args=` -> matched processes (no cwd
    yet). comm is the executable path (may hold spaces), args the full command line.
    Fills `parents` with every pid's parent."""
    exe, ppids, starts = {}, {}, {}
    for line in comm_out.splitlines():
        parts = line.strip().split(None, 7)
        if len(parts) == 8 and parts[0].isdigit():
            pid = int(parts[0])
            exe[pid], ppids[pid] = parts[7], int(parts[1]) if parts[1].isdigit() else None
            starts[pid] = _lstart(parts[2:7])
    if parents is not None:
        parents.update(ppids)
    out = []
    for line in args_out.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) != 2 or not parts[0].isdigit():
            continue
        pid = int(parts[0])
        argv = parts[1].split()
        h = classify(argv, exe.get(pid, ""), windows=False)
        if h:
            out.append({"harness": h, "pid": pid, "ppid": ppids.get(pid), "cwd": None,
                        "start": starts.get(pid), "files": []})
    return out


def parse_lsof(out: str) -> tuple[dict, dict]:
    """`lsof -p 1,2 -Fpfn` -> ({pid: cwd}, {pid: [open .jsonl files]})."""
    cwd, files, pid, fd = {}, {}, None, None
    for line in out.splitlines():
        if line.startswith("p") and line[1:].isdigit():
            pid, fd = int(line[1:]), None
        elif line.startswith("f"):
            fd = line[1:]
        elif line.startswith("n") and pid is not None:
            if fd == "cwd":
                cwd[pid] = line[1:]
            elif line.endswith(".jsonl"):
                files.setdefault(pid, []).append(line[1:])
    return cwd, files


def _mac_procs():
    env = {**os.environ, "LC_ALL": "C"}  # English day and month names in lstart
    parents: dict = {}
    procs = parse_ps(_run(["ps", "-axo", "pid=,ppid=,lstart=,comm="], env=env),
                     _run(["ps", "-axo", "pid=,args="], env=env), parents)
    if procs:
        cwd, files = parse_lsof(_run(["lsof", "-w", "-p", ",".join(str(p["pid"]) for p in procs), "-Fpfn"]))
        for p in procs:
            p["cwd"], p["files"] = cwd.get(p["pid"]), files.get(p["pid"], [])
    return procs, parents


WIN_QUERY = ("Get-CimInstance Win32_Process | Select-Object ProcessId,ParentProcessId,ExecutablePath,CommandLine,"
             "@{n='Start';e={([DateTimeOffset]$_.CreationDate).ToUnixTimeSeconds()}} | ConvertTo-Json -Compress")


def parse_win(out: str, parents: dict | None = None) -> list:
    try:
        rows = json.loads(out or "[]")
    except ValueError:
        return []
    if isinstance(rows, dict):
        rows = [rows]
    procs = []
    for r in rows:
        if parents is not None and r.get("ProcessId") is not None:
            parents[r["ProcessId"]] = r.get("ParentProcessId")
        if not r.get("CommandLine"):
            continue
        h = classify(split_command(r.get("CommandLine") or ""), r.get("ExecutablePath") or "", windows=True)
        if h:
            procs.append({"harness": h, "pid": r.get("ProcessId"), "ppid": r.get("ParentProcessId"), "cwd": None,
                          "start": r.get("Start"), "files": []})
    return procs


def _win_procs():
    parents: dict = {}
    procs = parse_win(_run(["powershell", "-NoProfile", "-NonInteractive", "-Command", WIN_QUERY], timeout=10), parents)
    return procs, parents


def list_processes() -> tuple[list | None, str]:
    """(processes, method). None when this machine gives no process information."""
    try:
        import psutil  # noqa: F401
        return _dedupe(*_psutil_procs()), "psutil"
    except ImportError:
        pass
    try:
        if sys.platform.startswith("linux"):
            return _dedupe(*_linux_procs()), "proc"
        if sys.platform == "darwin":
            return _dedupe(*_mac_procs()), "ps+lsof"
        if sys.platform == "win32":
            return _dedupe(*_win_procs()), "windows"
    except (OSError, subprocess.SubprocessError):
        return None, "failed"
    return None, "unsupported"


# ---------------------------------------------------------------- log index

def _first_lines(path: str, n: int = 30):
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for i, line in enumerate(fh):
                if i >= n:
                    return
                try:
                    yield json.loads(line)
                except ValueError:
                    continue
    except OSError:
        return


def _last_line(path: str, size: int) -> dict | None:
    try:
        with open(path, "rb") as fh:
            fh.seek(max(size - 65536, 0))
            lines = [x for x in fh.read().splitlines() if x.strip()]
        return json.loads(lines[-1]) if lines else None
    except (OSError, ValueError, IndexError):
        return None


def claude_slug(cwd: str) -> str:
    """How Claude Code names a project folder: every character other than a letter or digit becomes '-'."""
    return re.sub(r"[^A-Za-z0-9]", "-", cwd or "")


class Index:
    """The session files written in the last day, grouped into sessions. Stats only,
    plus the first lines of each new file (cached) and Copilot's last line."""

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.meta: dict = {}  # path -> what its first lines said
        self.tails: dict = {}  # path -> (mtime, last line)
        self.lock = threading.Lock()
        self.built = 0.0
        self.sessions: dict = {}

    def _walk(self, root: str, since: float):
        for dirpath, _dirs, names in os.walk(root):
            for n in names:
                if n.endswith(".jsonl"):
                    p = os.path.join(dirpath, n)
                    try:
                        st = os.stat(p)
                    except OSError:
                        continue
                    if st.st_mtime >= since:
                        yield p, st

    def _claude_meta(self, path: str, root: str) -> dict:
        rel = os.path.relpath(path, root).split(os.sep)
        sid, cwd = None, None
        if len(rel) >= 4 and rel[2] == "subagents":
            sid = rel[1]
        elif len(rel) == 2 and not rel[1].startswith("agent-"):
            sid = rel[1][:-len(".jsonl")]
        for r in _first_lines(path):
            sid = sid or r.get("sessionId")
            cwd = cwd or r.get("cwd")
            if sid and cwd:
                break
        return {"sid": sid, "cwd": cwd, "slug": rel[0]}

    def _codex_meta(self, path: str) -> dict:
        for r in _first_lines(path, 1):
            if r.get("type") == "session_meta":
                p = r.get("payload") or {}
                cwd = (p.get("runtime_workspace_roots") or [p.get("cwd") or ""])[0]
                if os.path.basename(cwd.rstrip("/\\")).startswith("superfan-ask-"):
                    return {"sid": None}
                return {"sid": p.get("session_id") or p.get("parent_thread_id") or p.get("id"), "cwd": cwd,
                        "thread": p.get("id"), "start_ord": p.get("subagent_history_start_ordinal") or 0}
        return {"sid": None}

    def _copilot_meta(self, path: str) -> dict:
        folder = os.path.dirname(path)
        from .parse import copilot_workspace
        ws = copilot_workspace(folder)
        return {"sid": os.path.basename(folder), "cwd": ws.get("cwd") or ws.get("git_root")}

    def refresh(self, now: float | None = None, force: bool = False) -> dict:
        now = now or time.time()
        with self.lock:
            if not force and now - self.built < INDEX_TTL_S:
                return self.sessions
            since = now - LOOKBACK_H * 3600
            out: dict = {}
            for h in HARNESSES:
                root = self.cfg.get(ROOT_KEY[h])
                if not root or not os.path.isdir(root):
                    continue
                for p, st in self._walk(root, since):
                    if h == "copilot" and os.path.basename(p) != "events.jsonl":
                        continue
                    m = self.meta.get(p)
                    if m is None:
                        m = self.meta[p] = (self._claude_meta(p, root) if h == "claude-code" else
                                            self._codex_meta(p) if h == "codex" else self._copilot_meta(p))
                    if not m.get("sid"):
                        continue
                    s = out.setdefault((h, m["sid"]), {"harness": h, "session_id": m["sid"], "cwd": None, "slug": None,
                                                       "files": [], "mtime": 0.0, "ended": False})
                    s["files"].append(p)
                    if h == "claude-code" and len(os.path.relpath(p, root).split(os.sep)) == 2:
                        s["cwd"] = s["cwd"] or m.get("cwd")
                        s["slug"] = m.get("slug")
                    elif h == "codex" and m.get("thread") == m["sid"]:
                        s["cwd"] = m.get("cwd")
                    else:
                        s["cwd"] = s["cwd"] or m.get("cwd")
                        s["slug"] = s["slug"] or m.get("slug")
                    s["mtime"] = max(s["mtime"], st.st_mtime)
                    if h == "copilot":
                        t = self.tails.get(p)
                        if not t or t[0] != st.st_mtime:
                            t = self.tails[p] = (st.st_mtime, _last_line(p, st.st_size))
                        s["ended"] = bool(t[1] and t[1].get("type") == "session.shutdown")
            for s in out.values():
                s["files"].sort()
                s["project"] = os.path.basename((s["cwd"] or "").rstrip("/\\")) or s.get("slug") or "?"
            self.sessions, self.built = out, now
            return out


START_SLACK_S = 5  # clock rounding between a process's start time and a file's mtime


def assign_states(sessions: dict, procs: list | None, now: float) -> None:
    """Sets state, age_s and how it was decided on every session (in place).

    A running CLI process accounts for at most one session, matched in this order:
    1. the session whose log file it holds open (exact; macOS, Linux, psutil);
    2. otherwise the most recently written session in its working folder (any folder
       on Windows, which does not give it) that was written after the process started:
       a process started this morning cannot be the one that wrote a log last
       touched yesterday and not since.
    A session with no process left over has ended."""
    flagged: dict = {}
    if procs is not None:
        by_file = {}
        for s in sessions.values():
            for f in s["files"]:
                by_file[_norm(f)] = s
        for h in HARNESSES:
            mine = [p for p in procs if p["harness"] == h]
            left = []
            for p in mine:
                c = _norm(p.get("cwd"))
                if c and os.path.basename(c).startswith("superfan-ask-"):
                    continue
                held = [by_file.get(_norm(f)) for f in p.get("files") or []]
                held = [x for x in held if x is not None and x["harness"] == h and not x["ended"]]
                if held:
                    for x in held:
                        flagged[id(x)] = "log file open"
                else:
                    left.append(p)
            cands = sorted((s for s in sessions.values() if s["harness"] == h and not s["ended"] and id(s) not in flagged),
                           key=lambda s: -s["mtime"])
            # newest processes first: they are the likeliest owners of the newest logs
            for p in sorted(left, key=lambda p: -(p.get("start") or 0)):
                c = _norm(p.get("cwd"))
                slug = claude_slug(c) if c else None
                start = p.get("start")
                for s in cands:
                    if id(s) in flagged:
                        continue
                    if c and not (_norm(s["cwd"]) == c or (h == "claude-code" and s.get("slug") == slug)):
                        continue
                    if start and s["mtime"] < start - START_SLACK_S:
                        continue
                    flagged[id(s)] = "process" if c else "process count"
                    break
    for s in sessions.values():
        age = max(now - s["mtime"], 0)
        s["age_s"] = int(age)
        s["last"] = datetime.fromtimestamp(s["mtime"], timezone.utc).isoformat()
        if s["ended"]:
            s["state"], s["by"] = "ended", "end marker"
        elif age < WORKING_S:
            s["state"], s["by"] = "working", "log written " + str(int(age)) + "s ago"
        elif id(s) in flagged:
            s["state"], s["by"] = "waiting", flagged[id(s)]
        elif procs is not None:
            s["state"], s["by"] = "ended", "no process"
        elif age < IDLE_S:
            s["state"], s["by"] = "idle", "file time"
        else:
            s["state"], s["by"] = "ended", "file time"


ORDER = {"working": 0, "waiting": 1, "idle": 2, "ended": 3}


class Live:
    """Shared by every request: the index, the process list, and one watcher per watched session."""

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.index = Index(cfg)
        self.check = cfg.get("process_check", True)
        self._procs: tuple = (0.0, None, "off")
        self.plock = threading.Lock()
        self.watchers: dict = {}
        self.wlock = threading.Lock()

    def processes(self) -> tuple:
        if not self.check:
            return None, "off"
        with self.plock:
            t, procs, how = self._procs
            if time.time() - t >= PROCESS_TTL_S:
                procs, how = list_processes()
                self._procs = (time.time(), procs, how)
            return procs, how

    def sessions(self, now: float | None = None) -> tuple[dict, str]:
        now = now or time.time()
        idx = self.index.refresh(now)
        procs, how = self.processes()
        assign_states(idx, procs, now)
        return idx, how

    def overview(self) -> dict:
        idx, how = self.sessions()
        rows = [{k: s[k] for k in ("harness", "session_id", "project", "state", "by", "age_s", "last")}
                for s in idx.values() if s["state"] != "ended"]
        rows.sort(key=lambda r: (ORDER[r["state"]], r["age_s"]))
        return {"process_check": how, "working_s": WORKING_S, "idle_s": IDLE_S, "sessions": rows,
                "now": datetime.now(timezone.utc).isoformat()}

    def watcher(self, harness: str, sid: str) -> "Watcher":
        with self.wlock:
            w = self.watchers.get((harness, sid))
            if w is None:
                w = self.watchers[(harness, sid)] = Watcher(self, harness, sid)
            return w


# ------------------------------------------------------------ one session

def _short_path(p: str) -> str:
    parts = [x for x in str(p).replace("\\", "/").split("/") if x]
    return "/".join(parts[-2:])


def _first_word(cmd) -> str:
    if isinstance(cmd, list):
        cmd = " ".join(str(c) for c in cmd)
    words = [w for w in str(cmd or "").split() if "=" not in w.split("/")[0]]  # skip FOO=bar prefixes
    w = words[0] if words else ""
    if w in ("bash", "sh", "zsh", "-lc", "-c") and len(words) > 2:
        return _first_word(words[2:]) if words[1] in ("-lc", "-c") else w
    return _base(w, False)[:40]


def _target(inp) -> str:
    if isinstance(inp, str):
        try:
            inp = json.loads(inp)
        except ValueError:
            return ""
    if not isinstance(inp, dict):
        return ""
    for k in ("file_path", "path", "notebook_path", "filePath"):
        if inp.get(k):
            return _short_path(inp[k])
    for k in ("command", "cmd"):
        if inp.get(k):
            return _first_word(inp[k])
    for k in ("subagent_type", "agent_type", "agentName", "skill"):
        if inp.get(k):
            return str(inp[k])[:40]
    return ""


_MARKERS = {"claude-code": b'"tool_use"', "codex": b'_call"', "copilot": b'"tool.execution_start"'}


def _tool_from(harness: str, r: dict, m: dict, sub: bool) -> list:
    """Tool calls in one log line as (time, role, tool, target)."""
    out = []
    if harness == "claude-code":
        if r.get("type") == "assistant":
            for b in (r.get("message") or {}).get("content") or []:
                if isinstance(b, dict) and b.get("type") == "tool_use":
                    out.append((r.get("timestamp"), "worker" if sub or r.get("isSidechain") else "orchestrator",
                                b.get("name") or "?", _target(b.get("input"))))
    elif harness == "codex":
        p = r.get("payload") or {}
        if (r.get("ordinal") or 0) >= m.get("start_ord", 0) and r.get("type") == "response_item" \
                and p.get("type") in ("function_call", "custom_tool_call", "local_shell_call"):
            role = "orchestrator" if m.get("thread") == m.get("sid") else "worker"
            out.append((r.get("timestamp"), role, p.get("name") or p.get("type"),
                        _target(p.get("arguments") or p.get("input") or p.get("action"))))
    elif r.get("type") == "tool.execution_start":
        d = r.get("data") or {}
        out.append((r.get("timestamp"), "worker" if r.get("agentId") else "orchestrator",
                    d.get("toolName") or "?", _target(d.get("arguments"))))
    return [x for x in out if x[0]]


class ToolFeed:
    """The last n tool calls of a session: time, agent, tool name and a short
    target (last two path parts or a command's first word), never arguments
    or text. Reads each file once, then only the bytes added since, and only
    parses lines that can hold a tool call (a single log line can be megabytes)."""

    def __init__(self, harness: str, n: int = RECENT_N):
        self.harness, self.n = harness, n
        self.offsets: dict = {}
        self.items: list = []

    def update(self, files: list, meta: dict) -> list:
        marker = _MARKERS[self.harness]
        for path in files:
            off = self.offsets.get(path, 0)
            try:
                with open(path, "rb") as fh:
                    fh.seek(off)
                    data = fh.read()
            except OSError:
                continue
            cut = data.rfind(b"\n") + 1  # leave a half-written last line for next time
            self.offsets[path] = off + cut
            m, sub = meta.get(path) or {}, os.sep + "subagents" + os.sep in path
            for line in data[:cut].splitlines():
                if marker not in line:
                    continue
                try:
                    self.items += _tool_from(self.harness, json.loads(line), m, sub)
                except ValueError:
                    continue
            self.items = sorted(self.items, key=lambda x: x[0])[-self.n:]
        return [{"ts": t, "role": role, "tool": tool, "target": tgt} for t, role, tool, tgt in self.items]


def recent_tools(harness: str, files: list, meta: dict, n: int = RECENT_N) -> list:
    return ToolFeed(harness, n).update(files, meta)


def burn(agents: list, now: datetime, minutes: int = 30) -> dict:
    """Tokens (and dollars) per minute over the last `minutes`, oldest first."""
    tokens, cost = [0] * minutes, [0.0] * minutes
    priced = False
    start = now - timedelta(minutes=minutes)
    for a in agents:
        for ts, t, c in a.timeline:
            if ts is None or ts < start or ts > now:
                continue
            i = min(int((ts - start).total_seconds() // 60), minutes - 1)
            tokens[i] += t
            if c is not None:
                cost[i] += c
                priced = True
    return {"tokens": tokens, "cost_usd": [round(c, 4) for c in cost] if priced else None,
            "last5_tokens": sum(tokens[-5:]), "last5_cost_usd": round(sum(cost[-5:]), 4) if priced else None}


class Watcher:
    """Re-reads one session's files when they change. The same parsers as the
    dashboard, so the numbers match `superfan scan` for the whole session."""

    def __init__(self, live: Live, harness: str, sid: str):
        self.live, self.harness, self.sid = live, harness, sid
        self.lock = threading.Lock()
        self.sig = None
        self.next_parse = 0.0
        self.parse_s = 0.0
        self.data: dict | None = None
        self.version = 0
        self.tools = ToolFeed(harness)
        self._agents: list = []
        self.burn_at = 0.0
        self.detail: dict | None = None
        self.detail_at = 0.0
        self.detail_sig = None

    def _parse(self, files: list):
        if self.harness == "claude-code":
            got = scan_claude(None, Prices(self.live.cfg.get("prices") or {}), paths=files)
        elif self.harness == "codex":
            got = scan_codex(None, paths=files)
        else:
            got = scan_copilot(None, paths=files)
        return got.get(self.sid) or (next(iter(got.values())) if self.harness == "copilot" and got else None)

    def snapshot(self) -> dict | None:
        """The latest view, re-read if a file changed (at most every couple of seconds)."""
        with self.lock:
            idx, how = self.live.sessions()
            s = idx.get((self.harness, self.sid))
            if s is None:
                if self.data:
                    self.data["state"] = {"state": "ended", "by": "no longer in the last day's logs", "age_s": None}
                return self.data
            sig = []
            for p in s["files"]:
                try:
                    st = os.stat(p)
                    sig.append((p, st.st_size, st.st_mtime))
                except OSError:
                    pass
            sig = tuple(sig)
            state = {k: s[k] for k in ("state", "by", "age_s", "last")}
            if sig != self.sig and time.time() >= self.next_parse:
                t0 = time.time()
                sess = self._parse(s["files"])
                self.parse_s = time.time() - t0
                self.next_parse = time.time() + max(1.0, 2 * self.parse_s)
                self.sig = sig
                if sess is not None:
                    self._agents = [a for a in sess.agents.values() if a.model_calls or a.tool_calls]
                    self._summary = None
                    self.data = self._view(sess, s)
                    self.version += 1
                    self.burn_at = time.time()
            elif self.data is not None and time.time() - self.burn_at >= 10:
                # Nothing new in the logs, but time moved on: the last-30-minutes chart and the
                # breakdown must not stay frozen at the last write.
                self.burn_at = time.time()
                b = burn(self._agents, datetime.now(timezone.utc))
                d = self._detail(self.data["summary"], s)
                if b != self.data["burn"] or d is not self.data["detail"]:
                    self.data["burn"], self.data["detail"] = b, d
                    self.version += 1
            if self.data is not None:
                self.data["state"] = state
                self.data["process_check"] = how
            return self.data

    def _detail(self, summary: dict, s: dict) -> dict | None:
        """Same "where tokens went" and suggestions as the dashboard's session drill-down,
        for this session's files only; at most every DETAIL_EVERY_S seconds."""
        if self.detail is not None and (self.sig == self.detail_sig or time.time() - self.detail_at < DETAIL_EVERY_S):
            return self.detail
        cfg = self.live.cfg
        files = s["files"]
        try:
            if self.harness == "claude-code":
                outs, _ = claude_outputs(None, Prices(cfg.get("prices") or {}), paths=files)
            elif self.harness == "codex":
                outs, _ = codex_outputs(None, paths=files)
            else:
                outs, _ = copilot_outputs(None, paths=files)
            outs = [o for o in outs if o["session"] == summary["session_id"]] or outs
            self.detail = advice.session_detail(summary, outs, cfg.get("orchestrator_limit", 0.40),
                                                cfg.get("output_limit", 8000))
        except Exception as e:  # the live view must keep working even if the breakdown can't be read
            self.detail = {"error": repr(e)}
        self.detail_at, self.detail_sig = time.time(), self.sig
        return self.detail

    def _view(self, sess, s: dict) -> dict:
        cfg = self.live.cfg
        summary = sess.summary()
        agents = [a for a in sess.agents.values() if a.model_calls or a.tool_calls]
        now = datetime.now(timezone.utc)
        orch = next((a for a in agents if a.role == "orchestrator"), None)
        live_agents = {r["agent_id"]: r for r in summary["agents"]}
        for a in agents:
            r = live_agents.get(a.agent_id)
            if r is not None:
                r["last_context"] = a.last_context
                r["last"] = a.last.isoformat() if a.last else None
        detail = self._detail(summary, s)
        flags = rules.orchestrator_share([summary], cfg.get("orchestrator_limit", 0.40))
        flags += rules.frequent_compaction([summary]) + rules.tool_definitions([summary])
        return {
            "harness": self.harness, "session_id": self.sid, "project": s["project"],
            "summary": summary,
            "burn": burn(agents, now),
            "context": {"last": orch.last_context if orch else 0, "max": orch.max_context if orch else 0},
            "recent": self.tools.update(s["files"], self.live.index.meta),
            "flags": flags,
            "detail": detail,
            "files": len(s["files"]),
            "parse_s": round(self.parse_s, 3),
            "read_at": now.isoformat(),
        }
