# task-forensics

When a Claude Code background task dies unexpectedly, this plugin tells
you **who killed it**.

A `PreToolUse` hook rewrites every background Bash command
(`run_in_background: true`) to run under a small signal-forensics
wrapper, `bin/sigwrap.py`. The wrapper runs the real command as a child
and, while it runs, waits for `SIGTERM`/`SIGINT`/`SIGHUP`/`SIGQUIT`.
Each signal is logged with the sender's pid, uid, cmdline, and parent
chain — then forwarded to the child, so task behavior is unchanged.
When the child exits, its status is logged and the wrapper exits with
the same status (`128+N` for death by signal N), so exit-code reporting
in the harness stays truthful.

The log is JSONL at `~/.claude/task-forensics/log.jsonl` (override dir
with `TASK_FORENSICS_LOG_DIR`; rotated once past 10MB). Three record
types per task: `armed` (wrapper/child pids, pgid, command snippet),
`signal` (the forensics), `exit`.

```json
{"event": "signal", "signo": 15, "si_pid": 12345, "si_uid": 1000,
 "sender": {"pid": 12345, "cmdline": "claude", "ppid_chain": [...]},
 "cmd": "sleep 30", "session": "abc-123", ...}
```

## What it can and cannot catch

- Catchable signals (TERM/INT/HUP/QUIT): full sender attribution.
- `SIGKILL` to the child only: **detected** (`exit` record with
  `killed_by_signal: 9`) but not attributed — SIGKILL is untrappable.
- `SIGKILL` to the whole process group: the wrapper dies as silently as
  the task. Only kernel-level tooling (an auditd rule on the kill
  syscalls, or bpftrace on `signal:signal_generate`) can attribute
  that; this plugin is the no-root tier.

## Permission behavior (read this)

The rewrite happens **before** permission evaluation, so the permission
system sees (and prompts for, and matches allow/deny rules against) the
*wrapped* command: `<python> …/bin/sigwrap.py --session <sid> --
'<your command>'`. Nothing is auto-approved — the hook never sets
`permissionDecision` — but prefix allow-rules like `Bash(npm:*)` will
no longer match your background commands, so you may see extra
prompts. That's the honest tradeoff for tamper-evident wrapping; add an
allow-rule for the sigwrap path only if you accept that it covers any
command it wraps.

## Fail-open guarantees

- The hook prints nothing and exits 0 on any error, non-Bash tool,
  foreground command, already-wrapped command, or missing wrapper file
  — the tool call proceeds unmodified.
- The wrapper `exec`s the command unwrapped if its own setup fails
  (e.g. no `sigtimedwait` on this platform), and logging failures never
  touch the task.
- `TASK_FORENSICS_DISABLE=1` turns the rewrite off without
  uninstalling.

## Install

As a plugin:

```
/plugin marketplace add msshives-gif/claude-plugins
/plugin install task-forensics@claude-plugins
```

Or manually (merges the hook into `~/.claude/settings.json`, or a
settings file passed as `$1`; idempotent, backs up first):

```
plugins/task-forensics/scripts/install.sh
plugins/task-forensics/scripts/uninstall.sh
```

Requires `updatedInput` support in PreToolUse hooks (Claude Code
≥ 2.0.10). Sender attribution reads `/proc`, so it's Linux/WSL-only;
elsewhere the wrapper still passes commands through unchanged.

## Tests

```
python3 -m unittest discover plugins/task-forensics/tests
```

MIT.
