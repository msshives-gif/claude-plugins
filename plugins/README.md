# Plugins

Each subdirectory is an independently installable Claude Code plugin,
listed in the repo-root marketplace (`.claude-plugin/marketplace.json`).

Rules for a plugin here:

- Own `.claude-plugin/plugin.json`, `hooks/`, `tests/`, `README.md`,
  `CHANGELOG.md`, and (if it supports manual install) its own
  `scripts/install.sh` + `uninstall.sh` with markers that match ONLY its
  own hook scripts.
- No `marketplace.json` — one marketplace, at the repo root.
- Hook script filenames must not collide with another plugin's uninstall
  markers (see each plugin's `tests/test_install_scripts.py`).
- No imports across plugins. Shared measurement code is vendored via
  `tools/sync-core.py` where a plugin needs it verbatim.
