"""Receiving dependency notifications use private ledger state and mocked Herdr."""
import ham_test_support  # Protect the live ledger before product imports.

import argparse
import tempfile
import unittest
from pathlib import Path
from unittest import mock

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
        self.notify = mock.patch.object(notifications.agent_messenger, "show_notification", return_value=True)
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
        self.summary_mock.assert_called_once_with(self.environment)

    def test_inactive_local_and_codex_only_never_notify(self):
        with self.store.transaction() as connection:
            connection.execute("UPDATE agents SET active=0 WHERE client IN ('omp','claude')")
        self.health(omp=False, claude=False)
        self.run_notice()
        self.notify_mock.assert_not_called()
        self.summary_mock.assert_not_called()

    def test_notification_failure_is_durable_and_deduplicated(self):
        self.notify_mock.return_value = False
        self.run_notice()
        self.run_notice()
        self.assertEqual(self.notify_mock.call_count, 1)
        failures = self.store.doctor()["notification_failures"]
        self.assertEqual(len(failures), 1)
        self.assertIn("not accepted", failures[0]["detail"])
        self.assertEqual(self.store.notification_errors[0]["code"], "notification_failed")

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


if __name__ == "__main__":
    unittest.main()
