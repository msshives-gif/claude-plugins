"""sigwrap.py behavior: exit-code and stdout passthrough, signal
logging with sender attribution, forwarding to the child."""
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest

PLUGIN = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SIGWRAP = os.path.join(PLUGIN, "bin", "sigwrap.py")


class SigwrapTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = dict(os.environ, TASK_FORENSICS_LOG_DIR=self.tmp.name)
        self.log = os.path.join(self.tmp.name, "log.jsonl")

    def records(self):
        if not os.path.isfile(self.log):
            return []
        with open(self.log) as fh:
            return [json.loads(line) for line in fh if line.strip()]

    def run_wrapped(self, command, timeout=30):
        return subprocess.run(
            [sys.executable, SIGWRAP, "--session", "test-sid", "--",
             command],
            capture_output=True, text=True, timeout=timeout, env=self.env)

    def wait_for(self, predicate, timeout=10):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.05)
        return False

    def test_exit_code_and_stdout_pass_through(self):
        r = self.run_wrapped("echo hi; exit 7")
        self.assertEqual(r.returncode, 7)
        self.assertEqual(r.stdout, "hi\n")
        events = [rec["event"] for rec in self.records()]
        self.assertEqual(events, ["armed", "exit"])
        exit_rec = self.records()[-1]
        self.assertEqual(exit_rec["returncode"], 7)
        self.assertIsNone(exit_rec["killed_by_signal"])
        self.assertEqual(exit_rec["session"], "test-sid")

    def test_sigterm_is_logged_with_sender_and_forwarded(self):
        proc = subprocess.Popen(
            [sys.executable, SIGWRAP, "--session", "test-sid", "--",
             "sleep 30"],
            env=self.env, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL)
        self.addCleanup(proc.wait)
        self.assertTrue(self.wait_for(
            lambda: any(r["event"] == "armed" for r in self.records())))
        os.kill(proc.pid, signal.SIGTERM)
        self.assertTrue(self.wait_for(
            lambda: proc.poll() is not None, timeout=15))
        self.assertEqual(proc.returncode, 128 + signal.SIGTERM)
        recs = self.records()
        sig = next(r for r in recs if r["event"] == "signal")
        self.assertEqual(sig["signo"], signal.SIGTERM)
        self.assertEqual(sig["si_pid"], os.getpid())
        self.assertEqual(sig["si_uid"], os.getuid())
        self.assertIn("python", sig["sender"]["cmdline"])
        exit_rec = next(r for r in recs if r["event"] == "exit")
        self.assertEqual(exit_rec["killed_by_signal"], signal.SIGTERM)

    def test_child_shares_process_group(self):
        # Group kills must reach the child directly even if the wrapper
        # cannot forward; armed record exposes the shared pgid.
        proc = subprocess.Popen(
            [sys.executable, SIGWRAP, "--", "sleep 30"],
            env=self.env, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL)
        self.addCleanup(proc.wait)
        self.assertTrue(self.wait_for(
            lambda: any(r["event"] == "armed" for r in self.records())))
        armed = next(r for r in self.records() if r["event"] == "armed")
        self.assertEqual(
            os.getpgid(armed["child_pid"]), armed["pgid"])
        os.kill(proc.pid, signal.SIGTERM)
        self.assertTrue(self.wait_for(
            lambda: proc.poll() is not None, timeout=15))

    def test_bad_arguments_exit_2(self):
        r = subprocess.run([sys.executable, SIGWRAP, "--session", "x"],
                           capture_output=True, text=True, env=self.env)
        self.assertEqual(r.returncode, 2)

    def test_logging_failure_does_not_break_the_task(self):
        env = dict(self.env,
                   TASK_FORENSICS_LOG_DIR="/dev/null/not-a-dir")
        r = subprocess.run(
            [sys.executable, SIGWRAP, "--", "echo still-works"],
            capture_output=True, text=True, timeout=30, env=env)
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout, "still-works\n")


if __name__ == "__main__":
    unittest.main()
