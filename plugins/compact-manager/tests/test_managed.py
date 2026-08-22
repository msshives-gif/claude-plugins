"""Unit tests for compact-manager Layer 2 managed mode."""
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
HOOKS = os.path.abspath(os.path.join(HERE, "..", "hooks"))
sys.path.insert(0, HOOKS)
import compact_manager as cm  # noqa: E402
import managed  # noqa: E402


def usage_row(current, model="claude-test"):
    return {"type": "assistant", "message": {
        "model": model, "stop_reason": "end_turn",
        "usage": {"input_tokens": current, "cache_read_input_tokens": 0,
                  "cache_creation_input_tokens": 0, "output_tokens": 0}}}


def boundary_row(post=100, trigger="manual"):
    return {"type": "system", "subtype": "compact_boundary",
            "compactMetadata": {"trigger": trigger, "postTokens": post}}


def append_rows(path, rows):
    with open(path, "ab") as fh:
        for row in rows:
            raw = row if isinstance(row, bytes) else json.dumps(row).encode()
            fh.write(raw + b"\n")


def write_proc(root, pid, tpgid, start, state="S"):
    directory = os.path.join(root, str(pid))
    os.makedirs(directory, exist_ok=True)
    # After comm: state(3), ppid(4), pgrp(5), session(6), tty_nr(7),
    # tpgid(8), then fields through starttime(22).
    tail = [state, "1", str(tpgid), "1", "1", str(tpgid)]
    tail += ["0"] * 13 + [str(start)]
    with open(os.path.join(directory, "stat"), "w") as fh:
        fh.write("%s (odd ) comm) %s\n" % (pid, " ".join(tail)))


class Result:
    def __init__(self, stdout="", returncode=0, stderr=""):
        self.stdout, self.returncode, self.stderr = stdout, returncode, stderr


class BindingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.proc = os.path.join(self.temp.name, "proc")
        self.sessions = os.path.join(self.temp.name, "sessions")
        self.projects = os.path.join(self.temp.name, "projects")
        os.makedirs(self.proc)
        os.makedirs(self.sessions)
        os.makedirs(self.projects)
        write_proc(self.proc, 10, 20, 1010)
        write_proc(self.proc, 20, 20, 2020)
        cwd = "/work/here"
        sid = "session-1234"
        with open(os.path.join(self.sessions, "20.json"), "w") as fh:
            json.dump({"pid": 20, "procStart": "2020", "sessionId": sid,
                       "cwd": cwd}, fh)
        project = os.path.join(self.projects, cwd.replace("/", "-"))
        os.makedirs(project)
        self.transcript = os.path.join(project, sid + ".jsonl")
        append_rows(self.transcript, [usage_row(1)])

    def runner(self, _argv):
        return Result("%1\t/dev/pts/7\t10\tclaude\t0\t120\t40\t$1\n")

    def test_proc_field_arithmetic_uses_last_paren(self):
        parsed = managed.proc_stat(10, self.proc)
        self.assertEqual(parsed, {"state": "S", "tpgid": 20,
                                  "starttime": 1010})

    def test_binding_walk_and_derived_transcript(self):
        binding, error = managed.build_binding(
            "sock", "%1", True, self.runner, self.proc, self.sessions,
            self.projects)
        self.assertIsNone(error)
        self.assertEqual(binding["claude_pid"], 20)
        self.assertEqual(binding["transcript_path"], self.transcript)
        self.assertEqual(binding["pane_root_start"], 1010)
        self.assertTrue(binding["attended"])

    def test_derives_transcript_for_dotted_underscored_cwd(self):
        # Claude Code slugs EVERY non-alphanumeric to "-" (verified live);
        # a cwd like ~/.cache/foo_bar must still resolve.
        cwd = "/work/.here_x"
        with open(os.path.join(self.sessions, "20.json"), "w") as fh:
            json.dump({"pid": 20, "procStart": "2020",
                       "sessionId": "session-1234", "cwd": cwd}, fh)
        project = os.path.join(self.projects, "-work--here-x")
        os.makedirs(project)
        transcript = os.path.join(project, "session-1234.jsonl")
        append_rows(transcript, [usage_row(1)])
        binding, error = managed.build_binding(
            "", "%1", True, self.runner, self.proc, self.sessions,
            self.projects)
        self.assertIsNone(error)
        self.assertEqual(binding["transcript_path"], transcript)

    def test_virgin_session_binds_with_pending_transcript(self):
        # start spawns claude BEFORE any turn exists; the path derives
        # (containment-checked) even though the file does not exist yet.
        os.unlink(self.transcript)
        binding, error = managed.build_binding(
            "", "%1", True, self.runner, self.proc, self.sessions,
            self.projects)
        self.assertIsNone(error)
        self.assertEqual(binding["transcript_path"], self.transcript)

    def test_rejects_registry_procstart_mismatch(self):
        with open(os.path.join(self.sessions, "20.json"), "w") as fh:
            json.dump({"procStart": "9", "sessionId": "session-1234",
                       "cwd": "/work/here"}, fh)
        binding, error = managed.build_binding(
            "", "%1", True, self.runner, self.proc, self.sessions,
            self.projects)
        self.assertIsNone(binding)
        self.assertEqual(error, "registry_start_mismatch")

    def test_rejects_nonleader_member_shape(self):
        # The tpgid is authoritative.  No registry for that group leader means
        # a claude that is merely a member cannot be guessed.
        os.unlink(os.path.join(self.sessions, "20.json"))
        binding, error = managed.build_binding(
            "", "%1", True, self.runner, self.proc, self.sessions,
            self.projects)
        self.assertIsNone(binding)
        self.assertEqual(error, "registry_missing")

    def test_validate_binding_foreground_loss_is_distinct(self):
        binding, _ = managed.build_binding(
            "", "%1", True, self.runner, self.proc, self.sessions,
            self.projects, "a" * 16)
        write_proc(self.proc, 10, 99, 1010)
        cfg = managed.load_config(base=dict(cm._DEFAULTS, state_dir=self.temp.name),
                                  environ={})
        ok, why = managed.validate_binding(
            binding, cfg, run_tmux=self.runner, proc_root=self.proc,
            check_leases=False)
        self.assertFalse(ok)
        self.assertEqual(why, "foreground_lost")

    def _validate(self, binding):
        cfg = managed.load_config(
            base=dict(cm._DEFAULTS, state_dir=self.temp.name), environ={})
        return managed.validate_binding(
            binding, cfg, run_tmux=self.runner, proc_root=self.proc,
            check_leases=False, sessions_dir=self.sessions)

    def test_validate_binding_retires_on_session_rotation(self):
        # /clear or in-app /resume: same pid, same starttime, registry
        # now names a different session id -> positive rotation proof.
        binding, _ = managed.build_binding(
            "", "%1", True, self.runner, self.proc, self.sessions,
            self.projects, "a" * 16)
        ok, _ = self._validate(binding)
        self.assertTrue(ok)  # matching id: healthy
        with open(os.path.join(self.sessions, "20.json"), "w") as fh:
            json.dump({"pid": 20, "procStart": "2020",
                       "sessionId": "session-5678", "cwd": "/work/here"},
                      fh)
        ok, why = self._validate(binding)
        self.assertFalse(ok)
        self.assertEqual(why, "session_rotated")
        # rotation is not a wait state: _transient must say retire
        # (self is unused on the non-pane_missing path)
        import types
        self.assertFalse(managed.Watcher._transient(
            types.SimpleNamespace(), "session_rotated"))

    def test_validate_binding_rotation_needs_positive_proof(self):
        binding, _ = managed.build_binding(
            "", "%1", True, self.runner, self.proc, self.sessions,
            self.projects, "a" * 16)
        registry = os.path.join(self.sessions, "20.json")
        # different id but procStart mismatch: not the same process,
        # proves nothing
        with open(registry, "w") as fh:
            json.dump({"pid": 20, "procStart": "9999",
                       "sessionId": "session-5678"}, fh)
        self.assertTrue(self._validate(binding)[0])
        # malformed entry: proves nothing
        with open(registry, "w") as fh:
            fh.write("not json")
        self.assertTrue(self._validate(binding)[0])
        # invalid session id shape: proves nothing
        with open(registry, "w") as fh:
            json.dump({"pid": 20, "procStart": "2020", "sessionId": "x"},
                      fh)
        self.assertTrue(self._validate(binding)[0])
        # missing entry entirely: proves nothing
        os.unlink(registry)
        self.assertTrue(self._validate(binding)[0])


class LeaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.proc = os.path.join(self.temp.name, "proc")
        os.makedirs(self.proc)
        self.cfg = dict(cm._DEFAULTS, state_dir=self.temp.name)
        self.paths = managed.managed_paths(self.cfg, "session-1234", "s", "%1")
        self.token = "a" * 16
        write_proc(self.proc, 50, 50, 5050)

    def test_acquires_pair(self):
        ok, detail = managed.acquire_leases(
            self.paths, self.token, 50, 5050, now=1000, proc_root=self.proc)
        self.assertTrue(ok, detail)
        self.assertTrue(managed.leases_owned(self.paths, self.token))

    def test_fresh_heartbeat_refuses_even_dead_pid(self):
        ok, _ = managed.acquire_leases(
            self.paths, "b" * 16, 999, 1, now=1000, proc_root=self.proc)
        self.assertTrue(ok)
        ok, detail = managed.acquire_leases(
            self.paths, self.token, 50, 5050, now=1001, proc_root=self.proc)
        self.assertFalse(ok)
        self.assertEqual(detail["reason"], "lease_held")

    def test_stale_and_dead_reclaimed(self):
        ok, _ = managed.acquire_leases(
            self.paths, "b" * 16, 999, 1, now=1000, proc_root=self.proc)
        self.assertTrue(ok)
        ok, detail = managed.acquire_leases(
            self.paths, self.token, 50, 5050,
            now=1000 + managed.LEASE_FRESH_S + 1, proc_root=self.proc)
        self.assertTrue(ok, detail)
        self.assertTrue(managed.leases_owned(self.paths, self.token))

    def test_prune_defers_journal_while_session_state_fresh(self):
        # A marathon session can outlive its dead watcher's lease TTL;
        # reaping its journal then would flip a real fired-compacts
        # count to null ("never watched") while the session is still on
        # the overview (Sol round-1). Cleanup must wait for the state
        # file to age out of the 24h window.
        now = 10_000_000
        lease_dir = os.path.join(self.temp.name, "managed", "leases")
        cm._private_makedirs(lease_dir)
        dead = {"run_token": "t" * 16, "pid": 999, "proc_start": 1,
                "heartbeat_at": now - 8 * 86_400}
        managed._atomic_json(
            os.path.join(lease_dir, "session-oldsid.json"), dead)
        managed._atomic_json(os.path.join(lease_dir, "pane-x.json"), dead)
        journal = os.path.join(self.temp.name, "managed", "watchers",
                               "oldsid.journal.jsonl")
        managed.journal_record(journal, "ACKED",
                               {"attempt_id": "a", "nonces": []})
        state_dir = os.path.join(self.temp.name, "state")
        os.makedirs(state_dir)
        state_path = os.path.join(state_dir, "oldsid.json")
        with open(state_path, "w") as fh:
            fh.write("{}")
        os.utime(state_path, (now - 100, now - 100))
        managed._prune_managed_locked(self.cfg, now=now,
                                      proc_root=self.proc)
        self.assertTrue(os.path.exists(journal))
        os.utime(state_path, (now - 86_401, now - 86_401))
        managed._prune_managed_locked(self.cfg, now=now,
                                      proc_root=self.proc)
        self.assertFalse(os.path.exists(journal))

    def test_prune_leaves_tokenless_lease_and_artifacts_untouched(self):
        # A dead, stale lease MISSING run_token has no cleanup
        # authority: deleting its artifacts and then KeyErroring out of
        # acquire_leases would poison every later adoption (Sol
        # round-3 blocker). It must be skipped entirely, no exception.
        now = 10_000_000
        lease_dir = os.path.join(self.temp.name, "managed", "leases")
        cm._private_makedirs(lease_dir)
        managed._atomic_json(
            os.path.join(lease_dir, "session-oldsid.json"),
            {"pid": 999, "proc_start": 1,
             "heartbeat_at": now - 8 * 86_400})
        journal = os.path.join(self.temp.name, "managed", "watchers",
                               "oldsid.journal.jsonl")
        managed.journal_record(journal, "ACKED",
                               {"attempt_id": "a", "nonces": []})
        managed._prune_managed_locked(self.cfg, now=now,
                                      proc_root=self.proc)
        self.assertTrue(os.path.exists(journal))
        self.assertTrue(os.path.exists(
            os.path.join(lease_dir, "session-oldsid.json")))

    def test_prune_sweeps_leaseless_stale_artifacts(self):
        # Clean retirement releases both leases but leaves the journal;
        # nothing ever revisited those files (Sol round-3). Past the
        # TTL, with the session off the overview, they are swept — but
        # a fresh state file, a recent mtime, or the session being
        # acquired all keep them.
        now = 10_000_000
        cm._private_makedirs(
            os.path.join(self.temp.name, "managed", "leases"))
        wdir = os.path.join(self.temp.name, "managed", "watchers")
        old = now - 8 * 86_400
        swept = os.path.join(wdir, "gone.journal.jsonl")
        managed.journal_record(swept, "WATCHER_RETIRED",
                               {"attempt_id": None, "nonces": []})
        os.utime(swept, (old, old))
        recent = os.path.join(wdir, "recent.journal.jsonl")
        managed.journal_record(recent, "WATCHER_RETIRED",
                               {"attempt_id": None, "nonces": []})
        fresh_state = os.path.join(wdir, "freshstate.journal.jsonl")
        managed.journal_record(fresh_state, "WATCHER_RETIRED",
                               {"attempt_id": None, "nonces": []})
        os.utime(fresh_state, (old, old))
        state_dir = os.path.join(self.temp.name, "state")
        os.makedirs(state_dir)
        with open(os.path.join(state_dir, "freshstate.json"), "w") as fh:
            fh.write("{}")
        acquiring = os.path.join(wdir, "session-1234.journal.jsonl")
        managed.journal_record(acquiring, "SUBMITTED",
                               {"attempt_id": "x", "nonces": []})
        os.utime(acquiring, (old, old))
        managed._prune_managed_locked(
            self.cfg,
            exclude_session_lease=self.paths["session_lease"],
            now=now, proc_root=self.proc)
        self.assertFalse(os.path.exists(swept))
        self.assertTrue(os.path.exists(recent))
        self.assertTrue(os.path.exists(fresh_state))
        self.assertTrue(os.path.exists(acquiring))

    def test_prune_sweep_respects_last_instant_identity(self):
        # The advisor's atomic replace is NOT under the txn lock: if
        # the pathname holds a different file by unlink time (inode or
        # mtime changed), eligibility was judged on a different file
        # and the sweep must leave it alone (Sol round-4).
        now = 10_000_000
        cm._private_makedirs(
            os.path.join(self.temp.name, "managed", "leases"))
        wdir = os.path.join(self.temp.name, "managed", "watchers")
        target = os.path.join(wdir, "gone.journal.jsonl")
        managed.journal_record(target, "WATCHER_RETIRED",
                               {"attempt_id": None, "nonces": []})
        old = now - 8 * 86_400
        os.utime(target, (old, old))
        real_lstat = os.lstat
        seen = {"n": 0}

        def replaced_between_checks(path, *args, **kwargs):
            res = real_lstat(path, *args, **kwargs)
            if os.fspath(path).endswith("gone.journal.jsonl"):
                seen["n"] += 1
                if seen["n"] == 2:  # the pre-unlink re-proof
                    return os.stat_result(
                        (res.st_mode, res.st_ino + 1, res.st_dev,
                         res.st_nlink, res.st_uid, res.st_gid,
                         res.st_size, res.st_atime, res.st_mtime,
                         res.st_ctime))
            return res

        with mock.patch.object(managed.os, "lstat",
                               side_effect=replaced_between_checks):
            managed._prune_managed_locked(self.cfg, now=now,
                                          proc_root=self.proc)
        self.assertEqual(seen["n"], 2)
        self.assertTrue(os.path.exists(target))

    def test_prune_reaps_session_lease_orphaned_by_pane_reuse(self):
        # While the fresh-state deferral holds a dead pair, another
        # session's acquisition can reuse the pane and overwrite the
        # pane lease with a new token. Once the state ages out, the
        # old session lease has ZERO matching pane leases — it must
        # still be reclaimable, not orphaned forever (Sol round-2).
        now = 10_000_000
        lease_dir = os.path.join(self.temp.name, "managed", "leases")
        cm._private_makedirs(lease_dir)
        dead = {"run_token": "t" * 16, "pid": 999, "proc_start": 1,
                "heartbeat_at": now - 8 * 86_400}
        managed._atomic_json(
            os.path.join(lease_dir, "session-oldsid.json"), dead)
        # The pane lease now belongs to a LIVE successor token.
        managed._atomic_json(
            os.path.join(lease_dir, "pane-x.json"),
            {"run_token": "u" * 16, "pid": 50, "proc_start": 5050,
             "heartbeat_at": now})
        journal = os.path.join(self.temp.name, "managed", "watchers",
                               "oldsid.journal.jsonl")
        managed.journal_record(journal, "ACKED",
                               {"attempt_id": "a", "nonces": []})
        managed._prune_managed_locked(self.cfg, now=now,
                                      proc_root=self.proc)
        self.assertFalse(os.path.exists(journal))
        self.assertFalse(os.path.exists(
            os.path.join(lease_dir, "session-oldsid.json")))
        # The successor's pane lease is untouched.
        self.assertTrue(os.path.exists(
            os.path.join(lease_dir, "pane-x.json")))

    def test_second_failure_rolls_back_first(self):
        cm._private_makedirs(os.path.dirname(self.paths["pane_lease"]))
        managed._atomic_json(
            self.paths["pane_lease"],
            {"run_token": "held", "pid": 50, "proc_start": 5050,
             "heartbeat_at": 1000})
        ok, _ = managed.acquire_leases(
            self.paths, self.token, 50, 5050, now=1001, proc_root=self.proc)
        self.assertFalse(ok)
        self.assertFalse(os.path.exists(self.paths["session_lease"]))

    def test_rollback_never_deletes_successor(self):
        cm._private_makedirs(os.path.dirname(self.paths["pane_lease"]))
        managed._atomic_json(
            self.paths["pane_lease"],
            {"run_token": "held", "pid": 50, "proc_start": 5050,
             "heartbeat_at": 1000})

        def successor():
            managed._atomic_json(
                self.paths["session_lease"],
                {"run_token": "successor", "pid": 50,
                 "proc_start": 5050, "heartbeat_at": 1000})
        ok, _ = managed.acquire_leases(
            self.paths, self.token, 50, 5050, now=1001,
            proc_root=self.proc, before_second=successor)
        self.assertFalse(ok)
        value, _ = managed.read_json_inode(self.paths["session_lease"])
        self.assertEqual(value["run_token"], "successor")

    def test_conditional_release_never_deletes_successor(self):
        ok, detail = managed.acquire_leases(
            self.paths, self.token, 50, 5050, now=1000, proc_root=self.proc)
        self.assertTrue(ok)
        inode = detail["inodes"][self.paths["session_lease"]]
        managed._atomic_json(
            self.paths["session_lease"],
            {"run_token": "successor", "pid": 50,
             "proc_start": 5050, "heartbeat_at": 1001})
        self.assertFalse(managed.conditional_remove(
            self.paths["session_lease"], self.token, inode,
            txn_path=self.paths["txn"]))

    def test_heartbeat_replaces_only_owned_pair(self):
        ok, _ = managed.acquire_leases(
            self.paths, self.token, 50, 5050, now=1000, proc_root=self.proc)
        self.assertTrue(ok)
        self.assertTrue(managed.heartbeat_leases(self.paths, self.token, 1010))
        for path in (self.paths["session_lease"], self.paths["pane_lease"]):
            value, _ = managed.read_json_inode(path)
            self.assertEqual(value["heartbeat_at"], 1010)

    def test_heartbeat_cannot_resurrect_rolled_back_lease(self):
        ok, _ = managed.acquire_leases(
            self.paths, self.token, 50, 5050, now=1000, proc_root=self.proc)
        self.assertTrue(ok)
        os.unlink(self.paths["pane_lease"])
        self.assertFalse(managed.heartbeat_leases(self.paths, self.token, 1010))
        self.assertFalse(os.path.exists(self.paths["pane_lease"]))


class CursorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = os.path.join(self.temp.name, "t.jsonl")

    def test_generation_stable_under_growth(self):
        append_rows(self.path, [usage_row(100), boundary_row(20)])
        cursor = managed.initial_scan(managed.default_cursor(), self.path)
        before = managed.generation(cursor)
        append_rows(self.path, [usage_row(30)])
        cursor = managed.scan_cursor(cursor, self.path)
        self.assertEqual(managed.generation(cursor), before)
        self.assertEqual(cursor["current"], 30)

    def test_boundary_resets_current_to_posttokens(self):
        append_rows(self.path, [usage_row(900), boundary_row(77)])
        cursor = managed.initial_scan(managed.default_cursor(), self.path)
        self.assertEqual(cursor["current"], 77)

    def test_boundary_missing_posttokens_resets_zero(self):
        append_rows(self.path, [usage_row(900),
                                {"compactMetadata": {"trigger": "manual"}}])
        cursor = managed.initial_scan(managed.default_cursor(), self.path)
        self.assertEqual(cursor["current"], 0)

    def test_same_inode_truncate_regrow_anchor_mismatch(self):
        append_rows(self.path, [usage_row(900), boundary_row(77)])
        cursor = managed.initial_scan(managed.default_cursor(), self.path)
        inode = os.stat(self.path).st_ino
        epoch = cursor["file_epoch"]
        with open(self.path, "wb") as fh:
            pass
        append_rows(self.path, [usage_row(33), usage_row(44), usage_row(55)])
        self.assertEqual(os.stat(self.path).st_ino, inode)
        cursor = managed.scan_cursor(cursor, self.path)
        self.assertEqual(cursor["file_epoch"], epoch + 1)
        self.assertEqual(cursor["current"], 55)

    def test_offline_recreate_detected_by_identity(self):
        append_rows(self.path, [usage_row(100)])
        cursor = managed.initial_scan(managed.default_cursor(), self.path)
        epoch = cursor["file_epoch"]
        os.unlink(self.path)
        append_rows(self.path, [usage_row(20)])
        cursor = managed.scan_cursor(cursor, self.path)
        self.assertEqual(cursor["file_epoch"], epoch + 1)
        self.assertEqual(cursor["current"], 20)

    def test_catchup_suppresses_until_snapshot_drained(self):
        append_rows(self.path, [usage_row(10), usage_row(20)])
        cursor = managed.scan_cursor(managed.default_cursor(), self.path, cap=20)
        self.assertFalse(cursor["caught_up"])
        cursor = managed.initial_scan(cursor, self.path)
        self.assertTrue(cursor["caught_up"])

    def test_trailing_fragment_not_consumed(self):
        append_rows(self.path, [usage_row(10)])
        with open(self.path, "ab") as fh:
            fh.write(b'{"half":')
        cursor = managed.scan_cursor(managed.default_cursor(), self.path)
        self.assertTrue(cursor["trailing_fragment"])
        self.assertFalse(cursor["caught_up"])

    def test_saved_cursor_has_exact_durable_shape(self):
        append_rows(self.path, [usage_row(10)])
        cursor = managed.initial_scan(managed.default_cursor(), self.path)
        saved = os.path.join(self.temp.name, "scan.json")
        managed.save_cursor(saved, cursor)
        with open(saved) as fh:
            value = json.load(fh)
        self.assertEqual(set(value), {
            "device", "inode", "observed_size", "offset", "file_epoch",
            "current", "boundary_count", "last_boundary", "anchor", "model",
            "discard_to_newline"})
        self.assertEqual(set(value["anchor"]),
                         {"offset", "sha256_of_first_row"})

    def test_giant_row_discard_keeps_advancing(self):
        # A single row larger than the scan budget must not stall the
        # cursor forever; the discard escape skips to the next newline
        # and later rows (including boundaries) still parse. The flag
        # must survive a save/load round-trip mid-discard.
        append_rows(self.path, [b"x" * 1000, usage_row(42),
                                boundary_row(post=7)])
        saved = os.path.join(self.temp.name, "scan.json")
        cursor = managed.scan_cursor(managed.default_cursor(), self.path,
                                     cap=300)
        self.assertTrue(cursor["discard_to_newline"])
        managed.save_cursor(saved, cursor)
        cursor = managed.load_cursor(saved)
        self.assertTrue(cursor["discard_to_newline"])
        for _ in range(40):
            cursor = managed.scan_cursor(cursor, self.path, cap=300)
            if cursor.get("caught_up"):
                break
        self.assertTrue(cursor["caught_up"])
        self.assertEqual(cursor["current"], 7)
        self.assertEqual(cursor["boundary_count"], 1)
        self.assertFalse(cursor["discard_to_newline"])

    def test_giant_first_row_gets_prefix_anchor_detecting_regrow(self):
        # Even an all-skipped file must carry an identity anchor, or a
        # same-size truncate-and-regrow behind the offset goes unseen.
        append_rows(self.path, [b"y" * 1000])
        cursor = managed.default_cursor()
        for _ in range(20):
            cursor = managed.scan_cursor(cursor, self.path, cap=300)
            if cursor.get("caught_up") or not cursor.get("trailing_fragment"):
                break
        self.assertIsNotNone(cursor["anchor"])
        self.assertIn("sha256_of_prefix", cursor["anchor"])
        epoch = cursor["file_epoch"]
        with open(self.path, "r+b") as fh:  # same inode, same size
            fh.seek(0)
            fh.write(b"z" * 1000)
        cursor = managed.scan_cursor(cursor, self.path, cap=300)
        self.assertEqual(cursor["file_epoch"], epoch + 1)

    def test_missing_transcript_cannot_remain_caught_up(self):
        append_rows(self.path, [usage_row(10)])
        cursor = managed.initial_scan(managed.default_cursor(), self.path)
        self.assertTrue(cursor["caught_up"])
        os.unlink(self.path)
        self.assertFalse(managed.scan_cursor(cursor, self.path)["caught_up"])


class StateMachineTests(unittest.TestCase):
    def setUp(self):
        self.cfg = managed.load_config(
            base=dict(cm._DEFAULTS, state_dir="/tmp/x"), environ={})
        self.gen = {"file_epoch": 1, "last_boundary_offset": None,
                    "last_boundary_sha256": None}
        self.attempt = managed.new_attempt("a" * 16, self.gen, 7, 100,
                                           "boot-a")

    def step(self, state, event, now=100, packet=None, gen=None, cursor=None):
        self.attempt["state"] = state
        return managed.transition_attempt(
            self.attempt, event, now, self.cfg, packet,
            self.gen if gen is None else gen, cursor=cursor)

    def test_happy_transitions(self):
        prepared = self.step("TRIGGERED", "prepared")
        typed = managed.transition_attempt(prepared, "typed_verified", 101,
                                           self.cfg, generation_value=self.gen)
        submitted = managed.transition_attempt(typed, "submitted", 102,
                                               self.cfg, generation_value=self.gen)
        own = {"seq": 1, "custom_instructions":
               "[cm-%s] ok" % self.attempt["nonce"]}
        acked = managed.transition_attempt(submitted, "timer", 103, self.cfg,
                                            own, self.gen)
        complete = managed.transition_attempt(
            acked, "timer", 104, self.cfg, own,
            dict(self.gen, file_epoch=2))
        self.assertEqual([prepared["state"], typed["state"],
                          submitted["state"], acked["state"],
                          complete["state"]],
                         ["PREPARED", "TYPED_VERIFIED", "SUBMITTED",
                          "ACKED", "BOUNDARY_CONFIRMED"])

    def test_submission_uncertain(self):
        out = self.step("TYPED_VERIFIED", "submission_uncertain")
        self.assertEqual(out["state"], "SUBMISSION_UNCERTAIN")

    def test_fast_completion_carries_own_proof(self):
        # OWN packet and boundary land inside ONE poll: SUBMITTED jumps
        # straight to BOUNDARY_CONFIRMED with no ACKED journal record,
        # so the record itself must carry the proof the confirmed
        # compaction was ours (Sol round-1 blocker: the fired-compacts
        # counter read 0 for a normal fast success).
        own = {"seq": 1, "custom_instructions":
               "[cm-%s] ok" % self.attempt["nonce"]}
        out = self.step("SUBMITTED", "timer", now=103, packet=own,
                        gen=dict(self.gen, file_epoch=2))
        self.assertEqual(out["state"], "BOUNDARY_CONFIRMED")
        self.assertIs(out.get("own_packet_proof"), True)

    def test_uncertain_submission_fast_boundary_carries_own_proof(self):
        # SUBMISSION_UNCERTAIN also reaches the generation branch: an
        # own packet plus boundary inside one poll proves the uncertain
        # submission actually fired.
        own = {"seq": 1, "custom_instructions":
               "[cm-%s] ok" % self.attempt["nonce"]}
        out = self.step("SUBMISSION_UNCERTAIN", "timer", now=103,
                        packet=own, gen=dict(self.gen, file_epoch=2))
        self.assertEqual(out["state"], "BOUNDARY_CONFIRMED")
        self.assertIs(out.get("own_packet_proof"), True)

    def test_foreign_fast_boundary_carries_no_own_proof(self):
        # A native compaction resolving a deferred attempt is NOT the
        # manager's compact — no own-proof may be fabricated.
        foreign = {"seq": 99, "trigger": "auto", "custom_instructions": ""}
        out = self.step("DEFERRED", "timer", now=103, packet=foreign,
                        gen=dict(self.gen, file_epoch=2))
        self.assertEqual(out["state"], "BOUNDARY_CONFIRMED")
        self.assertFalse(out.get("own_packet_proof"))

    def test_own_nonce_ignores_sequence_floor(self):
        self.attempt["state"] = "SUBMISSION_UNCERTAIN"
        packet = {"seq": 0, "custom_instructions":
                  "[cm-%s] text" % self.attempt["nonce"]}
        out = managed.transition_attempt(self.attempt, "timer", 100,
                                         self.cfg, packet, self.gen)
        self.assertEqual(out["state"], "ACKED")

    def test_journal_never_launders_hostile_proof_values(self):
        # bool("false") is True; a hostile journal value replayed
        # through recovery must not upgrade into real proof (Sol
        # round-3). Only the exact True survives serialization.
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = os.path.join(tmp.name, "j.jsonl")
        managed.journal_record(path, "CLEANUP_REQUIRED",
                               {"attempt_id": "a", "nonces": [],
                                "own_packet_proof": "false"})
        managed.journal_record(path, "CLEANUP_REQUIRED",
                               {"attempt_id": "b", "nonces": [],
                                "own_packet_proof": True})
        records = managed.read_journal(path)
        self.assertIs(records[0]["own_packet_proof"], False)
        self.assertIs(records[1]["own_packet_proof"], True)

    def test_stamp_recovered_proof(self):
        # Recovery-time classification: a crashed retry's terminal
        # mapping keeps the first submission's own-packet proof; a
        # foreign packet stamps nothing; hostile journal-derived
        # fields never raise (Sol round-3).
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        cfg = dict(cm._DEFAULTS, state_dir=tmp.name)
        sid = "sid-1"
        packet_file = managed.packet_path(cfg, sid)
        cm._private_makedirs(os.path.dirname(packet_file))
        with open(packet_file, "w") as fh:
            json.dump({"seq": 3, "custom_instructions": "[cm-n1] x"}, fh)
        recovered = {"state": "CLEANUP_REQUIRED", "nonces": ["n1", "n2"],
                     "attempt_packet_seq_floor": 5}
        managed._stamp_recovered_proof(recovered, cfg, sid)
        self.assertIs(recovered.get("own_packet_proof"), True)
        foreign = {"state": "CLEANUP_REQUIRED", "nonces": ["zz"],
                   "attempt_packet_seq_floor": 0}
        managed._stamp_recovered_proof(foreign, cfg, sid)
        self.assertNotIn("own_packet_proof", foreign)
        hostile = {"state": "CLEANUP_REQUIRED", "nonces": ["zz"],
                   "attempt_packet_seq_floor": "not-an-int"}
        managed._stamp_recovered_proof(hostile, cfg, sid)  # must not raise
        self.assertNotIn("own_packet_proof", hostile)
        managed._stamp_recovered_proof(None, cfg, sid)  # must not raise

    def test_floor_immutable_across_single_retry(self):
        submitted = self.step("TRIGGERED", "submitted")
        floor = submitted["attempt_packet_seq_floor"]
        first_nonce = submitted["nonce"]
        out = managed.transition_attempt(
            submitted, "timer", submitted["timers"]["ack_deadline_mono"],
            self.cfg, None, self.gen)
        self.assertEqual(out["state"], "TRIGGERED")
        self.assertEqual(out["retry_n"], 1)
        self.assertEqual(out["attempt_packet_seq_floor"], floor)
        self.assertNotEqual(out["nonce"], first_nonce)
        self.assertIn(first_nonce, out["nonces"])

    def test_second_ack_miss_safety_latches(self):
        submitted = self.step("TRIGGERED", "submitted")
        submitted["retry_n"] = 1
        out = managed.transition_attempt(
            submitted, "timer", submitted["timers"]["ack_deadline_mono"],
            self.cfg, None, self.gen)
        self.assertEqual((out["state"], out["latch_kind"]),
                         ("LATCHED", "SAFETY"))

    def test_acked_boundary_miss_safety_latches(self):
        self.attempt.update(state="ACKED", timers={
            "completion_deadline_mono": 110, "boot_id": "boot-a"})
        out = managed.transition_attempt(self.attempt, "timer", 110,
                                         self.cfg, None, self.gen)
        self.assertEqual(out["latch_kind"], "SAFETY")
        self.assertEqual(out["reason"], "missing_boundary")

    def test_acked_ignores_overwriting_foreign_packet(self):
        self.attempt.update(state="ACKED", timers={
            "completion_deadline_mono": 200, "boot_id": "boot-a"})
        packet = {"seq": 99, "trigger": "auto",
                  "custom_instructions": ""}
        out = managed.transition_attempt(self.attempt, "timer", 110,
                                         self.cfg, packet, self.gen)
        self.assertEqual(out["state"], "ACKED")

    def test_foreign_before_r4_defers(self):
        packet = {"seq": 8, "custom_instructions": "human"}
        out = self.step("TRIGGERED", "timer", packet=packet)
        self.assertEqual((out["state"], out["reason"]),
                         ("DEFERRED", "foreign_packet"))

    def test_foreign_deadline_is_not_extended_each_tick(self):
        packet = {"seq": 8, "custom_instructions": "human"}
        first = self.step("TRIGGERED", "timer", now=100, packet=packet)
        second = managed.transition_attempt(first, "timer", 120, self.cfg,
                                            packet, self.gen)
        self.assertEqual(second["timers"]["foreign_deadline_mono"],
                         first["timers"]["foreign_deadline_mono"])

    def test_completed_packet_does_not_reclassify_deferred_foreign(self):
        # A leftover packet whose compaction already completed (cursor
        # boundary count past its base) must not flip a deferred attempt
        # to foreign_packet: the retry gate excludes foreign_packet and
        # the reason change is never journaled, so a finished
        # auto-compact's packet silently wedged a live watcher forever
        # (fbeb0bf1: 590k tokens, idle pane, no compact, no alert).
        self.attempt["reason"] = "R3_changed"
        packet = {"seq": 7, "trigger": "auto", "base_compaction_count": 2,
                  "custom_instructions": ""}
        out = self.step("DEFERRED", "timer", packet=packet,
                        cursor={"boundary_count": 3})
        self.assertEqual((out["state"], out["reason"]),
                         ("DEFERRED", "R3_changed"))

    def test_unconfirmed_auto_packet_still_foreign(self):
        # Boundary count equal to the base means the packet's compaction
        # has NOT completed — the in-flight protection must hold.
        packet = {"seq": 7, "trigger": "auto", "base_compaction_count": 3,
                  "custom_instructions": ""}
        out = self.step("DEFERRED", "timer", packet=packet,
                        cursor={"boundary_count": 3})
        self.assertEqual((out["state"], out["reason"]),
                         ("DEFERRED", "foreign_packet"))

    def test_own_packet_acks_even_when_boundary_confirmed(self):
        # Staleness must never outrank OWN: the fired-compact proof
        # (cm_compacts) depends on the own-nonce classification.
        packet = {"seq": 1, "base_compaction_count": 0,
                  "custom_instructions":
                      "[cm-%s] ok" % self.attempt["nonce"]}
        out = self.step("SUBMITTED", "timer", packet=packet,
                        cursor={"boundary_count": 5})
        self.assertEqual(out["state"], "ACKED")

    def test_foreign_deadline_latches_while_packet_persists(self):
        # The FOREIGN branch returns before the timer branch, so the
        # deadline must fire inside it: a foreign packet that never
        # resolves (failed compact, epoch-crossed base count) would
        # otherwise defer forever with no alert.
        packet = {"seq": 8, "custom_instructions": "human"}
        first = self.step("TRIGGERED", "timer", now=100, packet=packet)
        self.assertEqual((first["state"], first["reason"]),
                         ("DEFERRED", "foreign_packet"))
        deadline = first["timers"]["foreign_deadline_mono"]
        out = managed.transition_attempt(first, "timer", deadline,
                                         self.cfg, packet, self.gen)
        self.assertEqual((out["state"], out["latch_kind"], out["reason"]),
                         ("LATCHED", "SAFETY", "foreign_uncertain"))

    def test_foreign_post_r4_requires_cleanup(self):
        packet = {"seq": 8, "custom_instructions": "human"}
        out = self.step("PREPARED", "timer", packet=packet)
        self.assertEqual(out["state"], "CLEANUP_REQUIRED")

    def test_threshold_latch_rearms_but_safety_does_not(self):
        threshold = self.step("TRIGGERED", "threshold_latch")
        threshold = managed.transition_attempt(
            threshold, "pct_rearmed", 101, self.cfg,
            generation_value=self.gen)
        safety = self.step("TRIGGERED", "safety_latch")
        safety = managed.transition_attempt(
            safety, "pct_rearmed", 101, self.cfg,
            generation_value=self.gen)
        self.assertEqual(threshold["state"], "READY")
        self.assertEqual(safety["state"], "LATCHED")

    def test_cleanup_does_not_auto_ack(self):
        self.attempt["state"] = "CLEANUP_REQUIRED"
        packet = {"seq": 99, "custom_instructions":
                  "[cm-%s] late" % self.attempt["nonce"]}
        out = managed.transition_attempt(self.attempt, "timer", 101,
                                         self.cfg, packet, self.gen)
        self.assertEqual(out["state"], "CLEANUP_REQUIRED")

    def test_cleanup_does_not_clear_on_generation_advance(self):
        self.attempt["state"] = "CLEANUP_REQUIRED"
        out = managed.transition_attempt(
            self.attempt, "timer", 101, self.cfg, None,
            dict(self.gen, file_epoch=2))
        self.assertEqual(out["state"], "CLEANUP_REQUIRED")


class JournalRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = os.path.join(self.temp.name, "j.jsonl")
        self.gen = {"file_epoch": 0, "last_boundary_offset": None,
                    "last_boundary_sha256": None}

    def record(self, state, boot="boot-a"):
        attempt = {"attempt_id": "id", "run_token": "tok",
                   "nonce": "1" * 16, "nonces": ["1" * 16],
                   "generation": self.gen, "retry_n": 0,
                   "attempt_packet_seq_floor": 3,
                   "timers": {"boot_id": boot, "ack_deadline_mono": 99}}
        managed.journal_record(self.path, state, attempt, now_wall=1)

    def test_lifecycle_record_is_skipped(self):
        self.record("SUBMISSION_UNCERTAIN")
        self.record("WATCHER_READY")
        self.assertEqual(managed.attempt_tail(self.path)["state"],
                         "SUBMISSION_UNCERTAIN")

    def test_torn_tail_ignored(self):
        self.record("SUBMITTED")
        with open(self.path, "ab") as fh:
            fh.write(b'{"schema":1,"state":"ACKED"')
        self.assertEqual(managed.attempt_tail(self.path)["state"], "SUBMITTED")

    def test_recovery_mapping_for_every_tail(self):
        expected = {
            "READY": "READY", "TRIGGERED": "TRIGGERED",
            "PREPARED": "CLEANUP_REQUIRED",
            "TYPED_VERIFIED": "SUBMISSION_UNCERTAIN",
            "SUBMITTED": "SUBMITTED", "ACKED": "ACKED",
            "BOUNDARY_CONFIRMED": "BOUNDARY_CONFIRMED",
            "DEFERRED": "DEFERRED", "LATCHED": "LATCHED",
            "SUBMISSION_UNCERTAIN": "SUBMISSION_UNCERTAIN",
            "CLEANUP_REQUIRED": "CLEANUP_REQUIRED",
        }
        for source, target in expected.items():
            with self.subTest(source=source):
                try:
                    os.unlink(self.path)
                except OSError:
                    pass
                self.record(source)
                self.assertEqual(managed.recover_attempt(
                    self.path, "boot-a")["state"], target)

    def test_cross_boot_submitted_safety_latches(self):
        self.record("SUBMITTED", boot="old-boot")
        out = managed.recover_attempt(self.path, "new-boot")
        self.assertEqual((out["state"], out["latch_kind"]),
                         ("LATCHED", "SAFETY"))

    def test_cross_boot_deferred_safety_latches(self):
        self.record("DEFERRED", boot="old-boot")
        out = managed.recover_attempt(self.path, "new-boot")
        self.assertEqual((out["state"], out["latch_kind"]),
                         ("LATCHED", "SAFETY"))

    def test_torn_tail_is_delimited_before_next_append(self):
        # A mid-loop write failure leaves a non-newline tail; the NEXT
        # append must not be swallowed into the same unparseable line.
        with open(self.path, "wb") as fh:
            fh.write(b'{"schema": 1, "state": "TRUNC')
        managed.journal_record(self.path, "LATCHED",
                               {"run_token": "t" * 16,
                                "latch_kind": "SAFETY"},
                               reason="operator_resolved")
        records = managed.read_journal(self.path)
        self.assertEqual(records[-1]["state"], "LATCHED")
        self.assertEqual(records[-1]["latch_kind"], "SAFETY")

    def test_unknown_boot_id_is_unproven_and_latches(self):
        # "unknown" == "unknown" must NOT count as proven same-boot: a
        # stale monotonic deadline would fire immediately post-reboot.
        self.record("SUBMITTED", boot="unknown")
        out = managed.recover_attempt(self.path, "unknown")
        self.assertEqual((out["state"], out["latch_kind"], out["reason"]),
                         ("LATCHED", "SAFETY", "unproven_boot_timer"))


class RequestConfigInstructionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.request = os.path.join(self.temp.name, "request.json")

    def write_request(self, value):
        with open(self.request, "w") as fh:
            json.dump(value, fh)

    def test_request_exact_schema_and_fingerprint(self):
        self.write_request({"request_id": "request-123", "reason": "done"})
        value = managed.validate_request(self.request, time.time())
        self.assertEqual(value["request_id"], "request-123")
        gen = {"file_epoch": 2, "last_boundary_offset": None,
               "last_boundary_sha256": None}
        self.assertEqual(managed.request_fingerprint(gen, "request-123"),
                         {"generation": gen, "request_id": "request-123"})

    def test_request_rejects_extra_key_bad_id_and_stale(self):
        for value in ({"request_id": "request-123", "extra": 1},
                      {"request_id": "short"},
                      {"request_id": "request-123", "reason": 1}):
            with self.subTest(value=value):
                self.write_request(value)
                self.assertIsNone(managed.validate_request(self.request,
                                                           time.time()))
        self.write_request({"request_id": "request-123"})
        old = time.time() - 700
        os.utime(self.request, (old, old))
        self.assertIsNone(managed.validate_request(self.request, time.time()))

    def test_pending_request_survives_file_expiry(self):
        # Staleness is judged at first observation only: once journaled,
        # the request stays actionable for its generation even after the
        # file ages past the mtime bound or disappears.
        gen = managed.generation_key(
            managed.generation(managed.default_cursor()))
        history = {"request-123": gen}
        self.assertEqual(
            managed.pending_request_id(history, set(), gen), "request-123")
        self.assertIsNone(managed.pending_request_id(history, {gen}, gen))
        self.assertIsNone(managed.pending_request_id(history, set(), "other"))

    def test_pending_request_deterministic_across_id_churn(self):
        history = {"zzz-request": "g", "aaa-request": "g"}
        self.assertEqual(
            managed.pending_request_id(history, set(), "g"), "aaa-request")

    def test_request_rejects_symlink_and_oversize(self):
        target = os.path.join(self.temp.name, "target")
        with open(target, "w") as fh:
            json.dump({"request_id": "request-123"}, fh)
        os.symlink(target, self.request)
        self.assertIsNone(managed.validate_request(self.request, time.time()))
        os.unlink(self.request)
        with open(self.request, "w") as fh:
            fh.write("x" * 5000)
        self.assertIsNone(managed.validate_request(self.request, time.time()))

    def test_config_defaults_and_clamps(self):
        base = dict(cm._DEFAULTS, soft_pct=0.7, hard_pct=0.8,
                    state_dir=self.temp.name)
        env = {"COMPACT_MANAGER_MANAGED_TRIGGER_PCT": "0.6",
               "COMPACT_MANAGER_MANAGED_STABLE_MS": "2",
               "COMPACT_MANAGER_MANAGED_POLL_S": "1",
               "COMPACT_MANAGER_MANAGED_ACK_TIMEOUT_S": "4",
               "COMPACT_MANAGER_MANAGED_COMPLETION_TIMEOUT_S": "5",
               "COMPACT_MANAGER_MANAGED_PANE_COMMANDS": '["claude","cc"]'}
        cfg = managed.load_config(base=base, environ=env)
        self.assertEqual(cfg["managed_trigger_pct"], 0.8)
        self.assertEqual(cfg["managed_stable_ms"], 200)
        self.assertEqual(cfg["managed_poll_s"], 5)
        self.assertEqual(cfg["managed_ack_timeout_s"], 30)
        self.assertEqual(cfg["managed_completion_timeout_s"], 30)
        self.assertEqual(cfg["managed_pane_commands"], ["claude", "cc"])

    def test_instruction_is_fixed_and_metacharacter_free(self):
        text = managed.instruction_text("a" * 16)
        self.assertEqual(text, "/compact [cm-%s] Preserve the task list and "
                         "open decisions to the handoff file" % ("a" * 16))
        self.assertIn("[", text)  # pinned literal glob characters
        self.assertIn("]", text)
        for char in managed.INSTRUCTION_DENYLIST:
            self.assertNotIn(char, text, repr(char))

    def test_instruction_shortens_when_pane_cannot_render_unwrapped(self):
        # R5/R6' verify the single bottom-anchored composer line, so an
        # instruction that soft-wraps can never verify: every attempt in
        # a narrow pane ended CLEANUP_REQUIRED/R5_composer_mismatch
        # (observed live at 76 cols, fbeb0bf1). Narrow panes get the
        # nonce-only form; wide panes keep the guidance tail.
        nonce = "a" * 16
        full = managed.instruction_text(nonce)
        self.assertEqual(managed.instruction_text(nonce, width=200), full)
        short = managed.instruction_text(nonce, width=76)
        self.assertEqual(short, "/compact [cm-%s]" % nonce)
        self.assertLessEqual(len("❯ " + short),
                             76 - managed.WRAP_MARGIN_COLS)
        for char in managed.INSTRUCTION_DENYLIST:
            self.assertNotIn(char, short, repr(char))
        # exact boundary: the widest pane that still forces the short
        # form, and the narrowest that fits the full form
        fits = len("❯ " + full) + managed.WRAP_MARGIN_COLS
        self.assertEqual(managed.instruction_text(nonce, width=fits), full)
        self.assertEqual(managed.instruction_text(nonce, width=fits - 1),
                         short)

    def test_instruction_guard_rejects_each_denylisted_character(self):
        original = managed.INSTRUCTION_TEMPLATE
        try:
            for char in managed.INSTRUCTION_DENYLIST:
                with self.subTest(char=repr(char)):
                    managed.INSTRUCTION_TEMPLATE = original + char
                    with self.assertRaises(ValueError):
                        managed.instruction_text("a" * 16)
        finally:
            managed.INSTRUCTION_TEMPLATE = original


class ResolveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.cfg = dict(cm._DEFAULTS, state_dir=self.temp.name)
        self.sid = "session-1234"
        self.paths = managed.managed_paths(self.cfg, self.sid, "sock", "%1")
        self.token = "a" * 16
        self.attempt = {
            "state": "CLEANUP_REQUIRED", "attempt_id": "attempt",
            "run_token": self.token, "nonce": "b" * 16,
            "nonces": ["b" * 16], "generation": managed.generation(
                managed.default_cursor()), "retry_n": 0,
            "attempt_packet_seq_floor": 1,
            "timers": {"boot_id": "boot"}}
        managed.journal_record(self.paths["journal"], "CLEANUP_REQUIRED",
                               self.attempt, reason="test")
        managed.save_cursor(self.paths["scan"], managed.default_cursor())

    def leases(self, pid, start, heartbeat):
        value = {"run_token": self.token, "pid": pid, "proc_start": start,
                 "heartbeat_at": heartbeat}
        managed._atomic_json(self.paths["session_lease"], value)
        managed._atomic_json(self.paths["pane_lease"], value)

    def test_resolves_dead_token_when_both_leases_reclaimable(self):
        self.leases(99999999, 1, time.time() - 1000)
        ok, message = managed.resolve_session(self.cfg, self.sid)
        self.assertTrue(ok, message)
        tail = managed.attempt_tail(self.paths["journal"])
        self.assertEqual((tail["state"], tail["latch_kind"], tail["reason"]),
                         ("LATCHED", "SAFETY", "operator_resolved"))

    def test_crashed_prepared_tail_is_visible_and_resolvable(self):
        # A crashed watcher leaves a raw PREPARED tail; status and
        # resolve must judge it through the conservative recovery
        # mapping (CLEANUP_REQUIRED), not the raw state.
        managed.journal_record(self.paths["journal"], "PREPARED",
                               dict(self.attempt, state="PREPARED"))
        rows = managed.status_rows(self.cfg)
        mine = [r for r in rows if r["session_id"] == self.sid]
        self.assertEqual(mine[0]["state"], "CLEANUP_REQUIRED")
        ok, message = managed.resolve_session(self.cfg, self.sid)
        self.assertTrue(ok, message)

    def test_live_resolve_requires_same_token_on_both_leases(self):
        live = managed.proc_stat(os.getpid())
        self.leases(os.getpid(), live["starttime"], time.time())
        pane, _ = managed.read_json_inode(self.paths["pane_lease"])
        pane["run_token"] = "successor"
        managed._atomic_json(self.paths["pane_lease"], pane)
        ok, message = managed.resolve_session(self.cfg, self.sid)
        self.assertFalse(ok)
        self.assertIn("both leases", message)


class LadderPredicateTests(unittest.TestCase):
    def test_idle_requires_nbsp_signature(self):
        self.assertTrue(managed.composer_idle("header\n❯\u00a0\nfooter"))
        self.assertFalse(managed.composer_idle("header\n❯ \nfooter"))
        self.assertFalse(managed.composer_idle("header\n❯\nfooter"))

    def test_busy_and_half_typed_fail(self):
        self.assertFalse(managed.composer_idle("❯\u00a0\nesc to interrupt"))
        self.assertFalse(managed.composer_idle("❯\u00a0rm -rf /tmp/x"))

    def test_exact_composer(self):
        text = managed.instruction_text("a" * 16)
        self.assertTrue(managed.snapshot_exact(
            managed.parse_pane("x\n❯\u00a0" + text), text))
        self.assertFalse(managed.snapshot_exact(
            managed.parse_pane("x\n❯\u00a0BAD" + text), text))
        self.assertFalse(managed.snapshot_exact(
            managed.parse_pane("x\n❯\u00a0" + text + "x"), text))
        # A modal anywhere in the layout vetoes even an exact match.
        self.assertFalse(managed.snapshot_exact(
            managed.parse_pane(" ❯ 1. Yes\n❯\u00a0" + text), text))
        self.assertFalse(managed.snapshot_exact(None, text))

    def test_trailing_capture_padding_is_ignored(self):
        # capture-pane -J preserves trailing spaces (live-gate catch):
        # the padded composer is still idle / still exact, while a
        # shell's "❯ "+padding (no NBSP) is still rejected.
        self.assertTrue(managed.composer_idle("❯ " + " " * 40))
        self.assertFalse(managed.composer_idle("❯" + " " * 40))
        text = managed.instruction_text("a" * 16)
        self.assertTrue(managed.snapshot_exact(managed.parse_pane(
            "x\n❯ " + text + " " * 30), text))
        self.assertFalse(managed.snapshot_exact(managed.parse_pane(
            "x\n❯ user " + text + " " * 30), text))

    def test_s6_capture_half_typed_predicate(self):
        capture_path = os.path.abspath(os.path.join(
            HERE, "..", "..", "..", "tools", "s6", "captures",
            "s6-results.jsonl"))
        with open(capture_path) as fh:
            rows = [json.loads(line) for line in fh]
        row = next(x for x in rows if x.get("scenario") == "f_half_typed")
        self.assertFalse(managed.composer_idle(row["composer_after"]))


IDLE = "❯ "


class LadderTmux:
    """Scripted tmux seam: fixed pane facts, queued captures, recorded
    send-keys. The queue's last capture repeats once exhausted."""

    def __init__(self, captures, send_rc=0, enter_rc=0):
        self.captures = list(captures)
        self.sent = []
        self.send_rc, self.enter_rc = send_rc, enter_rc
        self.display_rc = 0
        self.facts = "%1\t/dev/pts/7\t10\tclaude\t0\t120\t40\t$1\n"

    def __call__(self, argv, timeout=5):
        argv = list(argv)
        cmd = argv[2] if argv[:1] == ["-S"] else argv[0]
        if cmd == "display-message":
            return Result(self.facts, self.display_rc)
        if cmd == "capture-pane":
            cap = (self.captures.pop(0) if len(self.captures) > 1
                   else self.captures[0])
            return Result(cap)
        if cmd == "send-keys":
            self.sent.append(argv)
            rc = self.enter_rc if argv[-1] == "Enter" else self.send_rc
            return Result("", rc)
        return Result("")

    def enters(self):
        return [a for a in self.sent if a[-1] == "Enter"]


class EchoTmux:
    """Fake tmux whose pane echoes typed text like a real composer:
    idle until send-keys -l, then IDLE+text, cleared again on Enter."""

    def __init__(self):
        self.sent = []
        self.typed = ""
        self.display_rc = 0
        self.facts = "%1\t/dev/pts/7\t10\tclaude\t0\t120\t40\t$1\n"

    def __call__(self, argv, timeout=5):
        argv = list(argv)
        cmd = argv[2] if argv[:1] == ["-S"] else argv[0]
        if cmd == "display-message":
            return Result(self.facts, self.display_rc)
        if cmd == "capture-pane":
            return Result(IDLE + self.typed)
        if cmd == "send-keys":
            self.sent.append(argv)
            if argv[-1] == "Enter":
                self.typed = ""
            elif "-l" in argv:
                self.typed += argv[-1]
            return Result("")
        return Result("")


class RunLadderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.proc = os.path.join(self.temp.name, "proc")
        write_proc(self.proc, 10, 20, 1010)
        write_proc(self.proc, 20, 20, 2020)
        # Fixtures in this class are written against a 200k window
        # (the shipped default is 1M).
        self.cfg = managed.load_config(
            base=dict(cm._DEFAULTS, state_dir=self.temp.name,
                      context_window=200_000), environ={})
        self.sid = "session-1234"
        self.token = "a" * 16
        self.paths = managed.managed_paths(self.cfg, self.sid, "sock", "%1")
        lease = {"run_token": self.token, "pid": 20, "proc_start": 2020,
                 "heartbeat_at": time.time()}
        managed._atomic_json(self.paths["session_lease"], lease)
        managed._atomic_json(self.paths["pane_lease"], lease)
        self.transcript = os.path.join(self.temp.name, "t.jsonl")
        append_rows(self.transcript, [usage_row(50)])
        self.cursor = managed.initial_scan(managed.default_cursor(),
                                           self.transcript)
        self.binding = {"socket": "sock", "pane_id": "%1",
                        "pane_tty": "/dev/pts/7", "pane_root_pid": 10,
                        "pane_root_start": 1010, "claude_pid": 20,
                        "claude_start": 2020, "session_id": self.sid,
                        "transcript_path": self.transcript,
                        "tmux_session_id": "$1", "run_token": self.token,
                        "attended": True}
        self.attempt = managed.new_attempt(
            self.token, managed.generation(self.cursor), 0, 0.0, "boot")
        self.text = managed.instruction_text(self.attempt["nonce"])

    def run_ladder(self, tmux, packet=None):
        return managed.run_ladder(
            self.binding, self.cfg, self.paths, self.attempt, self.cursor,
            self.paths["journal"], lambda: packet, tmux, self.proc,
            wait=lambda seconds: None, now_mono=lambda: 0.0)

    def test_happy_path_submits(self):
        tmux = LadderTmux([IDLE, IDLE, IDLE + self.text, IDLE + self.text,
                           IDLE])
        out = self.run_ladder(tmux)
        self.assertEqual(out["state"], "SUBMITTED")
        self.assertEqual(len(tmux.enters()), 1)
        self.assertTrue(any(a[-1] == self.text for a in tmux.sent))

    def test_r5_mismatch_is_cleanup_and_never_enters(self):
        tmux = LadderTmux([IDLE, IDLE, IDLE + "x" + self.text])
        out = self.run_ladder(tmux)
        self.assertEqual(out["state"], "CLEANUP_REQUIRED")
        self.assertEqual(out["reason"], "R5_composer_mismatch")
        self.assertEqual(tmux.enters(), [])

    def test_r6_foreign_packet_aborts_before_enter(self):
        tmux = LadderTmux([IDLE, IDLE, IDLE + self.text, IDLE + self.text])
        out = self.run_ladder(tmux, packet={"seq": 1})
        self.assertEqual(out["state"], "CLEANUP_REQUIRED")
        self.assertEqual(out["reason"], "R6_prime_foreign_packet")
        self.assertEqual(tmux.enters(), [])

    def test_r6_auto_packet_foreign_even_after_seq_reset(self):
        tmux = LadderTmux([IDLE, IDLE, IDLE + self.text, IDLE + self.text])
        out = self.run_ladder(tmux, packet={"seq": 0, "trigger": "auto"})
        self.assertEqual(out["state"], "CLEANUP_REQUIRED")
        self.assertEqual(out["reason"], "R6_prime_foreign_packet")
        self.assertEqual(tmux.enters(), [])

    def test_narrow_pane_types_short_instruction_and_submits(self):
        # 60-col pane: the full instruction would soft-wrap and fail R5
        # forever; the ladder must type the nonce-only form instead.
        short = "/compact [cm-%s]" % self.attempt["nonce"]
        tmux = LadderTmux([IDLE, IDLE, IDLE + short, IDLE + short, IDLE])
        tmux.facts = "%1\t/dev/pts/7\t10\tclaude\t0\t60\t40\t$1\n"
        out = self.run_ladder(tmux)
        self.assertEqual(out["state"], "SUBMITTED")
        self.assertEqual(len(tmux.enters()), 1)
        self.assertTrue(any(a[-1] == short for a in tmux.sent))
        self.assertFalse(any(a[-1] == self.text for a in tmux.sent))

    def test_r6_completed_compactions_packet_is_not_foreign(self):
        # A leftover packet from an already-completed compaction (fresh
        # cursor shows the boundary advanced past its base) must not
        # abort the ladder as foreign — that terminal CLEANUP_REQUIRED
        # was the second half of the fbeb0bf1 wedge.
        append_rows(self.transcript, [boundary_row(post=50), usage_row(60)])
        self.cursor = managed.initial_scan(managed.default_cursor(),
                                           self.transcript)
        self.attempt = managed.new_attempt(
            self.token, managed.generation(self.cursor), 1, 0.0, "boot")
        self.text = managed.instruction_text(self.attempt["nonce"])
        tmux = LadderTmux([IDLE, IDLE, IDLE + self.text, IDLE + self.text,
                           IDLE])
        out = self.run_ladder(tmux, packet={"seq": 1, "trigger": "auto",
                                            "base_compaction_count": 0})
        self.assertEqual(out["state"], "SUBMITTED")
        self.assertEqual(len(tmux.enters()), 1)

    def test_r6_own_late_packet_aborts_before_enter(self):
        tmux = LadderTmux([IDLE, IDLE, IDLE + self.text, IDLE + self.text])
        packet = {"seq": 7,
                  "custom_instructions": "[cm-%s]" % self.attempt["nonce"]}
        out = self.run_ladder(tmux, packet=packet)
        self.assertEqual(out["state"], "CLEANUP_REQUIRED")
        self.assertEqual(out["reason"], "R6_prime_own_packet_late")
        self.assertEqual(tmux.enters(), [])
        # The late own packet proves the first submission fired: the
        # terminal CLEANUP_REQUIRED record must carry the proof so the
        # fired-compacts counter keeps it (Sol round-2 major).
        self.assertIs(out.get("own_packet_proof"), True)
        tail = managed.read_journal(self.paths["journal"])[-1]
        self.assertEqual(tail["state"], "CLEANUP_REQUIRED")
        self.assertIs(tail["own_packet_proof"], True)

    def test_r6_own_proof_survives_competing_abort(self):
        # Binding/composer failures take precedence over the
        # own_packet_late detail — but the OWN packet in hand is still
        # proof the first submission fired, and CLEANUP_REQUIRED never
        # re-inspects the packet (Sol round-3).
        tmux = LadderTmux([IDLE, IDLE, IDLE + self.text, IDLE])
        packet = {"seq": 7,
                  "custom_instructions": "[cm-%s]" % self.attempt["nonce"]}
        out = self.run_ladder(tmux, packet=packet)
        self.assertEqual(out["state"], "CLEANUP_REQUIRED")
        self.assertEqual(out["reason"], "R6_prime_composer_mismatch")
        self.assertEqual(tmux.enters(), [])
        self.assertIs(out.get("own_packet_proof"), True)
        tail = managed.read_journal(self.paths["journal"])[-1]
        self.assertIs(tail["own_packet_proof"], True)

    def test_r6_reobserves_boundary_advance(self):
        tmux = LadderTmux([IDLE, IDLE, IDLE + self.text, IDLE + self.text])
        append_rows(self.transcript, [boundary_row(post=5)])
        out = self.run_ladder(tmux)
        self.assertEqual(out["state"], "CLEANUP_REQUIRED")
        self.assertEqual(out["reason"], "R6_prime_boundary_advanced")
        self.assertEqual(tmux.enters(), [])

    def test_enter_failure_is_submission_uncertain(self):
        tmux = LadderTmux([IDLE, IDLE, IDLE + self.text, IDLE + self.text],
                          enter_rc=1)
        out = self.run_ladder(tmux)
        self.assertEqual(out["state"], "SUBMISSION_UNCERTAIN")

    def test_composer_not_cleared_is_submission_uncertain(self):
        tmux = LadderTmux([IDLE, IDLE, IDLE + self.text, IDLE + self.text,
                           IDLE + self.text, IDLE + self.text])
        out = self.run_ladder(tmux)
        self.assertEqual(out["state"], "SUBMISSION_UNCERTAIN")
        self.assertEqual(out["reason"], "composer_not_cleared")

    def test_r6_incomplete_rescan_aborts_before_enter(self):
        # An explicitly incomplete rescan (trailing fragment appears
        # mid-ladder) must not authorize Enter.
        tmux = LadderTmux([IDLE, IDLE, IDLE + self.text, IDLE + self.text])
        with open(self.transcript, "ab") as fh:
            fh.write(b'{"torn": tr')
        out = self.run_ladder(tmux)
        self.assertEqual(out["state"], "CLEANUP_REQUIRED")
        self.assertEqual(out["reason"], "R6_prime_scan_incomplete")
        self.assertEqual(tmux.enters(), [])


