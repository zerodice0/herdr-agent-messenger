"""Public display mutations request only the existing ledger worker lifecycle."""
import tests.ham_test_support as ham_test_support  # Protect live state before product imports.

from argparse import Namespace
import os
from unittest import mock

from ham import cli as agent_cli
from ham.ledger import cli, devices, display_repair, worker
from ham.ledger.settings import RepoSettings
from ham.ledger.status import AgentRegistry
from ham.ledger.store import LedgerError, LedgerStore
from tests.test_display_repair import EffectiveDisplay


class DisplayRepairLifecycleTest(ham_test_support.IsolatedLedgerTest):
    def setUp(self):
        super().setUp()
        self.environment.update(HERDR_AGENT_LEDGER_AUTOSTART="1", HERDR_TERMINAL_ID="terminal")
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.store = LedgerStore(environment=self.environment, canonical_name="display-lifecycle-test")
        self.repo_id = "display-lifecycle-repo"
        identity = mock.patch("ham.ledger.status.repository_identity", return_value={
            "repo_id": self.repo_id, "is_main_checkout": True,
        })
        identity.start()
        self.addCleanup(identity.stop)
        # Existing repositories without a mode remain off, with no mail/role work.
        with self.store.transaction() as connection:
            connection.execute("INSERT INTO repos VALUES (?,NULL,?)", (self.repo_id, 1.0))
        self.herdr = EffectiveDisplay()
        self.registry = AgentRegistry(self.store, display_runner=self.herdr)
        pane = {"terminal_id": "terminal", "pane_id": "pane", "workspace_id": "workspace"}
        self.template = {"panes": [pane], "agents": [{
            **pane, "agent": "omp", "name": "orange-rabbit", "agent_status": "idle",
            "agent_session": {"value": "session"}, "cwd": str(self.repo),
        }]}
        self.registry.reconcile(self.snapshot())
        self.registry.reconcile(self.snapshot())
        self.agent = self.registry.list()[0]
        patches = (
            mock.patch.object(cli, "AgentRegistry", return_value=self.registry),
            mock.patch.object(devices, "AgentRegistry", return_value=self.registry),
            mock.patch.object(self.registry, "resolve_context", return_value=self.agent),
            mock.patch("ham.ledger.status.live_snapshot", side_effect=lambda environment: self.snapshot()),
            mock.patch("ham.ops.resource_scan.scan_host_resources",
                       return_value={"listeners": [], "serve": [], "error": None}),
            mock.patch("subprocess.run", side_effect=AssertionError("real subprocess forbidden")),
            mock.patch.object(worker.os, "fork", side_effect=AssertionError("real worker start forbidden")),
        )
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.cli_ensure = self.patch_ensure(cli)
        self.device_ensure = self.patch_ensure(worker)
        self.assertEqual(RepoSettings(self.store).show(self.repo_id)["mode"], "off")
        self.assertFalse(worker.maintenance_pending(self.store))

    def patch_ensure(self, module):
        patch = mock.patch.object(module, "ensure_worker", return_value={"started": False})
        ensure = patch.start()
        self.addCleanup(patch.stop)
        return ensure

    def snapshot(self):
        return self.herdr.snapshot(self.template)

    def assert_pending(self):
        with self.store.read() as connection:
            entry = display_repair.load(connection)["pane"]
        self.assertEqual(entry["state"], "pending")
        self.assertTrue(worker.maintenance_pending(self.store))

    def assert_ensured(self, ensure):
        ensure.assert_called_once()
        store, environment = ensure.call_args.args
        self.assertEqual(store.directory, self.store.directory)
        self.assertIs(environment, self.environment)

    def status(self, operation="set", *, environment=None):
        return cli.status_operation(operation, environment=self.environment if environment is None else environment,
                                    phase="review", summary="Committed task")

    def device(self, *arguments, environment=None, dispatch=False):
        parsed = agent_cli.parse_cli_arguments(["device", *arguments])
        operation = cli.dispatch if dispatch else devices.device_operation
        return operation(parsed, self.environment if environment is None else environment)

    def seed_claim(self):
        devices.DeviceClaims(self.store, registry=self.registry).claim(
            self.agent, "phone", "serial", label="Pixel", environment=self.environment)
        self.registry.reconcile(self.snapshot())
        self.assertFalse(worker.maintenance_pending(self.store))

    def test_public_status_display_failure_in_off_mode_ensures_after_commit(self):
        self.herdr.metadata_fails = True

        def ensure(store, environment):
            self.assertEqual(self.registry.list()[0]["summary"], "Committed task")
            self.assert_pending()

        self.cli_ensure.side_effect = ensure
        result = self.status()
        self.assertEqual(result["display"]["failed"], ["pane"])
        self.assert_ensured(self.cli_ensure)
        self.device_ensure.assert_not_called()

    def test_public_status_clear_display_failure_ensures_worker(self):
        self.registry.set_status(self.agent, phase="review", summary="Previous task")
        self.registry.reconcile(self.snapshot())
        self.herdr.metadata_fails = True
        result = self.status("clear")
        self.assertEqual((result["phase"], result["summary"]), ("", ""))
        self.assertEqual(result["display"]["failed"], ["pane"])
        self.assert_pending()
        self.assert_ensured(self.cli_ensure)

    def test_public_device_claim_display_failure_ensures_after_commit(self):
        self.herdr.metadata_fails = True

        def ensure(store, environment):
            with self.store.read() as connection:
                self.assertEqual(connection.execute("SELECT device_id FROM device_claims").fetchone()[0], "serial")
            self.assert_pending()

        self.device_ensure.side_effect = ensure
        result = self.device("claim", "phone", "serial", "--label", "Pixel", dispatch=True)
        self.assertEqual(result["device"]["device_id"], "serial")
        self.assert_ensured(self.device_ensure)
        self.cli_ensure.assert_not_called()

    def test_public_device_release_display_failure_ensures_after_commit(self):
        self.seed_claim()
        self.herdr.metadata_fails = True

        def ensure(store, environment):
            with self.store.read() as connection:
                self.assertEqual(connection.execute("SELECT count(*) FROM device_claims").fetchone()[0], 0)
            self.assert_pending()

        self.device_ensure.side_effect = ensure
        result = self.device("release", "phone", "serial")
        self.assertEqual([row["device_id"] for row in result["released"]], ["serial"])
        self.assert_ensured(self.device_ensure)

    def test_autostart_zero_keeps_public_mutations_committed_without_ensure(self):
        environment = {**self.environment, "HERDR_AGENT_LEDGER_AUTOSTART": "0"}
        self.herdr.metadata_fails = True
        self.assertEqual(self.status(environment=environment)["summary"], "Committed task")
        self.assertEqual(self.device("claim", "phone", "serial", "--label", "Pixel",
                                     environment=environment)["device"]["device_id"], "serial")
        self.assertEqual(len(self.device("release", "phone", "serial", environment=environment)["released"]), 1)
        with mock.patch.object(cli, "_dispatch", return_value={"committed": True}):
            result = cli.dispatch(Namespace(command="ledger", ledger_command="event"), environment)
        self.assertTrue(result["committed"])
        self.assert_pending()
        self.cli_ensure.assert_not_called()
        self.device_ensure.assert_not_called()

    def test_read_only_status_board_list_device_list_never_ensure_even_with_pending_work(self):
        self.herdr.metadata_fails = True
        self.registry.set_status(self.agent, phase="review", summary="Pending repair")
        self.assert_pending()
        for operation in ("list", "board"):
            with self.subTest(operation=operation):
                result = cli.status_operation(operation, environment=self.environment, host="local")
                self.assertIn("agents", result)
        self.assertEqual(self.device("list", dispatch=True)["devices"], [])
        self.assertTrue(worker.worker_status(self.store)["maintenance_pending"])
        self.cli_ensure.assert_not_called()
        self.device_ensure.assert_not_called()

    def test_read_only_dispatch_preserves_no_ensure_behavior(self):
        commands = (
            Namespace(command="repo", repo_command="show"),
            Namespace(command="mail", mail_command="show"),
            Namespace(command="mail", mail_command="threads"),
            Namespace(command="ledger", ledger_command="worker"),
        )
        with mock.patch.object(cli, "_dispatch", return_value={"read_only": True}):
            for arguments in commands:
                with self.subTest(command=vars(arguments)):
                    self.assertTrue(cli.dispatch(arguments, self.environment)["read_only"])
        self.cli_ensure.assert_not_called()
        self.device_ensure.assert_not_called()

    def test_worker_start_errors_are_advisory_after_status_commit(self):
        self.herdr.metadata_fails = True
        for error in (OSError("fork unavailable"), LedgerError("worker_unavailable", "worker unavailable")):
            with self.subTest(error=type(error).__name__):
                self.cli_ensure.side_effect = error
                result = self.status()
                self.assertEqual(result["summary"], "Committed task")
                self.assertEqual(self.registry.list()[0]["summary"], "Committed task")
                self.assertEqual(result["warnings"], [{"code": "worker_start_failed", "message": str(error),
                                                       "committed": True}])
                self.assert_pending()

    def test_worker_start_errors_are_advisory_after_device_commit(self):
        self.herdr.metadata_fails = True
        self.device_ensure.side_effect = OSError("fork unavailable")
        claimed = self.device("claim", "phone", "serial", "--label", "Pixel")
        self.assertEqual(claimed["device"]["device_id"], "serial")
        self.assertEqual(claimed["warnings"], [{"code": "worker_start_failed", "message": "fork unavailable",
                                               "committed": True}])
        self.device_ensure.side_effect = LedgerError("worker_unavailable", "worker unavailable")
        released = self.device("release", "phone", "serial")
        self.assertEqual(len(released["released"]), 1)
        self.assertEqual(released["warnings"], [{"code": "worker_start_failed", "message": "worker unavailable",
                                                "committed": True}])
        with self.store.read() as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM device_claims").fetchone()[0], 0)
        self.assert_pending()

    def test_dispatch_retains_cli_ensure_patch_seam_and_existing_warnings(self):
        previous = {"code": "existing_warning"}
        self.cli_ensure.side_effect = OSError("fork unavailable")
        with mock.patch.object(cli, "_dispatch", return_value={"committed": True, "warnings": [previous]}):
            result = cli.dispatch(Namespace(command="ledger", ledger_command="event"), self.environment)
        self.assertTrue(result["committed"])
        self.assertEqual(result["warnings"], [previous, {"code": "worker_start_failed",
                                                        "message": "fork unavailable", "committed": True}])
        self.assert_ensured(self.cli_ensure)
        self.device_ensure.assert_not_called()

    def test_helper_respects_process_autostart_setting_without_explicit_environment(self):
        payload = {"committed": True}
        with mock.patch.dict(os.environ, HERDR_AGENT_LEDGER_AUTOSTART="0"):
            self.assertIs(worker.ensure_after_mutation(self.store, payload), payload)
        self.device_ensure.assert_not_called()

    def test_library_mutations_never_ensure_worker(self):
        self.herdr.metadata_fails = True
        self.registry.set_status(self.agent, phase="review", summary="Library only")
        claims = devices.DeviceClaims(self.store, registry=self.registry)
        claims.claim(self.agent, "phone", "serial", label="Pixel", environment=self.environment)
        claims.release(self.agent, "phone", "serial", environment=self.environment)
        self.assert_pending()
        self.cli_ensure.assert_not_called()
        self.device_ensure.assert_not_called()
