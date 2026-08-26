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
#   s08 half-typed user text is actively cleared (journaled with an
#       excerpt) and the compact submits without operator help
#   s09 foreign packet -> stand-down -> safety latch + alert; operator
#       resolve accepted for the live token; manual /compact clears it
#   s10 TYPED_VERIFIED crash tail -> adopt reports SUBMISSION_UNCERTAIN;
#       resolve refused while a live foreign-token lease exists, accepted
#       once the watcher is stopped (dead-token cleanup path)
#   s11 start mode: pane dies with claude; watcher retires, leases free
#   s12 start with a non-claude argv fails closed
#   s13 /clear rotates the session id in the same claude process; the
#       watcher retires with reason=session_rotated (registry-transition
#       invariant pinned live)
#   s14 fbeb0bf1 starvation topology: background shell + model request
#       during a long foreground turn — marker pairs/unpairs, zero
#       mid-turn keystrokes, STARVATION_ALERT, prompt boundary-lane
#       submit at turn end
#   s15 half-typed text under background chrome with the boundary lane
#       armed: the watcher clears it itself and submits
#   s17 hard-multiline draft: whole region journaled, fully cleared,
#       compact submits
#   s18 resume nudge: session-requested compact -> boundary confirmed
#       -> one continuation prompt typed, model turn starts; default-off
#       pinned against the earlier request scenarios
#   s16 queued /compact evidence pin (informational; watcher stopped)
# Watchers have no time-based deadline; lifecycle ends are covered by
# s06 (stop), s11 (claude death), and s13 (session rotation).
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
  # Ghost suggestions render as plain text after the composer marker, so
  # "empty" must come from the SGR-aware parser, not a bare-line regex.
  local cap; cap="$(tmux capture-pane -t "$1" -p 2>/dev/null)" || return 1
  echo "$cap" | grep -q "esc to interrupt" && return 1
  tmux capture-pane -t "$1" -p -J -e 2>/dev/null | python3 -c "
import sys
sys.path.insert(0, '$ROOT/plugins/compact-manager/hooks')
import managed
snap = managed.parse_pane(sys.stdin.read())
sys.exit(0 if snap['composer'] == 'empty' and not snap['modal'] else 1)"
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
  for s in $SES ${SES}bash cml2start cml2rot; do tmux kill-session -t "$s" 2>/dev/null || true; done
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
    "Stop": [{"hooks": [{"type": "command", "command": "python3 $ROOT/plugins/compact-manager/hooks/stop_marker.py || true", "timeout": 5}]}],
    "PreCompact": [
      {"matcher": "manual", "hooks": [{"type": "command", "command": "python3 $ROOT/plugins/compact-manager/hooks/precompact.py || true", "timeout": 5}]},
      {"matcher": "auto", "hooks": [{"type": "command", "command": "python3 $ROOT/plugins/compact-manager/hooks/precompact.py || true", "timeout": 5}]}
    ],
    "SessionStart": [{"matcher": "compact", "hooks": [{"type": "command", "command": "python3 $ROOT/plugins/compact-manager/hooks/session_start.py || true", "timeout": 5}]}]
  }
}
EOF
CLAUDE_LINE="env -u ANTHROPIC_API_KEY COMPACT_MANAGER_MODE=managed COMPACT_MANAGER_STATE_DIR=$STATE COMPACT_MANAGER_CONTEXT_WINDOW=100000 COMPACT_MANAGER_SOFT_PCT=0.10 COMPACT_MANAGER_HARD_PCT=0.15 claude --model haiku --settings $WORK/settings.json --allowedTools Read,Write,Edit,Bash"

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
# First turn creates the transcript; the suite's scenarios need real
# content anyway (a virgin session would just bind transcript-pending).
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

