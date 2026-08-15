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
            200_000)


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
                   COMPACT_MANAGER_STATE_DIR=self.dir.name)
        env.update(extra_env or {})
        p = subprocess.run(
            [sys.executable, os.path.join(HOOKS, script)],
            input=(payload if isinstance(payload, str)
                   else json.dumps(payload)),
            capture_output=True, text=True, env=env, timeout=30)
        self.assertEqual(p.returncode, 0, p.stderr)
        return json.loads(p.stdout) if p.stdout.strip() else {}

    def test_all_hooks_silent_when_off(self):
        for s in self.SCRIPTS:
            self.assertEqual(self.run_hook(s, {"session_id": "x"},
                                           mode="off"), {}, s)

    def test_all_hooks_fail_open_on_garbage(self):
        for s in self.SCRIPTS:
            self.assertEqual(self.run_hook(s, "{not json"), {}, s)

    def test_advisory_end_to_end(self):
        t = os.path.join(self.dir.name, "sess.jsonl")
        append_jsonl(t, [usage_row(10, 150_000, 0, 500)])
        payload = {"hook_event_name": "PostToolUse", "session_id": "sA",
                   "transcript_path": t, "tool_name": "Bash"}
        out = self.run_hook("advisor.py", payload)
        ctx = out["hookSpecificOutput"]["additionalContext"]
        self.assertIn("75%", ctx)  # 150,510 / 200,000
        # Same state again: hysteresis, no repeat.
        self.assertEqual(self.run_hook("advisor.py", payload), {})

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


if __name__ == "__main__":
    unittest.main()
