# Changelog — compact-manager

## Unreleased

- Overview: cleanly retired watchers now age out of the watchers list
  24h after their last journal write (same window the sessions list
  uses) instead of lingering until the 7-day TTL reap. Rows carrying
  hazard flags (DEAD-LEASE, ATTENTION, MALFORMED-LEASE) never age
  out of the display. Fixes stale multi-day RETIRED rows in the CLI
  overview and the ccam dashboard panel that relays it. A retired row
  whose leftover lease file is unreadable (status_rows drops
  unparseable leases, so it carries no flag) is surfaced as DEAD-LEASE
  and kept — the file still blocks adoption (Sol audit).
- Overview: per-session count of compactions the manager itself fired
  (`cm` column in the text table, `cm_compacts` in `overview --json`).
  Derived read-only from the watcher journal — attempts with own-nonce
  proof: ACKED, or the new `own_packet_proof` journal field stamped on
  a fast completion's BOUNDARY_CONFIRMED (packet and boundary inside
  one poll — Sol round-1 blocker) and on a retry's CLEANUP_REQUIRED
  whose late own packet proved the first submission fired (Sol
  round-2). Deduped by attempt_id so retries/recovery replays count
  once; deferred-foreign attempts resolved by native compactions do
  not count, and hostile non-string attempt ids neither crash nor
  count. `null`/blank = no retained watcher journal, distinct from a
  watched session at 0. The managed TTL reaper now defers a dead
  watcher's journal cleanup while the session's state file is inside
  the overview's 24h window, so a displayed count can't flip to null
  (Sol round-1) — and a session lease orphaned by pane reuse during
  that deferral stays reclaimable (zero-pane-match reap, Sol
  round-2). Round-3 hardening: a tokenless (malformed) lease is
  skipped entirely instead of deleting artifacts and KeyErroring out
  of acquisition; own proof survives a competing R6′ abort and is
  re-classified once at watcher recovery; journal writes persist
  `own_packet_proof` only for the exact True (no bool() laundering);
  a present-but-unreadable journal reports null, not 0 (null/blank =
  "count unavailable", covering absent AND unreadable journals); and
  the pruner sweeps lease-less journal/scan/request files past the
  TTL once their session ages off the overview (they previously
  accumulated forever), re-proving each file's identity (inode +
  mtime) at the last instant so a concurrent atomic replace can't
  lose a fresh file (Sol round-4).