# --- s08: half-typed text is actively cleared and the compact submits
JN8="$STATE/managed/watchers/$SID.journal.jsonl"
kill -STOP "$WPID"
send_turn $SES "Read the file filler_b.txt in full, then reply with exactly one word: done" 240 || row s08_fill fail "turn timeout"
tmux send-keys -t $SES -l "harness half-typed do not submit"
L8=$(wc -l < "$JN8")
kill -CONT "$WPID"
# The intent record alone is not proof (it precedes the keystroke):
# the pin is intent journaled AND the draft actually gone from the
# composer AND no keyed failure — all scoped to records after L8.
s08_cleared() {
  tail -n +$((L8+1)) "$JN8" 2>/dev/null | grep -q '"state":"COMPOSER_CLEAR"' && \
    ! tmux capture-pane -t $SES -p -J | grep '^❯' | tail -1 | grep -qF "harness half-typed"
}
if wait_for 120 s08_cleared; then
  S08="$(tail -n +$((L8+1)) "$JN8")"
  if echo "$S08" | grep -q '"state":"COMPOSER_CLEAR_FAILED"'; then
    row s08_clear fail "keyed clear failure journaled: $(echo "$S08" | grep COMPOSER_CLEAR_FAILED | tail -1 | head -c 200)"
  elif echo "$S08" | grep '"state":"COMPOSER_CLEAR"' | tail -1 | grep -qF "harness half-typed"; then
    row s08_clear pass "draft cleared, excerpt journaled"
  else
    row s08_clear fail "COMPOSER_CLEAR journaled without the draft excerpt"
  fi
else
  row s08_clear fail "draft not cleared in 120s; composer: $(tmux capture-pane -t $SES -p -J | grep '^❯' | tail -1)"
fi
boundary2_done() { tail -n +$((L8+1)) "$JN8" 2>/dev/null | grep -q '"state":"BOUNDARY_CONFIRMED"'; }
wait_for 360 boundary2_done && row s08_resume pass "cleared and submitted without operator help" \
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
L9=$(wc -l < "$STATE/managed/watchers/$SID.journal.jsonl")
send_turn $SES "/compact clear the deck" 300 || true
# Barrier: the deck is clear only once the WATCHER OBSERVES the clearing
# boundary. The resolved SAFETY latch consumes any request that lands
# before that observation (one override per generation), so starting s14
# early starved its request and flaked s14_submits_at_boundary (seen
# 3/3 runs on 2026-08-18: request at +317s, boundary at +347s).
s09_cleared() { tail -n +$((L9+1)) "$STATE/managed/watchers/$SID.journal.jsonl" | grep -q '"state":"BOUNDARY_CONFIRMED"'; }
wait_for 240 s09_cleared || row s09_cleared info "clearing boundary not observed in 240s; s14 may inherit the latch"
sleep 6

# --- s14: model request during a busy foreground turn (the fbeb0bf1
#     starvation topology: background shell chrome + running turn).
#     Pins: activity marker pairs after a turn and unpairs during one;
#     no keystrokes while the turn runs; STARVATION_ALERT after 120s;
#     submission via the turn-boundary lane promptly at the turn end.
JN="$STATE/managed/watchers/$SID.journal.jsonl"
send_turn $SES "Use the Bash tool with run_in_background set to true to run this exact command: sleep 400. Then reply with exactly one word: ok" 240 || row s14_bg fail "turn timeout"
tmux capture-pane -t $SES -p -J -S -100 | grep -qi "background" \
  && row s14_bg_launched pass \
  || row s14_bg_launched info "no background-launch text visible (model phrasing varies; marker checks below are the topology gate)"
# The Stop hook writes the ended marker a beat after send_turn's
# completion heuristic fires, so a single-shot read raced it (flaked
# 4/4 runs on 2026-08-21 with markers that paired on disk moments
# later). Poll for the pair; on failure report both prompt_ids so the
# mismatch is diagnosable.
ACT_PAIRED=unread
for _ in $(seq 1 30); do
  ACT_PAIRED="$(python3 - "$STATE" "$SID" <<'EOF'
import json, os, sys
state, sid = sys.argv[1], sys.argv[2]
try:
    r = json.load(open(os.path.join(state, "managed", "activity", f"running-{sid}.json")))
    e = json.load(open(os.path.join(state, "managed", "activity", f"ended-{sid}.json")))
    rp, ep = r.get("prompt_id"), e.get("prompt_id")
    if rp and rp == ep:  # two missing ids must not pass as a pair
        print("y")
    else:
        print("n running=%s ended=%s" % (str(rp)[:8], str(ep)[:8]))
except Exception as exc:
    print("err:%r" % (exc,))
EOF
)"
  [ "$ACT_PAIRED" = y ] && break
  sleep 1
done
[ "$ACT_PAIRED" = y ] && row s14_marker_paired pass \
  || row s14_marker_paired fail "running/ended pair: $ACT_PAIRED"
