# Changelog — compact-manager

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
`uninstall.sh` (boundary-safe, sibling-sparing, tested). 52 tests.

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
