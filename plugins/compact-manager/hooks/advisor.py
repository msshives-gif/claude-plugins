"""PostToolUse (all tools): the load-bearing hook. Measures the
session's OWN transcript incrementally, injects one advisory per
threshold crossing (with hysteresis), and delivers the wake packet on
the first tool call after a detected compaction."""
import json
import os
import sys
import time

try:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import compact_manager as cm
except Exception as e:
    print(f"compact-manager advisor: import failed: {e!r}",
          file=sys.stderr)
    sys.exit(0)


def run(payload, cfg):
    session_id = payload.get("session_id") or ""
    transcript = payload.get("transcript_path") or ""
    if not session_id or not transcript:
        return None
    paths = cm.state_paths(cfg, session_id)
    cm._private_makedirs(os.path.dirname(paths["lock"]))
    release = cm._locked_open(paths["lock"])
    try:
        st = cm.load_state(paths)
        before_boundaries = st.get("boundaries", 0)
        st = cm.incremental_scan(st, transcript)
        parts = []
        # Compaction landed since last look: reset the advisory arming
        # and deliver the packet (exactly-once via the seq CAS).
        if st.get("boundaries", 0) > before_boundaries:
            st["advisory_level"] = "none"
            # A compaction is a fresh crossing budget for the
            # unwatched warning too: without this, one scan spanning
            # boundary + re-climb would keep the flag stuck True.
            st["unwatched_warned"] = False
        text = cm.drain_packet(cfg, paths, st)
        if text:
            parts.append(text)
        eff = cm.window_for(cfg, st.get("model"))
        # Stamp the BASE effective thresholds (env-honoring, per-model
        # merged, but PRE-override) into the state file: readouts
        # prefer the stamps over re-deriving from their own config,
        # which cannot see this session's env overrides — and they
        # overlay the override file themselves, so overrides must not
        # be baked into the stamps: a merged stamp would keep showing
        # an override after --clear removed it (audit finding).
        st["eff_window"] = eff["context_window"]
        st["eff_soft_pct"] = eff["soft_pct"]
        st["eff_hard_pct"] = eff["hard_pct"]
        try:
            import managed as mg
            st["eff_trigger_pct"] = mg.trigger_for(mg.load_config(),
                                                   st.get("model"))
        except Exception:
            st["eff_trigger_pct"] = eff["hard_pct"]
        # The advisories themselves DO honor the overrides. One
        # snapshot for the whole run: a concurrent override write must
        # not mix generations between this merge and the unwatched
        # check below.
        ov = cm.session_overrides(cfg, session_id)
        eff = cm.apply_overrides(eff, ov)
        level, advisory = cm.advise(st, eff, paths["handoff"],
                                    cfg["rearm_band_pct"])
        if advisory:
            st["armed_at_ts"] = time.time()
            if cfg["mode"] == "managed":
                req = os.path.join(
                    cfg["state_dir"], "managed", "requests",
                    f"{cm.path_component(session_id)}.json")
                advisory += (
                    f" (managed mode: when you are AT the natural "
                    f"boundary, you may instead request compaction "
                    f"yourself by writing "
                    f'{{"request_id": "<8-64 letters/digits/dashes>"}} '
                    f"to {req} — the watcher compacts at the next "
                    f"safe idle moment.)")
            parts.append(advisory)
        st["advisory_level"] = level
        # Managed mode's blind spot: a session whose watcher retired or
        # died mid-flight climbs past its trigger with nobody holding
        # it, and the SessionStart notice never re-fires. Warn from
        # here — once per crossing, re-armed when usage drops back
        # below trigger (i.e. after a compaction or restart). Fails
        # silent on any surprise, per hook discipline.
        try:
            if cfg["mode"] != "managed":
                # A mode round-trip (managed → advisory → managed)
                # must not resurrect a suppressed warning from the
                # earlier managed stint. mode=off never reaches here
                # (the hook exits first) — that's fine: a NEW crossing
                # after an off interlude requires a compaction, and
                # the boundary branch above resets the flag on the
                # first managed scan spanning it; an off round-trip
                # with no compaction is still the SAME crossing, whose
                # warning was already delivered.
                st["unwatched_warned"] = False
            else:
                import managed as mg
                # ov is the run's single override snapshot (above);
                # eff already carries its window merge.
                trig = ov.get("managed_trigger_pct",
                              st.get("eff_trigger_pct") or eff["hard_pct"])
                window = eff["context_window"]
                above = window > 0 and \
                    (st.get("current", 0) or 0) / window >= trig
                if not above:
                    st["unwatched_warned"] = False
                elif not st.get("unwatched_warned"):
                    lease, _inode = mg.read_json_inode(os.path.join(
                        cfg["state_dir"], "managed", "leases",
                        "session-%s.json"
                        % cm.path_component(session_id)))
                    # None = ambiguous read (e.g. a heartbeat's atomic
                    # replace mid-flight): proves nothing — stay
                    # silent this tick rather than warn falsely and
                    # stick the warned flag. {} = positively absent.
                    if lease is not None and not mg.lease_attached(lease):
                        st["unwatched_warned"] = True
                        pct = 100.0 * (st.get("current", 0) or 0) / window
                        parts.append(
                            "compact-manager: managed mode, but NO "
                            "live watcher holds this session at %.0f%% "
                            "(trigger %.0f%%) — nothing will compact "
                            "it. Tell the user now so THEY can attach "
                            "a watcher (/compact-manager:attach, or "
                            "/compact-manager-attach on script "
                            "installs). Do not attach or adopt one "
                            "yourself." % (pct, 100.0 * trig))
        except Exception:
            pass
        cm.save_state(cfg, paths, st)
        if parts:
            cm.ledger_append(cfg, {"event": "inject",
                                   "hook": "advisor",
                                   "level": level,
                                   "n": len(parts)})
            return "\n\n".join(parts)
        return None
    finally:
        release()
        cm.prune_state(cfg)


def main():
    payload = cm.read_payload()
    cfg = cm.load_config()
    if cfg["mode"] == "off":
        return
    text = run(payload, cfg)
    if text:
        print(json.dumps({
            "suppressOutput": not cfg["system_message"],
            "hookSpecificOutput": {
                "hookEventName": "PostToolUse",
                "additionalContext": text,
            },
        }))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"compact-manager advisor: {e!r}", file=sys.stderr)
    sys.exit(0)