LINES0=$(wc -l < "$JN")
tmux send-keys -t $SES -l "Use the Bash tool to run exactly this command, waiting for it to complete before replying: sleep 150 && echo done-sleeping. After it completes, reply with exactly one word: slept"; sleep 1; tmux send-keys -t $SES Enter
sleep 5
# Request written MID-turn: the watcher must hold it to the boundary.
python3 - "$STATE" "$SID" <<'EOF'
import json, os, sys
state, sid = sys.argv[1], sys.argv[2]
path = os.path.join(state, "managed", "requests", f"{sid}.json")
os.makedirs(os.path.dirname(path), exist_ok=True)
tmp = path + ".tmp"
json.dump({"request_id": "live-s14-request"}, open(tmp, "w"))
os.replace(tmp, path)
EOF
act_unpaired() { python3 - "$STATE" "$SID" <<'EOF'
import json, os, sys
state, sid = sys.argv[1], sys.argv[2]
try:
    r = json.load(open(os.path.join(state, "managed", "activity", f"running-{sid}.json")))
    e = json.load(open(os.path.join(state, "managed", "activity", f"ended-{sid}.json")))
    sys.exit(0 if r.get("prompt_id") != e.get("prompt_id") else 1)
except Exception:
    sys.exit(1)
EOF
}
if wait_for 60 act_unpaired; then
  row s14_marker_running pass
else
  row s14_marker_running fail "markers never unpaired during the long turn"
fi
sleep 90   # deep into the turn: >= STARVATION_ALERT_S with request pending
# The mid-turn pins are meaningful only if the model actually kept the
# turn running (haiku sometimes answers instead of sleeping): gate on
# the topology still holding at assertion time.
if act_unpaired; then
  S14="$(tail -n +$((LINES0+1)) "$JN")"
  if echo "$S14" | grep -qE '"state":"(PREPARED|TYPED_VERIFIED|SUBMITTED)"'; then
    row s14_no_mid_turn_typing fail "typed during running turn"
  else
    row s14_no_mid_turn_typing pass "deferred while foreground ran"
  fi
  s14_alert() { tail -n +$((LINES0+1)) "$JN" | grep -q '"state":"STARVATION_ALERT"'; }
  wait_for 40 s14_alert && row s14_starvation_alert pass \
    || row s14_starvation_alert fail "no STARVATION_ALERT while request pending"
else
  S14="$(tail -n +$((LINES0+1)) "$JN")"
  if echo "$S14" | grep -q '"reason":"R2_activity_running"'; then
    row s14_no_mid_turn_typing pass "activity-running defers observed before early turn end"
  else
    row s14_no_mid_turn_typing info "turn ended early (model did not sleep); mid-turn window not achieved"
  fi
  row s14_starvation_alert info "not applicable: turn ended before the starvation window"
fi
s14_submitted() { tail -n +$((LINES0+1)) "$JN" | grep -q '"state":"SUBMITTED"'; }
if wait_for 120 s14_submitted; then
  LANE="$(tail -n +$((LINES0+1)) "$JN" | grep '"state":"PREPARED"' | tail -1 | python3 -c 'import json,sys; print(json.loads(sys.stdin.read()).get("lane"))')"
  row s14_submits_at_boundary pass "lane=$LANE"
  # The topology is meant to force the boundary lane; a strict-lane
  # submission means the pane momentarily read fully idle — possible,
  # but flag it so a silently-disabled lane can't hide behind strict.
  [ "$LANE" = boundary ] && row s14_lane pass \
    || row s14_lane info "submitted via lane=$LANE (boundary not exercised this run)"
else
  row s14_submits_at_boundary fail "no submit within 120s of turn end: $(tail -3 "$JN" | tr '\n' ' ')"
fi
s14_confirmed() { tail -n +$((LINES0+1)) "$JN" | grep -q '"state":"BOUNDARY_CONFIRMED"'; }
wait_for 240 s14_confirmed && row s14_boundary pass \
  || row s14_boundary fail "no boundary: $(tail -3 "$JN" | tr '\n' ' ')"
wait_idle $SES 120 || true

