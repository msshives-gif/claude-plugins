---
description: Show compact-manager mode, watchers, and per-session context usage
---

Report the current compact-manager state.

1. Locate the compact-manager CLI. The path in step 2 normally reads
   as an absolute path (script installs write it in). If it instead
   still says `${CLAUDE_PLUGIN_ROOT}` literally — plugin installs may
   not substitute command markdown — recover the plugin root with
   `find ~/.claude/plugins -maxdepth 6 -type d -name compact-manager
   2>/dev/null` (marketplace layout) and use
   `<plugin-root>/bin/compact-manager`.
2. Run `"${CLAUDE_PLUGIN_ROOT}/bin/compact-manager" overview` and
   relay its output verbatim in a code block. The logic (window
   resolution, attention flags, current-session marker) lives in the
   CLI, not here — do not re-derive it.
3. Add ONE line of interpretation for the CURRENT>> row: its usage
   percentage and whether a watcher holds it. Any row flagged
   `ATTENTION`, `DEAD-LEASE`, or `MALFORMED-LEASE` needs the human's
   eyes — lead with those. (Raw JSON, if needed: the `status`
   subcommand.) Note: the start-of-session status line uses stricter
   positive-proof attachment than the overview's conservative `live`
   flag, so the two can disagree on a malformed lease — the overview
   flags exactly that case as `MALFORMED-LEASE`.
