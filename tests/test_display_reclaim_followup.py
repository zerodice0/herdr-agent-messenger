"""Explicit cross-repository reclaim and bounded display lock slots."""
import tests.ham_test_support as ham_test_support  # Guard live state before product imports.

import copy
import unittest
from unittest import mock

from ham.ledger.display import LedgerDisplay
from ham.ledger.store import LedgerError, LedgerStore
from tests import test_display_restore as restore_tests


class DisplayReclaimFollowupTest(unittest.TestCase):
    setUp = restore_tests.DisplayRestoreTest.setUp
    new_registry = restore_tests.DisplayRestoreTest.new_registry
    snapshot = restore_tests.DisplayRestoreTest.snapshot
    entries = restore_tests.DisplayRestoreTest.entries
    agent = restore_tests.DisplayRestoreTest.agent
    writes = restore_tests.DisplayRestoreTest.writes
    initialize = restore_tests.DisplayRestoreTest.initialize
    restored = restore_tests.DisplayRestoreTest.restored
    lost_ownership = restore_tests.DisplayRestoreTest.lost_ownership
    insert_agent = restore_tests.DisplayRestoreTest.insert_agent
    dump = restore_tests.DisplayRestoreTest.dump

    def repo_mismatch(self):
        old, owned = self.restored()
        with self.store.transaction() as connection:
            connection.execute("UPDATE agents SET last_ham_label=NULL WHERE active=1")
            connection.execute("UPDATE agents SET repo_id=? WHERE agent_id=?",
                               ("historical-repo", old["agent_id"]))
        self.herdr.commands.clear()
        return old, owned

    def test_default_readonly_dry_run_lists_explicit_only_without_locks_or_bootstrap(self):
        old, owned = self.repo_mismatch()
        before = self.dump()
        files = set(self.store.directory.iterdir())
        with mock.patch("ham.ledger.store.ledger_directory", return_value=self.store.directory), \
                mock.patch.object(LedgerStore, "__init__", side_effect=AssertionError("bootstrap forbidden")), \
                mock.patch.object(LedgerStore, "lock", side_effect=AssertionError("lock forbidden")):
            readonly = LedgerStore.open_readonly()
            self.assertIsNotNone(readonly)
            result = LedgerDisplay(readonly, runner=self.herdr).reclaim(snapshot=self.snapshot())
        self.assertFalse(result["applied"])
        self.assertEqual(len(result["panes"]), 1)
        entry = result["panes"][0]
        self.assertEqual(entry["status"], "explicit_only")
        self.assertEqual(entry["owner_agent_id"], old["agent_id"])
        self.assertEqual(entry["prior_repo_id"], "historical-repo")
        self.assertEqual(entry["current_repo_id"], self.agent()["repo_id"])
        self.assertIn("explicit --pane", entry["reason"])
        self.assertEqual(entry["before"], {"border": owned, "last_ham_label": None})
        self.assertEqual(result["updated"], [])
        self.assertEqual(result["failed"], [])
        self.assertEqual(result["skipped"], [])
        self.assertEqual(self.dump(), before)
        self.assertEqual(set(self.store.directory.iterdir()), files)
        self.assertEqual(self.herdr.commands, [])

    def test_default_apply_leaves_explicit_only_listed_and_unlocked(self):
        self.repo_mismatch()
        before = self.dump()
        display = LedgerDisplay(self.store, runner=self.herdr)
        with mock.patch.object(self.store, "display_lock", side_effect=AssertionError("lock forbidden")):
            result = display.reclaim(apply=True, snapshot=self.snapshot())
        self.assertTrue(result["applied"])
        self.assertEqual(result["panes"][0]["status"], "explicit_only")
        self.assertEqual(result["panes"][0]["after"], result["panes"][0]["before"])
        self.assertEqual(result["updated"], [])
        self.assertEqual(result["failed"], [])
        self.assertEqual(result["skipped"], ["pane"])
        self.assertEqual(self.dump(), before)
        self.assertEqual(self.herdr.commands, [])

    def test_default_apply_updates_normal_candidate_beside_explicit_only(self):
        old, owned = self.repo_mismatch()
        with self.store.read() as connection:
            retired = dict(connection.execute("SELECT * FROM agents WHERE agent_id=?",
                                              (old["agent_id"],)).fetchone())
            current = dict(connection.execute("SELECT * FROM agents WHERE active=1").fetchone())
        normal = {**current, "agent_id": "normal-current", "pane_id": "normal-pane",
                  "terminal_id": "normal-terminal"}
        prior = {**retired, "agent_id": "normal-prior", "pane_id": "normal-pane",
                 "terminal_id": "normal-gone-terminal", "repo_id": current["repo_id"]}
        self.insert_agent(prior)
        self.insert_agent(normal)
        with self.store.transaction() as connection:
            connection.execute(
                "INSERT INTO agent_status(agent_id,turn_state,phase,summary,updated_at) VALUES (?,?,?,?,?)",
                (normal["agent_id"], "idle", "", "", 300),
            )
        self.herdr.labels["normal-pane"] = owned
        self.herdr.terminals["normal-pane"] = normal["terminal_id"]
        self.herdr.metadata["normal-pane"] = copy.deepcopy(self.herdr.metadata["pane"])
        snapshot = self.snapshot()
        snapshot["panes"].append({"pane_id": "normal-pane", "terminal_id": normal["terminal_id"],
                                  "workspace_id": normal["workspace_id"], "label": owned})
        result = LedgerDisplay(self.store, runner=self.herdr).reclaim(apply=True, snapshot=snapshot)
        self.assertEqual(result["updated"], ["normal-pane"])
        self.assertEqual(result["skipped"], ["pane"])
        self.assertEqual(result["failed"], [])
        entries = {entry["pane_id"]: entry for entry in result["panes"]}
        self.assertEqual(entries["pane"]["status"], "explicit_only")
        self.assertEqual(entries["normal-pane"]["status"], "updated")
        self.assertEqual(self.herdr.labels["pane"], owned)
        self.assertEqual(self.herdr.labels["normal-pane"], "🟧 orange-rabbit")
        with self.store.read() as connection:
            self.assertIsNone(connection.execute("SELECT last_ham_label FROM agents WHERE agent_id=?",
                                                (current["agent_id"],)).fetchone()[0])

    def test_repeatable_selected_apply_recovers_repo_mismatch(self):
        self.repo_mismatch()
        display = LedgerDisplay(self.store, runner=self.herdr)
        selected = ["missing-pane", "pane", "pane"]
        dry = display.reclaim(snapshot=self.snapshot(), pane_ids=selected)
        self.assertEqual(dry["panes"][0]["status"], "explicit_only")
        result = display.reclaim(apply=True, snapshot=self.snapshot(), pane_ids=selected)
        self.assertEqual(result["updated"], ["pane"])
        self.assertEqual(result["failed"], [])
        self.assertEqual(result["skipped"], [])
        self.assertEqual(result["panes"][0]["status"], "updated")
        self.assertEqual(self.agent()["last_ham_label"], "🟧 orange-rabbit")
        self.assertEqual(self.herdr.labels["pane"], "🟧 orange-rabbit")
        self.herdr.commands.clear()
        self.assertEqual(display.reclaim(apply=True, snapshot=self.snapshot(), pane_ids=selected)["panes"], [])
        self.assertEqual(self.herdr.commands, [])

    def test_same_terminal_older_generation_repo_mismatch_remains_normal(self):
        old, _ = self.lost_ownership()
        with self.store.transaction() as connection:
            connection.execute("UPDATE agents SET repo_id=?,workspace_id=?,client=? WHERE agent_id=?",
                               ("historical-repo", "historical-workspace", "historical-client",
                                old["agent_id"]))
        display = LedgerDisplay(self.store, runner=self.herdr)
        dry = display.reclaim(snapshot=self.snapshot())
        self.assertEqual(dry["panes"][0]["status"], "candidate")
        self.assertNotIn("reason", dry["panes"][0])
        result = display.reclaim(apply=True, snapshot=self.snapshot())
        self.assertEqual(result["updated"], ["pane"])
        self.assertEqual(result["skipped"], [])
        self.assertEqual(self.agent()["last_ham_label"], "🟧 orange-rabbit")

    def test_repo_mismatch_discovery_still_requires_exact_scope_and_label(self):
        old, owned = self.repo_mismatch()
        display = LedgerDisplay(self.store, runner=self.herdr)
        for column, value in (("host", "different-host"), ("workspace_id", "different-workspace"),
                              ("pane_id", "different-pane"), ("client", "different-client"),
                              ("last_ham_label", owned + " suffix"), ("active", 1)):
            with self.subTest(column=column):
                with self.store.transaction() as connection:
                    original = connection.execute(f"SELECT {column} FROM agents WHERE agent_id=?",
                                                  (old["agent_id"],)).fetchone()[0]
                    connection.execute(f"UPDATE agents SET {column}=? WHERE agent_id=?",
                                       (value, old["agent_id"]))
                try:
                    self.assertEqual(display.reclaim(snapshot=self.snapshot())["panes"], [])
                    self.assertEqual(display.reclaim(apply=True, snapshot=self.snapshot(),
                                                     pane_ids=["pane"])["panes"], [])
                finally:
                    with self.store.transaction() as connection:
                        connection.execute(f"UPDATE agents SET {column}=? WHERE agent_id=?",
                                           (original, old["agent_id"]))
        snapshot = self.snapshot()
        snapshot["panes"].append({"pane_id": "old-shell", "terminal_id": old["terminal_id"],
                                  "workspace_id": "elsewhere"})
        self.assertEqual(display.reclaim(snapshot=snapshot)["panes"], [])
        self.assertEqual(display.reclaim(apply=True, snapshot=snapshot, pane_ids=["pane"])["panes"], [])
        self.assertEqual(self.herdr.commands, [])

    def test_display_lock_keys_are_stable_and_bounded_without_creating_files(self):
        before = set(self.store.directory.iterdir())
        keys = {self.store.display_lock_key(host, f"pane-{index}")
                for host in (self.store.host, "remote") for index in range(1024)}
        allowed = {f"display-slot-{index:02x}.lock" for index in range(256)}
        self.assertTrue(keys <= allowed)
        self.assertLessEqual(len(keys), 256)
        self.assertEqual(self.store.display_lock_key(self.store.host, "pane"),
                         self.store.display_lock_key(self.store.host, "pane"))
        self.assertEqual(set(self.store.directory.iterdir()), before)
        with mock.patch.object(self.store, "lock") as lock:
            with self.store.display_lock(self.store.host, "pane"):
                pass
        lock.assert_called_once_with(
            self.store.display_lock_key(self.store.host, "pane").removesuffix(".lock"),
            timeout=0.25, error_code="display_busy")

    def test_colliding_panes_share_actual_lock_file_and_timeout(self):
        seen = {}
        for index in range(257):
            pane = f"collision-{index}"
            key = self.store.display_lock_key(self.store.host, pane)
            if key in seen:
                first, second = seen[key], pane
                break
            seen[key] = pane
        else:
            self.fail("257 pane keys must collide in 256 slots")
        other = LedgerStore(self.store.directory, canonical_name=self.store.host)
        with self.store.display_lock(self.store.host, first):
            with self.assertRaises(LedgerError) as error:
                with other.display_lock(self.store.host, second, timeout=0):
                    self.fail("A colliding pane must not acquire the held slot")
        self.assertEqual(error.exception.code, "display_busy")
        with other.display_lock(self.store.host, second, timeout=0):
            pass
        files = {path.name for path in self.store.directory.glob("display-*.lock")}
        self.assertEqual(files, {key})


if __name__ == "__main__":
    unittest.main()
