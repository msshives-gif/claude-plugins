"""PreCompact (manual + auto): persist the wake packet. ALWAYS exits 0
and never emits a decision — this hook must never block or alter a
compaction, only record what should survive it. Payload ground truth
(spike S5): trigger ("manual"|"auto") and custom_instructions
(verbatim text after /compact; empty for auto)."""
import json
import os
import sys

try:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import compact_manager as cm
except Exception as e:
    print(f"compact-manager precompact: import failed: {e!r}",
          file=sys.stderr)
    sys.exit(0)


def main():
    payload = cm.read_payload()
    cfg = cm.load_config()
    if cfg["mode"] == "off":
        return
    session_id = payload.get("session_id") or ""
    transcript = payload.get("transcript_path") or ""
    if not session_id:
        return
    paths = cm.state_paths(cfg, session_id)
    cm._private_makedirs(os.path.dirname(paths["lock"]))
    release = cm._locked_open(paths["lock"])
    try:
        st = cm.load_state(paths)
        if transcript:
            st = cm.incremental_scan(st, transcript)
        seq = cm.write_packet(
            cfg, paths, st,
            trigger=payload.get("trigger") or "",
            custom_instructions=payload.get("custom_instructions") or "",
            cwd=payload.get("cwd") or "")
        st["packet_seq"] = seq
        cm.save_state(cfg, paths, st)
        cm.ledger_append(cfg, {"event": "packet_written", "seq": seq,
                               "trigger": payload.get("trigger")})
    finally:
        release()


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"compact-manager precompact: {e!r}", file=sys.stderr)
    sys.exit(0)
