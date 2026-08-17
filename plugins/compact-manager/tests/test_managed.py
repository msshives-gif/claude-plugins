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

    def step(self, state, event, now=100, packet=None, gen=None):
        self.attempt["state"] = state
        return managed.transition_attempt(
            self.attempt, event, now, self.cfg, packet,
            self.gen if gen is None else gen)

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

    def test_own_nonce_ignores_sequence_floor(self):
        self.attempt["state"] = "SUBMISSION_UNCERTAIN"
        packet = {"seq": 0, "custom_instructions":
                  "[cm-%s] text" % self.attempt["nonce"]}
        out = managed.transition_attempt(self.attempt, "timer", 100,
                                         self.cfg, packet, self.gen)
        self.assertEqual(out["state"], "ACKED")

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
               "COMPACT_MANAGER_MANAGED_DEADLINE_HOURS": "1000",
               "COMPACT_MANAGER_MANAGED_PANE_COMMANDS": '["claude","cc"]'}
        cfg = managed.load_config(base=base, environ=env)
        self.assertEqual(cfg["managed_trigger_pct"], 0.8)
        self.assertEqual(cfg["managed_stable_ms"], 200)
        self.assertEqual(cfg["managed_poll_s"], 5)
        self.assertEqual(cfg["managed_ack_timeout_s"], 30)
        self.assertEqual(cfg["managed_completion_timeout_s"], 30)
        self.assertEqual(cfg["managed_deadline_hours"], 72)
        self.assertEqual(cfg["managed_pane_commands"], ["claude", "cc"])

    def test_instruction_is_fixed_and_metacharacter_free(self):
        text = managed.instruction_text("a" * 16)
        self.assertEqual(text, "/compact [cm-%s] Preserve the task list and "
                         "open decisions to the handoff file" % ("a" * 16))
        self.assertIn("[", text)  # pinned literal glob characters
        self.assertIn("]", text)
        for char in managed.INSTRUCTION_DENYLIST:
            self.assertNotIn(char, text, repr(char))

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
        self.assertTrue(managed.composer_exact("x\n❯\u00a0" + text, text))
        self.assertFalse(managed.composer_exact("x\n❯\u00a0BAD" + text, text))
        self.assertFalse(managed.composer_exact("x\n❯\u00a0" + text + "x", text))

    def test_trailing_capture_padding_is_ignored(self):
        # capture-pane -J preserves trailing spaces (live-gate catch):
        # the padded composer is still idle / still exact, while a
        # shell's "❯ "+padding (no NBSP) is still rejected.
        self.assertTrue(managed.composer_idle("❯ " + " " * 40))
        self.assertFalse(managed.composer_idle("❯" + " " * 40))
        text = managed.instruction_text("a" * 16)
        self.assertTrue(managed.composer_exact(
            "x\n❯ " + text + " " * 30, text))
        self.assertFalse(managed.composer_exact(
            "x\n❯ user " + text + " " * 30, text))

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

    def test_r6_own_late_packet_aborts_before_enter(self):
        tmux = LadderTmux([IDLE, IDLE, IDLE + self.text, IDLE + self.text])
        packet = {"seq": 7,
                  "custom_instructions": "[cm-%s]" % self.attempt["nonce"]}
        out = self.run_ladder(tmux, packet=packet)
        self.assertEqual(out["state"], "CLEANUP_REQUIRED")
        self.assertEqual(out["reason"], "R6_prime_own_packet_late")
        self.assertEqual(tmux.enters(), [])

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

    def make_watcher(self, rows, tmux):
        append_rows(self.transcript, rows)
        return managed.Watcher(self.binding, self.cfg, self.paths,
                               run_tmux=tmux, proc_root=self.proc,
                               wait=lambda seconds: None)

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
        # the transcript; it must not retire (deadline bounds the wait).
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


class WatcherDeadlineTests(unittest.TestCase):
    def test_deadline_expiry_retires_before_any_tmux_call(self):
        # The absolute deadline cannot be exercised live (1h floor); pin it
        # here: an expired watcher retires before touching tmux or leases.
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        cfg = managed.load_config(
            base=dict(cm._DEFAULTS, state_dir=temp.name), environ={})
        paths = managed.managed_paths(cfg, "session-1234", "sock", "%1")
        clock = iter([0.0, 0.0, cfg["managed_deadline_hours"] * 3600 + 1.0])
        calls = []

        def runner(argv, timeout=5):
            calls.append(argv)
            return Result("")
        binding = {"socket": "sock", "pane_id": "%1", "pane_tty": "t",
                   "pane_root_pid": 1, "pane_root_start": 1,
                   "claude_pid": 2, "claude_start": 2,
                   "session_id": "session-1234", "transcript_path": "/none",
                   "tmux_session_id": "$1", "run_token": "a" * 16,
                   "attended": True}
        watcher = managed.Watcher(binding, cfg, paths, run_tmux=runner,
                                  proc_root=temp.name,
                                  monotonic=lambda: next(clock))
        keep, reason = watcher.tick()
        self.assertEqual((keep, reason), (False, "deadline"))
        self.assertEqual(calls, [])


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
