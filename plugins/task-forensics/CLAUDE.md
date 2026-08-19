# task-forensics

Claude Code plugin: a PreToolUse hook (`hooks/wrap.py`) rewrites
background Bash commands to run under `bin/sigwrap.py`, which logs the
sender of any TERM/INT/HUP/QUIT before forwarding it to the task.

Simple code and simple documentation is best; complexity and big words
are the enemy. Prefer the boring obvious shape.

- Both layers MUST fail open: the hook emits nothing and exits 0 on any
  doubt; the wrapper execs the command unwrapped if its own setup
  fails. A forensics tool must never break or block the task it
  observes.
- The hook must never set `permissionDecision` — wrapping must stay
  visible to the normal permission flow (verified live: rules match the
  rewritten command). Auto-approving background commands would be a
  security regression, not a UX fix.
- Exit-status fidelity is part of the contract: wrapper exits with the
  child's code, `128+N` for death by signal N — the harness's task
  bookkeeping reads it.
- Uninstall markers must match only this plugin's own hook path
  (`tests/test_install_scripts.py`); siblings share the repo directory
  name.
- Test discipline: `python3 -m unittest discover tests`. The
  updatedInput hook contract is an undocumented Claude Code internal
  (verified against the 2.1.235 bundle); if the observed shape changes,
  keep degrade-to-silence and update tests.
