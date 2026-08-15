"""Install/uninstall script coverage, run against a temp settings file.

Regression anchor: uninstall.sh must remove ONLY subagent-context's own
hook entries. Its markers once included the bare string
"subagent-context", which substring-matches the absolute path of ANY
sibling plugin installed from plugins/<name>/ in this repo (the path
contains the repo directory name) — uninstalling subagent-context would
silently rip the sibling's hooks out. The markers are now path suffixes
of this plugin's three hook scripts only; these tests pin that.
"""
import json
import os
import subprocess
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INSTALL = os.path.join(REPO, "scripts", "install.sh")
UNINSTALL = os.path.join(REPO, "scripts", "uninstall.sh")


def _run(script, settings_path):
    return subprocess.run(
        ["bash", script, settings_path],
        capture_output=True, text=True, timeout=60)


def _hook_commands(settings):
    cmds = []
    for groups in settings.get("hooks", {}).values():
        for g in groups:
            for e in g.get("hooks", []):
                cmds.append(e.get("command", ""))
    return cmds


class InstallScriptTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.settings = os.path.join(self.tmp.name, "settings.json")

    def read(self):
        with open(self.settings) as fh:
            return json.load(fh)

    def test_install_then_uninstall_round_trips(self):
        r = _run(INSTALL, self.settings)
        self.assertEqual(r.returncode, 0, r.stderr)
        cmds = _hook_commands(self.read())
        self.assertEqual(len(cmds), 3)
        r = _run(UNINSTALL, self.settings)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(_hook_commands(self.read()), [])

    def test_install_is_idempotent(self):
        _run(INSTALL, self.settings)
        _run(INSTALL, self.settings)
        self.assertEqual(len(_hook_commands(self.read())), 3)

    def test_uninstall_spares_sibling_plugin_hooks(self):
        # A sibling installed from this repo's plugins/ dir: its absolute
        # command path CONTAINS the repo directory name ("subagent-context"
        # in a real checkout). Build it from REPO so the test exercises the
        # real hazard whatever the checkout is named.
        sibling = os.path.join(
            REPO, "plugins", "cross-session-send-guard", "hooks",
            "peer_send_guard.py")
        _run(INSTALL, self.settings)
        settings = self.read()
        settings["hooks"].setdefault("PreToolUse", []).append(
            {"matcher": "SendMessage", "hooks": [
                {"type": "command",
                 "command": f"python3 {sibling} || python {sibling}",
                 "timeout": 5}]})
        # A user's own unrelated hook must survive too.
        settings["hooks"].setdefault("PostToolUse", []).append(
            {"hooks": [{"type": "command",
                        "command": "echo user-hook", "timeout": 5}]})
        with open(self.settings, "w") as fh:
            json.dump(settings, fh)

        r = _run(UNINSTALL, self.settings)
        self.assertEqual(r.returncode, 0, r.stderr)
        cmds = _hook_commands(self.read())
        self.assertIn(f"python3 {sibling} || python {sibling}", cmds)
        self.assertIn("echo user-hook", cmds)
        self.assertEqual(len(cmds), 2)  # all three of our own are gone

    def test_hook_command_exits_zero_when_script_missing(self):
        # The installed command shape must fail open at the SHELL level:
        # a deleted clone or broken interpreter must not surface a
        # nonzero exit (2 would deny a PreToolUse tool call).
        missing = os.path.join(self.tmp.name, "gone", "guard.py")
        cmd = f"python3 '{missing}' || python '{missing}' || true"
        r = subprocess.run(["bash", "-c", cmd], capture_output=True)
        self.assertEqual(r.returncode, 0)

    def test_installed_command_carries_fail_open_tail(self):
        _run(INSTALL, self.settings)
        for cmd in _hook_commands(self.read()):
            self.assertTrue(cmd.endswith("|| true"), cmd)

    def test_uninstall_removes_legacy_gauge_paths(self):
        legacy = "/home/user/subagent-gauge/hooks/observer.py"
        with open(self.settings, "w") as fh:
            json.dump({"hooks": {"SubagentStop": [
                {"hooks": [{"type": "command",
                            "command": f"python3 {legacy}",
                            "timeout": 10}]}]}}, fh)
        r = _run(UNINSTALL, self.settings)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(_hook_commands(self.read()), [])


if __name__ == "__main__":
    unittest.main()
