"""Device ownership, refusal boundaries and read-only resource exports."""
import ham_test_support  # Protect the live ledger before product imports.
import contextlib
import copy
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest
from unittest import mock

import agent_skill_cli
from ledger_devices import DeviceClaims, device_rows, device_operation
from ledger_status import AgentRegistry
from ledger_store import LedgerError, LedgerStore, SCHEMA_VERSION
from ledger_views import status_board


class DeviceClaimsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.environment = {**os.environ, "HERDR_AGENT_LEDGER_DIR": str(self.root / "ledger"),
                            "HERDR_AGENT_LEDGER_AUTOSTART": "0", "HERDR_TERMINAL_ID": "a"}
        self.store = LedgerStore(environment=self.environment, canonical_name="alpha")
        self.registry = AgentRegistry(self.store)
        self.snapshot = {"panes": [], "agents": []}
        for terminal in ("a", "b"):
            pane = {"terminal_id": terminal, "pane_id": "p-" + terminal, "workspace_id": "w"}
            self.snapshot["panes"].append(pane)
            self.snapshot["agents"].append({**pane, "agent": "omp", "name": "agent-" + terminal,
                "agent_session": {"value": "session-" + terminal}, "agent_status": "idle", "cwd": str(self.root)})
        self.registry.reconcile(self.snapshot, refresh_display=False)
        self.project = mock.patch.object(AgentRegistry, "_refresh_display", return_value={"updated": [], "failed": []}).start()
        self.addCleanup(mock.patch.stopall)
        self.runner = mock.Mock(return_value=subprocess.CompletedProcess([], 0, "List of devices attached\nserial device\noffline offline\n", ""))
        self.claims = DeviceClaims(self.store, registry=self.registry, runner=self.runner)

    def agent(self, terminal="a"):
        return next(row for row in self.registry.list() if row["terminal_id"] == terminal)

    def claim(self, terminal="a", device="serial", kind="phone", **kwargs):
        return self.claims.claim(self.agent(terminal), kind, device, label=kwargs.pop("label", "Pixel"), **kwargs)

    def args(self, *args):
        return agent_skill_cli.parse_cli_arguments(["device", *args])

    def test_conflict_force_reclaim_and_release_scope(self):
        first = self.claim(now=10)
        self.assertIsNone(first["replaced"])
        again = self.claim(label="New label", now=20)
        self.assertEqual((again["device"]["label"], again["device"]["claimed_at"]), ("New label", 10))
        with self.assertRaises(LedgerError) as raised:
            self.claim("b")
        self.assertEqual(raised.exception.code, "device_claimed")
        self.assertEqual(raised.exception.holder["pane_id"], "p-a")
        self.assertEqual(self.claims.release(self.agent("b"))["released"], [])
        forced = self.claim("b", force=True, now=30)
        self.assertEqual(forced["replaced"]["holder"]["agent_id"], self.agent()["agent_id"])
        self.assertFalse(forced["replaced_stale"])
        self.project.assert_called_with({"p-a", "p-b"}, environment=None)
        self.claim("b", device="5554", kind="emu", label="Android")
        released = self.claims.release(self.agent("b"), "emu", "5554")["released"]
        self.assertEqual([row["device_id"] for row in released], ["emulator-5554"])
        self.assertEqual([row["device_id"] for row in self.claims.release(self.agent("b"))["released"]], ["serial"])

    def test_stale_holder_replaced_and_other_stale_claims_pruned(self):
        self.claim("b")
        self.claim("b", device="old")
        with self.store.transaction() as connection:
            connection.execute("UPDATE agents SET active=0 WHERE terminal_id='b'")
        result = self.claim(now=40)
        self.assertTrue(result["replaced_stale"])
        self.assertTrue(result["replaced"]["stale"])
        self.assertEqual([row["device_id"] for row in self.claims.list()["devices"]], ["serial"])

    def test_generation_change_refuses_mutation_and_registry_prunes(self):
        previous = self.agent()
        self.claim()
        changed = copy.deepcopy(self.snapshot)
        changed["agents"][0]["agent_session"]["value"] = "replacement"
        self.registry.reconcile(changed, refresh_display=False)
        self.assertEqual(self.claims.list()["devices"], [])
        for action in (lambda: self.claims.claim(previous, "phone", "serial", label="Pixel"),
                       lambda: self.claims.release(previous)):
            with self.assertRaises(LedgerError) as raised:
                action()
            self.assertEqual(raised.exception.code, "stale_session")

    def test_cli_refresh_preserves_stale_target_until_claim_reports_replacement(self):
        self.claim("b")
        changed = copy.deepcopy(self.snapshot)
        changed["agents"] = changed["agents"][:1]
        with mock.patch("ledger_status.live_snapshot", return_value=changed):
            result = device_operation(self.args("claim", "phone", "serial", "--label", "Pixel"), self.environment)
        self.assertTrue(result["replaced_stale"])

    def test_unresolved_caller_and_failed_snapshot_never_replace_holder(self):
        self.claim("b")
        inherited = {**self.environment, "HERDR_PANE_ID": "p-b"}
        inherited.pop("HERDR_TERMINAL_ID")
        with mock.patch("ledger_status.live_snapshot", return_value=self.snapshot), \
             mock.patch("ledger_status.process_ancestors", return_value=set()), \
             mock.patch("ledger_status.pane_shell_pid", return_value=999):
            with self.assertRaises(LedgerError) as raised:
                device_operation(self.args("claim", "phone", "serial", "--force"), inherited)
        self.assertEqual(raised.exception.code, "sender_unresolved")
        with mock.patch("ledger_status.live_snapshot", side_effect=LedgerError("host_unavailable", "offline")):
            with self.assertRaises(LedgerError) as raised:
                device_operation(self.args("claim", "phone", "serial", "--force"), self.environment)
        self.assertEqual(raised.exception.code, "host_unavailable")
        self.assertEqual(self.claims.list()["devices"][0]["holder"]["terminal_id"], "b")

    def test_default_labels_normalization_and_bounded_adb(self):
        self.runner.return_value = subprocess.CompletedProcess([], 0, "Pixel 9\n", "")
        result = self.claims.claim(self.agent(), "phone", "serial")
        self.assertEqual(result["device"]["label"], "Pixel 9")
        self.assertEqual(self.runner.call_args.kwargs["timeout"], 10)
        result = self.claims.claim(self.agent(), "emu", "5554")
        self.assertEqual((result["device"]["device_id"], result["device"]["label"]), ("emulator-5554", "5554"))
        self.runner.side_effect = FileNotFoundError()
        self.assertEqual(self.claims.claim(self.agent(), "phone", "other")["device"]["label"], "other")

    def test_attachment_states_and_read_only_listing(self):
        self.claim()
        self.claim(device="offline")
        ios = "00008110-001234567890ABCD"
        self.claim(device=ios)
        with self.store.transaction() as connection:
            connection.execute("UPDATE agents SET active=0 WHERE terminal_id='a'")
        with self.store.read() as connection:
            before = list(connection.iterdump())
        rows = {row["device_id"]: row for row in self.claims.list()["devices"]}
        self.assertTrue(rows["serial"]["attached"])
        self.assertFalse(rows["offline"]["attached"])
        self.assertIsNone(rows[ios]["attached"])
        self.assertTrue(rows["serial"]["stale"])
        self.assertEqual(self.runner.call_args.kwargs["timeout"], 2)
        self.runner.side_effect = FileNotFoundError()
        self.assertTrue(all(row["attached"] is None for row in self.claims.list()["devices"]))
        with self.store.read() as connection:
            self.assertEqual(list(connection.iterdump()), before)

    def test_list_with_absent_home_never_makes_directories(self):
        missing = self.root / "absent"
        environment = {"HOME": str(missing), "HERDR_AGENT_LEDGER_DIR": str(missing / "ledger")}
        with mock.patch.object(AgentRegistry, "refresh", side_effect=AssertionError("mutation")):
            self.assertEqual(device_operation(self.args("list"), environment), {"devices": []})
        self.assertFalse(missing.exists())

    def test_list_during_first_bootstrap_returns_empty_without_mutation(self):
        directory = self.root / "bootstrapping"
        directory.mkdir()
        path = directory / "ledger.sqlite3"
        environment = {**self.environment, "HERDR_AGENT_LEDGER_DIR": str(directory)}
        with sqlite3.connect(path) as connection:
            pass
        for metadata_table in (False, True):
            with self.subTest(metadata_table=metadata_table):
                if metadata_table:
                    with sqlite3.connect(path) as connection:
                        connection.execute("CREATE TABLE ledger_meta(key TEXT PRIMARY KEY,value TEXT)")
                before = path.read_bytes()
                with mock.patch.object(AgentRegistry, "refresh", side_effect=AssertionError("mutation")), \
                     mock.patch("ledger_devices.subprocess.run", side_effect=AssertionError("device probe")):
                    self.assertEqual(device_operation(self.args("list"), environment), {"devices": []})
                self.assertEqual(path.read_bytes(), before)
                self.assertEqual({item.name for item in directory.iterdir()}, {"ledger.sqlite3"})

    def test_list_does_not_hide_other_ledger_errors(self):
        with mock.patch.object(LedgerStore, "open_readonly",
                               side_effect=LedgerError("invalid_metadata", "broken")):
            with self.assertRaises(LedgerError) as raised:
                device_operation(self.args("list"), self.environment)
        self.assertEqual(raised.exception.code, "invalid_metadata")

    def test_cli_json_conflict_has_holder_and_list_does_not_refresh(self):
        self.claim("b")
        output = io.StringIO()
        with mock.patch("ledger_status.live_snapshot", return_value=self.snapshot), contextlib.redirect_stdout(output):
            code = agent_skill_cli.main(["device", "claim", "phone", "serial", "--label", "Pixel"], self.environment)
        payload = json.loads(output.getvalue())
        self.assertEqual(code, 1)
        self.assertEqual(payload["error"]["holder"]["alias"], "agent-b")
        with mock.patch.object(AgentRegistry, "refresh", side_effect=AssertionError("mutation")), \
             mock.patch("ledger_devices.subprocess.run", return_value=subprocess.CompletedProcess([], 0, "", "")):
            self.assertEqual(len(device_operation(self.args("list"), self.environment)["devices"]), 1)

    def test_v5_migration_preserves_agents_status_and_claim_foreign_key(self):
        self.registry.set_status(self.agent(), phase="review", summary="preserve")
        with self.store.transaction() as connection:
            connection.execute("DROP TABLE device_claims")
            connection.execute("UPDATE ledger_meta SET value='5' WHERE key='schema_version'")
        migrated = LedgerStore(environment=self.environment)
        with migrated.read() as connection:
            self.assertEqual(connection.execute("SELECT value FROM ledger_meta WHERE key='schema_version'").fetchone()[0], str(SCHEMA_VERSION))
            self.assertEqual(connection.execute("SELECT summary FROM agent_status WHERE agent_id=?", (self.agent()["agent_id"],)).fetchone()[0], "preserve")
            self.assertEqual(device_rows(connection), [])
            self.assertEqual(connection.execute("PRAGMA foreign_key_list(device_claims)").fetchone()[2], "agents")

    def test_board_resources_are_readonly_and_old_peers_remain_unknown(self):
        self.claim()
        self.claim("b", device="stale")
        with self.store.transaction() as connection:
            connection.execute("UPDATE agents SET active=0 WHERE terminal_id='b'")
            connection.execute("INSERT INTO peers VALUES ('beta','peer-uuid','beta','/peer')")
        listener = {"pid": 100, "port": 5180, "bind": "127.0.0.1", "process": "node", "argv": "node dev", "cwd": str(self.root), "pane_id": "p-a", "plugin_id": None, "started_at": 1}
        scanned = {"listeners": [listener], "serve": [], "error": None}
        class Transport:
            def call(inner, peer, command, payload):
                self.assertFalse(payload["refresh"])
                return {"host": {"canonical_name": "beta", "uuid": "peer-uuid"}, "agents": []}
        with self.store.read() as connection:
            before = list(connection.iterdump())
        with mock.patch("resource_scan.scan_host_resources", return_value=scanned), \
             mock.patch.object(AgentRegistry, "refresh", side_effect=AssertionError("mutation")):
            board = status_board(self.store, transport=Transport())
        agent = board["agents"][0]
        self.assertEqual([row["device_id"] for row in agent["devices"]], ["serial"])
        self.assertEqual(agent["servers"][0]["port"], 5180)
        self.assertEqual(len(board["hosts"][0]["resources"]["stale_devices"]), 1)
        self.assertNotIn("resources", board["hosts"][1])
        with self.store.read() as connection:
            self.assertEqual(list(connection.iterdump()), before)


if __name__ == "__main__":
    unittest.main()
