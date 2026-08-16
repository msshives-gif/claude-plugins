---
description: Stop the compact-manager watcher for this session
---

Stop the managed-mode watcher attached to the current Claude Code
session.

1. Locate the compact-manager CLI. The path below normally reads as an
   absolute path (script installs write it in). If it instead still
   says `${CLAUDE_PLUGIN_ROOT}` literally — plugin installs may not
   substitute command markdown — recover the plugin root with
   `find ~/.claude/plugins -maxdepth 6 -type d -name compact-manager
   2>/dev/null` (marketplace layout) and use
   `<plugin-root>/bin/compact-manager`.
2. Run `"${CLAUDE_PLUGIN_ROOT}/bin/compact-manager" overview`. The
   `CURRENT>>` row is this session (the state file updates on every
   tool call — including the one you just made). If a second session's
   row was also updated within the last couple of minutes, show both
   to the user and ask which to stop instead of guessing.
3. Run `"${CLAUDE_PLUGIN_ROOT}/bin/compact-manager" stop <session-id>`
   and report the result. If no watcher held this session, say so —
   that is a normal outcome, not an error.
