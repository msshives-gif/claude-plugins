"""Unit tests for the measurement core and state IO.

Fixtures are built in-code so the exact token numbers being asserted are
visible next to the assertion. Run: python3 -m unittest discover tests
"""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "hooks"))
import sgauge_common as sg


def usage_row(inp, cr, cc, out, stop_reason):
    return {"type": "assistant",
            "message": {"role": "assistant", "stop_reason": stop_reason,
                        "usage": {"input_tokens": inp,
                                  "cache_read_input_tokens": cr,
                                  "cache_creation_input_tokens": cc,
                                  "output_tokens": out}}}


def write_jsonl(path, rows):
    with open(path, "w") as fh:
        for r in rows:
            fh.write((r if isinstance(r, str) else json.dumps(r)) + "\n")


class ScanTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, "agent-atest-0123456789abcdef.jsonl")

    def tearDown(self):
        self.dir.cleanup()

    def test_terminal_row_preferred_and_output_included(self):
        # Streaming writes a preliminary row (stop_reason null) before the
        # terminal row of the same request; and the final response's
        # output tokens are part of stop-time context.
        write_jsonl(self.path, [
            usage_row(100_000, 50_000, 0, 1, None),
            usage_row(100_000, 50_000, 0, 4_000, "end_turn"),
        ])
        res = sg._scan(self.path)
        self.assertTrue(res["terminal"])
        self.assertEqual(res["current"], 154_000)
        self.assertEqual(res["prompt"], 150_000)

    def test_last_terminal_row_wins_over_stale_earlier_request(self):
        write_jsonl(self.path, [
            usage_row(50_000, 0, 0, 500, "tool_use"),
            usage_row(0, 190_000, 10_000, 2_000, "end_turn"),
        ])
        res = sg._scan(self.path)
        self.assertEqual(res["current"], 202_000)
        self.assertEqual(res["peak"], 202_000)

    def test_peak_survives_compaction(self):
        # Context shrinks after compaction; current reflects the small
        # post-compact value but peak and the compaction count expose it.
        write_jsonl(self.path, [
            usage_row(0, 380_000, 5_000, 3_000, "end_turn"),
            {"type": "summary", "isCompactSummary": True},
            usage_row(0, 60_000, 2_000, 1_000, "end_turn"),
        ])
        res = sg._scan(self.path)
        self.assertEqual(res["current"], 63_000)
        self.assertEqual(res["peak"], 388_000)
        self.assertEqual(res["compactions"], 1)

    def test_malformed_lines_skipped(self):
        write_jsonl(self.path, [
            "not json at all {",
            '"a bare json string"',
            "42",
            json.dumps({"message": "string-not-object"}),
            json.dumps({"message": {"usage": "string-not-object"}}),
            json.dumps({"message": {"usage": {"input_tokens": "NaN-ish",
                                              "output_tokens": None}}}),
            usage_row(1_000, 0, 0, 100, "end_turn"),
        ])
        res = sg._scan(self.path)
        self.assertEqual(res["current"], 1_100)

    def test_empty_file_unmeasurable(self):
        write_jsonl(self.path, [])
        self.assertIsNone(sg.measure(self.path, grace_ms=300))

    def test_missing_file_unmeasurable(self):
        self.assertIsNone(sg.measure(self.path + ".nope", grace_ms=300))


class QueueTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.cfg = dict(sg._DEFAULTS, state_dir=self.dir.name)

    def tearDown(self):
        self.dir.cleanup()

    def test_enqueue_drain_roundtrip_and_batch_cap(self):
        for i in range(5):
            sg.enqueue(self.cfg, "sess1", f"report-{i}")
        first = sg.drain_queue(self.cfg, "sess1", batch_max=3)
        self.assertEqual(first, ["report-0", "report-1", "report-2"])
        rest = sg.drain_queue(self.cfg, "sess1", batch_max=10)
        self.assertEqual(rest, ["report-3", "report-4"])
        self.assertEqual(sg.drain_queue(self.cfg, "sess1", 10), [])

    def test_sessions_isolated(self):
        sg.enqueue(self.cfg, "sess1", "one")
        self.assertEqual(sg.drain_queue(self.cfg, "sess2", 10), [])
        self.assertEqual(sg.drain_queue(self.cfg, "sess1", 10), ["one"])

    def test_agent_state_roundtrip(self):
        rec = {"agent_id": "aX", "name": "n", "current": 5}
        sg.write_agent_state(self.cfg, "sess1", rec)
        self.assertEqual(sg.load_agent_states(self.cfg, "sess1"), [rec])


class ConfigTests(unittest.TestCase):
    def test_env_overrides(self):
        os.environ["SUBAGENT_GAUGE_WARN_TOKENS"] = "42000"
        os.environ["SUBAGENT_GAUGE_HARD_BLOCK"] = "true"
        try:
            cfg = sg.load_config()
            self.assertEqual(cfg["warn_tokens"], 42000)
            self.assertTrue(cfg["hard_block"])
        finally:
            del os.environ["SUBAGENT_GAUGE_WARN_TOKENS"]
            del os.environ["SUBAGENT_GAUGE_HARD_BLOCK"]

    def test_bad_env_int_ignored(self):
        os.environ["SUBAGENT_GAUGE_WARN_TOKENS"] = "lots"
        try:
            cfg = sg.load_config()
            self.assertEqual(cfg["warn_tokens"], sg._DEFAULTS["warn_tokens"])
        finally:
            del os.environ["SUBAGENT_GAUGE_WARN_TOKENS"]


class FormatTests(unittest.TestCase):
    def test_over_threshold_flagged(self):
        res = {"current": 200_000, "peak": 200_000, "compactions": 0}
        line = sg.fmt_report("worker", "claude-opus-5", res, 150_000)
        self.assertIn("OVER THRESHOLD", line)
        self.assertIn("~200k", line)

    def test_under_threshold_plain(self):
        res = {"current": 40_000, "peak": 41_000, "compactions": 0}
        line = sg.fmt_report("worker", "", res, 150_000)
        self.assertNotIn("OVER THRESHOLD", line)


if __name__ == "__main__":
    unittest.main()