class TickWiringTests(unittest.TestCase):
    """Watcher.tick() driven end-to-end through the injectable seams."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.proc = os.path.join(self.temp.name, "proc")
        write_proc(self.proc, 10, 20, 1010)
        write_proc(self.proc, 20, 20, 2020)
        self.cfg = managed.load_config(
            base=dict(cm._DEFAULTS, state_dir=self.temp.name,
                      context_window=200_000), environ={})
        self.sid = "session-1234"
        self.token = "a" * 16
        self.paths = managed.managed_paths(self.cfg, self.sid, "sock", "%1")
        lease = {"run_token": self.token, "pid": 20, "proc_start": 2020,
                 "heartbeat_at": time.time()}
        managed._atomic_json(self.paths["session_lease"], lease)
        managed._atomic_json(self.paths["pane_lease"], lease)
        self.transcript = os.path.join(self.temp.name, "t.jsonl")
        self.binding = {"socket": "sock", "pane_id": "%1",
                        "pane_tty": "/dev/pts/7", "pane_root_pid": 10,
                        "pane_root_start": 1010, "claude_pid": 20,
                        "claude_start": 2020, "session_id": self.sid,
                        "transcript_path": self.transcript,
                        "tmux_session_id": "$1", "run_token": self.token,
                        "attended": True}

    def make_watcher(self, rows, tmux, **kw):
        append_rows(self.transcript, rows)
        kw.setdefault("wait", lambda seconds: None)
        return managed.Watcher(self.binding, self.cfg, self.paths,
                               run_tmux=tmux, proc_root=self.proc, **kw)

    def test_operator_stop_journals_stop_requested(self):
        # A stop must record its actual cause, not the incidental
        # last-tick status ("below_threshold") — and never leave the
        # journal without a final retirement record. (This exit goes
        # through the catch-up loop: the cursor starts uncaught-up.)
        watcher = self.make_watcher([usage_row(50)], LadderTmux([IDLE]))
        watcher.stop_requested = True
        watcher.run()
        records = list(managed.read_journal(self.paths["journal"]))
        self.assertEqual(records[-1]["state"], "WATCHER_RETIRED")
        self.assertEqual(records[-1].get("reason"), "stop_requested")

    def test_operator_stop_mid_tick_journals_stop_requested(self):
        # The tick-loop exit: cursor already caught up, tick returns
        # keep=True ("below_threshold"), stop must override that
        # incidental reason — while keep=False keeps its true reason.
        watcher = self.make_watcher([usage_row(50)], LadderTmux([IDLE]))
        watcher.cursor["caught_up"] = True
        watcher.stop_requested = True
        watcher.run()
        records = list(managed.read_journal(self.paths["journal"]))
        self.assertEqual(records[-1]["state"], "WATCHER_RETIRED")
        self.assertEqual(records[-1].get("reason"), "stop_requested")

    def test_missing_transcript_is_pending_not_retire(self):
        # A virgin session's watcher waits for the first turn to create
        # the transcript; it must not retire (the wait is bounded by
        # the session's own life — validate_binding retires on death
        # and rotation).
        watcher = self.make_watcher([], LadderTmux([IDLE]))
        if os.path.exists(self.transcript):
            os.unlink(self.transcript)
        keep, reason = watcher.tick()
        self.assertEqual((keep, reason), (True, "transcript_pending"))

    def test_copy_mode_is_transient_not_retire(self):
        tmux = LadderTmux([IDLE])
        tmux.facts = "%1\t/dev/pts/7\t10\tclaude\t1\t120\t40\t$1\n"
        watcher = self.make_watcher([usage_row(50)], tmux)
        self.assertEqual(watcher.tick(), (True, "pane_in_mode"))

    def test_zombie_leader_retires_claude_dead(self):
        # Watchers have no time-based deadline, so an exited-but-
        # unreaped claude (proc state Z, pid entry lingering) must be
        # positively recognized as death — otherwise the watcher
        # babysits the corpse forever.
        for state in ("Z", "X", "x"):
            write_proc(self.proc, 20, 20, 2020, state=state)
            watcher = self.make_watcher([usage_row(50)], LadderTmux([IDLE]))
            watcher.cursor["caught_up"] = True
            keep, reason = watcher.tick()
            self.assertEqual((keep, reason), (False, "claude_dead"), state)

    def test_stopped_leader_keeps_watcher_alive(self):
        # T (SIGSTOP) is a paused session, not an ended one: the
        # watcher stays for as long as the session lives.
        write_proc(self.proc, 20, 20, 2020, state="T")
        watcher = self.make_watcher([usage_row(50)], LadderTmux([IDLE]))
        watcher.cursor["caught_up"] = True
        keep, reason = watcher.tick()
        self.assertEqual((keep, reason), (True, "below_threshold"))

    def test_retire_skips_stale_record_after_ownership_loss(self):
        # A successor already owns the leases and has journaled READY:
        # the loser's trailing WATCHER_RETIRED would supersede the
        # successor's lifecycle in status and flag a live watcher
        # DEAD-LEASE (Sol round-3 MEDIUM) — the fenced append must
        # skip it.
        watcher = self.make_watcher([usage_row(50)], LadderTmux([IDLE]))
        successor = {"run_token": "b" * 16, "pid": 20, "proc_start": 2020,
                     "heartbeat_at": time.time()}
        managed._atomic_json(self.paths["session_lease"], successor)
        managed._atomic_json(self.paths["pane_lease"], successor)
        managed.journal_record(self.paths["journal"], "WATCHER_READY",
                               {"run_token": "b" * 16})
        watcher._retire("lease_lost")
        records = list(managed.read_journal(self.paths["journal"]))
        self.assertEqual(records[-1]["state"], "WATCHER_READY")
        self.assertNotIn("WATCHER_RETIRED",
                         [r.get("state") for r in records])

    def test_retire_writes_when_ownership_unknowable(self):
        # Lock timeout: ownership can't be judged — prefer the durable
        # record (old behavior) over a clean exit reading READY +
        # DEAD-LEASE forever.
        watcher = self.make_watcher([usage_row(50)], LadderTmux([IDLE]))
        with managed.txn_lock(self.paths["txn"]):
            watcher._retire("stop_requested")
        records = list(managed.read_journal(self.paths["journal"]))
        self.assertEqual(records[-1]["state"], "WATCHER_RETIRED")
        self.assertEqual(records[-1].get("reason"), "stop_requested")

    def test_stop_with_typed_attempt_journals_hazard_first(self):
        # An operator stop while an attempt sits SUBMITTED (typed
        # bytes of unproven disposition, typed_critical only spans the
        # ladder call) must journal CLEANUP_REQUIRED before the
        # retirement record — a bare WATCHER_RETIRED tail would read
        # as a clean exit in status and mask the hazard (Sol round-2
        # HIGH).
        watcher = self.make_watcher([usage_row(50)], LadderTmux([IDLE]))
        watcher.attempt = {"state": "SUBMITTED", "run_token": self.token,
                           "attempt_id": "att1", "nonce": "n1",
                           "nonces": ["n1"], "generation": None,
                           "attempt_packet_seq_floor": 0, "timers": {}}
        watcher.stop_requested = True
        watcher.run()
        records = list(managed.read_journal(self.paths["journal"]))
        self.assertEqual(records[-1]["state"], "WATCHER_RETIRED")
        self.assertEqual(records[-1].get("reason"), "stop_requested")
        hazards = [r for r in records if r.get("state") == "CLEANUP_REQUIRED"
                   and r.get("attempt_id") == "att1"]
        self.assertTrue(hazards)  # journaled before the retirement

    def test_pane_hiccup_with_live_claude_waits(self):
        tmux = LadderTmux([IDLE])
        tmux.display_rc = 1
        watcher = self.make_watcher([usage_row(50)], tmux)
        self.assertEqual(watcher.tick(), (True, "pane_missing"))

    def test_pane_missing_with_dead_claude_retires(self):
        tmux = LadderTmux([IDLE])
        tmux.display_rc = 1
        watcher = self.make_watcher([usage_row(50)], tmux)
        shutil.rmtree(os.path.join(self.proc, "20"))
        keep, reason = watcher.tick()
        self.assertEqual((keep, reason), (False, "pane_missing"))

    def test_pane_missing_with_zombie_claude_retires(self):
        # An exited-but-unreaped claude must not sustain a transient wait.
        tmux = LadderTmux([IDLE])
        tmux.display_rc = 1
        watcher = self.make_watcher([usage_row(50)], tmux)
        write_proc(self.proc, 20, 20, 2020, state="Z")
        keep, reason = watcher.tick()
        self.assertEqual((keep, reason), (False, "pane_missing"))

    def test_transient_still_heartbeats(self):
        # Copy mode lasting past the cadence must not starve the leases.
        tmux = LadderTmux([IDLE])
        tmux.facts = "%1\t/dev/pts/7\t10\tclaude\t1\t120\t40\t$1\n"
        watcher = self.make_watcher([usage_row(50)], tmux)
        watcher.next_heartbeat = 0.0
        before, _ = managed.read_json_inode(self.paths["session_lease"])
        keep, reason = watcher.tick()
        self.assertEqual((keep, reason), (True, "pane_in_mode"))
        after, _ = managed.read_json_inode(self.paths["session_lease"])
        self.assertGreater(after["heartbeat_at"], before["heartbeat_at"])

    def test_catchup_retire_journals_typed_hazard(self):
        # A recovered SUBMITTED attempt must not vanish silently when the
        # catch-up loop retires on a non-transient failure (tty change).
        tmux = LadderTmux([IDLE])
        tmux.facts = "%1\t/dev/pts/9\t10\tclaude\t0\t120\t40\t$1\n"
        watcher = self.make_watcher([usage_row(50)], tmux)
        watcher.attempt = managed.new_attempt(
            self.token, managed.generation(managed.default_cursor()),
            0, 0.0, "boot")
        watcher.attempt["state"] = "SUBMITTED"
        watcher.run()
        records = list(managed.read_journal(self.paths["journal"]))
        states = [r["state"] for r in records]
        self.assertIn("CLEANUP_REQUIRED", states)
        # The catch-up exit must also leave a final retirement record
        # with the failure as its reason — without it, status reads
        # WATCHER_READY + DEAD-LEASE for a watcher that left cleanly.
        self.assertEqual(records[-1]["state"], "WATCHER_RETIRED")
        self.assertEqual(records[-1].get("reason"), "tty_changed")

    def test_stale_auto_packet_does_not_wedge_deferred_attempt(self):
        # End-to-end wedge regression (fbeb0bf1): an auto-compact's
        # leftover packet, boundary long confirmed, plus a deferred
        # attempt whose retry is due. The tick must retry and submit —
        # not reclassify the attempt DEFERRED/foreign_packet and skip
        # the retry gate forever.
        rows = [usage_row(100), boundary_row(post=100), usage_row(170000)]
        tmux = EchoTmux()
        watcher = self.make_watcher(rows, tmux)
        cursor = managed.initial_scan(managed.default_cursor(),
                                      self.transcript)
        watcher.attempt = managed.new_attempt(
            self.token, managed.generation(cursor), 4, 0.0, "boot")
        watcher.attempt.update(state="DEFERRED", reason="R3_changed",
                               defer_class="opportunity")
        watcher.attempt["timers"]["next_attempt_at"] = 0.0
        packet_file = managed.packet_path(self.cfg, self.sid)
        cm._private_makedirs(os.path.dirname(packet_file))
        with open(packet_file, "w") as fh:
            json.dump({"seq": 4, "trigger": "auto",
                       "base_compaction_count": 0,
                       "custom_instructions": ""}, fh)
        keep, reason = watcher.tick()
        self.assertEqual((keep, reason), (True, "SUBMITTED"))

    def test_boundary_confirmed_latches_threshold_when_still_full(self):
        rows = [usage_row(170000), boundary_row(post=170000)]
        watcher = self.make_watcher(rows, LadderTmux([IDLE]))
        watcher.attempt = managed.new_attempt(
            self.token, managed.generation(managed.default_cursor()),
            0, 0.0, "boot")
        watcher.attempt["state"] = "SUBMITTED"
        watcher.attempt["timers"]["ack_deadline_mono"] = 10 ** 9
        keep, reason = watcher.tick()
        self.assertEqual((keep, reason), (True, "latched"))
        self.assertEqual((watcher.attempt["state"],
                          watcher.attempt["latch_kind"]),
                         ("LATCHED", "THRESHOLD"))

    def test_threshold_latch_immune_to_stale_own_packet(self):
        # The consumed attempt's packet must not re-ACK the latch (its
        # injection lifecycle is over), and the latch must re-arm on
        # GROWTH: context only grows between compactions, so a pure
        # pct-drop re-arm would latch managed mode forever.
        rows = [usage_row(170000), boundary_row(post=170000)]
        tmux = EchoTmux()
        watcher = self.make_watcher(rows, tmux)
        watcher.attempt = managed.new_attempt(
            self.token, managed.generation(managed.default_cursor()),
            0, 0.0, "boot")
        nonce = watcher.attempt["nonce"]
        watcher.attempt["state"] = "SUBMITTED"
        watcher.attempt["timers"]["ack_deadline_mono"] = 10 ** 9
        packet_file = managed.packet_path(self.cfg, self.sid)
        cm._private_makedirs(os.path.dirname(packet_file))
        with open(packet_file, "w") as fh:
            json.dump({"seq": 1, "custom_instructions": "[cm-%s]" % nonce,
                       "base_compaction_count": 0}, fh)
        keep, reason = watcher.tick()
        self.assertEqual((keep, reason), (True, "latched"))
        self.assertEqual(watcher.attempt["nonces"], [])
        keep, reason = watcher.tick()  # stale own packet must NOT re-ACK
        self.assertEqual((keep, reason), (True, "latched"))
        self.assertEqual(watcher.attempt["state"], "LATCHED")

    def test_threshold_latch_rearms_on_growth_and_refires(self):
        rows = [usage_row(170000), boundary_row(post=150000)]
        watcher = self.make_watcher(rows, EchoTmux())
        watcher.attempt = managed.new_attempt(
            self.token, managed.generation(managed.default_cursor()),
            0, 0.0, "boot")
        watcher.attempt["state"] = "SUBMITTED"
        watcher.attempt["timers"]["ack_deadline_mono"] = 10 ** 9
        keep, reason = watcher.tick()
        self.assertEqual((keep, reason), (True, "latched"))
        # rearm_band_pct * 200k window = 16k; grow past it, above trigger
        append_rows(self.transcript, [usage_row(180000)])
        keep, reason = watcher.tick()   # re-arm tick
        self.assertEqual((keep, reason), (True, "latched"))
        self.assertIsNone(watcher.attempt)
        keep, reason = watcher.tick()   # re-fire
        self.assertEqual((keep, reason), (True, "SUBMITTED"))

    def test_boundary_confirmed_clears_when_rearmed(self):
        rows = [usage_row(170000), boundary_row(post=5000)]
        watcher = self.make_watcher(rows, LadderTmux([IDLE]))
        watcher.attempt = managed.new_attempt(
            self.token, managed.generation(managed.default_cursor()),
            0, 0.0, "boot")
        watcher.attempt["state"] = "SUBMITTED"
        watcher.attempt["timers"]["ack_deadline_mono"] = 10 ** 9
        keep, reason = watcher.tick()
        self.assertEqual((keep, reason), (True, "below_threshold"))
        self.assertIsNone(watcher.attempt)

    def test_threshold_trigger_runs_ladder_to_submitted(self):
        watcher = self.make_watcher([usage_row(170000)], EchoTmux())
        keep, reason = watcher.tick()
        self.assertEqual((keep, reason), (True, "SUBMITTED"))
        states = [r["state"] for r in
                  managed.read_journal(self.paths["journal"])]
        for want in ("TRIGGERED", "PREPARED", "TYPED_VERIFIED", "SUBMITTED"):
            self.assertIn(want, states)

    def test_fast_own_completion_counts_end_to_end(self):
        # Full pipeline pin (Sol round-2): tick 1 runs the real ladder
        # to SUBMITTED; the OWN packet AND the boundary then land
        # inside one poll; tick 2 journals BOUNDARY_CONFIRMED carrying
        # own proof, no ACKED record ever exists, and the counter still
        # sees the fired compact.
        watcher = self.make_watcher([usage_row(170000)], EchoTmux())
        keep, reason = watcher.tick()
        self.assertEqual((keep, reason), (True, "SUBMITTED"))
        nonce = watcher.attempt["nonce"]
        packet_file = managed.packet_path(self.cfg, self.sid)
        cm._private_makedirs(os.path.dirname(packet_file))
        with open(packet_file, "w") as fh:
            json.dump({"seq": 1, "custom_instructions": "[cm-%s]" % nonce,
                       "base_compaction_count": 0}, fh)
        append_rows(self.transcript, [boundary_row(post=5000)])
        keep, _ = watcher.tick()
        self.assertTrue(keep)
        records = list(managed.read_journal(self.paths["journal"]))
        states = [r["state"] for r in records]
        self.assertIn("BOUNDARY_CONFIRMED", states)
        self.assertNotIn("ACKED", states)
        confirmed = [r for r in records
                     if r["state"] == "BOUNDARY_CONFIRMED"][-1]
        self.assertIs(confirmed["own_packet_proof"], True)
        self.assertEqual(managed.cm_compact_count(self.cfg, self.sid), 1)

    def test_notify_escapes_tmux_format_expansion(self):
        shown = []

        def tmux(argv, timeout=5):
            argv = list(argv)
            cmd = argv[2] if argv[:1] == ["-S"] else argv[0]
            if cmd == "list-clients":
                return Result("client0\n")
            if cmd == "display-message":
                shown.append(argv[-1])
            return Result("")
        attempt = {"state": "CLEANUP_REQUIRED",
                   "reason": "bad #{pane_id} #(cmd) input",
                   "run_token": self.token}
        managed.notify(self.binding, self.cfg, self.paths, attempt, tmux)
        self.assertEqual(len(shown), 1)
        self.assertNotIn("#{", shown[0].replace("##", ""))
        self.assertIn("##{pane_id}", shown[0])

    def test_override_file_changes_trigger_at_tick_time(self):
        # A RUNNING watcher must honor a mid-flight `override` write:
        # 130k of 200k (65%) sits below the default 80% trigger, then
        # the override file drops the trigger to 60% and the very next
        # tick fires.
        watcher = self.make_watcher([usage_row(130_000)], EchoTmux())
        self.assertEqual(watcher.tick(), (True, "below_threshold"))
        managed._atomic_json(cm.override_path(self.cfg, self.sid),
                             {"managed_trigger_pct": 0.6})
        self.assertEqual(watcher.tick(), (True, "SUBMITTED"))

    def test_override_window_changes_pct_at_tick_time(self):
        # Same shape via the window: 130k of an overridden 150k window
        # is 86.7%, over the unchanged 80% trigger.
        watcher = self.make_watcher([usage_row(130_000)], EchoTmux())
        self.assertEqual(watcher.tick(), (True, "below_threshold"))
        managed._atomic_json(cm.override_path(self.cfg, self.sid),
                             {"context_window": 150_000})
        self.assertEqual(watcher.tick(), (True, "SUBMITTED"))

    def test_request_triggers_below_threshold_with_fingerprint(self):
        watcher = self.make_watcher([usage_row(50)], EchoTmux())
        cm._private_makedirs(os.path.dirname(self.paths["request"]))
        with open(self.paths["request"], "w") as fh:
            json.dump({"request_id": "please-compact-now"}, fh)
        keep, reason = watcher.tick()
        self.assertEqual((keep, reason), (True, "SUBMITTED"))
        triggered = [r for r in managed.read_journal(self.paths["journal"])
                     if r.get("state") == "TRIGGERED"]
        self.assertEqual(
            triggered[-1]["request_fingerprint"]["request_id"],
            "please-compact-now")


class StopSessionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.cfg = dict(cm._DEFAULTS, state_dir=self.temp.name)
        self.sid = "session-1234"
        self.paths = managed.managed_paths(self.cfg, self.sid, "sock", "%1")
        managed._atomic_json(self.paths["session_lease"],
                             {"run_token": "a" * 16, "pid": 424242,
                              "proc_start": 7, "heartbeat_at": time.time()})

    def test_pidfd_reverifies_identity_after_open(self):
        # First proc_matches passes the txn check; the second (after
        # pidfd_open) sees a recycled pid and must block the signal.
        devnull = os.open(os.devnull, os.O_RDONLY)
        sent = []
        with mock.patch.object(managed, "proc_matches",
                               side_effect=[True, False]), \
                mock.patch.object(managed.os, "pidfd_open",
                                  return_value=devnull, create=True), \
                mock.patch.object(managed.signal, "pidfd_send_signal",
                                  side_effect=lambda *a: sent.append(a),
                                  create=True):
            ok, message = managed.stop_session(self.cfg, self.sid)
        self.assertFalse(ok)
        self.assertEqual(message, "pid changed before signal")
        self.assertEqual(sent, [])

    def test_pidfd_signals_when_identity_holds(self):
        devnull = os.open(os.devnull, os.O_RDONLY)
        sent = []
        with mock.patch.object(managed, "proc_matches",
                               return_value=True), \
                mock.patch.object(managed.os, "pidfd_open",
                                  return_value=devnull, create=True), \
                mock.patch.object(managed.signal, "pidfd_send_signal",
                                  side_effect=lambda *a: sent.append(a),
                                  create=True):
            ok, _ = managed.stop_session(self.cfg, self.sid, timeout=0.0)
        self.assertEqual(sent, [(devnull, managed.signal.SIGTERM)])


class CliTests(unittest.TestCase):
    def test_start_passes_claude_as_argv_vector(self):
        calls = []

        def runner(argv, timeout=5):
            calls.append(argv)
            return Result("%9\n")
        binding = {"session_id": "session-1234"}
        with mock.patch.object(managed, "_cli_binding",
                               return_value=(binding, None)), \
                mock.patch.object(managed, "spawn_watcher",
                                  return_value={"ok": True, "pid": 1,
                                                "run_token": "a" * 16}), \
                mock.patch.object(sys, "stdout", new=io.StringIO()):
            rc = managed.cli_main(
                ["start", "--session-name", "cm", "--", "claude",
                 "--model", "haiku"], run_tmux=runner)
        self.assertEqual(rc, 0)
        self.assertEqual(calls[0][-3:], ["claude", "--model", "haiku"])
        self.assertNotIn("sh -c", calls[0])

    def test_adopt_requires_attended(self):
        self.assertEqual(managed.cli_main(["adopt", "-t", "%1"]), 2)

    def _override_cli(self, tmp, argv):
        env = {"COMPACT_MANAGER_CONFIG": "/nonexistent/cm-test.json",
               "COMPACT_MANAGER_STATE_DIR": tmp}
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, env), \
                mock.patch.object(sys, "stdout", new=out), \
                mock.patch.object(sys, "stderr", new=err):
            rc = managed.cli_main(["override"] + argv)
        return rc, out.getvalue(), err.getvalue()

    def test_override_roundtrip_prefix_and_clear(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        cfg = dict(cm._DEFAULTS, state_dir=tmp.name)
        rc, out, err = self._override_cli(
            tmp.name, ["sess-ovr-1234", "trigger=60%", "soft=0.5",
                       "hard=55%", "window=500000"])
        self.assertEqual(rc, 0, err)
        self.assertEqual(cm.session_overrides(cfg, "sess-ovr-1234"),
                         {"managed_trigger_pct": 0.6, "soft_pct": 0.5,
                          "hard_pct": 0.55, "context_window": 500_000})
        self.assertIn("effective now", out)
        self.assertIn("trigger=60%", out)
        # Writer endpoints match the reader's inclusive bounds.
        for w, ok in (("10000", True), ("1000000000", True),
                      ("9999", False), ("1000000001", False),
                      ("9" * 4000, False)):
            frag, error = managed._parse_override_assignments(
                ["window=" + w])
            self.assertEqual(error is None, ok, w)
        # Bare percentages read as percentages (65 → 65%), merging onto
        # the existing file — addressed by unambiguous prefix this time
        # (the override file itself is an expand_sid source).
        rc, out, err = self._override_cli(tmp.name,
                                          ["sess-ovr", "trigger=65"])
        self.assertEqual(rc, 0, err)
        merged = cm.session_overrides(cfg, "sess-ovr-1234")
        self.assertEqual(merged["managed_trigger_pct"], 0.65)
        self.assertEqual(merged["soft_pct"], 0.5)  # earlier keys survive
        # Show-only invocation changes nothing.
        rc, out, err = self._override_cli(tmp.name, ["sess-ovr"])
        self.assertEqual(rc, 0, err)
        self.assertIn("override file:", out)
        # --clear removes the file; clearing again reports, exit 0.
        rc, out, err = self._override_cli(tmp.name,
                                          ["sess-ovr", "--clear"])
        self.assertEqual(rc, 0, err)
        self.assertEqual(cm.session_overrides(cfg, "sess-ovr-1234"), {})
        rc, out, err = self._override_cli(tmp.name,
                                          ["sess-ovr-1234", "--clear"])
        self.assertEqual(rc, 0, err)
        self.assertIn("no overrides", out)

    def test_override_rejects_bad_input_loudly(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        cfg = dict(cm._DEFAULTS, state_dir=tmp.name)
        for bad in (["sess-bad-1234", "soft=abc"],
                    ["sess-bad-1234", "window=5000"],
                    ["sess-bad-1234", "window=1e6"],
                    ["sess-bad-1234", "pct=0.5"],
                    ["sess-bad-1234", "trigger"],
                    ["sess-bad-1234", "trigger=150"],
                    ["sess-bad-1234", "trigger=0"],
                    ["sess-bad-1234", "trigger=nan"],
                    ["sess-bad-1234", "trigger=60%%"],
                    ["sess-bad-1234", "window=99999999999"],
                    # path_component canonicalization must not let an
                    # invalid sid alias a valid one's override file.
                    [".sess-bad-1234", "trigger=60%"],
                    ["sess bad 1234", "trigger=60%"],
                    ["abc", "trigger=60%"],
                    ["sess-bad-1234", "soft=1%", "--clear"]):
            rc, out, err = self._override_cli(tmp.name, bad)
            self.assertEqual(rc, 1, bad)
            self.assertTrue(err.strip(), bad)
        self.assertEqual(cm.session_overrides(cfg, "sess-bad-1234"), {})
        self.assertFalse(
            os.path.exists(os.path.join(tmp.name, "overrides")))


if __name__ == "__main__":
    unittest.main()


class OverviewTests(unittest.TestCase):
    def test_overview_flags_and_current_marker(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        cfg = dict(cm._DEFAULTS, mode="managed", state_dir=tmp.name,
                   context_window=200_000,
                   models={"fable": {"context_window": 1_000_000}})
        lease_dir = os.path.join(tmp.name, "managed", "leases")
        os.makedirs(lease_dir)
        with open(os.path.join(lease_dir, "session-sBad.json"), "w") as fh:
            json.dump({"heartbeat_at": time.time()}, fh)  # malformed: no pid
        state_dir = os.path.join(tmp.name, "state")
        os.makedirs(state_dir)
        now = time.time()
        with open(os.path.join(state_dir, "old.json"), "w") as fh:
            json.dump({"model": "claude-fable-5", "current": 100_000,
                       "peak": 200_000}, fh)
        os.utime(os.path.join(state_dir, "old.json"), (now - 60, now - 60))
        with open(os.path.join(state_dir, "new.json"), "w") as fh:
            json.dump({"model": "claude-opus-5", "current": 50_000,
                       "peak": 50_000}, fh)
        # A state file with a garbage token field degrades to one bad
        # row, never a lost readout (verify-round pin).
        with open(os.path.join(state_dir, "garbage.json"), "w") as fh:
            json.dump({"model": "claude-opus-5", "current": "oops",
                       "peak": None}, fh)
        os.utime(os.path.join(state_dir, "garbage.json"),
                 (now - 120, now - 120))
        out = managed.overview_text(cfg, now=now)
        self.assertIn("mode=managed", out)
        # global thresholds on the header; trigger defaults to hard
        self.assertIn("soft=70%  hard=80%  trigger=80%", out)
        self.assertIn("model overrides (1)", out)
        fable = [l for l in out.splitlines()
                 if l.strip().startswith("fable")][0]
        self.assertEqual(fable.split(),
                         ["fable", "1M", "70%", "80%", "80%"])
        garbage = [l for l in out.splitlines() if "garbage" in l][0]
        self.assertEqual(garbage.split()[:5],
                         ["garbage", "claude-opus-5", "0", "0", "0.0%"])
        self.assertIn("MALFORMED-LEASE", out)   # live-by-ambiguity, pid None
        self.assertIn("\n> new", out)           # newest mtime marked
        self.assertIn("10.0%", out)         # 100k of fable's 1M override
        self.assertIn("25.0%", out)         # 50k of the configured 200k global
        self.assertIn("updated", out)   # age column present
        self.assertIn("ago", out)

    def test_overview_data_json(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        cfg = dict(cm._DEFAULTS, mode="managed", state_dir=tmp.name,
                   context_window=200_000,
                   models={"fable": {"context_window": 1_000_000}})
        state_dir = os.path.join(tmp.name, "state")
        os.makedirs(state_dir)
        now = time.time()
        with open(os.path.join(state_dir, "aaaa-full-id.json"), "w") as fh:
            json.dump({"model": "claude-fable-5", "current": 100_000,
                       "peak": 200_000}, fh)
        with open(os.path.join(state_dir, "bad.json"), "w") as fh:
            json.dump({"model": {"weird": True}, "current": 1}, fh)
        # inf-model: json accepts 1e400 as Infinity; must not survive to
        # the payload (bare Infinity is invalid JSON to JS consumers)
        with open(os.path.join(state_dir, "infm.json"), "w") as fh:
            fh.write('{"model": 1e400, "current": 2}')
        # deeply nested but LOADABLE model: must not blow up the final
        # json.dumps after loading fine
        with open(os.path.join(state_dir, "nested.json"), "w") as fh:
            fh.write('{"model": ' + '[' * 900 + ']' * 900 + ', "current": 3}')
        data = managed.overview_data(cfg, now=now)
        # must be JSON-serializable end to end
        json.dumps(data)
        self.assertEqual(data["mode"], "managed")
        self.assertEqual(
            data["models"]["fable"],
            {"context_window": 1_000_000, "soft_pct": 0.70,
             "hard_pct": 0.80, "managed_trigger_pct": 0.80})
        rows = {s["session_id"]: s for s in data["sessions"]}
        # full ids in JSON (the text table shortens to 8 chars)
        good = rows["aaaa-full-id"]
        self.assertEqual(good["current"], 100_000)
        self.assertEqual(good["window"], 1_000_000)
        self.assertAlmostEqual(good["pct"], 10.0)
        self.assertIn("age_s", good)
        # a non-str model is not unreadable: window_for stringifies,
        # and the payload carries None instead of the hostile value
        bad = rows["bad"]
        self.assertNotIn("unreadable", bad)
        self.assertEqual(bad["current"], 1)
        self.assertIsNone(bad["model"])
        self.assertIsNone(rows["infm"]["model"])
        self.assertIsNone(rows["nested"]["model"])
        # the serialized form must be strict JSON (no bare Infinity)
        json.loads(json.dumps(data))
        for s in data["sessions"]:
            self.assertNotIn("Infinity", json.dumps(s))
        # text renderer consumes the same data without crashing
        self.assertIn("aaaa-ful", managed.overview_text(cfg, now=now))

    def test_overview_counts_manager_fired_compacts(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        cfg = dict(cm._DEFAULTS, mode="managed", state_dir=tmp.name,
                   context_window=200_000)
        state_dir = os.path.join(tmp.name, "state")
        os.makedirs(state_dir)
        for sid in ("watched-sid", "plain-sid"):
            with open(os.path.join(state_dir, sid + ".json"), "w") as fh:
                json.dump({"model": "claude-opus-5", "current": 10_000,
                           "peak": 10_000}, fh)
        journal = os.path.join(tmp.name, "managed", "watchers",
                               "watched-sid.journal.jsonl")
        # Attempt A fired and completed; the recovery replay of ACKED
        # must not double-count the same attempt.
        a = {"attempt_id": "aaa", "nonce": "n1", "nonces": ["n1"]}
        for state in ("TRIGGERED", "SUBMITTED", "ACKED", "ACKED",
                      "BOUNDARY_CONFIRMED"):
            managed.journal_record(journal, state, a)
        # Attempt B fired (own nonce reached PreCompact); completion
        # still pending — already a fired compact.
        managed.journal_record(journal, "ACKED",
                               {"attempt_id": "bbb", "nonces": ["n2"]})
        # Attempt C: deferred-foreign resolved by a NATIVE compaction —
        # BOUNDARY_CONFIRMED without own proof is not the manager's.
        c = {"attempt_id": "ccc", "nonces": ["n3"]}
        managed.journal_record(journal, "DEFERRED", c,
                               reason="foreign_packet")
        managed.journal_record(journal, "BOUNDARY_CONFIRMED", c)
        # Attempt D: fast completion — packet and boundary inside one
        # poll journals BOUNDARY_CONFIRMED with own proof, never ACKED.
        managed.journal_record(journal, "BOUNDARY_CONFIRMED",
                               {"attempt_id": "ddd", "nonces": ["n4"],
                                "own_packet_proof": True})
        # Attempt E: retry aborted at R6' on a late own packet — the
        # proof-stamped CLEANUP_REQUIRED still counts the fired compact.
        managed.journal_record(journal, "CLEANUP_REQUIRED",
                               {"attempt_id": "eee", "nonces": ["n5"],
                                "own_packet_proof": True},
                               reason="R6_prime_own_packet_late")
        # Hostile schema-valid records: non-string attempt ids must
        # neither crash the count nor fabricate fired compacts.
        for bad_id in (["x"], 7, True, ""):
            managed.journal_record(journal, "ACKED",
                                   {"attempt_id": bad_id, "nonces": []})
        # A watched session whose journal holds no fired attempt is 0,
        # not blank.
        idle_journal = os.path.join(tmp.name, "managed", "watchers",
                                    "plain-sid.journal.jsonl")
        managed.journal_record(idle_journal, "WATCHER_READY",
                               {"attempt_id": None, "nonces": []})
        now = time.time()
        data = managed.overview_data(cfg, now=now)
        json.dumps(data)
        rows = {s["session_id"]: s for s in data["sessions"]}
        self.assertEqual(rows["watched-sid"]["cm_compacts"], 4)
        self.assertEqual(rows["plain-sid"]["cm_compacts"], 0)
        # No journal at all means unmanaged, not zero fired compacts.
        with open(os.path.join(state_dir, "nojournal-sid.json"), "w") as fh:
            json.dump({"model": "claude-opus-5", "current": 1,
                       "peak": 1}, fh)
        data = managed.overview_data(cfg, now=time.time())
        rows = {s["session_id"]: s for s in data["sessions"]}
        self.assertIsNone(rows["nojournal-sid"]["cm_compacts"])
        out = managed.overview_text(cfg, now=now)
        self.assertIn("  cm  ", out)
        watched = [l for l in out.splitlines()
                   if "watched-" in l and "opus" in l][0]
        # Row tail is: ... trig cm "Ns ago" alive — cm sits 4th from end.
        self.assertEqual(watched.split()[-4], "4")
        self.assertIn("(cm = compactions", out)
        # Torn/garbage journal bytes degrade to what parses, never raise.
        with open(journal, "ab") as fh:
            fh.write(b"\x00garbage\n")
        self.assertEqual(managed.cm_compact_count(cfg, "watched-sid"), 4)
        # A PRESENT journal that cannot be read is unknown (None),
        # never a definite 0 (Sol round-3).
        os.chmod(journal, 0)
        try:
            self.assertIsNone(
                managed.cm_compact_count(cfg, "watched-sid"))
        finally:
            os.chmod(journal, 0o600)
        # The outer guard: an unexpected reader explosion degrades to
        # None, never an exception out of the readout.
        with mock.patch.object(managed, "_journal_records",
                               side_effect=RuntimeError("boom")):
            self.assertIsNone(
                managed.cm_compact_count(cfg, "watched-sid"))

    def test_per_model_trigger_override(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = os.path.join(tmp.name, "config.json")
        with open(path, "w") as fh:
            json.dump({"models": {
                "haiku": {"context_window": 200_000,
                          "managed_trigger_pct": 0.9},
                # only a trigger: cm's cleaner would drop this pattern,
                # the managed layer must graft it back
                "opus": {"managed_trigger_pct": 0.5},
            }}, fh)
        # base plays cm.load_config's cleaned output (trigger stripped)
        base = dict(cm._DEFAULTS, mode="managed",
                    models={"haiku": {"context_window": 200_000}})
        cfg = managed.load_config(
            base=base, environ={"COMPACT_MANAGER_CONFIG": path})
        self.assertEqual(managed.trigger_for(cfg, "claude-haiku-4"), 0.9)
        # 0.5 <= effective soft 0.7: invalid, falls back to the global
        # trigger (itself defaulted to hard)
        self.assertEqual(managed.trigger_for(cfg, "claude-opus-5"), 0.8)
        self.assertEqual(managed.trigger_for(cfg, "unmatched-model"), 0.8)
        # env models JSON wins over the file, same as the cm layer
        cfg_env = managed.load_config(base=dict(base), environ={
            "COMPACT_MANAGER_CONFIG": path,
            "COMPACT_MANAGER_MODELS":
                '{"haiku": {"managed_trigger_pct": 0.85}}'})
        self.assertEqual(managed.trigger_for(cfg_env, "claude-haiku-4"),
                         0.85)
        # EMPTY env var falls through to the file (cm's `env or file`
        # precedence, mirrored) — file triggers must survive
        cfg_empty = managed.load_config(base=dict(base), environ={
            "COMPACT_MANAGER_CONFIG": path,
            "COMPACT_MANAGER_MODELS": ""})
        self.assertEqual(managed.trigger_for(cfg_empty, "claude-haiku-4"),
                         0.9)

    def test_trigger_no_override_is_exact_noop(self):
        # A config with NO per-model triggers must use the global
        # trigger verbatim, even when a per-model soft override sits
        # ABOVE the global trigger (Sol HIGH-1: re-validating the
        # global against effective soft silently delayed compaction).
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = os.path.join(tmp.name, "config.json")
        with open(path, "w") as fh:
            json.dump({"managed_trigger_pct": 0.8}, fh)
        base = dict(cm._DEFAULTS, mode="managed",
                    models={"fable": {"soft_pct": 0.9, "hard_pct": 0.95}})
        cfg = managed.load_config(
            base=base, environ={"COMPACT_MANAGER_CONFIG": path})
        self.assertEqual(managed.trigger_for(cfg, "claude-fable-5"), 0.8)

    def test_trigger_only_pattern_does_not_shadow(self):
        # A longer trigger-only pattern must not shadow a shorter
        # pattern's window/soft/hard in window_for (Sol HIGH-2).
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = os.path.join(tmp.name, "config.json")
        with open(path, "w") as fh:
            json.dump({"models": {
                "opus": {"context_window": 200_000},
                "claude-opus-5": {"managed_trigger_pct": 0.5},
            }}, fh)
        # base plays cm's cleaned output: the trigger-only pattern is gone
        base = dict(cm._DEFAULTS, mode="managed",
                    models={"opus": {"context_window": 200_000}})
        cfg = managed.load_config(
            base=base, environ={"COMPACT_MANAGER_CONFIG": path})
        self.assertEqual(
            cm.window_for(cfg, "claude-opus-5")["context_window"], 200_000)
        # the invalid (<= soft) trigger falls back to the global one
        self.assertEqual(managed.trigger_for(cfg, "claude-opus-5"), 0.8)
        # and cfg["models"] itself was not polluted
        self.assertEqual(set(cfg["models"]), {"opus"})

    def test_expand_sid_prefixes(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        cfg = dict(cm._DEFAULTS, mode="managed", state_dir=tmp.name)
        lease_dir = os.path.join(tmp.name, "managed", "leases")
        os.makedirs(lease_dir)
        for sid in ("abcd1234-full-one", "abcd9999-full-two"):
            with open(os.path.join(lease_dir,
                                   "session-%s.json" % sid), "w") as fh:
                json.dump({"pid": 1}, fh)
        # unique prefix expands to the full id
        self.assertEqual(managed.expand_sid(cfg, "abcd1234"),
                         ("abcd1234-full-one", None))
        # exact id passes through
        self.assertEqual(managed.expand_sid(cfg, "abcd1234-full-one"),
                         ("abcd1234-full-one", None))
        # ambiguous prefix is an error naming the matches
        sid, error = managed.expand_sid(cfg, "abcd")
        self.assertIsNone(sid)
        self.assertIn("abcd1234-full-one", error)
        self.assertIn("abcd9999-full-two", error)
        # unknown id passes through so stop reports 'no live lease'
        self.assertEqual(managed.expand_sid(cfg, "zzzz"), ("zzzz", None))
        # ghost hygiene (Sol LOW-9): a dangling symlink and a
        # non-session-shaped name must not widen a prefix into ambiguity
        os.symlink("/nonexistent-target",
                   os.path.join(lease_dir, "session-abcd1234-ghost.json"))
        with open(os.path.join(lease_dir, "session-x!.json"), "w") as fh:
            fh.write("{}")
        # a DIRECTORY and a trailing-newline name are ghosts too (Sol r2)
        os.makedirs(os.path.join(lease_dir, "session-abcd1234-dir.json"))
        with open(os.path.join(lease_dir, "session-abcd1234nl\n.json"),
                  "w") as fh:
            fh.write("{}")
        self.assertEqual(managed.expand_sid(cfg, "abcd1234"),
                         ("abcd1234-full-one", None))

    def test_overview_flag_branches_and_bad_token_values(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        cfg = dict(cm._DEFAULTS, mode="managed", state_dir=tmp.name,
                   context_window=200_000)
        lease_dir = os.path.join(tmp.name, "managed", "leases")
        wdir = os.path.join(tmp.name, "managed", "watchers")
        os.makedirs(lease_dir)
        os.makedirs(wdir)
        # negative pid, fresh heartbeat: live-by-ambiguity + pid-malformed
        with open(os.path.join(lease_dir, "session-sNeg.json"), "w") as fh:
            json.dump({"run_token": "t", "pid": -7, "proc_start": 1,
                       "heartbeat_at": time.time()}, fh)
        # stale heartbeat + dead pid: provably dead lease, non-retired state
        with open(os.path.join(lease_dir, "session-sDead.json"), "w") as fh:
            json.dump({"run_token": "t", "pid": 999999999,
                       "proc_start": 1, "heartbeat_at": 1.0}, fh)
        # LATCHED journal tail -> ATTENTION
        with open(os.path.join(lease_dir, "session-sAlert.json"), "w") as fh:
            json.dump({"run_token": "u", "pid": os.getpid(),
                       "proc_start": 1, "heartbeat_at": time.time()}, fh)
        managed.journal_record(
            os.path.join(wdir, "sAlert.journal.jsonl"), "LATCHED",
            {"latch_kind": "SAFETY", "reason": "missing_ack"})
        # journal-only cleanly retired watcher (no lease): must read
        # RETIRED, not the READY default plus a DEAD-LEASE false alarm
        managed.journal_record(
            os.path.join(wdir, "sGone.journal.jsonl"), "WATCHER_RETIRED",
            {}, reason="claude_dead")
        # retirement AFTER a completed attempt: the final RETIRED record
        # wins over the older attempt row (Sol MEDIUM-3)
        managed.journal_record(
            os.path.join(wdir, "sDone.journal.jsonl"),
            "BOUNDARY_CONFIRMED", {"run_token": "t"})
        managed.journal_record(
            os.path.join(wdir, "sDone.journal.jsonl"), "WATCHER_RETIRED",
            {}, reason="claude_dead")
        # ...but retirement never masks a recovered HAZARD: a PREPARED
        # tail maps to CLEANUP_REQUIRED and must stay flagged
        managed.journal_record(
            os.path.join(wdir, "sHazard.journal.jsonl"),
            "PREPARED", {"run_token": "t"})
        managed.journal_record(
            os.path.join(wdir, "sHazard.journal.jsonl"), "WATCHER_RETIRED",
            {}, reason="claude_dead")
        # one malformed journal record (non-iterable nonces) must degrade
        # to a flagged default row, never abort the readout (Sol r2)
        with open(os.path.join(wdir, "sMangled.journal.jsonl"), "w") as fh:
            fh.write('{"schema": 1, "state": "LATCHED", "nonces": 42}\n')
        # retired journal but the lease was never released: the leftover
        # lease is a cleanup hazard, so DEAD-LEASE must survive (Sol LOW-5)
        with open(os.path.join(lease_dir, "session-sOrphan.json"), "w") as fh:
            json.dump({"run_token": "t", "pid": 999999999,
                       "proc_start": 1, "heartbeat_at": 1.0}, fh)
        managed.journal_record(
            os.path.join(wdir, "sOrphan.journal.jsonl"), "WATCHER_RETIRED",
            {}, reason="claude_dead")
        # state file with negative + non-finite token values -> zeros
        sdir = os.path.join(tmp.name, "state")
        os.makedirs(sdir)
        # deeply nested state file: RecursionError escapes json.load's
        # usual ValueError contract; the row must be skipped, never
        # abort the whole readout (Sol MEDIUM-4)
        with open(os.path.join(sdir, "deep.json"), "w") as fh:
            fh.write('{"a":' * 50_000 + "1" + "}" * 50_000)
        with open(os.path.join(sdir, "weird.json"), "w") as fh:
            fh.write('{"model": "m", "current": -50, "peak": 1e999}')
        # huge FINITE float: must zero, not print pct=inf%
        with open(os.path.join(sdir, "huge.json"), "w") as fh:
            fh.write('{"model": "m", "current": 1e308, "peak": 3}')
        # future mtime: surfaced as untrustworthy, never a fake 0s age
        fut = os.path.join(sdir, "future.json")
        with open(fut, "w") as fh:
            fh.write('{"model": "m", "current": 5, "peak": 5}')
        later = time.time() + 3600
        os.utime(fut, (later, later))
        # boundary pin: +2s is already an anomaly, and no negative age
        # may ever render
        fut2 = os.path.join(sdir, "future2.json")
        with open(fut2, "w") as fh:
            fh.write('{"model": "m", "current": 5, "peak": 5}')
        near = time.time() + 2
        os.utime(fut2, (near, near))
        # in-slop future fixture with a FIXED clock: exercises the
        # max(0,...) floor itself — without it this renders -1s-ago
        fixed_now = time.time()
        fut3 = os.path.join(sdir, "slop.json")
        with open(fut3, "w") as fh:
            fh.write('{"model": "m", "current": 5, "peak": 5}')
        os.utime(fut3, (fixed_now + 0.9, fixed_now + 0.9))
        out = managed.overview_text(cfg, now=fixed_now)
        neg = [l for l in out.splitlines() if "sNeg" in l][0]
        self.assertIn("MALFORMED-LEASE", neg)
        dead = [l for l in out.splitlines() if "sDead" in l][0]
        self.assertIn("DEAD-LEASE", dead)
        alert = [l for l in out.splitlines() if "sAlert" in l][0]
        self.assertIn("ATTENTION", alert)
        gone = [l for l in out.splitlines() if "sGone" in l][0]
        self.assertIn("RETIRED", gone)
        self.assertIn("claude_dead", gone)
        self.assertNotIn("DEAD-LEASE", gone)
        done = [l for l in out.splitlines() if "sDone" in l][0]
        self.assertIn("RETIRED", done)
        self.assertNotIn("DEAD-LEASE", done)
        hazard = [l for l in out.splitlines() if "sHazard" in l][0]
        self.assertIn("ATTENTION", hazard)
        orphan = [l for l in out.splitlines() if "sOrphan" in l][0]
        self.assertIn("RETIRED", orphan)
        self.assertIn("DEAD-LEASE", orphan)
        mangled = [l for l in out.splitlines() if "sMangled" in l][0]
        self.assertIn("journal unreadable", mangled)
        self.assertIn("DEAD-LEASE", mangled)  # no lease, degraded state
        self.assertNotIn("deep", out)  # nested bomb skipped, not fatal
        weird = [l for l in out.splitlines() if "weird" in l][0]
        self.assertEqual(weird.split()[:5], ["weird", "m", "0", "0", "0.0%"])
        huge = [l for l in out.splitlines() if "huge" in l][0]
        self.assertEqual(huge.split()[2], "0")
        self.assertNotIn("inf", huge)
        slop = [l for l in out.splitlines() if "slop" in l][0]
        self.assertIn("0s ago", slop)
        for name in ("future ", "future2 "):
            row = [l for l in out.splitlines() if name in l][0]
            self.assertIn("FUTURE-MTIME", row)
        self.assertNotRegex(out, r"-\d+s ago")  # no negative age anywhere

    def test_overview_retired_watcher_ages_out_after_24h(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        cfg = dict(cm._DEFAULTS, mode="managed", state_dir=tmp.name,
                   context_window=200_000)
        lease_dir = os.path.join(tmp.name, "managed", "leases")
        wdir = os.path.join(tmp.name, "managed", "watchers")
        os.makedirs(lease_dir)
        os.makedirs(wdir)
        now = time.time()
        old = now - 25 * 3600
        # cleanly retired >24h ago: drops from the overview (the
        # journal file itself lives on to state_ttl_days)
        old_done = os.path.join(wdir, "sOldDone.journal.jsonl")
        managed.journal_record(old_done, "WATCHER_RETIRED", {},
                               reason="deadline")
        os.utime(old_done, (old, old))
        # cleanly retired just now: stays
        managed.journal_record(
            os.path.join(wdir, "sNewDone.journal.jsonl"),
            "WATCHER_RETIRED", {}, reason="claude_dead")
        # retired >24h ago but the lease was never released: a hazard
        # row (DEAD-LEASE) never ages out of the display
        with open(os.path.join(lease_dir, "session-sOldOrphan.json"),
                  "w") as fh:
            json.dump({"run_token": "t", "pid": 999999999,
                       "proc_start": 1, "heartbeat_at": 1.0}, fh)
        old_orphan = os.path.join(wdir, "sOldOrphan.journal.jsonl")
        managed.journal_record(old_orphan, "WATCHER_RETIRED", {},
                               reason="claude_dead")
        os.utime(old_orphan, (old, old))
        # retired >24h ago with an UNREADABLE leftover lease: status_rows
        # reads has_lease=False (unparseable leases are dropped), but the
        # file still blocks adoption — must stay visible and flagged,
        # never age out as "clean" (Sol audit MEDIUM)
        with open(os.path.join(lease_dir, "session-sOldTorn.json"),
                  "w") as fh:
            fh.write("{not json")
        old_torn = os.path.join(wdir, "sOldTorn.journal.jsonl")
        managed.journal_record(old_torn, "WATCHER_RETIRED", {},
                               reason="claude_dead")
        os.utime(old_torn, (old, old))
        data = managed.overview_data(cfg, now=now)
        by_sid = {w["session_id"]: w for w in data["watchers"]}
        self.assertNotIn("sOldDone", by_sid)
        self.assertIn("sNewDone", by_sid)
        self.assertIn("sOldOrphan", by_sid)
        self.assertIn("DEAD-LEASE", by_sid["sOldOrphan"]["flags"])
        self.assertIn("sOldTorn", by_sid)
        self.assertIn("DEAD-LEASE", by_sid["sOldTorn"]["flags"])
        # journal stat failing (pruned/replaced between listing and
        # stat): the row stays visible and the readout survives
        real_getmtime = os.path.getmtime

        def flaky_getmtime(path):
            if path.endswith("sOldDone.journal.jsonl"):
                raise OSError("gone")
            return real_getmtime(path)

        with mock.patch.object(managed.os.path, "getmtime",
                               side_effect=flaky_getmtime):
            data = managed.overview_data(cfg, now=now)
        by_sid = {w["session_id"]: w for w in data["watchers"]}
        self.assertIn("sOldDone", by_sid)  # fail-open to visible
        self.assertIn("sNewDone", by_sid)


class LeaseAttachedTests(unittest.TestCase):
    def test_malformed_proc_start_is_not_attached(self):
        # A malformed lease must not read as attached even under a
        # fresh heartbeat — it would suppress the unwatched warning
        # (Sol audit). Well-formed = exact positive int starttime.
        base = {"run_token": "t" * 16, "pid": 1,
                "heartbeat_at": time.time()}
        for bad in (None, -5, 0, 1.5, "2020", True):
            self.assertFalse(
                managed.lease_attached(dict(base, proc_start=bad)),
                repr(bad))
        self.assertTrue(
            managed.lease_attached(dict(base, proc_start=2020)))


class SessionLivenessTests(unittest.TestCase):
    """live_session_ids and the overview's session_live/alive surface."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.proc = os.path.join(self.temp.name, "proc")
        self.sessions = os.path.join(self.temp.name, "sessions")
        os.makedirs(self.proc)
        os.makedirs(self.sessions)

    def _entry(self, pid, sid, start):
        with open(os.path.join(self.sessions, "%d.json" % pid), "w") as fh:
            json.dump({"pid": pid, "procStart": str(start),
                       "sessionId": sid}, fh)

    def test_liveness_needs_pid_and_start_proof(self):
        # proven live: pid exists, procStart matches starttime
        write_proc(self.proc, 20, 20, 2020)
        self._entry(20, "sess-live-1234", 2020)
        # dead: registry entry lingers, /proc dir gone
        self._entry(30, "sess-dead-1234", 3030)
        # pid reused: alive but starttime mismatch -> not this session
        write_proc(self.proc, 40, 40, 9999)
        self._entry(40, "sess-reuse-1234", 4040)
        # zombie: process exited, awaiting reap -> not live
        write_proc(self.proc, 60, 60, 6060, state="Z")
        self._entry(60, "sess-zomb-1234", 6060)
        # non-pid filename is ignored without costing completeness
        with open(os.path.join(self.sessions, "notapid.json"), "w") as fh:
            fh.write("{}")
        ids, complete = managed.live_session_ids(self.sessions, self.proc)
        self.assertTrue(complete)
        self.assertEqual(ids, {"sess-live-1234"})

    def test_overview_flags_unwatched_above_trigger(self):
        # Managed mode, provably live session at/over trigger, no
        # attached watcher: the row carries unwatched=True and the
        # text readout warns — the gap that let a live session sit
        # two days over trigger unnoticed.
        state_root = os.path.join(self.temp.name, "cmstate")
        cfg = dict(cm._DEFAULTS, mode="managed", state_dir=state_root,
                   context_window=200_000)
        sdir = os.path.join(state_root, "state")
        os.makedirs(sdir)
        write_proc(self.proc, 20, 20, 2020)
        self._entry(20, "sess-live-1234", 2020)
        with open(os.path.join(sdir, "sess-live-1234.json"), "w") as fh:
            json.dump({"model": "m", "current": 170_000,
                       "peak": 170_000}, fh)
        data = managed.overview_data(cfg, sessions_dir=self.sessions,
                                     proc_root=self.proc)
        self.assertIs(data["sessions"][0].get("unwatched"), True)
        text = managed.overview_text(cfg, sessions_dir=self.sessions,
                                     proc_root=self.proc)
        self.assertIn("NO live watcher", text)
        # Report-only surface relayed to models by the status command:
        # it must name the USER's remedy, never an imperative the
        # reader could execute (Sol audit blocker).
        warn = [l for l in text.splitlines() if "(!)" in l][0]
        self.assertIn("The user can attach", warn)
        self.assertNotIn("adopt", warn)
        # An attached watcher (fresh heartbeat) clears the flag.
        lease_dir = os.path.join(state_root, "managed", "leases")
        os.makedirs(lease_dir)
        lease_path = os.path.join(lease_dir,
                                  "session-sess-live-1234.json")
        with open(lease_path, "w") as fh:
            json.dump({"run_token": "t" * 16, "pid": os.getpid(),
                       "proc_start": 1, "heartbeat_at": time.time()},
                      fh)
        data = managed.overview_data(cfg, sessions_dir=self.sessions,
                                     proc_root=self.proc)
        self.assertNotIn("unwatched", data["sessions"][0])
        # Below trigger: no flag even with no lease.
        os.unlink(lease_path)
        with open(os.path.join(sdir, "sess-live-1234.json"), "w") as fh:
            json.dump({"model": "m", "current": 50_000,
                       "peak": 170_000}, fh)
        data = managed.overview_data(cfg, sessions_dir=self.sessions,
                                     proc_root=self.proc)
        self.assertNotIn("unwatched", data["sessions"][0])
        # Advisory mode: watchers are not expected; never flag.
        with open(os.path.join(sdir, "sess-live-1234.json"), "w") as fh:
            json.dump({"model": "m", "current": 170_000,
                       "peak": 170_000}, fh)
        data = managed.overview_data(
            dict(cfg, mode="advisory"), sessions_dir=self.sessions,
            proc_root=self.proc)
        self.assertNotIn("unwatched", data["sessions"][0])

    def test_overview_unwatched_edge_conditions(self):
        state_root = os.path.join(self.temp.name, "cmstate")
        cfg = dict(cm._DEFAULTS, mode="managed", state_dir=state_root,
                   context_window=200_000)
        sdir = os.path.join(state_root, "state")
        os.makedirs(sdir)
        write_proc(self.proc, 20, 20, 2020)
        self._entry(20, "sess-live-1234", 2020)
        # Exactly AT trigger (hard default 0.8): flags — >= not >.
        with open(os.path.join(sdir, "sess-live-1234.json"), "w") as fh:
            json.dump({"model": "m", "current": 160_000,
                       "peak": 160_000}, fh)
        data = managed.overview_data(cfg, sessions_dir=self.sessions,
                                     proc_root=self.proc)
        self.assertIs(data["sessions"][0].get("unwatched"), True)
        # Ambiguous lease read (symlink): proves nothing — no flag.
        lease_dir = os.path.join(state_root, "managed", "leases")
        os.makedirs(lease_dir)
        os.symlink("/nonexistent", os.path.join(
            lease_dir, "session-sess-live-1234.json"))
        data = managed.overview_data(cfg, sessions_dir=self.sessions,
                                     proc_root=self.proc)
        self.assertNotIn("unwatched", data["sessions"][0])
        # Dead session (registry entry, no /proc): liveness False —
        # never flag a corpse, however high its lingering state reads.
        self._entry(30, "sess-dead-1234", 3030)
        with open(os.path.join(sdir, "sess-dead-1234.json"), "w") as fh:
            json.dump({"model": "m", "current": 190_000,
                       "peak": 190_000}, fh)
        data = managed.overview_data(cfg, sessions_dir=self.sessions,
                                     proc_root=self.proc)
        dead = [s for s in data["sessions"]
                if s["session_id"] == "sess-dead-1234"][0]
        self.assertIs(dead["session_live"], False)
        self.assertNotIn("unwatched", dead)

    def test_liveness_unjudgeable_returns_incomplete(self):
        ids, complete = managed.live_session_ids(
            os.path.join(self.temp.name, "missing"), self.proc)
        self.assertEqual((ids, complete), (set(), False))
        ids, complete = managed.live_session_ids(
            self.sessions, os.path.join(self.temp.name, "noproc"))
        self.assertEqual((ids, complete), (set(), False))

    def test_liveness_partial_scan_reads_incomplete_not_dead(self):
        write_proc(self.proc, 20, 20, 2020)
        self._entry(20, "sess-live-1234", 2020)
        # an unreadable registry entry could name ANY session -> the
        # scan is incomplete and absence proves nothing
        with open(os.path.join(self.sessions, "50.json"), "w") as fh:
            fh.write("not json")
        ids, complete = managed.live_session_ids(self.sessions, self.proc)
        self.assertEqual(ids, {"sess-live-1234"})
        self.assertFalse(complete)

    def test_liveness_scan_bound_reads_incomplete(self):
        for pid in (20, 30, 40):
            write_proc(self.proc, pid, pid, pid * 100)
            self._entry(pid, "sess-p%d-1234" % pid, pid * 100)
        ids, complete = managed.live_session_ids(
            self.sessions, self.proc, limit=2)
        self.assertFalse(complete)
        self.assertEqual(len(ids), 2)

    def test_liveness_unreadable_proc_stat_is_unknown_not_live(self):
        # /proc/<pid> exists but stat is unopenable (a directory here):
        # start-time proof impossible -> neither live nor dead
        os.makedirs(os.path.join(self.proc, "70", "stat"))
        self._entry(70, "sess-hidn-1234", 7070)
        ids, complete = managed.live_session_ids(self.sessions, self.proc)
        self.assertEqual(ids, set())
        self.assertFalse(complete)

    def test_liveness_absurd_numeric_filename_skipped(self):
        # A digit stem beyond CPython's int-conversion limit (~4300)
        # would raise from int(); NAME_MAX prevents creating one on
        # disk, so inject via listdir. The guard skips any stem over 9
        # chars outright — no crash, no scan-bound cost, completeness
        # intact. (Also a zombie/dead-state pid is never live.)
        from unittest import mock
        write_proc(self.proc, 20, 20, 2020)
        self._entry(20, "sess-live-1234", 2020)
        write_proc(self.proc, 60, 60, 6060, state="X")
        self._entry(60, "sess-xdea-1234", 6060)
        names = ["9" * 5000 + ".json", "20.json", "60.json"]
        with mock.patch.object(managed.os, "listdir",
                               return_value=names):
            ids, complete = managed.live_session_ids(
                self.sessions, self.proc, limit=2)
        self.assertEqual(ids, {"sess-live-1234"})
        self.assertTrue(complete)

    def test_overview_carries_session_live_tristate(self):
        cfg = dict(cm._DEFAULTS, mode="managed", state_dir=self.temp.name,
                   context_window=200_000)
        state_dir = os.path.join(self.temp.name, "state")
        os.makedirs(state_dir)
        now = time.time()
        for sid in ("sess-live-1234", "sess-dead-1234"):
            with open(os.path.join(state_dir, sid + ".json"), "w") as fh:
                json.dump({"model": "claude-fable-5", "current": 1000,
                           "peak": 1000}, fh)
        write_proc(self.proc, 20, 20, 2020)
        self._entry(20, "sess-live-1234", 2020)
        data = managed.overview_data(cfg, now=now,
                                     sessions_dir=self.sessions,
                                     proc_root=self.proc)
        rows = {s["session_id"]: s for s in data["sessions"]}
        self.assertIs(rows["sess-live-1234"]["session_live"], True)
        self.assertIs(rows["sess-dead-1234"]["session_live"], False)
        json.loads(json.dumps(data))  # stays strict JSON
        out = managed.overview_text(cfg, now=now,
                                    sessions_dir=self.sessions,
                                    proc_root=self.proc)
        self.assertIn("alive", out)
        live_row = [l for l in out.splitlines() if "sess-liv" in l][0]
        self.assertEqual(live_row.split()[-1], "live")
        dead_row = [l for l in out.splitlines() if "sess-dea" in l][0]
        self.assertEqual(dead_row.split()[-1], "GONE")
        self.assertIn("(GONE = no live claude process", out)
        self.assertNotIn("alive=?", out)

    def test_overview_text_unreadable_row_verdict_aligns(self):
        # No real fixture reaches the unreadable branch since the
        # loader hardening, so stub the data layer: the renderer must
        # keep an unreadable row's alive verdict under its header.
        from unittest import mock
        cfg = dict(cm._DEFAULTS, mode="managed", state_dir=self.temp.name,
                   context_window=200_000)
        data = {"schema": 1, "generated_at": 0, "mode": "managed",
                "context_window": 200_000, "soft_pct": 0.7,
                "hard_pct": 0.8, "managed_trigger_pct": 0.8,
                "models": {}, "watchers": [],
                "sessions": [
                    {"session_id": "sess-good-1234", "session_live": True,
                     "model": "m", "current": 1000, "peak": 1000,
                     "window": 200_000, "pct": 0.5, "updated_epoch": 0,
                     "age_s": 5},
                    {"session_id": "sess-unrd-1234",
                     "session_live": False, "unreadable": True}]}
        with mock.patch.object(managed, "overview_data",
                               return_value=data):
            out = managed.overview_text(cfg)
        header = [l for l in out.splitlines() if "updated  alive" in l][0]
        unread = [l for l in out.splitlines()
                  if "unreadable state row" in l][0]
        self.assertEqual(unread.split()[-1], "GONE")
        self.assertEqual(unread.rindex("GONE"), header.rindex("alive"))

    def test_overview_liveness_unknown_renders_question_mark(self):
        cfg = dict(cm._DEFAULTS, mode="managed", state_dir=self.temp.name,
                   context_window=200_000)
        state_dir = os.path.join(self.temp.name, "state")
        os.makedirs(state_dir)
        with open(os.path.join(state_dir, "sess-any-1234.json"), "w") as fh:
            json.dump({"model": "claude-fable-5", "current": 1000,
                       "peak": 1000}, fh)
        data = managed.overview_data(
            cfg, sessions_dir=os.path.join(self.temp.name, "missing"),
            proc_root=self.proc)
        self.assertIsNone(data["sessions"][0]["session_live"])
        out = managed.overview_text(
            cfg, sessions_dir=os.path.join(self.temp.name, "missing"),
            proc_root=self.proc)
        row = [l for l in out.splitlines() if "sess-any" in l][0]
        self.assertEqual(row.split()[-1], "?")
        self.assertIn("alive=?", out)
        self.assertNotIn("(GONE =", out)


