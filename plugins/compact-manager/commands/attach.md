---
description: Attach the managed-mode watcher to this session's tmux pane
---

Attach the compact-manager watcher to the current Claude Code session so
`/compact` gets typed automatically at the configured threshold (managed
mode).

1. Locate the compact-manager CLI. The path below normally reads as an
   absolute path (script installs write it in). If it instead still
   says `${CLAUDE_PLUGIN_ROOT}` literally — plugin installs may not
   substitute command markdown — recover the plugin root with
   `find ~/.claude/plugins -maxdepth 6 -type d -name compact-manager
   2>/dev/null` (marketplace layout) and use
   `<plugin-root>/bin/compact-manager`.
2. Check the pane: run `echo "$TMUX_PANE"` in your shell tool. If it is
   empty, this session is not running under tmux — managed mode requires
   tmux. Report that and stop.
3. Run:
   `"${CLAUDE_PLUGIN_ROOT}/bin/compact-manager" adopt -t "$TMUX_PANE" --attended`
4. Interpret the result:
   - Success (prints `watcher pid=… run_token=…`): confirm with the
     `overview` subcommand and report this session's watcher state.
   - A lease error (`lease_held` or similar): a live watcher already holds
     this session or pane — report that it was already attached, with the
     `overview` output.
   - Any other error: report it verbatim. Do not retry in a loop.
5. If the configured mode is not `managed` (config file
   `~/.claude/compact-manager.json`, env `COMPACT_MANAGER_MODE`), the
   hooks will not cooperate with the watcher: tell the user to set
   `{"mode": "managed"}` there first.
