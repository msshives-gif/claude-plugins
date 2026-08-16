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
        self.assertEqual(len(_hook_commands(s)), 8)
        # The wiring shape matters: matchers must match hooks.json.
        matchers = {(ev, g.get("matcher"))
                    for ev, groups in s["hooks"].items() for g in groups}
        self.assertIn(("PostToolUse", "*"), matchers)
        self.assertIn(("PreCompact", "manual"), matchers)
        self.assertIn(("PreCompact", "auto"), matchers)
        self.assertIn(("SessionStart", "compact"), matchers)
        self.assertIn(("SessionStart", "startup"), matchers)
        self.assertIn(("SessionStart", "resume"), matchers)
        # Slash commands are installed as substituted, marker-tagged
        # copies beside the settings file (plugin-context variables are
        # never substituted for script installs, so symlinks would run
        # /bin/compact-manager).
        cmd_dir = os.path.join(self.tmp.name, "commands")
        src_dir = os.path.join(PLUGIN, "commands")
        expected = sorted("compact-manager-" + n
                          for n in os.listdir(src_dir) if n.endswith(".md"))
        self.assertTrue(expected)
        self.assertEqual(sorted(os.listdir(cmd_dir)), expected)
        for name in expected:
            dest = os.path.join(cmd_dir, name)
            self.assertFalse(os.path.islink(dest), dest)
            with open(dest) as fh:
                body = fh.read()
            self.assertNotIn("${CLAUDE_PLUGIN_ROOT}", body, dest)
            self.assertIn(PLUGIN, body, dest)
            self.assertIn("installed by compact-manager install.sh from",
                          body, dest)
        r = _run(UNINSTALL, self.settings)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(_hook_commands(self.read()), [])
        # ...and uninstall removes exactly those copies, sparing strangers.
        self.assertEqual(os.listdir(cmd_dir), [])

    def test_install_never_overwrites_user_command_file(self):
        cmd_dir = os.path.join(self.tmp.name, "commands")
        os.makedirs(cmd_dir)
        mine = os.path.join(cmd_dir, "compact-manager-attach.md")
        with open(mine, "w") as fh:
            fh.write("user-owned command\n")
        r = _run(INSTALL, self.settings)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("SKIPPED", r.stderr)
        with open(mine) as fh:
            self.assertEqual(fh.read(), "user-owned command\n")
        # Uninstall spares it too (no marker).
        r = _run(UNINSTALL, self.settings)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(os.path.isfile(mine))
        with open(mine) as fh:
            self.assertEqual(fh.read(), "user-owned command\n")

    def test_install_upgrades_legacy_symlink_to_copy(self):
        # Early 0.3.0 installs created symlinks; a rerun must reclaim
        # exactly those (they resolve into this clone) and replace them
        # with copies — while a foreign symlink is left alone.
        cmd_dir = os.path.join(self.tmp.name, "commands")
        os.makedirs(cmd_dir)
        ours = os.path.join(cmd_dir, "compact-manager-attach.md")
        os.symlink(os.path.join(PLUGIN, "commands", "attach.md"), ours)
        foreign_target = os.path.join(self.tmp.name, "foreign.md")
        with open(foreign_target, "w") as fh:
            fh.write("foreign\n")
        foreign = os.path.join(cmd_dir, "compact-manager-detach.md")
        os.symlink(foreign_target, foreign)
        r = _run(INSTALL, self.settings)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(os.path.islink(ours))
        self.assertTrue(os.path.islink(foreign))
        self.assertEqual(os.path.realpath(foreign), foreign_target)

    def test_marker_phrase_in_prose_is_not_ownership(self):
        # NEW-1 pin: recognition is the exact anchored marker LINE, not
        # the phrase anywhere. A user file MENTIONING the phrase is
        # neither overwritten by install nor deleted by uninstall.
        cmd_dir = os.path.join(self.tmp.name, "commands")
        os.makedirs(cmd_dir)
        mine = os.path.join(cmd_dir, "compact-manager-attach.md")
        prose = ("KEEP my notes: the plugin is "
                 "installed by compact-manager install.sh from my repo\n"
                 "and a decoy: installXsh from /victim/compact-manager -->\n")
        with open(mine, "w") as fh:
            fh.write(prose)
        r = _run(INSTALL, self.settings)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("SKIPPED", r.stderr)
        with open(mine) as fh:
            self.assertEqual(fh.read(), prose)
        r = _run(UNINSTALL, self.settings)
        self.assertEqual(r.returncode, 0, r.stderr)
        with open(mine) as fh:
            self.assertEqual(fh.read(), prose)

    def test_uninstall_removes_legacy_symlink_spares_foreign_symlink(self):
        cmd_dir = os.path.join(self.tmp.name, "commands")
        os.makedirs(cmd_dir)
        ours = os.path.join(cmd_dir, "compact-manager-attach.md")
        os.symlink(os.path.join(PLUGIN, "commands", "attach.md"), ours)
        foreign_target = os.path.join(self.tmp.name, "foreign.md")
        with open(foreign_target, "w") as fh:
            fh.write("foreign\n")
        foreign = os.path.join(cmd_dir, "compact-manager-detach.md")
        os.symlink(foreign_target, foreign)
        r = _run(UNINSTALL, self.settings)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(os.path.lexists(ours))
        self.assertTrue(os.path.islink(foreign))

    def test_uninstall_spares_foreign_command_files(self):
        _run(INSTALL, self.settings)
        cmd_dir = os.path.join(self.tmp.name, "commands")
        foreign = os.path.join(cmd_dir, "compact-manager-attach.md")
        os.remove(foreign)  # replace our copy with a real user file
        with open(foreign, "w") as fh:
            fh.write("user-owned command\n")
        r = _run(UNINSTALL, self.settings)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(os.listdir(cmd_dir),
                         ["compact-manager-attach.md"])

    def test_install_is_idempotent(self):
        r1 = _run(INSTALL, self.settings)
        self.assertEqual(r1.returncode, 0, r1.stderr)
        r2 = _run(INSTALL, self.settings)
        self.assertEqual(r2.returncode, 0, r2.stderr)
        self.assertEqual(len(_hook_commands(self.read())), 8)

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
        self.assertEqual(len(cmds), 9)  # lookalike survives + our eight
        self.assertTrue(any(advisor in c and ".backup" not in c
                            for c in cmds))

    def test_shadow_prefix_path_does_not_block_install(self):
        # A command whose path merely CONTAINS ours as a suffix
        # ("/shadow<our path>") must not count as installed.
        advisor = os.path.join(PLUGIN, "hooks", "advisor.py")
        with open(self.settings, "w") as fh:
            json.dump({"hooks": {"PostToolUse": [
                {"matcher": "*", "hooks": [
                    {"type": "command",
                     "command": f"python3 /shadow{advisor} || true",
                     "timeout": 5}]}]}}, fh)
        r = _run(INSTALL, self.settings)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(len(_hook_commands(self.read())), 9)

    def test_backups_do_not_collide(self):
        # install (no file yet: no backup) -> uninstall -> install can
        # all run within one second; the pid suffix keeps each backup
        # distinct so nothing is overwritten.
        _run(INSTALL, self.settings)
        _run(UNINSTALL, self.settings)
        _run(INSTALL, self.settings)
        baks = [f for f in os.listdir(self.tmp.name)
                if ".bak-compact-manager-" in f]
        self.assertEqual(len(baks), 2)

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
        self.assertEqual(len(cmds), 3)  # all eight of our own are gone

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
