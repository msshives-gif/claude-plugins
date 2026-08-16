---
description: Show compact-manager mode, watchers, and per-session context usage
---

Report the current compact-manager state, briefly:

1. Config: read `~/.claude/compact-manager.json` (env
   `COMPACT_MANAGER_*` overrides it). Report the mode and any per-model
   `context_window` overrides.
2. Watchers: run `"${CLAUDE_PLUGIN_ROOT}/bin/compact-manager" status`.
   Report each watcher's session id, pid, and state. Any watcher in
   `ALERT_DELIVERY`, or with a non-null `reason`, needs human attention
   — highlight it first.
3. Usage: for each file under `~/.claude/compact-manager/state/` (or the
   configured `state_dir`) modified in the last 24 hours, report the
   session id, `model`, `current` and `peak` tokens, and `current` as a
   percentage of that model's window (per the config's `models`
   overrides; default 200000). The most recently modified file is
   almost certainly the current session — mark it.

Present a short table plus one line of interpretation, e.g. "this
session is at ~40% of its window; watcher attached and READY."
