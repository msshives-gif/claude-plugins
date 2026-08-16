# Changelog — claude-plugins (repo)

Per-plugin changes live in each plugin's own CHANGELOG.md.

## 2026-08-16

Added `tools/status.py`: a read-only, one-screen readout of every
plugin's effective config (each knob tagged default/FILE/ENV, unusable
raw values flagged), hook wiring found in `~/.claude/settings*.json`
(matcher-aware), state dirs/ledger sizes, and live compact-manager
watchers. It imports each plugin's own `load_config()` so it can never
drift from what the hooks actually compute. Also fixed
`tools/run-tests.sh` (still ran a repo-root suite from the
single-plugin era) and a stale "NOT BUILT YET" comment on
compact-manager's `mode` default.

## 2026-08-15

Repo renamed `subagent-context` → `claude-plugins` and restructured to
a uniform marketplace monorepo: the subagent-context plugin moved from
the repo root into `plugins/subagent-context/` beside its siblings,
and the marketplace name changed to `claude-plugins` (install ids are
now `<plugin>@claude-plugins`). GitHub redirects the old repo slug;
anyone who installed under the old marketplace name should re-add the
marketplace and reinstall. History: the repo began life as
`subagent-gauge`, then `subagent-context` (single plugin), then the
"claude context tools" three-plugin suite, now this.