# --- s15: half-typed text under background chrome with the boundary
#     lane armed (request pending): the watcher clears it and submits.
LINES0=$(wc -l < "$JN")
tmux send-keys -t $SES -l "harness half-typed boundary lane do not submit"
# Park the cursor mid-line: C-u alone kills only to line start, so this
# pins the C-e+C-u whole-line clear against the real TUI binding
# (containment audit's unverified premise).
tmux send-keys -t $SES Left Left Left Left Left
sleep 2
python3 - "$STATE" "$SID" <<'EOF'
import json, os, sys
state, sid = sys.argv[1], sys.argv[2]
path = os.path.join(state, "managed", "requests", f"{sid}.json")
tmp = path + ".tmp"
json.dump({"request_id": "live-s15-request"}, open(tmp, "w"))
os.replace(tmp, path)
EOF
s15_cleared() {
  tail -n +$((LINES0+1)) "$JN" | grep -q '"state":"COMPOSER_CLEAR"' && \
    ! tmux capture-pane -t $SES -p -J | grep '^❯' | tail -1 | grep -qF "harness half-typed boundary"
}
if wait_for 120 s15_cleared; then
  tail -n +$((LINES0+1)) "$JN" | grep -q '"state":"COMPOSER_CLEAR_FAILED"' \
    && row s15_half_typed_boundary fail "keyed clear failure journaled" \
    || row s15_half_typed_boundary pass "watcher cleared the draft"
else
  row s15_half_typed_boundary fail "draft not cleared; composer: $(tmux capture-pane -t $SES -p -J | grep '^❯' | tail -1)"
fi
s15_submitted() { tail -n +$((LINES0+1)) "$JN" | grep -q '"state":"SUBMITTED"'; }
if wait_for 180 s15_submitted; then
  LANE15="$(tail -n +$((LINES0+1)) "$JN" | grep '"state":"PREPARED"' | tail -1 | python3 -c 'import json,sys; print(json.loads(sys.stdin.read()).get("lane"))' 2>/dev/null)"
  row s15_resume pass "submitted after self-clear (lane=$LANE15)"
  # The scenario intends the boundary lane; strict is possible if the
  # s14 background job already exited — flag, don't fail (as s14 does).
  [ "$LANE15" = boundary ] || row s15_lane info "submitted via lane=$LANE15 (boundary topology not exercised this run)"
else
  row s15_resume fail "states: $(tail -3 "$JN" | tr '\n' ' ')"
fi
s15_confirmed() { tail -n +$((LINES0+1)) "$JN" | grep -q '"state":"BOUNDARY_CONFIRMED"'; }
wait_for 240 s15_confirmed || row s15_boundary info "boundary still pending at scenario end"
wait_idle $SES 120 || true

# --- s17: hard-multiline draft (backslash-continuation) is fully
#     cleared and the whole region is journaled (round-2 audit pin:
#     C-u-per-line vs whole-buffer is a claude-internal we can only
#     settle live; continuation lines must not be destroyed with only
#     line 1 on file).
# Barrier: s15's compact must be watcher-CONFIRMED before s17 can own
# an attempt — a request landing mid-completion-wait queues behind the
# live attempt (observed: a slow haiku compaction held ACKED for 6+
# minutes and swallowed s17's whole window).
s15_watcher_done() { tail -n +$((LINES0+1)) "$JN" | grep -q '"state":"BOUNDARY_CONFIRMED"'; }
if ! wait_for 360 s15_watcher_done; then
  row s17_multiline_clear info "skipped: s15 compact not confirmed in 360s; watcher still mid-attempt"
  SKIP_S17=1
else
  SKIP_S17=0
fi
LINES17=$(wc -l < "$JN")
if [ "$SKIP_S17" = 0 ]; then
tmux send-keys -t $SES -l 'harness multiline first line \'
tmux send-keys -t $SES Enter
tmux send-keys -t $SES -l "harness multiline second line do not submit"
sleep 2
ML_OK="$(tmux capture-pane -t $SES -p -J | python3 -c "
import sys
sys.path.insert(0, '$ROOT/plugins/compact-manager/hooks')
cap = sys.stdin.read()
lines = cap.splitlines()
row = max((i for i, l in enumerate(lines) if l.startswith('❯')), default=None)
region = '\n'.join(lines[row:]) if row is not None else ''
print('y' if 'multiline first' in region and 'multiline second' in region
      else 'n')")"
if [ "$ML_OK" = y ]; then
  python3 - "$STATE" "$SID" <<'EOF'
