---
description: Stop the compact-manager watcher for this session
---

Stop the managed-mode watcher attached to the current Claude Code
session.

1. Locate the compact-manager CLI. The path in the later steps
   normally reads as an absolute path (script installs write it in).
   If it instead still says `${CLAUDE_PLUGIN_ROOT}` literally — plugin
   installs may not substitute command markdown — find the CLI file
   itself: `find ~/.claude/plugins -maxdepth 8 -type f -name
   compact-manager -path "*/bin/*" 2>/dev/null` (this handles
   versioned cache layouts, where a directory merely NAMED
   compact-manager has no bin/). If several paths match, use the most
   recently modified (`ls -t`).
2. Identify this session's id. If your environment exposes one
   directly (e.g. a `CLAUDE_SESSION_ID` env var, or a session id in a
   path you already know, like your transcript or scratchpad path),
   prefer that — it is authoritative. Otherwise use the overview
   heuristic in step 3.
3. Heuristic fallback: run
   `"${CLAUDE_PLUGIN_ROOT}/bin/compact-manager" overview` and read the
   `updated=…s-ago` ages. Your own session's state file was touched by
   your PREVIOUS tool call, so expect the `CURRENT>>` row to be only
   seconds old. If the top row is not seconds-old, or if two rows are
   both under ~2 minutes old (another session is active in parallel),
   do NOT guess: show the candidate rows to the user and ask which to
   stop.
4. Run `"${CLAUDE_PLUGIN_ROOT}/bin/compact-manager" stop <session-id>`
   and report the result. If no watcher held this session, say so —
   that is a normal outcome, not an error.
