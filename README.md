# claude-plugins

A Claude Code plugin marketplace ("claude context tools"):
plugins for context-size awareness, each installable independently.

| Plugin | What it does | Install id |
|---|---|---|
| [subagent-context](plugins/subagent-context/README.md) | Reports each subagent's context size to the orchestrating agent; warns before an overloaded agent gets re-tasked | `subagent-context@claude-plugins` |
| [cross-session-send-guard](plugins/cross-session-send-guard/README.md) | Warns before, and can gate, messaging a large idle peer session — a cold wake replays its whole transcript at full price | `cross-session-send-guard@claude-plugins` |
| [compact-manager](plugins/compact-manager/README.md) | Warns the main session as its own context fills; wake packet + reorientation across compaction; opt-in managed mode types `/compact` into your tmux pane at verified-idle moments | `compact-manager@claude-plugins` |

```
/plugin marketplace add msshives-gif/claude-plugins
/plugin install <name>@claude-plugins
```

Every plugin lives under [`plugins/`](plugins/); shared measurement
core is vendored (see `tools/sync-core.py`); layout rationale in
[docs/SUITE.md](docs/SUITE.md). `tools/status.py` prints a one-screen
readout of every plugin's effective config (default/file/env per
knob), hook wiring, and live watchers. History: this repo began life as
`subagent-gauge`, then `subagent-context` (single plugin) — GitHub
redirects the old slugs.

MIT.
