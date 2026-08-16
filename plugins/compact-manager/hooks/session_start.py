"""SessionStart, two duties by source:

- "compact": opportunistic reorientation right after a compaction.
  Spike S1: this injection DOES reach the model in interactive
  sessions on current Claude Code (not across headless resumes). It
  never advances the drain CAS — the durable delivery stays with
  advisor/reorient, so if this fires the model may see the
  reorientation twice (harmless duplicate by design).
- "startup"/"resume": in managed mode only, one status line saying
  whether a watcher holds THIS session. Without it, "mode is managed
  but nobody adopted this pane" looks identical to fully-enabled
  until the 80% line — the hooks fire, the config says managed, and
  the missing piece (a live session lease) is invisible.
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


def _lease_attached(managed, lease):
    """POSITIVE proof of a watcher, unlike managed.lease_is_live, whose
    ambiguous cases deliberately count as live so lease RECLAIM fails
    safe — the wrong default for a status display ({} would show as
    attached). Attached = well-formed lease AND (fresh heartbeat OR
    pid+starttime verified alive)."""
    if not isinstance(lease, dict):
        return False
    pid = lease.get("pid")
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return False
    token = lease.get("run_token")
    if not isinstance(token, str) or not token:
        return False
    if managed._finite_number(lease.get("proc_start")) is None:
        return False
    heartbeat = managed._finite_number(lease.get("heartbeat_at"))
    now = time.time()
    if heartbeat is not None and heartbeat <= now + 60 and \
            now - heartbeat < managed.LEASE_FRESH_S:
        return True
    return managed.proc_matches(pid, lease.get("proc_start"))


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
    if _lease_attached(managed, lease):
        text = ("compact-manager: managed mode, watcher attached to this "
                "session (pid %s)." % lease.get("pid"))
    else:
        adopt = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "..", "bin", "compact-manager")
        text = ("compact-manager: mode is managed but NO watcher holds "
                "this session — nothing will type /compact for you. "
                "Attach one with the attach command "
                "(/compact-manager:attach, or /compact-manager-attach on "
                "script installs) or: %s adopt -t \"$TMUX_PANE\" "
                "--attended" % os.path.normpath(adopt))
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
    elif source in ("startup", "resume"):
        _watcher_status(cfg, session_id)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"compact-manager session_start: {e!r}", file=sys.stderr)
    sys.exit(0)
