---
description: Attach the managed-mode watcher to this session's tmux pane
---

Attach the compact-manager watcher to the current Claude Code session so
`/compact` gets typed automatically at the configured threshold (managed
mode).

1. Check the pane: run `echo "$TMUX_PANE"` in your shell tool. If it is
   empty, this session is not running under tmux — managed mode requires
   tmux. Report that and stop.
2. Run:
   `"${CLAUDE_PLUGIN_ROOT}/bin/compact-manager" adopt -t "$TMUX_PANE" --attended`
3. Interpret the result:
   - Success (prints `watcher pid=… run_token=…`): confirm with
     `"${CLAUDE_PLUGIN_ROOT}/bin/compact-manager" status` and report this
     session's watcher state.
   - A lease error (`lease_held` or similar): a live watcher already holds
     this session or pane — report that it was already attached, with the
     `status` output.
   - Any other error: report it verbatim. Do not retry in a loop.
4. If the configured mode is not `managed` (config file
   `~/.claude/compact-manager.json`, env `COMPACT_MANAGER_MODE`), the
   hooks will not cooperate with the watcher: tell the user to set
   `{"mode": "managed"}` there first.
