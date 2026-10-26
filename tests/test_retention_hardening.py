"""Privacy retention, advisory bells, and HAM state isolation regressions."""
import tests.ham_test_support as ham_test_support

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock
import uuid

from ham.agents.request_lifecycle import JsonRequestStateStore, prune_request_records
from ham.ledger.store import LedgerStore, json_text, pending_outbox_backlog, queue_outbox

DAY = 86400


class RequestRetentionTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.store = JsonRequestStateStore(self.root / "requests")
        self.environment = {"HERDR_AGENT_REQUEST_STATE_DIR": str(self.store.directory)}
        self.now = time.time()

    def record(self, number, state="submitted_settled", terminal=True, age=31):
        request_id = f"{number:032x}"
        self.store.save(request_id, {"request_id": request_id, "state": state,
                                     "terminal": terminal, "_prompt": "private prompt"})
        path = self.store.directory / (request_id + ".json")
        os.utime(path, (self.now - age * DAY, self.now - age * DAY))
        return path

    def test_only_old_explicit_terminal_records_are_removed(self):
        expired = [self.record(1), self.record(2, "submission_failed"),
                   self.record(3, "submitted_unknown")]
        retained = [self.record(4, age=30), self.record(5, age=29),
                    self.record(6, "submitted_unknown", False),
                    self.record(7, "submitted_working", False),
                    self.record(8, "deferred", False),
                    self.record(9, "awaiting_decision", False),
                    self.record(10, "submitted_working", True),
                    self.record(11, terminal=None), self.record(12, state=None)]
        self.assertEqual(prune_request_records(self.environment, now=self.now), 3)
        self.assertTrue(all(not path.exists() for path in expired))
        self.assertTrue(all(path.exists() for path in retained))

    def test_locked_corrupt_and_symlink_records_do_not_block_other_pruning(self):
        locked = self.record(1)
        corrupt = self.record(2)
        corrupt.write_text("not json")
        os.utime(corrupt, (self.now - 31 * DAY,) * 2)
        outside = self.root / "outside.json"
        outside.write_text('{"terminal":true,"state":"submitted_settled"}')
        os.utime(outside, (self.now - 31 * DAY,) * 2)
        link = self.store.directory / ("3" * 32 + ".json")
        link.symlink_to(outside)
        removable = self.record(4)
        with self.store.lock(locked.stem):
            self.assertEqual(prune_request_records(self.environment, now=self.now), 1)
        self.assertTrue(locked.exists())
        self.assertTrue(corrupt.exists())
        self.assertTrue(outside.exists())
        self.assertTrue(link.is_symlink())
        self.assertFalse(removable.exists())

    def test_missing_queue_is_not_created(self):
        self.assertEqual(prune_request_records(self.environment, now=self.now), 0)
        self.assertFalse(self.store.directory.exists())


class LedgerRetentionTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.store = LedgerStore(self.root / "ledger", canonical_name="retention-test")
        self.now = time.time()

    def message(self, number, *, final_age=None):
        message_id, thread_id = str(uuid.UUID(int=number)), str(uuid.UUID(int=number + 100))
        with self.store.transaction() as connection:
            connection.execute("INSERT INTO threads VALUES (?,?,?)", (thread_id, "repo", "subject"))
            connection.execute(
                "INSERT INTO messages(message_id,thread_id,repo_id,sender_repo_id,origin_host,sender,"
                "sender_label,kind,hop,body,created_at,final_at,payload_hash) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (message_id, thread_id, "repo", "repo", "retention-test", "sender", "sender", "fyi", 0,
                 "private body", self.now - 100 * DAY,
                 None if final_age is None else self.now - final_age * DAY, "hash"))
            connection.execute("INSERT INTO dedupe_receipts VALUES (?,?,?,?,?)",
                               (message_id, "hash", "retention-test", json_text({"thread_id": thread_id}),
                                self.now - 100 * DAY))
        return message_id, thread_id

    def submission(self, number, acceptance, age, body="private submission"):
        with self.store.transaction() as connection:
            connection.execute(
                "INSERT INTO submissions(submission_id,via,acceptance,sender,sender_kind,target,body,"
                "body_sha256,created_at,updated_at) VALUES (?,'direct',?,'user','user','{}',?,'hash',?,?)",
                (str(number), acceptance, body, self.now - age * DAY, self.now - age * DAY))

    def dismissal(self, message_id):
        with self.store.transaction() as connection:
            fields = [row["name"] for row in connection.execute("PRAGMA table_info(mail_dismissals)")]
            values = {field: "fixture" for field in fields}
            values.update(message_id=message_id, dismissed_at=self.now - 100 * DAY, actor_generation=1)
            connection.execute(f"INSERT INTO mail_dismissals({','.join(fields)}) VALUES ({','.join('?' for _ in fields)})",
                               list(values.values()))

    def count(self, table):
        with self.store.read() as connection:
            return connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]

    def test_submissions_keep_boundary_recent_and_in_progress(self):
        for number, acceptance, age in ((1, "accepted", 31), (2, "failed", 31), (3, "unknown", 31),
                                         (4, "accepted", 30), (5, "accepted", 29), (6, "submitting", 100)):
            self.submission(number, acceptance, age)
        self.assertEqual(self.store.retention(now=self.now)["submissions"], 3)
        with self.store.read() as connection:
            self.assertEqual({row[0] for row in connection.execute("SELECT submission_id FROM submissions")},
                             {"4", "5", "6"})

    def test_body_expiry_keeps_replay_window_then_removes_orphan_metadata(self):
        expired, _ = self.message(1, final_age=91)
        active, _ = self.message(2)
        boundary, _ = self.message(3, final_age=90)
        self.dismissal(expired)
        first = self.store.retention(now=self.now)
        self.assertEqual(first["messages"], 1)
        self.assertEqual(self.count("dedupe_receipts"), 3)
        self.assertEqual(self.count("mail_dismissals"), 1)
        self.assertEqual(self.count("threads"), 3)
        self.store.retention(now=self.now + 30 * DAY)
        self.assertEqual(self.count("dedupe_receipts"), 3)  # exact replay boundary
        later = self.store.retention(now=self.now + 30 * DAY + 1)
        self.assertEqual(later["dedupe_receipts"], 1)
        self.assertEqual(later["mail_dismissals"], 1)
        with self.store.read() as connection:
            self.assertEqual({row[0] for row in connection.execute("SELECT message_id FROM dedupe_receipts")},
                             {active, boundary})
        self.assertEqual(self.count("threads"), 2)

    def test_pending_outbox_keeps_dedupe_and_dismissal_until_transport_finishes(self):
        message_id, _ = self.message(1, final_age=91)
        self.dismissal(message_id)
        with self.store.transaction() as connection:
            item_id = queue_outbox(connection, "peer", "receipt", {"created_at": self.now}, message_id)
        self.store.retention(now=self.now)
        self.store.retention(now=self.now + 31 * DAY)
        self.assertEqual(self.count("dedupe_receipts"), 1)
        self.assertEqual(self.count("mail_dismissals"), 1)
        self.assertEqual(self.count("outbox"), 1)
        with self.store.transaction() as connection:
            connection.execute("UPDATE outbox SET state='sent' WHERE item_id=?", (item_id,))
        self.store.retention(now=self.now + 31 * DAY + 1)
        self.assertEqual(self.count("dedupe_receipts"), 0)
        self.assertEqual(self.count("mail_dismissals"), 0)
        self.assertEqual(self.count("outbox"), 0)

    def test_secure_delete_clears_body_and_checkpoints_wal(self):
        marker = "PRIVATE_RETENTION_SENTINEL_" + uuid.uuid4().hex
        self.submission(1, "accepted", 31, marker * 1024)
        with self.store.read() as connection:
            self.assertEqual(connection.execute("PRAGMA secure_delete").fetchone()[0], 1)
        self.store.retention(now=self.now)
        self.assertNotIn(marker.encode(), self.store.path.read_bytes())
        wal = Path(str(self.store.path) + "-wal")
        self.assertTrue(not wal.exists() or wal.stat().st_size == 0)

    def test_withdrawal_tombstones_respect_age_and_pending_transport(self):
        old, boundary, pending = (str(uuid.UUID(int=number)) for number in (1, 2, 3))
        with self.store.transaction() as connection:
            for message_id, age in ((old, 31), (boundary, 30), (pending, 100)):
                connection.execute("INSERT INTO withdraw_tombstones VALUES (?,?,?)",
                                   (message_id, "retention-test", self.now - age * DAY))
            queue_outbox(connection, "peer", "withdraw", {"message_id": pending}, pending)
        self.store.retention(now=self.now)
        with self.store.read() as connection:
            self.assertEqual({row[0] for row in connection.execute("SELECT message_id FROM withdraw_tombstones")},
                             {boundary, pending})

    def test_orphan_outbox_enqueue_metadata_is_purged(self):
        with self.store.transaction() as connection:
            item_id = queue_outbox(connection, "peer", "setting", {"changed_at": self.now})
            connection.execute("DELETE FROM outbox WHERE item_id=?", (item_id,))
        self.store.retention(now=self.now)
        with self.store.read() as connection:
            self.assertIsNone(connection.execute("SELECT value FROM ledger_meta WHERE key=?",
                                                ("outbox_enqueued:" + item_id,)).fetchone())

    def test_backlog_uses_enqueue_time_not_retry_deadline_and_legacy_payload_age(self):
        with self.store.transaction() as connection:
            with mock.patch("ham.ledger.store.time.time", return_value=self.now - 90):
                item_id = queue_outbox(connection, "peer", "setting", {"changed_at": self.now - 1000})
            connection.execute("UPDATE outbox SET next_try_at=?", (self.now + 3600,))
            with mock.patch("ham.ledger.store.time.time", return_value=self.now):
                legacy_id = queue_outbox(connection, "legacy", "setting", {"changed_at": self.now - 120})
            connection.execute("DELETE FROM ledger_meta WHERE key=?", ("outbox_enqueued:" + legacy_id,))
            self.assertEqual(pending_outbox_backlog(connection, now=self.now),
                             {"peer": {"count": 1, "oldest_age_s": 90},
                              "legacy": {"count": 1, "oldest_age_s": 120}})
        self.assertEqual(self.store.doctor()["outbox_pending"]["peer"]["count"], 1)

    def test_empty_backlog_does_not_expand_doctor_output(self):
        self.assertNotIn("outbox_pending", self.store.doctor())


class BellRobustnessTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = LedgerStore(Path(directory.name) / "ledger", canonical_name="bells")

    def test_invalid_names_fail_only_after_valid_bells_ring(self):
        for name in ("", ".", "..", "/", "a/b", "bad\0name"):
            with self.subTest(name=name):
                good = self.store.bell_directory / "valid:terminal"
                good.unlink(missing_ok=True)
                with self.assertRaises(OSError):
                    self.store.ring([name, "valid:terminal"])
                self.assertTrue(good.is_file())
                self.assertTrue((self.store.bell_directory / "_board").is_file())

    def test_filesystem_failure_does_not_skip_remaining_bells(self):
        (self.store.bell_directory / "broken").mkdir()
        with self.assertRaises(OSError):
            self.store.ring(["broken", "working"])
        self.assertTrue((self.store.bell_directory / "working").is_file())


class HamStateGuardTest(unittest.TestCase):
    def test_roots_include_current_legacy_and_request_override(self):
        roots = ham_test_support.protected_state_directories({
            "HOME": "/fixture/home", "XDG_STATE_HOME": "/fixture/state",
            "HERDR_AGENT_REQUEST_STATE_DIR": "/fixture/requests",
        }, Path("/account"))
        self.assertIn(Path("/fixture/state/herdr-agent-messenger"), roots)
        self.assertIn(Path("/account/.local/state/herdr-agent-labels"), roots)
        self.assertIn(Path("/fixture/requests"), roots)

    def test_direct_requests_registry_and_delete_writes_trip_before_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            protected = root / "protected"
            protected.mkdir()
            existing = protected / "existing.json"
            existing.write_text("keep")
            environment = {**os.environ, ham_test_support.STATE_PROTECTION_ENV: json.dumps([str(protected)])}
            completed = subprocess.run([sys.executable, "-c", """
import tests.ham_test_support as safety
import os, pathlib, sys
from ham.agents.request_lifecycle import JsonRequestStateStore
root = pathlib.Path(sys.argv[1])
operations = [lambda: JsonRequestStateStore(root/'requests').save('a'*32, {'terminal': True}),
              lambda: (root/'registry.json').write_text('private'),
              lambda: (root/'existing.json').unlink(),
              lambda: os.replace(root/'existing.json', root/'renamed.json')]
for operation in operations:
    try:
        operation()
    except AssertionError as error:
        assert 'protected HAM state' in str(error), error
    else:
        raise AssertionError('write bypassed HAM state tripwire')
assert len(safety._violations) == len(operations), safety._violations
""", str(protected)], cwd=Path(__file__).parents[1], env=environment, capture_output=True, text=True)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(existing.read_text(), "keep")
            self.assertEqual(set(protected.iterdir()), {existing})

    def test_swallowed_request_violation_marks_test_failed(self):
        with tempfile.TemporaryDirectory() as directory:
            protected = Path(directory) / "protected"
            environment = {**os.environ, ham_test_support.STATE_PROTECTION_ENV: json.dumps([str(protected)])}
            completed = subprocess.run([sys.executable, "-c", """
import tests.ham_test_support
import pathlib, sys, unittest
from ham.agents.request_lifecycle import JsonRequestStateStore
class SwallowsRequestError(unittest.TestCase):
    def runTest(self):
        try:
            JsonRequestStateStore(pathlib.Path(sys.argv[1])).save('b'*32, {'terminal': True})
        except Exception:
            pass
result = unittest.TestResult()
SwallowsRequestError().run(result)
assert len(result.failures) == 1, result.failures
""", str(protected)], cwd=Path(__file__).parents[1], env=environment, capture_output=True, text=True)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertFalse(protected.exists())
