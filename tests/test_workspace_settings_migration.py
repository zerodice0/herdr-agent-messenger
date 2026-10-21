"""Legacy replicated role settings migrate only onto their authoritative host."""
import tests.ham_test_support as ham_test_support  # Protect the live ledger before product imports.

import json
from pathlib import Path
import tempfile
import unittest

from ham.ledger.settings import RepoSettings
from ham.ledger.status import AgentRegistry
from ham.ledger.store import LedgerError, LedgerStore, SCHEMA_VERSION, json_text


class WorkspaceSettingsMigrationTest(unittest.TestCase):
    def test_readonly_v6_requires_explicit_writable_upgrade_without_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            store = LedgerStore(Path(directory) / "ledger", canonical_name="alpha")
            with store.transaction() as connection:
                connection.execute("DROP TABLE workspace_settings")
                connection.execute("UPDATE ledger_meta SET value='6' WHERE key='schema_version'")
            with store.read() as connection:
                before = list(connection.iterdump())
            with self.assertRaises(LedgerError) as failure:
                LedgerStore.open_readonly({"HERDR_AGENT_LEDGER_DIR": str(store.directory)})
            self.assertEqual(failure.exception.code, "schema_upgrade_required")
            self.assertIn("ledger reconcile", failure.exception.message)
            with store.read() as connection:
                self.assertEqual(list(connection.iterdump()), before)
            LedgerStore(store.directory)
            reopened = LedgerStore.open_readonly({"HERDR_AGENT_LEDGER_DIR": str(store.directory)})
            self.assertIsNotNone(reopened)
            with reopened.read() as connection:
                self.assertEqual(connection.execute("SELECT count(*) FROM workspace_settings").fetchone()[0], 0)

    def test_readonly_newer_schema_requires_matching_version_not_reconcile(self):
        with tempfile.TemporaryDirectory() as directory:
            store = LedgerStore(Path(directory) / "ledger", canonical_name="alpha")
            with store.transaction() as connection:
                connection.execute("UPDATE ledger_meta SET value=? WHERE key='schema_version'",
                                   (str(SCHEMA_VERSION + 1),))
            with self.assertRaises(LedgerError) as failure:
                LedgerStore.open_readonly({"HERDR_AGENT_LEDGER_DIR": str(store.directory)})
            self.assertEqual(failure.exception.code, "unsupported_schema")
            self.assertNotIn("reconcile", failure.exception.message)

    def test_v6_roles_group_local_active_targets_and_retire_legacy_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = LedgerStore(root / "ledger", canonical_name="alpha")
            panes = [{"terminal_id": terminal, "pane_id": "pane-" + terminal, "workspace_id": workspace}
                     for terminal, workspace in (("a", "w1"), ("b", "w1"), ("c", "w2"), ("inactive", "w2"))]
            AgentRegistry(store).reconcile({"panes": panes, "agents": [
                {**pane, "agent": "omp", "name": pane["terminal_id"], "agent_status": "idle",
                 "agent_session": {"value": "session-" + pane["terminal_id"]}, "cwd": directory}
                for pane in panes]}, refresh_display=False)
            repo_id = AgentRegistry(store).list()[0]["repo_id"]
            target = lambda terminal, host="alpha": {"host": host, "terminal_id": terminal}
            with store.transaction() as connection:
                original_agents = [dict(row) for row in connection.execute("SELECT * FROM agents")]
                connection.execute("UPDATE agents SET active=0 WHERE terminal_id='inactive'")
                connection.execute("DROP TABLE workspace_settings")
                connection.execute("CREATE TABLE repo_members (repo_id TEXT,host TEXT,terminal_id TEXT)")
                connection.execute("CREATE TABLE setting_conditions (repo_id TEXT,field TEXT,version INTEGER,origin_host TEXT,expected_main TEXT)")
                for field, value in (("main", target("a")), ("members", [target("b"), target("c"),
                        target("inactive"), target("unknown"), target("remote", "beta")]),
                        ("computed_main", {"target": target("a"), "home_stamp": {}})):
                    connection.execute("INSERT INTO repo_settings VALUES (?,?,?,?,?,?,?)",
                                       (repo_id, field, json_text(value), 4, "alpha", "user", 10))
                connection.execute("INSERT INTO repo_members VALUES (?,?,?)", (repo_id, "alpha", "b"))
                connection.execute("INSERT INTO setting_conditions VALUES (?,?,?,?,?)", (repo_id, "main", 4, "alpha", "{}"))
                connection.execute("UPDATE ledger_meta SET value='6' WHERE key='schema_version'")
            migrated = LedgerStore(root / "ledger", canonical_name="alpha")
            settings = RepoSettings(migrated)
            self.assertEqual(settings.show(repo_id, "alpha", "w1")["settings"]["main"]["value"], target("a"))
            self.assertEqual(settings.show(repo_id, "alpha", "w1")["members"], [target("b")])
            self.assertEqual(settings.show(repo_id, "alpha", "w2")["members"], [target("c")])
            self.assertEqual(settings.show(repo_id, "beta", "w1")["members"], [])
            with migrated.read() as connection:
                self.assertEqual(connection.execute("SELECT value FROM ledger_meta WHERE key='schema_version'").fetchone()[0], str(SCHEMA_VERSION))
                self.assertEqual(set(settings.fields(connection, repo_id)), {"home", "mode"})
                self.assertEqual(connection.execute("SELECT count(*) FROM workspace_settings").fetchone()[0], 3)
                self.assertFalse(connection.execute("SELECT 1 FROM sqlite_master WHERE name IN ('repo_members','setting_conditions')").fetchall())
                dropped = json.loads(connection.execute("SELECT value FROM ledger_meta WHERE key='workspace_settings_migration_dropped'").fetchone()[0])
                self.assertEqual({entry["target"]["terminal_id"] for entry in dropped}, {"inactive", "unknown"})
                agents = [dict(row) for row in connection.execute("SELECT * FROM agents")]
                self.assertEqual(len(agents), len(original_agents))
                self.assertEqual({row["agent_id"] for row in agents}, {row["agent_id"] for row in original_agents})
            reopened = LedgerStore(root / "ledger")
            self.assertEqual(RepoSettings(reopened).show(repo_id, "alpha", "w1")["members"], [target("b")])

    def test_each_host_migrates_only_its_own_target(self):
        with tempfile.TemporaryDirectory() as directory:
            for host in ("alpha", "beta"):
                with self.subTest(host=host):
                    store = LedgerStore(Path(directory) / host, canonical_name=host)
                    pane = {"terminal_id": "a", "pane_id": "p", "workspace_id": "same"}
                    registry = AgentRegistry(store)
                    registry.reconcile({"panes": [pane], "agents": [{**pane, "agent": "omp", "name": "agent",
                        "agent_session": {"value": "session"}, "agent_status": "idle", "cwd": directory}]}, refresh_display=False)
                    repo_id = registry.list()[0]["repo_id"]
                    with store.transaction() as connection:
                        connection.execute("DROP TABLE workspace_settings")
                        connection.execute("INSERT INTO repo_settings VALUES (?,?,?,?,?,?,?)", (repo_id, "members",
                            json_text([{"host": "alpha", "terminal_id": "a"}, {"host": "beta", "terminal_id": "a"}]),
                            2, "alpha", "user", 1))
                        connection.execute("UPDATE ledger_meta SET value='6' WHERE key='schema_version'")
                    migrated = LedgerStore(Path(directory) / host)
                    with migrated.read() as connection:
                        rows = connection.execute("SELECT host,workspace_id,value FROM workspace_settings").fetchall()
                        self.assertEqual(len(rows), 1)
                        self.assertEqual((rows[0]["host"], rows[0]["workspace_id"]), (host, "same"))
                        self.assertEqual(json.loads(rows[0]["value"]), [{"host": host, "terminal_id": "a"}])
