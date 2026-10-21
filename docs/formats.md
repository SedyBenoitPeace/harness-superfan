# Log formats

What the parser reads, and the fields it relies on. All examples are
synthetic. Confirmed against real logs on 2026-10-16 unless marked
**unconfirmed**.

## Codex CLI (0.162.x) — `~/.codex/sessions/YYYY/MM/DD/rollout-<time>-<thread id>.jsonl`

One file per **thread**. A session is a root thread (`thread_source: "user"`)
plus every thread whose `session_id` points at it. Older days may be
compressed as `.jsonl.zst` (not read yet). A resumed thread keeps appending to
its original file, so an August file can hold October calls: filter by each
line's `timestamp`, not by the file's date.

Every line is `{"timestamp", "ordinal", "type", "payload"}`.

| `type` / `payload.type` | What it gives |
|---|---|
| `session_meta` (line 1) | `id` (thread), `session_id` (root), `parent_thread_id`, `thread_source` (`user`, `subagent`, `guardian_review`), `agent_role` (`harness-builder`, `harness-evaluator`, `explorer`, `worker`), `agent_path`, `cwd`, `cli_version`, `subagent_history_start_ordinal` |
| `turn_context` | `model`, `effort`, sandbox and approval policy |
| `event_msg` / `thread_settings_applied` | `thread_settings.model` (the model when no `turn_context` follows) |
| `token_usage_record` | one per model call: `thread_id`, `usage` {`input_tokens`, `cached_input_tokens`, `cache_write_input_tokens`, `output_tokens`, `reasoning_output_tokens`}, plus running `thread_token_usage` |
| `event_msg` / `token_count` | cumulative `info.total_token_usage` (no thread id; older CLIs have only this) |
| `response_item` / `custom_tool_call`, `function_call` | a tool call: `name` (`exec`, `spawn_agent`, `wait_agent`, `send_message`…), `input` or `arguments`, `call_id` |
| `response_item` / `*_call_output` | what the model saw back, by `call_id` |
| `event_msg` / `item_completed` (`CommandExecution`) | each shell command run inside an `exec` call: `command`, `exit_code`, `duration`, `thread_id` |
| `response_item` / `reasoning`, `agent_message` | encrypted; never read |

Gotchas:

- `input_tokens` **includes** `cached_input_tokens`; fresh input is the difference.
  `output_tokens` includes `reasoning_output_tokens`.
- A subagent file begins with a **copy of its parent's history** (a second
  `session_meta`, old tool calls, even an old `task_complete`). Skip every line
  with `ordinal < subagent_history_start_ordinal`, and only count
  `token_usage_record` lines whose `thread_id` is the file's own thread.
- `guardian_review` threads (model `codex-auto-review`) are the approval
  reviewer. The kit counts them as orchestration; so does the parser (role `reviewer`).
- No prices: Codex is reported in tokens only.

## Claude Code (2.x) — `~/.claude/projects/<project slug>/<session id>.jsonl` (**unconfirmed on this machine**)

One file per session. Subagent transcripts are either separate files,
`<session id>/subagents/agent-<agent id>.jsonl` (newer versions), or lines in
the main file with `isSidechain: true` (older versions). The parser handles both.

| Line field | What it gives |
|---|---|
| `type` | `user`, `assistant`, `system`, `summary` … only `assistant` carries usage |
| `sessionId`, `cwd`, `timestamp`, `isSidechain`, `agentId`, `requestId` | grouping and timing |
| `message.model` | model id (`<synthetic>` for client-made messages: skipped) |
| `message.usage` | `input_tokens` (fresh), `cache_read_input_tokens`, `cache_creation_input_tokens`, `cache_creation.ephemeral_5m_input_tokens` / `ephemeral_1h_input_tokens`, `output_tokens` |
| `message.content[]` of type `tool_use` | tool calls: `name` (`Bash`, `Read`, `Agent`/`Task` …), `id` |
| `toolUseResult.agentId` / `agentType` on the parent's tool result | which subagent type an agent id was |

