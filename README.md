# superfan

Reads the session logs your coding agents leave on disk and tells you, per
session, what it cost and where the tokens went: orchestrator versus each
subagent, model, cache use, tool calls, time. Built to watch runs of the
[Agent Harness Kit](https://github.com/SedyBenoitPeace/agent-harness-kit), but
the parser works on any Claude Code or Codex session.

Read-only: it opens log files and prints. Python 3.10+, standard library only.

## Use

```sh
# one JSON row per session for the last 7 days
python3 -m superfan scan --claude ~/.claude/projects --codex ~/.codex/sessions --days 7 --out sessions.json

# the 5 largest, as a Markdown table (--by cost for Claude Code dollars)
python3 -m superfan top sessions.json --n 5

# health rules (see below); --reports takes the kit's docs/runs folders,
# --tz the UTC offset their local times are in
python3 -m superfan check --codex ~/.codex/sessions --reports ~/src/app/docs/runs --tz +02:00
```

### Web page

```sh
python3 -m superfan serve --reports ~/src/app/docs/runs
```

Opens `http://127.0.0.1:8765` with a **Run now** button, a window picker
(24 hours to 90 days) and a harness filter (All, Claude Code, Codex) that
applies to the whole page:

- Summary cards; click one for its detail (sessions per day, token mix and
  models, Claude Code spend by token kind, model and project, tokens by role,
  every health-rule finding).
- Under All, sessions grouped by project with per-harness numbers; under one
  harness, a flat list. Click a session for its agents, where its tokens went,
  its largest tool outputs and **What to improve** (rule-based suggestions).
- **Ask Claude** on a session runs `claude -p` with every tool off and no
  saved session; **Ask ChatGPT** runs `codex exec` (your Codex login) in a
  read-only sandbox from an empty temporary folder, with `--ephemeral` when
  the CLI has it. Both get that session's numbers and command names, never
  the transcript. `--ask-model` and `--ask-codex-model` pick the models.
  Questions asked this way never show up as sessions in the page. The same
  two buttons on the home page ask across every session in the window and
  harness filter (totals, the 40 largest sessions, the breakdown, findings).
- **Weekly review**: a fresh 7-day read plus the health rules, written up by
  Claude or ChatGPT (top sessions, what fired, changes to make first, kit
  issues worth filing). Shown on the page with a Download .md button; nothing
  is filed or saved.
- **Where tokens go**, one card per harness; click a kind of work for the
  commands and files behind it, the sessions, and the largest single outputs.

It reads `~/.claude/projects` and `~/.codex/sessions` by default
(`--claude ''` or `--codex ''` turns one off). It listens on 127.0.0.1 only,
refuses other Host headers, needs a custom header on anything that does work
(so other websites can't trigger it), writes nothing, and keeps the last
result in memory. `python3 -m superfan report` prints the overview in the
terminal.

Each session row has: `harness`, `project` (working folder name), `models`,
`tokens` {`input` (fresh), `cache_read`, `cache_write`, `output`, `total`},
`cost_usd` (Claude Code only; Codex and Copilot stay tokens only), `active_s`
(time with no gap over 10 minutes) and `duration_s` (wall), `model_calls`,
`tool_calls` and `tools`, `orchestration_share`, and `agents[]` with the same
fields per orchestrator, subagent and approval reviewer.

Dollars use `superfan/prices.json` (Anthropic API list prices). On a
subscription that is the API-equivalent value, not your bill. Pass
`--prices` to use your own table.

## Health rules

| Rule | Fires when | Default |
|---|---|---|
| R1 orchestrator share | orchestrator plus approval reviewers take more than X% of a session with subagents | 40%, sessions over 1M tokens |
| R2 report matches logs | a kit run report has negative or blank numbers, an `unknown` model, or totals more than 10% away from the logs for the same window | 10% |
| R3 large tool output | one tool output is at least N tokens, so every later call in that thread pays for it again | 8,000 tokens |

Each finding names the session, the agent, the tokens and why it matters.

## Where tokens go

`superfan.breakdown` (`codex_outputs`, `claude_outputs`, `summarise`) groups
tool calls by kind of work and estimates the tokens each kind keeps in
context: output size times the model calls that follow it in the same
thread. It is an estimate (characters / 4, no compaction). A call is grouped
first by its tool name (`TOOL_CATEGORIES`: Read, apply_patch, spawn_agent,
MCP tools...), then a shell call by its first command (`DEFAULT_CATEGORIES`:
kit scripts, skills and agent docs, reading files, simulator, build, git...).
Both are ordered regular expressions you can replace. Claude Code outputs
also get a dollar estimate at the model's cache-read price.

## Formats

See [docs/formats.md](docs/formats.md) for what each log looks like and the
traps (Codex subagent files that start with a copy of the parent's history;
Claude Code responses split over several lines with the same usage).

## Tests

```sh
python3 -m unittest discover tests
```

Fixtures are synthetic. The Codex ones come from the Agent Harness Kit's test
data (MIT). Never commit a real session log, trimmed or not.

## Not yet

Copilot CLI logs; Codex `.jsonl.zst` archives; R2 for Claude Code runs.

## License

MIT, see [LICENSE](LICENSE).
