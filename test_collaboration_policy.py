"""Policy behavior against private real ledgers and legacy submission stores."""
import ham_test_support  # Protect the live ledger before product imports.
from dataclasses import replace
import uuid
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from agent_batch_dispatch import BatchRequest, DispatchOutcome, SUBMITTED, dispatch_batch
from agent_directory import parse_agent_payload
from collaboration_policy import CollaborationPolicy
from ledger_remote import RemoteLedger
from ledger_settings import RepoSettings
from ledger_status import AgentRegistry
from ledger_store import LedgerError, LedgerStore
from request_lifecycle import JsonRequestStateStore


class StatusEndpoint:
    def __init__(self, local, remote):
        self.local = RemoteLedger(local)
        self.remote = RemoteLedger(remote)
        self.calls = []
        self.error = None
        self.transform = lambda value: value

    def status_export(self):
        return self.local.status_export()

    def query_status(self, host, repo_id=None):
        self.calls.append((host, repo_id))
        if self.error:
            raise self.error
        return self.transform(self.remote.status_export(repo_id))


class CollaborationPolicyTest(unittest.TestCase):
    def setUp(self):
        scanner = mock.patch("resource_scan.scan_host_resources",
                             side_effect=AssertionError("policy must not scan host resources"))
        scanner.start()
        self.addCleanup(scanner.stop)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.project = self.project_at("project", "git:policy/shared")
        self.remote_project = self.project_at("remote-project", "git:policy/shared")
        self.other_project = self.project_at("other-project", "git:policy/other")
        self.local = LedgerStore(self.root / "local-ledger", canonical_name="alpha")
        self.remote = LedgerStore(self.root / "remote-ledger", canonical_name="beta")
        self.local_snapshot = self.snapshot(self.project, "a")
        self.remote_snapshot = self.snapshot(self.remote_project, "b")
        # These policy scenarios model a repository known before auto-default
        # rollout; participation changes below remain explicit test actions.
        for store in (self.local, self.remote):
            with store.transaction() as connection:
                connection.execute("INSERT INTO repos VALUES (?,NULL,?)", ("git:policy/shared", 1.0))
        AgentRegistry(self.local).reconcile(self.local_snapshot, refresh_display=False)
        AgentRegistry(self.remote).reconcile(self.remote_snapshot, refresh_display=False)
        self.sender = AgentRegistry(self.local).list()[0]
        self.repo_id = self.sender["repo_id"]
        self.targets = parse_agent_payload({"result": self.remote_snapshot}, host="remote-alias", local=False)
        self.local_targets = parse_agent_payload({"result": self.local_snapshot}, host="local", local=True)
        self.endpoint = StatusEndpoint(self.local, self.remote)
        self.requests = JsonRequestStateStore(self.root / "legacy-requests")

    def project_at(self, name, repo_id):
        path = self.root / name
        path.mkdir()
        subprocess.run(["git", "init", "-q", str(path)], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(path), "config", "ham.repo-id", repo_id], check=True, capture_output=True)
        return path

    @staticmethod
    def snapshot(directory, prefix):
        panes = [{"terminal_id": f"{prefix}-{index}", "pane_id": f"{prefix}:p{index}", "workspace_id": prefix}
                 for index in range(3)]
        agents = [{**pane, "agent": "codex", "agent_status": "idle", "name": f"bird-{index}",
                   "agent_session": {"value": f"session-{pane['terminal_id']}"}, "cwd": str(directory)}
                  for index, pane in enumerate(panes)]
        return {"panes": panes, "agents": agents}

    @staticmethod
    def settings(store):
        return RepoSettings(store, display_runner=lambda command: subprocess.CompletedProcess(
            command, 0, '{"result":{"pane":{"label":"manual fixture"}}}', ""))

    def mode(self, store, value):
        self.settings(store).mutate(self.repo_id, "mode", value)

    def members(self, store, *terminals):
        self.settings(store).mutate(self.repo_id, "members", [
            {"host": store.host, "terminal_id": terminal} for terminal in terminals])

    def policy(self):
        return CollaborationPolicy(self.local, self.sender, remote=self.endpoint)

    def database(self):
        with self.local.read() as connection:
            return "\n".join(connection.iterdump())

    def batch(self, policy, recipients):
        def resolve(request):
            target = recipients[int(request.route)]
            policy.check(target)
            return BatchRequest(request.route, request.message), target.identity

        def submit(request):
            self.requests.save(uuid.uuid4().hex, {"state": "submitted", "message": request.message})
            return DispatchOutcome(SUBMITTED)

        return dispatch_batch([{"route": str(index), "message": "Run the assigned work."}
                               for index in range(len(recipients))], submit,
                              resolve_request=resolve, before_dispatch=policy.before_dispatch)

    def test_off_sender_needs_no_remote_peer_or_query(self):
        self.endpoint.error = LedgerError("peer_not_configured", "No peer setup")
        before = self.database()
        payload = self.batch(self.policy(), self.targets)
        self.assertEqual([item["status"] for item in payload["results"]], [SUBMITTED] * 3)
        self.assertEqual(len(self.requests.list_request_ids()), 3)
        self.assertEqual(self.endpoint.calls, [])
        self.assertEqual(self.database(), before)

    def test_manual_nonmember_sender_keeps_ordinary_legacy_behavior(self):
        self.mode(self.local, "manual")
        self.members(self.local, "a-1")
        self.endpoint.error = LedgerError("host_unavailable", "offline")
        self.assertEqual(self.batch(self.policy(), self.targets)["status"], "succeeded")
        self.assertEqual(self.endpoint.calls, [])

    def test_manual_main_is_participant_without_members_entry(self):
        self.mode(self.local, "manual")
        self.settings(self.local).mutate(self.repo_id, "main", {"host": "alpha", "terminal_id": "a-0"})
        self.members(self.local, "a-1")
        with self.assertRaises(LedgerError) as caught:
            self.policy().check(self.local_targets[1])
        self.assertEqual(caught.exception.code, "use_mailbox")

    def test_manual_nonmember_recipient_remains_allowed(self):
        self.mode(self.local, "manual")
        self.members(self.local, "a-0")
        self.policy().check(self.local_targets[1])
        self.assertEqual(self.endpoint.calls, [])

    def test_remote_off_and_nonmember_recipients_remain_allowed(self):
        self.mode(self.local, "auto")
        for mode in ("off", "manual"):
            with self.subTest(mode=mode):
                self.mode(self.remote, mode)
                policy = self.policy()
                policy.check(self.targets[0])
                policy.before_dispatch()

    def test_same_repo_remote_manual_and_auto_are_blocked_without_state_changes(self):
        self.mode(self.local, "auto")
        for mode in ("manual", "auto"):
            with self.subTest(mode=mode):
                self.mode(self.remote, mode)
                self.members(self.remote, "b-0")
                before = self.database()
                with self.assertRaises(LedgerError) as caught:
                    self.policy().check(self.targets[0])
                self.assertEqual(caught.exception.code, "use_mailbox")
                self.assertIn("mail send --to remote-alias/bird-0", caught.exception.message)
                self.assertEqual(self.database(), before)
                self.assertEqual(self.requests.list_request_ids(), [])

    def test_authoritative_different_repo_allows_identical_looking_remote_cwd(self):
        self.mode(self.local, "auto")
        changed = self.snapshot(self.other_project, "b")
        AgentRegistry(self.remote).reconcile(changed, refresh_display=False)
        other_repo = AgentRegistry(self.remote).list()[0]["repo_id"]
        self.settings(self.remote).mutate(other_repo, "mode", "auto")
        target = replace(self.targets[0], cwd=str(self.project))
        self.policy().check(target)
        self.assertEqual(self.endpoint.calls, [("remote-alias", None)])

    def test_batch_checks_every_target_and_caches_one_unfiltered_host_export(self):
        self.mode(self.local, "auto")
        self.mode(self.remote, "manual")
        self.members(self.remote, "b-1")
        before = self.database()
        with self.assertRaises(LedgerError) as caught:
            self.batch(self.policy(), self.targets)
        self.assertEqual(caught.exception.code, "use_mailbox")
        self.assertEqual(self.endpoint.calls, [("remote-alias", None)])
        self.assertEqual(self.requests.list_request_ids(), [])
        self.assertEqual(self.database(), before)

    def test_blocked_first_middle_or_last_never_writes_legacy_submissions(self):
        self.mode(self.local, "auto")
        self.mode(self.remote, "manual")
        for blocked in range(3):
            with self.subTest(blocked=blocked):
                self.members(self.remote, f"b-{blocked}")
                with self.assertRaises(LedgerError):
                    self.batch(self.policy(), self.targets)
                self.assertEqual(self.requests.list_request_ids(), [])

    def test_remote_failure_blocks_local_allowed_recipient_before_any_queue_write(self):
        self.mode(self.local, "manual")
        self.members(self.local, "a-0")
        self.endpoint.error = LedgerError("transport_unavailable", "SSH response lost")
        before = self.database()
        with self.assertRaises(LedgerError) as caught:
            self.batch(self.policy(), [self.local_targets[1], *self.targets])
        self.assertEqual(caught.exception.code, "transport_unavailable")
        self.assertEqual(self.endpoint.calls, [("remote-alias", None)])
        self.assertEqual(self.requests.list_request_ids(), [])
        self.assertEqual(self.database(), before)

    def test_external_remote_discovery_error_is_not_swallowed_by_batch(self):
        self.mode(self.local, "auto")
        policy = self.policy()

        def resolve(request):
            if request.route == "remote":
                error = LedgerError("host_unavailable", "remote discovery failed")
                policy.record_error(error)
                raise error
            return request, request.route

        with self.assertRaises(LedgerError) as caught:
            dispatch_batch([{"route": "allowed", "message": "one"}, {"route": "remote", "message": "two"}],
                           lambda request: self.requests.save(uuid.uuid4().hex, {}),
                           resolve_request=resolve, before_dispatch=policy.before_dispatch)
        self.assertEqual(caught.exception.code, "host_unavailable")
        self.assertEqual(self.requests.list_request_ids(), [])

    def test_unverifiable_remote_metadata_fails_entire_batch(self):
        self.mode(self.local, "auto")
        transforms = [lambda value: {}, lambda value: [],
                      lambda value: {**value, "agents": []},
                      lambda value: {**value, "agents": [*value["agents"], value["agents"][0]]}]
        for field, replacement in (("repo_id", None), ("role", "unrecognized"),
                                   ("role", []), ("session_ref", "replaced"), ("client", "omp")):
            def change(value, field=field, replacement=replacement):
                value["agents"][0][field] = replacement
                return value
            transforms.append(change)
        for transform in transforms:
            with self.subTest(transform=transform):
                self.endpoint.transform = transform
                with self.assertRaises(LedgerError) as caught:
                    self.batch(self.policy(), self.targets)
                self.assertEqual(caught.exception.code, "policy_unverifiable")
                self.assertEqual(self.requests.list_request_ids(), [])

    def test_missing_membership_field_is_not_treated_as_nonparticipant(self):
        self.mode(self.local, "auto")

        def remove_role(value):
            del value["agents"][0]["role"]
            return value

        self.endpoint.transform = remove_role
        with self.assertRaises(LedgerError) as caught:
            self.policy().check(self.targets[0])
        self.assertEqual(caught.exception.code, "policy_unverifiable")


if __name__ == "__main__":
    unittest.main()