Gotchas:

- One API response is written as several lines (one per content block) with the
  **same** `message.id` and the same `usage`: count usage once per
  (`message.id`, `requestId`).
- Unlike Codex, `input_tokens` excludes cache reads and writes.
- Cost = fresh × input price + cache read × read price + 5-minute writes × 1.25
  × input + 1-hour writes × 2 × input + output × output price
  (`superfan/prices.json`; API list prices, so on a subscription this is the
  API-equivalent value, not a bill).

## GitHub Copilot CLI (1.0.9x) — `~/.copilot/session-state/<session id>/events.jsonl` (**tool results unconfirmed**)

One folder per session: `events.jsonl` plus `workspace.yaml` (`cwd`, `git_root`,
`repository`, `branch`; the project name is the folder of `git_root`). Every
line is `{"type", "data", "id", "timestamp", "parentId"}`, plus `agentId` on a
subagent's lines (absent = orchestrator).

| `type` | What it gives |
|---|---|
| `session.start` | `sessionId`, `copilotVersion`, `context.cwd` / `gitRoot` |
| `session.usage_record` | one per model call: `usage` {`model`, `inputTokens`, `outputTokens`, `cacheReadTokens`, `cacheWriteTokens`, `reasoningTokens`, `initiator`, `interactionType`} |
| `subagent.started` / `subagent.completed` | `agentName` (the agent type), `model`, `totalTokens`, `totalToolCalls`, `durationMs` |
| `session.compaction_start` | `toolDefinitionsTokens`, `systemTokens`, `conversationTokens`, `tokenLimit` (Copilot's own estimate) |
| `session.compaction_complete` | `compactionTokensUsed` {`inputTokens`, `outputTokens`, `cacheReadTokens`, `cacheWriteTokens`} |
| `session.usage_checkpoint` | `promptCacheBreakState[].models.*.tool_tokens` (orchestrator only) |
| `tool.execution_start` / `tool.execution_complete` | `toolCallId`, `toolName` (MCP tools are `<server>-<tool>`), `arguments`; `result.content` is what the model saw |
| `user.message`, `assistant.message`, `system.message`, `summaryContent` | prompts and transcripts; never read |

Gotchas:

- `inputTokens` **includes** cache reads and writes; fresh input is the rest.
- Compaction is a separate model call that `subagent.completed totalTokens`
  leaves out. The parser adds it to the agent's tokens and also reports it
  as `compaction_tokens` (and as model `compaction` in `by_model`).
- Copilot's local size estimates (`compaction_start`) run about 1.4x under the
  billed `inputTokens`; the parser only uses them for the tool-definition size.
- `copilotUsage.totalNanoAiu` (AI credits) is a price unit; it is never read.
  Copilot is reported in tokens only.

## Agent Harness Kit run reports — `docs/runs/<YYYY-MM-DD-HHMM>.md` (kit 3.9–3.12)

Markdown written by `run-report.sh` at the end of a continuous run.

- `## Summary`: `Start`/`End` (commit and local time), `Stop reason`,
  `Counts: done N · skipped N · not started N`, then either
  `Cost: $X — feature agents $Y, orchestration $Z (P%)` and `Cost by model`
  (Claude Code edition) or `Tokens: …` and `Tokens by model` (Codex edition).
- `## Done`: one line per feature: id, title, commit, minutes, dollars, model,
  and an indented `Decisions:` line.
- `## Skipped`, `## Not started`, `## Waiting on a human`, `## Supervisor`, `## Branches`.

The run ledger behind it lives in `.harness-run/` in the project:
`usage.log` (`<feature|orchestration> <harness> <model> <input incl. cache> <cached> <output>`;
the kit's Copilot numbers leave out compaction, so R2 says so when that explains the gap),
`cost.log` (`<feature> <dollars>`), `run-cost` (`agents $` / `orchestration $`),
`models.log`, `budget.log`, `STOP`, `start` (run's first commit), `previous/`.