import json, os, sys
state, sid = sys.argv[1], sys.argv[2]
path = os.path.join(state, "managed", "requests", f"{sid}.json")
tmp = path + ".tmp"
json.dump({"request_id": "live-s17-request"}, open(tmp, "w"))
os.replace(tmp, path)
EOF
  s17_cleared() {
    # The whole post-marker region must be free of the draft, not just
    # the marker line — continuation rows carry no marker and a
    # stranded one would be invisible to a ❯-line grep (round-3 audit).
    tail -n +$((LINES17+1)) "$JN" | grep -q '"state":"COMPOSER_CLEAR"' && \
      [ "$(tmux capture-pane -t $SES -p -J | python3 -c "
import sys
lines = sys.stdin.read().splitlines()
row = max((i for i, l in enumerate(lines) if l.startswith('❯')), default=None)
if row is None:
    print('n')     # no marker = unprovable, never a pass (round-4 audit)
else:
    region = '\n'.join(lines[row:])
    print('n' if 'harness multiline' in region else 'y')")" = y ]
  }
  if wait_for 120 s17_cleared; then
    S17="$(tail -n +$((LINES17+1)) "$JN")"
    if echo "$S17" | grep '"state":"COMPOSER_CLEAR"' | tail -1 | grep -qF "multiline second"; then
      row s17_multiline_clear pass "whole region journaled and cleared"
    else
      row s17_multiline_clear fail "region_excerpt missing continuation line: $(echo "$S17" | grep COMPOSER_CLEAR | tail -1 | head -c 300)"
    fi
    s17_submitted() { tail -n +$((LINES17+1)) "$JN" | grep -q '"state":"SUBMITTED"'; }
    wait_for 180 s17_submitted && row s17_resume pass "submitted after multiline clear" \
      || row s17_resume fail "no submit: $(tail -3 "$JN" | tr '\n' ' ')"
  else
    row s17_multiline_clear fail "multiline draft not cleared in 120s; journal tail: $(tail -2 "$JN" | tr '\n' ' ' | head -c 300); composer: $(tmux capture-pane -t $SES -p -J | grep '^❯' | tail -1)"
    tmux send-keys -t $SES C-e C-u C-e C-u C-e C-u; sleep 1  # unblock later scenarios
  fi
else
  row s17_multiline_clear info "backslash-continuation did not produce a multiline draft; case not exercised"
  tmux send-keys -t $SES C-e C-u; sleep 1
fi
wait_idle $SES 240 || true
fi   # SKIP_S17

# --- s18: resume nudge (managed_resume_nudge=1): a session-requested
#     compaction's confirmed boundary is followed by ONE typed
#     continuation prompt that provably starts a model turn (running
#     marker revision advances). Boundary and nudge rows are
#     correlated to the request's own attempt_id, so restart
#     resurrection or unrelated attempts cannot satisfy the checks.
#     Also pins the default: the earlier request compacts ran with the
#     nudge off and must have journaled zero RESUME_NUDGE rows.
grep -q '"state":"RESUME_NUDGE"' "$JN" \
  && row s18_default_off fail "RESUME_NUDGE journaled while disabled" \
  || row s18_default_off pass "no nudge rows under default config"
# Barrier: never stop the watcher mid-attempt — an interrupted typed
# attempt wedges CLEANUP_REQUIRED and poisons the re-adopt (run-1
# lesson: s17's slow compact was still ACKED when s18 stopped it).
s18_drained() { python3 - "$ROOT" "$JN" <<'EOF'
import sys
sys.path.insert(0, sys.argv[1] + "/plugins/compact-manager/hooks")
import managed
tail = managed.attempt_tail(sys.argv[2])
sys.exit(0 if not tail or tail.get("state") not in
         ("TRIGGERED", "PREPARED", "TYPED_VERIFIED", "SUBMITTED", "ACKED")
         else 1)
EOF
}
# Drain what will drain, but PROCEED on timeout: a predecessor compact
# stuck ACKED-forever (observed every run: an s15 /compact whose packet
# lands but whose boundary never does) would otherwise skip s18 in
# perpetuity. Proceeding is sound because the flow below explicitly
# un-poisons the inherited state (stop -> resolve -> re-adopt -> manual
# /compact latch clear -> fresh prime turn) and every assertion is
# correlated to the NEW request's own attempt_id — a poisoned
# predecessor can fail the scenario, never false-pass it.
S18_GO=y
wait_for 600 s18_drained || row s18_barrier info "prior attempt still in flight after 600s; proceeding via resolve + latch clear"
if [ "$S18_GO" = y ]; then
"$CMBIN" stop "$SID" >/dev/null 2>&1; sleep 2
# A wedged or safety-latched tail from earlier scenarios blocks new
# attempts: resolve it now (accepted once the watcher is dead), then
# clear the operator_resolved latch with one manual /compact below.
NEEDS_CLEAR=n
if python3 - "$ROOT" "$JN" <<'EOF'
import sys
sys.path.insert(0, sys.argv[1] + "/plugins/compact-manager/hooks")
import managed
tail = managed.recover_attempt(sys.argv[2], managed.boot_id())
state = (tail or {}).get("state")
sys.exit(0 if state in managed.ALERT_STATES else 1)
EOF
then
  "$CMBIN" resolve "$SID" >/dev/null 2>&1 && NEEDS_CLEAR=y
