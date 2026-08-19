# Changelog

## 0.1.0 — 2026-08-19

Initial release: PreToolUse hook wraps background Bash tasks in
`bin/sigwrap.py`, which logs the sender (pid/uid/cmdline/parent chain)
of any TERM/INT/HUP/QUIT to `~/.claude/task-forensics/log.jsonl`,
forwards the signal, and preserves the task's output and exit status.
Fail-open on every path; `TASK_FORENSICS_DISABLE=1` escape hatch.
