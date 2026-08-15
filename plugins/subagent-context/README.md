# subagent-context

Your main Claude Code session can't see how full each subagent's
context is. These hooks tell it — and warn it before it hands more
work to an agent with degraded performance.

## The problem

When a Claude session runs subagents (background agents, teammates,
review panels), nothing reports how much context each one has used. So
the main session routinely says "one more round" to an agent sitting at
350k+ tokens. Agents that full get worse: recall drops, they lean on
their own earlier conclusions, they churn more, and reviews may
rubber-stamp. This plugin gives the dispatcher context awareness,
configurable.

## What it does

Three small hooks.

1. **Observer** (`SubagentStop`) — when a subagent stops, reads its
   context size from its transcript (current, peak, and whether it was
   ever compacted) and saves the numbers.
2. **Drain** (`PostToolUse`) — on the orchestrator's next tool call,
   slips any new reports into its context:

   > `[subagent-context] research-worker (claude-opus-5): ~383k tokens —
   > OVER THRESHOLD: prefer spawning a fresh agent over re-tasking this
   > one; long-context agents degrade.`

3. **Guard** (`PreToolUse` on `SendMessage`) — catches the moment
   before a session sends more work to a full agent, re-reading
   the target's transcript right then so the number reflects what the
   agent is NOW, not what it was at its last stop. Past `warn_tokens`
   it adds a warning to that tool call. Past `block_tokens` (350k by
   default) — or if the agent has ever been compacted (see
   `compaction_action`) — the send is challenged: denied once with a
   reason the model sees, and allowed if the model retries after
   weighing it (`block_style: "deny_once"`, the default). Set
   `block_style: "ask"` if you'd rather answer a confirmation dialog
   yourself — but note an unattended session then hangs on that
   dialog until you answer.

This costs you nothing other than a few tokens on an existing message:
no extra model calls.

Reports go to whatever session spawned the agent. The root session gets reports
for its own spawns, teammates, and Workflow-tool agents (labeled with
the run id); a subagent that spawns its own subagents gets their
reports in its own context, and the same warn logic applies when it
re-tasks them.

## Install

**As a plugin:**

```
/plugin marketplace add msshives-gif/claude-plugins
/plugin install subagent-context@claude-plugins
```

**Manual (no plugin system):**

```bash
git clone https://github.com/msshives-gif/subagent-context
cd subagent-context && ./scripts/install.sh   # merges into ~/.claude/settings.json, with backup
```

Either way, restart any running Claude Code sessions — hooks are read
at startup. `./scripts/uninstall.sh` reverses the manual install.

**Requirements**

- Python 3.9 or newer, on PATH as `python3` or `python`. No other
  dependencies.
- A Claude Code version that includes `agent_id` and
  `agent_transcript_path` in the `SubagentStop` hook payload. To check:
  spawn any subagent, then run `python3 scripts/status.py`. If it lists
  the agent, you're set.
- Developed and tested on Linux; macOS runs the same code paths.
  Windows should work (portable locking, `python` fallback) but hasn't
  been tested on a real Windows machine — reports welcome. On Windows,
  install as a plugin (`install.sh` is a bash script).

## Configuration

Every knob works as an environment variable (`SUBAGENT_CONTEXT_<NAME>`)
or a key in `~/.claude/subagent-context.json` (env wins; point
`SUBAGENT_CONTEXT_CONFIG` at a different config path if you want one):

| Knob | Default | Meaning |
|---|---|---|
| `warn_tokens` | `250000` | Above this, reports carry an OVER-THRESHOLD warning and the guard starts firing. |
| `block_tokens` | `350000` | Above this, messaging the agent is gated per `block_style`. `0` turns this off. |
| `compaction_action` | `block` | What a compacted agent triggers: `off` (nothing — size thresholds still apply), `warn`, or `block` (gate per `block_style`). |
| `block_style` | `deny_once` | How the gate behaves. `deny_once`: denied once with a model-facing reason; a retry passes; re-arms after `deny_once_ttl_seconds`. `ask`: human confirmation dialog (root session only; hangs unattended sessions). |
| `deny_once_ttl_seconds` | `900` | How long a deny-once challenge stays satisfied before it re-arms. |
| `report_min_tokens` | `0` | Only report agents at least this big. `0` = report every stop. |
| `models` | `{}` | Per-model overrides for `warn_tokens`, `block_tokens`, `report_min_tokens`, `compaction_action` — see below. |
| `system_message` | `true` | Also show each report to you in the UI. |
| `drain_batch_max` | `20` | Max reports delivered per tool call. |
| `flush_grace_ms` | `4000` | How long to wait for a stopping agent's transcript to finish being written. |
| `state_dir` | `~/.claude/subagent-context` | Where recorded numbers live. |
| `ledger` / `ledger_max_bytes` | `true` / 5MB | Keep an audit log of every observation, rotated at the size limit. |
| `state_ttl_days` | `7` | Old sessions' records get cleaned up after this. |

