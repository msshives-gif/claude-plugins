# Suite layout decision (2026-08-15)

**Decision: subagent-context stays rooted at `./`; sibling plugins live
under `plugins/<name>/`; the marketplace keeps the name
`subagent-context`.** Suite identity ("claude context tools") is
carried by the README umbrella and the marketplace description only.

## Why not a uniform `plugins/` migration (considered, rejected)

- The install identity is `<pluginName>@<marketplaceName>` =
  `subagent-context@subagent-context`. Both components survive either
  layout, so identity doesn't decide it.
- Moving the shipped plugin's `source` from `./` to a subdir relies on
  unverified Claude Code behavior (whether `marketplace update`
  re-points an installed plugin when only its source path moves —
  `renames` covers plugin *name* changes only). Risking the proven,
  tagged v0.3.0 for cosmetic symmetry is a bad trade.
- Renaming the marketplace to a suite name would rewrite every existing
  install id and break it.

Revisit only if Claude Code documents source-move behavior on update, or
a major version forces a breaking release anyway.

## Consequences accepted

- Asymmetric tree (one plugin at root, others in `plugins/`).
- Root `README.md` does double duty (umbrella + subagent-context's own
  doc); root `scripts/` belongs to subagent-context, suite tooling goes
  in `tools/`.
- Cross-plugin uninstall safety is a real hazard in this layout: a
  sibling's absolute install path contains the repo directory name, so
  uninstall markers must match per-plugin hook path suffixes, never the
  repo/plugin name. Pinned by `tests/test_install_scripts.py`.

## Vendoring

`tools/sync-core.py` vendors `hooks/subagent_context.py` verbatim into
plugins that need it (currently: cross-session-send-guard only), with a
provenance header; the receiving plugin carries a drift-guard test.
compact-manager instead hand-ports a modified lib and owns its copy — no
drift guard there. No plugin imports from another plugin.
