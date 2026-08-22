"""compact-manager Layer 1 tests: incremental measurement, advisory
decisions, packet lifecycle, config, and fail-open hook shells.
Fixture rows mirror the real shapes pinned by spike S3
(fixtures/transcripts/compact_rows.jsonl in the repo root)."""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest

HOOKS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..",
                     "hooks")
sys.path.insert(0, HOOKS)
import compact_manager as cm  # noqa: E402


def usage_row(inp, cr, cc, out, stop="end_turn", model="claude-haiku-4-5"):
    return {"type": "assistant",
            "message": {"role": "assistant", "stop_reason": stop,
                        "model": model,
                        "usage": {"input_tokens": inp,
                                  "cache_read_input_tokens": cr,
                                  "cache_creation_input_tokens": cc,
                                  "output_tokens": out}}}


def boundary_row(trigger, pre, post):
    return {"type": "system", "subtype": "compact_boundary",
            "compactMetadata": {"trigger": trigger, "preTokens": pre,
                                "postTokens": post}}


def append_jsonl(path, rows):
    with open(path, "a") as fh:
        for r in rows:
            fh.write((r if isinstance(r, str) else json.dumps(r)) + "\n")


class ScanTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = os.path.join(self.dir.name, "t.jsonl")

    def scan(self, st=None):
        return cm.incremental_scan(st or cm.load_state(
            {"state": "/nonexistent"}), self.path)

    def test_incremental_advance(self):
        append_jsonl(self.path, [usage_row(10, 40_000, 0, 500)])
        st = self.scan()
        self.assertEqual(st["current"], 40_510)
        first_offset = st["offset"]
        append_jsonl(self.path, [usage_row(10, 60_000, 0, 500)])
        st = cm.incremental_scan(st, self.path)
        self.assertEqual(st["current"], 60_510)
        self.assertGreater(st["offset"], first_offset)

    def test_partial_trailing_line_not_consumed(self):
        append_jsonl(self.path, [usage_row(10, 40_000, 0, 500)])
        with open(self.path, "a") as fh:
            fh.write('{"half": "row with no newline')
        st = self.scan()
        self.assertEqual(st["current"], 40_510)
        with open(self.path, "a") as fh:
            fh.write('"}\n')
        st2 = cm.incremental_scan(st, self.path)
        self.assertEqual(st2["current"], 40_510)  # junk row skipped

    def test_truncation_resets_and_reparses(self):
        # A replaced file can reuse the inode, so make the shrink
        # unambiguous: two rows before, one after.
        append_jsonl(self.path, [usage_row(10, 80_000, 0, 500),
                                 usage_row(10, 90_000, 0, 500)])
        st = self.scan()
        self.assertEqual(st["current"], 90_510)
        os.unlink(self.path)
        append_jsonl(self.path, [usage_row(10, 20_000, 0, 500)])
        st = cm.incremental_scan(st, self.path)
        self.assertEqual(st["current"], 20_510)

    def test_boundary_detection_by_trigger(self):
        append_jsonl(self.path, [
            usage_row(10, 150_000, 0, 500),
            boundary_row("auto", 218_707, 28_552),
            usage_row(10, 28_000, 0, 500),
            boundary_row("manual", 64_849, 2_793),
        ])
        st = self.scan()
        self.assertEqual(st["boundaries"], 2)
        self.assertEqual(st["auto_boundaries"], 1)

    def test_peak_survives_compaction(self):
        append_jsonl(self.path, [
            usage_row(10, 150_000, 0, 500),
            boundary_row("manual", 150_510, 5_000),
            usage_row(10, 4_500, 0, 500),
        ])
        st = self.scan()
        self.assertEqual(st["current"], 5_010)
        self.assertEqual(st["peak"], 150_510)

    def test_boundary_resets_current_to_post_tokens(self):
        # Observed live (M2): with no usage row yet after the boundary,
        # a stale pre-compact `current` made the advisor tell a
        # freshly-compacted session it was still ~116% full.
        append_jsonl(self.path, [
            usage_row(10, 150_000, 0, 500),
            boundary_row("manual", 150_510, 28_552),
        ])
        st = self.scan()
        self.assertEqual(st["current"], 28_552)
        self.assertEqual(st["peak"], 150_510)

    def test_boundary_without_post_tokens_resets_to_zero(self):
        append_jsonl(self.path, [
            usage_row(10, 150_000, 0, 500),
            {"type": "system", "subtype": "compact_boundary",
             "compactMetadata": {"trigger": "manual"}},
        ])
        self.assertEqual(self.scan()["current"], 0)

    def test_model_captured(self):
        append_jsonl(self.path, [usage_row(1, 1, 1, 1,
                                           model="claude-opus-9[1m]")])
        self.assertEqual(self.scan()["model"], "claude-opus-9[1m]")

    def test_missing_file_is_noop(self):
        st = cm.load_state({"state": "/nonexistent"})
        self.assertEqual(cm.incremental_scan(st, "/nope.jsonl"), st)

    def test_partial_line_offset_not_advanced(self):
        append_jsonl(self.path, [usage_row(10, 40_000, 0, 500)])
        st = self.scan()
        before = st["offset"]
        with open(self.path, "a") as fh:
            fh.write('{"half": "no newline')
        st = cm.incremental_scan(st, self.path)
        self.assertEqual(st["offset"], before)  # fragment unconsumed

    def test_same_inode_shrink_resets(self):
        append_jsonl(self.path, [usage_row(10, 80_000, 0, 500),
                                 usage_row(10, 90_000, 0, 500)])
        st = self.scan()
        with open(self.path, "w"):
            pass  # truncate IN PLACE: same inode, smaller size
        append_jsonl(self.path, [usage_row(10, 20_000, 0, 500)])
        st = cm.incremental_scan(st, self.path)
        self.assertEqual(st["current"], 20_510)

    def test_giant_line_skipped_within_budget(self):
        # A single line larger than the read budget must be skipped
        # (discard_to_newline), not reread forever. Round-trip the
        # state through save/load each pass: real hooks are separate
        # processes, so the discard flag must survive persistence
        # (audit finding — an in-memory-only loop masks the drop).
        saved = cm.SCAN_MAX_BYTES
        cm.SCAN_MAX_BYTES = 400
        cfg = dict(cm._DEFAULTS, state_dir=self.dir.name)
        paths = cm.state_paths(cfg, "sG")
        try:
            with open(self.path, "w") as fh:
                fh.write("g" * 1000 + "\n")
            append_jsonl(self.path, [usage_row(10, 30_000, 0, 500)])
            st = cm.load_state(paths)
            for _ in range(10):
                st = cm.incremental_scan(st, self.path)
                cm.save_state(cfg, paths, st)
                st = cm.load_state(paths)
                if st.get("current"):
                    break
            self.assertEqual(st["current"], 30_510)
        finally:
            cm.SCAN_MAX_BYTES = saved

    def test_reset_clears_discard_flag(self):
        # A replaced file must not inherit the old file's skip mode.
        append_jsonl(self.path, [usage_row(10, 20_000, 0, 500)])
        st = dict(cm._STATE_DEFAULTS, inode=-1, discard_to_newline=True)
        st = cm.incremental_scan(st, self.path)
        self.assertFalse(st["discard_to_newline"])
        self.assertEqual(st["current"], 20_510)


