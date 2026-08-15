# Changelog — compact-manager

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
