"""Public read commands against private SQLite and a model-free peer endpoint."""
import ham_test_support  # Protect the live ledger before product imports.

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sqlite3
import sys
import tempfile
import unittest
import uuid
from unittest import mock

import ledger_cli
from ledger_mail import Mailbox
from ledger_remote import RemoteLedger
from ledger_status import AgentRegistry
from ledger_store import LedgerError, LedgerStore
from test_ledger_core import snapshot


ROOT = Path(__file__).resolve().parent
LAUNCHER = ROOT / ".agents/skills/herdr-agent-messenger/scripts/herdr_agent_messenger.py"


class ReadCliTest(unittest.TestCase):
    def setUp(self):
        helper = Path.home() / ".config/herdr/scripts/agent_work.py"
        if helper.is_file():
            work = Path(subprocess.run(
                [sys.executable, str(helper), "path", "--checkout", str(ROOT), "--name", "cli-read-tests"],
                check=True, capture_output=True, text=True).stdout.strip())
        else:
            work = Path(os.environ.get("AGENT_WORK_ROOT", Path.home() / ".cache/agent-work")) / "cli-read-tests"
            work.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=work)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        for name in ("home", "config", "state", "data", "cache", "runtime", "tmp", "bin", "repo", "other"):
            (self.root / name).mkdir()
        self.environment = {
            "PATH": str(self.root / "bin") + os.pathsep + os.environ.get("PATH", os.defpath),
            "HOME": str(self.root / "home"), "XDG_CONFIG_HOME": str(self.root / "config"),
            "XDG_STATE_HOME": str(self.root / "state"), "XDG_DATA_HOME": str(self.root / "data"),
            "XDG_CACHE_HOME": str(self.root / "cache"), "XDG_RUNTIME_DIR": str(self.root / "runtime"),
            "TMPDIR": str(self.root / "tmp"), "HERDR_SOCKET_PATH": str(self.root / "absent-herdr.sock"),
            "HERDR_AGENT_LEDGER_DIR": str(self.root / "ledger"),
            "HERDR_AGENT_REQUEST_STATE_DIR": str(self.root / "requests"),
            "HERDR_AGENT_LEDGER_AUTOSTART": "0", "PYTHONDONTWRITEBYTECODE": "1",
            "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": str(self.root / "gitconfig"),
            "HERDR_PANE_ID": "w1:p1", "HERDR_TERMINAL_ID": "term-a",
        }
        (self.root / "gitconfig").write_text("")
        config = self.root / "ssh-config"
        config.write_text("Host peer-fixture origin-fixture fixture\n  HostName never-connect.invalid\n")
        allowlist = self.root / "ssh-hosts"
        allowlist.write_text("peer-fixture\norigin-fixture\nfixture\n")
        self.environment["HERDR_AGENT_MESSENGER_SSH_CONFIG"] = str(config)
        self.environment["HERDR_AGENT_MESSENGER_SSH_HOSTS_FILE"] = str(allowlist)
        self.repo = self.root / "repo"
        subprocess.run(["git", "init", "-q", str(self.repo)], env=self.environment, check=True,
                       capture_output=True, timeout=10)
        subprocess.run(["git", "-C", str(self.repo), "remote", "add", "origin", "https://fixture.invalid/team/read.git"],
                       env=self.environment, check=True, capture_output=True, timeout=10)
        self.snapshot = snapshot(self.repo)
        other_pane = {"terminal_id": "term-other", "pane_id": "w1:p3", "workspace_id": "w1"}
        self.snapshot["panes"].append(other_pane)
        self.snapshot["agents"].append({**other_pane, "agent": "codex", "agent_status": "idle",
                                        "name": "other-agent", "agent_session": {"value": "session-other"},
                                        "cwd": str(self.root / "other")})
        self.snapshot_path = self.root / "snapshot.json"
        self.snapshot_path.write_text(json.dumps({"result": self.snapshot}))
        self.herdr_calls = self.root / "herdr-calls.jsonl"
        herdr = self.root / "bin/herdr"
        herdr.write_text("#!" + sys.executable + "\nimport json,sys\n"
                         + "with open(" + repr(str(self.herdr_calls)) + ",'a') as stream: stream.write(json.dumps(sys.argv[1:])+'\\n')\n"
                         + "if sys.argv[1:] != ['api','snapshot']: sys.exit(71)\n"
                         + "print(open(" + repr(str(self.snapshot_path)) + ").read())\n")
        herdr.chmod(0o700)
        self.environment["HERDR_BIN_PATH"] = str(herdr)
        self.store = LedgerStore(environment=self.environment, canonical_name="alpha")
        registry = AgentRegistry(self.store)
        registry.reconcile(self.snapshot, refresh_display=False)
        rows = registry.list()
        self.sender = next(row for row in rows if row["terminal_id"] == "term-a")
        self.recipient = next(row for row in rows if row["terminal_id"] == "term-b")
        self.other = next(row for row in rows if row["terminal_id"] == "term-other")
        self.repo_id = self.sender["repo_id"]
        self.mail = Mailbox(self.store, self.environment)
        with self.store.transaction() as connection:
            connection.execute("UPDATE agent_status SET phase='review',summary='Read-only summary',updated_at=125.0 WHERE agent_id=?",
                               (self.sender["agent_id"],))

    def cli(self, *arguments, stdin=None, environment=None):
        completed = subprocess.run([sys.executable, str(LAUNCHER), *arguments], cwd=ROOT,
                                   env=self.environment if environment is None else environment,
                                   input=None if stdin is None else json.dumps(stdin),
                                   capture_output=True, text=True, timeout=30)
        self.assertEqual(completed.stderr, "", completed.stderr)
        return completed, json.loads(completed.stdout)

    @staticmethod
    def database_state(store):
        with store.read() as connection:
            return hashlib.sha256("\n".join(connection.iterdump()).encode()).hexdigest()

    def add_peer_endpoint(self):
        peer_environment = {**self.environment, "HERDR_AGENT_LEDGER_DIR": str(self.root / "peer-ledger"),
                            "HERDR_SOCKET_PATH": str(self.root / "absent-peer-herdr.sock")}
        self.peer_store = LedgerStore(environment=peer_environment, canonical_name="beta")
        peer_snapshot = snapshot(self.repo)
        AgentRegistry(self.peer_store).reconcile(peer_snapshot, refresh_display=False)
        with self.peer_store.transaction() as connection:
            connection.execute("UPDATE agent_status SET phase='remote-review',summary='Peer stored summary',updated_at=225.0")
        local_remote = RemoteLedger(self.store, self.environment)
        local_remote.add_peer(canonical_name="beta", uuid=self.peer_store.host_uuid, ssh_alias="peer-fixture",
                              remote_ham_root=str(ROOT), remote_ledger_dir=str(self.peer_store.directory),
                              remote_socket=peer_environment["HERDR_SOCKET_PATH"])
        RemoteLedger(self.peer_store, peer_environment).add_peer(
            canonical_name="alpha", uuid=self.store.host_uuid, ssh_alias="origin-fixture", remote_ham_root=str(ROOT))
        failure = self.root / "peer-down"
        ssh = self.root / "bin/ssh"
        ssh.write_text("#!" + sys.executable + "\nimport json,os,sys\n"
                       + "if os.path.exists(" + repr(str(failure)) + "):\n"
                       + " print(json.dumps({'error':{'code':'transport_unavailable','message':'Private peer fixture is offline'}}));sys.exit(255)\n"
                       + "os.execve(" + repr(sys.executable) + "," + repr([sys.executable, str(ROOT / "ledger_cli.py"), "ledger", "status-export", "--wire"])
                       + "," + repr(peer_environment) + ")\n")
        ssh.chmod(0o700)
        return failure

    def test_status_views_without_ledger_keep_empty_shapes_without_bootstrap(self):
        missing = self.root / "absent-ledger-home"
        environment = {**self.environment, "HOME": str(missing),
                       "HERDR_AGENT_LEDGER_DIR": str(missing / "ledger")}
        for operation in ("list", "board"):
            for repo_id in (None, self.repo_id):
                with self.subTest(operation=operation, repo_id=repo_id):
                    arguments = ["status", operation]
                    if repo_id is not None:
                        arguments += ["--repo", repo_id]
                    completed, result = self.cli(*arguments, environment=environment)
                    self.assertEqual(completed.returncode, 0)
                    self.assertEqual(result, {"repo_id": repo_id, "agents": [],
                                             **({"hosts": []} if operation == "board" else {})})
        self.assertFalse(missing.exists())
        self.assertFalse(self.herdr_calls.exists())

    def test_status_views_during_first_bootstrap_return_empty_without_mutation(self):
        directory = self.root / "bootstrapping-ledger"
        directory.mkdir()
        path = directory / "ledger.sqlite3"
        environment = {**self.environment, "HERDR_AGENT_LEDGER_DIR": str(directory)}
        with sqlite3.connect(path) as connection:
            pass
        for metadata_table in (False, True):
            if metadata_table:
                with sqlite3.connect(path) as connection:
                    connection.execute("CREATE TABLE ledger_meta(key TEXT PRIMARY KEY,value TEXT)")
            before = path.read_bytes()
            for operation in ("list", "board"):
                with self.subTest(metadata_table=metadata_table, operation=operation):
                    completed, result = self.cli("status", operation, environment=environment)
                    self.assertEqual(completed.returncode, 0)
                    self.assertEqual(result, {"repo_id": None, "agents": [],
                                             **({"hosts": []} if operation == "board" else {})})
                    self.assertEqual(path.read_bytes(), before)
                    self.assertEqual({item.name for item in directory.iterdir()}, {"ledger.sqlite3"})
        self.assertFalse(self.herdr_calls.exists())

    def test_status_views_do_not_hide_other_ledger_errors(self):
        for operation in ("list", "board"):
            with self.subTest(operation=operation), mock.patch.object(
                    LedgerStore, "open_readonly", side_effect=LedgerError("invalid_metadata", "broken")):
                with self.assertRaises(LedgerError) as raised:
                    ledger_cli.status_operation(operation, environment=self.environment)
                self.assertEqual(raised.exception.code, "invalid_metadata")

    def test_wire_readonly_export_during_bootstrap_returns_structured_ledger_error(self):
        directory = self.root / "bootstrapping-export"
        directory.mkdir()
        path = directory / "ledger.sqlite3"
        path.touch()
        environment = {**self.environment, "HERDR_AGENT_LEDGER_DIR": str(directory)}
        envelope = {"protocol": 1, "transport_host": "peer", "transport_uuid": str(uuid.uuid4()),
                    "payload": {"refresh": False, "resources": True}}
        completed, result = self.cli("ledger", "status-export", "--wire",
                                     stdin=envelope, environment=environment)
        self.assertEqual((completed.returncode, result["error"]["code"]), (1, "ledger_not_initialized"))
        self.assertEqual(path.read_bytes(), b"")
        self.assertEqual({item.name for item in directory.iterdir()}, {"ledger.sqlite3"})
        self.assertFalse(self.herdr_calls.exists())

    def test_wire_export_resources_require_literal_true_without_registry_refresh(self):
        peer_id = str(uuid.uuid4())
        RemoteLedger(self.store, self.environment).add_peer(
            canonical_name="peer", uuid=peer_id, ssh_alias="fixture", remote_ham_root=str(ROOT))
        scanned = {"listeners": [], "serve": [], "error": None}
        before = self.database_state(self.store)
        for requested in (None, False, 1, "true", True):
            with self.subTest(resources=requested):
                envelope = {"protocol": 1, "transport_host": "peer", "transport_uuid": peer_id,
                            "payload": {"repo_id": self.repo_id, "refresh": False}}
                if requested is not None:
                    envelope["payload"]["resources"] = requested
                arguments = argparse.Namespace(command="ledger", ledger_command="status-export", wire=True)
                with mock.patch("ledger_cli._stdin_object", return_value=envelope), \
                     mock.patch("resource_scan.scan_host_resources", return_value=scanned) as scanner, \
                     mock.patch.object(AgentRegistry, "refresh", side_effect=AssertionError("mutation")):
                    result = ledger_cli.dispatch(arguments, self.environment)
                self.assertEqual(scanner.call_count, 1 if requested is True else 0)
                self.assertEqual("resources" in result, requested is True)
        self.assertEqual(self.database_state(self.store), before)
        self.assertFalse(self.herdr_calls.exists())

    def test_threads_enumerate_sent_received_and_pending_without_touching_delivery(self):
        sent = self.mail.send(self.sender, self.recipient, body="Sent locally")
        received = self.mail.send(self.recipient, self.sender, body="Received locally")
        self.add_peer_endpoint()
        remote = {**self.recipient, "host": "beta"}
        pending = self.mail.send(self.sender, remote, body="Pending remote copy")
        self.mail.send(self.other, self.other, body="Different repo")
        before = self.database_state(self.store)
        ids, cursor = [], None
        while True:
            arguments = ["mail", "threads", "--repo", self.repo_id, "--limit", "1"]
            if cursor:
                arguments += ["--cursor", cursor]
            completed, result = self.cli(*arguments)
            self.assertEqual(completed.returncode, 0)
            ids.extend(row["thread_id"] for row in result["threads"])
            cursor = result["next_cursor"]
            if cursor is None:
                break
        self.assertEqual(set(ids), {sent["thread_id"], received["thread_id"], pending["thread_id"]})
        self.assertEqual(len(ids), 3)
        completed, current = self.cli("mail", "threads")
        self.assertEqual(completed.returncode, 0)
        self.assertEqual(current["repo_id"], self.repo_id)
        self.assertEqual(self.database_state(self.store), before)

    def test_direct_history_pages_and_rejects_cursor_tampering(self):
        identities = [self.mail.begin_direct(self.repo_id, self.recipient, body="Direct " + str(index))["submission_id"]
                      for index in range(3)]
        self.mail.begin_direct(self.other["repo_id"], self.other, body="Other repository direct")
        before = self.database_state(self.store)
        completed, first = self.cli("mail", "direct-list", "--repo", self.repo_id, "--limit", "2")
        self.assertEqual(completed.returncode, 0)
        completed, second = self.cli("mail", "direct-list", "--repo", self.repo_id, "--limit", "2", "--cursor", first["next_cursor"])
        self.assertEqual(completed.returncode, 0)
        self.assertIsNone(second["next_cursor"])
        self.assertEqual({row["submission_id"] for row in first["submissions"] + second["submissions"]}, set(identities))
        token = first["next_cursor"]
        tampered = ("A" if token[0] != "A" else "B") + token[1:]
        for arguments in (("mail", "direct-list", "--repo", self.repo_id, "--cursor", tampered),
                          ("mail", "threads", "--repo", self.repo_id, "--cursor", token)):
            completed, error = self.cli(*arguments)
            self.assertEqual((completed.returncode, error["error"]["code"]), (1, "invalid_cursor"))
        self.assertEqual(self.database_state(self.store), before)

    def test_board_queries_real_private_peer_and_reports_failure_without_inventing_agents(self):
        failure = self.add_peer_endpoint()
        before = [self.database_state(store) for store in (self.store, self.peer_store)]
        completed, board = self.cli("status", "board", "--repo", self.repo_id)
        self.assertEqual(completed.returncode, 0)
        self.assertEqual({row["host"] for row in board["hosts"]}, {"alpha", "beta"})
        self.assertEqual({row["phase"] for row in board["agents"] if row["host"] == "beta"}, {"remote-review"})
        local = next(row for row in board["agents"] if row["agent_id"] == self.sender["agent_id"])
        self.assertEqual((local["summary"], local["status_updated_at"]), ("Read-only summary", 125.0))
        self.assertTrue({"role", "turn_state", "phase", "summary", "unread", "extension_present", "client",
                         "workspace_id", "pane_id", "generation", "is_main_checkout", "status_updated_at", "last_seen"} <= local.keys())
        failure.write_text("offline")
        completed, failed = self.cli("status", "board", "--repo", self.repo_id)
        self.assertEqual(completed.returncode, 0)
        peer = next(row for row in failed["hosts"] if row["host"] == "beta")
        self.assertEqual((peer["stale"], peer["unread"], peer["error"]["code"], peer["agents"]),
                         (True, "?", "transport_unavailable", []))
        self.assertTrue(all(row["host"] == "alpha" for row in failed["agents"]))
        self.assertEqual([self.database_state(store) for store in (self.store, self.peer_store)], before)
        self.assertFalse(self.herdr_calls.exists(), "Stored-board reads must not inspect or mutate Herdr")

    def test_wire_read_only_export_validates_mode_and_keeps_status_unchanged(self):
        peer_id = str(uuid.uuid4())
        RemoteLedger(self.store, self.environment).add_peer(canonical_name="peer", uuid=peer_id,
                                                          ssh_alias="fixture", remote_ham_root=str(ROOT))
        envelope = {"protocol": 1, "transport_host": "peer", "transport_uuid": peer_id,
                    "payload": {"repo_id": self.repo_id, "refresh": False}}
        before = self.database_state(self.store)
        completed, result = self.cli("ledger", "status-export", "--wire", stdin=envelope)
        self.assertEqual(completed.returncode, 0)
        self.assertEqual(result["host"]["canonical_name"], "alpha")
        self.assertEqual(len(result["agents"]), 2)
        envelope["payload"]["refresh"] = 0
        completed, error = self.cli("ledger", "status-export", "--wire", stdin=envelope)
        self.assertEqual((completed.returncode, error["error"]["code"]), (1, "invalid_request"))
        self.assertEqual(self.database_state(self.store), before)
        self.assertFalse(self.herdr_calls.exists())


if __name__ == "__main__":
    unittest.main()
