---
description: Show compact-manager mode, watchers, and per-session context usage
---

Report the current compact-manager state.

1. Locate the compact-manager CLI. The path in the later steps
   normally reads as an absolute path (script installs write it in).
   If it instead still says `${CLAUDE_PLUGIN_ROOT}` literally — plugin
   installs may not substitute command markdown — find the CLI file
   itself: `find ~/.claude/plugins -maxdepth 8 -type f -name
   compact-manager -path "*/bin/*" 2>/dev/null` (this handles
   versioned cache layouts, where a directory merely NAMED
   compact-manager has no bin/). If several paths match, use the most
   recently modified (`ls -t`).
2. Run `"${CLAUDE_PLUGIN_ROOT}/bin/compact-manager" overview` and
   relay its output verbatim in a code block. The logic (window
   resolution, attention flags, current-session marker) lives in the
   CLI, not here — do not re-derive it.
3. Add ONE line of interpretation for the CURRENT>> row: its usage
   percentage and whether a watcher holds it. (`CURRENT>>` is a
   recency heuristic — check its `updated=…s-ago` age; seconds-old
   means it is almost certainly the invoking session.) Any row flagged
   `ATTENTION`, `DEAD-LEASE`, or `MALFORMED-LEASE` needs the human's
   eyes — lead with those. (Raw JSON, if needed: the `status`
   subcommand.) Note: the start-of-session status line uses stricter
   positive-proof attachment than the overview's conservative `live`
   flag, so the two can disagree on a malformed lease. The
   `MALFORMED-LEASE` flag catches pid-malformed shapes on rows that
   surface as live; it cannot see an empty lease file (no row is
   produced at all), a dead lease's pid shape (flagged `DEAD-LEASE`
   instead), or fields the row does not carry (token, start time,
   heartbeat).
