"""Behavioral read-only board coverage using private SQLite peer services."""
import tests.ham_test_support as ham_test_support  # Protect the live ledger before product imports.

import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import uuid

from ham.agents.output import compact_board_payload
from ham.ledger.mail import Mailbox
from ham.ledger.remote import RemoteLedger
from ham.ledger.runtime import NativeReceiver
from ham.ledger.settings import RepoSettings
from ham.ledger.status import AgentRegistry
from ham.ledger.store import LedgerError, LedgerStore
from ham.ledger.views import status_board


ROOT = Path(__file__).resolve().parents[1]
REPO = "git:example.test/team/board"
OTHER_REPO = "git:example.test/team/other"


class PrivatePeerTransport:
    """Run authenticated real read services on another private database, never SSH."""

    def __init__(self, owner, stores, environment):
        self.owner = owner
        self.stores = stores
        self.environment = environment
        self.calls = []
        self.failures = {}
        self.identities = {}

    def call(self, peer, command, payload):
        canonical = peer["canonical_name"]
        self.calls.append((canonical, command, dict(payload)))
        if canonical in self.failures:
            raise LedgerError("transport_unavailable", self.failures[canonical])
        target = self.stores[canonical]
        service = RemoteLedger(target, self.environment, transport=self)
        result = service.receive(command, RemoteLedger(self.owner).envelope(payload))
        if canonical in self.identities:
            result["host"] = self.identities[canonical]
        return result


class StatusBoardTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Use the checkout's managed disk storage before replacing HOME below.
        helper = Path.home()/".config/herdr/scripts/agent_work.py"
        if helper.is_file():
            cls.work = Path(subprocess.run(
                [sys.executable, str(helper), "path", "--checkout", str(ROOT),
                 "--name", "ledger-views-tests"], check=True, text=True,
                capture_output=True).stdout.strip())
        else:
            cls.work = Path(os.environ.get("AGENT_WORK_ROOT", Path.home()/".cache/agent-work"))/"ledger-views-tests"
            cls.work.mkdir(parents=True, exist_ok=True)

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=self.work)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        home = self.root/"home"
        home.mkdir()
        config = self.root/"ssh-config"
        config.write_text("Host a b c\n  HostName never-connect.invalid\n")
        allowlist = self.root/"ssh-hosts"
        allowlist.write_text("a\nb\nc\n")
        environment = {
            "HOME": str(home), "XDG_CONFIG_HOME": str(home/"config"),
            "XDG_STATE_HOME": str(home/"state"), "XDG_CACHE_HOME": str(home/"cache"),
            "HERDR_SOCKET_PATH": str(self.root/"absent-herdr.sock"),
            "HERDR_AGENT_LEDGER_DIR": str(self.root/"unused-default-ledger"),
            "HERDR_AGENT_MESSENGER_REQUEST_DIR": str(self.root/"requests"),
            "HERDR_AGENT_LEDGER_AUTOSTART": "0", "PYTHONDONTWRITEBYTECODE": "1",
            "HERDR_AGENT_MESSENGER_SSH_CONFIG": str(config),
            "HERDR_AGENT_MESSENGER_SSH_HOSTS_FILE": str(allowlist),
            "PATH": os.environ.get("PATH", ""),
        }
        patch = mock.patch.dict(os.environ, environment, clear=True)
        patch.start()
        self.addCleanup(patch.stop)
        self.environment = dict(os.environ)
        self.scanner = mock.Mock(return_value={"listeners": [], "serve": [], "error": None})
        resource_patch = mock.patch("ham.ops.resource_scan.scan_host_resources", self.scanner)
        resource_patch.start()
        self.addCleanup(resource_patch.stop)
        self.stores = {name: LedgerStore(self.root/name, canonical_name=name,
                                        environment=self.environment)
                       for name in ("alpha", "beta", "gamma")}
        self.local = self.stores["alpha"]
        self.transport = PrivatePeerTransport(self.local, self.stores, self.environment)
        aliases = {"alpha": "a", "beta": "b", "gamma": "c"}
        for name, store in self.stores.items():
            service = RemoteLedger(store, self.environment, self.transport)
            for target, peer_store in self.stores.items():
                if target != name:
                    service.add_peer(canonical_name=target, uuid=peer_store.host_uuid,
                                     ssh_alias=aliases[target], remote_ham_root=str(ROOT))
        self.agents = {}
        for name, store in self.stores.items():
            self.agents[name] = self.add_agent(store, name, REPO)
        self.other = self.add_agent(self.local, "other", OTHER_REPO, extension=False)

    def add_agent(self, store, label, repo_id, *, extension=True, workspace_id=None):
        agent_id = str(uuid.uuid4())
        terminal = "terminal-"+label
        workspace = workspace_id or "workspace-"+label
        pane = workspace + (":pane-" + label if workspace_id else ":pane-1")
        with store.transaction() as connection:
            connection.execute("INSERT OR IGNORE INTO repos VALUES (?,NULL,?)", (repo_id, 10.0))
            connection.execute(
                "INSERT INTO agents(agent_id,host,terminal_id,workspace_id,pane_id,session_ref,"
                "generation,client,repo_id,is_main_checkout,alias,extension_present,first_seen,last_seen) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (agent_id, store.host, terminal, workspace, pane, "session-"+label,
                 3, "codex", repo_id, 1, label, int(extension), 10.0, 80.0))
            connection.execute("INSERT INTO terminal_slots VALUES (?,?,?,?,1,?)",
                               (store.host, terminal, pane, workspace, 80.0))
            connection.execute("INSERT INTO agent_status VALUES (?,?,?,?,?)",
                               (agent_id, "idle", "review", "Stored summary "+label, 25.0))
        return next(row for row in AgentRegistry(store).list() if row["agent_id"] == agent_id)

    def board(self, **options):
        return status_board(self.local, environment=self.environment,
                            transport=self.transport, resource_scanner=self.scanner, **options)

    def enqueue(self, host, *, state="queued", agent=None):
        store = self.stores[host]
        agent = self.agents[host] if agent is None else agent
        result = Mailbox(store, self.environment).send(agent, agent, body="Private unread fixture")
        if state != "queued":
            with store.transaction() as connection:
                connection.execute("UPDATE deliveries SET state=? WHERE message_id=?",
                                   (state, result["message_id"]))
        return result

    @staticmethod
    def snapshot(store):
        with store.read() as connection:
            return tuple(connection.iterdump())

    def test_local_rich_rows_preserve_full_export_and_real_status_timestamp(self):
        board = self.board(host="local")
        exported = RemoteLedger(self.local).status_export(resources=True, resource_scanner=self.scanner)["agents"]
        self.assertEqual(board["agents"], [{**row, "stale": False} for row in exported])
        row = next(row for row in board["agents"] if row["repo_id"] == REPO)
        self.assertEqual({key: row[key] for key in (
            "client", "workspace_id", "pane_id", "generation", "is_main_checkout", "last_seen",
            "extension_present", "turn_state", "phase", "summary", "status_updated_at")}, {
            "client": "codex", "workspace_id": "workspace-alpha", "pane_id": "workspace-alpha:pane-1",
            "generation": 3, "is_main_checkout": 1, "last_seen": 80.0, "extension_present": 1,
            "turn_state": "idle", "phase": "review", "summary": "Stored summary alpha", "status_updated_at": 25.0,
        })
        self.assertIsNone(row["role"])
        self.assertEqual(board["hosts"], [{
            "host": "alpha", "uuid": self.local.host_uuid, "local": True, "stale": False,
            "error": None, "unread": 0, "agents": board["agents"],
            "resources": {"servers_unowned": [], "dead_serve": [], "stale_devices": [], "error": None},
        }])
        self.assertIsNone(board["repo_id"])
        self.assertEqual(self.transport.calls, [])

    def test_repository_scope_and_current_unread_exclude_delivered_messages(self):
        self.enqueue("alpha")
        self.enqueue("alpha", state="delivering")
        self.enqueue("alpha", state="delivered")
        self.enqueue("alpha", agent=self.other)
        scoped = self.board(host="local", repo_id=REPO)
        self.assertEqual(scoped["repo_id"], REPO)
        self.assertEqual([row["repo_id"] for row in scoped["agents"]], [REPO])
        self.assertEqual(scoped["agents"][0]["unread"], 2)
        self.assertEqual(scoped["hosts"][0]["unread"], 2)
        all_repos = self.board(host="local")
        self.assertEqual(all_repos["hosts"][0]["unread"], 3)
        self.assertEqual(self.board(host="local", repo_id="missing")["agents"], [])

    def test_cross_repo_unread_belongs_to_current_delivery_repo_not_thread_origin(self):
        sent = Mailbox(self.local, self.environment).send(
            self.other, self.agents["alpha"], kind="action", body="Cross-repository board fixture")
        inbox = Mailbox(self.local).read(self.agents["alpha"])["messages"]
        self.assertEqual([row["message_id"] for row in inbox], [sent["message_id"]])
        self.assertEqual(inbox[0]["repo_id"], OTHER_REPO)
        self.assertEqual(inbox[0]["recipient_repo_id"], REPO)
        scoped = self.board(host="local", repo_id=REPO)
        self.assertEqual(scoped["agents"][0]["unread"], 1)
        self.assertEqual(scoped["hosts"][0]["unread"], 1)
        self.assertEqual(self.board(host="local", repo_id=OTHER_REPO)["hosts"][0]["unread"], 0)
        compact = compact_board_payload(self.board(host="local"))
        self.assertEqual({row["address"]: row["repo"] for row in compact["agents"]},
                         {"alpha/alpha": "board", "alpha/other": "other"})

        with self.local.transaction() as connection:
            connection.execute("UPDATE agents SET repo_id=? WHERE agent_id=?",
                               (OTHER_REPO, self.agents["alpha"]["agent_id"]))
        moved = self.board(host="local", repo_id=OTHER_REPO)
        self.assertEqual({row["unread"] for row in moved["agents"]}, {0})
        self.assertEqual(moved["hosts"][0]["unread"], 0)

    def test_roles_are_authoritative_per_host_even_with_identical_workspace_ids(self):
        for host in ("alpha", "beta"):
            store = self.stores[host]
            with store.transaction() as connection:
                connection.execute("UPDATE agents SET workspace_id='shared' WHERE repo_id=?", (REPO,))
                connection.execute("UPDATE terminal_slots SET workspace_id='shared' WHERE terminal_id=?",
                                   (self.agents[host]["terminal_id"],))
            settings = RepoSettings(store, display_runner=lambda command: subprocess.CompletedProcess(
                command, 0, '{"result":{"pane":{"label":"manual fixture"}}}', ""))
            settings.mutate(REPO, "mode", "auto")
            self.assertEqual(settings.show(REPO, host, "shared")["main"],
                             {"host": host, "terminal_id": self.agents[host]["terminal_id"]})
            settings.mutate(REPO, "main", {
                "host": host, "terminal_id": self.agents[host]["terminal_id"]},
                host=host, workspace_id="shared")
        board = self.board(repo_id=REPO)
        roles = {row["host"]: row["role"] for row in board["agents"]}
        self.assertEqual(roles, {"alpha": "main", "beta": "main", "gamma": None})
        for host in ("alpha", "beta"):
            settings = RepoSettings(self.stores[host])
            self.assertEqual(settings.show(REPO, host, "shared")["main"],
                             {"host": host, "terminal_id": self.agents[host]["terminal_id"]})

    def test_runtime_roles_match_board_for_workspace_auto_manual_and_off(self):
        worker = self.add_agent(self.local, "worker", REPO, workspace_id="workspace-alpha")
        worktree = self.add_agent(self.local, "worktree", REPO, workspace_id="workspace-worktree")
        with self.local.transaction() as connection:
            connection.execute("UPDATE agents SET is_main_checkout=0 WHERE agent_id IN (?,?)",
                               (worker["agent_id"], worktree["agent_id"]))
        settings = RepoSettings(self.local, display_runner=lambda command: subprocess.CompletedProcess(
            command, 0, '{"result":{"pane":{"label":"manual fixture"}}}', ""))
        settings.mutate(REPO, "members", [{"host": "alpha", "terminal_id": worker["terminal_id"]}],
                        host="alpha", workspace_id="workspace-alpha")
        settings.mutate(REPO, "main", {"host": "alpha", "terminal_id": self.agents["alpha"]["terminal_id"]},
                        host="alpha", workspace_id="workspace-alpha")
        expected_roles = {
            "auto": {"alpha": "main", "worker": "worker", "worktree": "main"},
            "manual": {"alpha": "main", "worker": "worker", "worktree": None},
            "off": {"alpha": None, "worker": None, "worktree": None},
        }
        for mode, expected in expected_roles.items():
            with self.subTest(mode=mode):
                settings.mutate(REPO, "mode", mode)
                before = self.snapshot(self.local)
                rows = self.board(host="local", repo_id=REPO)["agents"]
                self.assertEqual({row["alias"]: row["role"] for row in rows}, expected)
                for row in rows:
                    inspected = NativeReceiver(self.local).inspect(
                        terminal_id=row["terminal_id"], agent_id=row["agent_id"], generation=row["generation"])
                    self.assertEqual(inspected["role"], row["role"])
                self.assertEqual(self.snapshot(self.local), before)

    def test_default_reads_all_registered_hosts_and_verified_peer_data(self):
        self.enqueue("beta")
        board = self.board(repo_id=REPO)
        self.assertEqual([item["host"] for item in board["hosts"]], ["alpha", "beta", "gamma"])
        self.assertEqual([item["local"] for item in board["hosts"]], [True, False, False])
        self.assertEqual({row["host"] for row in board["agents"]}, set(self.stores))
        beta = board["hosts"][1]
        self.assertEqual(beta["uuid"], self.stores["beta"].host_uuid)
        self.assertEqual(beta["unread"], 1)
        self.assertFalse(beta["stale"])
        self.assertIsNone(beta["error"])
        self.assertEqual(beta["agents"][0]["summary"], "Stored summary beta")
        self.assertEqual(self.transport.calls, [
            ("beta", "status-export", {"repo_id": REPO, "refresh": False, "resources": True}),
            ("gamma", "status-export", {"repo_id": REPO, "refresh": False, "resources": True}),
        ])

    def test_host_selection_accepts_local_canonical_and_registered_alias(self):
        for selector, canonical in (("local", "alpha"), ("alpha", "alpha"),
                                    ("beta", "beta"), ("b", "beta"), ("c", "gamma")):
            with self.subTest(selector=selector):
                board = self.board(host=selector)
                self.assertEqual([item["host"] for item in board["hosts"]], [canonical])
                self.assertEqual({row["host"] for row in board["agents"]}, {canonical})

    def test_unknown_and_ambiguous_hosts_fail_closed_before_transport(self):
        for selector in ("not-registered", "", "b@example.test"):
            with self.subTest(selector=selector), self.assertRaises(LedgerError) as caught:
                self.board(host=selector)
            self.assertEqual(caught.exception.code, "peer_not_configured")
        with self.local.transaction() as connection:
            connection.execute("UPDATE peers SET ssh_alias='b' WHERE canonical_name='gamma'")
        with self.assertRaises(LedgerError) as caught:
            self.board(host="b")
        self.assertEqual(caught.exception.code, "peer_not_configured")
        self.assertEqual(self.transport.calls, [])

    def test_failed_peer_without_snapshot_has_unknown_unread_and_no_invented_agents(self):
        self.transport.failures["beta"] = "Private peer unavailable"
        board = self.board(repo_id=REPO)
        beta = board["hosts"][1]
        self.assertEqual(beta, {"host": "beta", "uuid": self.stores["beta"].host_uuid,
                               "local": False, "stale": True,
                               "error": {"code": "transport_unavailable", "message": "Private peer unavailable"},
                               "unread": "?", "agents": []})
        self.assertEqual({row["host"] for row in board["agents"]}, {"alpha", "gamma"})
        self.assertFalse(board["hosts"][2]["stale"])

    def test_previous_verified_rows_survive_failure_only_as_stale_unknown(self):
        self.enqueue("beta")
        self.add_agent(self.stores["beta"], "beta-other", OTHER_REPO)
        previous = self.board()
        untouched = copy.deepcopy(previous)
        self.transport.failures["beta"] = "Offline"
        board = self.board(previous=previous, repo_id=REPO)
        beta = board["hosts"][1]
        self.assertEqual(beta["agents"], [
            {**row, "stale": True, "unread": "?"}
            for row in previous["agents"] if row["host"] == "beta" and row["repo_id"] == REPO])
        self.assertEqual(beta["unread"], "?")
        self.assertEqual(previous, untouched)
        self.assertTrue(all(row["repo_id"] == REPO for row in board["agents"]))
        # A subsequent stateless read must not pick up the earlier in-memory rows.
        self.assertEqual(self.board(host="beta")["agents"], [])
        del self.transport.failures["beta"]
        recovered = self.board(host="beta", repo_id=REPO, previous=board)
        self.assertEqual(recovered["hosts"][0]["unread"], 1)
        self.assertFalse(recovered["hosts"][0]["stale"])
        self.assertIsNone(recovered["hosts"][0]["error"])
        self.assertFalse(recovered["agents"][0]["stale"])

    def test_previous_rows_with_different_uuid_are_not_reused(self):
        previous = self.board()
        previous["hosts"][1]["uuid"] = str(uuid.uuid4())
        self.transport.failures["beta"] = "Offline"
        board = self.board(host="b", previous=previous)
        self.assertEqual(board["agents"], [])
        self.assertEqual(board["hosts"][0]["unread"], "?")

    def test_peer_identity_change_is_a_host_failure_not_trusted_live_rows(self):
        self.transport.identities["beta"] = {"canonical_name": "beta", "uuid": str(uuid.uuid4())}
        board = self.board(host="b")
        self.assertEqual(board["hosts"][0]["error"]["code"], "peer_identity_conflict")
        self.assertEqual(board["hosts"][0]["unread"], "?")
        self.assertEqual(board["agents"], [])

    def test_board_reads_never_refresh_registry_mutate_databases_or_replicate_agents(self):
        self.enqueue("alpha")
        self.enqueue("beta")
        before = {host: self.snapshot(store) for host, store in self.stores.items()}
        board = self.board()
        self.assertEqual(len(board["agents"]), 4)
        self.assertEqual({host: self.snapshot(store) for host, store in self.stores.items()}, before)
        self.assertEqual({row["host"] for row in AgentRegistry(self.local).list()}, {"alpha"})
        self.assertFalse((self.root/"unused-default-ledger").exists())
        self.assertFalse((self.root/"absent-herdr.sock").exists())
        # Missing live snapshot socket would make a refresh erase/fail these stored rows.
        self.assertEqual(len(AgentRegistry(self.local).list()), 2)



if __name__ == "__main__":
    unittest.main()
