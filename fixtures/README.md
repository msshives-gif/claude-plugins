# Shared fixture corpus

Real-shape, synthetic-content samples usable by every plugin's tests
(locate via `fixtures/locate.py` — see its docstring for the walk-up
import pattern; don't hardcode `parents[N]`).

- `sessions/` — `~/.claude/sessions/<pid>.json` registry samples (field
  set observed on a real machine 2026-08-15; values synthetic).
- `transcripts/` — session transcript JSONL with real-schema `usage`
  rows (`peer_small`, `peer_large` with preliminary rows mixed in).

Deliberately not here yet (land with their verification spikes, from
sanitized real captures, not fabricated):

- a compacted main-session transcript (`isCompactSummary` +
  `compact_boundary` shape) — compact-manager spike S3;
- a forked-session transcript (inherited-context question) and a real
  cross-session SendMessage PreToolUse payload — send-guard spike.

subagent-context's own `tests/fixtures/` predates this corpus and stays
where it is; new plugins use this directory.
