# cross-session-send-guard

Warns before — and can gate — a `SendMessage` to a large idle peer
Claude Code session.

## The problem

Sessions on one machine can message each other. Waking a peer replays
its whole transcript; once its prompt cache has expired, that's a
full-price cold read of everything the peer ever did. Session age tells
you nothing about size — and a freshly forked session even
under-reports briefly (its transcript starts modest and catches up as
it runs), so "started 1m ago" proves nothing. The motivating incident:
a quick deconfliction ping to a "started 1m ago" peer that was actually
a ~100k-token fork, billed as a cold read.

## What it does

One `PreToolUse` hook on `SendMessage`. When the target resolves to a
**live peer session** (via the harness's `~/.claude/sessions` registry;
in-process subagents never appear there — that's the sibling
[subagent-context](../../README.md) plugin's domain):

- below `warn_tokens`: silent;
- at/above `warn_tokens`: injects a warning with the measured size,
  cold/warm state, and an estimated cold-read cost — plus the cheap
  alternatives (write a file/handoff instead; batch pings into one
  send);
- at/above `block_tokens` **and** cold, from the root session: the send
  also needs your confirmation. You can always say yes. (Subagents get
  the warning only; in headless runs the confirmation acts as a
  refusal.)

Unresolvable targets are silent; every path fails open. Address forms
understood: the peer's registry name, `name [ref]`, and
`uds:/…/cc-socks/<pid>.sock`. Liveness requires the registry's recorded
process start time to match the live process (a recycled pid never
counts). If two DIFFERENT live sessions share the target name, the
guard stays silent — with or without a `[ref]` disambiguator (the ref
isn't derivable from disk, and gating a guessed session would be worse
than no warning); two pids for one resumed session are fine.

## Install

```
/plugin marketplace add msshives-gif/subagent-context
/plugin install cross-session-send-guard@subagent-context
```

Manual: `./scripts/install.sh` (merges the one hook into
`~/.claude/settings.json`; `./scripts/uninstall.sh` reverses it).
Restart running sessions — hooks are read at startup.

## Configuration

Env `CROSS_SESSION_SEND_GUARD_<NAME>` or a key in
`~/.claude/cross-session-send-guard.json` (env wins;
`CROSS_SESSION_SEND_GUARD_CONFIG` points elsewhere):

| Knob | Default | Meaning |
|---|---|---|
| `enabled` | `true` | Master switch. |
| `warn_tokens` | `50000` | At/above this measured peer size, warn. |
| `block_tokens` | `150000` | At/above this AND cold: root-session confirmation. `0` = off. |
| `cache_ttl_seconds` | `3600` | Transcript idle longer than this = cold. |
| `usd_per_mtok` | `3.00` | Price for the *estimated* cost line only. |
| `system_message` | `true` | Also show warnings to you in the UI. |
| `measure_max_bytes` | `50000000` | Transcripts bigger than this warn as size-unknown instead of being parsed (hook-budget cap). |

Defaults are lower than subagent-context's on purpose: a cold wake
pays full input price on the whole transcript.

## Interplay with subagent-context

Both plugins hook `PreToolUse` on `SendMessage` and consult different
stores (subagent = recorded in-process spawn; peer = live registry
entry with a real process). Normally exactly one speaks. The stores
are not provably exclusive — a name could simultaneously be a spawned
subagent and a live peer session — but in that overlap the outputs
compose safely: warnings are additive and the harness takes the most
restrictive permission decision.

## Limitations

- Built on undocumented internals (the sessions registry and transcript
  formats). A Claude Code change degrades this to silence, never to
  blocking.
- Measured size is as of the peer's last completed model call; a
  just-forked idle peer under-measures for its first turn or two.
- Transcripts over ~50MB aren't parsed (budget); they warn as
  size-unknown and still gate when cold.
- A peer running a Claude Code version without messaging sockets can be
  measured but can't receive messages anyway; the harness rejects the
  send regardless of this guard.

## Uninstall

`/plugin uninstall cross-session-send-guard` or
`./scripts/uninstall.sh`.

MIT.
