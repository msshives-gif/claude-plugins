---
description: Show compact-manager mode, watchers, and per-session context usage
---

Report the current compact-manager state, briefly:

1. Config: read `~/.claude/compact-manager.json` (env
   `COMPACT_MANAGER_*` overrides it). Report the mode, the global
   `context_window` if set, and any per-model `models` overrides.
2. Watchers: run `"${CLAUDE_PLUGIN_ROOT}/bin/compact-manager" status`.
   Report each watcher's session id, pid, state, and `live` flag.
   Needs-human-attention (highlight first): any `LATCHED`,
   `SUBMISSION_UNCERTAIN`, or `CLEANUP_REQUIRED` state, and any row
   with `live` false whose state is not `WATCHER_RETIRED`. Read the
   flags conservatively, matching how they are computed: `live` false
   is definitive (the lease is provably dead), but `live` true only
   means the lease could not be proven dead — a malformed lease also
   reads as live, so `live: true` with `pid: null` is itself a
   needs-attention row, and `state` is the journal's conservative
   recovery mapping (default `WATCHER_READY`), not a heartbeat.
   Transient `reason` values on a healthy watcher (e.g. deferrals like
   `foreground_lost` or `pane_in_mode`) are normal — mention, don't
   alarm.
3. Usage: for each file under `~/.claude/compact-manager/state/` (or
   the configured `state_dir`) modified in the last 24 hours, report
   the session id, `model`, `current` and `peak` tokens, and `current`
   as a percentage of that session's window — resolved the way the
   plugin does: the config's `models` override whose key is the
   LONGEST substring match against the state file's `model`, else the
   config's global `context_window`, else 200000. The most recently
   modified file is
   almost certainly the current session — mark it.

Present a short table plus one line of interpretation, e.g. "this
session is at ~40% of its window; watcher attached, READY, and live."
