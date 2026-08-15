# Send-guard resolution spike (2026-08-15)

Read-only census of one live machine (8 sessions), grounding
`plugins/cross-session-send-guard/hooks/peers.py`. Values sanitized.

## Findings

1. **Registry**: `~/.claude/sessions/<pid>.json` maps `name` →
   `sessionId`, `cwd`, `procStart`, `updatedAt`, `status`, and (from
   CC ≈2.1.231) `messagingSocketPath`. Older live sessions have no
   socket — they're measurable but can't receive peer messages at all
   (the harness rejects the send).
2. **Liveness**: zero stale files observed, but `/proc/<pid>` existence
   alone is not enough — sessions run for weeks, so pid recycling is a
   real hazard. The registry's `procStart` matches
   `/proc/<pid>/stat` starttime for genuine sessions; require equality.
3. **Collisions**: no duplicate live names observed, but one *session*
   had two live pids (a `--resume` alongside the original) — resolving
   by name → newest `updatedAt`, then measuring the resolved
   `sessionId`'s transcript, handles it (both resolve to one file).
4. **Address forms** (264 SendMessage records): registry name verbatim;
   `name [ref]` (the harness rejects an ambiguous bare name with a
   "re-send with the ref" error; the ref hex is NOT derivable from
   on-disk registry state — strip it and resolve by base name);
   `uds:/…/cc-socks/<pid>.sock`.
5. **Payload**: PreToolUse carries root `session_id`, `tool_name`,
   `tool_input` (with `to`), plus `agent_id` inside a subagent — same
   contract the sibling guard already uses.
6. **Fork inheritance — hypothesis refuted**: a fork's transcript does
   NOT open with the parent's full inherited context; its first usage
   row was ~73k against a ~45-49k fresh-session baseline, then grew to
   accurate current values (~580k) as it ran. Last-terminal-row
   measurement is correct; a just-forked idle peer under-measures
   briefly (documented limitation, no special handling).
7. **Resolvable-but-unmeasurable** exists (a live session whose
   transcript has no terminal usage rows): must stay silent, not error.

## Consequences in code

`resolve_peer` requires: live registry match (procStart equality),
newest-wins on collision, never the sender's own session, transcript
must exist; any surprise → None. The issue's pid→cwd→newest-transcript
heuristic is documentation-only — multiple sessions share a cwd, so it
can name the wrong session and is never used as a gating path.
