"""wrap.py hook: rewrites only background Bash commands, emits the
documented updatedInput shape, and fails open (silent) everywhere
else."""
import json
import os
import shlex
import subprocess
import sys
import unittest

PLUGIN = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WRAP = os.path.join(PLUGIN, "hooks", "wrap.py")
SIGWRAP = os.path.join(PLUGIN, "bin", "sigwrap.py")


def run_hook(payload, env_extra=None):
    env = dict(os.environ)
    env.pop("TASK_FORENSICS_DISABLE", None)
    env.update(env_extra or {})
    stdin = payload if isinstance(payload, str) else json.dumps(payload)
    return subprocess.run([sys.executable, WRAP], input=stdin,
                          capture_output=True, text=True, timeout=30,
                          env=env)


def event(command="sleep 5", background=True, tool="Bash"):
    return {"session_id": "sid-1", "tool_name": tool,
            "tool_input": {"command": command,
                           "run_in_background": background}}


class WrapTests(unittest.TestCase):
    def test_background_command_is_wrapped(self):
        r = run_hook(event("echo 'a b'; sleep 1"))
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout)
        hso = out["hookSpecificOutput"]
        self.assertEqual(hso["hookEventName"], "PreToolUse")
        self.assertNotIn("permissionDecision", hso)
        updated = hso["updatedInput"]
        self.assertTrue(updated["run_in_background"])
        parts = shlex.split(updated["command"])
        self.assertEqual(parts[1], SIGWRAP)
        self.assertEqual(parts[2:4], ["--session", "sid-1"])
        self.assertEqual(parts[4], "--")
        self.assertEqual(parts[5], "echo 'a b'; sleep 1")

    def test_wrapped_command_round_trips_through_bash(self):
        # The rewritten string, run through bash exactly as the harness
        # would, must hand sigwrap the original command byte-for-byte.
        original = "echo \"q'uote\" $HOME; sleep 0"
        r = run_hook(event(original))
        updated = json.loads(r.stdout)["hookSpecificOutput"][
            "updatedInput"]["command"]
        probe = updated.replace(
            shlex.quote(SIGWRAP),
            shlex.quote(os.path.join(PLUGIN, "tests", "argv_echo.py")))
        rr = subprocess.run(["bash", "-c", probe], capture_output=True,
                            text=True, timeout=30)
        self.assertEqual(rr.returncode, 0, rr.stderr)
        argv = json.loads(rr.stdout)
        self.assertEqual(argv[argv.index("--") + 1:], [original])

    def test_foreground_command_untouched(self):
        r = run_hook(event(background=False))
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout, "")

    def test_non_bash_tool_untouched(self):
        r = run_hook(event(tool="Write"))
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout, "")

    def test_already_wrapped_command_untouched(self):
        r = run_hook(event(f"python3 {SIGWRAP} -- 'sleep 5'"))
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout, "")

    def test_empty_command_untouched(self):
        r = run_hook(event(""))
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout, "")

    def test_malformed_stdin_fails_open(self):
        r = run_hook("{not json")
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout, "")

    def test_disable_env_var(self):
        r = run_hook(event(), env_extra={"TASK_FORENSICS_DISABLE": "1"})
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout, "")


if __name__ == "__main__":
    unittest.main()
