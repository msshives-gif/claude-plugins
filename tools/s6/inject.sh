#!/bin/bash
# S6 safety-ladder injector: attempts to type TEXT + Enter into tmux
# session $1's active pane, aborting at the first tripped rail.
# Rails (in order):
#   R1 pane_current_command is claude/node (never a shell)
#   R2 composer idle: no "esc to interrupt", last ❯-line empty
#   R3 pane stable: two captures STABLE_MS apart hash-identical
#   R4 type text WITHOUT Enter (send-keys -l)
#   R5 composer now contains EXACTLY our text after ❯ (nothing else)
#   R6 Enter, then verify submission (composer cleared or busy)
# NEVER clears the line; on abort after R4 the typed text is left
# visible (measured, reported). Prints one JSON verdict line.
set -uo pipefail
SES="$1"; TEXT="$2"
STABLE_MS="${3:-300}"

fail() { echo "{\"sent\": false, \"aborted_at\": \"$1\", \"detail\": \"$2\"}"; exit 0; }

cap() { tmux capture-pane -t "$SES" -p 2>/dev/null; }
composer_line() { cap | grep "^❯" | tail -1; }

# R1: foreground process. claude CLI shows up as its node binary or a
# renamed argv; a shell (bash/zsh/sh) is the destructive case.
CMD="$(tmux display -p -t "$SES" '#{pane_current_command}' 2>/dev/null)"
case "$CMD" in
  bash|zsh|sh|fish|dash) fail R1_shell "$CMD" ;;
  "") fail R1_no_pane "" ;;
esac

# R2: idle composer.
C="$(cap)"
echo "$C" | grep -q "esc to interrupt" && fail R2_busy ""
echo "$C" | grep -qE "^❯.{0,3}$" || fail R2_composer_not_empty \
  "$(composer_line | head -c 60)"

# R3: stability window.
H1="$(cap | md5sum)"
sleep "$(python3 -c "print($STABLE_MS/1000)")"
H2="$(cap | md5sum)"
[ "$H1" = "$H2" ] || fail R3_pane_changed ""
# Re-verify R1 after the wait (claude could have exited meanwhile).
CMD2="$(tmux display -p -t "$SES" '#{pane_current_command}' 2>/dev/null)"
[ "$CMD2" = "$CMD" ] || fail R3_command_changed "$CMD2"

# R4: type, no Enter.
tmux send-keys -t "$SES" -l "$TEXT" || fail R4_send_failed ""
sleep 0.4

# R5: composer must contain exactly our text (allow TUI wrapping by
# checking the first 40 chars appear and no foreign prefix).
LINE="$(composer_line)"
STRIPPED="$(echo "$LINE" | sed -E 's/^❯[ \xc2\xa0]*//')"
if [ "${STRIPPED:0:40}" != "${TEXT:0:40}" ]; then
  fail R5_composer_mismatch "$(echo "$STRIPPED" | head -c 80)"
fi

# R6: submit and verify.
tmux send-keys -t "$SES" Enter || fail R6_enter_failed ""
sleep 1
C3="$(cap)"
if echo "$C3" | grep -qE "^❯.{0,3}$" || echo "$C3" | grep -q "esc to interrupt" \
   || ! echo "$C3" | grep -qF "${TEXT:0:40}"; then
  echo "{\"sent\": true, \"aborted_at\": null, \"detail\": \"\"}"
else
  echo "{\"sent\": true, \"aborted_at\": \"R6_unverified\", \"detail\": \"text may still be in composer\"}"
fi