The defaults assume long-context models. A model with a 200k window
compacts before reaching 250k — compaction is itself reported and
guarded, but if you mix model families, give each its own thresholds.
The default `deny_once` gate self-resolves in headless and unattended
runs (the model just retries); with `block_style: "ask"` a headless
run can't answer the dialog, so the block acts as a refusal there.

### Per-model thresholds

`models` maps a model-ID substring to overrides for `warn_tokens`,
`block_tokens`, `report_min_tokens`, and `compaction_action`. In
`~/.claude/subagent-context.json`:

```json
{
  "warn_tokens": 250000,
  "models": {
    "claude-fable-5": { "warn_tokens": 400000, "block_tokens": 700000 },
    "opus": { "warn_tokens": 250000, "block_tokens": 350000 },
    "haiku": { "warn_tokens": 150000 }
  }
}
```

A pattern matches when it appears anywhere in the agent's model ID
(case-insensitive); the longest matching pattern wins, so
`claude-opus-4-8` can be more specific than `opus`. Knobs a match
doesn't set fall back to the global values, and agents whose model is
unknown always use the globals. As an environment variable,
`SUBAGENT_CONTEXT_MODELS` takes the same mapping as a JSON string.

## Checking the numbers yourself

Reports arrive on their own. To look at the current numbers any time:

```bash
python3 scripts/status.py                 # all sessions, newest first
python3 scripts/status.py --session 02ff  # prefix match
```

Installed as a plugin, you won't have a clone of the repo. Run the
script from the plugin's own directory — `/plugin` shows you the path.

## Limitations (read before relying on it)

- **Built on undocumented internals.** Context is measured from Claude
  Code's subagent transcript files, whose format is not a public
  interface. If a Claude Code update changes those formats, this plugin
  stops producing reports. It will not break your session. Built and
  verified against Claude Code as of 2026-08-02.
- **Report numbers are minimums.** Reports are taken when an agent
  stops. In practice that's often — `SubagentStop` fires every time an
  agent goes idle, not only when it finishes for good (that's what we
  observed; it isn't documented) — but an agent that's mid-task has
  already grown past its last report. The guard is fresher: it re-reads
  the target's transcript at send time, so its number is current as of
  the agent's last completed model call (an in-flight call is still
  invisible).
- **Compaction is treated as a warning sign.** After auto-compaction an
  agent's current context looks small again. Reports show the peak and
  a `COMPACTED xN` flag, and the guard escalates per
  `compaction_action` — by default re-tasking a compacted agent gets
  the deny-once challenge (set `"warn"` for warnings only).
- **The guard watches `SendMessage`.** That's how existing agents get
  more work in current Claude Code. Fresh `Agent` spawns start empty
  and need no guard. No `SendMessage` tool in your setup? The guard
  never fires; reports still work.
- **Privacy.** Recorded state stays on your machine, under `state_dir`:
  agent names, models, session IDs, token counts, transcript paths.
  Nothing goes over the network.
- **Install one way, not both.** Plugin install *and* `install.sh`
  together would run every hook twice and duplicate reports.

## Uninstall

Plugin: `/plugin uninstall subagent-context`. Manual:
`./scripts/uninstall.sh`. Then delete `~/.claude/subagent-context/`.

## Development

```bash
python3 -m unittest discover tests
```

Design rationale, the verified hook-channel behavior this depends on,
and rejected alternatives: [docs/DESIGN.md](docs/DESIGN.md).

## License

MIT.
