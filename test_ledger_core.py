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

from ledger_mail import Mailbox, payload_hash, validate_payload
from ledger_remote import RemoteLedger
from ledger_settings import RepoSettings, validate_setting
from ledger_status import AgentRegistry, normalize_origin, repository_identity
from ledger_store import SCHEMA_VERSION, LedgerError, LedgerStore
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
        seeded = self.settings.show(self.repo_id, self.store.host, "w1")
        self.assertEqual(seeded["mode"], "auto")
        fields = seeded["settings"]
        self.assertEqual(set(fields), {"home", "mode"})
        self.assertEqual((fields["home"]["version"], fields["home"]["origin_host"], fields["home"]["value"]),
                         (1, "alpha", "alpha"))
        self.assertEqual((fields["mode"]["version"], fields["mode"]["origin_host"], fields["mode"]["changed_by"]),
                         (1, "!default", "home"))
        self.assertEqual(seeded["main"], {"host": "alpha", "terminal_id": "term-a"})
        self.assertEqual((seeded["host"], seeded["workspace_id"]), ("alpha", "w1"))
        self.assertEqual(seeded["members"], [])
        self.register(recipient_present=False)
        self.assertEqual(self.settings.show(self.repo_id, self.store.host, "w1")["settings"], fields)

    def test_local_repo_auto_stays_local_and_ambiguous_main_is_none(self):
        with self.store.transaction() as connection:
            connection.execute("INSERT INTO peers VALUES (?,?,?,?)", ("beta", str(uuid.uuid4()), "b", "/peer"))
        self.register()
        self.assertTrue(self.repo_id.startswith("local:"))
        current = self.settings.show(self.repo_id, self.store.host, "w1")
        self.assertEqual(current["mode"], "auto")
        self.assertIsNone(current["main"])
        self.assertEqual([self.settings.role(agent) for agent in self.registry.list()], ["worker", "worker"])
        with self.store.read() as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM outbox").fetchone()[0], 0)

    def test_known_repo_without_settings_stays_off_through_registration_and_reads(self):
        with self.store.transaction() as connection:
            connection.execute("INSERT INTO repos VALUES (?,NULL,?)", (self.repo_id, 1.0))
        self.register()
        with self.store.read() as connection:
            before = list(connection.iterdump())
        self.assertEqual(self.registry.repo_show(self.repo_id, self.store.host, "w1")["mode"], "off")
        self.assertEqual(self.settings.show("git:not-yet-observed/repo", self.store.host, "w1")["mode"], "off")
        self.assertTrue(all(agent["role"] is None for agent in
                            status_board(self.store, host="local", resource_scanner=lambda: {
                                "listeners": [], "serve": [], "error": None})["agents"]))
        self.assertTrue(all(agent["role"] is None for agent in RemoteLedger(self.store).status_export()["agents"]))
        self.assertEqual(Mailbox(self.store).read(self.registry.list()[0])["messages"], [])
        with self.store.read() as connection:
            self.assertEqual(list(connection.iterdump()), before)
        self.assertEqual(self.settings.show(self.repo_id, self.store.host, "w1")["settings"], {})

    def test_existing_repo_settings_preserve_off_manual_auto_and_home(self):
        cases = (("mode", "off"), ("mode", "manual"), ("mode", "auto"), ("home", "beta"))
        for index, (field, value) in enumerate(cases):
            with self.subTest(field=field, value=value):
                store = LedgerStore(self.root / f"existing-{index}", canonical_name="alpha")
                settings = RepoSettings(store)
                with store.transaction() as connection:
                    settings._apply(connection, {"repo_id": self.repo_id, "field": field, "value": value,
                                                 "version": 1, "origin_host": "beta",
                                                 "changed_by": "user", "changed_at": 1.0})
                before = settings.show(self.repo_id, store.host, "w1")["settings"]
                AgentRegistry(store).reconcile(snapshot(self.repo), refresh_display=False)
                current = settings.show(self.repo_id, store.host, "w1")
                self.assertEqual(current["settings"], before)
                self.assertEqual(current["mode"], value if field == "mode" else "off")

    def test_seed_uses_existing_protocol_validation_and_lowest_origin_order(self):
        self.register()
        seed = self.settings.show(self.repo_id, self.store.host, "w1")["settings"]["mode"]
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


class WorkspaceRolesTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.repo = self.root / "repo"
        self.worktree = self.root / "worktree"
        self.git("init", "-q", str(self.repo))
        self.git("-C", str(self.repo), "-c", "user.name=Workspace Test",
                 "-c", "user.email=workspace@example.invalid", "commit", "-q",
                 "--allow-empty", "-m", "initial")
        self.git("-C", str(self.repo), "config", "ham.repo-id", "git:workspace/roles")
        self.git("-C", str(self.repo), "worktree", "add", "-q", "-b", "worker", str(self.worktree))
        self.store = LedgerStore(self.root / "ledger", canonical_name="alpha")
        self.registry = AgentRegistry(self.store)
        self.settings = RepoSettings(self.store, display_runner=lambda command: subprocess.CompletedProcess(
            command, 0, '{"result":{"pane":{"label":"manual fixture"}}}', ""))
        self.repo_id = "git:workspace/roles"

    @staticmethod
    def git(*arguments):
        subprocess.run(["git", *arguments], check=True, capture_output=True, text=True)

    def register(self, *placements):
        panes = [{"terminal_id": terminal, "pane_id": "pane-" + terminal, "workspace_id": workspace}
                 for terminal, workspace, _ in placements]
        agents = [{**pane, "agent": "omp", "agent_status": "idle", "name": pane["terminal_id"],
                   "agent_session": {"value": "session-" + pane["terminal_id"]}, "cwd": str(directory)}
                  for pane, (_, _, directory) in zip(panes, placements)]
        self.registry.reconcile({"panes": panes, "agents": agents}, refresh_display=False)
        return {agent["terminal_id"]: agent for agent in self.registry.list()}

    def show(self, workspace="w1"):
        return self.settings.show(self.repo_id, self.store.host, workspace)

    def mutate(self, field, value, workspace="w1"):
        return self.settings.mutate(self.repo_id, field, value,
                                    host=self.store.host, workspace_id=workspace)

    def test_distinct_workspaces_each_elect_their_single_active_agent(self):
        agents = self.register(("a", "w1", self.repo), ("b", "w2", self.worktree))
        self.assertEqual({terminal: self.settings.role(agent) for terminal, agent in agents.items()},
                         {"a": "main", "b": "main"})
        self.assertEqual(self.show("w1")["main"], {"host": "alpha", "terminal_id": "a"})
        self.assertEqual(self.show("w2")["main"], {"host": "alpha", "terminal_id": "b"})
        self.assertIsNone(self.show("unobserved")["main"])

    def test_shared_scope_main_checkout_tie_rules(self):
        cases = (
            ((self.repo, self.worktree), {"a": "main", "b": "worker"}, "a"),
            ((self.worktree, self.worktree), {"a": "worker", "b": "worker"}, None),
            ((self.repo, self.repo), {"a": "worker", "b": "worker"}, None),
        )
        for directories, roles, main in cases:
            with self.subTest(directories=directories):
                agents = self.register(("a", "w1", directories[0]), ("b", "w1", directories[1]))
                self.assertEqual({terminal: self.settings.role(agent) for terminal, agent in agents.items()}, roles)
                self.assertEqual(self.show()["main"], {"host": "alpha", "terminal_id": main} if main else None)

    def test_moved_explicit_main_is_inert_and_auto_falls_back_without_changing_settings(self):
        self.register(("a", "w1", self.repo), ("b", "w1", self.worktree))
        explicit = {"host": "alpha", "terminal_id": "b"}
        self.mutate("main", explicit)
        self.assertEqual(self.show()["main"], explicit)
        before = self.show()["settings"]["main"]
        agents = self.register(("a", "w1", self.repo), ("b", "w2", self.worktree))
        with self.store.read() as connection:
            database = tuple(connection.iterdump())
        self.assertEqual(self.show()["main"], {"host": "alpha", "terminal_id": "a"})
        self.assertEqual(self.show()["settings"]["main"], before)
        self.assertEqual(self.settings.role(agents["a"]), "main")
        self.assertEqual(self.settings.role(agents["b"]), "main")
        self.assertEqual(self.show("w2")["main"], explicit)
        with self.store.read() as connection:
            self.assertEqual(tuple(connection.iterdump()), database)
        self.settings.mutate(self.repo_id, "mode", "manual")
        self.assertIsNone(self.show()["main"])
        self.assertIsNone(self.show("w2")["main"])
        self.assertTrue(all(self.settings.role(agent) is None for agent in agents.values()))

    def test_inactive_explicit_main_does_not_override_surviving_auto_main(self):
        self.register(("a", "w1", self.repo), ("b", "w1", self.worktree))
        self.mutate("main", {"host": "alpha", "terminal_id": "b"})
        self.register(("a", "w1", self.repo))
        self.assertEqual(self.show()["main"], {"host": "alpha", "terminal_id": "a"})
        self.assertEqual(self.show()["settings"]["main"]["value"], {"host": "alpha", "terminal_id": "b"})

    def test_manual_main_and_members_are_scoped_and_mode_is_repo_wide(self):
        agents = self.register(("a", "w1", self.repo), ("b", "w1", self.worktree),
                               ("c", "w2", self.worktree), ("d", "w2", self.worktree))
        self.settings.mutate(self.repo_id, "mode", "manual")
        self.mutate("main", {"host": "alpha", "terminal_id": "a"})
        self.mutate("members", [{"host": "alpha", "terminal_id": "a"}, {"host": "alpha", "terminal_id": "b"}])
        self.mutate("members", [{"host": "alpha", "terminal_id": "c"}], "w2")
        self.assertEqual({terminal: self.settings.role(agent) for terminal, agent in agents.items()},
                         {"a": "main", "b": "worker", "c": "worker", "d": None})
        self.assertIsNone(self.show("w2")["main"])
        self.assertEqual(self.show("w2")["members"], [{"host": "alpha", "terminal_id": "c"}])
        self.settings.mutate(self.repo_id, "mode", "off")
        self.assertEqual(self.show()["mode"], "off")
        self.assertEqual(self.show("w2")["mode"], "off")
        self.assertTrue(all(self.settings.role(agent) is None for agent in agents.values()))

    def test_manual_membership_does_not_follow_an_agent_to_a_new_workspace(self):
        self.register(("a", "w1", self.repo), ("b", "w1", self.worktree))
        self.settings.mutate(self.repo_id, "mode", "manual")
        self.mutate("members", [{"host": "alpha", "terminal_id": "b"}])
        agents = self.register(("a", "w1", self.repo), ("b", "w2", self.worktree))
        self.assertIsNone(self.settings.role(agents["b"]))
        self.assertEqual(self.show()["members"], [{"host": "alpha", "terminal_id": "b"}])
        self.assertEqual(self.show("w2")["members"], [])
        self.mutate("members", [{"host": "alpha", "terminal_id": "b"}], "w2")
        self.assertEqual(self.settings.role(agents["b"]), "worker")

    def test_main_and_members_require_explicit_scope_and_live_in_scope_targets(self):
        self.register(("a", "w1", self.repo), ("b", "w2", self.worktree))
        self.register(("a", "w1", self.repo))
        targets = ({"host": "alpha", "terminal_id": "a"},
                   {"host": "alpha", "terminal_id": "b"},
                   {"host": "beta", "terminal_id": "a"},
                   {"host": "alpha", "terminal_id": "unknown"})
        for field in ("main", "members"):
            value = targets[0] if field == "main" else [targets[0]]
            for scope in ({}, {"host": "alpha"}, {"workspace_id": "w1"}):
                with self.subTest(field=field, scope=scope), self.assertRaises(LedgerError):
                    self.settings.mutate(self.repo_id, field, value, **scope)
            for target in targets:
                value = target if field == "main" else [target]
                with self.subTest(field=field, target=target), self.assertRaises(LedgerError):
                    self.mutate(field, value, "w2")


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
            connection.execute("INSERT INTO ledger_meta VALUES (?,?)", ("schema_version", str(SCHEMA_VERSION)))
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

    def register_cross_repo(self):
        other = self.root / "other"
        other.mkdir()
        self.registry.reconcile(snapshot(self.repo, recipient_repo=other,
                                         recipient_session="cross-repo-session"))
        self.sender, self.recipient = self.agents()
        self.assertNotEqual(self.sender["repo_id"], self.recipient["repo_id"])
        return other

    def claim_identity(self, claim):
        return {key: claim[key] for key in ("claim_id", "agent_id", "generation")}

    def database_snapshot(self):
        with self.store.read() as connection:
            data = '\n'.join(connection.iterdump())
        bells = {p.name: (p.stat().st_mtime_ns, p.read_bytes()) for p in self.store.bell_directory.iterdir()}
        return hashlib.sha256(data.encode()).hexdigest(), bells

    def turn_end_flag(self, message_id):
        with self.store.read() as connection:
            return connection.execute("SELECT fyi_turn_end FROM deliveries WHERE message_id=?",
                                      (message_id,)).fetchone()[0]

    def test_missing_local_receiver_is_advisory_and_preserves_queue(self):
        for client in ("omp", "claude"):
            with self.subTest(client=client):
                recipient = {**self.recipient, "client": client, "extension_present": 0}
                sent = self.mail.send(self.sender, recipient, body="Receiver dependency notice")
                self.assertEqual(sent["state"], "queued")
                warning = next(item for item in sent.get("warnings", [])
                               if item["code"] == "recipient_no_receiver")
                self.assertEqual(warning["client"], client)
                self.assertIn("runtime status", warning["message"])
                self.assertIn(sent["message_id"], [item["message_id"] for item in
                              self.mail.read(self.recipient)["messages"]])

    def test_registered_and_codex_recipients_have_no_receiver_advisory(self):
        for client, present in (("omp", 1), ("claude", 1), ("codex", 0)):
            with self.subTest(client=client):
                sent = self.mail.send(self.sender, {**self.recipient, "client": client,
                                                   "extension_present": present}, body="No warning")
                self.assertNotIn("recipient_no_receiver", [item["code"] for item in sent.get("warnings", [])])

    def test_fyi_ready_receiver_delay_uses_current_local_state(self):
        self.registry.agent_session(terminal_id=self.recipient["terminal_id"],
                                    session_ref=self.recipient["session_ref"], client="omp",
                                    repo_id=self.recipient["repo_id"])
        recipient = self.agents()[1]
        # Reuse an idle mapping deliberately: current authoritative state wins,
        # including for symbolic main recipients without projected status fields.
        recipient.pop("turn_state")
        for kind, state, delayed in (("fyi", "idle", True), ("fyi", "done", True),
                                     ("action", "idle", False), ("action", "done", False),
                                     ("fyi", "working", False), ("fyi", "blocked", False),
                                     ("fyi", "unknown", False)):
            with self.subTest(kind=kind, state=state):
                with self.store.transaction() as connection:
                    connection.execute("UPDATE agent_status SET turn_state=? WHERE agent_id=?",
                                       (state, recipient["agent_id"]))
                sent = self.mail.send(self.sender, recipient, kind=kind, body="Decision enables work")
                if delayed:
                    self.assertEqual(sent["fyi_wake_after_seconds"], 300)
                else:
                    self.assertNotIn("fyi_wake_after_seconds", sent)
                self.assertEqual(sent["state"], "queued")
                self.assertEqual(self.turn_end_flag(sent["message_id"]),
                                 int(kind == "fyi" and state == "working"))
                self.assertEqual(sent.get("fyi_turn_end", False), kind == "fyi" and state == "working")
                if kind == "fyi" and state == "working":
                    self.assertIs(sent["fyi_turn_end"], True)
                self.assertIn(sent["message_id"], [item["message_id"] for item in
                              self.mail.read(recipient)["messages"]])

    def test_fyi_delay_requires_participating_registered_omp_receiver(self):
        cases = (("omp", 0, "auto"), ("claude", 1, "auto"),
                 ("codex", 1, "auto"), ("omp", 1, "off"), ("omp", 1, "manual"))
        settings = RepoSettings(self.store, display_runner=lambda command:
                                subprocess.CompletedProcess(command, 0, "{}", ""))
        for client, present, mode in cases:
            with self.subTest(client=client, present=present, mode=mode):
                settings.mutate(self.recipient["repo_id"], "mode", mode)
                with self.store.transaction() as connection:
                    connection.execute("UPDATE agents SET client=?,extension_present=? WHERE agent_id=?",
                                       (client, present, self.recipient["agent_id"]))
                sent = self.mail.send(self.sender, self.agents()[1], body="No wake-capable receiver")
                self.assertNotIn("fyi_wake_after_seconds", sent)

    def test_fyi_reply_to_action_uses_same_ready_state_rule(self):
        self.registry.agent_session(terminal_id=self.recipient["terminal_id"],
                                    session_ref=self.recipient["session_ref"], client="omp",
                                    repo_id=self.recipient["repo_id"])
        recipient = self.agents()[1]
        for state, delayed in (("idle", True), ("working", False)):
            with self.subTest(state=state):
                parent = self.mail.send(recipient, self.sender, kind="action", body="Which decision?")
                with self.store.transaction() as connection:
                    connection.execute("UPDATE agent_status SET turn_state=? WHERE agent_id=?",
                                       (state, recipient["agent_id"]))
                reply = self.mail.send(self.sender, recipient, reply_to=parent["message_id"],
                                       body="Use the approved option")
                if delayed:
                    self.assertEqual(reply["fyi_wake_after_seconds"], 300)
                else:
                    self.assertNotIn("fyi_wake_after_seconds", reply)
                self.assertEqual(self.mail.read(recipient)["messages"][-1]["kind"], "fyi")

    def test_enqueue_samples_fresh_authoritative_working_state_not_caller_or_message_age(self):
        cases = (("working", 0, "fyi", 1), ("working", 60, "fyi", 0),
                 ("idle", 0, "fyi", 0), ("done", 0, "fyi", 0),
                 ("unknown", 0, "fyi", 0), ("working", 0, "action", 0))
        for state, age, kind, expected in cases:
            with self.subTest(state=state, age=age, kind=kind):
                with self.store.transaction() as connection:
                    connection.execute("UPDATE agent_status SET turn_state=?,updated_at=? WHERE agent_id=?",
                                       (state, time.time()-86400, self.recipient["agent_id"]))
                    connection.execute("UPDATE agents SET last_seen=? WHERE agent_id=?",
                                       (time.time()-age, self.recipient["agent_id"]))
                # Status timestamps record transitions, not heartbeat freshness;
                # neither that old timestamp nor payload creation time decides.
                value = {**self.payload(), "kind": kind}
                accepted = self.mail.enqueue(value)
                self.assertEqual(self.turn_end_flag(value["message_id"]), expected)
                self.assertEqual(accepted["payload_hash"], payload_hash(value))
                self.assertEqual(accepted["state"], "queued")

    def test_enqueue_dedupe_never_upgrades_or_downgrades_original_turn_end_flag(self):
        for first_state, retry_state, expected in (("working", "idle", 1), ("idle", "working", 0)):
            with self.subTest(first_state=first_state, retry_state=retry_state):
                with self.store.transaction() as connection:
                    connection.execute("UPDATE agent_status SET turn_state=? WHERE agent_id=?",
                                       (first_state, self.recipient["agent_id"]))
                    connection.execute("UPDATE agents SET last_seen=? WHERE agent_id=?",
                                       (time.time(), self.recipient["agent_id"]))
                value = self.payload()
                first = self.mail.enqueue(value)
                with self.store.transaction() as connection:
                    connection.execute("UPDATE agent_status SET turn_state=? WHERE agent_id=?",
                                       (retry_state, self.recipient["agent_id"]))
                replayed = Mailbox(LedgerStore(self.store.directory, canonical_name="alpha")).enqueue(value)
                self.assertEqual(replayed, first)
                self.assertEqual(self.turn_end_flag(value["message_id"]), expected)
                self.assertEqual(replayed["payload_hash"], payload_hash(value))

    def test_enqueue_requires_active_matching_repo_and_present_local_recipient_for_flag(self):
        with self.store.transaction() as connection:
            connection.execute("UPDATE agent_status SET turn_state='working' WHERE agent_id=?",
                               (self.recipient["agent_id"],))
        cases = ("inactive", "wrong_repo", "terminal_closed", "missing_status")
        for case in cases:
            with self.subTest(case=case):
                with self.store.transaction() as connection:
                    connection.execute("UPDATE agents SET active=?,last_seen=? WHERE agent_id=?",
                                       (int(case != "inactive"), time.time(), self.recipient["agent_id"]))
                    connection.execute("UPDATE terminal_slots SET present=? WHERE host=? AND terminal_id=?",
                                       (int(case != "terminal_closed"), self.store.host, self.recipient["terminal_id"]))
                    if case == "missing_status":
                        connection.execute("DELETE FROM agent_status WHERE agent_id=?",
                                           (self.recipient["agent_id"],))
                value = self.payload()
                if case == "wrong_repo":
                    value.update(sender_repo_id=self.sender["repo_id"], recipient_repo_id="git:org/other")
                accepted = self.mail.enqueue(value)
                self.assertEqual(self.turn_end_flag(value["message_id"]), 0)
                self.assertEqual(accepted["hold_reason"],
                                 {"inactive": "recipient_absent", "wrong_repo": "recipient_changed",
                                  "terminal_closed": "terminal_closed", "missing_status": None}[case])

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

    def test_cross_repo_send_read_claim_ack_and_handle_use_recipient_repo(self):
        self.register_cross_repo()
        sent = self.send(kind="action", subject="Cross-repository review")
        self.assertEqual(sent["state"], "queued")
        before = self.database_snapshot()
        inbox = self.mail.read(self.recipient, message_id=sent["message_id"])["messages"]
        self.assertEqual(len(inbox), 1)
        message = inbox[0]
        self.assertEqual((message["repo_id"], message["sender_repo_id"], message["recipient_repo_id"]),
                         (self.sender["repo_id"], self.sender["repo_id"], self.recipient["repo_id"]))
        self.assertIsNone(message["hold_reason"])
        self.assertEqual(self.mail.read(self.sender, message_id=sent["message_id"])["messages"], [])
        self.assertEqual(self.database_snapshot(), before)
        with self.assertRaises(LedgerError) as caught:
            self.mail.handle(self.sender, [sent["message_id"]])
        self.assertEqual(caught.exception.code, "message_not_found")
        claim = self.claim(kinds=["action"])
        self.assertEqual([item["message_id"] for item in claim["messages"]], [sent["message_id"]])
        self.assertEqual(claim["messages"][0]["recipient_repo_id"], self.recipient["repo_id"])
        self.assertIsNone(self.claim()["claim_id"])
        delivered = self.mail.started(**self.claim_identity(claim),
                                      evidence="test:cross-repo-context-applied")["receipts"][0]
        self.assertEqual(delivered["state"], "delivered")
        handled = self.mail.handle(self.recipient, [sent["message_id"]])["receipts"][0]
        self.assertEqual(handled["state"], "handled")
        self.assertEqual(handled["delivered_agent_id"], self.recipient["agent_id"])
        self.assertEqual(self.mail.read(self.recipient)["messages"], [])
        self.assertEqual(self.mail.read(self.recipient, all_states=True)["messages"][0]["state"], "handled")

    def test_cross_repo_pull_handle_does_not_require_native_claim(self):
        self.register_cross_repo()
        sent = self.send()
        handled = self.mail.handle(self.recipient, [sent["message_id"]])["receipts"][0]
        self.assertEqual(handled["state"], "handled")
        self.assertIsNone(handled["delivered_at"])
        self.assertIsNone(handled["delivered_agent_id"])
        self.assertIsNone(self.claim()["claim_id"])

    def test_user_cross_repo_send_keeps_selected_sender_origin(self):
        self.register_cross_repo()
        mail = Mailbox(self.store, {"HERDR_AGENT_LEDGER_AUTOSTART": "0"})
        sent = mail.send_user(self.sender["repo_id"], self.recipient, body="User cross-repo request")
        self.assertEqual(sent["state"], "queued")
        thread = mail.show_thread(sent["thread_id"])
        self.assertEqual(thread["repo_id"], self.sender["repo_id"])
        message = mail.read(self.recipient)["messages"][0]
        self.assertEqual((message["sender"], message["sender_repo_id"], message["recipient_repo_id"]),
                         ("user", self.sender["repo_id"], self.recipient["repo_id"]))
        reply = mail.send(self.recipient, self.sender, body="User request reviewed",
                          reply_to=sent["message_id"])
        self.assertEqual(reply["thread_id"], sent["thread_id"])

    def test_cross_repo_recipient_switch_holds_until_intended_repo_returns(self):
        other = self.register_cross_repo()
        sent = self.send()
        intended_repo = self.recipient["repo_id"]
        # Switching into the conversation's origin is still the wrong occupant.
        self.registry.reconcile(snapshot(self.repo, recipient_session="wrong-origin-session"))
        self.sender, self.recipient = self.agents()
        self.assertEqual(self.mail.read(self.recipient, message_id=sent["message_id"])["messages"], [])
        self.assertEqual(self.claim()["messages"], [])
        with self.assertRaises(LedgerError) as caught:
            self.mail.handle(self.recipient, [sent["message_id"]])
        self.assertEqual(caught.exception.code, "message_not_found")
        with self.store.read() as connection:
            row = connection.execute("SELECT recipient_repo_id,state,hold_reason FROM deliveries "
                                     "WHERE message_id=?", (sent["message_id"],)).fetchone()
        self.assertEqual(tuple(row), (intended_repo, "queued", "recipient_changed"))
        self.registry.reconcile(snapshot(self.repo, recipient_repo=other,
                                         recipient_session="intended-repo-returned"))
        self.sender, self.recipient = self.agents()
        self.assertEqual(self.mail.read(self.recipient)["messages"][0]["message_id"], sent["message_id"])
        self.assertIsNone(self.mail.read(self.recipient)["messages"][0]["hold_reason"])
        self.assertEqual([item["message_id"] for item in self.claim()["messages"]], [sent["message_id"]])

    def test_cross_repo_enqueue_holds_changed_occupant_despite_matching_thread_repo(self):
        self.register_cross_repo()
        value = {**self.payload(), "sender_repo_id": self.sender["repo_id"],
                 "recipient_repo_id": self.recipient["repo_id"]}
        self.registry.reconcile(snapshot(self.repo, recipient_session="origin-repo-occupant"))
        self.sender, self.recipient = self.agents()
        accepted = self.mail.enqueue(value)
        self.assertEqual((accepted["state"], accepted["hold_reason"]), ("queued", "recipient_changed"))
        self.assertEqual(self.mail.read(self.recipient, message_id=value["message_id"])["messages"], [])
        self.assertEqual(self.claim()["messages"], [])

    def test_cross_repo_operator_not_applied_recovery_checks_recipient_repo(self):
        other = self.register_cross_repo()
        self.send()
        claim = self.claim()
        self.registry.reconcile(snapshot(self.repo, recipient_session="wrong-repo"))
        self.sender, self.recipient = self.agents()
        resolved = self.mail.resolve_claim(**self.claim_identity(claim), disposition="not_applied",
                                           evidence="test:no-context-was-applied")["receipts"]
        self.assertEqual(len(resolved), 1)
        self.assertEqual((resolved[0]["state"], resolved[0]["hold_reason"]),
                         ("queued", "recipient_changed"))
        with self.store.read() as connection:
            self.assertEqual(connection.execute("SELECT hold_reason FROM deliveries").fetchone()[0],
                             "recipient_changed")
        self.assertEqual(self.claim()["messages"], [])
        self.registry.reconcile(snapshot(self.repo, recipient_repo=other,
                                         recipient_session="intended-repo"))
        self.sender, self.recipient = self.agents()
        self.assertEqual(len(self.claim()["messages"]), 1)

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

    def test_cross_repo_replies_keep_origin_and_allow_any_thread_participant(self):
        other = self.register_cross_repo()
        first = self.send(subject="Immutable origin subject", kind="action")
        reply = self.mail.send(self.recipient, self.sender, body="Reviewed across repositories",
                               reply_to=first["message_id"], subject="Must not replace subject")
        followup = self.mail.send(self.sender, self.recipient, body="Followup",
                                  reply_to=reply["message_id"])
        # The original sender may reply to its own earlier message as a sender,
        # not merely to messages addressed to it.
        own_reply = self.mail.send(self.sender, self.recipient, body="Original sender clarification",
                                   reply_to=first["message_id"])
        # Recipient membership is terminal+host, so a new session may continue.
        self.registry.reconcile(snapshot(self.repo, recipient_repo=other,
                                         recipient_session="replacement-participant"))
        self.sender, self.recipient = self.agents()
        continued = self.mail.send(self.recipient, self.sender, body="Replacement session reply",
                                   reply_to=own_reply["message_id"])
        thread = self.mail.show_thread(first["thread_id"])
        self.assertEqual(thread["repo_id"], self.sender["repo_id"])
        self.assertEqual(thread["subject"], "Immutable origin subject")
        rows = {row["message_id"]: row for row in thread["messages"]}
        for result, hop, sender_repo, recipient_repo in (
                (first, 0, self.sender["repo_id"], self.recipient["repo_id"]),
                (reply, 1, self.recipient["repo_id"], self.sender["repo_id"]),
                (followup, 2, self.sender["repo_id"], self.recipient["repo_id"]),
                (own_reply, 1, self.sender["repo_id"], self.recipient["repo_id"]),
                (continued, 2, self.recipient["repo_id"], self.sender["repo_id"])):
            row = rows[result["message_id"]]
            self.assertEqual(result["thread_id"], first["thread_id"])
            self.assertEqual((row["repo_id"], row["hop"], row["sender_repo_id"], row["recipient_repo_id"]),
                             (thread["repo_id"], hop, sender_repo, recipient_repo))
        self.assertEqual(rows[first["message_id"]]["state"], "handled")
        self.assertIsNone(rows[first["message_id"]]["delivered_at"])

    def test_sender_session_restart_can_continue_thread_only_in_original_repo(self):
        other = self.register_cross_repo()
        first = self.send(subject="Sender identity survives a restart")
        current = snapshot(self.repo, recipient_repo=other)
        current["agents"][0]["agent_session"]["value"] = "sender-restarted"
        self.registry.reconcile(current, refresh_display=False)
        self.sender, self.recipient = self.agents()
        reply = self.mail.send(self.sender, self.recipient, body="Same terminal and repository",
                               reply_to=first["message_id"])
        self.assertEqual(reply["thread_id"], first["thread_id"])
        current["agents"][0]["cwd"] = str(other)
        self.registry.reconcile(current, refresh_display=False)
        self.sender, self.recipient = self.agents()
        before = self.database_snapshot()
        with self.assertRaises(LedgerError) as caught:
            self.mail.send(self.sender, self.recipient, body="Wrong project on old sender terminal",
                           reply_to=first["message_id"])
        self.assertEqual(caught.exception.code, "not_participant")
        self.assertEqual(self.database_snapshot(), before)

    def test_reused_recipient_terminal_cannot_reply_to_or_handle_stale_mail(self):
        self.register_cross_repo()
        first = self.send(subject="Intended for another repository")
        self.registry.reconcile(snapshot(self.repo, recipient_session="other-project"),
                                refresh_display=False)
        self.sender, self.recipient = self.agents()
        before = self.database_snapshot()
        with self.assertRaises(LedgerError) as caught:
            self.mail.send(self.recipient, self.sender, body="Not an intended participant",
                           reply_to=first["message_id"])
        self.assertEqual(caught.exception.code, "not_participant")
        self.assertEqual(self.database_snapshot(), before)
        row = self.mail.show_thread(first["thread_id"])["messages"][0]
        self.assertEqual((row["state"], row["hold_reason"]), ("queued", "recipient_changed"))

    def test_nonparticipant_cannot_reply_even_from_thread_origin_repo(self):
        other = self.register_cross_repo()
        first = self.send(subject="Private participant set")
        current = snapshot(self.repo, recipient_repo=other, recipient_session="cross-repo-session")
        pane = {"terminal_id": "term-c", "pane_id": "w1:p3", "workspace_id": "w1"}
        current["panes"].append(pane)
        current["agents"].append({**current["agents"][0], **pane, "name": "green-fox",
                                  "agent_session": {"value": "session-c"}})
        self.registry.reconcile(current)
        outsider = next(agent for agent in self.registry.list() if agent["terminal_id"] == "term-c")
        self.assertEqual(outsider["repo_id"], self.sender["repo_id"])
        before = self.database_snapshot()
        with self.assertRaises(LedgerError) as caught:
            self.mail.send(outsider, self.recipient, body="Uninvited reply", reply_to=first["message_id"])
        self.assertEqual(caught.exception.code, "not_participant")
        self.assertEqual(self.database_snapshot(), before)
        # Being invited in any later delivery permits replying to the root.
        invited = self.mail.send(self.sender, outsider, body="Please join this thread",
                                 reply_to=first["message_id"])
        accepted = self.mail.send(outsider, self.recipient, body="Now a thread participant",
                                  reply_to=first["message_id"])
        self.assertEqual(invited["thread_id"], first["thread_id"])
        self.assertEqual(accepted["thread_id"], first["thread_id"])

    def test_legacy_and_v8_payload_hashes_cover_exact_accepted_fields(self):
        legacy = self.payload()
        expected_legacy = hashlib.sha256(json.dumps(
            legacy, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
        self.assertEqual(validate_payload(legacy), legacy)
        self.assertEqual(payload_hash(legacy), expected_legacy)
        modern = {**legacy, "sender_repo_id": self.sender["repo_id"],
                  "recipient_repo_id": self.recipient["repo_id"]}
        self.assertEqual(validate_payload(modern), modern)
        self.assertNotEqual(payload_hash(modern), expected_legacy)
        self.assertEqual(payload_hash(dict(reversed(list(modern.items())))), payload_hash(modern))
        for key in ("sender_repo_id", "recipient_repo_id"):
            with self.subTest(key=key):
                self.assertNotEqual(payload_hash({**modern, key: "git:changed/repository"}),
                                    payload_hash(modern))
                incomplete = dict(modern)
                incomplete.pop(key)
                with self.assertRaises(LedgerError) as caught:
                    validate_payload(incomplete)
                self.assertEqual(caught.exception.code, "invalid_message")
                for invalid in (None, "", 1, "x" * 4097):
                    with self.subTest(invalid=invalid), self.assertRaises(LedgerError) as caught:
                        validate_payload({**modern, key: invalid})
                    self.assertEqual(caught.exception.code, "invalid_message")
        accepted = self.mail.enqueue(legacy)
        self.assertEqual(accepted["payload_hash"], expected_legacy)
        self.assertEqual(self.mail.enqueue(legacy), accepted)
        # Migration does not reinterpret old UUIDs as extended v8 payloads.
        with self.assertRaises(LedgerError) as caught:
            self.mail.enqueue(modern)
        self.assertEqual(caught.exception.code, "message_conflict")

    def test_v9_migration_is_conservative_for_existing_fyi_and_flags_only_new_accepts(self):
        with self.store.transaction() as connection:
            connection.execute("UPDATE agent_status SET turn_state='working' WHERE agent_id=?",
                               (self.recipient["agent_id"],))
            connection.execute("UPDATE agents SET last_seen=? WHERE agent_id=?",
                               (time.time(), self.recipient["agent_id"]))
        old_fyi, old_action, held_fyi = self.payload(), {**self.payload(), "kind": "action"}, self.payload()
        for value in (old_fyi, old_action, held_fyi):
            self.mail.enqueue(value)
        with self.store.transaction() as connection:
            connection.execute("UPDATE deliveries SET hold_reason='recipient_changed' WHERE message_id=?",
                               (held_fyi["message_id"],))
            connection.execute("ALTER TABLE deliveries DROP COLUMN fyi_turn_end")
            connection.execute("UPDATE ledger_meta SET value='9' WHERE key='schema_version'")
            before = [dict(row) for row in connection.execute("SELECT * FROM deliveries ORDER BY message_id")]
            hashes = [tuple(row) for row in connection.execute(
                "SELECT message_id,payload_hash FROM messages ORDER BY message_id")]
            receipts = [tuple(row) for row in connection.execute(
                "SELECT * FROM dedupe_receipts ORDER BY message_id")]
        migrated = LedgerStore(self.store.directory, canonical_name="alpha")
        mail = Mailbox(migrated)
        with migrated.read() as connection:
            self.assertEqual(connection.execute(
                "SELECT value FROM ledger_meta WHERE key='schema_version'").fetchone()[0], "10")
            column = next(row for row in connection.execute("PRAGMA table_info(deliveries)")
                          if row["name"] == "fyi_turn_end")
            self.assertEqual((column["type"], column["notnull"], column["dflt_value"]), ("INTEGER", 1, "0"))
            after = list(connection.execute("SELECT * FROM deliveries ORDER BY message_id"))
            self.assertEqual([{key: row[key] for key in row.keys() if key != "fyi_turn_end"} for row in after], before)
            self.assertEqual([row["fyi_turn_end"] for row in after], [0, 0, 0])
            self.assertEqual([tuple(row) for row in connection.execute(
                "SELECT message_id,payload_hash FROM messages ORDER BY message_id")], hashes)
            self.assertEqual([tuple(row) for row in connection.execute(
                "SELECT * FROM dedupe_receipts ORDER BY message_id")], receipts)
        # A retry is still the old acceptance, even with fresh working status.
        mail.enqueue(old_fyi)
        self.assertEqual(self.turn_end_flag(old_fyi["message_id"]), 0)
        with migrated.transaction() as connection:
            connection.execute("UPDATE agents SET last_seen=? WHERE agent_id=?",
                               (time.time(), self.recipient["agent_id"]))
        new_fyi = self.payload()
        mail.enqueue(new_fyi)
        self.assertEqual(self.turn_end_flag(new_fyi["message_id"]), 1)
        reopened = LedgerStore(migrated.directory, canonical_name="alpha")
        with reopened.read() as connection:
            flags = {row["message_id"]: row["fyi_turn_end"]
                     for row in connection.execute("SELECT message_id,fyi_turn_end FROM deliveries")}
        self.assertEqual(flags, {old_fyi["message_id"]: 0, old_action["message_id"]: 0,
                                 held_fyi["message_id"]: 0, new_fyi["message_id"]: 1})
        for invalid in (None, -1, 2):
            with self.subTest(invalid=invalid):
                with self.assertRaises(sqlite3.IntegrityError):
                    with reopened.transaction() as connection:
                        connection.execute("UPDATE deliveries SET fyi_turn_end=? WHERE message_id=?",
                                           (invalid, new_fyi["message_id"]))

    def test_v7_migration_backfills_endpoint_repos_preserving_queues_holds_and_hashes(self):
        held = self.payload()
        self.mail.enqueue(held)
        self.register_cross_repo()
        queued = {**self.payload(), "repo_id": self.recipient["repo_id"]}
        self.mail.enqueue(queued)
        remote_payload = {**self.payload(), "recipient_host": "beta",
                          "recipient_address": "beta/remote-owl"}
        remote = RemoteLedger(self.store)
        with self.store.transaction() as connection:
            connection.execute("INSERT INTO peers VALUES (?,?,?,?)",
                               ("beta", str(uuid.uuid4()), "inert-beta", "/inert/ham"))
        with self.store.transaction() as connection:
            pending = remote.submit(connection, remote_payload)
        self.assertEqual(pending["state"], "pending")
        payloads = {value["message_id"]: value for value in (held, queued, remote_payload)}
        expected_hashes = {identity: hashlib.sha256(json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
                           for identity, value in payloads.items()}
        with self.store.read() as connection:
            before_messages = [{key: row[key] for key in row.keys() if key != "sender_repo_id"}
                               for row in connection.execute("SELECT * FROM messages ORDER BY message_id")]
            before_deliveries = [{key: row[key] for key in row.keys() if key != "recipient_repo_id"}
                                 for row in connection.execute("SELECT * FROM deliveries ORDER BY message_id")]
            before_receipts = [tuple(row) for row in connection.execute(
                "SELECT * FROM dedupe_receipts ORDER BY message_id")]
            before_routes = [tuple(row) for row in connection.execute(
                "SELECT * FROM message_routes ORDER BY message_id")]
            cursor_key = connection.execute(
                "SELECT value FROM ledger_meta WHERE key='history_cursor_key'").fetchone()[0]
        # Reconstruct v7's actual missing-column shape rather than merely
        # changing the version tag on a fully migrated database.
        with self.store.transaction() as connection:
            connection.execute("ALTER TABLE messages DROP COLUMN sender_repo_id")
            connection.execute("ALTER TABLE deliveries DROP COLUMN recipient_repo_id")
            connection.execute("UPDATE ledger_meta SET value='7' WHERE key='schema_version'")
        migrated = LedgerStore(self.store.directory, canonical_name="alpha")
        mail = Mailbox(migrated)
        with migrated.read() as connection:
            self.assertEqual(connection.execute(
                "SELECT value FROM ledger_meta WHERE key='schema_version'").fetchone()[0], str(SCHEMA_VERSION))
            self.assertEqual(connection.execute(
                "SELECT value FROM ledger_meta WHERE key='history_cursor_key'").fetchone()[0], cursor_key)
            self.assertEqual(migrated.host_uuid, self.store.host_uuid)
            messages = list(connection.execute("SELECT * FROM messages ORDER BY message_id"))
            deliveries = list(connection.execute("SELECT * FROM deliveries ORDER BY message_id"))
            self.assertEqual([{key: row[key] for key in row.keys() if key != "sender_repo_id"}
                              for row in messages], before_messages)
            self.assertEqual([{key: row[key] for key in row.keys() if key != "recipient_repo_id"}
                              for row in deliveries], before_deliveries)
            self.assertEqual([tuple(row) for row in connection.execute(
                "SELECT * FROM dedupe_receipts ORDER BY message_id")], before_receipts)
            self.assertEqual([tuple(row) for row in connection.execute(
                "SELECT * FROM message_routes ORDER BY message_id")], before_routes)
            for message in messages:
                self.assertEqual(message["sender_repo_id"], message["repo_id"])
                self.assertEqual(message["payload_hash"], expected_hashes[message["message_id"]])
            by_id = {row["message_id"]: row for row in deliveries}
            for identity, value in payloads.items():
                self.assertEqual(by_id[identity]["recipient_repo_id"], value["repo_id"])
            self.assertEqual((by_id[held["message_id"]]["state"], by_id[held["message_id"]]["hold_reason"]),
                             ("queued", "recipient_changed"))
            self.assertEqual((by_id[queued["message_id"]]["state"], by_id[queued["message_id"]]["hold_reason"]),
                             ("queued", None))
        # Exact legacy retransmissions remain idempotent after the new columns
        # have been populated, and retain the original immutable receipt hash.
        for value in (held, queued):
            replay = mail.enqueue(value)
            self.assertEqual(replay["payload_hash"], expected_hashes[value["message_id"]])
        self.assertEqual([row["message_id"] for row in mail.read(self.recipient)["messages"]],
                         [queued["message_id"]])
        handled = mail.handle(self.recipient, [queued["message_id"]])["receipts"][0]
        self.assertEqual(handled["payload_hash"], expected_hashes[queued["message_id"]])
        self.assertEqual(handled["state"], "handled")
        incoming = {**pending, "state": "queued", "hold_reason": None,
                    "authoritative_host": "beta", "updated_at": pending["updated_at"] + 1}
        remote = RemoteLedger(migrated)
        with self.assertRaises(LedgerError) as caught:
            with migrated.transaction() as connection:
                remote.apply_receipt(connection, {**incoming, "payload_hash": "0" * 64}, "beta")
        self.assertEqual(caught.exception.code, "message_conflict")
        with migrated.transaction() as connection:
            accepted = remote.apply_receipt(connection, incoming, "beta")
        self.assertEqual(accepted["state"], "queued")
        self.assertEqual(accepted["payload_hash"], expected_hashes[remote_payload["message_id"]])
        migrated.retention(now=time.time() + 91 * 86400)
        self.assertEqual(mail.enqueue(queued), handled)
        with self.assertRaises(LedgerError) as caught:
            mail.enqueue({**queued, "sender_repo_id": queued["repo_id"],
                          "recipient_repo_id": queued["repo_id"]})
        self.assertEqual(caught.exception.code, "message_conflict")

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
