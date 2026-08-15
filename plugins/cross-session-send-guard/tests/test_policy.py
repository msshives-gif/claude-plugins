"""Policy tiers end-to-end: run the real hook as a subprocess against a
faked sessions registry + transcripts, driving coldness via mtime."""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
HOOK = os.path.join(HERE, "..", "hooks", "peer_send_guard.py")
sys.path.insert(0, os.path.join(HERE, "..", "hooks"))
from test_peers import make_proc  # noqa: E402  (same dir import)


def usage_row(inp, cr, cc, out):
    return {"type": "assistant",
            "message": {"role": "assistant", "stop_reason": "end_turn",
                        "usage": {"input_tokens": inp,
                                  "cache_read_input_tokens": cr,
                                  "cache_creation_input_tokens": cc,
                                  "output_tokens": out}}}


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.sessions = os.path.join(self.tmp.name, "sessions")
        self.projects = os.path.join(self.tmp.name, "projects")
        self.proc = os.path.join(self.tmp.name, "proc")
        os.makedirs(self.sessions)

    def add_peer(self, name, tokens, cold, pid=101, session_id="sess-a"):
        with open(os.path.join(self.sessions, f"{pid}.json"), "w") as fh:
            json.dump({"pid": pid, "name": name, "sessionId": session_id,
                       "cwd": "/home/u/proj", "updatedAt": 1,
                       "procStart": 777}, fh)
        make_proc(self.proc, pid, 777)
        d = os.path.join(self.projects, "-home-u-proj")
        os.makedirs(d, exist_ok=True)
        tp = os.path.join(d, f"{session_id}.jsonl")
        with open(tp, "w") as fh:
            fh.write(json.dumps(usage_row(10, tokens - 1010, 0, 1000))
                     + "\n")
        if cold:
            old = time.time() - 7200
            os.utime(tp, (old, old))
        return tp

    def run_hook(self, to, agent_id=None, extra_env=None):
        payload = {"hook_event_name": "PreToolUse",
                   "session_id": "sender-session",
                   "tool_name": "SendMessage",
                   "tool_input": {"to": to, "message": "hi"}}
        if agent_id:
            payload["agent_id"] = agent_id
        env = {k: v for k, v in os.environ.items()
               if not k.startswith("CROSS_SESSION_SEND_GUARD_")}
        env.update(
            CROSS_SESSION_SEND_GUARD_CONFIG="/nonexistent/csg-test.json",
            CROSS_SESSION_SEND_GUARD_SESSIONS_DIR=self.sessions,
            CROSS_SESSION_SEND_GUARD_PROJECTS_DIR=self.projects,
            CROSS_SESSION_SEND_GUARD_PROC_ROOT=self.proc)
        env.update(extra_env or {})
        p = subprocess.run([sys.executable, HOOK],
                           input=json.dumps(payload), capture_output=True,
                           text=True, env=env, timeout=30)
        self.assertEqual(p.returncode, 0, p.stderr)
        return json.loads(p.stdout) if p.stdout.strip() else {}

    def test_small_warm_peer_silent(self):
        self.add_peer("peer-a", 30_000, cold=False)
        self.assertEqual(self.run_hook("peer-a"), {})

    def test_large_warm_peer_warns_no_ask(self):
        self.add_peer("peer-a", 200_000, cold=False)
        out = self.run_hook("peer-a")
        hso = out["hookSpecificOutput"]
        self.assertIn("~200k tokens", hso["additionalContext"])
        self.assertIn("warm", hso["additionalContext"])
        self.assertNotIn("permissionDecision", hso)

    def test_large_cold_peer_asks_from_root(self):
        self.add_peer("peer-a", 200_000, cold=True)
        out = self.run_hook("peer-a")
        hso = out["hookSpecificOutput"]
        self.assertEqual(hso["permissionDecision"], "ask")
        self.assertIn("cold", hso["additionalContext"])
        self.assertIn("$", hso["additionalContext"])  # cost estimate

    def test_large_cold_peer_warns_only_from_subagent(self):
        self.add_peer("peer-a", 200_000, cold=True)
        out = self.run_hook("peer-a", agent_id="aSub1")
        hso = out["hookSpecificOutput"]
        self.assertNotIn("permissionDecision", hso)

    def test_medium_cold_peer_warns_no_ask(self):
        self.add_peer("peer-a", 100_000, cold=True)  # warn <= x < block
        out = self.run_hook("peer-a")
        self.assertNotIn("permissionDecision", out["hookSpecificOutput"])

    def test_unresolvable_target_silent(self):
        self.assertEqual(self.run_hook("nobody-here"), {})

    def test_disabled_is_silent(self):
        self.add_peer("peer-a", 200_000, cold=True)
        out = self.run_hook("peer-a", extra_env={
            "CROSS_SESSION_SEND_GUARD_ENABLED": "false"})
        self.assertEqual(out, {})

    def test_unmeasurable_transcript_silent(self):
        tp = self.add_peer("peer-a", 200_000, cold=True)
        with open(tp, "w") as fh:
            fh.write("not json at all\n")
        old = time.time() - 7200
        os.utime(tp, (old, old))
        self.assertEqual(self.run_hook("peer-a"), {})

    def test_garbage_stdin_fails_open(self):
        p = subprocess.run([sys.executable, HOOK], input="{not json",
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(p.returncode, 0)


if __name__ == "__main__":
    unittest.main()
