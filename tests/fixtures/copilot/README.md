Synthetic GitHub Copilot CLI session (`~/.copilot/session-state/<session>/`),
in the shape of Copilot CLI 1.0.9x events. Every value is made up.

One orchestrator (3 model calls) and one `harness-builder` subagent (3 calls,
one context compaction). Prompt and summary text is `<redacted>` or filler.

`...0002` is in the older shape (CLI 1.0.8x): no `session.usage_record`
lines, only cumulative `session.shutdown` totals, one before the test window
and one inside it.
