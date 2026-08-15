# Suite layout decision (2026-08-15, rev 2)

**Decision: uniform marketplace monorepo named `claude-plugins`.**
Every plugin lives under `plugins/<name>/`; one marketplace at the
repo root; install ids are `<plugin>@claude-plugins`. The repo was
renamed from `subagent-context` (GitHub redirects the old slug).

## Why this shape

- One repo scales for "keep adding tools": a new plugin is a subdir
  plus a marketplace entry. This is also the dominant ecosystem shape
  for personal marketplaces (claude-plugins-official and popular
  personal marketplaces are monorepos).
- All plugins share one risk surface (undocumented Claude Code
  transcript/registry formats) and, where needed verbatim, a vendored
  measurement core — one place to notice and fix format drift.
- Separate-repos-plus-umbrella was considered and rejected: it taxes
  every future tool with repo setup and multiplies format-drift fixes,
  and its main benefit (install-id branding) is achieved by the
  marketplace name alone.

## Supersedes

Rev 1 (same day) kept subagent-context rooted at `./` with siblings in
`plugins/`, because moving an installed plugin's `source` relied on
unverified re-point behavior. That concern died on inspection: the
only real installs were repo-direct hook scripts (not plugin-system
installs), and external marketplace adoption was zero on rename day —
so the migration window was free. Anyone on the old marketplace name
re-adds `msshives-gif/claude-plugins` and reinstalls.