class AdviseTests(unittest.TestCase):
    EFF = {"soft_pct": 0.70, "hard_pct": 0.80, "context_window": 100_000}

    def advise(self, current, level="none"):
        st = {"current": current, "advisory_level": level}
        return cm.advise(st, self.EFF, "/h.md", rearm_band=0.08)

    def test_below_soft_silent(self):
        self.assertEqual(self.advise(50_000), ("none", None))

    def test_soft_crossing_advises_once(self):
        level, text = self.advise(72_000)
        self.assertEqual(level, "soft")
        self.assertIn("72%", text)
        self.assertIn("/h.md", text)
        # At the same level: no repeat.
        self.assertEqual(self.advise(75_000, level="soft"), ("soft", None))

    def test_hard_crossing_escalates(self):
        level, text = self.advise(85_000, level="soft")
        self.assertEqual(level, "hard")
        self.assertIn("imminent", text)

    def test_no_rearm_inside_band(self):
        # soft at 70%, band 8%: 65% stays armed, 60% re-arms.
        self.assertEqual(self.advise(65_000, level="soft"),
                         ("soft", None))
        level, text = self.advise(60_000, level="soft")
        self.assertEqual(level, "none")
        self.assertIsNone(text)

    def test_straight_to_hard_from_none(self):
        level, text = self.advise(90_000)
        self.assertEqual(level, "hard")
        self.assertIn("imminent", text)

    def test_hard_rearm_steps_to_soft_not_none(self):
        # hard at 85% -> drop to 71%: below the hard re-arm point but
        # still above soft. A straight reset to none would emit a fresh
        # soft advisory on a purely DOWNWARD move (audit finding).
        self.assertEqual(self.advise(71_000, level="hard"),
                         ("soft", None))

    def test_hard_rearm_all_the_way_down(self):
        self.assertEqual(self.advise(60_000, level="hard"),
                         ("none", None))

    def test_recross_after_stepdown_fires_hard(self):
        level, text = self.advise(71_000, level="hard")  # silent stepdown
        self.assertIsNone(text)
        level, text = self.advise(85_000, level=level)
        self.assertEqual(level, "hard")
        self.assertIn("imminent", text)


class PacketTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.cfg = dict(cm._DEFAULTS, state_dir=self.dir.name)
        self.paths = cm.state_paths(self.cfg, "sessX")

    def test_packet_roundtrip_and_exactly_once(self):
        st = {"current": 160_000, "peak": 170_000, "boundaries": 3,
              "packet_seq": 0, "last_drained_packet_seq": -1,
              "armed_at_ts": 0}
        seq = cm.write_packet(self.cfg, self.paths, st, "manual",
                              "keep the plan", "/w")
        st["packet_seq"] = seq
        # Compaction not yet landed (boundaries unchanged): no drain.
        self.assertIsNone(cm.drain_packet(self.cfg, self.paths, st))
        st["boundaries"] = 4
        text = cm.drain_packet(self.cfg, self.paths, st)
        self.assertIn("~160k", text)
        self.assertIn("keep the plan", text)
        # Second drain of the same seq: nothing.
        self.assertIsNone(cm.drain_packet(self.cfg, self.paths, st))

    def test_fresh_handoff_embedded_stale_ignored(self):
        cm._private_makedirs(os.path.dirname(self.paths["handoff"]))
        with open(self.paths["handoff"], "w") as fh:
            fh.write("GOALS: finish the port\n")
        st = {"current": 1, "peak": 1, "boundaries": 0, "packet_seq": 0,
              "armed_at_ts": time.time() - 60}  # handoff newer: fresh
        cm.write_packet(self.cfg, self.paths, st, "auto", "", "/w")
        p = cm.load_packet(self.paths)
        self.assertTrue(p["handoff_fresh"])
        self.assertIn("GOALS", p["handoff_excerpt"])
        st["armed_at_ts"] = time.time() + 60  # handoff older than arming
        cm.write_packet(self.cfg, self.paths, st, "auto", "", "/w")
        p = cm.load_packet(self.paths)
        self.assertFalse(p["handoff_fresh"])
        self.assertIn("re-read any", cm.reorientation_text(p).lower())

    def test_excerpt_capped(self):
        cm._private_makedirs(os.path.dirname(self.paths["handoff"]))
        with open(self.paths["handoff"], "w") as fh:
            fh.write("x" * 100_000)
        st = {"current": 1, "peak": 1, "boundaries": 0, "packet_seq": 0,
              "armed_at_ts": 0}
        cm.write_packet(self.cfg, self.paths, st, "auto", "", "/w")
        p = cm.load_packet(self.paths)
        self.assertLessEqual(len(p["handoff_excerpt"]),
                             self.cfg["handoff_excerpt_bytes"])

    def test_excerpt_cap_is_bytes_not_chars(self):
        # Cap 3999 deliberately splits a 4-byte emoji so the
        # decode(errors=replace) path is actually exercised.
        cfg = dict(self.cfg, handoff_excerpt_bytes=3_999)
        cm._private_makedirs(os.path.dirname(self.paths["handoff"]))
        with open(self.paths["handoff"], "w", encoding="utf-8") as fh:
            fh.write("\U0001F680" * 4000)  # 16k UTF-8 bytes
        st = {"current": 1, "peak": 1, "boundaries": 0, "packet_seq": 0,
              "armed_at_ts": 0}
        cm.write_packet(cfg, self.paths, st, "auto", "", "/w")
        p = cm.load_packet(self.paths)
        self.assertLessEqual(
            len(p["handoff_excerpt"].encode("utf-8")),
            cfg["handoff_excerpt_bytes"] + 4)
        self.assertIn("�", p["handoff_excerpt"][-2:])

    def test_drain_once_survives_save_reload(self):
        st = dict(cm._STATE_DEFAULTS, current=1, peak=1)
        seq = cm.write_packet(self.cfg, self.paths, st, "manual", "", "/w")
        st["packet_seq"] = seq
        st["boundaries"] = 1
        self.assertIsNotNone(cm.drain_packet(self.cfg, self.paths, st))
        cm.save_state(self.cfg, self.paths, st)
        st2 = cm.load_state(self.paths)
        self.assertIsNone(cm.drain_packet(self.cfg, self.paths, st2))

    def test_delivery_cap_tracks_excerpt_knob(self):
        # Raising handoff_excerpt_bytes must not be silently undone by
        # a fixed cap at delivery time (audit finding).
        cfg = dict(self.cfg, handoff_excerpt_bytes=20_000)
        cm._private_makedirs(os.path.dirname(self.paths["handoff"]))
        with open(self.paths["handoff"], "w") as fh:
            fh.write("y" * 20_000)
        st = dict(cm._STATE_DEFAULTS, current=1, peak=1)
        seq = cm.write_packet(cfg, self.paths, st, "manual", "", "/w")
        st.update(packet_seq=seq, boundaries=1)
        text = cm.drain_packet(cfg, self.paths, st)
        self.assertGreater(len(text), 19_000)


