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
from ledger_store import SCHEMA_VERSION, LedgerError, LedgerStore
from runtime_installer import MANIFEST, RUNTIME, manage
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

    def test_runtime_status_without_client_reports_healthy_readonly_summary(self):
        home = Path(self.environment["HOME"])
        destinations = {"omp": home / ".omp/agent/extensions", "claude": home / ".claude"}
        for client, destination in destinations.items():
            manage("install", destination, client=client)
        with self.store.transaction() as connection:
            connection.execute("UPDATE agents SET extension_present=1")
        before = self.database_state(self.store)
        files = {path: path.read_bytes() for path in home.rglob("*") if path.is_file()}
        completed, result = self.cli("runtime", "status")
        self.assertEqual(completed.returncode, 0)
        self.assertEqual(set(result), {"clients", "sessions", "ledger", "next_steps"})
        self.assertEqual(result["clients"], [
            {**manage("status", destination, client=client), "error": None}
            for client, destination in destinations.items()
        ])
        self.assertEqual(result["sessions"], {
            "omp": {"receiver_registered": 1, "receiver_absent": 0},
            "claude": {"receiver_registered": 0, "receiver_absent": 0},
            "codex": {"receiver_registered": 2, "receiver_absent": 0},
        })
        self.assertEqual(result["ledger"], {"available": True, "error": None})
        self.assertEqual(result["next_steps"], [])
        self.assertEqual(self.database_state(self.store), before)
        self.assertEqual({path: path.read_bytes() for path in home.rglob("*") if path.is_file()}, files)
        self.assertFalse(self.herdr_calls.exists())

    def test_runtime_status_without_ledger_reports_unknown_counts_without_creating_home(self):
        home = self.root / "absent-runtime-home"
        environment = {**self.environment, "HOME": str(home),
                       "HERDR_AGENT_LEDGER_DIR": str(home / "ledger")}
        completed, result = self.cli("runtime", "status", environment=environment)
        self.assertEqual(completed.returncode, 0)
        self.assertEqual(set(result), {"clients", "sessions", "ledger", "next_steps"})
        self.assertEqual(result["clients"], [
            {"client": client, "destination": str(destination), "installed": False,
             "owner": None, "error": None}
            for client, destination in (("omp", home / ".omp/agent/extensions"),
                                        ("claude", home / ".claude"))
        ])
        self.assertEqual(result["sessions"], {
            client: {"receiver_registered": None, "receiver_absent": None}
            for client in ("omp", "claude", "codex")
        })
        self.assertIs(result["ledger"]["available"], False)
        self.assertEqual(result["ledger"]["error"]["code"], "ledger_not_initialized")
        self.assertTrue(result["ledger"]["error"]["message"])
        self.assertTrue(any("ledger reconcile" in step for step in result["next_steps"]))
        self.assertFalse(any("runtime install" in step for step in result["next_steps"]))
        self.assertFalse(home.exists())
        self.assertFalse(self.herdr_calls.exists())

    def test_runtime_status_during_bootstrap_reports_unavailable_without_schema_writes(self):
        directory = self.root / "runtime-bootstrapping-ledger"
        directory.mkdir()
        path = directory / "ledger.sqlite3"
        path.touch()
        environment = {**self.environment, "HERDR_AGENT_LEDGER_DIR": str(directory)}
        completed, result = self.cli("runtime", "status", environment=environment)
        self.assertEqual(completed.returncode, 0)
        self.assertEqual(result["ledger"]["error"]["code"], "ledger_not_initialized")
        self.assertIs(result["ledger"]["available"], False)
        self.assertEqual(result["sessions"], {
            client: {"receiver_registered": None, "receiver_absent": None}
            for client in ("omp", "claude", "codex")
        })
        self.assertEqual(path.read_bytes(), b"")
        self.assertEqual({item.name for item in directory.iterdir()}, {"ledger.sqlite3"})
        self.assertFalse(self.herdr_calls.exists())

    def test_runtime_status_reports_schema_upgrade_without_migrating_or_inventing_zero_counts(self):
        with self.store.transaction() as connection:
            connection.execute("UPDATE ledger_meta SET value=? WHERE key='schema_version'",
                               (str(SCHEMA_VERSION - 1),))
        before = self.database_state(self.store)
        completed, result = self.cli("runtime", "status")
        self.assertEqual(completed.returncode, 0)
        self.assertIs(result["ledger"]["available"], False)
        self.assertEqual(result["ledger"]["error"]["code"], "schema_upgrade_required")
        self.assertEqual(result["sessions"], {
            client: {"receiver_registered": None, "receiver_absent": None}
            for client in ("omp", "claude", "codex")
        })
        self.assertTrue(any("ledger reconcile" in step for step in result["next_steps"]))
        self.assertEqual(self.database_state(self.store), before)
        self.assertFalse(self.herdr_calls.exists())

    def test_runtime_status_counts_only_active_local_clients_across_repositories_and_gives_remedies(self):
        current = json.loads(json.dumps(self.snapshot))
        states = []
        for scope in ("local", "remote", "inactive"):
            for client in ("omp", "claude", "codex"):
                for registered in (0, 1):
                    terminal = f"{scope}-{client}-{registered}"
                    pane = {"terminal_id": terminal, "pane_id": terminal, "workspace_id": "w1"}
                    current["panes"].append(pane)
                    current["agents"].append({
                        **pane, "agent": client, "agent_status": "idle", "name": terminal,
                        "agent_session": {"value": terminal},
                        "cwd": str(self.root / "other" if client == "codex" else self.repo),
                    })
                    states.append((registered, "beta" if scope == "remote" else self.store.host,
                                   int(scope != "inactive"), terminal))
        AgentRegistry(self.store).reconcile(current, refresh_display=False)
        with self.store.transaction() as connection:
            connection.execute("UPDATE agents SET active=0")
            connection.executemany(
                "UPDATE agents SET extension_present=?,host=?,active=? WHERE terminal_id=?", states)
        manage("install", Path(self.environment["HOME"]) / ".omp/agent/extensions")
        before = self.database_state(self.store)
        completed, result = self.cli("runtime", "status")
        self.assertEqual(completed.returncode, 0)
        self.assertEqual(result["sessions"], {
            client: {"receiver_registered": 1, "receiver_absent": 1}
            for client in ("omp", "claude", "codex")
        })
        self.assertEqual(result["ledger"], {"available": True, "error": None})
        omp = next(step for step in result["next_steps"] if step.startswith("omp:"))
        self.assertIn("1 sessions without receiver", omp)
        self.assertIn("activation pending", omp)
        self.assertIn("restart", omp)
        self.assertNotIn("`runtime install", omp)
        claude = next(step for step in result["next_steps"] if step.startswith("claude:"))
        self.assertIn("1 sessions without receiver", claude)
        self.assertIn("user", claude)
        self.assertIn("runtime install claude", claude)
        self.assertIn("restart", claude)
        self.assertFalse(any(step.startswith("codex:") for step in result["next_steps"]))
        self.assertEqual(self.database_state(self.store), before)
        self.assertFalse(self.herdr_calls.exists())

    def test_runtime_status_next_steps_skip_clients_without_active_local_sessions(self):
        for used in ("omp", "claude", "codex"):
            with self.subTest(used=used):
                with self.store.transaction() as connection:
                    connection.execute("UPDATE agents SET client=?,extension_present=0", (used,))
                completed, result = self.cli("runtime", "status")
                self.assertEqual(completed.returncode, 0)
                if used == "codex":
                    self.assertEqual(result["next_steps"], [])
                else:
                    self.assertEqual(len(result["next_steps"]), 1)
                    self.assertIn(f"runtime install {used}", result["next_steps"][0])
                self.assertFalse(self.herdr_calls.exists())

    def test_runtime_status_reports_conflict_without_recommending_replacement_or_hiding_other_client(self):
        home = Path(self.environment["HOME"])
        omp = home / ".omp/agent/extensions"
        claude = home / ".claude"
        omp.mkdir(parents=True)
        (omp / RUNTIME).write_text("Foreign receiver; preserve.")
        manage("install", claude, client="claude")
        with self.store.transaction() as connection:
            connection.execute("UPDATE agents SET client='omp',extension_present=0")
        before = self.database_state(self.store)
        completed, result = self.cli("runtime", "status")
        self.assertEqual(completed.returncode, 0)
        rows = {row["client"]: row for row in result["clients"]}
        self.assertEqual(rows["omp"]["error"]["code"], "foreign_runtime")
        self.assertIs(rows["omp"]["installed"], False)
        self.assertEqual(rows["claude"], {**manage("status", claude, client="claude"), "error": None})
        step = next(step for step in result["next_steps"] if step.startswith("omp:"))
        self.assertIn("foreign_runtime", step)
        self.assertIn("conflict", step.lower())
        self.assertNotIn("runtime install omp", step)
        self.assertEqual((omp / RUNTIME).read_text(), "Foreign receiver; preserve.")
        self.assertFalse((omp / MANIFEST).exists())
        self.assertEqual(self.database_state(self.store), before)
        self.assertFalse(self.herdr_calls.exists())

    def test_runtime_status_with_client_preserves_existing_status_shape_and_environment_destinations(self):
        home = Path(self.environment["HOME"])
        claude = self.root / "custom-claude"
        environment = {**self.environment, "CLAUDE_CONFIG_DIR": str(claude)}
        destinations = {"omp": home / ".omp/agent/extensions", "claude": claude}
        before = self.database_state(self.store)
        for client, destination in destinations.items():
            for installed in (False, True):
                with self.subTest(client=client, installed=installed):
                    if installed:
                        manage("install", destination, client=client)
                    completed, result = self.cli("runtime", "status", client, environment=environment)
                    self.assertEqual(completed.returncode, 0)
                    self.assertEqual(result, manage("status", destination, client=client))
                    self.assertNotIn("sessions", result)
                    self.assertNotIn("clients", result)
            explicit = self.root / f"explicit-{client}"
            completed, result = self.cli("runtime", "status", client, "--destination", str(explicit),
                                         environment=environment)
            self.assertEqual(completed.returncode, 0)
            self.assertEqual(result, manage("status", explicit, client=client))
            self.assertFalse(explicit.exists())
        self.assertFalse((home / ".claude").exists())
        self.assertEqual(self.database_state(self.store), before)
        self.assertFalse(self.herdr_calls.exists())

    def test_runtime_status_destination_without_client_is_rejected_without_side_effects(self):
        destination = self.root / "ambiguous-runtime"
        before = self.database_state(self.store)
        completed, result = self.cli("runtime", "status", "--destination", str(destination))
        self.assertEqual((completed.returncode, result["error"]["code"]), (1, "invalid_destination"))
        self.assertIn("client", result["error"]["message"].lower())
        self.assertFalse(destination.exists())
        self.assertEqual(self.database_state(self.store), before)
        self.assertFalse(self.herdr_calls.exists())

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

    def test_inbox_compact_and_verbose_keep_reply_context_without_acknowledging(self):
        parent = self.mail.send(self.sender, self.recipient, body="Review this")
        reply = self.mail.send(self.recipient, self.sender, body="OK", reply_to=parent["message_id"])
        before = self.database_state(self.store)
        for selection in ((), ("--message", reply["message_id"])):
            with self.subTest(selection=selection):
                completed, compact = self.cli("inbox", "read", *selection)
                self.assertEqual(completed.returncode, 0)
                self.assertNotIn("terminal_id", compact)
                message = compact["messages"][0]
                self.assertEqual(
                    (message["message_id"], message["thread_id"], message["sender"],
                     message["reply_to"], message["hop"], message["body"], message["state"]),
                    (reply["message_id"], parent["thread_id"], "alpha/" + self.recipient["alias"],
                     parent["message_id"], 1, "OK", "queued"))
                self.assertTrue({"payload_hash", "claimed_generation", "delivered_at"}.isdisjoint(message))
                completed, verbose = self.cli("inbox", "read", *selection, "--verbose")
                self.assertEqual(completed.returncode, 0)
                self.assertEqual(verbose, self.mail.read(self.sender, message_id=reply["message_id"]))
                self.assertIn("payload_hash", verbose["messages"][0])
                self.assertIn("delivered_at", verbose["messages"][0])
        self.assertEqual(self.database_state(self.store), before)

    def test_status_list_compact_and_verbose_preserve_the_python_api(self):
        before = self.database_state(self.store)
        completed, compact = self.cli("status", "list")
        self.assertEqual(completed.returncode, 0)
        self.assertEqual({row["repo_id"] for row in compact["agents"]},
                         {self.repo_id, self.other["repo_id"]})
        self.assertTrue(all("agent_id" not in row and "session_ref" not in row
                            for row in compact["agents"]))
        expected = ledger_cli.status_operation("list", environment=self.environment)
        for arguments in (("status", "list", "--verbose"), ("status", "--verbose", "list")):
            completed, verbose = self.cli(*arguments)
            self.assertEqual(completed.returncode, 0)
            self.assertEqual(verbose, expected)
        completed, filtered = self.cli("status", "list", "--repo", self.repo_id)
        self.assertEqual(completed.returncode, 0)
        self.assertTrue(all("repo_id" not in row for row in filtered["agents"]))
        self.assertEqual(self.database_state(self.store), before)

    def test_board_queries_real_private_peer_and_reports_failure_without_inventing_agents(self):
        failure = self.add_peer_endpoint()
        before = [self.database_state(store) for store in (self.store, self.peer_store)]
        completed, board = self.cli("status", "board", "--repo", self.repo_id)
        self.assertEqual(completed.returncode, 0)
        self.assertEqual({row["host"] for row in board["hosts"]}, {"alpha", "beta"})
        self.assertEqual({row["phase"] for row in board["agents"] if row["host"] == "beta"}, {"remote-review"})
        local = next(row for row in board["agents"] if row["address"] == "alpha/" + self.sender["alias"])
        self.assertEqual(local["summary"], "Read-only summary")
        self.assertTrue({"role", "turn_state", "phase", "summary", "unread", "extension_present",
                         "workspace", "devices", "servers", "stale"} <= local.keys())
        self.assertTrue({"agent_id", "session_ref", "generation", "status_updated_at", "last_seen"}.isdisjoint(local))
        self.assertTrue(all("agents" not in row for row in board["hosts"]))
        completed, verbose = self.cli("status", "board", "--repo", self.repo_id, "--verbose")
        self.assertEqual(completed.returncode, 0)
        full = next(row for row in verbose["agents"] if row["agent_id"] == self.sender["agent_id"])
        self.assertEqual(full["status_updated_at"], 125.0)
        self.assertEqual(full, next(row for row in verbose["hosts"][0]["agents"]
                                   if row["agent_id"] == self.sender["agent_id"]))
        failure.write_text("offline")
        completed, failed = self.cli("status", "board", "--repo", self.repo_id)
        self.assertEqual(completed.returncode, 0)
        peer = next(row for row in failed["hosts"] if row["host"] == "beta")
        self.assertEqual((peer["stale"], peer["unread"], peer["error"]["code"]),
                         (True, "?", "transport_unavailable"))
        self.assertNotIn("agents", peer)
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
