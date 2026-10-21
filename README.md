# superfan

Reads the session logs your coding agents leave on disk and tells you, per
session, what it cost and where the tokens went: orchestrator versus each
subagent, model, cache use, tool calls, time. Built to watch runs of the
[Agent Harness Kit](https://github.com/SedyBenoitPeace/agent-harness-kit), but
the parser works on any Claude Code, Codex or GitHub Copilot CLI session.

It only reads your logs and run reports. The one place it writes, and the
only place it deletes, is its own reviews folder (see Reviews). Python 3.10+,
standard library only. 
## Platforms

Tested on macOS. The code is standard-library Python and Claude Code,
Codex and Copilot keep their logs in the same places on Linux, so it should work there,
but that is untested. Windows is untested. Automatic reviews use launchd
(macOS); on Linux use cron or a systemd timer.

## Use

```sh
# one JSON row per session for the last 7 days
python3 -m superfan scan --claude ~/.claude/projects --codex ~/.codex/sessions \
  --copilot ~/.copilot/session-state --days 7 --out sessions.json

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
(24 hours to 90 days) and a harness filter (All, Claude Code, Codex, Copilot) that
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
- **Reviews** tab: run a review of one day, one run or a custom window (see
  below), and read the saved ones, newest first, with Download .md and
  Delete, plus "delete reviews older than N days".
- **Where tokens go**, one card per harness; click a kind of work for the
  commands and files behind it, the sessions, and the largest single outputs.

It reads `~/.claude/projects`, `~/.codex/sessions` and
`~/.copilot/session-state` by default (`--claude ''`, `--codex ''` or
`--copilot ''` turns one off). It listens on 127.0.0.1 only,
refuses other Host headers, needs a custom header on anything that does work
(so other websites can't trigger it), and keeps the last result in memory.
The only files it writes are reviews. `python3 -m superfan report` prints the overview in the
terminal.

Each session row has: `harness`, `project` (working folder name), `models`,
`tokens` {`input` (fresh), `cache_read`, `cache_write`, `output`, `total`},
`cost_usd` (Claude Code only; Codex and Copilot stay tokens only), `active_s`
(time with no gap over 10 minutes) and `duration_s` (wall), `model_calls`,
`tool_calls` and `tools`, `orchestration_share`, and `agents[]` with the same
fields per orchestrator, subagent and approval reviewer. Copilot sessions
also have `compactions`, `compaction_tokens` (tokens context compaction used;
included in `tokens`) and, per agent, `tool_def_tokens` (the tool-definition
block re-sent with every call).

Dollars use `superfan/prices.json` (Anthropic API list prices). On a
subscription that is the API-equivalent value, not your bill. Pass
`--prices` to use your own table.

## Reviews

```sh
python3 -m superfan review --day                 # today, compared with the 6 days before
python3 -m superfan review --day 2026-10-15
python3 -m superfan review --run ~/src/app/docs/runs/2026-10-15-1417.md   # one run, plus R2
python3 -m superfan review --since 2026-10-13 --until 2026-10-15T12:00
```

Python computes every number: totals per harness (dollars for Claude Code
only), the largest sessions, the top 3 problems (the page's "What to improve"
tips, grouped across sessions and ordered by tokens at stake), the health
rules, and for `--run` whether the run report agrees with the logs. A model
then writes a short summary from those numbers only: `claude -p` with every
tool off by default, `--with codex` for ChatGPT, `--with both` for a second
opinion, `--with none` for numbers only. It never sees a transcript, and if
the model is missing the numbers are still saved.

Each review is saved as `<date-time>-<scope>.md` plus a `.json` of the
numbers in `~/.superfan/reviews` (or `$SUPERFAN_REVIEWS`, or `--reviews DIR`).
Keep that folder outside any repository: reviews describe your own sessions
and projects. Delete one from the page, or old ones with
`superfan review --prune 30` or the page's "older than N days". `--keep-days N`
(on `review` and `serve`) deletes reviews older than N days after each new
one; it is off by default. Deleting only touches files named like a review
in that folder, never logs or run reports. The page's Reviews tab runs the same command and lists the
same folder.

### Automatic reviews (optional, macOS)

The repo has no schedule; reviews run when you ask. To run them on their own,
`examples/launchd/` has two launchd agents with placeholder paths:

- `com.example.superfan.daily.plist`: `review --day` every evening at 21:00.
- `com.example.superfan.runs.plist`: watches your projects' `docs/runs`
  folders and runs `review --run latest` when a report appears. `latest`
  takes the newest report and skips it if it was already reviewed.

Copy one to `~/Library/LaunchAgents/`, replace every `/PATH/TO/...` and
`YOUR-NAME` (set `PATH` so launchd finds `claude` and `codex`), then
`launchctl load ~/Library/LaunchAgents/<file>`. `launchctl unload` turns it
off. Keep your filled-in copies out of the repo. On Linux, a cron line or a
systemd timer running the same command does the same job.

## Health rules

| Rule | Fires when | Default |
|---|---|---|
| R1 orchestrator share | orchestrator plus approval reviewers take more than X% of a session with subagents | 40%, sessions over 1M tokens |
| R2 report matches logs | a kit run report has negative or blank numbers, an `unknown` model, or totals more than 10% away from the logs for the same window | 10% |
| R3 large tool output | one tool output is at least N tokens, so every later call in that thread pays for it again | 8,000 tokens |

Each finding names the session, the agent, the tokens and why it matters.

## Where tokens go

`superfan.breakdown` (`codex_outputs`, `claude_outputs`, `copilot_outputs`, `summarise`) groups
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

Fixtures are synthetic. The Copilot one is hand-made. The Codex ones come from the Agent Harness Kit's test
data (MIT). Never commit a real session log, trimmed or not.

## Not yet

Codex `.jsonl.zst` archives; R2 for Claude Code runs;
Windows scheduling examples.

## License

MIT, see [LICENSE](LICENSE).
