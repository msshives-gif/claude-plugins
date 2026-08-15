#!/bin/bash
# Live regression suite for compact-manager v0.2.0 managed mode.
# GATED: drives real claude (haiku) sessions in tmux through the real
# watcher. Run manually; costs a few dollar-cents of haiku tokens and
# ~15-20 minutes. Results land in $OUT as JSONL, one row per check.
#
# Scenarios (plan: docs/plans/m3-managed-mode.md "Testing"):
#   s01 adopt without --attended refused
#   s02 adopt of a bash pane refused (binding walk fails)
#   s03 adopt happy: lease pair + heartbeat advances
#   s04 second adopt of the same pane refused (collision)
#   s05 Ctrl-Z foreground loss = defer/wait, fg = recovery (no cleanup)
#   s06 CLI stop releases leases and kills the watcher
#   s07 threshold trigger -> ladder -> nonce ack -> boundary confirmed
#   s08 half-typed user text defers; watcher never clears or types over it
#   s09 foreign packet -> stand-down -> safety latch + alert; operator
#       resolve accepted for the live token; manual /compact clears it
#   s10 TYPED_VERIFIED crash tail -> adopt reports SUBMISSION_UNCERTAIN;
#       resolve refused while a live foreign-token lease exists, accepted
#       once the watcher is stopped (dead-token cleanup path)
#   s11 start mode: pane dies with claude; watcher retires, leases free
#   s12 start with a non-claude argv fails closed
# Deadline expiry is unit-tested (1h floor makes it impractical live).
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
D="$(cd "$(dirname "$0")" && pwd)"
WORK="${LIVE_MANAGED_WORK:-$HOME/.cache/compact-manager-live}"
STATE="$WORK/state"
OUT="$D/live-results.jsonl"
CMBIN="$ROOT/plugins/compact-manager/bin/compact-manager"
SES=cml2
export COMPACT_MANAGER_STATE_DIR="$STATE"
export COMPACT_MANAGER_MODE=managed
export COMPACT_MANAGER_CONTEXT_WINDOW=100000
export COMPACT_MANAGER_SOFT_PCT=0.10
export COMPACT_MANAGER_HARD_PCT=0.15
export COMPACT_MANAGER_MANAGED_POLL_S=5
export COMPACT_MANAGER_MANAGED_ACK_TIMEOUT_S=60
export COMPACT_MANAGER_MANAGED_COMPLETION_TIMEOUT_S=120

row() {  # row <scenario> <pass|fail|info> <detail...>
  local sc="$1" verdict="$2"; shift 2
  python3 -c 'import json,sys,time; print(json.dumps({"ts": int(time.time()), "scenario": sys.argv[1], "verdict": sys.argv[2], "detail": " ".join(sys.argv[3:])[:400]}))' "$sc" "$verdict" "$@" >> "$OUT"
  echo "[$sc] $verdict $*"
}

jstates() {  # journal states for the session, comma-joined
  python3 - "$STATE/managed/watchers/$1.journal.jsonl" <<'EOF'
import json, sys
states = []
try:
    for line in open(sys.argv[1], "rb"):
        if not line.endswith(b"\n"):
            continue
        try:
            states.append(json.loads(line).get("state", "?"))
        except Exception:
            pass
except OSError:
    pass
print(",".join(states))
EOF
}

sid_of() {  # newest live session_id from compact-manager status
  "$CMBIN" status 2>/dev/null | python3 -c 'import json,sys; rows=json.load(sys.stdin); live=[r for r in rows if r.get("live")]; print(live[-1]["session_id"] if live else "")'
}

pane_of() { tmux list-panes -t "$1" -F '#{pane_id}' | head -1; }

pane_idle() {
  local cap; cap="$(tmux capture-pane -t "$1" -p 2>/dev/null)" || return 1
  echo "$cap" | grep -q "esc to interrupt" && return 1
  echo "$cap" | grep -qE "^❯.{0,3}$"
}

wait_idle() {  # wait_idle <target> [ceiling-s]
  local deadline=$((SECONDS + ${2:-300}))
  until pane_idle "$1"; do
    [ $SECONDS -ge $deadline ] && return 1
    sleep 5
  done
}

