---
description: Set this session's compaction thresholds (trigger/soft/hard/window)
argument-hint: "trigger=60% soft=50% hard=55% window=500000 (any subset, or --clear)"
---

Apply the user's requested per-session compaction thresholds:
$ARGUMENTS

1. Locate the compact-manager CLI. The path in the later steps
   normally reads as an absolute path (script installs write it in).
   If it instead still says `${CLAUDE_PLUGIN_ROOT}` literally — plugin
   installs may not substitute command markdown — find the CLI file
   itself: `find ~/.claude/plugins -maxdepth 8 -type f -name
   compact-manager -path "*/bin/*" 2>/dev/null` (this handles
   versioned cache layouts, where a directory merely NAMED
   compact-manager has no bin/). If several paths match, use the most
   recently modified (`ls -t`).
2. Determine this session's id. The start-of-session compact-manager
   status line bakes the exact `override <session-id> …` command —
   prefer that id. Failing that, run
   `"${CLAUDE_PLUGIN_ROOT}/bin/compact-manager" overview` and use the
   `>`-marked row's session id ONLY if its `updated` age is
   seconds-old (`>` is recency, not identity); if it is older, ask the
   user rather than guessing.
3. Translate the user's request into the CLI's key=value form — keys
   `trigger`, `soft`, `hard` (percentages like `60%` or fractions like
   `0.6`) and `window` (integer tokens) — and run:
   `"${CLAUDE_PLUGIN_ROOT}/bin/compact-manager" override <session-id>
   key=value […]`. To undo, pass `--clear` instead of assignments;
   with no assignments at all the command just shows the current
   state. Relay the command's own output verbatim — it prints the
   stored override file and the resulting effective thresholds; do not
   re-derive them.
4. If the user's request doesn't map cleanly onto these keys (e.g. a
   global or per-model change, or another session's), say so and point
   at `~/.claude/compact-manager.json` instead of forcing it through
   this per-session mechanism.
