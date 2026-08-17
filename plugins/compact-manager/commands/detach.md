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
3. Heuristic fallback. State files are written by a PostToolUse hook
   AFTER each tool result, so on your first tool call of a session
   your own row does not exist yet. Therefore: (a) first run any
   trivial command (e.g. `echo priming`) — completing it writes your
   state file; (b) then run
   `"${CLAUDE_PLUGIN_ROOT}/bin/compact-manager" overview` and read the
   `updated` ages. Expect YOUR row marked `>`, seconds old. (The
   overview prints session ids shortened to 8 characters; `stop`
   accepts an unambiguous prefix, so the short id is usable as-is.)
   Do NOT proceed on the heuristic alone — show the row and ask the
   user to confirm — if ANY of: the top row is not seconds-old, its
   age says `FUTURE-MTIME`, two rows are under ~2 minutes old (a
   parallel active session), or only a single session row exists and
   you could not confirm the id from your environment in step 2.
4. Run `"${CLAUDE_PLUGIN_ROOT}/bin/compact-manager" stop <session-id>`
   and report the result. If no watcher held this session, say so —
   that is a normal outcome, not an error.
