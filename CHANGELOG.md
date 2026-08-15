# Changelog — claude-plugins (repo)

Per-plugin changes live in each plugin's own CHANGELOG.md.

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
