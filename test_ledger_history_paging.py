"""Read-only history paging against private SQLite and inert host fixtures."""

import base64
import hmac
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock
import uuid

from ledger_mail import Mailbox
from ledger_remote import RemoteLedger
from ledger_status import AgentRegistry
from ledger_store import LedgerError, LedgerStore, SCHEMA_VERSION, json_text
from test_ledger_core import snapshot


class InertTransport:
    def call(self, *args, **kwargs):
        raise AssertionError("History fixtures must never contact a remote host.")


class LedgerHistoryPagingTest(unittest.TestCase):
    def setUp(self):
        # The integrated runner supplies disk-backed, checkout-specific storage.
        self.temp = tempfile.TemporaryDirectory(
            prefix="history-paging-", dir=os.environ.get("AGENT_WORK_ROOT") or os.environ.get("TMPDIR"))
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.environment = {
            "HOME": str(self.root / "home"),
            "XDG_CONFIG_HOME": str(self.root / "config"),
            "XDG_STATE_HOME": str(self.root / "state"),
            "XDG_CACHE_HOME": str(self.root / "cache"),
            "XDG_RUNTIME_DIR": str(self.root / "runtime"),
            "TMPDIR": str(self.root / "temporary"),
            "HERDR_SOCKET_PATH": str(self.root / "inert.sock"),
            "HERDR_AGENT_LEDGER_DIR": str(self.root / "ledger"),
            "HERDR_AGENT_REQUEST_DIR": str(self.root / "requests"),
            "HERDR_AGENT_LEDGER_AUTOSTART": "0",
        }
        for key in ("HOME", "XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME",
                    "XDG_RUNTIME_DIR", "TMPDIR", "HERDR_AGENT_REQUEST_DIR"):
            Path(self.environment[key]).mkdir(mode=0o700)
        isolated = mock.patch.dict(os.environ, self.environment, clear=True)
        isolated.start()
        self.addCleanup(isolated.stop)
        self.store = LedgerStore(environment=self.environment, canonical_name="alpha")
        self.mail = Mailbox(self.store, self.environment)
        repo = self.root / "repo"
        repo.mkdir()
        registry = AgentRegistry(self.store)
        registry.reconcile(snapshot(repo), refresh_display=False)
        self.agents = registry.list()
        self.repo_id = self.agents[0]["repo_id"]
        self.other_repo = "git:other/repository"
        self.remote = RemoteLedger(self.store, self.environment, transport=InertTransport())
        with self.store.transaction() as connection:
            connection.execute("INSERT INTO peers VALUES (?,?,?,?)",
                               ("beta", str(uuid.uuid4()), "inert-beta", "/inert/ham"))

    def message(self, number, updated_at, *, repo_id=None, origin="alpha", recipient="alpha",
                sender="unrelated-agent", thread_id=None):
        identity = str(uuid.UUID(int=number))
        value = {
            "message_id": identity, "thread_id": thread_id or identity,
            "repo_id": self.repo_id if repo_id is None else repo_id,
            "reply_to": thread_id, "origin_host": origin, "sender": sender,
            "sender_label": "inert sender", "kind": "fyi", "hop": int(thread_id is not None),
            "body": f"message {number}", "subject": "history fixture", "created_at": float(number),
            "recipient_host": recipient, "recipient_terminal_id": "term-b",
            "recipient_address": f"{recipient}/term-b",
        }
        with self.store.transaction() as connection:
            result = self.remote.submit(connection, value)
            connection.execute("UPDATE deliveries SET updated_at=? WHERE message_id=?",
                               (updated_at, identity))
        return identity, result

    def direct(self, updated_at, *, repo_id=None, acceptance=None):
        record = self.mail.begin_direct(self.repo_id if repo_id is None else repo_id,
                                        {"host": "beta", "terminal_id": "not-a-participant"},
                                        body="Actual direct submission history")
        if acceptance:
            self.mail.finish_direct(record["submission_id"], acceptance=acceptance,
                                    evidence="inert fixture acceptance")
        with self.store.transaction() as connection:
            connection.execute("UPDATE submissions SET updated_at=? WHERE submission_id=?",
                               (updated_at, record["submission_id"]))
        return record["submission_id"]

    def history(self, kind):
        return self.mail.list_threads if kind == "threads" else self.mail.list_direct

    @staticmethod
    def fields(kind):
        return ("threads", "thread_id") if kind == "threads" else ("submissions", "submission_id")

    def seed_pages(self, kind):
        if kind == "threads":
            values = [(self.message(index + 1, stamp)[0], stamp)
                      for index, stamp in enumerate((50.0, 40.0, 40.0, 40.0, 30.0, 10.0))]
            # A second message belongs to one thread, not a second history entry.
            self.message(100, 60.0, thread_id=values[-1][0])
            values[-1] = (values[-1][0], 60.0)
            self.message(200, 100.0, repo_id=self.other_repo)
        else:
            values = [(self.direct(stamp, acceptance=acceptance), stamp)
                      for stamp, acceptance in ((50.0, None), (40.0, "accepted"),
                                                (40.0, "unknown"), (40.0, "failed"),
                                                (30.0, None), (10.0, None))]
            self.direct(100.0, repo_id=self.other_repo)
        return [identity for identity, _ in sorted(values, key=lambda item: (-item[1], item[0]))]

    def test_empty_first_pages_and_legacy_positional_arguments(self):
        for kind in ("threads", "direct"):
            items, _ = self.fields(kind)
            self.assertEqual(self.history(kind)(self.repo_id, 100),
                             {"repo_id": self.repo_id, items: [], "next_cursor": None})
            self.assertEqual(self.history(kind)(), {"repo_id": None, items: [], "next_cursor": None})

    def test_first_end_tied_boundaries_and_no_duplicates_on_unchanged_data(self):
        for kind in ("threads", "direct"):
            with self.subTest(kind=kind):
                expected = self.seed_pages(kind)
                items, identity = self.fields(kind)
                method = self.history(kind)
                initial = method(self.repo_id)
                self.assertEqual([row[identity] for row in initial[items]], expected)
                self.assertIsNone(initial["next_cursor"])
                self.assertIsNone(method(self.repo_id, 6)["next_cursor"])
                self.assertEqual(len(method(limit=1000)[items]), 7)
                cursor, collected, pages = None, [], []
                while True:
                    page = method(self.repo_id, 2, cursor=cursor)
                    self.assertEqual(page["repo_id"], self.repo_id)
                    pages.append(page)
                    collected.extend(row[identity] for row in page[items])
                    cursor = page["next_cursor"]
                    if cursor is None:
                        break
                    self.assertIsInstance(cursor, str)
                self.assertEqual(collected, expected)
                self.assertEqual(len(collected), len(set(collected)))
                self.assertEqual([len(page[items]) for page in pages], [2, 2, 2])
                self.assertTrue(all(page["next_cursor"] for page in pages[:-1]))
                # A different page size is allowed on continuation.
                remainder = method(self.repo_id, 1000, cursor=pages[0]["next_cursor"])
                self.assertEqual([row[identity] for row in remainder[items]], expected[2:])
                self.assertIsNone(remainder["next_cursor"])
                if kind == "threads":
                    self.assertEqual(len(initial[items][0]["messages"]), 2)

    def test_repo_scope_includes_sent_received_and_pending_remote_copies_not_participants(self):
        sent, _ = self.message(1, 30.0, sender=self.agents[0]["agent_id"])
        received, _ = self.message(2, 20.0, origin="beta", sender="remote-nonparticipant")
        pending, result = self.message(3, 10.0, recipient="beta", sender="inactive-nonparticipant")
        excluded, _ = self.message(4, 40.0, repo_id=self.other_repo)
        self.assertEqual(result["state"], "pending")
        # No member selection is involved in reading repository history.
        with self.store.read() as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM repo_members").fetchone()[0], 0)
        page = self.mail.list_threads(self.repo_id)
        self.assertEqual([row["thread_id"] for row in page["threads"]], [sent, received, pending])
        self.assertEqual(page["threads"][-1]["messages"][0]["state"], "pending")
        self.assertIn(excluded, {row["thread_id"] for row in self.mail.list_threads()["threads"]})

    def test_direct_only_excludes_spawn_initial_and_includes_all_acceptances(self):
        expected = {self.direct(10.0, acceptance=value)
                    for value in (None, "accepted", "unknown", "failed")}
        other = self.direct(20.0, repo_id=self.other_repo)
        target = {"transport_host": "alpha", "host": "alpha", "local": True,
                  "terminal_id": "inert", "session_ref": "inert-session", "client": "omp",
                  "pane_id": "inert-pane", "address": "alpha/inert", "workspace_id": "inert-workspace",
                  "tab_id": "inert-tab", "created": True, "repo_id": self.repo_id}
        spawn = self.mail.claim_spawn_submission("inert-operation", target=target, body="Initial input",
                                                sender={"kind": "unknown", "repo_id": self.repo_id},
                                                readiness_evidence="inert bound target")
        rows = self.mail.list_direct(self.repo_id)["submissions"]
        self.assertEqual({row["submission_id"] for row in rows}, expected)
        self.assertEqual({row["acceptance"] for row in rows}, {"submitting", "accepted", "unknown", "failed"})
        self.assertTrue(all(row["via"] == "direct" for row in rows))
        all_ids = {row["submission_id"] for row in self.mail.list_direct()["submissions"]}
        self.assertEqual(all_ids, expected | {other})
        self.assertNotIn(spawn["submission_id"], all_ids)

    def assert_invalid_cursor(self, method, cursor, repo_id=None):
        with self.assertRaises(LedgerError) as caught:
            method(repo_id, 1, cursor=cursor)
        self.assertEqual(caught.exception.code, "invalid_cursor")

    def test_cursor_tampering_wrong_repo_kind_and_ledger_are_rejected(self):
        other_store = LedgerStore(self.root / "other-ledger", environment=self.environment,
                                  canonical_name="alpha")
        other_mail = Mailbox(other_store, self.environment)
        for kind in ("threads", "direct"):
            self.seed_pages(kind)
            method = self.history(kind)
            token = method(self.repo_id, 1)["next_cursor"]
            payload, signature = token.split(".")
            modified = ("A" if payload[0] != "A" else "B") + payload[1:] + "." + signature
            bad_signature = payload + "." + ("A" if signature[0] != "A" else "B") + signature[1:]
            for value in (modified, bad_signature, "", "not-a-token", token + "=", token + ".extra",
                          "A" * 32769, 7, False, [], {}, b"bytes"):
                with self.subTest(kind=kind, cursor=repr(value)[:80]):
                    self.assert_invalid_cursor(method, value, self.repo_id)
            for scope in (None, self.other_repo):
                self.assert_invalid_cursor(method, token, scope)
            all_repos = method(limit=1)["next_cursor"]
            self.assert_invalid_cursor(method, all_repos, self.repo_id)
            wrong_kind = self.mail.list_direct if kind == "threads" else self.mail.list_threads
            self.assert_invalid_cursor(wrong_kind, token, self.repo_id)
            cross_ledger = other_mail.list_threads if kind == "threads" else other_mail.list_direct
            self.assert_invalid_cursor(cross_ledger, token, self.repo_id)

    def test_authenticated_cursor_schema_is_strict(self):
        self.seed_pages("threads")
        token = self.mail.list_threads(self.repo_id, 1)["next_cursor"]
        encoded = token.split(".")[0]
        original = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
        with self.store.read() as connection:
            key = bytes.fromhex(connection.execute(
                "SELECT value FROM ledger_meta WHERE key='history_cursor_key'").fetchone()[0])

        def signed(value):
            payload = json_text(value).encode("utf-8")
            encode = lambda data: base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")
            return encode(payload) + "." + encode(hmac.digest(key, payload, "sha256"))

        invalid = [{**original, field: value} for field, value in (
            ("v", True), ("v", 2), ("ledger", str(uuid.uuid4())), ("kind", "unknown"),
            ("repo", self.other_repo), ("updated_at", True), ("updated_at", "30"),
            ("updated_at", float("inf")), ("updated_at", float("nan")),
            ("updated_at", 10 ** 400), ("id", "not-a-uuid"), ("id", 1))]
        invalid.extend(({**original, "extra": 1}, {k: v for k, v in original.items() if k != "repo"}, []))
        for value in invalid:
            with self.subTest(value=value):
                self.assert_invalid_cursor(self.mail.list_threads, signed(value), self.repo_id)

    def test_cursor_key_persists_and_reads_do_not_mutate_database(self):
        for kind in ("threads", "direct"):
            self.seed_pages(kind)
            items, identity = self.fields(kind)
            first = self.history(kind)(self.repo_id, 1)
            with self.store.read() as connection:
                before = list(connection.iterdump())
                key = connection.execute("SELECT value FROM ledger_meta WHERE key='history_cursor_key'").fetchone()[0]
                self.assertEqual(len(key), 64)
                self.assertEqual(connection.execute("SELECT value FROM ledger_meta WHERE key='schema_version'").fetchone()[0],
                                 str(SCHEMA_VERSION))
            reopened = Mailbox(LedgerStore(self.store.directory, environment=self.environment), self.environment)
            method = reopened.list_threads if kind == "threads" else reopened.list_direct
            continued = method(self.repo_id, 100, cursor=first["next_cursor"])
            self.assertNotIn(first[items][0][identity], {row[identity] for row in continued[items]})
            with self.store.read() as connection:
                self.assertEqual(list(connection.iterdump()), before)

    def test_preexisting_ledger_gains_one_durable_metadata_key_without_schema_bump(self):
        with self.store.transaction() as connection:
            connection.execute("DELETE FROM ledger_meta WHERE key='history_cursor_key'")
        first = LedgerStore(self.store.directory, environment=self.environment)
        second = LedgerStore(self.store.directory, environment=self.environment)
        self.assertEqual(first._history_cursor_key, second._history_cursor_key)
        self.assertEqual(first.host_uuid, self.store.host_uuid)
        with self.store.read() as connection:
            self.assertEqual(connection.execute("SELECT value FROM ledger_meta WHERE key='schema_version'").fetchone()[0],
                             str(SCHEMA_VERSION))

    def test_live_pages_do_not_claim_a_snapshot_when_rows_change_or_disappear(self):
        for kind in ("threads", "direct"):
            expected = self.seed_pages(kind)
            method = self.history(kind)
            items, identity = self.fields(kind)
            first = method(self.repo_id, 2)
            moved = expected[-1]
            with self.store.transaction() as connection:
                if kind == "threads":
                    connection.execute("UPDATE deliveries SET updated_at=1000 WHERE message_id IN "
                                       "(SELECT message_id FROM messages WHERE thread_id=?)", (moved,))
                else:
                    connection.execute("UPDATE submissions SET updated_at=1000 WHERE submission_id=?", (moved,))
            continued = method(self.repo_id, 100, cursor=first["next_cursor"])
            self.assertNotIn(moved, {row[identity] for row in continued[items]})
            self.assertEqual(method(self.repo_id)[items][0][identity], moved)
            with self.store.transaction() as connection:
                if kind == "threads":
                    connection.execute("DELETE FROM messages WHERE repo_id=?", (self.repo_id,))
                else:
                    connection.execute("DELETE FROM submissions WHERE repo_id=?", (self.repo_id,))
            self.assertEqual(method(self.repo_id, 2, cursor=first["next_cursor"]),
                             {"repo_id": self.repo_id, items: [], "next_cursor": None})

    def test_invalid_limits_are_typed_and_bounded(self):
        for method in (self.mail.list_threads, self.mail.list_direct):
            for limit in (0, -1, 1001, True, False, 1.0, 2.5, "2", None, [], float("inf")):
                with self.subTest(method=method.__name__, limit=limit):
                    with self.assertRaises(LedgerError) as caught:
                        method(self.repo_id, limit)
                    self.assertEqual(caught.exception.code, "invalid_limit")


if __name__ == "__main__":
    unittest.main()
