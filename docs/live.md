# Live sessions

How `serve` decides which sessions are open, and how the Live screen updates.
Code: `superfan/live.py`, `superfan/web/live.html`, endpoints in `superfan/serve.py`.

## Which sessions are open

Every few seconds `serve` looks at the log files written in the last day
(file times only, plus the first lines of a file it has not seen yet) and
groups them into sessions, the same way the parser does:

| CLI | Files of one session | End marker |
|---|---|---|
| Claude Code | `<project>/<session>.jsonl` and `<project>/<session>/subagents/agent-*.jsonl` | none |
| Codex | every rollout file whose `session_meta` points at the same root thread | none |
| Copilot | `<session>/events.jsonl` | last line is `session.shutdown` (a resumed session writes more after it) |

It then lists running processes and keeps the ones that are a CLI:

| Platform | How | Working folder |
|---|---|---|
| any, with psutil installed | `psutil.process_iter` | yes |
| Linux | `/proc/<pid>/cmdline`, `/proc/<pid>/cwd` | yes |
| macOS | `ps -axo pid=,ppid=,comm=` and `ps -axo pid=,args=`, then `lsof -a -d cwd -p <pids> -Fpn` | yes |
| Windows | PowerShell `Get-CimInstance Win32_Process` (ProcessId, ParentProcessId, ExecutablePath, CommandLine) | **no**: Windows keeps it in the process's own memory (PEB); only psutil reads it |

A process is Claude Code if its program is `claude` (or a Node/Bun script
named `claude`, or the `@anthropic-ai/claude-code` package); the Claude
desktop app (`Claude.app/.../MacOS/Claude`, `AnthropicClaude\` on Windows)
is not, but the Claude Code it starts is. Codex is `codex` or `@openai/codex`
(including `codex app-server`, used by the IDE extension and the app);
Copilot is `copilot` or `@github/copilot`. A wrapper and the binary it starts
(npm shim, then native `codex`) count once, by parent id.

States:

1. **Ended** if the log has an end marker.
2. **Working** if a session file was written in the last 2 minutes.
3. **Open, waiting** if a CLI process accounts for it. Each process accounts
   for at most one session: the one whose log file it holds open, if the OS
   says so (lsof, `/proc/<pid>/fd`, psutil); otherwise the most recently
   written session in its working folder (any folder on Windows) that was
   **written after the process started**. A process started this morning
   cannot own a log last written yesterday. Processes nested anywhere under
   another process of the same CLI (helpers, a shell in between) count once.
4. **Ended** if processes were listed and none accounts for it.
5. Without process information (`--no-process-check`, or listing failed):
   **Idle** if written in the last 30 minutes, else ended.

Process start times: psutil `create_time`, `/proc/<pid>/stat` field 22,
`ps -o lstart` (C locale) on macOS, `CreationDate` on Windows.

## How the Live screen updates

`GET /api/live/stream?h=<harness>&id=<session>` is a Server-Sent Events
stream. Once a second the server stats the session's files; when one changed
it re-reads that session with the same `scan_*` functions the dashboard uses
(so totals match `superfan scan`), at most every max(1 s, 2 × last read
time). It sends `view` on new data, `state` when the state changes, `ping`
every 15 s. `GET /api/live` is the open list; `GET /api/live/session` one
view without streaming.

Measured on the synthetic fixtures: a new log line reached the open page in
under 0.5 s.

## v2 ideas (not built)

- Notify (macOS notification or a sound) when a rule fires or the burn rate
  crosses a limit while you watch.
- A cost or token cap per session that warns before it is crossed.
- Several sessions side by side (a kit run's lanes).
- Large tool outputs and re-reads checked live (needs the breakdown for one
  session instead of the whole window).
- Read only new lines (keep a byte offset per file) for very large sessions,
  if a re-read gets slow.
- Context gauge against the model's real window size instead of the largest
  prompt so far (Copilot logs `tokenLimit` in `session.compaction_start`).
- Windows working folders without psutil (would need reading the process
  PEB through ctypes).
- Assistants without local logs (claude.ai, ChatGPT) are out of reach: they
  keep no token log on your machine.