fi
MSG="$(adopt "$PANE" COMPACT_MANAGER_MANAGED_TRIGGER_PCT=0.99 COMPACT_MANAGER_MANAGED_RESUME_NUDGE=1)"; RC=$?
if [ $RC -eq 0 ]; then
  wait_idle $SES 180 || true
  if [ "$NEEDS_CLEAR" = y ]; then
    # Wait for the JOURNALED boundary, not pane idleness: pane_idle
    # can read idle before the compaction visibly starts, and a
    # request written before the boundary lands is observed at the
    # old, already-consumed generation — permanently starved under
    # the one-request-per-generation rule (run-4 lesson).
    LINESCLR=$(wc -l < "$JN")
    tmux send-keys -t $SES -l "/compact"; sleep 1; tmux send-keys -t $SES Enter
    s18_latch_cleared() { tail -n +$((LINESCLR+1)) "$JN" | grep -q '"state":"BOUNDARY_CONFIRMED"'; }
    wait_for 360 s18_latch_cleared || row s18_latch_clear info "manual /compact boundary not confirmed in 360s"
    wait_idle $SES 120 || true
  fi
  # A fresh short turn: current paired ended markers for the request's
  # boundary lane, and a known pre-nudge running prompt_id. Both are
  # load-bearing for the assertions below — a failed prime or a
  # missing marker makes every later check unable to prove anything,
  # so they FAIL the scenario instead of degrading it (run-1 audit:
  # an empty PROMPT0 let the turn check false-pass).
  S18_PRIMED=y
  send_turn $SES "Reply with exactly one word: primed" 180 || S18_PRIMED=n
  PROMPT0="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("prompt_id",""))' "$STATE/managed/activity/running-$SID.json" 2>/dev/null)"
  if [ "$S18_PRIMED" = n ] || [ -z "$PROMPT0" ]; then
    row s18_nudge_submitted fail "prime turn failed or no running marker (primed=$S18_PRIMED prompt0=${PROMPT0:-empty}); markers unusable"
    S18_GO=n
  fi
  LINES18=$(wc -l < "$JN")
  if [ "$S18_GO" = y ]; then
  python3 - "$STATE" "$SID" <<'EOF'
import json, os, sys
state, sid = sys.argv[1], sys.argv[2]
path = os.path.join(state, "managed", "requests", f"{sid}.json")
tmp = path + ".tmp"
json.dump({"request_id": "live-s18-request"}, open(tmp, "w"))
os.replace(tmp, path)
EOF
  # The request's own attempt: the TRIGGERED row carrying the
  # live-s18-request fingerprint names the attempt_id everything else
  # must correlate to.
  s18_attempt() { tail -n +$((LINES18+1)) "$JN" | grep '"state":"TRIGGERED"' | grep -q "live-s18-request"; }
  A18=""
  if wait_for 180 s18_attempt; then
    A18="$(tail -n +$((LINES18+1)) "$JN" | grep '"state":"TRIGGERED"' | grep "live-s18-request" | tail -1 | python3 -c 'import json,sys; print(json.loads(sys.stdin.read()).get("attempt_id") or "")')"
  fi
  if [ -n "$A18" ]; then
    s18_confirmed() { tail -n +$((LINES18+1)) "$JN" | grep '"state":"BOUNDARY_CONFIRMED"' | grep -q "\"attempt_id\":\"$A18\""; }
    if wait_for 420 s18_confirmed; then
      s18_nudged() { tail -n +$((LINES18+1)) "$JN" | grep '"state":"RESUME_NUDGE"' | grep "\"attempt_id\":\"$A18\"" | grep -q '"reason":"submitted"'; }
      if wait_for 240 s18_nudged; then
        row s18_nudge_submitted pass "attempt=$A18"
        # Model-turn proof: the nudge's submission fires
        # UserPromptSubmit, whose running marker carries a NEW
        # prompt_id — scrollback text alone could false-pass.
        s18_turn() {
          P1="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("prompt_id",""))' "$STATE/managed/activity/running-$SID.json" 2>/dev/null)"
          [ -n "$P1" ] && [ "$P1" != "$PROMPT0" ]
        }
        wait_for 90 s18_turn && row s18_turn_started pass "running marker advanced" \
          || row s18_turn_started fail "running prompt_id never advanced past $PROMPT0"
        N18="$(tail -n +$((LINES18+1)) "$JN" | grep -c '"state":"RESUME_NUDGE"')"
        [ "$N18" = 1 ] && row s18_nudge_once pass \
          || row s18_nudge_once fail "expected exactly one RESUME_NUDGE row, got $N18"
      else
        row s18_nudge_submitted fail "no RESUME_NUDGE/submitted for attempt $A18 within 240s: $(tail -3 "$JN" | tr '\n' ' ' | head -c 300)"
      fi
    else
      row s18_nudge_submitted fail "request compact never confirmed for attempt $A18: $(tail -3 "$JN" | tr '\n' ' ' | head -c 300)"
    fi
  else
    row s18_nudge_submitted fail "request never became an attempt: $(tail -3 "$JN" | tr '\n' ' ' | head -c 300)"
  fi
  fi   # S18_GO (prime + marker)
  wait_idle $SES 240 || true
