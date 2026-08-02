# subagent-gauge

**A fuel gauge for your subagents.** Your main Claude Code session
can't see how full each subagent's context is. These hooks tell it —
and warn it before it hands more work to an agent that's already full.

## The problem

When a Claude session runs subagents (background agents, teammates,
review panels), nothing reports how much context each one has used —
not task notifications, not the `/agents` UI, not hook payloads. So the
main session routinely says "one more round" to an agent sitting at
350k+ tokens. Agents that full get worse: recall drops, they lean on
their own earlier conclusions, reviews rubber-stamp. And the main
session never finds out why.

## What it does

Three small hooks. Pure Python, nothing else to install. If a hook hits
a problem it goes quiet — it will never break or block your session.

1. **Observer** (`SubagentStop`) — when a subagent stops, reads its
   context size from its transcript (current, peak, and whether it was
   ever compacted) and saves the numbers.
2. **Drain** (`PostToolUse`) — on the orchestrator's next tool call,
   slips any new reports into its context:

   > `[subagent-gauge] research-worker (claude-opus-5): ~383k tokens —
   > OVER THRESHOLD: prefer spawning a fresh agent over re-tasking this
   > one; long-context agents degrade.`

3. **Guard** (`PreToolUse` on `SendMessage`) — catches the moment
   before the main session sends more work to a full agent. Past
   `warn_tokens` it adds a warning to that tool call. Past
   `block_tokens` (350k by default) it also asks you to confirm. You
   can always say yes.

This costs you nothing: no extra model calls, and the subagent's own
final answer is never altered.

Reports go to whoever spawned the agent. The root session gets reports
for its own spawns, teammates, and Workflow-tool agents (labeled with
the run id); a subagent that spawns its own subagents gets their
reports in its own context, and the same warn logic applies when it
re-tasks them.

## Install

**As a plugin:**

```
/plugin marketplace add msshives-gif/subagent-gauge
/plugin install subagent-gauge@subagent-gauge
```

**Manual (no plugin system):**

```bash
git clone https://github.com/msshives-gif/subagent-gauge
cd subagent-gauge && ./scripts/install.sh   # merges into ~/.claude/settings.json, with backup
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

Every knob works as an environment variable (`SUBAGENT_GAUGE_<NAME>`)
or a key in `~/.claude/subagent-gauge.json` (env wins; point
`SUBAGENT_GAUGE_CONFIG` at a different config path if you want one):

| Knob | Default | Meaning |
|---|---|---|
| `warn_tokens` | `150000` | Above this, reports carry an OVER-THRESHOLD warning and the guard starts firing. |
| `block_tokens` | `350000` | Above this, messaging the agent needs your confirmation. `0` turns this off. |
| `report_min_tokens` | `0` | Only report agents at least this big. `0` = report every stop. |
| `system_message` | `true` | Also show each report to you in the UI. |
| `drain_batch_max` | `20` | Max reports delivered per tool call. |
| `flush_grace_ms` | `4000` | How long to wait for a stopping agent's transcript to finish being written. |
| `state_dir` | `~/.claude/subagent-gauge` | Where recorded numbers live. |
| `ledger` / `ledger_max_bytes` | `true` / 5MB | Keep an audit log of every observation, rotated at the size limit. |
| `state_ttl_days` | `7` | Old sessions' records get cleaned up after this. |

150k suits models with a 200k window; raise it if you run 1M-context
agents. The confirmation prompt can't be answered in a headless run, so
there the block acts as a refusal.

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
- **Numbers are minimums.** Reports are taken when an agent stops. In
  practice that's often — `SubagentStop` fires every time an agent goes
  idle, not only when it finishes for good (that's what we observed; it
  isn't documented) — but an agent that's mid-task has already grown
  past its last report.
- **Compaction is treated as a warning sign.** After auto-compaction an
  agent's current context looks small again. Reports show the peak and
  a `COMPACTED xN` flag, and the guard still fires for it.
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

Plugin: `/plugin uninstall subagent-gauge`. Manual:
`./scripts/uninstall.sh`. Then delete `~/.claude/subagent-gauge/`.

## Development

```bash
python3 -m unittest discover tests
```

Design rationale, the verified hook-channel behavior this depends on,
and rejected alternatives: [docs/DESIGN.md](docs/DESIGN.md).

## License

MIT.
