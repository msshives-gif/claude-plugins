# Sibling plugins

Each subdirectory here is an independently installable Claude Code
plugin, listed in the repo-root marketplace
(`.claude-plugin/marketplace.json`). The subagent-context plugin itself
is rooted at the repo root (`source: "./"`), not here — moving an
already-installed plugin's source path is riskier than the asymmetry
(see docs/SUITE.md).

Rules for a sibling:

- Own `.claude-plugin/plugin.json`, `hooks/`, `tests/`, `README.md`,
  `CHANGELOG.md`, and (if it supports manual install) its own
  `scripts/install.sh` + `uninstall.sh` with markers that match ONLY its
  own hook scripts.
- No `marketplace.json` — one marketplace, at the repo root.
- Hook script filenames must not collide with another plugin's uninstall
  markers (see `tests/test_install_scripts.py` in the repo root).
- No imports across plugins. Shared measurement code is vendored via
  `tools/sync-core.py` where a plugin needs it verbatim.
