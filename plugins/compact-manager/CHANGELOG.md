# Changelog — compact-manager

## 0.3.0 — 2026-08-16

Discoverability: managed mode's one invisible state — configured on,
hooks firing, but no watcher adopted for the session — is now surfaced
instead of discovered at the 80% line.

- Slash commands `attach` / `detach` / `status` (plugin namespace
  `/compact-manager:<name>`; `install.sh` installs substituted,
  marker-tagged copies beside the settings file as
  `/compact-manager-<name>` — plugin-context `${CLAUDE_PLUGIN_ROOT}`
  is never substituted for script installs, so symlinks would have
  resolved to `/bin/compact-manager`. Existing unmarked files are
  never overwritten; `uninstall.sh` removes only marker-carrying
  files or legacy symlinks resolving into this clone, canonicalizing
  via python3 rather than `readlink -f`).
- Second-round verification hardening: attachment proof rejects
  future heartbeats beyond 1s of slop (same-machine writes — further
  future is malformed, not skew); the MALFORMED-LEASE flag covers
  pid-malformed shapes on surfaced live rows (what it cannot see is
  documented); overview rows carry an `updated=…s-ago` age column
  with future mtimes surfaced as untrustworthy, detach's
  current-session heuristic primes its own state row first,
  age-verifies, prefers an environment-provided session id, and asks
  rather than guessing on any ambiguity; the CLI-discovery fallback
  finds the CLI file itself (versioned plugin-cache layouts have
  directories named compact-manager with no bin/). Token values
  outside [0, 1e11] render as zeros per-row.
- Default `context_window` is now 1,000,000 (the Claude 5 standard;
  was 200k). Deliberate failure direction: unknown new model names
  must never compact early at a stale small default — a legacy 200k
  model just misses advisories and falls back to native compaction.
  Override legacy models via `models`.
- `bin/compact-manager overview`: deterministic runtime readout
  (watcher rows with ATTENTION/DEAD-LEASE/MALFORMED-LEASE flags,
  per-session usage percentages, current-session marker). The status
  slash command relays it instead of re-deriving logic in prose, and
  all three command bodies locate the CLI defensively — placeholder
  substitution in command markdown is version/install-form-dependent.
- SessionStart wiring gains `startup` and `resume` matchers: in
  managed mode the hook injects one watcher-status line (attached with
  pid, or the attach command to run). Attachment needs positive proof
  — fresh lease heartbeat or verified-alive pid — never
  lease_is_live()'s ambiguity-counts-as-live reclaim default.
  Advisory/off stay silent; non-dict payloads and non-string session
  ids too. (This paragraph folds in a same-day cross-family review:
  2 user-file deletion paths, the plugin-variable break, the
  false-attached path, and fail-open gaps — all fixed pre-release.)

## 0.2.0 — 2026-08-15

Released after two two-family code-audit rounds (all findings folded)
and a 20/20 live regression run against the real CLI over tmux
(tools/live-managed/run.sh — gated, run manually).

Layer 2: `managed` mode. A per-session watcher daemon (started
explicitly with `bin/compact-manager start|adopt`; installing the
plugin alone still changes nothing) tails the session transcript with
its own durable cursor and types `/compact` into the session's tmux
pane at verified-idle moments — behind a six-rail ladder (identity
conjunction on the pane/tty/foreground-process walk, empty-composer
signature, stability window, type-without-Enter, exact composer
verify, full revalidation before Enter) that aborts on any doubt and
never clears the composer. Injection is confirmed end-to-end: a nonce
round-trips through the PreCompact wake packet and completion
requires the transcript's compaction boundary; a missing ack gets
exactly one retry, then a safety latch that never re-injects and
clears only via operator `resolve` or a real compaction. Ownership is
a session+pane lease pair under one flock'd transaction file with
10s heartbeats (two watchers can never share a pane; reclaim needs a
stale heartbeat AND a dead pid+starttime); every action is journaled
fsync-first, and crash recovery maps each journal tail to its
conservative state (typed-but-unconfirmed → SUBMISSION_UNCERTAIN /
CLEANUP_REQUIRED, resolved only by a human). Triggers: threshold
(`managed_trigger_pct`, default `hard_pct`) or a model-written
request file advertised by the advisory. `start` runs claude with no
shell under the pane; `adopt` targets an existing (even attended)
pane and requires `--attended` acknowledging the worst-case-race
residual. Linux/WSL only; advisory mode unaffected. New `managed_*`
config knobs, all clamped. Hooks, install scripts, and the plugin's
hook surface are unchanged.

## 0.1.1 — 2026-08-15

Post-release audit round (live M2 verification + two fresh-context
auditors, two model families). Fixes: a compaction boundary now resets
the measured `current` to `compactMetadata.postTokens` (the advisor
used to warn a freshly-compacted session that its context was still
full — caught live and by both auditors); downward hysteresis steps
hard→soft→none instead of emitting a spurious soft advisory on a
purely downward move; per-model overrides get the same sanity clamps
as globals; zero/garbage config values restore defaults instead of
misbehaving quietly; transcript scanning is byte-budgeted with a
giant-line escape; the handoff excerpt cap is a true byte cap and the
delivery cap tracks the knob; the state file is field-validated on
load (self-recovery from corruption); lock stale-break is
rename-atomic and release is owner-aware; `state_ttl_days` is now
real (daily best-effort reaper). New: manual `scripts/install.sh` /
`uninstall.sh` (boundary-safe, sibling-sparing, tested).

Re-verify round (two fresh auditors, two families) hardened the fixes
themselves: `discard_to_newline` persists across hook processes and
clears on file replacement; numeric state fields require exact
non-negative ints (a float offset used to wedge scanning for good);
the reaper only touches sentinel-marked dirs, skips symlinked
subdirs, and deletes only plugin-named regular files (lstat ages);
installer presence-detection is boundary-safe (a `.backup` lookalike
no longer blocks the real install); backups get collision-proof
names; the stdin cap is a true byte cap; the lock stale-break
re-checks what it displaced (restoring a displaced live lock via
O_EXCL, never clobbering a later acquirer; residual overlap after a
>10s-stale break is documented, worst case one lost advisory
update); huge-int timestamps no longer wedge state via
math.isfinite's OverflowError; the pruner only deletes stems
path_component could generate and skips junctions/symlinked files;
installer matching is boundary-safe on both sides. A third
two-family pass on the fix diff found no production defect. 66
tests.

## 0.1.0 — 2026-08-15

Layer 1 (advisory). Incremental own-transcript measurement with
inode/shrink reset guard; soft/hard advisories with hysteresis and
per-model windows; wake packet at PreCompact (mechanical facts +
model-written handoff excerpt, freshness-checked); reorientation
delivered at-most-once via PostToolUse/UserPromptSubmit with an
opportunistic SessionStart(compact) copy. Default mode `off` —
installing is inert. Grounded in live spikes S1-S5
(docs/spikes/compact-manager.md): SessionStart compact injection works
interactively; native auto-compact measured firing at ~1.09× the
nominal window; /compact custom instructions round-trip into the
PreCompact payload. Layer 2 (managed tmux injection) not built;
`managed` mode reserved.