class ThresholdStampTests(unittest.TestCase):
    """Advisor-stamped effective thresholds flow through the overview."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.cfg = dict(cm._DEFAULTS, mode="managed",
                        state_dir=self.temp.name, context_window=200_000)
        self.state_dir = os.path.join(self.temp.name, "state")
        os.makedirs(self.state_dir)
        self.proc = os.path.join(self.temp.name, "proc")
        self.sessions = os.path.join(self.temp.name, "sessions")
        os.makedirs(self.proc)
        os.makedirs(self.sessions)

    def _state(self, sid, extra):
        with open(os.path.join(self.state_dir, sid + ".json"), "w") as fh:
            json.dump(dict({"model": "claude-fable-5", "current": 120_000,
                            "peak": 120_000}, **extra), fh)

    def test_stamped_thresholds_override_readout_config(self):
        # A session running with env overrides stamps its own numbers;
        # the overview must prefer them over its own (different) config.
        self._state("sess-stmp-1234",
                    {"eff_window": 1_000_000, "eff_soft_pct": 0.5,
                     "eff_hard_pct": 0.6, "eff_trigger_pct": 0.55})
        data = managed.overview_data(self.cfg, sessions_dir=self.sessions,
                                     proc_root=self.proc)
        row = data["sessions"][0]
        self.assertEqual(row["window"], 1_000_000)
        self.assertAlmostEqual(row["pct"], 12.0)  # 120k of 1M, not 200k
        self.assertEqual((row["soft_pct"], row["hard_pct"],
                          row["trigger_pct"]), (0.5, 0.6, 0.55))
        out = managed.overview_text(self.cfg, sessions_dir=self.sessions,
                                    proc_root=self.proc)
        line = [l for l in out.splitlines() if "sess-stm" in l][0]
        self.assertIn("12.0%", line)
        self.assertIn("55%", line)  # trig column carries the stamp
        self.assertIn("trig", out)

    def test_override_file_beats_stale_stamps(self):
        # A mid-session `override` write must read truthfully at once,
        # not wait for the advisor's next restamp.
        self._state("sess-ovrd-1234",
                    {"eff_window": 200_000, "eff_soft_pct": 0.7,
                     "eff_hard_pct": 0.8, "eff_trigger_pct": 0.8})
        d = os.path.join(self.temp.name, "overrides")
        os.makedirs(d)
        with open(os.path.join(d, "sess-ovrd-1234.json"), "w") as fh:
            json.dump({"managed_trigger_pct": 0.6, "soft_pct": 0.5,
                       "hard_pct": 0.55, "context_window": 150_000}, fh)
        data = managed.overview_data(self.cfg, sessions_dir=self.sessions,
                                     proc_root=self.proc)
        row = data["sessions"][0]
        self.assertEqual(row["window"], 150_000)
        self.assertAlmostEqual(row["pct"], 80.0)  # 120k of 150k
        self.assertEqual((row["soft_pct"], row["hard_pct"],
                          row["trigger_pct"]), (0.5, 0.55, 0.6))
        # --clear (file removal) reads truthfully immediately too: the
        # stamps are the base values, so nothing override-flavored can
        # linger after the file is gone (audit finding).
        os.unlink(os.path.join(d, "sess-ovrd-1234.json"))
        data = managed.overview_data(self.cfg, sessions_dir=self.sessions,
                                     proc_root=self.proc)
        row = data["sessions"][0]
        self.assertEqual(row["window"], 200_000)
        self.assertEqual((row["soft_pct"], row["hard_pct"],
                          row["trigger_pct"]), (0.7, 0.8, 0.8))

    def test_partial_override_on_unstamped_row_reclamps(self):
        # Only soft overridden, above the config hard: the overlay
        # applies the same soft<=hard clamp as everything else, and the
        # untouched values fall back to the readout's derivation.
        self._state("sess-part-1234", {})
        d = os.path.join(self.temp.name, "overrides")
        os.makedirs(d)
        with open(os.path.join(d, "sess-part-1234.json"), "w") as fh:
            json.dump({"soft_pct": 0.9}, fh)
        data = managed.overview_data(self.cfg, sessions_dir=self.sessions,
                                     proc_root=self.proc)
        row = data["sessions"][0]
        self.assertEqual((row["soft_pct"], row["hard_pct"]), (0.8, 0.8))
        self.assertEqual(row["window"], 200_000)
        self.assertEqual(row["trigger_pct"], 0.8)

    def test_unstamped_and_garbage_stamps_fall_back(self):
        self._state("sess-none-1234", {})  # pre-stamp state file
        self._state("sess-junk-1234",
                    {"eff_window": "big", "eff_soft_pct": 7,
                     "eff_hard_pct": -1, "eff_trigger_pct": float("nan")})
        data = managed.overview_data(self.cfg, sessions_dir=self.sessions,
                                     proc_root=self.proc)
        for row in data["sessions"]:
            self.assertEqual(row["window"], 200_000)  # readout config
            self.assertAlmostEqual(row["pct"], 60.0)
            self.assertEqual((row["soft_pct"], row["hard_pct"],
                              row["trigger_pct"]), (0.7, 0.8, 0.8))
        json.loads(json.dumps(data))


DIM = "\x1b[2m"
RESET = "\x1b[0m"
FIXTURES = os.path.join(HERE, "fixtures")


class PaneParserTests(unittest.TestCase):
    """parse_pane / snapshot_exact against synthetic lines and the live
    fixtures captured 2026-08-18 (Claude Code 2.1.x, tmux)."""

    def test_bare_composer_is_empty(self):
        self.assertEqual(managed.parse_pane("x\n❯ ")["composer"], "empty")

    def test_ghost_suggestion_is_empty(self):
        # Suggestions render dim; plain-stripped they are byte-identical
        # to typed text, so only the SGR-aware parse may call them empty.
        cap = "header\n❯ %scheck if it's still running%s" % (DIM, RESET)
        self.assertEqual(managed.parse_pane(cap)["composer"], "empty")

    def test_per_word_dim_wrap_is_empty(self):
        # Narrow panes wrap the suggestion per word with PLAIN spaces
        # between the dim words (live fixture): whitespace need not be
        # dim, every other character must be.
        cap = "❯ %sIs%s %sit%s %srunning?%s" % (
            DIM, RESET, DIM, RESET, DIM, RESET)
        self.assertEqual(managed.parse_pane(cap)["composer"], "empty")

    def test_typed_text_is_nonempty(self):
        self.assertEqual(
            managed.parse_pane("❯ half typed")["composer"], "nonempty")

    def test_typed_after_ghost_is_nonempty(self):
        cap = "❯ %sghost%s typed" % (DIM, RESET)
        self.assertEqual(managed.parse_pane(cap)["composer"], "nonempty")

    def test_sgr22_ends_dim(self):
        cap = "❯ %sa\x1b[22mb" % DIM
        self.assertEqual(managed.parse_pane(cap)["composer"], "nonempty")

    def test_unknown_escape_is_unknown(self):
        for cap in ("❯ \x1b[2Ax", "❯ \x1b]0;titlex", "❯ \x9bmx"):
            self.assertEqual(managed.parse_pane(cap)["composer"], "unknown")

    def test_ascii_space_marker_is_unknown(self):
        # A shell prompt or history echo ("❯ text", ASCII space) must
        # never classify as a live composer.
        self.assertEqual(managed.parse_pane("❯ text")["composer"], "unknown")

    def test_modal_selection_row_flags_modal(self):
        snap = managed.parse_pane("stuff\n ❯ 1. Yes\n   2. No")
        self.assertTrue(snap["modal"])

    def test_column_zero_history_echo_is_not_modal(self):
        # A user prompt beginning "1. " echoes at column 0; calling it
        # a modal would starve the watcher forever (audit finding).
        snap = managed.parse_pane("❯ 1. first item of my list\n❯\u00a0")
        self.assertFalse(snap["modal"])
        self.assertEqual(snap["composer"], "empty")

    def test_extended_color_params_are_not_dim(self):
        # 38;5;2 / 38;2;r;g;b operands must not read as SGR 2 (audit
        # blocker: colored typed text classified empty).
        for cap in ("❯\u00a0\x1b[38;5;2mtyped",
                    "❯\u00a0\x1b[38;2;10;20;30mtyped",
                    "❯\u00a0\x1b[48;5;22mtyped"):
            self.assertEqual(managed.parse_pane(cap)["composer"],
                             "nonempty", cap)
        # dim + extended color together still reads dim.
        self.assertEqual(managed.parse_pane(
            "❯\u00a0\x1b[2;38;5;196mghost")["composer"], "empty")
        # malformed extended color is ambiguous -> unknown.
        self.assertEqual(managed.parse_pane(
            "❯\u00a0\x1b[38mx")["composer"], "unknown")

    def test_bottom_anchored_takes_last_marker_line(self):
        # History echoes its prompts with "❯ "; only the LAST marker
        # line is the live composer.
        cap = "❯ old prompt echoed\n● reply\n❯ "
        snap = managed.parse_pane(cap)
        self.assertEqual(snap["composer"], "empty")
        self.assertEqual(snap["row"], 2)

    def test_absent_composer(self):
        snap = managed.parse_pane("no prompt here\nat all")
        self.assertEqual(snap["composer"], "absent")

    def _fixture(self, name):
        with open(os.path.join(FIXTURES, name)) as fh:
            return fh.read()

    def test_live_suggestion_fixtures_are_empty(self):
        for name in ("bg-suggestion.ansi.txt",
                     "bg-suggestion-narrow60.ansi.txt"):
            snap = managed.parse_pane(self._fixture(name))
            self.assertEqual(snap["composer"], "empty", name)
            self.assertFalse(snap["modal"], name)

    def test_live_half_typed_fixture_is_nonempty(self):
        snap = managed.parse_pane(
            self._fixture("half-typed-bg-running.ansi.txt"))
        self.assertEqual(snap["composer"], "nonempty")

    def test_live_modal_fixture_vetoes(self):
        snap = managed.parse_pane(self._fixture("permission-modal.ansi.txt"))
        self.assertTrue(snap["modal"])
        self.assertNotEqual(snap["composer"], "empty")

    def test_live_foreground_generating_plain_fixture(self):
        # Foreground generation renders a byte-identical empty composer,
        # and this live capture caught the footer with NO "esc to
        # interrupt" (it flickers off between repaints): the strict
        # lane's R2 veto is NOT reliable mid-generation — its real
        # guards are R3 whole-pane stability (the spinner's elapsed
        # counter repaints every second) and, for the boundary lane,
        # the activity marker still reading "running".
        cap = self._fixture("foreground-generating.txt")
        snap = managed.parse_pane(cap)
        self.assertEqual(snap["composer"], "empty")
        self.assertFalse(snap["modal"])


class ActivityMarkerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.cfg = managed.load_config(
            base=dict(cm._DEFAULTS, state_dir=self.temp.name), environ={})
        self.sid = "session-1234"
        self.transcript = os.path.join(self.temp.name, "t.jsonl")
        with open(self.transcript, "w") as fh:
            fh.write("{}\n")

    def payload(self, prompt_id, **kw):
        value = {"session_id": self.sid, "prompt_id": prompt_id,
                 "transcript_path": self.transcript}
        value.update(kw)
        return value

    def read(self):
        return managed.read_activity(self.cfg, self.sid, self.transcript)

    def test_pairing_happy_path(self):
        self.assertTrue(managed.write_activity(
            self.cfg, self.payload("prompt-aaa1"), "running"))
        self.assertTrue(managed.write_activity(
            self.cfg, self.payload("prompt-aaa1"), "ended"))
        phase, revision = self.read()
        self.assertEqual(phase, "ended")
        self.assertEqual(revision[0], "prompt-aaa1")

    def test_missing_files_unknown_then_running(self):
        # No evidence at all: unknown. A valid bound running half with
        # no ended half is AFFIRMATIVE turn-in-flight evidence: it
        # vetoes even the strict lane (audit blocker fix).
        self.assertEqual(self.read(), ("unknown", None))
        managed.write_activity(self.cfg, self.payload("prompt-aaa1"), "running")
        self.assertEqual(self.read(), ("running", None))

    def test_new_running_unpairs_old_ended(self):
        # The stale-Stop interleaving that blocked the single-file
        # protocol: ended(A) landing while running(B) exists must not
        # read as ended.
        managed.write_activity(self.cfg, self.payload("prompt-aaa1"), "running")
        managed.write_activity(self.cfg, self.payload("prompt-aaa1"), "ended")
        managed.write_activity(self.cfg, self.payload("prompt-bbb2"), "running")
        self.assertEqual(self.read(), ("running", None))

    def test_write_refuses_bad_payloads(self):
        for bad in ({}, {"session_id": self.sid},
                    self.payload("x"),                      # short prompt_id
                    self.payload("prompt-aaa1", session_id="bad/../id"),
                    dict(self.payload("prompt-aaa1"), transcript_path=None)):
            self.assertFalse(managed.write_activity(self.cfg, bad, "running"))
        self.assertFalse(managed.write_activity(
            self.cfg, self.payload("prompt-aaa1"), "elsewhere"))

    def test_reader_rejects_corrupt_and_hostile_files(self):
        managed.write_activity(self.cfg, self.payload("prompt-aaa1"), "running")
        managed.write_activity(self.cfg, self.payload("prompt-aaa1"), "ended")
        paths = managed.activity_paths(self.cfg, self.sid)
        # An unusable ended half never pairs; the still-valid running
        # half reads as affirmative turn-in-flight.
        with open(paths["ended"], "w") as fh:
            fh.write("{nope")
        self.assertEqual(self.read(), ("running", None))
        with open(paths["ended"], "w") as fh:
            fh.write("x" * (managed.ACTIVITY_MAX_BYTES + 10))
        self.assertEqual(self.read(), ("running", None))
        os.unlink(paths["ended"])
        target = os.path.join(self.temp.name, "target.json")
        with open(target, "w") as fh:
            fh.write("{}")
        os.symlink(target, paths["ended"])
        self.assertEqual(self.read(), ("running", None))
        # A corrupt RUNNING half is not affirmative anything: unknown.
        os.unlink(paths["ended"])
        with open(paths["running"], "w") as fh:
            fh.write("{nope")
        self.assertEqual(self.read(), ("unknown", None))

    def test_reader_rejects_future_and_mismatched_records(self):
        managed.write_activity(self.cfg, self.payload("prompt-aaa1"), "running")
        managed.write_activity(self.cfg, self.payload("prompt-aaa1"), "ended")
        paths = managed.activity_paths(self.cfg, self.sid)
        with open(paths["ended"]) as fh:
            record = json.load(fh)
        # Each unusable/unbound ended half degrades to running (the
        # valid running half is affirmative), never to ended.
        managed._atomic_json(paths["ended"],
                             dict(record, written_at=time.time() + 3600))
        self.assertEqual(self.read(), ("running", None))
        managed._atomic_json(paths["ended"],
                             dict(record, session_id="session-9999"))
        self.assertEqual(self.read(), ("running", None))
        managed._atomic_json(paths["ended"],
                             dict(record, transcript_path="/elsewhere.jsonl"))
        self.assertEqual(self.read(), ("running", None))
        # ended OLDER than running: a stale Stop's write reordered
        # after a newer prompt — turn in flight.
        managed._atomic_json(paths["ended"],
                             dict(record, written_at=record["written_at"] - 60))
        self.assertEqual(self.read(), ("running", None))

    def test_reader_rejects_transcript_identity_change(self):
        managed.write_activity(self.cfg, self.payload("prompt-aaa1"), "running")
        managed.write_activity(self.cfg, self.payload("prompt-aaa1"), "ended")
        # A marker recorded against a different file identity (the
        # transcript was replaced) must not pair. Rewrite the recorded
        # inode rather than recreating the file — filesystems happily
        # reuse a just-freed inode.
        paths = managed.activity_paths(self.cfg, self.sid)
        with open(paths["ended"]) as fh:
            record = json.load(fh)
        managed._atomic_json(
            paths["ended"],
            dict(record, transcript_inode=record["transcript_inode"] + 1))
        self.assertEqual(self.read(), ("running", None))
        # And an identity-less record (stat failed at write time) is
        # not pairing evidence either (audit major).
        managed._atomic_json(
            paths["ended"],
            dict(record, transcript_device=None, transcript_inode=None))
        self.assertEqual(self.read(), ("running", None))

    def test_torn_read_degrades_to_unknown(self):
        # The audit-blocker interleaving: running A read; UPS installs
        # running B; ended A read. The coherence re-read of running
        # must skew and the result must never be "ended".
        self.assertTrue(managed.write_activity(
            self.cfg, self.payload("prompt-aaa1"), "running"))
        self.assertTrue(managed.write_activity(
            self.cfg, self.payload("prompt-aaa1"), "ended"))
        paths = managed.activity_paths(self.cfg, self.sid)
        real = managed._read_activity_file
        state = {"n": 0}

        def interleaved(path, session_id, now):
            record = real(path, session_id, now)
            state["n"] += 1
            if state["n"] == 1:
                # After the first (running) read, a new prompt lands.
                managed.write_activity(
                    self.cfg, self.payload("prompt-bbb2"), "running")
            return record

        import unittest.mock as mock
        with mock.patch.object(managed, "_read_activity_file",
                               side_effect=interleaved):
            phase, revision = managed.read_activity(
                self.cfg, self.sid, self.transcript)
        self.assertNotEqual(phase, "ended")

    def test_no_max_age_on_ended(self):
        managed.write_activity(self.cfg, self.payload("prompt-aaa1"), "running")
        managed.write_activity(self.cfg, self.payload("prompt-aaa1"), "ended")
        paths = managed.activity_paths(self.cfg, self.sid)
        for name in ("running", "ended"):
            with open(paths[name]) as fh:
                record = json.load(fh)
            managed._atomic_json(
                paths[name], dict(record, written_at=time.time() - 7200))
        phase, _ = self.read()
        self.assertEqual(phase, "ended")


BUSY_IDLE = "esc to interrupt\n❯ "


class BoundaryLaneTests(RunLadderTests):
    """run_ladder with the turn-boundary lane armed. Inherits the
    RunLadderTests fixtures; its inherited tests also re-run with the
    default strict-only arguments, pinning that the lane is opt-in."""

    def setUp(self):
        super().setUp()
        self.marker_payload = {"session_id": self.sid,
                               "prompt_id": "prompt-aaa1",
                               "transcript_path": self.transcript}

    def write_pair(self):
        managed.write_activity(self.cfg, self.marker_payload, "running")
        managed.write_activity(self.cfg, self.marker_payload, "ended")

    def run_boundary(self, tmux, packet=None, reader=None):
        reader = reader or (lambda: managed.read_activity(
            self.cfg, self.sid, self.transcript))
        return managed.run_ladder(
            self.binding, self.cfg, self.paths, self.attempt, self.cursor,
            self.paths["journal"], lambda: packet, tmux, self.proc,
            wait=lambda seconds: None, now_mono=lambda: 0.0,
            boundary_ok=True, activity_reader=reader,
            now_wall=lambda: time.time() + 5)

    def test_boundary_submits_through_busy_chrome(self):
        # The fbeb0bf1 starvation topology: busy footer, empty composer,
        # foreground turn provably ended.
        self.write_pair()
        tmux = LadderTmux([BUSY_IDLE, BUSY_IDLE, BUSY_IDLE,
                           BUSY_IDLE + self.text, BUSY_IDLE + self.text,
                           BUSY_IDLE])
        out = self.run_boundary(tmux)
        self.assertEqual(out["state"], "SUBMITTED")
        self.assertEqual(out.get("lane"), "boundary")
        self.assertEqual(len(tmux.enters()), 1)

    def test_boundary_accepts_ghost_suggestion(self):
        self.write_pair()
        ghost = "esc to interrupt\n❯ %ssuggested%s" % (DIM, RESET)
        tmux = LadderTmux([ghost, ghost, ghost, BUSY_IDLE + self.text,
                           BUSY_IDLE + self.text, BUSY_IDLE])
        out = self.run_boundary(tmux)
        self.assertEqual(out["state"], "SUBMITTED")

    def test_busy_chrome_defers_without_marker(self):
        tmux = LadderTmux([BUSY_IDLE])
        out = self.run_boundary(tmux)
        self.assertEqual(out["state"], "DEFERRED")
        self.assertEqual(out["reason"], "R2_activity_unknown")
        self.assertEqual(out.get("defer_class"), "opportunity")
        self.assertEqual(tmux.sent, [])

    def test_unpaired_marker_defers(self):
        self.write_pair()
        managed.write_activity(
            self.cfg, dict(self.marker_payload, prompt_id="prompt-bbb2"),
            "running")
        tmux = LadderTmux([BUSY_IDLE])
        out = self.run_boundary(tmux)
        self.assertEqual(out["state"], "DEFERRED")
        self.assertEqual(out["reason"], "R2_activity_running")

    def test_unsettled_marker_defers(self):
        self.write_pair()
        tmux = LadderTmux([BUSY_IDLE])
        out = managed.run_ladder(
            self.binding, self.cfg, self.paths, self.attempt, self.cursor,
            self.paths["journal"], lambda: None, tmux, self.proc,
            wait=lambda seconds: None, now_mono=lambda: 0.0,
            boundary_ok=True,
            activity_reader=lambda: managed.read_activity(
                self.cfg, self.sid, self.transcript),
            now_wall=time.time)  # real clock: marker written <1s ago
        self.assertEqual(out["state"], "DEFERRED")
        self.assertEqual(out["reason"], "R2_activity_unsettled")

    def test_half_typed_defers_in_boundary_lane(self):
        self.write_pair()
        tmux = LadderTmux([BUSY_IDLE + "half typed"])
        out = self.run_boundary(tmux)
        self.assertEqual(out["state"], "DEFERRED")
        self.assertEqual(out["reason"], "R2_composer_nonempty")
        self.assertEqual(tmux.sent, [])

    def test_modal_defers_in_boundary_lane(self):
        self.write_pair()
        tmux = LadderTmux([" ❯ 1. Yes\n❯ "])
        out = self.run_boundary(tmux)
        self.assertEqual(out["state"], "DEFERRED")
        self.assertEqual(out["reason"], "R2_modal")
        self.assertEqual(tmux.sent, [])

    def test_busy_chrome_still_defers_without_boundary_authorization(self):
        # boundary_ok=False (below hard, no request): unchanged contract.
        self.write_pair()
        tmux = LadderTmux([BUSY_IDLE])
        out = self.run_ladder(tmux)
        self.assertEqual(out["state"], "DEFERRED")
        self.assertEqual(out["reason"], "R2_not_idle")

    def test_marker_flip_before_typing_defers(self):
        # A new prompt starting between preflight and R4 must close the
        # window BEFORE the first byte.
        self.write_pair()
        revisions = [self.read_revision()] * 2 + [("unknown", None)]
        reader = lambda: revisions.pop(0) if revisions else ("unknown", None)
        tmux = LadderTmux([BUSY_IDLE, BUSY_IDLE, BUSY_IDLE])
        out = self.run_boundary(tmux, reader=reader)
        self.assertEqual(out["state"], "DEFERRED")
        self.assertEqual(out["reason"], "R4_boundary_changed")
        self.assertEqual(tmux.sent, [])

    def test_marker_flip_after_typing_is_cleanup(self):
        # After typing, a changed marker means unprovable input state:
        # CLEANUP_REQUIRED, never Enter.
        self.write_pair()
        good = self.read_revision()
        revisions = [good, good, good, ("unknown", None)]
        reader = lambda: revisions.pop(0) if revisions else ("unknown", None)
        tmux = LadderTmux([BUSY_IDLE, BUSY_IDLE, BUSY_IDLE,
                           BUSY_IDLE + self.text, BUSY_IDLE + self.text])
        out = self.run_boundary(tmux, reader=reader)
        self.assertEqual(out["state"], "CLEANUP_REQUIRED")
        self.assertEqual(out["reason"], "R6_prime_activity_changed")
        self.assertEqual(tmux.enters(), [])

    def test_strict_lane_still_works_with_lane_armed(self):
        # No marker at all: quiet pane authorizes via strict as before.
        tmux = LadderTmux([IDLE, IDLE, IDLE + self.text, IDLE + self.text,
                           IDLE])
        out = self.run_boundary(tmux)
        self.assertEqual(out["state"], "SUBMITTED")
        self.assertEqual(out.get("lane"), "strict")

    def test_running_marker_vetoes_strict_lane(self):
        # Mid-generation the pane can look strict-idle (empty composer,
        # footer flicker); an affirmative running marker must veto it.
        managed.write_activity(self.cfg, self.marker_payload, "running")
        tmux = LadderTmux([IDLE])
        out = self.run_boundary(tmux)
        self.assertEqual(out["state"], "DEFERRED")
        self.assertEqual(out["reason"], "R2_activity_running")
        self.assertEqual(tmux.sent, [])

    def test_no_marker_evidence_keeps_strict_contract(self):
        # Sessions whose hooks never wrote markers (unknown phase) keep
        # the original hook-independent strict lane.
        tmux = LadderTmux([IDLE, IDLE, IDLE + self.text, IDLE + self.text,
                           IDLE])
        out = self.run_boundary(tmux)
        self.assertEqual(out["state"], "SUBMITTED")
        self.assertEqual(out.get("lane"), "strict")

    def read_revision(self):
        return managed.read_activity(self.cfg, self.sid, self.transcript)


class SchedulerTests(TickWiringTests):
    """Reason-aware defer scheduling, hard promotion, activity due-now,
    starvation attention, and the pending fast poll."""

    def make_deferred_watcher(self, defer_class):
        watcher = self.make_watcher([usage_row(50)], LadderTmux([IDLE]))
        watcher.cursor = managed.scan_cursor(
            watcher.cursor, self.transcript)
        watcher.attempt = managed.new_attempt(
            self.token, managed.generation(watcher.cursor), 0, 0.0, "boot",
            "request")
        watcher.attempt["state"] = "DEFERRED"
        watcher.attempt["reason"] = "R2_activity_unknown"
        watcher.attempt["defer_class"] = defer_class
        return watcher

    def test_opportunity_defer_never_grows_backoff(self):
        watcher = self.make_deferred_watcher("opportunity")
        watcher.attempt["state"] = "DEFERRED"
        before = watcher.backoff
        watcher._schedule_defer(100.0)
        self.assertEqual(watcher.backoff, before)
        self.assertEqual(watcher.attempt["timers"]["next_attempt_at"],
                         100.0 + watcher.cfg["managed_poll_s"])

    def test_structural_defer_keeps_exponential_backoff(self):
        watcher = self.make_deferred_watcher("structural")
        before = watcher.backoff
        watcher._schedule_defer(100.0)
        self.assertEqual(watcher.attempt["timers"]["next_attempt_at"],
                         100.0 + before)
        self.assertEqual(watcher.backoff,
                         min(managed.BACKOFF_MAX_S, before * 2))

    def test_new_ended_marker_makes_attempt_due(self):
        watcher = self.make_deferred_watcher("opportunity")
        watcher.attempt["activity_rev_at_defer"] = None
        self.assertFalse(watcher._activity_advanced())  # no marker yet
        payload = {"session_id": self.sid, "prompt_id": "prompt-aaa1",
                   "transcript_path": self.transcript}
        managed.write_activity(self.cfg, payload, "running")
        managed.write_activity(self.cfg, payload, "ended")
        self.assertTrue(watcher._activity_advanced())

    def test_same_revision_is_not_due(self):
        # Watcher (and transcript) first: markers stat the transcript
        # at write time, and identity-less markers never pair.
        watcher = self.make_deferred_watcher("opportunity")
        payload = {"session_id": self.sid, "prompt_id": "prompt-aaa1",
                   "transcript_path": self.transcript}
        managed.write_activity(self.cfg, payload, "running")
        managed.write_activity(self.cfg, payload, "ended")
        phase, revision = watcher._activity_reader()
        watcher.attempt["activity_rev_at_defer"] = list(revision)
        self.assertFalse(watcher._activity_advanced())

    def test_request_attempt_is_boundary_authorized_below_hard(self):
        watcher = self.make_deferred_watcher("opportunity")
        self.assertTrue(watcher._boundary_ok())

    def test_threshold_attempt_promotes_at_hard(self):
        watcher = self.make_deferred_watcher("opportunity")
        watcher.attempt["trigger_source"] = "threshold"
        watcher.cursor["current"] = int(0.5 * 200_000)
        self.assertFalse(watcher._boundary_ok())
        watcher.cursor["current"] = int(0.85 * 200_000)
        self.assertTrue(watcher._boundary_ok())

    def test_starvation_alert_fires_once_and_is_informational(self):
        watcher = self.make_deferred_watcher("opportunity")
        watcher._maybe_starvation_alert(0.0)  # starts the eligibility clock
        self.assertEqual(watcher.attempt["timers"].get("eligible_mono"), 0.0)
        watcher._maybe_starvation_alert(managed.STARVATION_ALERT_S + 1)
        watcher._maybe_starvation_alert(managed.STARVATION_ALERT_S + 2)
        records = [r for r in managed.read_journal(self.paths["journal"])
                   if r.get("state") == "STARVATION_ALERT"]
        self.assertEqual(len(records), 1)
        self.assertGreaterEqual(records[0].get("starved_for_s", 0),
                                managed.STARVATION_ALERT_S)
        # Not an attempt state: recovery must not resurrect it.
        self.assertNotIn("STARVATION_ALERT", managed.ATTEMPT_STATES)
        self.assertIn("STARVATION_ALERT", managed.LIFECYCLE_STATES)
        # The attempt keeps retrying: no latch.
        self.assertEqual(watcher.attempt["state"], "DEFERRED")

    def test_starvation_alert_requires_eligibility(self):
        watcher = self.make_deferred_watcher("opportunity")
        watcher.attempt["trigger_source"] = "threshold"
        watcher.cursor["current"] = 100  # far below hard
        watcher._maybe_starvation_alert(0.0)
        watcher._maybe_starvation_alert(managed.STARVATION_ALERT_S + 1)
        records = [r for r in managed.read_journal(self.paths["journal"])
                   if r.get("state") == "STARVATION_ALERT"]
        self.assertEqual(records, [])

    def test_trigger_source_survives_journal_roundtrip(self):
        watcher = self.make_deferred_watcher("opportunity")
        managed.journal_record(self.paths["journal"], "DEFERRED",
                               watcher.attempt, reason="R2_activity_unknown")
        recovered = managed.recover_attempt(self.paths["journal"], "boot")
        self.assertEqual(recovered.get("trigger_source"), "request")


class RoundTwoFixTests(unittest.TestCase):
    """Pins for the round-2 audit fixes (virgin markers, SGR arity,
    eligibility clock, starvation recovery, poll interval)."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.cfg = managed.load_config(
            base=dict(cm._DEFAULTS, state_dir=self.temp.name), environ={})
        self.sid = "session-1234"
        self.transcript = os.path.join(self.temp.name, "t.jsonl")

    def payload(self, prompt_id):
        return {"session_id": self.sid, "prompt_id": prompt_id,
                "transcript_path": self.transcript}

    def read(self):
        return managed.read_activity(self.cfg, self.sid, self.transcript)

    def test_virgin_running_marker_is_affirmative_running(self):
        # First prompt of a session: the hook fires BEFORE the
        # transcript file exists, so the running marker carries
        # (None, None) identity. That must still veto (phase running),
        # not fall to unknown (round-2 audit blocker: strict lane could
        # type mid-first-turn).
        managed.write_activity(self.cfg, self.payload("prompt-aaa1"),
                               "running")
        with open(self.transcript, "w") as fh:
            fh.write("{}\n")
        self.assertEqual(self.read(), ("running", None))

    def test_virgin_running_pairs_with_bound_ended(self):
        # ...and when the first turn ends (transcript now exists, ended
        # is fully bound), the identity-less running half may pair — the
        # boundary lane must not starve until a second prompt.
        managed.write_activity(self.cfg, self.payload("prompt-aaa1"),
                               "running")
        with open(self.transcript, "w") as fh:
            fh.write("{}\n")
        managed.write_activity(self.cfg, self.payload("prompt-aaa1"),
                               "ended")
        phase, revision = self.read()
        self.assertEqual(phase, "ended")
        self.assertEqual(revision[0], "prompt-aaa1")

    def test_mismatched_running_identity_is_unknown(self):
        with open(self.transcript, "w") as fh:
            fh.write("{}\n")
        managed.write_activity(self.cfg, self.payload("prompt-aaa1"),
                               "running")
        paths = managed.activity_paths(self.cfg, self.sid)
        with open(paths["running"]) as fh:
            record = json.load(fh)
        managed._atomic_json(
            paths["running"],
            dict(record, transcript_inode=record["transcript_inode"] + 1))
        self.assertEqual(self.read(), ("unknown", None))

    def test_truncated_extended_color_fails_closed(self):
        # "38;5" / "38;2;r;g" truncations are ambiguous: unknown, never
        # a dim misread (round-2 audit: ESC[2;38;5m read typed as empty).
        for cap in ("❯ \x1b[2;38;5mtyped",
                    "❯ \x1b[38;5mtyped",
                    "❯ \x1b[38;2;1;2mtyped",
                    "❯ \x1b[58;9mtyped",
                    # EMPTY operands are just as ambiguous as missing
                    # ones (a terminal may read a trailing ";" as an
                    # extra reset param) — round-4 Sol catch.
                    "❯ \x1b[2;38;5;mtyped",
                    "❯ \x1b[38;5;;2mtyped",
                    "❯ \x1b[2;38;2;1;2;mtyped"):
            self.assertEqual(managed.parse_pane(cap)["composer"],
                             "unknown", cap)

    def test_colon_form_colors_do_not_affect_dim(self):
        self.assertEqual(managed.parse_pane(
            "❯ \x1b[38:5:2mtyped")["composer"], "nonempty")
        self.assertEqual(managed.parse_pane(
            "❯ \x1b[2m\x1b[38:5:2mghost")["composer"], "empty")


