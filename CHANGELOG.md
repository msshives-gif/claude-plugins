# Changelog

## 0.1.0 — 2026-08-01

Initial release: observer (SubagentStop measurement → state + queue),
drain (PostToolUse injection into the orchestrator), guard (PreToolUse
on SendMessage: warns above `warn_tokens`, asks for confirmation above
`block_tokens`), status CLI, plugin packaging plus manual
install/uninstall scripts, unit tests, design doc.
Supersedes the private v1 relay-based hook (see docs/DESIGN.md for why
the relay was dropped).
