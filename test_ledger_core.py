"""Behavioral tests use private state and real SQLite, never the user's ledger."""
import ham_test_support  # Protect the live ledger before product imports.

from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import uuid

from ledger_mail import Mailbox
from ledger_remote import RemoteLedger
from ledger_settings import RepoSettings, validate_setting
from ledger_status import AgentRegistry, normalize_origin, repository_identity
from ledger_store import LedgerError, LedgerStore
from ledger_views import status_board

ROOT = Path(__file__).resolve().parent


def snapshot(directory, *, recipient_session="session-b", recipient_repo=None,
             recipient_present=True, recipient_agent=True, moved=False):
    panes = [{"terminal_id": "term-a", "pane_id": "w1:p1", "workspace_id": "w1"}]
    agents = [{**panes[0], "agent": "codex", "agent_status": "working", "name": "orange-rabbit",
               "agent_session": {"value": "session-a"}, "cwd": str(directory)}]
    if recipient_present:
        pane = {"terminal_id": "term-b", "pane_id": "w2:p9" if moved else "w1:p2",
                "workspace_id": "w2" if moved else "w1"}
        panes.append(pane)
        if recipient_agent:
            agents.append({**pane, "agent": "omp", "agent_status": "idle", "name": "blue-owl",
                           "agent_session": {"value": recipient_session}, "cwd": str(recipient_repo or directory)})
    return {"panes": panes, "agents": agents}


class NewRepoDefaultsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.store = LedgerStore(self.root / "ledger", canonical_name="alpha")
        self.registry = AgentRegistry(self.store)
        self.settings = RepoSettings(self.store)
        self.repo_id = repository_identity(str(self.repo), self.store.host)["repo_id"]

    def register(self, **options):
        return self.registry.reconcile(snapshot(self.repo, **options), refresh_display=False)

    def test_new_repo_seeds_home_auto_once_and_reconciliation_keeps_versions(self):
        self.register(recipient_present=False)
        seeded = self.settings.show(self.repo_id)
        self.assertEqual(seeded["mode"], "auto")
        fields = seeded["settings"]
        self.assertEqual(set(fields), {"home", "mode", "computed_main"})
        self.assertEqual((fields["home"]["version"], fields["home"]["origin_host"], fields["home"]["value"]),
                         (1, "alpha", "alpha"))
        self.assertEqual((fields["mode"]["version"], fields["mode"]["origin_host"], fields["mode"]["changed_by"]),
                         (1, "!default", "home"))
        self.assertEqual(fields["computed_main"]["value"]["target"],
                         {"host": "alpha", "terminal_id": "term-a"})
        self.register(recipient_present=False)
        self.settings.reconcile_local(refresh_display=False)
        self.assertEqual(self.settings.show(self.repo_id)["settings"], fields)

    def test_local_repo_auto_stays_local_and_ambiguous_main_is_none(self):
        with self.store.transaction() as connection:
            connection.execute("INSERT INTO peers VALUES (?,?,?,?)", ("beta", str(uuid.uuid4()), "b", "/peer"))
        self.register()
        self.assertTrue(self.repo_id.startswith("local:"))
        current = self.settings.show(self.repo_id)
        self.assertEqual(current["mode"], "auto")
        self.assertIsNone(current["settings"]["computed_main"]["value"]["target"])
        self.assertEqual([self.settings.role(agent) for agent in self.registry.list()], ["worker", "worker"])
        with self.store.read() as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM outbox").fetchone()[0], 0)

    def test_known_repo_without_settings_stays_off_through_registration_and_reads(self):
        with self.store.transaction() as connection:
            connection.execute("INSERT INTO repos VALUES (?,NULL,?)", (self.repo_id, 1.0))
        self.register()
        self.settings.reconcile_local(refresh_display=False)
        with self.store.read() as connection:
            before = list(connection.iterdump())
        self.assertEqual(self.registry.repo_show(self.repo_id)["mode"], "off")
        self.assertEqual(self.settings.show("git:not-yet-observed/repo")["mode"], "off")
        self.assertTrue(all(agent["role"] is None for agent in
                            status_board(self.store, host="local", resource_scanner=lambda: {
                                "listeners": [], "serve": [], "error": None})["agents"]))
        self.assertTrue(all(agent["role"] is None for agent in RemoteLedger(self.store).status_export()["agents"]))
        self.assertEqual(Mailbox(self.store).read(self.registry.list()[0])["messages"], [])
        with self.store.read() as connection:
            self.assertEqual(list(connection.iterdump()), before)
        self.assertEqual(self.settings.show(self.repo_id)["settings"], {})

    def test_any_existing_settings_preserve_off_manual_main_members_and_home(self):
        cases = (("mode", "off"), ("mode", "manual"), ("mode", "auto"),
                 ("home", "beta"), ("main", {"host": "beta", "terminal_id": "selected"}),
                 ("members", [{"host": "beta", "terminal_id": "selected"}]))
        for index, (field, value) in enumerate(cases):
            with self.subTest(field=field, value=value):
                store = LedgerStore(self.root / f"existing-{index}", canonical_name="alpha")
                settings = RepoSettings(store)
                with store.transaction() as connection:
                    settings._apply(connection, {"repo_id": self.repo_id, "field": field, "value": value,
                                                 "version": 1, "origin_host": "beta",
                                                 "changed_by": "user", "changed_at": 1.0})
                before = settings.show(self.repo_id)["settings"]
                AgentRegistry(store).reconcile(snapshot(self.repo), refresh_display=False)
                settings.reconcile_local(refresh_display=False)
                current = settings.show(self.repo_id)
                self.assertEqual(current["settings"], before)
                self.assertEqual(current["mode"], value if field == "mode" else "off")

    def test_seed_uses_existing_protocol_validation_and_lowest_origin_order(self):
        self.register()
        seed = self.settings.show(self.repo_id)["settings"]["mode"]
        self.assertEqual(validate_setting(seed), seed)
        with self.assertRaises(LedgerError):
            validate_setting({**seed, "version": 0})
        # Explicit names begin with ASCII alnum; normalized default hostnames
        # additionally permit "-", "." and "_", which also sort after "!".
        for host in ("0", "A", "a", "z-host", "-host", ".host", "_host"):
            self.assertLess((seed["version"], seed["origin_host"]), (1, host))

    def test_seed_failure_rolls_back_repository_agents_and_publication(self):
        apply = RepoSettings._apply

        def fail_mode(settings, connection, value, **options):
            if value["field"] == "mode":
                raise LedgerError("invalid_setting", "Injected atomic seed failure")
            return apply(settings, connection, value, **options)

        with patch.object(RepoSettings, "_apply", fail_mode):
            with self.assertRaises(LedgerError):
                self.register()
        with self.store.read() as connection:
            for table in ("repos", "repo_settings", "agents", "terminal_slots", "outbox"):
                self.assertEqual(connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0], 0)


class ReadonlyLedgerBootstrapTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.directory = self.root / "ledger"
        self.environment = {"HERDR_AGENT_LEDGER_DIR": str(self.directory)}

    def test_absent_readonly_ledger_does_not_create_directory(self):
        self.assertIsNone(LedgerStore.open_readonly(self.environment))
        self.assertFalse(self.directory.exists())

    def test_readonly_first_bootstrap_sqlite_errors_become_not_initialized(self):
        self.directory.mkdir()
        path = self.directory / "ledger.sqlite3"
        for contents in (b"", b"not a sqlite database"):
            with self.subTest(contents=contents):
                path.write_bytes(contents)
                before = path.read_bytes()
                with self.assertRaises(LedgerError) as raised:
                    LedgerStore.open_readonly(self.environment)
                self.assertEqual(raised.exception.code, "ledger_not_initialized")
                self.assertIsInstance(raised.exception.__cause__, sqlite3.Error)
                self.assertEqual(path.read_bytes(), before)
                self.assertEqual({item.name for item in self.directory.iterdir()}, {"ledger.sqlite3"})

    def test_readonly_first_bootstrap_missing_metadata_becomes_not_initialized(self):
        self.directory.mkdir()
        with sqlite3.connect(self.directory / "ledger.sqlite3") as connection:
            connection.execute("CREATE TABLE ledger_meta(key TEXT PRIMARY KEY,value TEXT)")
        for key, value in (("self_canonical_name", "alpha"), ("self_uuid", "fixture-uuid"),
                           ("history_cursor_key", "00" * 32)):
            with self.subTest(missing=key):
                with self.assertRaises(LedgerError) as raised:
                    LedgerStore.open_readonly(self.environment)
                self.assertEqual(raised.exception.code, "ledger_not_initialized")
                self.assertIsInstance(raised.exception.__cause__, KeyError)
                with sqlite3.connect(self.directory / "ledger.sqlite3") as connection:
                    connection.execute("INSERT INTO ledger_meta VALUES (?,?)", (key, value))
        store = LedgerStore.open_readonly(self.environment)
        self.assertEqual((store.host, store.host_uuid), ("alpha", "fixture-uuid"))
        self.assertFalse((self.directory / "bootstrap.lock").exists())
        self.assertFalse((self.directory / "bell").exists())


class LedgerCoreTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.store = LedgerStore(self.root / "ledger", canonical_name="alpha")
        self.registry = AgentRegistry(
            self.store, display_runner=lambda command: subprocess.CompletedProcess(
                command, 0, '{"result":{"pane":{"label":"manual fixture"}}}', ""))
        self.mail = Mailbox(self.store)
        self.registry.reconcile(snapshot(self.repo))
        self.sender, self.recipient = self.agents()

    def agents(self):
        agents = self.registry.list()
        return next(a for a in agents if a["terminal_id"] == "term-a"), next(a for a in agents if a["terminal_id"] == "term-b")

    def send(self, **options):
        return self.mail.send(self.sender, self.recipient, body="Please inspect the changed boundary. 한글", **options)

    def payload(self, message_id=None):
        identity = message_id or str(uuid.uuid4())
        return {"message_id": identity, "thread_id": identity, "repo_id": self.sender["repo_id"],
                "reply_to": None, "origin_host": "alpha", "sender": self.sender["agent_id"],
                "sender_label": "orange-rabbit", "kind": "fyi", "hop": 0, "body": "Immutable text",
                "subject": "Review", "created_at": 100.0, "recipient_host": "alpha",
                "recipient_terminal_id": "term-b", "recipient_address": "alpha/blue-owl"}

    def claim(self, **options):
        return self.mail.claim(terminal_id="term-b", agent_id=self.recipient["agent_id"],
                               generation=self.recipient["generation"], **options)

    def claim_identity(self, claim):
        return {key: claim[key] for key in ("claim_id", "agent_id", "generation")}

    def database_snapshot(self):
        with self.store.read() as connection:
            data = '\n'.join(connection.iterdump())
        bells = {p.name: (p.stat().st_mtime_ns, p.read_bytes()) for p in self.store.bell_directory.iterdir()}
        return hashlib.sha256(data.encode()).hexdigest(), bells

    def test_host_persistence_permissions_and_wrong_reopen(self):
        other = LedgerStore(self.store.directory)
        self.assertEqual(other.host_uuid, self.store.host_uuid)
        self.assertEqual(other.host, "alpha")
        self.assertEqual(self.store.directory.stat().st_mode & 0o777, 0o700)
        self.assertEqual(self.store.path.stat().st_mode & 0o777, 0o600)
        with self.assertRaises(LedgerError) as caught:
            LedgerStore(self.store.directory, canonical_name="beta")
        self.assertEqual(caught.exception.code, "host_identity_conflict")

    def test_concurrent_first_bootstrap_has_one_host_identity(self):
        path = self.root / "concurrent-ledger"
        with ThreadPoolExecutor(max_workers=8) as executor:
            identities = list(executor.map(lambda _: LedgerStore(path, canonical_name="same").host_uuid, range(16)))
        self.assertEqual(len(set(identities)), 1)

    def test_read_invariance_including_same_pane_subagent_context(self):
        sent = self.send()
        before = self.database_snapshot()
        for _ in range(3):
            # Root and task-agent reads share the same terminal; neither is an ACK.
            result = self.mail.read(self.recipient, message_id=sent["message_id"])
            self.assertEqual(result["messages"][0]["state"], "queued")
        self.assertEqual(self.database_snapshot(), before)

    def test_codex_pull_handle_never_fabricates_native_delivery(self):
        sent = self.send()
        result = self.mail.handle(self.recipient, [sent["message_id"]])["receipts"][0]
        self.assertEqual(result["state"], "handled")
        self.assertIsNone(result["delivered_at"])
        self.assertIsNone(result["delivered_agent_id"])
        self.assertIsNotNone(result["handled_at"])
        self.assertEqual(self.mail.handle(self.recipient, [sent["message_id"]])["receipts"][0], result)

    def test_handle_batch_is_atomic_on_wrong_recipient(self):
        first = self.send()
        second = self.mail.send(self.recipient, self.sender, body="Response")
        with self.assertRaises(LedgerError):
            self.mail.handle(self.recipient, [first["message_id"], second["message_id"]])
        self.assertEqual(self.mail.read(self.recipient)["messages"][0]["state"], "queued")

    def test_claim_withdraw_cas_race_has_one_winner(self):
        for _ in range(12):
            sent = self.send(kind="action")
            barrier = threading.Barrier(2)
            def claim():
                barrier.wait()
                return self.claim(kinds=["action"])
            def withdraw():
                barrier.wait()
                return self.mail.withdraw(sent["message_id"], sender=self.sender)
            with ThreadPoolExecutor(max_workers=2) as executor:
                one, two = executor.submit(claim), executor.submit(withdraw)
                claimed, withdrawn = one.result(), two.result()
            included = any(m["message_id"] == sent["message_id"] for m in claimed["messages"])
            self.assertNotEqual(included, withdrawn["withdrawn"])
            if included:
                self.mail.release(**self.claim_identity(claimed), disposition="not_applied", evidence="test:confirmed-no-submit")
                self.mail.withdraw(sent["message_id"], sender=self.sender)

    def test_parallel_sends_and_claims_have_no_lost_or_duplicate_deliveries(self):
        with ThreadPoolExecutor(max_workers=8) as executor:
            sent = list(executor.map(lambda _: self.send(), range(32)))
        with ThreadPoolExecutor(max_workers=4) as executor:
            claims = list(executor.map(lambda _: self.claim(), range(4)))
        ids = [m["message_id"] for claim in claims for m in claim["messages"]]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(set(ids), {item["message_id"] for item in sent})

    def test_claim_ack_exact_generation_and_nonempty_evidence(self):
        sent = self.send()
        claim = self.claim()
        identity = self.claim_identity(claim)
        with self.assertRaises(LedgerError) as caught:
            self.mail.started(**identity, evidence="")
        self.assertEqual(caught.exception.code, "missing_evidence")
        with self.assertRaises(LedgerError):
            self.mail.started(**{**identity, "generation": 999}, evidence="test:context")
        self.assertEqual(self.mail.read(self.recipient)["messages"][0]["state"], "delivering")
        result = self.mail.started(**identity, evidence="test:context-applied")["receipts"][0]
        self.assertEqual(result["state"], "delivered")
        self.assertEqual(result["delivered_agent_id"], self.recipient["agent_id"])
        self.assertEqual(self.mail.started(**identity, evidence="test:duplicate"), {"receipts": []})
        self.assertFalse(self.mail.withdraw(sent["message_id"], sender=self.sender)["withdrawn"])

    def test_failed_acceptance_can_release_but_unknown_never_replays(self):
        self.send(kind="action")
        claim = self.claim()
        identity = self.claim_identity(claim)
        self.assertEqual(self.mail.release(**identity, disposition="unknown", evidence="test:timeout"), {"released": 0, "uncertain": 1})
        self.assertIsNone(self.claim()["claim_id"])
        self.assertEqual(self.mail.release(**identity, disposition="not_applied", evidence="test:confirmed-cancel"), {"released": 1, "uncertain": 0})
        self.assertIsNotNone(self.claim()["claim_id"])

    def test_unknown_claim_survives_session_replacement_and_terminal_close(self):
        self.send()
        claim = self.claim()
        identity = self.claim_identity(claim)
        self.registry.reconcile(snapshot(self.repo, recipient_session="new-session"))
        self.sender, self.recipient = self.agents()
        self.assertEqual(self.recipient["generation"], 2)
        self.assertIsNone(self.claim()["claim_id"])
        with self.assertRaises(LedgerError) as caught:
            self.mail.started(**identity, evidence="test:old-context")
        self.assertEqual(caught.exception.code, "stale_session")
        self.registry.reconcile(snapshot(self.repo, recipient_present=False))
        with self.store.read() as connection:
            row = connection.execute("SELECT state,hold_reason FROM deliveries").fetchone()
        self.assertEqual(tuple(row), ("delivering", "acceptance_unknown"))
        self.mail.release(**identity, disposition="not_applied", evidence="test:transcript-proves-not-applied")
        self.registry.reconcile(snapshot(self.repo, recipient_present=False))
        with self.store.read() as connection:
            self.assertEqual(connection.execute("SELECT state FROM deliveries").fetchone()[0], "undeliverable")

    def test_absent_other_repo_and_same_repo_replacement(self):
        self.send()
        self.registry.reconcile(snapshot(self.repo, recipient_agent=False))
        with self.store.read() as connection:
            self.assertEqual(connection.execute("SELECT hold_reason FROM deliveries").fetchone()[0], "recipient_absent")
        other = self.root / "other"
        other.mkdir()
        self.registry.reconcile(snapshot(self.repo, recipient_repo=other, recipient_session="other-session"))
        self.sender, self.recipient = self.agents()
        self.assertEqual(self.claim()["messages"], [])
        self.assertEqual(self.mail.read(self.recipient)["messages"], [])
        with self.store.read() as connection:
            self.assertEqual(connection.execute("SELECT hold_reason FROM deliveries").fetchone()[0], "recipient_changed")
        self.registry.reconcile(snapshot(self.repo, recipient_session="same-repo-new-session"))
        self.sender, self.recipient = self.agents()
        self.assertEqual(len(self.claim()["messages"]), 1)

    def test_terminal_move_keeps_slot_and_generation(self):
        self.send()
        old = self.recipient
        self.registry.reconcile(snapshot(self.repo, moved=True))
        moved = self.registry.resolve_context({"HERDR_TERMINAL_ID": "term-b"})
        self.assertEqual((moved["terminal_id"], moved["agent_id"], moved["generation"]),
                         (old["terminal_id"], old["agent_id"], old["generation"]))
        self.assertEqual(moved["pane_id"], "w2:p9")
        self.assertEqual(self.mail.read(moved)["messages"][0]["pane_id"], "w2:p9")

    def test_inherited_pane_alone_is_not_process_identity(self):
        with self.assertRaises(LedgerError) as caught:
            self.registry.resolve_context({"HERDR_PANE_ID": "w1:p2"}, {"panes": [], "agents": []})
        self.assertEqual(caught.exception.code, "sender_unresolved")

    def test_unreported_session_does_not_change_generation_on_display_revision(self):
        value = snapshot(self.repo)
        for raw in value["agents"]:
            raw.pop("agent_session")
            raw["revision"] = 2
        self.registry.reconcile(value)
        before = [(a["agent_id"], a["generation"]) for a in self.registry.list()]
        for raw in value["agents"]:
            raw["revision"] = 3
            raw["agent_status"] = "working"
        self.registry.reconcile(value)
        self.assertEqual([(a["agent_id"], a["generation"]) for a in self.registry.list()], before)

    def test_stale_extension_registration_does_not_replace_current_session(self):
        old = self.recipient
        self.registry.reconcile(snapshot(self.repo, recipient_session="replacement"))
        with self.assertRaises(LedgerError) as caught:
            self.registry.agent_session(terminal_id="term-b", session_ref=old["session_ref"],
                                        client=old["client"], repo_id=old["repo_id"])
        self.assertEqual(caught.exception.code, "stale_session")
        new = self.agents()[1]
        self.assertEqual(new["session_ref"], "replacement")
        self.assertFalse(new["extension_present"])
        result = self.registry.agent_session(terminal_id="term-b", session_ref=new["session_ref"], client=new["client"], repo_id=new["repo_id"])
        self.assertTrue(result["extension_present"])

    def test_incomplete_snapshot_does_not_close_agents_or_deliveries(self):
        self.send()
        before = self.database_snapshot()
        with self.assertRaises(LedgerError):
            self.registry.reconcile({"agents": []})
        self.assertEqual(self.database_snapshot(), before)

    def test_withdraw_before_accept_tombstone_and_changed_payload_conflict(self):
        value = self.payload()
        self.mail.withdraw(value["message_id"])
        self.assertEqual(self.mail.enqueue(value)["state"], "withdrawn")
        self.assertEqual(self.mail.enqueue(value)["state"], "withdrawn")
        with self.assertRaises(LedgerError) as caught:
            self.mail.enqueue({**value, "body": "changed"})
        self.assertEqual(caught.exception.code, "message_conflict")

    def test_permanent_dedupe_survives_body_retention(self):
        value = self.payload()
        self.mail.enqueue(value)
        final = self.mail.handle(self.recipient, [value["message_id"]])["receipts"][0]
        result = self.store.retention(now=time.time() + 91 * 86400)
        self.assertEqual(result["messages"], 1)
        self.assertEqual(self.mail.read(self.recipient, all_states=True)["messages"], [])
        self.assertEqual(self.mail.enqueue(value), final)
        with self.assertRaises(LedgerError) as caught:
            self.mail.enqueue({**value, "body": "reused ID"})
        self.assertEqual(caught.exception.code, "message_conflict")
        self.assertEqual(self.store.doctor()["counts"]["messages"], 0)
        self.assertEqual(self.store.doctor()["counts"]["dedupe_receipts"], 1)

    def test_bell_failure_after_commit_is_success_with_durable_notification_warning(self):
        board = self.store.bell_directory / "_board"
        board.unlink()
        board.mkdir()
        sent = self.send()
        self.assertEqual(sent["state"], "queued")
        self.assertEqual(sent["warnings"][0]["code"], "notification_failed")
        self.assertTrue(sent["warnings"][0]["committed"])
        self.assertEqual(self.store.doctor()["counts"]["messages"], 1)
        self.assertTrue(self.store.doctor()["notification_failures"])
        self.assertEqual(self.mail.read(self.recipient)["messages"][0]["message_id"], sent["message_id"])

    def test_repeated_withdraw_preserves_original_final_timestamp_and_receipt(self):
        sent = self.send()
        first = self.mail.withdraw(sent["message_id"], sender=self.sender)
        with self.store.read() as connection:
            final_at = connection.execute("SELECT final_at FROM messages WHERE message_id=?", (sent["message_id"],)).fetchone()[0]
        second = self.mail.withdraw(sent["message_id"], sender=self.sender)
        with self.store.read() as connection:
            self.assertEqual(connection.execute("SELECT final_at FROM messages WHERE message_id=?", (sent["message_id"],)).fetchone()[0], final_at)
        self.assertEqual(first, second)

    def test_invalid_body_rolls_back_thread_and_message(self):
        before = self.database_snapshot()
        for body in ("", "한" * 6000):
            with self.assertRaises(LedgerError):
                self.mail.send(self.sender, self.recipient, body=body)
        self.assertEqual(self.database_snapshot(), before)

    def test_reply_thread_hop_and_handled_parent_are_one_transaction(self):
        first = self.send(subject="Thread title", kind="action")
        reply = self.mail.send(self.recipient, self.sender, body="Reviewed", reply_to=first["message_id"])
        self.assertEqual(reply["thread_id"], first["thread_id"])
        thread = self.mail.show_thread(first["thread_id"])
        self.assertEqual(thread["subject"], "Thread title")
        self.assertEqual(thread["messages"][0]["state"], "handled")
        self.assertEqual(thread["messages"][1]["hop"], 1)
        self.assertIsNone(thread["messages"][0]["delivered_at"])

    def test_status_log_retention_preserves_current_state(self):
        self.registry.set_status(self.sender, phase="review", summary="Inspecting CAS")
        before = self.registry.list()
        result = self.store.retention(now=time.time() + 31 * 86400)
        self.assertGreaterEqual(result["status_log"], 1)
        self.assertEqual(self.registry.list(), before)

    def test_launcher_doctor_and_usage_are_actual_subprocesses(self):
        environment = {**os.environ, "HERDR_AGENT_LEDGER_DIR": str(self.store.directory),
                       "PYTHONDONTWRITEBYTECODE": "1"}
        cli = ROOT / ".agents/skills/herdr-agent-messenger/scripts/herdr_agent_messenger.py"
        result = subprocess.run([sys.executable, str(cli), "ledger", "doctor"], env=environment,
                                capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["host"]["canonical_name"], "alpha")
        result = subprocess.run([sys.executable, str(cli), "mail", "send", "--to", "local/blue-owl"],
                                env=environment, capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 2)


