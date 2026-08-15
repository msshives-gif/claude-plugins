"""PreToolUse hook on SendMessage: warn before waking a large idle
PEER SESSION.

Waking a peer replays its whole transcript; past the prompt-cache TTL
that's a full-price cold read (the motivating incident: an accidental
ping to a forked ~100k-token idle peer). Below warn_tokens: silent.
At warn_tokens: a warning with the measured size, cold/warm state, and
an estimated cold-read cost. At block_tokens AND cold, from the root
session: the send needs explicit confirmation ("ask" — overridable;
headless runs treat it as a refusal).

Domain is disjoint from subagent-context's guard by construction: this
hook fires only for targets that resolve to a live entry in the
harness's ~/.claude/sessions registry; in-process subagents never
appear there. Unresolvable targets are silent. Every path fails open.
"""
import json
import os
import sys

try:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import _core
    import config
    import peers
except Exception as e:
    print(f"cross-session-send-guard: import failed: {e!r}",
          file=sys.stderr)
    sys.exit(0)

# Skip measuring transcripts bigger than this (same rationale as the
# sibling guard: _scan has no internal deadline; stay inside the 5s
# hook budget). Oversized peers are exactly the ones worth warning
# about, so emit a size-unknown warning rather than staying silent.
MEASURE_MAX_BYTES = 50_000_000


def main():
    payload = json.loads(sys.stdin.read())
    if payload.get("tool_name") != "SendMessage":
        return
    cfg = config.load_config()
    if not cfg["enabled"]:
        return
    target = (payload.get("tool_input") or {}).get("to") or ""
    peer = peers.resolve_peer(target, payload, cfg,
                              proc_root=cfg["proc_root"])
    if not peer:
        return

    import time
    try:
        size = os.path.getsize(peer["transcript"])
        age_s = max(0, time.time() - os.path.getmtime(peer["transcript"]))
    except Exception:
        return
    cold = age_s > cfg["cache_ttl_seconds"]

    res = None
    if size <= MEASURE_MAX_BYTES:
        try:
            res = _core._scan(peer["transcript"])
        except Exception:
            res = None
    if res and res["rows"]:
        current = res["current"]
        model = ""  # registry doesn't record the model; use globals
    else:
        current = None
        model = ""
    th = config.thresholds(cfg, model)

    if current is not None and current < th["warn_tokens"]:
        return
    if current is None and size <= MEASURE_MAX_BYTES:
        return  # resolvable but unmeasurable (no usage rows): stay silent

    if current is not None:
        size_txt = f"~{current / 1000:.0f}k tokens of context"
        cost = current / 1_000_000 * cfg["usd_per_mtok"]
        cost_txt = (f"; a cold wake re-reads all of it "
                    f"(est. ~${cost:.2f})" if cold else "")
    else:
        size_txt = (f"a transcript too large to measure quickly "
                    f"({size / 1_000_000:.0f}MB)")
        cost_txt = "; a cold wake re-reads all of it" if cold else ""
    state_txt = (f"cold (idle ~{age_s / 60:.0f}m, past the cache TTL)"
                 if cold else f"warm (active ~{age_s / 60:.0f}m ago)")

    warn = (f"[cross-session-send-guard] '{_core.sanitize(peer['name'], 60)}'"
            f" is a peer session with {size_txt}, currently {state_txt}"
            f"{cost_txt}. If you only need to hand off information, write "
            "it to a file or the handoff instead of waking the peer; wake "
            "it only if you need ITS context. Queued messages drain in one "
            "context read — batch into one send rather than several pings.")

    out = {
        "suppressOutput": not cfg["system_message"],
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "additionalContext": warn,
        },
    }
    # Confirmation only from the root session (a subagent may have no
    # one to answer; an unanswerable ask is a hard block) and only for
    # a big COLD peer — warm peers are cheap to message.
    big = (current is not None and th["block_tokens"]
           and current >= th["block_tokens"]) or size > MEASURE_MAX_BYTES
    if not payload.get("agent_id") and cold and big and th["block_tokens"]:
        out["hookSpecificOutput"]["permissionDecision"] = "ask"
        out["hookSpecificOutput"]["permissionDecisionReason"] = warn
    print(json.dumps(out))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"cross-session-send-guard: {e!r}", file=sys.stderr)
    sys.exit(0)
