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

    def test_hook_commands_exit_zero_when_script_missing(self):
        # The REAL command definitions must fail open at the shell
        # level: a deleted clone or plugin dir must not surface a
        # nonzero exit (2 would deny a PreToolUse tool call). Run every
        # command from hooks.json with a nonexistent plugin root, and
        # the installer-emitted commands from a settings file pointing
        # at a deleted clone.
        with open(os.path.join(REPO, "hooks", "hooks.json")) as fh:
            hooks_json = json.load(fh)
        cmds = [e["command"]
                for groups in hooks_json["hooks"].values()
                for g in groups for e in g["hooks"]]
        self.assertEqual(len(cmds), 3)
        for cmd in cmds:
            self.assertTrue(cmd.endswith("|| true"), cmd)
            r = subprocess.run(
                ["bash", "-c", cmd], capture_output=True,
                env=dict(os.environ,
                         CLAUDE_PLUGIN_ROOT=os.path.join(self.tmp.name,
                                                         "gone")))
            self.assertEqual(r.returncode, 0, cmd)

    def test_installed_commands_exit_zero_when_clone_deleted(self):
        _run(INSTALL, self.settings)
        for cmd in _hook_commands(self.read()):
            broken = cmd.replace(REPO, os.path.join(self.tmp.name, "gone"))
            r = subprocess.run(["bash", "-c", broken], capture_output=True)
            self.assertEqual(r.returncode, 0, broken)

    def test_uninstall_spares_unrelated_hook_with_common_basename(self):
        # A third-party hook that happens to live at .../hooks/guard.py
        # (outside this clone and any standard-named install dir) must
        # survive.
        cmd = "python3 /opt/unrelated/hooks/guard.py"
        with open(self.settings, "w") as fh:
            json.dump({"hooks": {"PreToolUse": [
                {"hooks": [{"type": "command", "command": cmd,
                            "timeout": 5}]}]}}, fh)
        _run(UNINSTALL, self.settings)
        self.assertEqual(_hook_commands(self.read()), [cmd])

    def test_installed_command_carries_fail_open_tail(self):
        _run(INSTALL, self.settings)
        for cmd in _hook_commands(self.read()):
            self.assertTrue(cmd.endswith("|| true"), cmd)

    def test_uninstall_spares_sibling_reusing_hook_basename(self):
        # Even a sibling that names its hook guard.py must survive: the
        # plugins/ path segment marks it as not ours.
        sibling = os.path.join(REPO, "plugins", "some-sibling", "hooks",
                               "guard.py")
        with open(self.settings, "w") as fh:
            json.dump({"hooks": {"PreToolUse": [
                {"hooks": [{"type": "command",
                            "command": f"python3 {sibling}",
                            "timeout": 5}]}]}}, fh)
        _run(UNINSTALL, self.settings)
        self.assertEqual(_hook_commands(self.read()),
                         [f"python3 {sibling}"])

    def test_uninstall_matches_windows_backslash_paths(self):
        cmd = r'python3 "C:\Users\u\subagent-context\hooks\guard.py"'
        with open(self.settings, "w") as fh:
            json.dump({"hooks": {"PreToolUse": [
                {"hooks": [{"type": "command", "command": cmd,
                            "timeout": 5}]}]}}, fh)
        _run(UNINSTALL, self.settings)
        self.assertEqual(_hook_commands(self.read()), [])

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
