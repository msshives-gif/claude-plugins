#!/bin/bash
# S6 destructive-safety spike: adversarial scenarios against the
# safety-ladder injector, in a throwaway haiku session. Results ->
# s6-results.jsonl. Scenarios:
#  a happy-path /compact injection with nonce -> PreCompact(manual) ack
#  b busy model -> R2 trips
#  c user typing DURING injection -> R3/R5 trips; measure pollution
#  d claude exited to shell -> R1 trips (and measure what
#    pane_current_command reports for live claude vs shell)
#  e second attached client -> measure list-clients detectability
#  f half-typed user text in composer -> R2/R5 trips BEFORE Enter
set -uo pipefail
D="$(cd "$(dirname "$0")" && pwd)"
OUT="$D/s6-results.jsonl"
SES=cms6

log() { echo "{\"ts\": $(date +%s), $1}" >> "$OUT"; }

pane_idle() {
  local cap; cap="$(tmux capture-pane -t $SES -p)"
  echo "$cap" | grep -q "esc to interrupt" && return 1
  echo "$cap" | grep -qE "^❯.{0,3}$" || return 1
  return 0
}
wait_idle() {
  local deadline=$((SECONDS + ${1:-180}))
  until pane_idle; do [ $SECONDS -ge $deadline ] && return 1; sleep 4; done
}

rm -f "$OUT" "$D/s6-hook-events.jsonl"
tmux kill-session -t $SES 2>/dev/null || true
tmux new-session -d -s $SES -c "$D" -x 200 -y 50 \
  "env -u ANTHROPIC_API_KEY S6_HOOK_LOG=$D/s6-hook-events.jsonl claude --model haiku --settings $D/settings.json"
wait_idle 120 || { log '"scenario": "launch", "ok": false'; exit 1; }
CMD_LIVE="$(tmux display -p -t $SES '#{pane_current_command}')"
log "\"scenario\": \"launch\", \"ok\": true, \"pane_cmd_live\": \"$CMD_LIVE\", \"clients\": $(tmux list-clients -t $SES 2>/dev/null | wc -l)"

# --- b: busy model must trip R2
tmux send-keys -t $SES -l "Count slowly from 1 to 30, one number per line."; sleep 1
tmux send-keys -t $SES Enter; sleep 3
V="$(bash "$D/inject.sh" $SES "/compact should-not-send-busy")"
log "\"scenario\": \"b_busy\", \"verdict\": $V"
wait_idle 180

# --- f: half-typed user text must trip before Enter
tmux send-keys -t $SES -l "rm -rf /tmp/half-typed-do-not-run"; sleep 1
V="$(bash "$D/inject.sh" $SES "/compact should-not-send-halftyped")"
COMPOSER_AFTER="$(tmux capture-pane -t $SES -p | grep '^❯' | tail -1 | head -c 100)"
log "\"scenario\": \"f_half_typed\", \"verdict\": $V, \"composer_after\": $(echo "$COMPOSER_AFTER" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))')"
# clean the composer for later scenarios (WE may do this in the spike;
# the real watcher never would)
tmux send-keys -t $SES C-u; sleep 1

# --- c: user typing DURING injection (race the ladder)
( sleep 0.15; tmux send-keys -t $SES -l "USERRACE" ) &
RACER=$!
V="$(bash "$D/inject.sh" $SES "/compact should-probably-not-send-race" 400)"
wait $RACER 2>/dev/null
COMPOSER_AFTER="$(tmux capture-pane -t $SES -p | grep '^❯' | tail -1 | head -c 120)"
log "\"scenario\": \"c_race\", \"verdict\": $V, \"composer_after\": $(echo "$COMPOSER_AFTER" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))')"
tmux send-keys -t $SES C-u; sleep 1

# --- a: happy path with nonce ack
NONCE="cm-s6-$RANDOM$RANDOM"
V="$(bash "$D/inject.sh" $SES "/compact [$NONCE] wrap up, keep the task list")"
log "\"scenario\": \"a_happy\", \"nonce\": \"$NONCE\", \"verdict\": $V"
# wait for compaction to finish, then check the hook log for the nonce
deadline=$((SECONDS + 240))
ACK=0
until [ $SECONDS -ge $deadline ]; do
  grep -q "$NONCE" "$D/s6-hook-events.jsonl" 2>/dev/null && { ACK=1; break; }
  sleep 5
done
log "\"scenario\": \"a_ack\", \"nonce_in_precompact\": $ACK"
wait_idle 120

# --- e: second client attached — detectability only
tmux new-session -d -s cms6watch -x 80 -y 24 "tmux attach -t $SES \; detach -P 2>/dev/null; sleep 30" 2>/dev/null || true
sleep 2
CLIENTS=$(tmux list-clients -t $SES 2>/dev/null | wc -l)
log "\"scenario\": \"e_clients\", \"clients_seen\": $CLIENTS"
tmux kill-session -t cms6watch 2>/dev/null || true

# --- d: exit to shell must trip R1
tmux send-keys -t $SES -l "/exit"; sleep 1; tmux send-keys -t $SES Enter
sleep 6
CMD_SHELL="$(tmux display -p -t $SES '#{pane_current_command}' 2>/dev/null || echo GONE)"
if [ "$CMD_SHELL" != "GONE" ]; then
  V="$(bash "$D/inject.sh" $SES "/compact must-not-reach-a-shell")"
  PANE_TAIL="$(tmux capture-pane -t $SES -p | tail -3 | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))')"
  log "\"scenario\": \"d_shell\", \"pane_cmd\": \"$CMD_SHELL\", \"verdict\": $V, \"pane_tail\": $PANE_TAIL"
else
  log "\"scenario\": \"d_shell\", \"pane_cmd\": \"GONE\", \"verdict\": {\"sent\": false, \"aborted_at\": \"R1_no_pane\"}"
fi

tmux kill-session -t $SES 2>/dev/null || true
echo DONE
