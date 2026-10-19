"""Receiving dependency notifications use private ledger state and mocked Herdr."""
import ham_test_support  # Protect the live ledger before product imports.

import argparse
import builtins
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import agent_messenger
import ledger_cli
from ledger_status import AgentRegistry
from ledger_store import LedgerStore
import receiver_notifications as notifications


class ReceiverNotificationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.environment = {"HOME": str(self.root), "HERDR_AGENT_LEDGER_DIR": str(self.root / "ledger"),
                            "HERDR_AGENT_LEDGER_AUTOSTART": "0"}
        self.store = LedgerStore(environment=self.environment, canonical_name="alpha")
        panes = [{"terminal_id": client, "pane_id": "w1:" + client, "workspace_id": "w1"}
                 for client in ("omp", "claude", "codex")]
        snapshot = {"panes": panes, "agents": [
            {**pane, "agent": pane["terminal_id"], "agent_status": "idle", "name": pane["terminal_id"],
             "agent_session": {"value": "session-" + pane["terminal_id"]}, "cwd": str(self.root)}
            for pane in panes]}
        AgentRegistry(self.store).reconcile(snapshot, refresh_display=False)
        self.summary = mock.patch.object(notifications.runtime_installer, "summary")
        self.summary_mock = self.summary.start()
        self.addCleanup(self.summary.stop)
        self.notify = mock.patch.object(agent_messenger, "show_notification", return_value=True)
        self.notify_mock = self.notify.start()
        self.addCleanup(self.notify.stop)
        self.health(omp=False, claude=True)

    def health(self, *, omp=True, claude=True, error=None):
        self.summary_mock.return_value = {"clients": [
            {"client": client, "installed": installed, "error": error if client == "omp" else None}
            for client, installed in (("omp", omp), ("claude", claude))]}

    def run_notice(self):
        notifications.notify_missing_runtime(self.store, self.environment)

    def test_dedup_survives_reopening_store_and_set_changes_renotify(self):
        self.run_notice()
        self.assertEqual(self.notify_mock.call_count, 1)
        self.assertIn("omp (not_installed)", self.notify_mock.call_args.args[0])
        self.assertEqual(self.notify_mock.call_args.args[1], self.environment)
        notifications.notify_missing_runtime(LedgerStore(environment=self.environment), self.environment)
        self.assertEqual(self.notify_mock.call_count, 1)
        self.health(omp=False, claude=False)
        self.run_notice()
        self.assertEqual(self.notify_mock.call_count, 2)
        self.assertIn("claude (not_installed)", self.notify_mock.call_args.args[0])
        self.health(omp=False, claude=False, error={"code": "foreign_runtime", "message": "Foreign file"})
        self.run_notice()
        self.assertEqual(self.notify_mock.call_count, 3)
        self.assertIn("foreign_runtime", self.notify_mock.call_args.args[0])
        self.health()
        self.run_notice()
        self.assertEqual(self.notify_mock.call_count, 3)
        self.health(omp=False)
        self.run_notice()
        self.assertEqual(self.notify_mock.call_count, 4)

    def test_installed_runtime_with_unregistered_sessions_does_not_notify(self):
        self.health()
        self.run_notice()
        self.notify_mock.assert_not_called()
        self.assertEqual(self.summary_mock.call_args_list,
                         [mock.call(self.environment), mock.call(self.environment)])

    def test_inactive_local_and_codex_only_never_notify(self):
        with self.store.transaction() as connection:
            connection.execute("UPDATE agents SET active=0 WHERE client IN ('omp','claude')")
        self.health(omp=False, claude=False)
        self.run_notice()
        self.notify_mock.assert_not_called()
        self.summary_mock.assert_not_called()

    def test_registered_receivers_skip_default_destination_inspection(self):
        with self.store.transaction() as connection:
            connection.execute("UPDATE agents SET extension_present=1")
        self.health(omp=False, claude=False, error={"code": "foreign_runtime"})
        self.run_notice()
        self.summary_mock.assert_not_called()
        self.notify_mock.assert_not_called()

    def test_only_local_absent_receiver_clients_can_trigger_notification(self):
        with self.store.transaction() as connection:
            connection.execute("UPDATE agents SET extension_present=1 WHERE client='omp'")
            connection.execute("UPDATE agents SET host='remote' WHERE client='claude'")
        self.health(omp=False, claude=False)
        self.run_notice()
        self.summary_mock.assert_not_called()
        self.notify_mock.assert_not_called()
        with self.store.transaction() as connection:
            connection.execute("UPDATE agents SET host=? WHERE client='claude'", (self.store.host,))
        self.run_notice()
        self.assertEqual(self.notify_mock.call_count, 1)
        self.assertIn("claude (not_installed)", self.notify_mock.call_args.args[0])
        self.assertNotIn("omp (", self.notify_mock.call_args.args[0])

    def test_unchanged_state_uses_only_plain_reads_and_no_messenger_import(self):
        for installed in (False, True):
            with self.subTest(installed=installed):
                self.health(omp=installed)
                self.run_notice()
                with mock.patch.object(self.store, "transaction",
                                       side_effect=AssertionError("Unexpected write transaction")), \
                        mock.patch.object(builtins, "__import__", wraps=builtins.__import__) as imports:
                    self.run_notice()
                self.assertFalse(any(call.args[0] == "agent_messenger" for call in imports.call_args_list))
        self.assertEqual(self.store.notification_errors, [])

    def test_compare_and_set_refreshes_active_receivers_after_an_overlapping_claim(self):
        with self.store.transaction() as connection:
            connection.execute("UPDATE agents SET active=0 WHERE client IN ('omp','claude')")
        other = LedgerStore(environment=self.environment)
        transaction = self.store.transaction

        def overlapping_claim(*args, **kwargs):
            with other.transaction() as connection:
                connection.execute("UPDATE agents SET active=1 WHERE client='omp'")
            notifications.notify_missing_runtime(other, self.environment)
            return transaction(*args, **kwargs)

        with mock.patch.object(self.store, "transaction", side_effect=overlapping_claim):
            self.run_notice()
        self.run_notice()
        self.assertEqual(self.notify_mock.call_count, 1)
        with self.store.read() as connection:
            state = connection.execute("SELECT value FROM ledger_meta WHERE key=?",
                                       (notifications.META_KEY,)).fetchone()[0]
        self.assertEqual(state, '[["omp","not_installed"]]')

    def test_notification_failure_is_durable_and_deduplicated(self):
        self.notify_mock.return_value = False
        self.run_notice()
        self.run_notice()
        self.assertEqual(self.notify_mock.call_count, 1)
        failures = self.store.doctor()["notification_failures"]
        self.assertEqual(len(failures), 1)
        self.assertIn("not accepted", failures[0]["detail"])
        self.assertEqual(self.store.notification_errors[0]["code"], "notification_failed")
        self.assertEqual(self.store.notification_errors[0]["recovery"], "receiver_runtime_set_change")
        self.health()
        self.run_notice()
        self.health(omp=False)
        self.run_notice()
        self.assertEqual(self.notify_mock.call_count, 2)

    def test_notification_exception_and_failed_diagnostic_are_swallowed(self):
        self.notify_mock.side_effect = OSError("Herdr offline")
        with mock.patch.object(self.store, "note_notification_failure", side_effect=OSError("Disk offline")):
            self.run_notice()
        self.assertIn("Herdr offline", self.store.notification_errors[0]["message"])

    def test_reconcile_and_event_notify_only_after_refresh(self):
        for command in ("reconcile", "event"):
            events = []
            with mock.patch.object(AgentRegistry, "refresh", side_effect=lambda env: (
                    events.append("refresh") or {}, {"refreshed": True})), \
                    mock.patch.object(notifications, "notify_missing_runtime",
                                      side_effect=lambda store, env: events.append("notify")):
                result = ledger_cli._dispatch(argparse.Namespace(command="ledger", ledger_command=command),
                                              self.environment, self.store)
            self.assertEqual(events, ["refresh", "notify"])
            self.assertEqual(result, {"refreshed": True})

    def test_reconcile_and_event_survive_hook_and_import_failures(self):
        original_import = builtins.__import__

        def failed_import(name, *args, **kwargs):
            if name == "receiver_notifications":
                raise ImportError("Notification helper unavailable")
            return original_import(name, *args, **kwargs)

        for command in ("reconcile", "event"):
            for failure in ("import", "hook"):
                with self.subTest(command=command, failure=failure):
                    self.store.notification_errors.clear()
                    patcher = (mock.patch.object(builtins, "__import__", side_effect=failed_import)
                               if failure == "import" else
                               mock.patch.object(notifications, "notify_missing_runtime",
                                                 side_effect=RuntimeError("Notification helper failed")))
                    with mock.patch.object(AgentRegistry, "refresh", return_value=({}, {"refreshed": True})), patcher:
                        result = ledger_cli._dispatch(argparse.Namespace(command="ledger", ledger_command=command),
                                                      self.environment, self.store)
                    self.assertTrue(result["refreshed"])
                    self.assertEqual(result["warnings"][0]["code"], "notification_failed")
                    self.assertIn("Notification helper", result["warnings"][0]["message"])
        self.assertTrue(self.store.doctor()["notification_failures"])



if __name__ == "__main__":
    unittest.main()