wait_for() {  # wait_for <ceiling-s> <cmd...> — poll a check, bounded
  local deadline=$((SECONDS + $1)); shift
  until "$@"; do
    [ $SECONDS -ge $deadline ] && return 1
    sleep 3
  done
}

send_turn() {  # send_turn <target> <text> [idle-ceiling]
  tmux send-keys -t "$1" -l "$2"; sleep 1
  tmux capture-pane -t "$1" -p | grep -qF "${2:0:40}" || { tmux send-keys -t "$1" -l "$2"; sleep 1; }
  tmux send-keys -t "$1" Enter; sleep 3
  wait_idle "$1" "${3:-300}"
}

adopt() {  # adopt <pane> [extra-env...] -> stdout of CLI; rc passthrough
  env "${@:2}" "$CMBIN" adopt -t "$1" --attended 2>&1
}

cleanup() {
  for s in $SES ${SES}bash cml2start; do tmux kill-session -t "$s" 2>/dev/null || true; done
  # best-effort: stop any watcher we leaked
  "$CMBIN" status 2>/dev/null | python3 -c 'import json,sys; [print(r["pid"]) for r in json.load(sys.stdin) if r.get("live") and r.get("pid")]' 2>/dev/null | while read -r p; do kill "$p" 2>/dev/null || true; done
}
trap cleanup EXIT

rm -f "$OUT"
rm -rf "$STATE"; mkdir -p "$WORK" "$STATE"
python3 - "$WORK" <<'EOF'
import random, sys
random.seed(7)
words = ("harbor lantern granite meadow copper thistle beacon quarry velvet "
         "orchard ember sable willow cinder marrow pewter juniper gable "
         "russet fathom").split()
for name in ("filler_a", "filler_b", "filler_c"):
    with open(f"{sys.argv[1]}/{name}.txt", "w") as fh:
        for i in range(1200):
            fh.write(f"{name} line {i}: " + " ".join(random.choices(words, k=9)) + "\n")
EOF
cat > "$WORK/settings.json" <<EOF
{
  "hooks": {
    "PostToolUse": [{"matcher": "*", "hooks": [{"type": "command", "command": "python3 $ROOT/plugins/compact-manager/hooks/advisor.py || true", "timeout": 5}]}],
    "UserPromptSubmit": [{"hooks": [{"type": "command", "command": "python3 $ROOT/plugins/compact-manager/hooks/reorient.py || true", "timeout": 5}]}],
    "PreCompact": [
      {"matcher": "manual", "hooks": [{"type": "command", "command": "python3 $ROOT/plugins/compact-manager/hooks/precompact.py || true", "timeout": 5}]},
      {"matcher": "auto", "hooks": [{"type": "command", "command": "python3 $ROOT/plugins/compact-manager/hooks/precompact.py || true", "timeout": 5}]}
    ],
    "SessionStart": [{"matcher": "compact", "hooks": [{"type": "command", "command": "python3 $ROOT/plugins/compact-manager/hooks/session_start.py || true", "timeout": 5}]}]
  }
}
EOF
CLAUDE_LINE="env -u ANTHROPIC_API_KEY COMPACT_MANAGER_MODE=managed COMPACT_MANAGER_STATE_DIR=$STATE COMPACT_MANAGER_CONTEXT_WINDOW=100000 COMPACT_MANAGER_SOFT_PCT=0.10 COMPACT_MANAGER_HARD_PCT=0.15 claude --model haiku --settings $WORK/settings.json --allowedTools Read,Write"

accept_trust() {  # first run in a fresh workspace shows the trust dialog
  local deadline=$((SECONDS + 60))
  while [ $SECONDS -lt $deadline ]; do
    pane_idle "$1" && return 0
    if tmux capture-pane -t "$1" -p 2>/dev/null | grep -q "trust this folder"; then
      tmux send-keys -t "$1" Enter; sleep 3
    fi
    sleep 3
  done
  return 1
}