- Turn-boundary lane: the watcher can now type a requested (or
  hard-threshold) `/compact` at a foreground turn boundary even while
  background tasks keep the pane busy — the failure that let a
  marathon session starve a written handoff for 28 minutes until
  native auto-compact fired stale (live case fbeb0bf1, 2026-08-17).
  Proof comes from a paired activity marker (UserPromptSubmit writes
  `running-<sid>.json`, Stop writes `ended-<sid>.json`, paired by the
  shared `prompt_id`; two lock-free atomic files, compared at watcher
  read time, so a stale Stop can never authorize a newer turn) plus a
  structured SGR-aware pane parse: bottom-anchored composer, dim
  ghost-suggestion text counts as empty, typed text and modals veto,
  unknown escapes veto. The strict whole-pane-idle lane keeps its
  typing contract for ordinary below-hard threshold attempts, with
  two deliberate changes: an affirmative "running" marker or a modal
  layout now vetoes it too (mid-generation panes can look
  byte-identical to idle), and its input-opportunity retries use the
  fast poll cadence instead of exponential backoff. Supporting changes: reason-aware defer scheduling (input
  opportunities retry at `managed_poll_s`; only structural failures
  back off exponentially — the old blanket 30→300s backoff was half
  the starvation), `trigger_source` persisted through the journal,
  dynamic promotion to the lane when usage crosses hard mid-retry, a
  pending attempt pins the outer loop to the fast poll cadence, a new
  ended marker makes a deferred attempt due immediately, and a
  non-latching STARVATION_ALERT (journal row + ATTENTION flag in
  status/overview + best-effort tmux display-message) after two
  minutes of blocked eligibility. Live-pinned UI facts (fixtures in
  tests/fixtures/): suggestions render ESC[2m-dim per word and are
  byte-identical to typed text once stripped; `esc to interrupt`
  flickers off mid-generation (the R2 veto alone was never a reliable
  turn discriminator); modal selection rows use "❯ "+ASCII-space.
  Post-commit audit round (fresh Sol + 2 Opus lanes) hardened the
  first cut: extended-color SGR params (38;5;n / 38;2;r;g;b) no
  longer misread as dim; the two-file marker read is linearized by a
  coherence re-read and is three-valued (an unpaired running marker
  is affirmative turn-in-flight evidence and vetoes both lanes);
  marker identity requires a matching transcript device/inode and
  ended >= running ordering; the pre-typing revalidation captures
  before its final marker read; SUBMITTED/ACKED attempts keep the
  fast poll cadence; the modal pattern requires indentation so a
  history echo of a prompt starting "1. " can't starve the watcher;
  starvation timing runs from first eligibility. Documented
  limitations: co-installed BLOCKING Stop hooks can fake a turn end
  (bounded: the typed /compact queues to the real boundary); modal
  coverage is the numbered-selection shape.
  Live-suite catches, fixed: an explicit model request now overrides
  a THRESHOLD latch (still_above_rearm_band) instead of starving until
  the re-arm band fills; the harness allows the Bash tool so the s14
  topology can actually run. (s16's queued-/compact pin
  stayed inconclusive in that run — the turn ended before injection —
  and remains an informational scenario.)
  Design: GPT-5.6 Sol rounds 1-4 (2026-08-18), two-file marker
  protocol green-lit round 3, final sign-off round 4; Opus + Sol
  audit rounds same date.

- Per-session threshold overrides, changeable mid-flight: a new
  `override` CLI subcommand writes a validated
  `<state_dir>/overrides/<sid>.json` (`trigger`/`soft`/`hard`/
  `window`, any subset, `--clear` resets, sid prefixes accepted). The
  advisor merges it into its effective thresholds (stamps stay BASE —
  pre-override — so nothing override-flavored lingers after
  `--clear`), a RUNNING watcher re-reads one consistent snapshot per
  decision so a change lands within one poll, and both readouts
  overlay the file over stamps and derivation — a change (or a clear)
  displays truthfully immediately.
  Values are range-validated (fractions in (0, 1], window 10k–1e9,
  soft kept ≤ hard) but not clamped beyond that: it is the human's
  lever, and the managed-mode
  start-of-session status line advertises the exact command (real
  session id baked in) for use when the user asks. New `set` slash
  command translates a spoken request into the CLI call. Override
  files age out with the session's other state (TTL reaper).

- Watchers retire on session-id rotation instead of babysitting the
  dead id: `/clear` and in-app `/resume` keep the claude process but
  rotate its session id, which previously left the watcher holding
  its lease against a frozen transcript until the 24h deadline while
  the live session went unwatched. `validate_binding` now retires
  with `reason=session_rotated` on positive proof only — a readable
  registry entry for the bound pid, same start time, carrying a
  different valid session id; missing/malformed registry entries
  never retire a healthy watcher. Retirement is journaled from every
  exit path (including the initial catch-up loop). SessionStart now
  also fires on `clear` (new hook matcher), so the fresh session id
  immediately hears that no watcher holds it, with the attach
  command. Pinned live by suite scenario s13 (/clear rotation →
  final journal record `WATCHER_RETIRED`/`session_rotated` + lease
  released); in-app `/resume` rotation shares the code path but has
  no live scenario yet.
- Operator stops journal `reason=stop_requested` (previously the
  retirement record inherited the incidental last-tick status, e.g.
  `below_threshold`), from both the tick loop and the catch-up loop.
- Session rows display the thresholds each session ACTUALLY runs
  with: the advisor stamps its effective (env-honoring,
  per-model-merged) window/soft/hard/trigger into the state file
  (`eff_*` fields, validated on load); the overview prefers stamps
  over re-deriving from its own config, which cannot see another
  session's env overrides. Text table gains a `trig` column; JSON
  rows gain `soft_pct`/`hard_pct`/`trigger_pct`. Unstamped or
  garbage stamps fall back to the readout's own derivation.

- Overview session rows carry a liveness verdict: `alive` column in
  the text readout (`live` / `GONE` / `?`), `session_live`
  (true/false/null) in `overview --json`. Proof-based (sessions
  registry entry + `/proc` pid + non-zombie state + start-time match,
  mirroring lease-reclaim's "declaring death needs proof"), and dead
  is only asserted from a COMPLETE registry scan — any unjudged entry
  (unreadable file, unreadable `/proc` stat, scan bound) degrades the
  absent sessions to unknown, never to dead; machines with no `/proc`
  or registry read as unknown throughout. Distinguishes "idle but
  running" from "exited, state lingering out the 24h window".

## 0.3.0 — 2026-08-16

Discoverability: managed mode's one invisible state — configured on,
hooks firing, but no watcher adopted for the session — is now surfaced
instead of discovered at the 80% line.

- `setup` slash command: guided first-run mode choice + window
  overrides + command introduction (install itself stays inert by
  design; merges into an existing config, never clobbers).
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