class RoundTwoWatcherTests(TickWiringTests):
    def make_request_watcher(self):
        watcher = self.make_watcher([usage_row(50)], LadderTmux([IDLE]))
        watcher.cursor = managed.scan_cursor(watcher.cursor, self.transcript)
        watcher.attempt = managed.new_attempt(
            self.token, managed.generation(watcher.cursor), 0, 0.0, "boot",
            "request")
        watcher.attempt["state"] = "DEFERRED"
        watcher.attempt["reason"] = "R2_activity_unknown"
        watcher.attempt["defer_class"] = "opportunity"
        return watcher

    def test_schedule_defer_starts_eligibility_clock(self):
        # The clock must start at the first ELIGIBLE defer, not one
        # poll later inside the alert check (round-2 audit).
        watcher = self.make_request_watcher()
        watcher._schedule_defer(50.0)
        self.assertEqual(watcher.attempt["timers"].get("eligible_mono"), 50.0)
        watcher._schedule_defer(60.0)  # never restarted
        self.assertEqual(watcher.attempt["timers"].get("eligible_mono"), 50.0)

    def test_starvation_flag_survives_recovery(self):
        # The alert re-journals the DEFERRED tail so a crashed watcher
        # does not re-alert the same attempt after recovery.
        watcher = self.make_request_watcher()
        watcher._maybe_starvation_alert(0.0)
        watcher._maybe_starvation_alert(managed.STARVATION_ALERT_S + 1)
        recovered = managed.recover_attempt(self.paths["journal"], "boot")
        self.assertTrue(recovered.get("starvation_alerted"))
        self.assertEqual(recovered.get("state"), "DEFERRED")

    def test_poll_interval_fast_for_all_pending_states(self):
        watcher = self.make_request_watcher()
        watcher.cursor["current"] = 100  # far below trigger
        for state in ("TRIGGERED", "DEFERRED", "SUBMITTED", "ACKED"):
            watcher.attempt["state"] = state
            self.assertEqual(watcher._poll_interval(),
                             watcher.cfg["managed_poll_s"], state)
        watcher.attempt = None
        self.assertEqual(watcher._poll_interval(), 60)


