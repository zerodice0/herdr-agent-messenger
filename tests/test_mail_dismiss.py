"""Sender-side attention resolution keeps delivery evidence and wire truth intact."""
import tests.ham_test_support as ham_test_support  # Protect the live ledger before product imports.

from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest import mock
import uuid

from ham.ledger.mail import Mailbox, payload_hash, validate_payload
from ham.ledger.settings import RepoSettings
from ham.ledger.status import AgentRegistry
from ham.ledger.store import LedgerError, LedgerStore, SCHEMA_VERSION, receipt, save_receipt


class MailDismissTest(unittest.TestCase):
    repo_id = "git:example/dismiss"

    def setUp(self):
        work = Path(os.environ.get("AGENT_WORK_ROOT", Path.home() / ".cache/agent-work")) / "mail-dismiss-tests"
        work.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=work)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = LedgerStore(self.root / "ledger", canonical_name="alpha")
        self.registry = AgentRegistry(self.store)
        self.mail = Mailbox(self.store)
        self.settings = RepoSettings(self.store)
        identity = mock.patch("ham.ledger.status.repository_identity", side_effect=lambda cwd, host: {
            "repo_id": "git:example/other" if cwd == "other" else self.repo_id,
            "is_main_checkout": cwd == "main"})
        identity.start()
        self.addCleanup(identity.stop)
        self.observed = self.snapshot()
        self.agents = self.register()

    def snapshot(self):
        placements = [("main", "anchor", "main"), ("sender", "linked", "worktree"),
                      ("worker", "linked", "worktree"), ("recipient", "anchor", "worktree"),
                      ("outsider", "separate", "main"), ("wrong-repo", "anchor", "other")]
        panes = [{"terminal_id": terminal, "workspace_id": workspace, "pane_id": "p-" + terminal}
                 for terminal, workspace, _ in placements]
        return {"panes": panes, "agents": [
            {**pane, "agent": "codex", "name": pane["terminal_id"], "agent_status": "idle",
             "agent_session": {"value": "session-" + pane["terminal_id"]}, "cwd": cwd}
            for pane, (_, _, cwd) in zip(panes, placements)],
            "workspaces": [{"workspace_id": workspace, "label": workspace,
                            "worktree": {"repo_key": "shared" if workspace != "separate" else "other",
                                         "is_linked_worktree": workspace == "linked"}}
                           for workspace in ("anchor", "linked", "separate")]}

    def register(self):
        self.registry.reconcile(self.observed, refresh_display=False)
        return {row["terminal_id"]: row for row in self.registry.list()}

    def sent(self, *, sender="sender", state="undeliverable"):
        result = self.mail.send(self.agents[sender], self.agents["recipient"], body="Failure context", kind="action")
        with self.store.transaction() as connection:
            connection.execute("UPDATE deliveries SET state=?,hold_reason='terminal_closed',evidence='native:original',"
                               "confirmation_source='native',updated_at=123.0 WHERE message_id=?",
                               (state, result["message_id"]))
            if state in {"handled", "withdrawn", "undeliverable"}:
                connection.execute("UPDATE messages SET final_at=123.0 WHERE message_id=?", (result["message_id"],))
            save_receipt(connection, result["message_id"])
        return result["message_id"]

    def dismiss(self, *ids, actor="sender", reason="Recipient closed; no action remains"):
        return self.mail.dismiss(self.agents[actor], ids, reason=reason)

    def assert_error(self, code, operation):
        with self.assertRaises(LedgerError) as caught:
            operation()
        self.assertEqual(caught.exception.code, code)

    def audits(self):
        with self.store.read() as connection:
            return [dict(row) for row in connection.execute("SELECT * FROM mail_dismissals ORDER BY message_id")]

    def test_sender_dismissal_preserves_all_delivery_evidence_and_views(self):
        message_id = self.sent()
        self.assertIsNone(self.mail.show_thread(message_id)["messages"][0]["dismissal"])
        with self.store.read() as connection:
            before = dict(connection.execute("SELECT m.*,d.* FROM messages m JOIN deliveries d USING(message_id)").fetchone())
            dedupe = dict(connection.execute("SELECT * FROM dedupe_receipts").fetchone())
            counts = {table: connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                      for table in ("outbox", "wake_log", "native_claims")}
        with mock.patch("ham.ledger.mail.save_receipt", side_effect=AssertionError("No dismissal receipt")):
            result = self.dismiss(message_id)
        audit = result["dismissals"][0]
        self.assertEqual(audit["actor_terminal_id"], "sender")
        self.assertEqual(audit["actor_alias"], self.agents["sender"]["alias"])
        self.assertEqual(audit["failure"]["state"], "undeliverable")
        self.assertEqual(audit["sender_scope_workspace_id"], "anchor")
        with self.store.read() as connection:
            self.assertEqual(dict(connection.execute("SELECT m.*,d.* FROM messages m JOIN deliveries d USING(message_id)").fetchone()), before)
            self.assertEqual(dict(connection.execute("SELECT * FROM dedupe_receipts").fetchone()), dedupe)
            for table, count in counts.items():
                self.assertEqual(connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0], count)
            self.assertNotIn("dismissal", receipt(connection, message_id))
        for view in (self.mail.show_thread(message_id)["messages"][0],
                     self.mail.list_threads(self.repo_id)["threads"][0]["messages"][0],
                     self.mail.read(self.agents["recipient"], message_id=message_id)["messages"][0]):
            self.assertEqual(view["dismissal"], audit)
            self.assertEqual(view["state"], "undeliverable")

    def test_sender_restarted_terminal_is_owner_even_off(self):
        message_id = self.sent()
        old = self.agents["sender"]
        for raw in self.observed["agents"]:
            if raw["terminal_id"] == "sender":
                raw["agent_session"] = {"value": "replacement"}
        self.agents = self.register()
        self.assertNotEqual(old["agent_id"], self.agents["sender"]["agent_id"])
        with self.store.transaction() as connection:
            connection.execute("UPDATE repo_settings SET value='\"off\"' WHERE repo_id=? AND field='mode'", (self.repo_id,))
        self.assertEqual(self.dismiss(message_id)["dismissals"][0]["actor_agent_id"], self.agents["sender"]["agent_id"])
        self.assert_error("stale_session", lambda: self.mail.dismiss(old, [message_id], reason="Old actor"))

    def test_current_group_main_after_sender_closes_and_linked_manual_main(self):
        first = self.sent()
        self.observed["agents"] = [row for row in self.observed["agents"] if row["terminal_id"] != "sender"]
        self.observed["panes"] = [row for row in self.observed["panes"] if row["terminal_id"] != "sender"]
        self.agents = self.register()
        self.assertEqual(self.dismiss(first, actor="main")["dismissals"][0]["actor_terminal_id"], "main")
        second = self.sent(sender="recipient")
        with self.store.transaction() as connection:
            connection.execute("UPDATE repo_settings SET value='\"manual\"' WHERE repo_id=? AND field='mode'", (self.repo_id,))
            connection.execute("INSERT INTO workspace_settings VALUES (?,?,?,?,?,?,?,?)",
                               (self.repo_id, "alpha", "anchor", "main", json.dumps({"host": "alpha", "terminal_id": "worker"}), 1, "user", time.time()))
        self.assertEqual(self.dismiss(second, actor="worker")["dismissals"][0]["actor_terminal_id"], "worker")

    def test_closed_linked_sender_and_workspace_keep_send_time_anchor_authority(self):
        message_id = self.sent()
        original_sender = self.agents["sender"]["agent_id"]
        self.observed["agents"] = [row for row in self.observed["agents"]
                                   if row["workspace_id"] != "linked"]
        self.observed["panes"] = [row for row in self.observed["panes"]
                                  if row["workspace_id"] != "linked"]
        self.observed["workspaces"] = [row for row in self.observed["workspaces"]
                                       if row["workspace_id"] != "linked"]
        self.agents = self.register()
        with self.store.transaction() as connection:
            connection.execute("DELETE FROM agent_status WHERE agent_id=?", (original_sender,))
            connection.execute("DELETE FROM agents WHERE agent_id=?", (original_sender,))
            self.assertIsNone(connection.execute(
                "SELECT 1 FROM workspaces WHERE workspace_id='linked'").fetchone())
        self.assert_error("not_sender", lambda: self.dismiss(message_id, actor="outsider"))
        first = self.dismiss(message_id, actor="main")
        self.assertEqual(first["dismissals"][0]["sender_scope_workspace_id"], "anchor")
        self.assertEqual(first["dismissals"][0]["sender_workspace_id"], "linked")
        self.assertEqual(self.store.retention(now=time.time() + 91 * 86400)["messages"], 1)
        self.assertEqual(self.dismiss(message_id, actor="main", reason="Preserve first"), first)
        self.assert_error("not_sender", lambda: self.dismiss(message_id, actor="outsider"))

    def test_sender_scope_is_local_metadata_not_a_message_hash_or_wire_field(self):
        with self.store.transaction() as connection:
            connection.execute("INSERT INTO peers VALUES (?,?,?,?)",
                               ("beta", str(uuid.uuid4()), "fixture", "/fixture"))
        recipient = {**self.agents["recipient"], "host": "beta", "recipient_address": "beta/recipient"}
        result = self.mail.send(self.agents["sender"], recipient, body="Wire-compatible scope snapshot")
        with self.store.read() as connection:
            row = connection.execute("SELECT * FROM messages WHERE message_id=?", (result["message_id"],)).fetchone()
            context = json.loads(row["sender_context"])
            payload = json.loads(connection.execute("SELECT payload FROM outbox WHERE kind='message'").fetchone()[0])
        self.assertEqual(context["scope_workspace_id"], "anchor")
        self.assertEqual(context["terminal_id"], "sender")
        self.assertNotIn("sender_context", payload["message"])
        self.assertEqual(validate_payload(payload["message"]), payload["message"])
        self.assertEqual(payload_hash(payload["message"]), row["payload_hash"])
        self.assertEqual(result["payload_hash"], row["payload_hash"])
        self.assert_error("invalid_message", lambda: validate_payload(
            {**payload["message"], "sender_context": context}))

    def test_nonowners_wrong_repo_group_worker_and_off_main_fail_closed(self):
        message_id = self.sent()
        for actor in ("wrong-repo", "outsider", "worker", "recipient"):
            with self.subTest(actor=actor):
                self.assert_error("not_sender", lambda: self.dismiss(message_id, actor=actor))
        with self.store.transaction() as connection:
            connection.execute("UPDATE repo_settings SET value='\"off\"' WHERE repo_id=? AND field='mode'", (self.repo_id,))
        self.assert_error("not_sender", lambda: self.dismiss(message_id, actor="main"))
        self.assertEqual(self.audits(), [])

    def test_sender_authority_never_uses_thread_origin_or_recipient_repository(self):
        message_id = self.sent()
        other_repo = self.agents["wrong-repo"]["repo_id"]
        with self.store.transaction() as connection:
            connection.execute("UPDATE messages SET repo_id=? WHERE message_id=?", (other_repo, message_id))
            connection.execute("UPDATE threads SET repo_id=? WHERE thread_id=?", (other_repo, message_id))
            connection.execute("UPDATE deliveries SET recipient_repo_id=? WHERE message_id=?", (other_repo, message_id))
        self.assert_error("not_sender", lambda: self.dismiss(message_id, actor="wrong-repo"))
        audit = self.dismiss(message_id, actor="main")["dismissals"][0]
        self.assertEqual(audit["sender_repo_id"], self.repo_id)

    def test_restarted_original_terminal_in_wrong_repository_is_not_owner(self):
        message_id = self.sent()
        for raw in self.observed["agents"]:
            if raw["terminal_id"] == "sender":
                raw["cwd"] = "other"
                raw["agent_session"] = {"value": "wrong-repo-restart"}
        self.agents = self.register()
        self.assert_error("not_sender", lambda: self.dismiss(message_id))
        self.assertEqual(self.audits(), [])

    def test_verified_generation_fields_override_untrusted_actor_repo_and_alias(self):
        message_id = self.sent()
        forged = {**self.agents["worker"], "terminal_id": "worker", "repo_id": "fake", "alias": "fake"}
        self.assert_error("not_sender", lambda: self.mail.dismiss(forged, [message_id], reason="Forged"))
        forged = {**self.agents["sender"], "alias": "fake", "repo_id": "fake"}
        audit = self.mail.dismiss(forged, [message_id], reason="Real owner")["dismissals"][0]
        self.assertEqual(audit["actor_repo_id"], self.repo_id)
        self.assertEqual(audit["actor_alias"], self.agents["sender"]["alias"])

    def test_wrong_origin_and_missing_sender_group_evidence_fail_closed(self):
        message_id = self.sent()
        with self.store.transaction() as connection:
            connection.execute("UPDATE messages SET origin_host='beta' WHERE message_id=?", (message_id,))
        self.assert_error("wrong_origin", lambda: self.dismiss(message_id, actor="main"))
        with self.store.transaction() as connection:
            connection.execute("UPDATE messages SET origin_host='alpha',sender=? WHERE message_id=?", (str(uuid.uuid4()), message_id))
        self.assert_error("not_sender", lambda: self.dismiss(message_id, actor="main"))
        with self.store.transaction() as connection:
            connection.execute("UPDATE messages SET sender=? WHERE message_id=?", (self.agents["sender"]["agent_id"], message_id))
            connection.execute("UPDATE agents SET workspace_id='' WHERE agent_id=?", (self.agents["sender"]["agent_id"],))
            connection.execute("UPDATE messages SET sender_context=NULL WHERE message_id=?", (message_id,))
        self.assert_error("not_sender", lambda: self.dismiss(message_id, actor="main"))

    def test_ineligible_states_and_missing_ids_are_atomic(self):
        eligible = self.sent()
        for state in ("pending", "queued", "delivering", "delivered", "handled", "withdrawn"):
            message_id = self.sent(state=state)
            with self.subTest(state=state):
                self.assert_error("not_undeliverable", lambda: self.dismiss(eligible, message_id))
                self.assertEqual(self.audits(), [])
        self.assert_error("message_not_found", lambda: self.dismiss(eligible, str(uuid.uuid4())))
        other = self.sent(sender="outsider")
        self.assert_error("not_sender", lambda: self.dismiss(eligible, other))
        self.assertEqual(self.audits(), [])

    def test_empty_ids_invalid_ids_and_blank_reasons_do_not_mutate(self):
        self.assert_error("invalid_id", lambda: self.dismiss())
        self.assert_error("invalid_id", lambda: self.dismiss("bad"))
        for reason in ("", " \n\t", None):
            self.assert_error("missing_reason", lambda: self.dismiss(str(uuid.uuid4()), reason=reason))
        self.assertEqual(self.audits(), [])

    def test_duplicates_repeat_and_concurrent_repeats_preserve_first_audit(self):
        message_id = self.sent()
        barrier = threading.Barrier(2)
        def run(reason):
            barrier.wait()
            return self.dismiss(message_id, message_id, reason=reason)
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(run, reason) for reason in ("First A", "First B")]
            first, second = [future.result() for future in futures]
        self.assertEqual(first, second)
        self.assertEqual(len(first["dismissals"]), 1)
        self.assertEqual(self.dismiss(message_id, actor="main", reason="Do not rewrite"), first)
        self.assertEqual(len(self.audits()), 1)
        self.assert_error("not_sender", lambda: self.dismiss(message_id, actor="worker"))

    def test_audit_survives_retention_and_authorizes_repeat_without_sender_row(self):
        message_id = self.sent()
        first = self.dismiss(message_id)
        self.assertEqual(self.store.retention(now=time.time() + 91 * 86400)["messages"], 1)
        original_sender = self.agents["sender"]["agent_id"]
        for raw in self.observed["agents"]:
            if raw["terminal_id"] == "sender":
                raw["agent_session"] = {"value": "after-retention"}
        self.agents = self.register()
        with self.store.transaction() as connection:
            connection.execute("DELETE FROM agent_status WHERE agent_id=?", (original_sender,))
            connection.execute("DELETE FROM agents WHERE agent_id=?", (original_sender,))
        self.assertEqual(self.dismiss(message_id, reason="Ignored replacement reason"), first)
        self.assertEqual(self.dismiss(message_id, actor="main"), first)
        self.assert_error("not_sender", lambda: self.dismiss(message_id, actor="outsider"))
        with self.store.read() as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM messages").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT count(*) FROM mail_dismissals").fetchone()[0], 1)
            self.assertEqual(receipt(connection, message_id)["state"], "undeliverable")
        with self.store.transaction() as connection:
            connection.execute("UPDATE repo_settings SET value='\"off\"' WHERE repo_id=? AND field='mode'", (self.repo_id,))
        self.assert_error("not_sender", lambda: self.dismiss(message_id, actor="main"))
        self.assertEqual(self.dismiss(message_id), first)

    def test_v10_additive_migration_keeps_data_settings_and_identity(self):
        message_id = self.sent()
        with self.store.transaction() as connection:
            connection.execute("DROP TABLE mail_dismissals")
            connection.execute("ALTER TABLE messages DROP COLUMN sender_context")
            connection.execute("UPDATE ledger_meta SET value='10' WHERE key='schema_version'")
            connection.execute("INSERT INTO workspace_settings VALUES (?,?,?,?,?,?,?,?)",
                               (self.repo_id, "alpha", "anchor", "main",
                                json.dumps({"host": "alpha", "terminal_id": "main"}), 4, "user", 5.0))
        with self.store.read() as connection:
            legacy_messages = [dict(row) for row in connection.execute("SELECT * FROM messages")]
            before = {table: [tuple(row) for row in connection.execute(f"SELECT * FROM {table}")]
                      for table in ("deliveries", "agents", "repo_settings", "workspace_settings", "dedupe_receipts", "workspaces")}
        self.assert_error("schema_upgrade_required", lambda: LedgerStore.open_readonly(
            {"HERDR_AGENT_LEDGER_DIR": str(self.store.directory)}))
        upgraded = LedgerStore(self.store.directory, canonical_name="alpha")
        self.assertEqual(upgraded.host_uuid, self.store.host_uuid)
        self.assertEqual(upgraded.doctor()["schema_version"], 11)
        self.assertEqual(SCHEMA_VERSION, 11)
        with upgraded.read() as connection:
            for table, rows in before.items():
                self.assertEqual([tuple(row) for row in connection.execute(f"SELECT * FROM {table}")], rows)
            self.assertEqual(connection.execute("SELECT count(*) FROM mail_dismissals").fetchone()[0], 0)
            self.assertEqual([{key: row[key] for key in row.keys() if key != "sender_context"}
                              for row in connection.execute("SELECT * FROM messages")], legacy_messages)
            self.assertIsNone(connection.execute("SELECT sender_context FROM messages").fetchone()[0])
        self.mail = Mailbox(upgraded)
        self.assert_error("not_sender", lambda: self.dismiss(message_id, actor="outsider"))
        self.assertEqual(self.dismiss(message_id, actor="main")["dismissals"][0]["sender_scope_workspace_id"], "anchor")
        self.assertEqual(Mailbox(upgraded).dismiss(self.agents["sender"], [message_id], reason="After migration")["dismissals"][0]["message_id"], message_id)

    def test_v10_missing_historical_linked_workspace_fails_closed_after_upgrade(self):
        message_id = self.sent()
        self.observed["agents"] = [row for row in self.observed["agents"]
                                   if row["workspace_id"] != "linked"]
        self.observed["panes"] = [row for row in self.observed["panes"]
                                  if row["workspace_id"] != "linked"]
        self.observed["workspaces"] = [row for row in self.observed["workspaces"]
                                       if row["workspace_id"] != "linked"]
        self.agents = self.register()
        with self.store.transaction() as connection:
            original_hash = connection.execute(
                "SELECT payload_hash FROM messages WHERE message_id=?", (message_id,)).fetchone()[0]
            connection.execute("DROP TABLE mail_dismissals")
            connection.execute("ALTER TABLE messages DROP COLUMN sender_context")
            connection.execute("UPDATE ledger_meta SET value='10' WHERE key='schema_version'")
        upgraded = LedgerStore(self.store.directory, canonical_name="alpha")
        self.mail = Mailbox(upgraded)
        self.assert_error("not_sender", lambda: self.dismiss(message_id, actor="main"))
        self.assert_error("not_sender", lambda: self.dismiss(message_id, actor="outsider"))
        with upgraded.read() as connection:
            row = connection.execute(
                "SELECT sender_context,payload_hash FROM messages WHERE message_id=?", (message_id,)).fetchone()
            self.assertIsNone(row["sender_context"])
            self.assertEqual(row["payload_hash"], original_hash)
            self.assertEqual(receipt(connection, message_id)["payload_hash"], original_hash)
        self.assertEqual(self.audits(), [])

    def test_v10_recorded_link_without_anchor_is_not_historical_scope_evidence(self):
        message_id = self.sent()
        with self.store.transaction() as connection:
            connection.execute("UPDATE messages SET sender_context=NULL WHERE message_id=?", (message_id,))
            connection.execute("DELETE FROM workspaces WHERE workspace_id='anchor'")
        self.assert_error("not_sender", lambda: self.dismiss(message_id, actor="main"))
        self.assertEqual(self.audits(), [])
