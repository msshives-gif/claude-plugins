"""SessionStart, two duties by source:

- "compact": opportunistic reorientation right after a compaction.
  Spike S1: this injection DOES reach the model in interactive
  sessions on current Claude Code (not across headless resumes). It
  never advances the drain CAS — the durable delivery stays with
  advisor/reorient, so if this fires the model may see the
  reorientation twice (harmless duplicate by design).
- "startup"/"resume"/"clear": in managed mode only, one status line
  saying whether a watcher holds THIS session. Without it, "mode is
  managed but nobody adopted this pane" looks identical to
  fully-enabled until the 80% line — the hooks fire, the config says
  managed, and the missing piece (a live session lease) is invisible.
  clear matters as much as startup: /clear rotates the session id,
  retiring any watcher (session_rotated), so the fresh id must hear
  that nobody holds it.
"""
import json
import os
import sys
import time

try:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import compact_manager as cm
except Exception as e:
    print(f"compact-manager session_start: import failed: {e!r}",
          file=sys.stderr)
    sys.exit(0)


def _emit(cfg, text):
    print(json.dumps({
        "suppressOutput": not cfg["system_message"],
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": text,
        },
    }))


def _reorient(cfg, session_id):
    paths = cm.state_paths(cfg, session_id)
    packet = cm.load_packet(paths)
    if not packet:
        return
    cm.ledger_append(cfg, {"event": "inject", "hook": "session_start",
                           "seq": packet.get("seq")})
    _emit(cfg, cm.reorientation_text(packet, cm.delivery_cap(cfg)))


def _watcher_status(cfg, session_id):
    if cfg["mode"] != "managed":
        return
    import managed
    lease_path = managed.managed_paths(cfg, session_id)["session_lease"]
    lease = None
    try:
        with open(lease_path) as fh:
            lease = json.load(fh)
    except (OSError, ValueError):
        lease = None
    cli = os.path.normpath(
        os.path.join(os.path.dirname(os.path.abspath(__file__)),
                     "..", "bin", "compact-manager"))
    if managed.lease_attached(lease):
        text = ("compact-manager: managed mode, watcher attached to this "
                "session (pid %s)." % lease.get("pid"))
    else:
        text = ("compact-manager: mode is managed but NO watcher holds "
                "this session — nothing will type /compact for you. "
                "Attach one with the attach command "
                "(/compact-manager:attach, or /compact-manager-attach on "
                "script installs) or: \"%s\" adopt -t \"$TMUX_PANE\" "
                "--attended" % cli)
    # Advertise the per-session threshold mechanism with the exact
    # command (this hook knows the real session id) so the model never
    # has to discover it — but only ever uses it when the human asks.
    # An id the CLI's own validator would reject is not baked into a
    # copy-pasteable command line.
    sid = (session_id if managed._SESSION_ID.fullmatch(session_id)
           else "<session-id>")
    text += (" If the user asks to change this session's compaction "
             "thresholds, run: \"%s\" override %s trigger=NN%% "
             "(keys: trigger/soft/hard/window, any subset; --clear "
             "resets)." % (cli, sid))
    cm.ledger_append(cfg, {"event": "inject", "hook": "session_start",
                           "kind": "watcher_status"})
    _emit(cfg, text)


def main():
    payload = cm.read_payload()
    if not isinstance(payload, dict):
        return
    cfg = cm.load_config()
    if cfg["mode"] == "off":
        return
    source = payload.get("source") or payload.get("session_started_from")
    session_id = payload.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        return
    if source == "compact":
        _reorient(cfg, session_id)
    elif source in ("startup", "resume", "clear"):
        # clear matters as much as startup: /clear rotates the session
        # id, which retires any watcher (session_rotated) — the fresh
        # id must hear that nobody holds it.
        _watcher_status(cfg, session_id)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"compact-manager session_start: {e!r}", file=sys.stderr)
    sys.exit(0)