class RequestOverridesLatchTests(TickWiringTests):
    def latched_watcher(self):
        watcher = self.make_watcher([usage_row(50)], LadderTmux([IDLE]))
        watcher.cursor = managed.scan_cursor(watcher.cursor, self.transcript)
        # Post-compaction plateau: pct above the re-arm line with no new
        # growth, so the ordinary re-arm cannot fire.
        watcher.cursor["current"] = 180_000
        watcher.attempt = {
            "state": "LATCHED", "latch_kind": "THRESHOLD",
            "run_token": self.token,
            "generation": managed.generation(watcher.cursor),
            "latch_tokens": 180_000, "nonces": [], "nonce": "",
            "attempt_packet_seq_floor": 0, "retry_n": 0,
            "timers": {"boot_id": "boot"}}
        return watcher

    def test_threshold_latch_holds_without_request(self):
        watcher = self.latched_watcher()
        keep, reason = watcher.tick()
        self.assertEqual((keep, reason), (True, "latched"))
        self.assertEqual(watcher.attempt["state"], "LATCHED")

    def test_request_overrides_threshold_latch(self):
        # A model request during still_above_rearm_band must clear the
        # latch (live-suite catch: the request starved 250+s otherwise).
        watcher = self.latched_watcher()
        watcher.request_history["req-override-1"] = managed.generation_key(
            managed.generation(watcher.cursor))
        keep, reason = watcher.tick()
        self.assertEqual((keep, reason), (True, "latched"))
        self.assertIsNone(watcher.attempt)
        records = managed.read_journal(self.paths["journal"])
        ready = [r for r in records if r.get("state") == "READY"]
        self.assertTrue(ready)
        self.assertEqual(ready[-1].get("reason"), "request_overrides_latch")
        # Next tick creates the request-triggered attempt.
        keep, reason = watcher.tick()
        self.assertTrue(keep)
        self.assertIsNotNone(watcher.attempt)
        self.assertEqual(watcher.attempt.get("trigger_source"), "request")
