# claude-plugins ("claude context tools")

Marketplace monorepo: every plugin lives under `plugins/<name>/`, one
marketplace at `.claude-plugin/marketplace.json`. Simple code and
simple documentation is best; complexity and big words are the enemy.
Prefer the boring obvious shape; build machinery only when real use
forces it.

- All plugins parse undocumented Claude Code internals (transcript
  JSONL, sessions registry, meta.json). Never make parsing stricter
  without keeping degrade-to-silence; when the observed format
  changes, update the affected plugins' fixtures TOGETHER — this
  shared surface is why the repo is a monorepo.
- Shared measurement core: `plugins/subagent-context/hooks/
  subagent_context.py`, vendored into send-guard by
  `tools/sync-core.py` (drift-guard test enforces sync).
  compact-manager owns its hand-ported copy outright.
- Hooks MUST fail open (exit 0 on every path) and each plugin's
  uninstall markers must match only its own hook paths — plugins
  share the repo directory name (see each plugin's
  `tests/test_install_scripts.py`).
- Keep the repo system-agnostic: no machine-specific paths, hosts, or
  identities in tracked files; managed mode is Linux/WSL-only and says
  so rather than pretending otherwise.
- Per-plugin discipline lives in each plugin's own CLAUDE.md/README.
- Unit suites: `python3 -m unittest discover plugins/<name>/tests`
  per plugin. compact-manager's managed mode additionally has a gated
  LIVE suite (`tools/live-managed/run.sh` — spawns a real claude
  session; run manually) that must pass before releasing changes to
  the watcher/ladder.
