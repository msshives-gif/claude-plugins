"""Manual install/uninstall coverage against a temp settings file.

Regression anchor (same hazard as the siblings): every plugin in this
repo shares the repo directory name in its absolute paths, so uninstall
matching must key on THIS plugin's hook script paths — never the repo
name — or uninstalling compact-manager would rip out subagent-context
and cross-session-send-guard hooks too.
"""
import json
import os
import subprocess
import tempfile
import unittest

PLUGIN = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO = os.path.dirname(os.path.dirname(PLUGIN))
INSTALL = os.path.join(PLUGIN, "scripts", "install.sh")
UNINSTALL = os.path.join(PLUGIN, "scripts", "uninstall.sh")


def _run(script, settings_path):
    return subprocess.run(["bash", script, settings_path],
                          capture_output=True, text=True, timeout=60)


def _hook_commands(settings):
    cmds = []
    for groups in settings.get("hooks", {}).values():
        for g in groups:
            for e in g.get("hooks", []):
                cmds.append(e.get("command", ""))
    return cmds


class CompactManagerInstallTests(unittest.TestCase):
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
        s = self.read()
        self.assertEqual(len(_hook_commands(s)), 6)
        # The wiring shape matters: matchers must match hooks.json.
        matchers = {(ev, g.get("matcher"))
                    for ev, groups in s["hooks"].items() for g in groups}
        self.assertIn(("PostToolUse", "*"), matchers)
        self.assertIn(("PreCompact", "manual"), matchers)
        self.assertIn(("PreCompact", "auto"), matchers)
        self.assertIn(("SessionStart", "compact"), matchers)
        r = _run(UNINSTALL, self.settings)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(_hook_commands(self.read()), [])

    def test_install_is_idempotent(self):
        r1 = _run(INSTALL, self.settings)
        self.assertEqual(r1.returncode, 0, r1.stderr)
        r2 = _run(INSTALL, self.settings)
        self.assertEqual(r2.returncode, 0, r2.stderr)
        self.assertEqual(len(_hook_commands(self.read())), 6)

    def test_backup_suffix_lookalike_does_not_block_install(self):
        # A command referencing "<our advisor path>.backup" must not
        # make the installer think the real hook is already present.
        advisor = os.path.join(PLUGIN, "hooks", "advisor.py")
        with open(self.settings, "w") as fh:
            json.dump({"hooks": {"PostToolUse": [
                {"matcher": "*", "hooks": [
                    {"type": "command",
                     "command": f"python3 {advisor}.backup || true",
                     "timeout": 5}]}]}}, fh)
        r = _run(INSTALL, self.settings)
        self.assertEqual(r.returncode, 0, r.stderr)
        cmds = _hook_commands(self.read())
        self.assertEqual(len(cmds), 7)  # lookalike survives + our six
        self.assertTrue(any(advisor in c and ".backup" not in c
                            for c in cmds))

    def test_uninstall_spares_sibling_and_root_plugin_hooks(self):
        # Both siblings' absolute paths contain the repo dir name; a
        # repo-name marker would remove them all.
        root_hook = os.path.join(REPO, "hooks", "guard.py")
        peer_hook = os.path.join(REPO, "plugins", "cross-session-send-guard",
                                 "hooks", "peer_send_guard.py")
        _run(INSTALL, self.settings)
        s = self.read()
        s["hooks"].setdefault("PreToolUse", []).append(
            {"matcher": "SendMessage", "hooks": [
                {"type": "command",
                 "command": f"python3 {root_hook} || true", "timeout": 5},
                {"type": "command",
                 "command": f"python3 {peer_hook} || true", "timeout": 5}]})
        s["hooks"].setdefault("PostToolUse", []).append(
            {"hooks": [{"type": "command", "command": "echo user-hook",
                        "timeout": 5}]})
        with open(self.settings, "w") as fh:
            json.dump(s, fh)
        r = _run(UNINSTALL, self.settings)
        self.assertEqual(r.returncode, 0, r.stderr)
        cmds = _hook_commands(self.read())
        self.assertIn(f"python3 {root_hook} || true", cmds)
        self.assertIn(f"python3 {peer_hook} || true", cmds)
        self.assertIn("echo user-hook", cmds)
        self.assertEqual(len(cmds), 3)  # all six of our own are gone

    def test_uninstall_removes_standard_plugin_dir_paths(self):
        # A marketplace install lives under …/compact-manager/hooks/;
        # uninstall must catch those paths too.
        cmd = ("python3 /x/plugins/cache/compact-manager/hooks/advisor.py"
               " || true")
        with open(self.settings, "w") as fh:
            json.dump({"hooks": {"PostToolUse": [
                {"matcher": "*", "hooks": [
                    {"type": "command", "command": cmd,
                     "timeout": 5}]}]}}, fh)
        _run(UNINSTALL, self.settings)
        self.assertEqual(_hook_commands(self.read()), [])

    def test_uninstall_spares_backup_and_basename_reuse(self):
        keep = [
            "python3 /elsewhere/other-tool/hooks/advisor.py || true",
            "python3 /x/compact-manager/hooks/advisor.py.backup || true",
        ]
        with open(self.settings, "w") as fh:
            json.dump({"hooks": {"PostToolUse": [
                {"matcher": "*", "hooks": [
                    {"type": "command", "command": c, "timeout": 5}
                    for c in keep]}]}}, fh)
        _run(UNINSTALL, self.settings)
        self.assertEqual(sorted(_hook_commands(self.read())), sorted(keep))


if __name__ == "__main__":
    unittest.main()
