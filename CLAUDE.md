# subagent-gauge

Claude Code plugin: three hooks (observer/drain/guard) that report
subagent context sizes to the orchestrating agent. Shared logic lives in
`hooks/sgauge_common.py`; hook entry points stay thin and MUST fail open
(exit 0 on every path — a gauge must never block agent work).

- The transcript JSONL / meta.json formats are undocumented Claude Code
  internals. Never make parsing stricter without keeping the
  degrade-to-no-report behavior; update `tests/` fixtures when the
  observed format changes.
- `SubagentStop` `additionalContext` continues the STOPPING SUBAGENT,
  not the parent — do not reintroduce relay-through-the-agent; see
  docs/DESIGN.md "Rejected alternatives" before touching delivery.
- Test discipline: measurement core and state IO are unit-tested
  (`python3 -m unittest discover tests`) — keep new parsing logic
  covered there. Hook wiring is verified by live headless-session tests
  (see DESIGN); install scripts are covered by running them against a
  temp settings file, not unit tests.
