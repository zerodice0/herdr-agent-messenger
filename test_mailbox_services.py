"""Shared user/recovery/spawn services exercised with private real SQLite state."""
import ham_test_support  # Protect the live ledger before product imports.

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
import uuid
from unittest import mock

from agent_directory import user_prompt_text
from ledger_mail import Mailbox
from ledger_settings import DEFAULT_THRESHOLDS, RepoSettings
from ledger_status import AgentRegistry
from ledger_store import LedgerError, LedgerStore, receipt
from mailbox_tui import OPENER_PANE, OPENER_TERMINAL, REPO_ENV, execute_job, local_snapshot
from test_ledger_core import snapshot


class MailboxServicesTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.store = LedgerStore(self.root / "ledger", canonical_name="alpha")
        self.registry = AgentRegistry(self.store, display_runner=self.display)
        self.registry.reconcile(snapshot(self.repo))
        self.sender, self.recipient = sorted(self.registry.list(), key=lambda row: row["terminal_id"])
        self.mail = Mailbox(self.store, {"HERDR_AGENT_LEDGER_AUTOSTART": "0"})
        self.repo_id = self.sender["repo_id"]

    @staticmethod
    def display(command):
        return subprocess.CompletedProcess(command, 0, '{"result": {"pane": {"label": "manual"}}}', "")

    def message(self):
        return self.mail.send(self.sender, self.recipient, body="Apply the pending change", kind="action")

    def claim(self):
        claim = self.mail.claim(terminal_id=self.recipient["terminal_id"],
                                agent_id=self.recipient["agent_id"],
                                generation=self.recipient["generation"], via="wake")
        return {key: claim[key] for key in ("claim_id", "agent_id", "generation")}

    def state(self, message_id):
        with self.store.read() as connection:
            return receipt(connection, message_id)

    def settings_environment(self):
        return {"HERDR_AGENT_LEDGER_DIR": str(self.store.directory),
                "HERDR_AGENT_LEDGER_AUTOSTART": "0",
                OPENER_PANE: self.sender["pane_id"], OPENER_TERMINAL: self.sender["terminal_id"],
                REPO_ENV: self.repo_id}

    def save_settings(self, field, value, *, scope=None, environment=None):
        request = {"operation": "setting", "repo_id": self.repo_id, "field": field, "value": value,
                   "scope": scope or {"repo_id": self.repo_id, "host": self.store.host,
                                      "workspace_id": self.sender["workspace_id"],
                                      "requested_workspace_id": self.sender["workspace_id"]}}
        with mock.patch("ledger_display.LedgerDisplay.refresh", return_value={"updated": [], "failed": []}), \
             mock.patch("mailbox_tui.herdr", side_effect=LedgerError("isolated", "No live Herdr")):
            return execute_job(request, environment or self.settings_environment())

    def test_settings_save_targets_opener_scope_not_selected_agent_workspace(self):
        self.registry.reconcile(snapshot(self.repo, moved=True), refresh_display=False)
        target = {"host": self.store.host, "terminal_id": self.sender["terminal_id"]}
        for field, value in (("main", target), ("members", [target])):
            result = self.save_settings(field, value)
            self.assertEqual((result["repo_id"], result["host"], result["workspace_id"]),
                             (self.repo_id, self.store.host, "w1"))
            self.assertEqual(result["settings"][field]["value"], value)
            other = RepoSettings(self.store).show(self.repo_id, self.store.host, "w2")
            self.assertNotIn(field, other["settings"])
        with mock.patch("mailbox_tui.herdr", side_effect=LedgerError("isolated", "No live Herdr")), \
             mock.patch("resource_scan.scan_host_resources",
                        return_value={"listeners": [], "serve": [], "error": None}):
            result = local_snapshot(self.store, self.repo_id, self.settings_environment())
        self.assertEqual(result["scope"],
                         {"repo_id": self.repo_id, "host": self.store.host, "workspace_id": "w1",
                          "requested_workspace_id": "w1"})
        self.assertEqual(result["settings"]["settings"]["main"]["value"], target)

    def test_settings_reject_out_of_scope_main_and_members(self):
        self.registry.reconcile(snapshot(self.repo, moved=True), refresh_display=False)
        targets = [{"host": self.store.host, "terminal_id": self.recipient["terminal_id"]},
                   {"host": "another-host", "terminal_id": self.sender["terminal_id"]},
                   {"host": self.store.host, "terminal_id": "inactive-or-unknown"}]
        for target in targets:
            for field, value in (("main", target), ("members", [target])):
                with self.subTest(field=field, target=target), self.assertRaises(LedgerError) as raised:
                    self.save_settings(field, value)
                self.assertEqual(raised.exception.code, "target_out_of_scope")

    def test_main_and_members_require_complete_current_opener_scope(self):
        expected = {"repo_id": self.repo_id, "host": self.store.host, "workspace_id": "w1",
                    "requested_workspace_id": "w1"}
        for scope in ({"repo_id": self.repo_id}, {**expected, "host": "another-host"},
                      {**expected, "workspace_id": "w2"}, {**expected, "repo_id": "another-repo"},
                      {**expected, "requested_workspace_id": "w2"},
                      {key: value for key, value in expected.items() if key != "requested_workspace_id"}):
            for field, value in (("main", None), ("members", [])):
                with self.subTest(scope=scope, field=field), self.assertRaises(LedgerError) as raised:
                    self.save_settings(field, value, scope=scope)
                self.assertEqual(raised.exception.code, "invalid_settings_scope")
        environment = self.settings_environment()
        environment.pop(OPENER_TERMINAL)
        environment[OPENER_PANE] = "missing:pane"
        for field, value in (("main", None), ("members", [])):
            with self.subTest(field=field), self.assertRaises(LedgerError) as raised:
                self.save_settings(field, value, environment=environment)
            self.assertEqual(raised.exception.code, "invalid_settings_scope")

    def test_mode_and_thresholds_need_only_repo_id_even_without_opener_workspace(self):
        thresholds = {**DEFAULT_THRESHOLDS, "idle_minutes": 42}
        for missing_opener in (False, True):
            environment = self.settings_environment()
            if missing_opener:
                environment.pop(OPENER_TERMINAL)
                environment[OPENER_PANE] = "missing:pane"
            for field, value in (("mode", "manual"), ("thresholds", thresholds)):
                with self.subTest(missing_opener=missing_opener, field=field), \
                     mock.patch("ledger_display.LedgerDisplay.refresh", return_value={"updated": [], "failed": []}), \
                     mock.patch("mailbox_tui.herdr", side_effect=LedgerError("isolated", "No live Herdr")):
                    result = execute_job({"operation": "setting", "repo_id": self.repo_id,
                                          "field": field, "value": value}, environment)
                self.assertEqual(result["settings"][field]["value"], value)
                self.assertEqual(RepoSettings(self.store).show(self.repo_id)["settings"][field]["value"], value)

    def test_settings_opener_terminal_tracks_workspace_move(self):
        environment = self.settings_environment()
        with self.store.transaction() as connection:
            for table in ("agents", "terminal_slots"):
                connection.execute(f"UPDATE {table} SET workspace_id='w2',pane_id='w2:p7' "
                                   "WHERE host=? AND terminal_id=?", (self.store.host, self.sender["terminal_id"]))
        with self.assertRaises(LedgerError) as raised:
            self.save_settings("main", None, environment=environment)
        self.assertEqual(raised.exception.code, "invalid_settings_scope")
        scope = {"repo_id": self.repo_id, "host": self.store.host, "workspace_id": "w2",
                 "requested_workspace_id": "w2"}
        result = self.save_settings("main", None, scope=scope, environment=environment)
        self.assertEqual(result["workspace_id"], "w2")

    def test_settings_mode_and_thresholds_remain_repository_wide(self):
        self.registry.reconcile(snapshot(self.repo, moved=True), refresh_display=False)
        thresholds = {**DEFAULT_THRESHOLDS, "idle_minutes": 42}
        self.save_settings("mode", "manual")
        self.save_settings("thresholds", thresholds)
        other = RepoSettings(self.store).show(self.repo_id, self.store.host, "w2")
        self.assertEqual(other["mode"], "manual")
        self.assertEqual(other["settings"]["thresholds"]["value"], thresholds)
        self.assertNotIn("main", other["settings"])
        self.assertNotIn("members", other["settings"])

    def test_settings_retains_existing_unavailable_member_but_rejects_moved_member(self):
        target = {"host": self.store.host, "terminal_id": self.recipient["terminal_id"]}
        self.save_settings("members", [target])
        with self.store.transaction() as connection:
            connection.execute("UPDATE agents SET active=0 WHERE host=? AND terminal_id=?",
                               (self.store.host, self.recipient["terminal_id"]))
        result = self.save_settings("members", [target])
        self.assertEqual(result["members"], [target])
        self.registry.reconcile(snapshot(self.repo, moved=True), refresh_display=False)
        with self.assertRaises(LedgerError) as raised:
            self.save_settings("members", [target])
        self.assertEqual(raised.exception.code, "target_out_of_scope")
        self.assertEqual(self.save_settings("members", [])["members"], [])

    def target(self):
        return {"host": None, "transport_host": "ssh-selected", "local": False,
                "terminal_id": "spawn-terminal", "session_ref": "spawn-session", "client": "omp",
                "pane_id": "spawn-pane", "address": "ssh-selected/new-worker",
                "workspace_id": "spawn-workspace", "tab_id": "spawn-tab", "created": "pane"}

    def spawn(self, operation_id, **overrides):
        options = {"target": self.target(), "body": "One initial assignment 한글",
                   "sender": {"kind": "unknown"}, "readiness_evidence": '{"ready":true}'}
        options.update(overrides)
        return self.mail.claim_spawn_submission(operation_id, **options)

    def test_user_mail_has_no_fabricated_agent_and_reply_does_not_handle_parent(self):
        parent = self.message()
        reply = self.mail.send_user(self.repo_id, self.sender, body="User follow-up", kind="action",
                                    reply_to=parent["message_id"], subject="not a new subject")
        self.assertEqual(reply["thread_id"], parent["thread_id"])
        messages = self.mail.show_thread(parent["thread_id"])["messages"]
        user = next(row for row in messages if row["message_id"] == reply["message_id"])
        self.assertEqual((user["sender"], user["sender_label"], user["body"]),
                         ("user", "user", "User follow-up"))
        self.assertEqual(self.state(parent["message_id"])["state"], "queued")
        self.assertEqual(len(self.registry.list()), 2)

    def test_user_cross_repo_keeps_sender_context_and_recipient_snapshot(self):
        other = self.root / "other-repo"
        other.mkdir()
        self.registry.reconcile(snapshot(self.repo, recipient_repo=other), refresh_display=False)
        recipient = next(row for row in self.registry.list() if row["terminal_id"] == "term-b")
        sent = self.mail.send_user(self.repo_id, recipient, body="Cross-repo user task")
        row = self.mail.read(recipient, message_id=sent["message_id"])["messages"][0]
        self.assertEqual((row["repo_id"], row["sender_repo_id"], row["recipient_repo_id"]),
                         (self.repo_id, self.repo_id, recipient["repo_id"]))
        self.assertIsNone(row["hold_reason"])
        self.assertEqual(row["sender"], "user")

    def test_cross_repo_operator_recovery_guards_recipient_snapshot(self):
        other = self.root / "other-repo"
        other.mkdir()
        self.registry.reconcile(snapshot(self.repo, recipient_repo=other), refresh_display=False)
        self.recipient = next(row for row in self.registry.list() if row["terminal_id"] == "term-b")
        for moved in (False, True):
            with self.subTest(moved=moved):
                self.registry.reconcile(snapshot(self.repo, recipient_repo=other), refresh_display=False)
                self.recipient = next(row for row in self.registry.list() if row["terminal_id"] == "term-b")
                sent = self.message()
                claim = self.claim()
                self.registry.reconcile(snapshot(self.repo, recipient_repo=self.repo if moved else other,
                                                 recipient_session=f"replacement-{moved}"),
                                        refresh_display=False)
                result = self.mail.resolve_claim(**claim, disposition="not_applied",
                                                 evidence="Original journal did not admit context")["receipts"][0]
                self.assertEqual(result["state"], "queued")
                row = self.mail.show_thread(sent["thread_id"])["messages"][0]
                self.assertEqual(row["hold_reason"], "recipient_changed" if moved else None)

    def test_worker_start_failure_preserves_committed_user_message(self):
        for error in (LedgerError("busy", "worker unavailable"), OSError("worker unavailable")):
            with self.subTest(error=type(error).__name__):
                with mock.patch("ledger_worker.ensure_worker", side_effect=error):
                    sent = Mailbox(self.store, {}).send_user(self.repo_id, self.recipient, body="Committed user mail")
                self.assertEqual(self.state(sent["message_id"])["state"], "queued")
                self.assertTrue(sent["warnings"][0]["committed"])
                self.assertEqual(self.mail.read(self.recipient, message_id=sent["message_id"])["messages"][0]["sender"], "user")

    def test_pending_remote_threads_are_visible_without_changing_delivery(self):
        with self.store.transaction() as connection:
            connection.execute("INSERT INTO peers(canonical_name,uuid,ssh_alias) VALUES (?,?,?)",
                               ("beta", str(uuid.uuid4()), "beta-ssh"))
        remote = {**self.recipient, "host": "beta"}
        sent = self.mail.send_user(self.repo_id, remote, body="Pending transport", subject="Remote")
        before = self.state(sent["message_id"])
        threads = self.mail.list_threads(self.repo_id)["threads"]
        self.assertEqual(threads[0]["thread_id"], sent["thread_id"])
        self.assertEqual(threads[0]["subject"], "Remote")
        self.assertEqual(threads[0]["messages"][0]["body"], "Pending transport")
        self.assertEqual(threads[0]["messages"][0]["state"], "pending")
        self.assertEqual(self.state(sent["message_id"]), before)
        self.assertEqual(self.mail.list_threads("other-repository")["threads"], [])

    def test_direct_persists_exact_supplied_body_without_native_delivery(self):
        body = user_prompt_text("  User text 한글\n")
        direct = self.mail.begin_direct(self.repo_id, self.recipient, body=body, subject="Direct")
        self.assertEqual((direct["via"], direct["sender"], direct["acceptance"], direct["body"]),
                         ("direct", "user", "submitting", body))
        accepted = self.mail.finish_direct(direct["submission_id"], acceptance="accepted", evidence="Herdr accepted terminal input")
        for later in ("unknown", "failed", "accepted"):
            self.assertEqual(self.mail.finish_direct(direct["submission_id"], acceptance=later,
                                                    evidence="late response"), accepted)
        listed = self.mail.list_direct(self.repo_id)["submissions"]
        self.assertEqual(listed[0]["body"], body)
        self.assertEqual(listed[0]["acceptance"], "accepted")
        self.assertNotIn("delivered_at", listed[0])
        self.assertEqual(self.mail.list_threads(self.repo_id)["threads"], [])

    def test_direct_unknown_and_failed_remain_truthful(self):
        for acceptance in ("unknown", "failed"):
            direct = self.mail.begin_direct(self.repo_id, self.recipient, body="Exact text")
            result = self.mail.finish_direct(direct["submission_id"], acceptance=acceptance,
                                            evidence="Transport response")
            self.assertEqual(result["acceptance"], acceptance)
            self.assertEqual(result["body"], "Exact text")
        self.assertEqual(self.mail.list_direct("other-repository")["submissions"], [])

    def test_spawn_has_one_permission_across_concurrent_claims_and_all_states(self):
        operation = str(uuid.uuid4())
        with ThreadPoolExecutor(max_workers=6) as workers:
            results = list(workers.map(lambda _: self.spawn(operation), range(12)))
        self.assertEqual(sum(row["should_submit"] for row in results), 1)
        self.assertEqual(len({row["submission_id"] for row in results}), 1)
        self.assertEqual(self.mail.get_spawn_submission(operation)["acceptance"], "submitting")
        for acceptance in ("unknown", "failed", "accepted"):
            self.mail.finish_spawn_submission(operation, acceptance=acceptance, evidence="Observed transport outcome")
            self.assertFalse(self.spawn(operation)["should_submit"])
        self.assertEqual(self.mail.get_spawn_submission(operation)["acceptance"], "accepted")
        self.assertEqual(self.mail.list_direct()["submissions"], [])

    def test_spawn_immutable_binding_rejects_changed_body_session_sender_or_alias(self):
        operation = str(uuid.uuid4())
        self.spawn(operation)
        variants = [{"body": "Different body"},
                    {"target": {**self.target(), "session_ref": "replacement"}},
                    {"target": {**self.target(), "transport_host": "other-ssh"}},
                    {"sender": {"kind": "non-agent"}}]
        for options in variants:
            with self.subTest(options=options):
                with self.assertRaises(LedgerError) as raised:
                    self.spawn(operation, **options)
                self.assertEqual(raised.exception.code, "submission_conflict")
        reopened = Mailbox(LedgerStore(self.store.directory))
        record = reopened.get_spawn_submission(operation)
        self.assertEqual(record["acceptance"], "submitting")
        self.assertIsNone(record["target"]["host"])
        self.assertEqual(record["target"]["transport_host"], "ssh-selected")
        self.assertEqual(record["sender"], {"kind": "unknown"})

    def test_spawn_agent_authorship_and_accepted_result_never_regress(self):
        sender = {key: self.sender[key] for key in ("host", "terminal_id", "session_ref", "client", "agent_id")}
        operation = str(uuid.uuid4())
        self.spawn(operation, sender={"kind": "agent", **sender})
        accepted = self.mail.finish_spawn_submission(operation, acceptance="accepted", evidence="Transport acceptance")
        for acceptance in ("failed", "unknown", "accepted"):
            self.assertEqual(self.mail.finish_spawn_submission(operation, acceptance=acceptance, evidence="late"), accepted)
        self.assertEqual(accepted["sender_kind"], "agent")
        self.assertEqual(accepted["sender"]["agent_id"], self.sender["agent_id"])
        self.assertEqual(accepted["via"], "spawn_initial")

    def test_operator_applied_uses_old_claim_identity_and_propagates_explicit_source(self):
        sent = self.message()
        claim = self.claim()
        self.registry.reconcile(snapshot(self.repo, recipient_session="replacement"))
        with self.store.transaction() as connection:
            connection.execute("UPDATE message_routes SET receipt_host='upstream' WHERE message_id=?", (sent["message_id"],))
        resolved = self.mail.resolve_claim(**claim, disposition="applied", evidence="Inspected original session journal")["receipts"][0]
        self.assertEqual(resolved["state"], "delivered")
        self.assertEqual(resolved["via"], "wake")
        self.assertEqual(resolved["delivered_agent_id"], claim["agent_id"])
        self.assertEqual(resolved["confirmation_source"], "operator")
        self.assertIn("Inspected original session journal", resolved["evidence"])
        shown = self.mail.show_thread(sent["thread_id"])["messages"][0]
        self.assertEqual(shown["confirmation_source"], "operator")
        with self.store.read() as connection:
            payload = json.loads(connection.execute("SELECT payload FROM outbox WHERE kind='receipt' ORDER BY peer_seq DESC LIMIT 1").fetchone()[0])
            audit = connection.execute("SELECT * FROM claim_resolutions WHERE claim_id=?", (claim["claim_id"],)).fetchone()
            self.assertEqual(audit["agent_id"], claim["agent_id"])
        self.assertEqual(payload["confirmation_source"], "operator")
        self.assertEqual(payload["evidence"], resolved["evidence"])
        self.assertEqual(self.mail.resolve_claim(**claim, disposition="applied", evidence="Inspected original session journal")["receipts"][0], resolved)

    def test_operator_not_applied_releases_only_old_claim_and_does_not_submit(self):
        first = self.message()
        claim = self.claim()
        second = self.message()
        self.registry.reconcile(snapshot(self.repo, recipient_session="replacement"))
        resolved = self.mail.resolve_claim(**claim, disposition="not_applied", evidence="No original journal admission")["receipts"][0]
        self.assertEqual((resolved["state"], resolved["confirmation_source"]), ("queued", "operator"))
        self.assertIsNone(resolved["delivered_agent_id"])
        self.assertEqual(self.state(second["message_id"])["state"], "queued")
        recipient = next(row for row in self.registry.list() if row["terminal_id"] == "term-b")
        fresh = self.mail.claim(terminal_id=recipient["terminal_id"], agent_id=recipient["agent_id"], generation=recipient["generation"])
        self.assertEqual(len(fresh["messages"]), 2)
        started = self.mail.started(**{key: fresh[key] for key in ("claim_id", "agent_id", "generation")}, evidence="Native context admitted")["receipts"]
        self.assertTrue(all(row["confirmation_source"] == "native" for row in started))
        self.assertTrue(all(row["evidence"] == "Native context admitted" for row in started))
        self.assertEqual(self.state(first["message_id"])["delivered_agent_id"], recipient["agent_id"])

    def test_claim_resolution_rejects_wrong_identity_blank_evidence_and_conflicting_repeat(self):
        sent = self.message()
        claim = self.claim()
        for changes in ({"agent_id": str(uuid.uuid4())}, {"generation": claim["generation"] + 1}, {"evidence": " "}):
            options = {**claim, "disposition": "applied", "evidence": "Operator proof", **changes}
            with self.assertRaises(LedgerError):
                self.mail.resolve_claim(**options)
        self.assertEqual(self.state(sent["message_id"])["state"], "delivering")
        self.mail.resolve_claim(**claim, disposition="applied", evidence="Operator proof")
        with self.assertRaises(LedgerError):
            self.mail.resolve_claim(**claim, disposition="not_applied", evidence="Operator proof")

    def test_late_native_ack_does_not_erase_operator_confirmation(self):
        sent = self.message()
        claim = self.claim()
        resolved = self.mail.resolve_claim(**claim, disposition="applied", evidence="Operator inspected journal")["receipts"][0]
        self.mail.started(**claim, evidence="Late native ACK")
        self.assertEqual(self.state(sent["message_id"]), resolved)

    def test_display_label_cas_preserves_blank_and_rejects_stale_generation(self):
        identity = self.recipient["agent_id"]
        self.assertTrue(self.registry.set_display_label(identity, None, "HAM label")["updated"])
        self.assertTrue(self.registry.set_display_label(identity, "HAM label", "")["updated"])
        self.assertEqual(self.registry.set_display_label(identity, "HAM label", "stale")["last_ham_label"], "")
        self.registry.reconcile(snapshot(self.repo, recipient_session="replacement"))
        self.assertFalse(self.registry.set_display_label(identity, "", "old actor")["updated"])

    def test_schema_two_additive_upgrade_retains_operator_attribution(self):
        sent = self.message()
        claim = self.claim()
        self.mail.resolve_claim(**claim, disposition="applied", evidence="Historical operator proof")
        with self.store.transaction() as connection:
            connection.execute("ALTER TABLE deliveries DROP COLUMN confirmation_source")
            connection.execute("UPDATE ledger_meta SET value='2' WHERE key='schema_version'")
        upgraded = LedgerStore(self.store.directory)
        with upgraded.read() as connection:
            restored = receipt(connection, sent["message_id"])
        self.assertEqual(restored["confirmation_source"], "operator")
        self.assertEqual(restored["delivered_agent_id"], claim["agent_id"])
        self.assertEqual(restored["via"], "wake")

    def test_schema_three_upgrade_preserves_native_claim_without_guessing_journal(self):
        identity = str(uuid.uuid4())
        with self.store.transaction() as connection:
            connection.execute(
                "INSERT INTO native_claims(claim_id,client,session_ref,marker,content_sha256,created_at) "
                "VALUES (?,?,?,?,?,?)", (identity, "omp", "old-session", "old-marker", "old-hash", 1.0))
            for column in ("transcript_path", "transcript_start_offset", "content_bytes"):
                connection.execute(f"ALTER TABLE native_claims DROP COLUMN {column}")
            connection.execute("UPDATE ledger_meta SET value='3' WHERE key='schema_version'")
        upgraded = LedgerStore(self.store.directory)
        with upgraded.transaction() as connection:
            old = dict(connection.execute("SELECT * FROM native_claims WHERE claim_id=?", (identity,)).fetchone())
            self.assertEqual(old["session_ref"], "old-session")
            self.assertEqual(old["content_sha256"], "old-hash")
            self.assertTrue(all(old[key] is None for key in
                                ("transcript_path", "transcript_start_offset", "content_bytes")))
            connection.execute(
                "INSERT INTO native_claims(claim_id,client,session_ref,marker,content_sha256,created_at,"
                "transcript_path,transcript_start_offset,content_bytes) VALUES (?,?,?,?,?,?,?,?,?)",
                (str(uuid.uuid4()), "claude", "new-session", "new-marker", "new-hash", 2.0,
                 "/private-owned/transcript.jsonl", 42, 80))
        reopened = LedgerStore(self.store.directory)
        with reopened.read() as connection:
            current = connection.execute("SELECT * FROM native_claims WHERE client='claude'").fetchone()
        self.assertEqual((current["transcript_path"], current["transcript_start_offset"], current["content_bytes"]),
                         ("/private-owned/transcript.jsonl", 42, 80))


if __name__ == "__main__":
    unittest.main()