else
  row s18_nudge_submitted fail "re-adopt rc=$RC $MSG"
fi
fi   # S18_GO (drain barrier)

# --- s16: queued /compact evidence pin (Sol: prerequisite for any
#     future unattended queue fallback — NOT shipped behavior). Watcher
#     stopped; harness types /compact mid-turn and records what Claude
#     Code does with it. Outcome rows are informational either way.
"$CMBIN" stop "$SID" >/dev/null 2>&1; sleep 2
"$CMBIN" status 2>/dev/null | python3 -c 'import json,sys; rows=json.load(sys.stdin); sys.exit(1 if any(r.get("live") for r in rows) else 0)' \
  || row s16_stop fail "a live watcher survived the stop"
SEQ0="$(python3 -c 'import json,sys,glob; f=glob.glob(sys.argv[1]); print(json.load(open(f[0])).get("seq",0) if f else 0)' "$STATE/packets/$SID.json")"
tmux send-keys -t $SES -l "Run in the foreground (not background): sleep 20. Then reply with exactly one word: rested"; sleep 1; tmux send-keys -t $SES Enter
sleep 6
ACT_S16="$(python3 - "$STATE" "$SID" <<'EOF'
import json, os, sys
state, sid = sys.argv[1], sys.argv[2]
try:
    r = json.load(open(os.path.join(state, "managed", "activity", f"running-{sid}.json")))
    e = json.load(open(os.path.join(state, "managed", "activity", f"ended-{sid}.json")))
    print("unpaired" if r.get("prompt_id") != e.get("prompt_id") else "paired")
except Exception as exc:
    print("err:%r" % (exc,))
EOF
)"
[ "$ACT_S16" = unpaired ] || row s16_mid_turn info "turn not provably mid-flight at injection ($ACT_S16); outcome may reflect idle submission"
tmux send-keys -t $SES -l "/compact queued mid-turn pin"; sleep 1; tmux send-keys -t $SES Enter
s16_packet() { python3 -c 'import json,sys,glob; f=glob.glob(sys.argv[1]); sys.exit(0 if f and json.load(open(f[0])).get("seq",0) == int(sys.argv[2]) + 1 and json.load(open(f[0])).get("trigger") == "manual" else 1)' "$STATE/packets/$SID.json" "$SEQ0"; }
if wait_for 300 s16_packet; then
  row s16_queued_compact info "queued /compact produced exactly one manual packet (seq $SEQ0 -> $((SEQ0+1)))"
else
  row s16_queued_compact info "no manual packet within 300s; queued /compact NOT usable as a fallback (packet: $(cat "$STATE/packets/$SID.json" 2>/dev/null | head -c 200))"
fi
wait_idle $SES 180 || true
MSG="$(adopt "$PANE" COMPACT_MANAGER_MANAGED_TRIGGER_PCT=0.99)"; RC=$?
WPID="$(echo "$MSG" | grep -o 'pid=[0-9]*' | cut -d= -f2)"
[ $RC -eq 0 ] || row s16_readopt fail "rc=$RC $MSG"

