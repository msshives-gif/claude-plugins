---
description: Stop the compact-manager watcher for this session
---

Stop the managed-mode watcher attached to the current Claude Code
session.

1. Run `"${CLAUDE_PLUGIN_ROOT}/bin/compact-manager" status` and list the
   live watchers.
2. Identify this session's id: the measurement state files under
   `~/.claude/compact-manager/state/` (or the configured `state_dir`)
   update on every tool call, so the most recently modified one is
   almost certainly this session — `ls -t <state_dir>/state/*.json | head -1`.
   If two files were modified within the last couple of minutes (another
   session is active in parallel), show both to the user and ask which
   to stop instead of guessing.
3. Run `"${CLAUDE_PLUGIN_ROOT}/bin/compact-manager" stop <session-id>`
   and report the result. If no watcher held this session, say so —
   that is a normal outcome, not an error.
