---
description: First-run setup — pick a compact-manager mode and learn the commands
---

Walk the user through turning compact-manager on. Be brief — this is
two decisions, not a tutorial.

1. Locate the compact-manager CLI. The path in the later steps
   normally reads as an absolute path (script installs write it in).
   If it instead still says `${CLAUDE_PLUGIN_ROOT}` literally — plugin
   installs may not substitute command markdown — find the CLI file
   itself: `find ~/.claude/plugins -maxdepth 8 -type f -name
   compact-manager -path "*/bin/*" 2>/dev/null` (this handles
   versioned cache layouts, where a directory merely NAMED
   compact-manager has no bin/). If several paths match, use the most
   recently modified (`ls -t`).
2. Read the current state: `~/.claude/compact-manager.json` (may not
   exist; mode defaults to `off`),
   `"${CLAUDE_PLUGIN_ROOT}/bin/compact-manager" overview`, and
   `env | grep '^COMPACT_MANAGER_'` — environment variables OVERRIDE
   the file, so if any are set, warn the user that editing the file
   alone may not change effective behavior in sessions carrying those
   variables. If a mode is already set, say so and ask whether the
   user wants to change it before continuing.
3. Explain the choice in about three sentences, then ask which mode
   they want:
   - **advisory** — pure hooks, any platform, zero risk: the model
     is warned crossing 70% of the context window (and again, firmer,
     at 80% — whichever thresholds are crossed) so it can save its
     working state, and after any compaction the saved state is
     injected back so the session resumes oriented. The user still
     runs `/compact` themselves.
   - **managed** — advisory plus a per-session watcher that types
     `/compact` into the session's tmux pane at a verified-idle
     moment. Linux/WSL + tmux only; each session must be attached
     explicitly.
   - (**off** stays available — everything inert.)
4. Ask one follow-up: do they run models with a context window other
   than 1M? The default `context_window` is 1,000,000 (the Claude 5
   standard); smaller/legacy models need a `models` override, e.g.
   `{"haiku": {"context_window": 200000}}` (substring match, longest
   wins). Getting this wrong is asymmetric: too small warns — and in
   managed mode compacts — far too early; too large just means
   advisories never fire.
5. If the user is switching AWAY from managed (to advisory or off):
   running watchers snapshot their config at spawn and will keep
   typing `/compact` regardless of the file change. Check `overview`
   for live watchers and stop each one
   (`"${CLAUDE_PLUGIN_ROOT}/bin/compact-manager" stop <session-id>`,
   with the user confirming the list) — or tell the user explicitly
   that existing watchers stay active until stopped or their 24h
   deadline.
6. Write `~/.claude/compact-manager.json` with the chosen mode (and
   any overrides). If the file exists, MERGE — show the user the
   exact final JSON before writing, and never drop keys they already
   set. The hooks re-read the file each time, so it takes effect
   immediately.
7. Close with the command map, one line each: managed sessions attach
   with the attach command (`/compact-manager:attach` or
   `/compact-manager-attach`); `…status` shows mode, watchers, and
   per-session usage (or run `bin/compact-manager overview` directly
   — no model turn); `…detach` stops this session's watcher. If they
   chose managed and this session is in tmux (`$TMUX_PANE` non-empty),
   offer to attach right now.
