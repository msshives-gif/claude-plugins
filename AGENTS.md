# subagent-context

Claude Code plugin: three hooks (observer/drain/guard) that report
subagent context sizes to the orchestrating agent. Shared logic lives in
`hooks/subagent_context.py`; hook entry points stay thin and MUST fail
open (exit 0 on every path — a reporting tool must never block agent
work).

Simple code and simple documentation is best; complexity and big words
are the enemy. Prefer the boring obvious shape; build machinery only
when real use forces it (see DESIGN.md "Deliberately not built").

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
  (see DESIGN); install scripts are covered by
  `tests/test_install_scripts.py`, which runs them against a temp
  settings file — uninstall markers must match only this plugin's own
  hook paths (never the repo name; siblings in plugins/ share it).
