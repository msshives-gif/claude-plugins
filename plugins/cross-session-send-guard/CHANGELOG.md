# Changelog — cross-session-send-guard

## 0.1.0 — 2026-08-15

First release. PreToolUse guard on SendMessage for peer sessions:
registry-based resolution (name / `name [ref]` / `uds:` socket forms,
procStart anti-recycle liveness, newest-wins collisions, never the
sender's own session), transcript measurement via the vendored
subagent-context scanner, mtime-vs-TTL coldness, tiered
silent/warn/ask policy (ask: root session + cold + big only), and
fail-open on every path.