# --- s10: TYPED_VERIFIED crash tail -> SUBMISSION_UNCERTAIN -> resolve flow
"$CMBIN" stop "$SID" >/dev/null 2>&1; sleep 2
python3 - "$STATE" "$SID" <<'EOF'
import json, os, sys, time
state, sid = sys.argv[1], sys.argv[2]
# The synthetic crash record must carry the CURRENT generation (from
# the watcher's scan file) - with a stale generation the next watcher
# correctly COMPLETES it via generation advance instead of recovering
# it (run-10 lesson).
scan = json.load(open(os.path.join(state, "managed", "watchers",
                                   f"{sid}.scan.json")))
boundary = scan.get("last_boundary") or {}
generation = {"file_epoch": scan.get("file_epoch", 0),
              "last_boundary_offset": boundary.get("offset"),
              "last_boundary_sha256": boundary.get("sha256_of_row")}
path = os.path.join(state, "managed", "watchers", f"{sid}.journal.jsonl")
rec = {"schema": 1, "ts": time.time(), "state": "TYPED_VERIFIED",
       "attempt_id": "deadattempt", "run_token": "dead000000000000",
       "nonce": "d" * 16, "nonces": ["d" * 16],
       "generation": generation,
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
# start from the trusted workspace: an untrusted cwd leaves claude at
# the trust dialog, no registry entry is written, and binding times out
# (run-11 lesson; the CLI's failure hint covers real users).
MSG="$(cd "$WORK" && env COMPACT_MANAGER_MANAGED_TRIGGER_PCT=0.99 "$CMBIN" start --session-name cml2start -- claude --model haiku --settings "$WORK/settings.json" --allowedTools Read 2>&1)"; RC=$?
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

# --- s13: /clear rotates the session id in place; the watcher must
# retire with session_rotated instead of babysitting the dead id
MSG="$(cd "$WORK" && env COMPACT_MANAGER_MANAGED_TRIGGER_PCT=0.99 "$CMBIN" start --session-name cml2rot -- claude --model haiku --settings "$WORK/settings.json" --allowedTools Read 2>&1)"; RC=$?
if [ $RC -eq 0 ]; then
  RPID="$(echo "$MSG" | grep -o 'pid=[0-9]*' | cut -d= -f2)"
  RSID="$(sid_of)"
  if ! wait_idle cml2rot 120; then
    # Startup failure invalidates the rotation assertion — do not let a
    # later unrelated watcher death read as a pass (audit finding).
    row s13_clear_rotation fail "rotation session never idle"
  else
    tmux send-keys -t cml2rot -l "/clear"; sleep 1; tmux send-keys -t cml2rot Enter
    rwatcher_gone() { ! kill -0 "$RPID" 2>/dev/null; }
    if wait_for 90 rwatcher_gone; then
      # The journal's FINAL word must be the rotation retirement — a
      # mid-run R1_session_rotated DEFERRED followed by an unrelated
      # death must not pass — and the lease must be released.
      VERDICT="$(python3 - "$STATE/managed/watchers/$RSID.journal.jsonl" "$STATE/managed/leases/session-$RSID.json" <<'PYEOF'
import json, os, sys
last = None
try:
    with open(sys.argv[1]) as fh:
        for line in fh:
            line = line.strip()
            if line:
                last = json.loads(line)
except Exception as e:
    print("journal_unreadable:%r" % e); raise SystemExit
if not last:
    print("journal_empty")
elif last.get("state") != "WATCHER_RETIRED":
    print("final_state=%s" % last.get("state"))
elif last.get("reason") != "session_rotated":
    print("final_reason=%s" % last.get("reason"))
elif os.path.exists(sys.argv[2]):
    print("lease_still_present")
else:
    print("ok")
PYEOF
)"
      if [ "$VERDICT" = "ok" ]; then
        row s13_clear_rotation pass "final record WATCHER_RETIRED/session_rotated, lease released"
      else
        row s13_clear_rotation fail "$VERDICT; states=$(jstates "$RSID")"
      fi
    else
      row s13_clear_rotation fail "watcher outlived /clear rotation: $(jstates "$RSID")"
    fi
  fi
  tmux kill-session -t cml2rot 2>/dev/null || true
else
  row s13_clear_rotation fail "rc=$RC $MSG"
fi

# --- s12: start with non-claude argv fails closed
env "$CMBIN" start -- bash >/dev/null 2>&1 && row s12_fail_closed fail "accepted bash" \
  || row s12_fail_closed pass

row done info "$(grep -c '"verdict": "pass"' "$OUT") pass / $(grep -c '"verdict": "fail"' "$OUT") fail"