class ManagedIntegrationTests(unittest.TestCase):
    """Layer-1 pieces the managed mode relies on."""

    def test_reorientation_strips_nonce_prefix(self):
        p = {"pre_current": 1000, "pre_peak": 1000,
             "custom_instructions": "[cm-a1b2c3d4] keep the plan",
             "handoff_fresh": False, "handoff_excerpt": "",
             "handoff_path": "/h.md"}
        text = cm.reorientation_text(p)
        self.assertIn('"keep the plan"', text)
        self.assertNotIn("cm-a1b2c3d4", text)
        # A plain user instruction is untouched.
        p["custom_instructions"] = "keep the plan"
        self.assertIn('"keep the plan"', cm.reorientation_text(p))

    def test_stop_marker_is_pure_noop_even_managed(self):
        import subprocess
        hooks = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "..", "hooks")
        with tempfile.TemporaryDirectory() as d:
            env = {k: v for k, v in os.environ.items()
                   if not k.startswith("COMPACT_MANAGER_")}
            env.update(COMPACT_MANAGER_MODE="managed",
                       COMPACT_MANAGER_STATE_DIR=d,
                       COMPACT_MANAGER_CONFIG="/nonexistent/x.json")
            p = subprocess.run(
                [sys.executable, os.path.join(hooks, "stop_marker.py")],
                input=json.dumps({"session_id": "s1"}),
                capture_output=True, text=True, env=env, timeout=30)
            self.assertEqual(p.returncode, 0, p.stderr)
            self.assertEqual(p.stdout.strip(), "")
            self.assertEqual(os.listdir(d), [])  # writes nothing


class StateValidationTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.paths = {"state": os.path.join(self.dir.name, "s.json")}

    def test_corrupt_fields_reset_individually(self):
        with open(self.paths["state"], "w") as fh:
            json.dump({"offset": "bad", "advisory_level": "sideways",
                       "current": True, "inode": "x", "peak": 7,
                       "last_drained_packet_seq": 3}, fh)
        st = cm.load_state(self.paths)
        self.assertEqual(st["offset"], 0)
        self.assertEqual(st["advisory_level"], "none")
        self.assertEqual(st["current"], 0)
        self.assertIsNone(st["inode"])
        self.assertEqual(st["peak"], 7)  # valid values survive
        self.assertEqual(st["last_drained_packet_seq"], 3)

    def test_non_dict_state_resets(self):
        with open(self.paths["state"], "w") as fh:
            fh.write('["not", "a", "dict"]')
        self.assertEqual(cm.load_state(self.paths)["offset"], 0)

    def test_numeric_fields_require_exact_ints(self):
        # A float offset would make fh.seek() raise on every scan,
        # leaving the hook permanently inert (audit finding).
        with open(self.paths["state"], "w") as fh:
            json.dump({"offset": 1.5, "current": -3, "peak": float("nan"),
                       "boundaries": 2.0, "packet_seq": 1e308,
                       "last_drained_packet_seq": -1,
                       "armed_at_ts": 12.5}, fh)
        st = cm.load_state(self.paths)
        self.assertEqual(st["offset"], 0)
        self.assertEqual(st["current"], 0)
        self.assertEqual(st["peak"], 0)
        self.assertEqual(st["boundaries"], 0)
        self.assertEqual(st["packet_seq"], 0)
        self.assertEqual(st["last_drained_packet_seq"], -1)  # -1 legal
        self.assertEqual(st["armed_at_ts"], 12.5)  # finite float legal

    def test_huge_int_timestamp_does_not_wedge(self):
        # math.isfinite(10**309) raises OverflowError; that must not
        # escape load_state and leave the plugin permanently inert.
        with open(self.paths["state"], "w") as fh:
            fh.write('{"armed_at_ts": %d, "offset": 5}' % 10**309)
        st = cm.load_state(self.paths)
        self.assertEqual(st["armed_at_ts"], 10**309)  # huge int accepted
        self.assertEqual(st["offset"], 5)
        with open(self.paths["state"], "w") as fh:
            fh.write('{"armed_at_ts": 1e309}')  # inf float -> default
        self.assertEqual(cm.load_state(self.paths)["armed_at_ts"], 0)

    def test_discard_flag_survives_save_reload(self):
        cfg = dict(cm._DEFAULTS, state_dir=self.dir.name)
        paths = cm.state_paths(cfg, "sH")
        st = dict(cm._STATE_DEFAULTS, discard_to_newline=True)
        cm.save_state(cfg, paths, st)
        self.assertTrue(cm.load_state(paths)["discard_to_newline"])