tmux kill-session -t $SES 2>/dev/null || true
tmux new-session -d -s $SES -c "$WORK" -x 200 -y 50   # shell-backed: adopt's case
tmux send-keys -t $SES -l "$CLAUDE_LINE"; sleep 1; tmux send-keys -t $SES Enter
accept_trust $SES
wait_idle $SES 120 || { row launch fail "claude never idle: $(tmux capture-pane -t $SES -p | tail -3 | tr '\n' ' ')"; exit 1; }
PANE="$(pane_of $SES)"
row launch info "pane=$PANE"
# A fresh session has no transcript file until its first turn; adopt
# correctly refuses (transcript_missing) until one exists.
send_turn $SES "Reply with exactly one word: ready" 120 || row first_turn fail "turn timeout"

# --- s01: adopt without --attended
"$CMBIN" adopt -t "$PANE" >/dev/null 2>&1
[ $? -eq 2 ] && row s01_no_attended pass || row s01_no_attended fail "rc=$?"

# --- s02: adopt of a bash pane
tmux new-session -d -s ${SES}bash -x 80 -y 24
BASHPANE="$(pane_of ${SES}bash)"
MSG="$(adopt "$BASHPANE" COMPACT_MANAGER_MANAGED_TRIGGER_PCT=0.99)"; RC=$?
{ [ $RC -ne 0 ] && echo "$MSG" | grep -q "binding refused"; } \
  && row s02_bash_pane pass "$MSG" || row s02_bash_pane fail "rc=$RC $MSG"
tmux kill-session -t ${SES}bash

# --- s03: adopt happy (high trigger so nothing fires yet)
MSG="$(adopt "$PANE" COMPACT_MANAGER_MANAGED_TRIGGER_PCT=0.99)"; RC=$?
WPID="$(echo "$MSG" | grep -o 'pid=[0-9]*' | cut -d= -f2)"
SID="$(sid_of)"
if [ $RC -eq 0 ] && [ -n "$WPID" ] && [ -n "$SID" ]; then
  row s03_adopt pass "pid=$WPID sid=$SID"
else
  row s03_adopt fail "rc=$RC $MSG"; exit 1
fi
HB1="$(python3 -c 'import json,sys,glob; print(json.load(open(glob.glob(sys.argv[1])[0]))["heartbeat_at"])' "$STATE/managed/leases/session-*.json")"
sleep 12
HB2="$(python3 -c 'import json,sys,glob; print(json.load(open(glob.glob(sys.argv[1])[0]))["heartbeat_at"])' "$STATE/managed/leases/session-*.json")"
python3 -c "import sys; sys.exit(0 if float('$HB2') > float('$HB1') else 1)" \
  && row s03_heartbeat pass "$HB1 -> $HB2" || row s03_heartbeat fail "$HB1 -> $HB2"

# --- s04: collision
MSG="$(adopt "$PANE" COMPACT_MANAGER_MANAGED_TRIGGER_PCT=0.99)"; RC=$?
{ [ $RC -ne 0 ] && echo "$MSG" | grep -qi "lease\|held"; } \
  && row s04_collision pass "$MSG" || row s04_collision fail "rc=$RC $MSG"

# --- s05: Ctrl-Z defer / fg return
tmux send-keys -t $SES C-z; sleep 6
if kill -0 "$WPID" 2>/dev/null; then
  J="$(jstates "$SID")"
  case "$J" in *CLEANUP_REQUIRED*) row s05_ctrlz fail "cleanup during fg loss: $J";;
    *) row s05_ctrlz pass "watcher waiting through fg loss";; esac
else
  row s05_ctrlz fail "watcher died on fg loss"
fi
tmux send-keys -t $SES -l "fg"; tmux send-keys -t $SES Enter
wait_idle $SES 60 || row s05_fg_return fail "pane not idle after fg"
sleep 8
kill -0 "$WPID" 2>/dev/null && row s05_fg_return pass "watcher alive after fg" \
  || row s05_fg_return fail "watcher dead after fg"

# --- s06: CLI stop
MSG="$("$CMBIN" stop "$SID" 2>&1)"; RC=$?
sleep 2
if [ $RC -eq 0 ] && ! kill -0 "$WPID" 2>/dev/null && [ ! -f "$STATE/managed/leases/session-$SID.json" ]; then
  row s06_stop pass "$MSG"
