"""Unit tests for compact-manager Layer 2 managed mode."""
import io
import json
import os
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


def write_proc(root, pid, tpgid, start):
    directory = os.path.join(root, str(pid))
    os.makedirs(directory, exist_ok=True)
    # After comm: state(3), ppid(4), pgrp(5), session(6), tty_nr(7),
    # tpgid(8), then fields through starttime(22).
    tail = ["S", "1", str(tpgid), "1", "1", str(tpgid)]
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
        self.assertEqual(parsed, {"tpgid": 20, "starttime": 1010})

    def test_binding_walk_and_derived_transcript(self):
        binding, error = managed.build_binding(
            "sock", "%1", True, self.runner, self.proc, self.sessions,
            self.projects)
        self.assertIsNone(error)
        self.assertEqual(binding["claude_pid"], 20)
        self.assertEqual(binding["transcript_path"], self.transcript)
        self.assertEqual(binding["pane_root_start"], 1010)
        self.assertTrue(binding["attended"])

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
            "current", "boundary_count", "last_boundary", "anchor", "model"})
        self.assertEqual(set(value["anchor"]),
                         {"offset", "sha256_of_first_row"})

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

    def test_s6_capture_half_typed_predicate(self):
        capture_path = os.path.abspath(os.path.join(
            HERE, "..", "..", "..", "tools", "s6", "captures",
            "s6-results.jsonl"))
        with open(capture_path) as fh:
            rows = [json.loads(line) for line in fh]
        row = next(x for x in rows if x.get("scenario") == "f_half_typed")
        self.assertFalse(managed.composer_idle(row["composer_after"]))


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