class SessionOverrideTests(unittest.TestCase):
    """Per-session override file: validated per key, fail open."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.cfg = dict(cm._DEFAULTS, state_dir=self.dir.name)

    def write(self, sid, value):
        d = os.path.join(self.dir.name, "overrides")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, cm.path_component(sid) + ".json"),
                  "w") as fh:
            fh.write(value if isinstance(value, str) else json.dumps(value))

    def test_missing_file_is_empty(self):
        self.assertEqual(cm.session_overrides(self.cfg, "sess-none"), {})

    def test_each_key_validated_independently(self):
        # hard_pct out of range and an unknown key drop; the rest stay.
        self.write("sess-mix", {"soft_pct": 0.5, "hard_pct": 2.0,
                                "managed_trigger_pct": 0.6,
                                "context_window": 500_000,
                                "mystery_knob": 1})
        self.assertEqual(cm.session_overrides(self.cfg, "sess-mix"),
                         {"soft_pct": 0.5, "managed_trigger_pct": 0.6,
                          "context_window": 500_000})

    def test_garbage_values_and_shapes_read_as_empty(self):
        for junk in ("{not json", '"scalar"', "[1, 2]",
                     json.dumps({"soft_pct": True, "hard_pct": 0,
                                 "managed_trigger_pct": -0.5,
                                 "context_window": 9_999}),
                     json.dumps({"soft_pct": "0.5",
                                 "context_window": 500_000.0}),
                     # A huge window survives int math but overflows
                     # float conversion downstream — reader rejects it.
                     json.dumps({"context_window": 10**12}),
                     '{"soft_pct": NaN, "hard_pct": Infinity}'):
            self.write("sess-junk", junk)
            self.assertEqual(cm.session_overrides(self.cfg, "sess-junk"),
                             {}, junk)

    def test_window_bounds_are_inclusive(self):
        # Exact endpoints accepted; one past either end rejected. The
        # ceiling keeps a huge int from overflowing float math.
        for w, want in ((10_000, {"context_window": 10_000}),
                        (1_000_000_000, {"context_window": 1_000_000_000}),
                        (9_999, {}), (1_000_000_001, {}),
                        (10 ** 4000, {})):
            self.write("sess-bnd", {"context_window": w})
            self.assertEqual(cm.session_overrides(self.cfg, "sess-bnd"),
                             want, w)

    def test_apply_overrides_reclamps_soft_to_hard(self):
        # soft raised above hard clamps down, exactly as the loaders do.
        out = cm.apply_overrides(
            {"soft_pct": 0.7, "hard_pct": 0.8, "context_window": 200_000},
            {"soft_pct": 0.9})
        self.assertEqual((out["soft_pct"], out["hard_pct"]), (0.8, 0.8))
        out = cm.apply_overrides(
            {"soft_pct": 0.7, "hard_pct": 0.8, "context_window": 200_000},
            {"hard_pct": 0.6, "context_window": 500_000})
        self.assertEqual((out["soft_pct"], out["hard_pct"],
                          out["context_window"]), (0.6, 0.6, 500_000))

    def test_prune_reaps_aged_override_files(self):
        d = os.path.join(self.dir.name, "overrides")
        os.makedirs(d)
        dead = os.path.join(d, "dead.json")
        with open(dead, "w") as fh:
            fh.write("{}")
        old = time.time() - 30 * 86_400
        os.utime(dead, (old, old))
        cm._write_sentinel(self.cfg)
        cm.prune_state(dict(self.cfg, state_ttl_days=7))
        self.assertFalse(os.path.exists(dead))

    def test_prune_spares_override_of_live_session(self):
        # An override is written once and read forever; its own mtime
        # goes stale while the session lives. A fresh sibling state
        # file (the advisor touches it every tool call) must protect
        # it from the reaper (audit finding).
        d = os.path.join(self.dir.name, "overrides")
        sd = os.path.join(self.dir.name, "state")
        os.makedirs(d)
        os.makedirs(sd)
        ovr = os.path.join(d, "livesess.json")
        with open(ovr, "w") as fh:
            fh.write("{}")
        old = time.time() - 30 * 86_400
        os.utime(ovr, (old, old))
        with open(os.path.join(sd, "livesess.json"), "w") as fh:
            fh.write("{}")  # fresh mtime
        cm._write_sentinel(self.cfg)
        cm.prune_state(dict(self.cfg, state_ttl_days=7))
        self.assertTrue(os.path.exists(ovr))


class PruneTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.cfg = dict(cm._DEFAULTS, state_dir=self.dir.name,
                        state_ttl_days=7)

    def _old(self, path):
        t = time.time() - 30 * 86_400
        os.utime(path, (t, t))

    def test_prunes_only_marked_dirs_and_own_patterns(self):
        base = self.dir.name
        for sub in ("state", "packets", "handoff", "locks"):
            os.makedirs(os.path.join(base, sub))
        own = os.path.join(base, "state", "dead.json")
        foreign = os.path.join(base, "state", "notes.txt")
        for p in (own, foreign):
            with open(p, "w") as fh:
                fh.write("x")
            self._old(p)
        # No sentinel yet: nothing may be deleted.
        cm.prune_state(self.cfg)
        self.assertTrue(os.path.exists(own))
        cm._write_sentinel(self.cfg)
        cm.prune_state(self.cfg)
        self.assertFalse(os.path.exists(own))      # aged, matching name
        self.assertTrue(os.path.exists(foreign))   # pattern-protected

    def _symlink(self, src, dst):
        try:
            os.symlink(src, dst)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks unavailable on this platform")

    def test_prune_skips_symlinked_subdir(self):
        base = self.dir.name
        victim_dir = os.path.join(base, "victims")
        os.makedirs(victim_dir)
        victim = os.path.join(victim_dir, "precious.json")
        with open(victim, "w") as fh:
            fh.write("x")
        self._old(victim)
        self._symlink(victim_dir, os.path.join(base, "state"))
        cm._write_sentinel(self.cfg)
        cm.prune_state(self.cfg)
        self.assertTrue(os.path.exists(victim))

    def test_prune_skips_symlinked_file(self):
        base = self.dir.name
        os.makedirs(os.path.join(base, "state"))
        target = os.path.join(base, "target.json")
        with open(target, "w") as fh:
            fh.write("x")
        self._old(target)
        self._symlink(target, os.path.join(base, "state", "link.json"))
        cm._write_sentinel(self.cfg)
        cm.prune_state(self.cfg)
        self.assertTrue(os.path.exists(target))

    def test_prune_skips_names_the_plugin_cannot_generate(self):
        base = self.dir.name
        os.makedirs(os.path.join(base, "state"))
        odd = os.path.join(base, "state", "annual report.json")
        with open(odd, "w") as fh:
            fh.write("x")
        self._old(odd)
        cm._write_sentinel(self.cfg)
        cm.prune_state(self.cfg)
        self.assertTrue(os.path.exists(odd))

    def test_fresh_files_survive(self):
        base = self.dir.name
        os.makedirs(os.path.join(base, "state"))
        live = os.path.join(base, "state", "live.json")
        with open(live, "w") as fh:
            fh.write("x")
        cm._write_sentinel(self.cfg)
        cm.prune_state(self.cfg)
        self.assertTrue(os.path.exists(live))


class LockTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.lock = os.path.join(self.dir.name, "x.lock")

    def test_stale_lock_broken_and_released(self):
        with open(self.lock, "w") as fh:
            fh.write("dead-owner")
        old = time.time() - 60
        os.utime(self.lock, (old, old))
        release = cm._locked_open(self.lock, timeout=1.0)
        self.assertTrue(os.path.exists(self.lock))
        release()
        self.assertFalse(os.path.exists(self.lock))

    def test_release_leaves_successor_lock(self):
        release = cm._locked_open(self.lock, timeout=1.0)
        with open(self.lock, "w") as fh:
            fh.write("someone-else")  # peer stale-broke and re-took it
        release()
        self.assertTrue(os.path.exists(self.lock))

    def test_live_lock_contention_times_out(self):
        cm._locked_open(self.lock, timeout=1.0)  # held, fresh mtime
        with self.assertRaises(cm.LockTimeout):
            cm._locked_open(self.lock, timeout=0.2)

    def test_stale_break_restores_displaced_live_lock(self):
        # Interleave: the lock looks stale at the first check, but by
        # rename time a peer has re-acquired (the displaced copy is
        # fresh). The recheck must restore the peer's lock — same
        # token, file still present — and this racer must time out.
        from unittest import mock
        with open(self.lock, "w") as fh:
            fh.write("peer-token")

        def fake_getmtime(p):
            return (time.time() - 60) if p == self.lock else time.time()

        with mock.patch("os.path.getmtime", side_effect=fake_getmtime):
            with self.assertRaises(cm.LockTimeout):
                cm._locked_open(self.lock, timeout=0.3)
        with open(self.lock) as fh:
            self.assertEqual(fh.read(), "peer-token")


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self._saved = {k: os.environ.pop(k) for k in list(os.environ)
                       if k.startswith("COMPACT_MANAGER_")}
        os.environ["COMPACT_MANAGER_CONFIG"] = "/nonexistent/cm-test.json"

    def tearDown(self):
        for k in list(os.environ):
            if k.startswith("COMPACT_MANAGER_"):
                del os.environ[k]
        os.environ.update(self._saved)

    def test_default_mode_off(self):
        self.assertEqual(cm.load_config()["mode"], "off")

    def test_mode_choices_validated(self):
        os.environ["COMPACT_MANAGER_MODE"] = "Advisory"
        self.assertEqual(cm.load_config()["mode"], "advisory")
        os.environ["COMPACT_MANAGER_MODE"] = "sideways"
        self.assertEqual(cm.load_config()["mode"], "off")

    def test_float_pcts(self):
        os.environ["COMPACT_MANAGER_SOFT_PCT"] = "0.5"
        self.assertEqual(cm.load_config()["soft_pct"], 0.5)
        os.environ["COMPACT_MANAGER_SOFT_PCT"] = "inf"
        self.assertEqual(cm.load_config()["soft_pct"],
                         cm._DEFAULTS["soft_pct"])

    def test_soft_clamped_to_hard(self):
        os.environ["COMPACT_MANAGER_SOFT_PCT"] = "0.9"
        os.environ["COMPACT_MANAGER_HARD_PCT"] = "0.8"
        c = cm.load_config()
        self.assertEqual(c["soft_pct"], 0.8)

    def test_per_model_window(self):
        os.environ["COMPACT_MANAGER_MODELS"] = json.dumps(
            {"[1m]": {"context_window": 1_000_000}})
        cfg = cm.load_config()
        self.assertEqual(
            cm.window_for(cfg, "claude-opus-9[1m]")["context_window"],
            1_000_000)
        self.assertEqual(
            cm.window_for(cfg, "claude-haiku-4-5")["context_window"],
            1_000_000)  # no override -> the 1M global default

    def test_default_window_is_one_million(self):
        # Deliberate: unknown new models must not warn/compact early at
        # a stale small default; legacy models are the override case.
        self.assertEqual(cm._DEFAULTS["context_window"], 1_000_000)
        self.assertEqual(cm.load_config()["context_window"], 1_000_000)

    def test_bool_garbage_keeps_default(self):
        os.environ["COMPACT_MANAGER_SYSTEM_MESSAGE"] = "garbage"
        self.assertTrue(cm.load_config()["system_message"])
        os.environ["COMPACT_MANAGER_SYSTEM_MESSAGE"] = "off"
        self.assertFalse(cm.load_config()["system_message"])

    def test_zero_pcts_restore_defaults(self):
        os.environ["COMPACT_MANAGER_SOFT_PCT"] = "0"
        os.environ["COMPACT_MANAGER_HARD_PCT"] = "0"
        c = cm.load_config()
        self.assertEqual(c["soft_pct"], cm._DEFAULTS["soft_pct"])
        self.assertEqual(c["hard_pct"], cm._DEFAULTS["hard_pct"])

    def test_per_model_insane_overrides_degrade(self):
        os.environ["COMPACT_MANAGER_MODELS"] = json.dumps(
            {"opus": {"context_window": 0, "hard_pct": 9,
                      "soft_pct": 0}})
        eff = cm.window_for(cm.load_config(), "claude-opus-9")
        self.assertEqual(eff["context_window"], 10_000)
        self.assertEqual(eff["hard_pct"], 1.0)
        self.assertEqual(eff["soft_pct"], cm._DEFAULTS["soft_pct"])


class HookShellTests(unittest.TestCase):
    """Every entry point exits 0 and stays silent in mode=off / on
    garbage; the advisory pipeline works end-to-end via subprocess."""

    SCRIPTS = ("advisor.py", "reorient.py", "precompact.py",
               "session_start.py", "stop_marker.py")

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)

    def run_hook(self, script, payload, mode="advisory", extra_env=None):
        env = {k: v for k, v in os.environ.items()
               if not k.startswith("COMPACT_MANAGER_")}
        env.update(COMPACT_MANAGER_CONFIG="/nonexistent/cm-test.json",
                   COMPACT_MANAGER_MODE=mode,
                   COMPACT_MANAGER_STATE_DIR=self.dir.name,
                   # The numeric fixtures below are written against a
                   # 200k window; the shipped default is 1M.
                   COMPACT_MANAGER_CONTEXT_WINDOW="200000")
        env.update(extra_env or {})
        p = subprocess.run(
            [sys.executable, os.path.join(HOOKS, script)],
            input=(payload if isinstance(payload, str)
                   else json.dumps(payload)),
            capture_output=True, text=True, env=env, timeout=30)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.last_stderr = p.stderr
        return json.loads(p.stdout) if p.stdout.strip() else {}

    def test_all_hooks_silent_when_off(self):
        for s in self.SCRIPTS:
            self.assertEqual(self.run_hook(s, {"session_id": "x"},
                                           mode="off"), {}, s)

    def test_all_hooks_fail_open_on_garbage(self):
        for s in self.SCRIPTS:
            self.assertEqual(self.run_hook(s, "{not json"), {}, s)

    def test_session_start_watcher_status_managed(self):
        base = {"hook_event_name": "SessionStart", "session_id": "sW",
                "source": "startup"}
        # advisory mode: watcher status is a managed-mode concern only.
        self.assertEqual(self.run_hook("session_start.py", base), {})
        # managed, no lease: the attach hint fires.
        out = self.run_hook("session_start.py", base, mode="managed")
        ctx = out["hookSpecificOutput"]["additionalContext"]
        self.assertIn("NO watcher", ctx)
        self.assertIn("adopt", ctx)
        # managed, live lease (fresh heartbeat is sufficient): attached.
        lease_dir = os.path.join(self.dir.name, "managed", "leases")
        os.makedirs(lease_dir, exist_ok=True)
        with open(os.path.join(lease_dir, "session-sW.json"), "w") as fh:
            json.dump({"run_token": "t", "pid": 12345, "proc_start": 1,
                       "heartbeat_at": time.time()}, fh)
        out = self.run_hook("session_start.py", base, mode="managed")
        ctx = out["hookSpecificOutput"]["additionalContext"]
        self.assertIn("attached", ctx)
        self.assertIn("12345", ctx)
        # Ambiguity is NOT attachment: lease_is_live's malformed-counts-
        # as-live reclaim default must not leak into the status line.
        lease_file = os.path.join(lease_dir, "session-sW.json")
        with open(lease_file, "w") as fh:
            json.dump({}, fh)
        out = self.run_hook("session_start.py", base, mode="managed")
        self.assertIn("NO watcher",
                      out["hookSpecificOutput"]["additionalContext"])
        # Stale heartbeat + dead pid: not attached either.
        with open(lease_file, "w") as fh:
            json.dump({"run_token": "t", "pid": 999999999,
                       "proc_start": 1, "heartbeat_at": 1.0}, fh)
        out = self.run_hook("session_start.py", base, mode="managed")
        self.assertIn("NO watcher",
                      out["hookSpecificOutput"]["additionalContext"])
        # Fresh heartbeat does NOT rescue a malformed lease: pid -1, or
        # a missing run_token/proc_start (round-2 audit pin).
        for bad in ({"pid": -1, "heartbeat_at": time.time()},
                    {"pid": 12345, "heartbeat_at": time.time()},
                    {"pid": 12345, "run_token": "t",
                     "heartbeat_at": time.time()},
                    # future heartbeat is malformed, not clock skew:
                    # it must not count as fresh evidence for a dead pid
                    {"pid": 999999999, "run_token": "t", "proc_start": 1,
                     "heartbeat_at": time.time() + 30}):
            with open(lease_file, "w") as fh:
                json.dump(bad, fh)
            out = self.run_hook("session_start.py", base, mode="managed")
            self.assertIn("NO watcher",
                          out["hookSpecificOutput"]["additionalContext"],
                          bad)
        # Non-string session ids and non-dict payloads stay silent.
        self.assertEqual(self.run_hook(
            "session_start.py", dict(base, session_id={"g": True}),
            mode="managed"), {})
        self.assertEqual(self.last_stderr, "")
        self.assertEqual(self.run_hook("session_start.py", "[1, 2]",
                                       mode="managed"), {})
        self.assertEqual(self.last_stderr, "")
        # resume and clear behave like startup (a /clear rotates the
        # session id and retires any watcher, so the fresh id must hear
        # its coverage status); unknown sources stay silent.
        with open(lease_file, "w") as fh:
            json.dump({"run_token": "t", "pid": 12345, "proc_start": 1,
                       "heartbeat_at": time.time()}, fh)
        out = self.run_hook("session_start.py",
                            dict(base, source="resume"), mode="managed")
        self.assertIn("attached",
                      out["hookSpecificOutput"]["additionalContext"])
        out = self.run_hook("session_start.py",
                            dict(base, source="clear"), mode="managed")
        self.assertIn("attached",
                      out["hookSpecificOutput"]["additionalContext"])
        self.assertEqual(self.run_hook(
            "session_start.py", dict(base, source="mystery"),
            mode="managed"), {})

    def test_advisory_end_to_end(self):
        t = os.path.join(self.dir.name, "sess.jsonl")
        append_jsonl(t, [usage_row(10, 150_000, 0, 500)])
        payload = {"hook_event_name": "PostToolUse", "session_id": "sA",
                   "transcript_path": t, "tool_name": "Bash"}
        out = self.run_hook("advisor.py", payload)
        ctx = out["hookSpecificOutput"]["additionalContext"]
        self.assertIn("75%", ctx)  # 150,510 / 200,000
        # The advisor stamps its effective thresholds into the state
        # file so readouts can honor per-session/per-model overrides.
        with open(os.path.join(self.dir.name, "state", "sA.json")) as fh:
            st = json.load(fh)
        self.assertEqual(st["eff_window"], 200_000)
        self.assertEqual((st["eff_soft_pct"], st["eff_hard_pct"]),
                         (0.7, 0.8))
        self.assertEqual(st["eff_trigger_pct"], 0.8)
        # Same state again: hysteresis, no repeat.
        self.assertEqual(self.run_hook("advisor.py", payload), {})

    def test_advisor_warns_unwatched_above_trigger_once(self):
        # Managed session over trigger with no attached watcher: the
        # advisor injects the mid-flight attach warning (the
        # SessionStart notice never re-fires, so this is the only
        # in-session signal) — once per crossing, re-armed when a
        # compaction drops usage below trigger.
        t = os.path.join(self.dir.name, "sess.jsonl")
        append_jsonl(t, [usage_row(10, 170_000, 0, 500)])
        payload = {"hook_event_name": "PostToolUse", "session_id": "sU",
                   "transcript_path": t, "tool_name": "Bash"}
        out = self.run_hook("advisor.py", payload, mode="managed")
        ctx = out["hookSpecificOutput"]["additionalContext"]
        self.assertIn("NO live watcher", ctx)
        self.assertIn("adopt", ctx)
        # Same crossing: silent (advisory hysteresis + warned flag).
        self.assertEqual(self.run_hook("advisor.py", payload,
                                       mode="managed"), {})
        # Compaction drops below trigger: flag re-arms (the packet
        # delivery may inject, but not the unwatched warning).
        append_jsonl(t, [boundary_row("auto", 170_510, 20_000),
                         usage_row(10, 20_000, 0, 500)])
        out = self.run_hook("advisor.py", payload, mode="managed")
        ctx = (out.get("hookSpecificOutput") or {}).get(
            "additionalContext", "")
        self.assertNotIn("NO live watcher", ctx)
        # Climb back over trigger: warns again.
        append_jsonl(t, [usage_row(10, 171_000, 0, 500)])
        out = self.run_hook("advisor.py", payload, mode="managed")
        ctx = out["hookSpecificOutput"]["additionalContext"]
        self.assertIn("NO live watcher", ctx)

    def test_advisor_unwatched_honors_override_trigger(self):
        # Nondefault trigger + window via the override file: 100k of a
        # 150k window is 66.7% — over the 60% override trigger, under
        # the 80% hard default. Only the correct trigger/window source
        # warns here (Sol audit: the defaults would mask a wrong
        # source), and no threshold advisory accompanies it.
        d = os.path.join(self.dir.name, "overrides")
        os.makedirs(d)
        with open(os.path.join(d, "sT.json"), "w") as fh:
            json.dump({"managed_trigger_pct": 0.6,
                       "context_window": 150_000}, fh)
        t = os.path.join(self.dir.name, "sess.jsonl")
        append_jsonl(t, [usage_row(10, 100_000, 0, 500)])
        payload = {"hook_event_name": "PostToolUse", "session_id": "sT",
                   "transcript_path": t, "tool_name": "Bash"}
        out = self.run_hook("advisor.py", payload, mode="managed")
        ctx = out["hookSpecificOutput"]["additionalContext"]
        self.assertIn("NO live watcher", ctx)
        self.assertIn("67%", ctx)
        self.assertIn("60%", ctx)
        # The injected warning must steer to the user, never instruct
        # the model to attach/adopt itself (Sol audit blocker).
        self.assertIn("Do not attach or adopt one yourself", ctx)
        # Exactly AT the trigger warns too (>=, not >): 90,000 of
        # 150,000 is precisely 60%.
        t2 = os.path.join(self.dir.name, "sess2.jsonl")
        append_jsonl(t2, [usage_row(0, 90_000, 0, 0)])
        with open(os.path.join(d, "sQ.json"), "w") as fh:
            json.dump({"managed_trigger_pct": 0.6,
                       "context_window": 150_000}, fh)
        out = self.run_hook("advisor.py",
                            dict(payload, session_id="sQ",
                                 transcript_path=t2), mode="managed")
        ctx = out["hookSpecificOutput"]["additionalContext"]
        self.assertIn("NO live watcher", ctx)

    def test_advisor_unwatched_survives_off_round_trip(self):
        # managed (warn) → off (hook inert) → managed again: the same
        # crossing stays suppressed (already delivered), and a NEW
        # crossing — which requires a compaction boundary — warns
        # again via the boundary reset, even though off mode never
        # touched the flag (Sol audit round-3).
        t = os.path.join(self.dir.name, "sess.jsonl")
        append_jsonl(t, [usage_row(10, 170_000, 0, 500)])
        payload = {"hook_event_name": "PostToolUse", "session_id": "sR",
                   "transcript_path": t, "tool_name": "Bash"}
        out = self.run_hook("advisor.py", payload, mode="managed")
        self.assertIn("NO live watcher",
                      out["hookSpecificOutput"]["additionalContext"])
        self.assertEqual(self.run_hook("advisor.py", payload,
                                       mode="off"), {})
        # Back in managed, same crossing: still suppressed.
        self.assertEqual(self.run_hook("advisor.py", payload,
                                       mode="managed"), {})
        # Compaction + re-climb (spanning one scan): warns again.
        append_jsonl(t, [boundary_row("auto", 170_510, 20_000),
                         usage_row(10, 171_000, 0, 500)])
        out = self.run_hook("advisor.py", payload, mode="managed")
        self.assertIn("NO live watcher",
                      out["hookSpecificOutput"]["additionalContext"])

    def test_advisor_unwatched_silent_when_attached_or_advisory(self):
        lease_dir = os.path.join(self.dir.name, "managed", "leases")
        os.makedirs(lease_dir, exist_ok=True)
        with open(os.path.join(lease_dir, "session-sV.json"), "w") as fh:
            json.dump({"run_token": "t" * 16, "pid": os.getpid(),
                       "proc_start": 1, "heartbeat_at": time.time()}, fh)
        t = os.path.join(self.dir.name, "sess.jsonl")
        append_jsonl(t, [usage_row(10, 170_000, 0, 500)])
        payload = {"hook_event_name": "PostToolUse", "session_id": "sV",
                   "transcript_path": t, "tool_name": "Bash"}
        out = self.run_hook("advisor.py", payload, mode="managed")
        ctx = (out.get("hookSpecificOutput") or {}).get(
            "additionalContext", "")
        self.assertNotIn("NO live watcher", ctx)  # attached watcher
        # Advisory mode: watchers are not expected; never warn.
        out = self.run_hook("advisor.py",
                            dict(payload, session_id="sW"))
        ctx = (out.get("hookSpecificOutput") or {}).get(
            "additionalContext", "")
        self.assertNotIn("NO live watcher", ctx)
        # Ambiguous lease read (symlink → read_json_inode None): proves
        # nothing — no warning, and the warned flag must not stick.
        os.symlink("/nonexistent",
                   os.path.join(lease_dir, "session-sX.json"))
        out = self.run_hook("advisor.py",
                            dict(payload, session_id="sX"),
                            mode="managed")
        ctx = (out.get("hookSpecificOutput") or {}).get(
            "additionalContext", "")
        self.assertNotIn("NO live watcher", ctx)
        with open(os.path.join(self.dir.name, "state", "sX.json")) as fh:
            self.assertFalse(json.load(fh).get("unwatched_warned"))

    def test_advisor_honors_session_override_file(self):
        # 80,510 tokens: silent against the 200k/70% defaults, but the
        # override file (150k window, 50% soft) makes it a soft
        # advisory. The stamps stay BASE (pre-override): readouts
        # overlay the override file themselves, and a merged stamp
        # would keep showing an override after --clear removed it.
        d = os.path.join(self.dir.name, "overrides")
        os.makedirs(d)
        with open(os.path.join(d, "sO.json"), "w") as fh:
            json.dump({"soft_pct": 0.5, "hard_pct": 0.55,
                       "managed_trigger_pct": 0.6,
                       "context_window": 150_000}, fh)
        t = os.path.join(self.dir.name, "sess.jsonl")
        append_jsonl(t, [usage_row(10, 80_000, 0, 500)])
        payload = {"hook_event_name": "PostToolUse", "session_id": "sO",
                   "transcript_path": t, "tool_name": "Bash"}
        out = self.run_hook("advisor.py", payload)
        self.assertIn("additionalContext", out.get("hookSpecificOutput", {}))
        with open(os.path.join(self.dir.name, "state", "sO.json")) as fh:
            st = json.load(fh)
        self.assertEqual(st["eff_window"], 200_000)
        self.assertEqual((st["eff_soft_pct"], st["eff_hard_pct"],
                          st["eff_trigger_pct"]), (0.7, 0.8, 0.8))
        # Without the override file the same reading is silent (proves
        # the advisory above came from the merged thresholds).
        os.unlink(os.path.join(d, "sO.json"))
        append_jsonl(t, [usage_row(10, 80_100, 0, 500)])
        self.assertEqual(self.run_hook(
            "advisor.py", dict(payload, session_id="sP")), {})

    def test_session_start_advertises_override_command(self):
        base = {"hook_event_name": "SessionStart",
                "session_id": "sess-adv-1234", "source": "startup"}
        out = self.run_hook("session_start.py", base, mode="managed")
        ctx = out["hookSpecificOutput"]["additionalContext"]
        # The CLI path is quoted (paths with spaces stay pasteable).
        self.assertIn('" override sess-adv-1234 trigger=NN%', ctx)
        self.assertIn('run: "', ctx)
        self.assertIn("user asks", ctx)
        # A sid the CLI's validator would reject is never baked into a
        # copy-pasteable command line.
        out = self.run_hook("session_start.py",
                            dict(base, session_id="s W"), mode="managed")
        ctx = out["hookSpecificOutput"]["additionalContext"]
        self.assertIn("override <session-id>", ctx)
        self.assertNotIn("override s W", ctx)

    def test_full_compaction_cycle(self):
        t = os.path.join(self.dir.name, "sess.jsonl")
        append_jsonl(t, [usage_row(10, 165_000, 0, 500)])
        base = {"session_id": "sB", "transcript_path": t}
        out = self.run_hook("advisor.py",
                            dict(base, hook_event_name="PostToolUse"))
        self.assertIn("imminent",
                      out["hookSpecificOutput"]["additionalContext"])
        # PreCompact persists the packet…
        self.run_hook("precompact.py",
                      dict(base, hook_event_name="PreCompact",
                           trigger="auto", custom_instructions=""))
        # …the compaction lands in the transcript…
        append_jsonl(t, [boundary_row("auto", 165_510, 20_000),
                         usage_row(10, 19_000, 0, 500)])
        # SessionStart(compact) injects opportunistically w/o consuming…
        out = self.run_hook("session_start.py",
                            dict(base, hook_event_name="SessionStart",
                                 source="compact"))
        self.assertIn("Reorientation",
                      out["hookSpecificOutput"]["additionalContext"])
        # …and the next tool call delivers the durable packet once.
        out = self.run_hook("advisor.py",
                            dict(base, hook_event_name="PostToolUse"))
        self.assertIn("Reorientation",
                      out["hookSpecificOutput"]["additionalContext"])
        self.assertEqual(self.run_hook(
            "advisor.py", dict(base, hook_event_name="PostToolUse")), {})

    def test_reorient_measures_too(self):
        t = os.path.join(self.dir.name, "sess.jsonl")
        append_jsonl(t, [usage_row(10, 150_000, 0, 500)])
        out = self.run_hook("reorient.py",
                            {"hook_event_name": "UserPromptSubmit",
                             "session_id": "sC", "transcript_path": t})
        self.assertIn("75%", out["hookSpecificOutput"]["additionalContext"])

    def test_boundary_without_usage_row_stays_silent(self):
        # The M2-live regression at hook level: right after a boundary,
        # with no post-compaction usage row yet, the advisor must NOT
        # warn that context is still full.
        t = os.path.join(self.dir.name, "sess.jsonl")
        append_jsonl(t, [usage_row(10, 165_000, 0, 500)])
        base = {"session_id": "sF", "transcript_path": t}
        self.run_hook("advisor.py",
                      dict(base, hook_event_name="PostToolUse"))
        append_jsonl(t, [boundary_row("manual", 165_510, 20_000)])
        out = self.run_hook("advisor.py",
                            dict(base, hook_event_name="PostToolUse"))
        ctx = out.get("hookSpecificOutput", {}).get("additionalContext", "")
        self.assertNotIn("full", ctx)

    def test_corrupt_state_self_heals(self):
        t = os.path.join(self.dir.name, "sess.jsonl")
        append_jsonl(t, [usage_row(10, 150_000, 0, 500)])
        sd = os.path.join(self.dir.name, "state")
        os.makedirs(sd, exist_ok=True)
        with open(os.path.join(sd, "sD.json"), "w") as fh:
            json.dump({"offset": "bad", "current": None,
                       "advisory_level": 42}, fh)
        out = self.run_hook("advisor.py",
                            {"hook_event_name": "PostToolUse",
                             "session_id": "sD", "transcript_path": t})
        self.assertIn("75%",
                      out["hookSpecificOutput"]["additionalContext"])

    def test_oversized_stdin_fails_open(self):
        big = ('{"session_id": "x", "pad": "'
               + "a" * (cm.STDIN_MAX_BYTES + 100) + '"}')
        self.assertEqual(self.run_hook("advisor.py", big), {})

    def test_unwritable_state_dir_fails_open(self):
        t = os.path.join(self.dir.name, "sess.jsonl")
        append_jsonl(t, [usage_row(10, 150_000, 0, 500)])
        ro = os.path.join(self.dir.name, "ro")
        os.makedirs(ro, mode=0o500)
        self.addCleanup(os.chmod, ro, 0o700)
        self.run_hook("advisor.py",  # asserts exit 0 internally
                      {"hook_event_name": "PostToolUse",
                       "session_id": "sE", "transcript_path": t},
                      extra_env={"COMPACT_MANAGER_STATE_DIR": ro})


if __name__ == "__main__":
    unittest.main()