else
  row s06_stop fail "rc=$RC alive=$(kill -0 "$WPID" 2>/dev/null && echo y || echo n) $MSG"
fi

# --- s07: threshold trigger -> happy path
MSG="$(adopt "$PANE")"; RC=$?   # trigger = hard_pct 0.15
WPID="$(echo "$MSG" | grep -o 'pid=[0-9]*' | cut -d= -f2)"
[ $RC -eq 0 ] || { row s07_adopt fail "rc=$RC $MSG"; exit 1; }
send_turn $SES "Read the file filler_a.txt in full, then reply with exactly one word: done" 240 || row s07_fill fail "turn timeout"
boundary_done() { jstates "$SID" | grep -q BOUNDARY_CONFIRMED; }
if wait_for 360 boundary_done; then
  J="$(jstates "$SID")"
  ok=1
  for want in TRIGGERED PREPARED TYPED_VERIFIED SUBMITTED BOUNDARY_CONFIRMED; do
    echo "$J" | grep -q "$want" || { ok=0; row s07_happy fail "missing $want in $J"; break; }
  done
  [ $ok -eq 1 ] && row s07_happy pass "$J"
  echo "$J" | grep -q ACKED || row s07_acked info "ACKED not journaled (boundary landed same tick)"
  NONCE_OK="$(python3 -c 'import json,sys,glob; f=glob.glob(sys.argv[1]); print("y" if f and "[cm-" in json.load(open(f[0])).get("custom_instructions","") else "n")' "$STATE/packets/*.json")"
  [ "$NONCE_OK" = y ] && row s07_nonce pass || row s07_nonce fail "no [cm- nonce in packet custom_instructions"
else
  row s07_happy fail "no BOUNDARY_CONFIRMED in 360s; states=$(jstates "$SID"); pane=$(tmux capture-pane -t $SES -p | tail -3 | tr '\n' ' ')"
fi

# --- s08: half-typed text defers; never cleared, never typed over
kill -STOP "$WPID"
send_turn $SES "Read the file filler_b.txt in full, then reply with exactly one word: done" 240 || row s08_fill fail "turn timeout"
tmux send-keys -t $SES -l "harness half-typed do not submit"
kill -CONT "$WPID"
sleep 25
CAP="$(tmux capture-pane -t $SES -p -J | grep '^❯' | tail -1)"
if echo "$CAP" | grep -qF "harness half-typed do not submit" && ! echo "$CAP" | grep -q "/compact"; then
  row s08_half_typed pass "composer intact, deferred"
else
  row s08_half_typed fail "composer: $CAP"
fi
tmux send-keys -t $SES C-u; sleep 1   # harness clears; the watcher may not
boundary2_done() { [ "$(jstates "$SID" | grep -c BOUNDARY_CONFIRMED)" -ge 2 ]; }
wait_for 360 boundary2_done && row s08_resume pass "injected after clear" \
  || row s08_resume fail "states=$(jstates "$SID")"

# --- s09: foreign packet -> stand-down -> safety latch -> resolve
kill -STOP "$WPID"
send_turn $SES "Read the file filler_c.txt in full, then reply with exactly one word: done" 240 || row s09_fill fail "turn timeout"
python3 - "$STATE" "$SID" <<'EOF'
import glob, json, os, sys
state, sid = sys.argv[1], sys.argv[2]
path = os.path.join(state, "packets", f"{sid}.json")
os.makedirs(os.path.dirname(path), exist_ok=True)
json.dump({"seq": 9999, "trigger": "manual", "base_compaction_count": 9999,
           "custom_instructions": "foreign harness packet"}, open(path, "w"))
EOF
kill -CONT "$WPID"
latched() { grep -q foreign_uncertain "$STATE/managed/watchers/$SID.journal.jsonl" 2>/dev/null; }
if wait_for 240 latched; then
  J="$(jstates "$SID")"
  echo "$J" | grep -q ALERT_DELIVERY && row s09_foreign pass "$J" \
    || row s09_foreign fail "latched but no alert delivery: $J"
else
  row s09_foreign fail "no latch in 240s: $(jstates "$SID")"
fi
MSG="$("$CMBIN" resolve "$SID" 2>&1)"; RC=$?
[ $RC -eq 0 ] && row s09_resolve pass "$MSG" || row s09_resolve fail "rc=$RC $MSG"
rm -f "$STATE/packets/$SID.json"
send_turn $SES "/compact clear the deck" 300 || true
sleep 10

# --- s10: TYPED_VERIFIED crash tail -> SUBMISSION_UNCERTAIN -> resolve flow
"$CMBIN" stop "$SID" >/dev/null 2>&1; sleep 2
python3 - "$STATE" "$SID" <<'EOF'
import json, os, sys, time
state, sid = sys.argv[1], sys.argv[2]
path = os.path.join(state, "managed", "watchers", f"{sid}.journal.jsonl")
rec = {"schema": 1, "ts": time.time(), "state": "TYPED_VERIFIED",
       "attempt_id": "deadattempt", "run_token": "dead000000000000",
       "nonce": "d" * 16, "nonces": ["d" * 16],
       "generation": {"file_epoch": 0, "last_boundary_offset": None,
                       "last_boundary_sha256": None},
       "retry_n": 0, "packet_seq_at_prepare": 0,
       "attempt_packet_seq_floor": 0,
       "timers": {"boot_id": "dead-boot"}}
with open(path, "a") as fh:
    fh.write(json.dumps(rec) + "\n")
EOF
MSG="$(adopt "$PANE" COMPACT_MANAGER_MANAGED_TRIGGER_PCT=0.99)"; RC=$?
WPID="$(echo "$MSG" | grep -o 'pid=[0-9]*' | cut -d= -f2)"
echo "$MSG" | grep -q "recovered=SUBMISSION_UNCERTAIN" \
  && row s10_recover pass "$MSG" || row s10_recover fail "rc=$RC $MSG"
MSG="$("$CMBIN" resolve "$SID" 2>&1)"; RC=$?
[ $RC -ne 0 ] && row s10_resolve_live_refused pass "$MSG" \
  || row s10_resolve_live_refused fail "resolve accepted with live foreign-token lease: $MSG"
"$CMBIN" stop "$SID" >/dev/null 2>&1; sleep 2
MSG="$("$CMBIN" resolve "$SID" 2>&1)"; RC=$?
[ $RC -eq 0 ] && row s10_resolve_dead pass "$MSG" || row s10_resolve_dead fail "rc=$RC $MSG"

# --- s11: start mode — pane dies with claude, watcher retires
MSG="$(env COMPACT_MANAGER_MANAGED_TRIGGER_PCT=0.99 "$CMBIN" start --session-name cml2start -- claude --model haiku --settings "$WORK/settings.json" --allowedTools Read 2>&1)"; RC=$?
if [ $RC -eq 0 ]; then
  SPANE="$(pane_of cml2start)"
  SPID="$(echo "$MSG" | grep -o 'pid=[0-9]*' | cut -d= -f2)"
  wait_idle cml2start 120 || row s11_start fail "start session never idle"
  tmux send-keys -t cml2start -l "/exit"; sleep 1; tmux send-keys -t cml2start Enter
  pane_gone() { ! tmux has-session -t cml2start 2>/dev/null; }
  watcher_gone() { ! kill -0 "$SPID" 2>/dev/null; }
  wait_for 60 pane_gone && row s11_pane_dies pass || row s11_pane_dies fail "pane survives /exit"
  wait_for 60 watcher_gone && row s11_watcher_retires pass || row s11_watcher_retires fail "watcher outlived pane"
else
  row s11_start fail "rc=$RC $MSG"
fi

# --- s12: start with non-claude argv fails closed
env "$CMBIN" start -- bash >/dev/null 2>&1 && row s12_fail_closed fail "accepted bash" \
  || row s12_fail_closed pass

row done info "$(grep -c '"verdict": "pass"' "$OUT") pass / $(grep -c '"verdict": "fail"' "$OUT") fail"