class RepositoryIdentityTest(unittest.TestCase):
    def test_standard_origin_spellings_converge_without_losing_nonstandard_port_or_path_case(self):
        values = ["https://GitHub.com/Owner/Repo.git", "ssh://git@github.com/Owner/Repo.git",
                  "git@github.com:Owner/Repo.git", "https://github.com:443/Owner/Repo/",
                  "ssh://git@github.com:22/Owner/Repo"]
        self.assertEqual({normalize_origin(value) for value in values}, {"git:github.com/Owner/Repo"})
        self.assertEqual(normalize_origin("ssh://git@host:2222/Owner/Repo.git"), "git:host:2222/Owner/Repo")
        self.assertNotEqual(normalize_origin("https://host/owner/repo"), normalize_origin("https://host/Owner/Repo"))
        self.assertEqual(normalize_origin("ssh://git@[::1]:2222/o/r.git"), "git:[::1]:2222/o/r")

    def test_explicit_repo_identity_preserves_configured_rename_and_worktree_main_detection(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo, worktree = root / "repo", root / "worktree"
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            subprocess.run(["git", "-C", str(repo), "-c", "user.name=Test", "-c", "user.email=t@example.test",
                            "commit", "--allow-empty", "-qm", "base"], check=True)
            subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", "https://host/old/name.git"], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "ham.repo-id", "git:host/stable/name"], check=True)
            subprocess.run(["git", "-C", str(repo), "remote", "set-url", "origin", "git@host:new/name.git"], check=True)
            subprocess.run(["git", "-C", str(repo), "worktree", "add", "-qb", "test", str(worktree)], check=True)
            main = repository_identity(str(repo), "alpha")
            linked = repository_identity(str(worktree), "alpha")
            self.assertEqual(main["repo_id"], "git:host/stable/name")
            self.assertEqual(main["repo_id"], linked["repo_id"])
            self.assertTrue(main["is_main_checkout"])
            self.assertFalse(linked["is_main_checkout"])


if __name__ == "__main__":
    unittest.main()
