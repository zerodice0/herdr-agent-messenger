"""Exercise legacy CLI policy against private real registries and request stores."""

from copy import deepcopy
from io import StringIO
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

import agent_skill_cli
import ledger_display
import ledger_status
from agent_directory import ProbeResult, parse_agent_payload
from ledger_remote import RemoteLedger, SSHTransport
from ledger_settings import RepoSettings
from ledger_status import AgentRegistry
from ledger_store import LedgerError, LedgerStore
from request_lifecycle import JsonRequestStateStore


class CliCollaborationPolicyTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="ham-cli-policy-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.repo_id = "git:cli-policy/shared"
        self.project = self.project_at("project", self.repo_id)
        self.other_project = self.project_at("other-project", "git:cli-policy/other")
        self.environment = {
            "HOME": str(self.root / "home"),
            "XDG_STATE_HOME": str(self.root / "state"),
            "XDG_CONFIG_HOME": str(self.root / "config"),
            "HERDR_SOCKET_PATH": str(self.root / "inert.sock"),
            "HERDR_PANE_ID": "a:p0",
            "HERDR_TERMINAL_ID": "a-0",
            "HERDR_AGENT_LEDGER_DIR": str(self.root / "alpha-ledger"),
            "HERDR_AGENT_REQUEST_STATE_DIR": str(self.root / "legacy-requests"),
            "HERDR_AGENT_LEDGER_AUTOSTART": "0",
            "HERDR_AGENT_MESSENGER_SSH_CONFIG": str(self.root / "ssh-config"),
            "HERDR_AGENT_MESSENGER_SSH_HOSTS_FILE": str(self.root / "ssh-hosts"),
        }
        aliases = ("beta-alias", "gamma-alias", "delta-alias")
        Path(self.environment["HERDR_AGENT_MESSENGER_SSH_CONFIG"]).write_text(
            "".join(f"Host {alias}\n  HostName unused.invalid\n" for alias in aliases),
            encoding="utf-8",
        )
        Path(self.environment["HERDR_AGENT_MESSENGER_SSH_HOSTS_FILE"]).write_text(
            "\n".join(aliases) + "\n", encoding="utf-8"
        )
        environment_patch = mock.patch.dict(os.environ, self.environment)
        environment_patch.start()
        self.addCleanup(environment_patch.stop)
        self.local = LedgerStore(environment=self.environment, canonical_name="alpha")
        self.remotes = {
            alias: LedgerStore(self.root / f"{name}-ledger", canonical_name=name)
            for alias, name in zip(aliases, ("beta", "gamma", "delta"))
        }
        self.local_snapshot = self.snapshot(self.project, "a")
        self.remote_snapshots = {
            alias: self.snapshot(self.project, prefix)
            for alias, prefix in zip(aliases, ("b", "c", "d"))
        }
        # Existing policy scenarios start with repositories known before rollout.
        for store in (self.local, *self.remotes.values()):
            with store.transaction() as connection:
                connection.execute("INSERT INTO repos VALUES (?,NULL,?)", (self.repo_id, 1.0))
        AgentRegistry(self.local).reconcile(self.local_snapshot, refresh_display=False)
        for alias, store in self.remotes.items():
            AgentRegistry(store).reconcile(self.remote_snapshots[alias], refresh_display=False)
        self.requests = JsonRequestStateStore(Path(self.environment["HERDR_AGENT_REQUEST_STATE_DIR"]))
        self.status_calls = []
        self.discovery_calls = []
        self.transport_calls = []
        self.display_calls = []
        self.events = []
        self.status_errors = {}
        self.discovery_errors = {}
        self.turn_observation = None
        self.prompt_error = ""
        self.patch(agent_skill_cli, "probe_local_agents", self.local_probe)
        self.patch(agent_skill_cli, "query_remote_agents", self.remote_probe)
        self.patch(ledger_status, "live_snapshot", lambda environment=None: deepcopy(self.local_snapshot))
        self.patch(agent_skill_cli, "run_bounded_command", self.native_command)
        self.patch(ledger_display, "run_bounded_command", self.display_command)
        # Only replace the wire call: peer selection, identity checks, roles and
        # status-export all remain their real implementations.
        self.patch(SSHTransport, "call", lambda transport, peer, command, payload: self.wire_call(peer, command, payload))

    def patch(self, owner, name, replacement):
        patch = mock.patch.object(owner, name, replacement)
        patch.start()
        self.addCleanup(patch.stop)

    def project_at(self, name, repo_id):
        path = self.root / name
        path.mkdir()
        subprocess.run(["git", "init", "-q", str(path)], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(path), "config", "ham.repo-id", repo_id],
                       check=True, capture_output=True)
        return path

    @staticmethod
    def snapshot(directory, prefix):
        panes = [{"terminal_id": f"{prefix}-{index}", "pane_id": f"{prefix}:p{index}",
                  "workspace_id": prefix, "focused": False} for index in range(3)]
        agents = [{**pane, "agent": "codex", "agent_status": "idle", "name": f"bird-{prefix}-{index}",
                   "agent_session": {"value": f"session-{prefix}-{index}"}, "cwd": str(directory)}
                  for index, pane in enumerate(panes)]
        return {"panes": panes, "agents": agents}

    def probe(self, host, snapshot, *, local):
        snapshot = deepcopy(snapshot)
        if self.turn_observation is not None:
            status = "working" if self.turn_observation == 0 else "idle"
            self.turn_observation += 1
            for raw in snapshot["agents"]:
                raw["agent_status"] = status
        agents = parse_agent_payload({"result": snapshot}, host=host, local=local)
        return ProbeResult(host, tuple(agents), True)

    def local_probe(self, environment=None, **kwargs):
        return self.probe("local", self.local_snapshot, local=True)

    def remote_probe(self, host, **kwargs):
        self.discovery_calls.append(host)
        if host in self.discovery_errors:
            return ProbeResult(host, (), False, self.discovery_errors[host])
        return self.probe(host, self.remote_snapshots[host], local=False)

    def display_command(self, command, **kwargs):
        self.display_calls.append(list(command))
        return subprocess.CompletedProcess(command, 0, '{"result":{"pane":{"label":""}}}', "")

    def native_command(self, command, **kwargs):
        self.transport_calls.append(list(command))
        text = " ".join(command)
        if "agent prompt" in text:
            self.events.append("prompt")
            self.turn_observation = 0
            return subprocess.CompletedProcess(command, 1 if self.prompt_error else 0, "", self.prompt_error)
        if "agent read" in text:
            output = "baseline\n" if self.turn_observation is None else "baseline\ncompleted\n"
            return subprocess.CompletedProcess(command, 0, output, "")
        raise AssertionError(f"Unexpected native transport: {command!r}")

    def wire_call(self, peer, command, payload):
        self.assertEqual(command, "status-export")
        host = peer["ssh_alias"]
        self.status_calls.append((host, payload.get("repo_id")))
        self.events.append(host)
        if host in self.status_errors:
            raise self.status_errors[host]
        return RemoteLedger(self.remotes[host]).status_export(payload.get("repo_id"))

    def settings(self, store):
        return RepoSettings(store, environment=self.environment, display_runner=self.display_command)

    def mode(self, store, mode, *, repo_id=None, members=(), main=None):
        settings = self.settings(store)
        repo_id = repo_id or self.repo_id
        settings.mutate(repo_id, "mode", mode)
        settings.mutate(repo_id, "members", [{"host": store.host, "terminal_id": terminal}
                                             for terminal in members])
        settings.mutate(repo_id, "main", {"host": store.host, "terminal_id": main} if main else None)

    def peers(self):
        remote = RemoteLedger(self.local, self.environment)
        for alias, store in self.remotes.items():
            remote.add_peer(canonical_name=store.host, uuid=store.host_uuid,
                            ssh_alias=alias, remote_ham_root=str(self.root / "remote-plugin"))

    def invoke(self, arguments):
        output = StringIO()
        with mock.patch("sys.stdout", output):
            code = agent_skill_cli.main(arguments, self.environment)
        return code, json.loads(output.getvalue())

    @staticmethod
    def single_arguments(command, address):
        arguments = [command, "--address", address, "--message", "Do the assigned work.", "--timeout", "2000"]
        if command == "send":
            arguments.append("--no-wait")
        return arguments

    @staticmethod
    def batch_arguments(addresses, wait):
        return ["batch", "--requests-json", json.dumps([
            {"address": address, "message": f"Assigned work {index}."}
            for index, address in enumerate(addresses)
        ]), "--wait" if wait else "--no-wait", "--max-workers", "1", "--timeout", "2000"]

    def assert_no_legacy_effects(self):
        self.assertEqual(self.transport_calls, [])
        self.assertEqual(self.requests.list_request_ids(), [])
        self.assertFalse(self.requests.directory.exists(), "Even legacy lock/state directories must not be opened")
        with self.local.read() as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM messages").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT count(*) FROM deliveries").fetchone()[0], 0)

    def assert_mailbox_refusal(self, arguments, address):
        code, payload = self.invoke(arguments)
        self.assertEqual(code, 1, payload)
        self.assertEqual(payload["error"]["code"], "use_mailbox", payload)
        self.assertIn(f"mail send --to {address} --body", payload["error"]["message"])
        self.assertIn("No prompt was submitted", payload["error"]["message"])
        self.assert_no_legacy_effects()

    def test_seeded_auto_rejects_legacy_send_request_and_batch_without_user_mutation(self):
        for raw in self.local_snapshot["agents"]:
            raw["cwd"] = str(self.other_project)
        AgentRegistry(self.local).reconcile(self.local_snapshot, refresh_display=False)
        repo_id = "git:cli-policy/other"
        mode = self.settings(self.local).show(repo_id)["settings"]["mode"]
        self.assertEqual((mode["value"], mode["changed_by"]), ("auto", "home"))
        address = "local/bird-a-1"
        for command in ("send", "request"):
            with self.subTest(command=command):
                self.assert_mailbox_refusal(self.single_arguments(command, address), address)
        # Exercise the SQL stored-mode batch gate, not only role() fallbacks.
        for wait in (False, True):
            with self.subTest(batch_wait=wait):
                self.assert_mailbox_refusal(self.batch_arguments([address], wait), address)

    def test_send_and_request_same_repo_remote_manual_auto_members_use_mailbox(self):
        self.peers()
        for sender_mode in ("manual", "auto"):
            for recipient_mode in ("manual", "auto"):
                self.mode(self.local, sender_mode, members=("a-0",))
                self.mode(self.remotes["beta-alias"], recipient_mode, members=("b-0",))
                for command in ("send", "request"):
                    with self.subTest(sender=sender_mode, recipient=recipient_mode, command=command):
                        self.status_calls.clear()
                        address = "beta-alias/bird-b-0"
                        self.assert_mailbox_refusal(self.single_arguments(command, address), address)
                        self.assertEqual(self.status_calls, [("beta-alias", None)])

    def test_send_and_request_same_repo_local_manual_auto_members_use_mailbox(self):
        for mode in ("manual", "auto"):
            self.mode(self.local, mode, members=("a-0", "a-1"))
            for command in ("send", "request"):
                with self.subTest(mode=mode, command=command):
                    address = "local/bird-a-1"
                    self.assert_mailbox_refusal(self.single_arguments(command, address), address)
                    self.assertEqual(self.status_calls, [])

    def test_manual_main_participates_without_explicit_membership(self):
        self.peers()
        self.mode(self.local, "manual", main="a-0")
        self.mode(self.remotes["beta-alias"], "manual", main="b-0")
        for command in ("send", "request"):
            with self.subTest(command=command):
                address = "beta-alias/bird-b-0"
                self.assert_mailbox_refusal(self.single_arguments(command, address), address)

    def assert_ordinary_delivery(self, command, address):
        before = set(self.requests.list_request_ids())
        self.transport_calls.clear()
        self.turn_observation = None
        code, payload = self.invoke(self.single_arguments(command, address))
        self.assertEqual(code, 0, payload)
        self.assertEqual(sum("agent prompt" in " ".join(call) for call in self.transport_calls), 1)
        if command == "send":
            self.assertTrue(payload["sent"])
            self.assertEqual(set(self.requests.list_request_ids()), before)
        else:
            added = set(self.requests.list_request_ids()) - before
            self.assertEqual(len(added), 1)
            record = self.requests.load(added.pop())
            self.assertEqual(record["state"], "submitted_settled")
            self.assertTrue(record["submitted"])
            self.assertTrue(record["terminal"])
            self.assertIn("completed", record["response"]["output"])

    def test_off_sender_keeps_ordinary_send_request_without_peer_or_registry_refresh(self):
        self.mode(self.local, "off")
        self.mode(self.remotes["beta-alias"], "auto")
        with mock.patch.object(ledger_status, "live_snapshot", side_effect=AssertionError("Off must not refresh registry")):
            for command in ("send", "request"):
                with self.subTest(command=command):
                    self.assert_ordinary_delivery(command, "beta-alias/bird-b-0")
        self.assertEqual(self.status_calls, [])
        self.assertEqual(RemoteLedger(self.local).peers(), [])

    def test_manual_nonmember_sender_keeps_ordinary_send_request_without_peer(self):
        self.mode(self.local, "manual", members=("a-1",))
        self.mode(self.remotes["beta-alias"], "auto")
        for command in ("send", "request"):
            with self.subTest(command=command):
                self.assert_ordinary_delivery(command, "beta-alias/bird-b-0")
        self.assertEqual(self.status_calls, [])
        self.assertEqual(RemoteLedger(self.local).peers(), [])

    def test_manual_nonmember_recipient_keeps_ordinary_local_send_request_without_peer(self):
        self.mode(self.local, "manual", members=("a-0",))
        for command in ("send", "request"):
            with self.subTest(command=command):
                self.assert_ordinary_delivery(command, "local/bird-a-1")
        self.assertEqual(self.status_calls, [])
        self.assertEqual(RemoteLedger(self.local).peers(), [])

    def test_remote_off_or_manual_nonmember_recipient_keeps_ordinary_delivery(self):
        self.peers()
        self.mode(self.local, "auto")
        for mode in ("off", "manual"):
            self.mode(self.remotes["beta-alias"], mode, members=("b-1",))
            for command in ("send", "request"):
                with self.subTest(mode=mode, command=command):
                    self.status_calls.clear()
                    self.assert_ordinary_delivery(command, "beta-alias/bird-b-0")
                    self.assertEqual(self.status_calls, [("beta-alias", None)])

    def test_different_repo_local_recipient_keeps_ordinary_delivery_without_peer(self):
        self.local_snapshot["agents"][1]["cwd"] = str(self.other_project)
        AgentRegistry(self.local).reconcile(self.local_snapshot, refresh_display=False)
        self.mode(self.local, "auto")
        self.mode(self.local, "auto", repo_id="git:cli-policy/other")
        for command in ("send", "request"):
            with self.subTest(command=command):
                self.assert_ordinary_delivery(command, "local/bird-a-1")
        self.assertEqual(self.status_calls, [])
        self.assertEqual(RemoteLedger(self.local).peers(), [])

    def test_off_request_transport_failure_retains_ordinary_lifecycle_record(self):
        self.mode(self.local, "off")
        self.prompt_error = "Native prompt was rejected"
        code, payload = self.invoke(self.single_arguments("request", "beta-alias/bird-b-0"))
        self.assertEqual(code, 1, payload)
        request_ids = self.requests.list_request_ids()
        self.assertEqual(len(request_ids), 1)
        record = self.requests.load(request_ids[0])
        self.assertEqual(record["state"], "submission_failed")
        self.assertEqual(record["error_code"], "prompt_failed")
        self.assertIn(self.prompt_error, record["error"])
        self.assertFalse(record["submitted"])
        self.assertEqual(self.status_calls, [])

    def test_different_repo_remote_uses_authoritative_identity_not_discovery_cwd(self):
        self.peers()
        self.mode(self.local, "auto")
        actual = self.snapshot(self.other_project, "b")
        AgentRegistry(self.remotes["beta-alias"]).reconcile(actual, refresh_display=False)
        self.mode(self.remotes["beta-alias"], "auto", repo_id="git:cli-policy/other")
        # Discovery still advertises the sender's path. Only the actual remote
        # registry's repository identity is authoritative for the policy.
        for command in ("send", "request"):
            with self.subTest(command=command):
                self.status_calls.clear()
                self.assert_ordinary_delivery(command, "beta-alias/bird-b-0")
                self.assertEqual(self.status_calls, [("beta-alias", None)])

    def test_allowed_batch_reuses_preflight_policy_and_queries_before_transport(self):
        self.peers()
        self.mode(self.local, "auto")
        addresses = [f"beta-alias/bird-b-{index}" for index in range(3)]
        self.events.clear()
        code, payload = self.invoke(self.batch_arguments(addresses, False))
        self.assertEqual(code, 3, payload)
        self.assertEqual([item["status"] for item in payload["results"]], ["submitted"] * 3)
        self.assertEqual(self.status_calls, [("beta-alias", None)])
        self.assertEqual(self.events, ["beta-alias", "prompt", "prompt", "prompt"])
        self.assertEqual(self.requests.list_request_ids(), [])

    def test_batch_blocked_first_middle_last_preflights_every_host_without_writes(self):
        self.peers()
        self.mode(self.local, "auto")
        addresses = [f"beta-alias/bird-b-{index}" for index in range(3)] + ["gamma-alias/bird-c-0"]
        for blocked in range(3):
            self.mode(self.remotes["beta-alias"], "manual", members=(f"b-{blocked}",))
            for wait in (False, True):
                with self.subTest(blocked=blocked, wait=wait):
                    self.status_calls.clear()
                    self.discovery_calls.clear()
                    self.assert_mailbox_refusal(self.batch_arguments(addresses, wait), addresses[blocked])
                    self.assertEqual(self.discovery_calls, ["beta-alias"] * 3 + ["gamma-alias"])
                    self.assertEqual(self.status_calls, [("beta-alias", None), ("gamma-alias", None)])

    def test_batch_late_remote_query_failure_still_checks_later_hosts_before_commit(self):
        self.peers()
        self.mode(self.local, "manual", members=("a-0",))
        self.status_errors["gamma-alias"] = LedgerError("transport_unavailable", "Status reply was lost")
        addresses = ["local/bird-a-1", "beta-alias/bird-b-0", "gamma-alias/bird-c-0",
                     "gamma-alias/bird-c-1", "delta-alias/bird-d-0"]
        for wait in (False, True):
            with self.subTest(wait=wait):
                self.status_calls.clear()
                self.discovery_calls.clear()
                code, payload = self.invoke(self.batch_arguments(addresses, wait))
                self.assertEqual(code, 1, payload)
                self.assertEqual(payload["error"]["code"], "transport_unavailable", payload)
                self.assertEqual(self.status_calls, [("beta-alias", None), ("gamma-alias", None), ("delta-alias", None)])
                self.assertEqual(self.discovery_calls,
                                 ["beta-alias", "gamma-alias", "gamma-alias", "delta-alias"])
                self.assert_no_legacy_effects()

    def test_batch_failed_discovery_before_lazy_policy_creation_is_retained(self):
        self.peers()
        self.mode(self.local, "manual", members=("a-0",))
        self.discovery_errors["beta-alias"] = "The target snapshot is unavailable"
        addresses = ["beta-alias/bird-b-0", "local/bird-a-1", "gamma-alias/bird-c-0"]
        for wait in (False, True):
            with self.subTest(wait=wait):
                self.status_calls.clear()
                self.discovery_calls.clear()
                code, payload = self.invoke(self.batch_arguments(addresses, wait))
                self.assertEqual(code, 1, payload)
                self.assertEqual(payload["error"]["code"], "host_unavailable", payload)
                self.assertEqual(self.discovery_calls, ["beta-alias", "gamma-alias"])
                self.assertEqual(self.status_calls, [("gamma-alias", None)])
                self.assert_no_legacy_effects()

    def test_batch_initial_policy_snapshot_failure_is_retained_after_context_recovers(self):
        self.peers()
        self.mode(self.local, "manual", members=("a-0",))
        addresses = ["local/bird-a-1", "gamma-alias/bird-c-0"]
        for wait in (False, True):
            with self.subTest(wait=wait):
                self.status_calls.clear()
                self.discovery_calls.clear()
                failure = LedgerError("host_unavailable", "Temporary local snapshot failure")
                # Both recipients resolve and are nonparticipants. The first
                # policy initialization alone fails; the second gets a real,
                # participating sender context and otherwise permits delivery.
                with mock.patch.object(
                    ledger_status, "live_snapshot",
                    side_effect=[failure, deepcopy(self.local_snapshot)],
                ) as snapshot:
                    code, payload = self.invoke(self.batch_arguments(addresses, wait))
                self.assertEqual(snapshot.call_count, 2)
                self.assertEqual(code, 1, payload)
                self.assertEqual(payload["error"]["code"], failure.code, payload)
                self.assertIn(failure.message, payload["error"]["message"])
                self.assertEqual(self.discovery_calls, ["gamma-alias"])
                self.assertEqual(self.status_calls, [("gamma-alias", None)])
                self.assert_no_legacy_effects()


if __name__ == "__main__":
    unittest.main()
