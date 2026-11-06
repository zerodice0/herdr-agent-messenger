"""Restored pane ownership, serialized projection and captured-host replay."""
import tests.ham_test_support as ham_test_support  # Guard live state before product imports.

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import copy
import json
from pathlib import Path
import subprocess
import threading
import tempfile
import unittest
from unittest import mock

from ham.ledger import display_repair
from ham.ledger import store as ledger_store
from ham.ledger.display import LedgerDisplay
from ham.ledger.status import AgentRegistry, recoverable_display_owner
from ham.ledger.store import LedgerError, LedgerStore
from tests import test_display_repair as repair_tests
from tests.test_ledger_display import HerdrDisplay


class DisplayRestoreTest(unittest.TestCase):
    # Reuse the isolated store, effective server and subprocess tripwire without
    # inheriting (and rerunning) the unrelated display-repair test methods.
    setUp = repair_tests.DisplayRepairTest.setUp
    new_registry = repair_tests.DisplayRepairTest.new_registry
    snapshot = repair_tests.DisplayRepairTest.snapshot
    entries = repair_tests.DisplayRepairTest.entries
    agent = repair_tests.DisplayRepairTest.agent
    reports = repair_tests.DisplayRepairTest.reports
    writes = repair_tests.DisplayRepairTest.writes
    initialize = repair_tests.DisplayRepairTest.initialize
    lost_ownership = repair_tests.DisplayRepairTest.lost_ownership

    def restored(self, *, reconcile=True, session="restored-session", border=None,
                 historical_markerless=False):
        old = self.initialize()
        self.registry.set_status(old, phase="review", summary="Historical task")
        owned = self.agent()["last_ham_label"]
        if historical_markerless:
            legacy = "orange-rabbit · Historical task"
            self.registry.set_display_label(old["agent_id"], owned, legacy)
            self.herdr.labels["pane"] = legacy
            owned = legacy
        for row in (*self.template["panes"], *self.template["agents"]):
            row["terminal_id"] = "restored-terminal"
        self.template["agents"][0]["agent_session"] = {"value": session}
        self.herdr.terminals["pane"] = "restored-terminal"
        if border is not None:
            self.herdr.labels["pane"] = border
        self.herdr.commands.clear()
        if reconcile:
            self.registry.reconcile(self.snapshot(), now=300, refresh_display=False)
        return old, owned

    def insert_agent(self, row):
        with self.store.transaction() as connection:
            keys = tuple(row)
            connection.execute(
                f"INSERT INTO agents({','.join(keys)}) VALUES ({','.join('?' for _ in keys)})",
                tuple(row[key] for key in keys),
            )

    def recover(self, border, *, live_terminals=None):
        current = next(row for row in self.registry.list()
                       if row["terminal_id"] == self.herdr.terminals["pane"])
        with self.store.read() as connection:
            return recoverable_display_owner(
                connection, current, border, live_terminals=live_terminals,
            )

    def dump(self):
        with self.store.read() as connection:
            return list(connection.iterdump())

    def test_new_terminal_recovers_ownership_then_refreshes_current_status(self):
        old, owned = self.restored()
        current = self.agent()
        self.assertNotEqual(current["agent_id"], old["agent_id"])
        self.assertNotEqual(current["terminal_id"], old["terminal_id"])
        self.assertEqual(current["generation"], 1)
        self.assertEqual(current["last_ham_label"], owned)
        self.assertEqual(self.recover(owned), None)  # Ownership is already recorded.
        self.registry.set_status(current, phase="review", summary="Restored task")
        self.assertEqual(self.herdr.labels["pane"], "🟧 orange-rabbit · Restored task")
        self.assertEqual(self.agent()["last_ham_label"], self.herdr.labels["pane"])
        with self.store.read() as connection:
            retired = dict(connection.execute(
                "SELECT * FROM agents WHERE agent_id=?", (old["agent_id"],)).fetchone())
        self.assertFalse(retired["active"])
        self.assertEqual(retired["last_ham_label"], owned)

    def test_first_reconcile_recovers_and_projects_without_explicit_reclaim(self):
        self.restored(reconcile=False)
        self.registry.reconcile(self.snapshot(), now=300)
        self.assertEqual(self.herdr.labels["pane"], "🟧 orange-rabbit")
        self.assertEqual(self.agent()["last_ham_label"], "🟧 orange-rabbit")
        self.registry.set_status(self.agent(), phase="review", summary="Automatic restored projection")
        self.assertEqual(self.herdr.labels["pane"], "🟧 orange-rabbit · Automatic restored projection")

    def test_markerless_historical_border_transfers_exactly_then_migrates_once(self):
        _, owned = self.restored(historical_markerless=True)
        self.assertEqual(owned, "orange-rabbit · Historical task")
        self.assertEqual(self.agent()["last_ham_label"], owned)
        self.assertEqual(self.herdr.labels["pane"], owned)
        self.registry.reconcile(self.snapshot(), now=400)
        self.assertEqual(self.agent()["last_ham_label"], "🟧 orange-rabbit")
        self.assertEqual(self.herdr.labels["pane"], "🟧 orange-rabbit")
        self.assertEqual([command for command in self.writes() if command[:2] == ["pane", "rename"]],
                         [["pane", "rename", "pane", "🟧 orange-rabbit"]])
        self.herdr.commands.clear()
        self.registry.reconcile(self.snapshot(), now=401)
        self.assertEqual(self.herdr.commands, [])

    def test_restored_markerless_lookalike_is_not_exact_historical_ownership(self):
        markerless = "orange-rabbit · Historical task"
        self.restored(border=markerless)
        self.assertIsNone(self.agent()["last_ham_label"])
        display = LedgerDisplay(self.store, runner=self.herdr)
        self.assertEqual(display.reclaim(apply=True, snapshot=self.snapshot())["panes"], [])
        self.registry.reconcile(self.snapshot(), now=400)
        self.assertEqual(self.herdr.labels["pane"], markerless)
        self.assertIsNone(self.agent()["last_ham_label"])

    def test_markerless_historical_border_reclaim_keeps_exact_before_and_marked_after(self):
        _, owned = self.restored(historical_markerless=True)
        with self.store.transaction() as connection:
            connection.execute("UPDATE agents SET last_ham_label=NULL WHERE active=1")
        display = LedgerDisplay(self.store, runner=self.herdr)
        dry = display.reclaim(snapshot=self.snapshot())
        self.assertEqual(dry["panes"][0]["before"], {"border": owned, "last_ham_label": None})
        self.assertEqual(dry["panes"][0]["after"],
                         {"border": "🟧 orange-rabbit", "last_ham_label": "🟧 orange-rabbit"})
        applied = display.reclaim(apply=True, snapshot=self.snapshot())
        self.assertEqual(applied["updated"], ["pane"])
        self.assertEqual(applied["panes"][0]["before"], {"border": owned, "last_ham_label": None})
        self.assertEqual(self.agent()["last_ham_label"], "🟧 orange-rabbit")
        self.assertEqual(self.herdr.labels["pane"], "🟧 orange-rabbit")

    def test_existing_null_restored_row_recovers_on_next_reconcile(self):
        _, owned = self.restored()
        current_id = self.agent()["agent_id"]
        with self.store.transaction() as connection:
            connection.execute("UPDATE agents SET last_ham_label=NULL WHERE active=1")
        self.assertEqual(self.herdr.labels["pane"], owned)
        self.registry.reconcile(self.snapshot(), now=400)
        self.assertEqual(self.agent()["agent_id"], current_id)
        self.assertEqual(self.agent()["last_ham_label"], "🟧 orange-rabbit")
        self.assertEqual(self.herdr.labels["pane"], "🟧 orange-rabbit")

    def test_changed_session_restores_display_only_not_session_or_status(self):
        old, owned = self.restored(session="entirely-new-session")
        current = self.agent()
        self.assertNotEqual(current["session_ref"], old["session_ref"])
        self.assertEqual(current["session_ref"], "entirely-new-session")
        self.assertEqual(current["last_ham_label"], owned)
        self.assertEqual((current["phase"], current["summary"]), ("", ""))

    def test_human_border_is_preserved_after_restore_and_status_refresh(self):
        self.restored(border="Human task title")
        self.assertIsNone(self.agent()["last_ham_label"])
        self.registry.reconcile(self.snapshot(), now=400)
        self.registry.set_status(self.agent(), phase="review", summary="Do not replace the human title")
        self.assertEqual(self.herdr.labels["pane"], "Human task title")
        self.assertIsNone(self.agent()["last_ham_label"])
        self.assertFalse(any(command[:2] == ["pane", "rename"] for command in self.writes()))

    def test_retired_owner_must_match_every_nonterminal_scope_field(self):
        old, owned = self.restored()
        with self.store.transaction() as connection:
            connection.execute("UPDATE agents SET last_ham_label=NULL WHERE active=1")
        for key in ("host", "workspace_id", "pane_id", "repo_id", "client"):
            with self.subTest(scope=key):
                with self.store.transaction() as connection:
                    original = connection.execute(
                        f"SELECT {key} FROM agents WHERE agent_id=?", (old["agent_id"],)).fetchone()[0]
                    connection.execute(f"UPDATE agents SET {key}=? WHERE agent_id=?",
                                       ("other-scope", old["agent_id"]))
                self.assertIsNone(self.recover(owned, live_terminals={"restored-terminal"}))
                with self.store.transaction() as connection:
                    connection.execute(f"UPDATE agents SET {key}=? WHERE agent_id=?",
                                       (original, old["agent_id"]))
        self.assertEqual(self.recover(owned)["agent_id"], old["agent_id"])

    def test_exact_text_required_prefix_suffix_empty_and_unowned_history_refused(self):
        _, owned = self.restored()
        with self.store.transaction() as connection:
            connection.execute("UPDATE agents SET last_ham_label=NULL WHERE active=1")
        for border in ("", "Human: " + owned, owned + " human suffix", owned[:-1]):
            with self.subTest(border=border):
                self.assertIsNone(self.recover(border))
        for value in (None, ""):
            with self.subTest(historical_label=value):
                with self.store.transaction() as connection:
                    connection.execute("UPDATE agents SET last_ham_label=? WHERE active=0", (value,))
                self.assertIsNone(self.recover(owned))

    def test_active_prior_owner_is_not_reclaimable(self):
        old, owned = self.restored()
        with self.store.transaction() as connection:
            connection.execute("UPDATE agents SET last_ham_label=NULL WHERE active=1")
            connection.execute("UPDATE agents SET active=1 WHERE agent_id=?", (old["agent_id"],))
        self.assertIsNone(self.recover(owned, live_terminals={"restored-terminal"}))

    def test_retired_terminal_live_in_an_agentless_pane_elsewhere_is_not_reclaimable(self):
        old, owned = self.restored(reconcile=False)
        # The prior client exited, but its terminal remains in a shell pane that
        # does not occur in the agents list. Only the full panes snapshot proves it.
        snapshot = self.snapshot()
        snapshot["panes"].append({
            "terminal_id": old["terminal_id"], "pane_id": "shell-elsewhere",
            "workspace_id": "other-workspace", "label": "Shell title",
        })
        self.registry.reconcile(snapshot, now=300, refresh_display=False)
        self.assertIsNone(self.agent()["last_ham_label"])
        self.assertIsNone(self.recover(owned))
        self.assertIsNone(self.recover(owned, live_terminals={old["terminal_id"], "restored-terminal"}))
        self.assertEqual(self.herdr.labels["pane"], owned)

    def test_gone_terminal_with_any_active_agent_is_not_reclaimable(self):
        old, owned = self.restored()
        with self.store.read() as connection:
            retired = dict(connection.execute(
                "SELECT * FROM agents WHERE agent_id=?", (old["agent_id"],)).fetchone())
        active_elsewhere = {
            **retired, "agent_id": "active-on-retired-terminal", "generation": 2,
            "pane_id": "other-pane", "workspace_id": "other-workspace", "active": 1,
            "last_ham_label": None,
        }
        self.insert_agent(active_elsewhere)
        with self.store.transaction() as connection:
            connection.execute("UPDATE agents SET last_ham_label=NULL WHERE agent_id=?",
                               (self.agent()["agent_id"],))
        current = next(row for row in self.registry.list() if row["terminal_id"] == "restored-terminal")
        with self.store.read() as connection:
            self.assertIsNone(recoverable_display_owner(
                connection, current, owned, live_terminals={"restored-terminal"},
            ))

    def test_latest_owner_uses_last_seen_then_first_seen(self):
        old, owned = self.restored()
        with self.store.read() as connection:
            retired = dict(connection.execute(
                "SELECT * FROM agents WHERE agent_id=?", (old["agent_id"],)).fetchone())
        candidates = [
            {**retired, "agent_id": "older-last-seen", "terminal_id": "old-a", "last_seen": 400, "first_seen": 390},
            {**retired, "agent_id": "earlier-first-seen", "terminal_id": "old-b", "last_seen": 500, "first_seen": 410},
            {**retired, "agent_id": "latest-owner", "terminal_id": "old-c", "last_seen": 500, "first_seen": 420},
        ]
        for row in reversed(candidates):  # Insertion order is not recency.
            self.insert_agent(row)
        with self.store.transaction() as connection:
            connection.execute("UPDATE agents SET last_ham_label=NULL WHERE active=1")
        self.assertEqual(self.recover(owned)["agent_id"], "latest-owner")
        self.assertEqual(self.recover(owned, live_terminals={"old-c"})["agent_id"], "earlier-first-seen")

    def test_same_terminal_older_generation_still_recovers(self):
        old, owned = self.lost_ownership()
        self.assertEqual(self.recover(owned, live_terminals={"terminal"})["agent_id"], old["agent_id"])
        self.registry.reconcile(self.snapshot(), now=400)
        self.assertEqual(self.agent()["last_ham_label"], "🟧 orange-rabbit")

    def test_display_lock_has_quarter_second_budget_and_distinct_pane_host_keys(self):
        with mock.patch.object(self.store, "lock") as lock:
            for host, pane in ((self.store.host, "pane"), (self.store.host, "other"), ("remote", "pane")):
                with self.store.display_lock(host, pane):
                    pass
        names = [call.args[0] for call in lock.call_args_list]
        self.assertEqual(len(set(names)), 3)
        for call in lock.call_args_list:
            self.assertEqual(call.kwargs, {"timeout": 0.25, "error_code": "display_busy"})

    def test_display_lock_respects_shorter_shared_deadline(self):
        # The runtime reserves one second for returning its JSON response.
        with mock.patch.object(ledger_store.time, "monotonic", side_effect=[100.0, 100.0, 100.11]), \
                mock.patch.object(ledger_store.fcntl, "flock", side_effect=[BlockingIOError(), None]), \
                mock.patch.object(ledger_store.time, "sleep") as sleep:
            with ledger_store.runtime_budget(1.1):
                with self.assertRaises(LedgerError) as error:
                    with self.store.display_lock(self.store.host, "pane"):
                        self.fail("An expired lock must not be acquired")
        self.assertEqual(error.exception.code, "display_busy")
        sleep.assert_not_called()

    def test_projector_contention_skips_without_writes_diagnostics_or_repair_attempts(self):
        self.initialize()
        self.drift_for_retry()
        before = self.dump()
        other = LedgerStore(self.store.directory, canonical_name=self.store.host)
        display = LedgerDisplay(other, runner=self.herdr)
        with self.store.display_lock(self.store.host, "pane"):
            result = display.refresh("pane")
        self.assertEqual(result, {"updated": [], "failed": [], "skipped": ["pane"]})
        self.assertEqual(self.dump(), before)
        self.assertEqual(self.herdr.commands, [])
        self.assertEqual(other.notification_errors, [])
        self.assertEqual(self.store.notification_errors, [])

    def drift_for_retry(self):
        self.herdr.metadata["pane"]["herdr.agent-messenger"]["display_agent"] = "drifted"
        self.herdr.commands.clear()

    def test_queued_retry_contention_never_consumes_attempt_or_exhausts(self):
        self.initialize()
        self.drift_for_retry()
        self.herdr.metadata_fails = True
        self.registry.reconcile(self.snapshot(), now=200)
        self.herdr.metadata_fails = False
        pending = self.entries()
        self.assertEqual(pending["pane"]["attempts"], 1)
        self.herdr.commands.clear()
        other = LedgerStore(self.store.directory, canonical_name=self.store.host)
        contender = AgentRegistry(other, display_runner=self.herdr)
        before = self.dump()
        with self.store.display_lock(self.store.host, "pane"):
            for _ in range(display_repair.MAX_ATTEMPTS + 1):
                result = contender._refresh_display(
                    {"pane"}, snapshot=self.snapshot(), now=pending["pane"]["next_try_at"],
                )
                self.assertEqual(result, {"updated": [], "failed": []})
        self.assertEqual(self.entries(), pending)
        self.assertEqual(self.dump(), before)
        self.assertEqual(self.herdr.commands, [])
        self.assertEqual(other.notification_errors, [])

    def test_explicit_skipped_refresh_preserves_workers_existing_retry_reservation(self):
        self.initialize()
        self.drift_for_retry()
        self.herdr.metadata_fails = True
        self.registry.reconcile(self.snapshot(), now=200)
        self.herdr.metadata_fails = False
        pending = self.entries()
        other = LedgerStore(self.store.directory, canonical_name=self.store.host)
        contender = AgentRegistry(other, display_runner=self.herdr)
        self.herdr.commands.clear()
        with self.store.display_lock(self.store.host, "pane"):
            result = contender._refresh_display({"pane"}, feature_invoked=True, now=300)
        self.assertEqual(result, {"updated": [], "failed": []})
        marked = self.entries()
        self.assertEqual(marked["pane"].pop("explicit_pending"), pending["pane"]["fingerprint"])
        self.assertEqual(marked, pending)
        self.assertEqual(self.herdr.commands, [])
        self.assertEqual(other.notification_errors, [])

    def test_reconcile_contention_errors_instead_of_returning_stale_bindings(self):
        self.restored(reconcile=False)
        before = self.dump()
        other = LedgerStore(self.store.directory, canonical_name=self.store.host)
        contender = AgentRegistry(other, display_runner=self.herdr)
        with self.store.display_lock(self.store.host, "pane"), ledger_store.runtime_budget(1.1):
            with self.assertRaises(LedgerError) as error:
                contender.reconcile(self.snapshot(), now=300)
        self.assertEqual(error.exception.code, "ledger_busy")
        self.assertEqual(self.dump(), before)
        self.assertEqual(self.herdr.commands, [])
        self.assertEqual(other.notification_errors, [])

    def test_two_projectors_share_lock_and_never_diverge_border_from_db(self):
        self.initialize()
        with mock.patch.object(self.registry, "_refresh_display"):
            self.registry.set_status(self.agent(), phase="review", summary="Concurrent projection")
        self.herdr.commands.clear()
        first_inside = threading.Event()
        release_first = threading.Event()
        second_waiting = threading.Event()
        other = LedgerStore(self.store.directory, canonical_name=self.store.host)
        original_lock = other.display_lock

        def runner(command):
            if command[:2] == ["pane", "get"] and not first_inside.is_set():
                first_inside.set()
                if not release_first.wait(5):
                    raise AssertionError("First projector was not released")
            return self.herdr(command)

        @contextmanager
        def waiting_lock(host, pane):
            second_waiting.set()
            # Give this scheduling test time to serialize; the real .25-second
            # skip path is exercised separately under actual contention.
            with original_lock(host, pane, timeout=5):
                yield

        first = LedgerDisplay(self.store, runner=runner)
        second = LedgerDisplay(other, runner=self.herdr)
        with ThreadPoolExecutor(max_workers=2) as pool, \
                mock.patch.object(other, "display_lock", side_effect=waiting_lock):
            future_first = pool.submit(first.refresh, "pane")
            try:
                self.assertTrue(first_inside.wait(5))
                future_second = pool.submit(second.refresh, "pane")
                self.assertTrue(second_waiting.wait(5))
            finally:
                release_first.set()
            results = [future_first.result(timeout=5), future_second.result(timeout=5)]
        for result in results:
            self.assertEqual(result, {"updated": ["pane"], "failed": [], "skipped": []})
        self.assertEqual(self.herdr.labels["pane"], "🟧 orange-rabbit · Concurrent projection")
        self.assertEqual(self.agent()["last_ham_label"], self.herdr.labels["pane"])
        renames = [command for command in self.writes() if command[:2] == ["pane", "rename"]]
        self.assertEqual(len(renames), 1)
        self.assertEqual(self.store.notification_errors + other.notification_errors, [])

    def test_binding_changed_while_waiting_refuses_stale_projector(self):
        stale = self.initialize()
        waiting = threading.Event()
        acquired = threading.Event()
        other = LedgerStore(self.store.directory, canonical_name=self.store.host)
        original_lock = other.display_lock

        @contextmanager
        def waiting_lock(host, pane):
            waiting.set()
            with original_lock(host, pane, timeout=5):
                acquired.set()
                yield

        with ThreadPoolExecutor(max_workers=1) as pool, \
                mock.patch.object(other, "display_lock", side_effect=waiting_lock):
            with self.store.display_lock(self.store.host, "pane"):
                future = pool.submit(LedgerDisplay(other, runner=self.herdr).refresh_agent, stale)
                self.assertTrue(waiting.wait(5))
                with self.store.transaction() as connection:
                    connection.execute("UPDATE agents SET active=0 WHERE agent_id=?", (stale["agent_id"],))
                    replacement = {key: stale[key] for key in (
                        "agent_id", "host", "terminal_id", "workspace_id", "pane_id", "session_ref",
                        "generation", "client", "repo_id", "is_main_checkout", "alias", "active",
                        "extension_present", "last_ham_label", "first_seen", "last_seen", "native_session_pid",
                    )}
                    replacement.update(agent_id="replacement", terminal_id="replacement-terminal", generation=1)
                    keys = tuple(replacement)
                    connection.execute(
                        f"INSERT INTO agents({','.join(keys)}) VALUES ({','.join('?' for _ in keys)})",
                        tuple(replacement[key] for key in keys),
                    )
                    connection.execute("INSERT INTO agent_status VALUES (?,'idle','','',400)", ("replacement",))
                self.herdr.terminals["pane"] = "replacement-terminal"
            self.assertIsNone(future.result(timeout=5))
        self.assertTrue(acquired.is_set(), "Exercise stale-binding refusal, not lock timeout")
        self.assertEqual(self.herdr.commands, [])
        self.assertEqual(self.herdr.labels["pane"], "🟧 orange-rabbit")
        self.assertEqual(self.agent()["agent_id"], "replacement")
        self.assertEqual(other.notification_errors, [])

    def test_reclaim_dry_run_lists_only_exact_candidate_without_mutating_any_state(self):
        old, owned = self.restored()
        with self.store.transaction() as connection:
            connection.execute("UPDATE agents SET last_ham_label=NULL WHERE active=1")
        snapshot = self.snapshot()
        before_snapshot = copy.deepcopy(snapshot)
        before = self.dump()
        metadata = copy.deepcopy(self.herdr.metadata)
        display = LedgerDisplay(self.store, runner=self.herdr)
        result = display.reclaim(snapshot=snapshot)
        self.assertFalse(result["applied"])
        self.assertEqual(len(result["panes"]), 1)
        candidate = result["panes"][0]
        self.assertEqual(candidate["pane_id"], "pane")
        self.assertEqual(candidate["agent_id"], self.agent()["agent_id"])
        self.assertEqual(candidate["owner_agent_id"], old["agent_id"])
        self.assertEqual(candidate["status"], "candidate")
        self.assertEqual(candidate["before"], {"border": owned, "last_ham_label": None})
        self.assertEqual(candidate["after"], {"border": "🟧 orange-rabbit", "last_ham_label": "🟧 orange-rabbit"})
        self.assertEqual(self.dump(), before)
        self.assertEqual(self.herdr.metadata, metadata)
        self.assertEqual(self.herdr.commands, [])
        self.assertEqual(snapshot, before_snapshot)
        self.assertEqual(display.reclaim(snapshot=snapshot, pane_id="unrelated")["panes"], [])

    def test_reclaim_apply_refreshes_and_second_apply_is_write_free(self):
        _, owned = self.restored()
        with self.store.transaction() as connection:
            connection.execute("UPDATE agents SET last_ham_label=NULL WHERE active=1")
        display = LedgerDisplay(self.store, runner=self.herdr)
        self.assertEqual(self.herdr.labels["pane"], owned)
        applied = display.reclaim(apply=True, snapshot=self.snapshot(), pane_id="pane")
        self.assertTrue(applied["applied"])
        self.assertEqual(applied["updated"], ["pane"])
        self.assertEqual(applied["failed"], [])
        self.assertEqual(applied["panes"][0]["status"], "updated")
        self.assertEqual(self.agent()["last_ham_label"], "🟧 orange-rabbit")
        self.assertEqual(self.herdr.labels["pane"], "🟧 orange-rabbit")
        self.herdr.commands.clear()
        before = self.dump()
        again = display.reclaim(apply=True, snapshot=self.snapshot())
        self.assertEqual(again["panes"], [])
        self.assertEqual(again["updated"], [])
        self.assertEqual(self.dump(), before)
        self.assertEqual(self.herdr.commands, [])

    def test_reclaim_apply_rechecks_human_rename_before_recording_ownership(self):
        self.restored()
        with self.store.transaction() as connection:
            connection.execute("UPDATE agents SET last_ham_label=NULL WHERE active=1")
        stale_snapshot = self.snapshot()
        self.herdr.labels["pane"] = "Human rename after scan"
        before = self.dump()
        result = LedgerDisplay(self.store, runner=self.herdr).reclaim(apply=True, snapshot=stale_snapshot)
        self.assertEqual(result["updated"], [])
        self.assertEqual(self.dump(), before)
        self.assertIsNone(self.agent()["last_ham_label"])
        self.assertEqual(self.herdr.labels["pane"], "Human rename after scan")
        self.assertEqual(self.writes(), [])

    def test_reclaim_apply_contention_is_silent_and_leaves_candidate_unowned(self):
        self.restored()
        with self.store.transaction() as connection:
            connection.execute("UPDATE agents SET last_ham_label=NULL WHERE active=1")
        other = LedgerStore(self.store.directory, canonical_name=self.store.host)
        before = self.dump()
        with self.store.display_lock(self.store.host, "pane"):
            result = LedgerDisplay(other, runner=self.herdr).reclaim(apply=True, snapshot=self.snapshot())
        self.assertEqual(result["skipped"], ["pane"])
        self.assertEqual(result["failed"], [])
        self.assertEqual(self.dump(), before)
        self.assertEqual(self.herdr.commands, [])
        self.assertEqual(other.notification_errors, [])

    def test_explicit_repo_exception_requires_gone_different_terminal_and_exact_scope(self):
        old, owned = self.restored()
        current = self.agent()
        with self.store.transaction() as connection:
            connection.execute("UPDATE agents SET last_ham_label=NULL WHERE active=1")
            connection.execute("UPDATE agents SET repo_id='historical-repo' WHERE agent_id=?",
                               (old["agent_id"],))
        current = {**current, "last_ham_label": None}
        with self.store.read() as connection:
            self.assertIsNone(recoverable_display_owner(connection, current, owned))
            candidate = recoverable_display_owner(
                connection, current, owned, allow_repo_mismatch=True,
                live_terminals={"restored-terminal"},
            )
            self.assertEqual(candidate["agent_id"], old["agent_id"])
            self.assertIsNone(recoverable_display_owner(
                connection, current, owned, allow_repo_mismatch=True,
                live_terminals={old["terminal_id"], "restored-terminal"},
            ))
        # Explicit selection cannot weaken host/workspace/pane/client checks.
        for key in ("host", "workspace_id", "pane_id", "client"):
            with self.subTest(scope=key), self.store.read() as connection:
                wrong = {**current, key: "other"}
                self.assertIsNone(recoverable_display_owner(
                    connection, wrong, owned, allow_repo_mismatch=True, live_terminals=set(),
                ))
        # The legacy same-terminal proof needs no repository exception.
        same_terminal = {**current, "terminal_id": old["terminal_id"], "generation": 2}
        with self.store.read() as connection:
            self.assertEqual(recoverable_display_owner(
                connection, same_terminal, owned, live_terminals=set(),
            )["agent_id"], old["agent_id"])

    def test_repeatable_reclaim_cli_forwards_only_explicit_panes(self):
        from ham.ledger import display as ledger_display
        import io

        result = {"applied": False, "panes": [], "updated": [], "failed": [], "skipped": []}
        with mock.patch.object(ledger_display, "LedgerStore") as factory, \
                mock.patch.object(LedgerDisplay, "reclaim", return_value=result) as reclaim, \
                mock.patch("sys.stdout", new_callable=io.StringIO) as output:
            factory.open_readonly.return_value = self.store
            self.assertEqual(ledger_display.main([
                "reclaim", "--pane", "first-pane", "--pane", "second-pane",
            ]), 0)
        self.assertEqual(reclaim.call_args.kwargs["pane_ids"], ["first-pane", "second-pane"])
        self.assertFalse(reclaim.call_args.kwargs["apply"])
        self.assertEqual(json.loads(output.getvalue()), {"result": result})
        factory.assert_not_called()
        factory.open_readonly.assert_called_once_with()

    def test_status_committed_during_metadata_skip_is_queued_and_next_snapshot_converges(self):
        self.initialize()
        with mock.patch.object(self.registry, "_refresh_display"):
            self.registry.set_status(self.agent(), phase="review", summary="First projection")
        nested = []

        def newer_status():
            nested.append(self.registry.set_status(
                self.agent(), phase="review", summary="Latest committed status",
            ))

        self.herdr.after_report = newer_status
        self.herdr.commands.clear()
        result = self.registry._refresh_display({"pane"}, feature_invoked=True, now=200)
        self.assertEqual(result, {"updated": [], "failed": []})
        self.assertEqual(nested[0]["display"], {"updated": [], "failed": []})
        pending = self.entries()["pane"]
        self.assertEqual(pending["state"], "pending")
        self.assertEqual(pending["attempts"], 0)
        self.assertEqual(self.agent()["summary"], "Latest committed status")
        self.assertEqual(self.store.notification_errors, [])
        self.registry.reconcile(self.snapshot(), now=pending["next_try_at"])
        self.assertEqual(self.herdr.labels["pane"], "🟧 orange-rabbit · Latest committed status")
        self.assertEqual(self.agent()["last_ham_label"], self.herdr.labels["pane"])
        self.registry.reconcile(self.snapshot(), now=pending["next_try_at"] + 1)
        self.assertEqual(self.entries(), {})

    def test_real_sweep_refund_retains_status_deferred_during_its_metadata_write(self):
        from ham.ledger import worker

        self.initialize()
        with mock.patch.object(self.registry, "_refresh_display"):
            self.registry.set_status(self.agent(), phase="review", summary="First sweep target")
        in_flight = []

        def commit_new_status():
            in_flight.append(copy.deepcopy(self.entries()["pane"]))
            self.registry.set_status(
                self.agent(), phase="review", summary="Explicit target during sweep",
            )
            in_flight.append(copy.deepcopy(self.entries()["pane"]))

        # The real sweep owns the pane lock, not the test. Its own skipped
        # refresh must refund its attempt after the nested status was deferred.
        self.herdr.after_report = commit_new_status
        self.registry.reconcile(self.snapshot(), now=200)
        self.assertEqual(in_flight[0]["attempts"], 1)
        self.assertNotIn("explicit_pending", in_flight[0])
        self.assertIn("explicit_pending", in_flight[1])
        pending = self.entries()["pane"]
        desired = LedgerDisplay(self.store, runner=self.herdr).projection(self.agent())
        fingerprint = display_repair.fingerprint(self.agent(), desired)
        self.assertEqual(pending["explicit_pending"], fingerprint)
        self.assertEqual(pending["fingerprint"], fingerprint)
        self.assertEqual(pending["attempts"], 0)
        self.assertEqual(pending["state"], "pending")
        self.assertTrue(worker.maintenance_pending(self.store))
        self.assertEqual(self.store.notification_errors, [])
        self.registry.reconcile(self.snapshot(), now=pending["next_try_at"])
        self.assertEqual(self.herdr.labels["pane"], "🟧 orange-rabbit · Explicit target during sweep")
        self.assertEqual(self.agent()["last_ham_label"], self.herdr.labels["pane"])
        self.registry.reconcile(self.snapshot(), now=pending["next_try_at"] + 1)
        self.assertEqual(self.entries(), {})

    def assert_explicit_completion_retains_newer_deferred_target(self, *, fail):
        from ham.ledger import worker

        self.initialize()
        self.drift_for_retry()
        self.registry.reconcile(self.snapshot(), now=200)
        self.assertEqual(self.entries()["pane"]["attempts"], 1)
        with mock.patch.object(self.registry, "_refresh_display"):
            self.registry.set_status(self.agent(), phase="review", summary="Older explicit target")
        interrupted = []

        def project(command):
            if command[:2] != ["pane", "rename"] or interrupted:
                return self.herdr(command)
            interrupted.append(True)
            result = (subprocess.CompletedProcess(command, 1, "", "simulated rename failure")
                      if fail else self.herdr(command))
            self.registry.set_status(
                self.agent(), phase="review", summary="New target while rename completes",
            )
            return result

        self.registry = AgentRegistry(self.store, display_runner=project)
        result = self.registry._refresh_display({"pane"}, feature_invoked=True, now=300)
        self.assertEqual(result["failed"], ["pane"] if fail else [])
        self.assertEqual(result["updated"], [] if fail else ["pane"])
        self.assertTrue(interrupted)
        pending = self.entries()["pane"]
        projection = LedgerDisplay(self.store, runner=self.herdr).projection(self.agent())
        self.assertEqual(pending["explicit_pending"], display_repair.fingerprint(self.agent(), projection))
        self.assertTrue(worker.maintenance_pending(self.store))
        self.registry.reconcile(self.snapshot(), now=400)
        self.assertEqual(self.herdr.labels["pane"], "🟧 orange-rabbit · New target while rename completes")
        self.assertEqual(self.agent()["last_ham_label"], self.herdr.labels["pane"])
        self.registry.reconcile(self.snapshot(), now=401)
        self.assertEqual(self.entries(), {})

    def test_successful_explicit_projection_keeps_newer_request_deferred_during_rename(self):
        self.assert_explicit_completion_retains_newer_deferred_target(fail=False)

    def test_failed_explicit_projection_keeps_newer_request_deferred_during_rename(self):
        self.assert_explicit_completion_retains_newer_deferred_target(fail=True)

    def test_reconcile_deduplicates_colliding_lock_slots(self):
        snapshot = copy.deepcopy(self.template)
        second_pane = {**snapshot["panes"][0], "pane_id": "second-pane",
                       "terminal_id": "second-terminal"}
        snapshot["panes"].append(second_pane)
        snapshot["agents"].append({**snapshot["agents"][0], **second_pane})
        with mock.patch.object(self.store, "display_lock_key", return_value="display-slot-00.lock"), \
                mock.patch.object(self.store, "display_lock", wraps=self.store.display_lock) as lock:
            self.registry.reconcile(snapshot, now=100, refresh_display=False)
        self.assertEqual(lock.call_count, 1)
        self.assertEqual(len(self.registry.list()), 2)

    def test_named_label_projector_contention_defers_retry_without_stderr_or_writes(self):
        from ham.agents import labels
        import io

        self.initialize()
        observed = self.snapshot()["agents"][0]
        observed["display_agent"] = "Drifted source"
        other = LedgerStore(self.store.directory, canonical_name=self.store.host)
        with self.store.display_lock(self.store.host, "pane"), \
                mock.patch("ham.ledger.display.LedgerStore", return_value=other), \
                mock.patch.object(labels, "fetch_agent_info", return_value=observed), \
                mock.patch.object(labels, "run_herdr", side_effect=lambda *args: self.herdr(args)), \
                mock.patch("sys.stdout", new_callable=io.StringIO) as stdout, \
                mock.patch("sys.stderr", new_callable=io.StringIO) as stderr:
            self.assertEqual(labels.assign_label("pane"), 0)
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(self.herdr.commands, [])
        pending = self.entries()["pane"]
        self.assertEqual(pending["state"], "pending")
        self.assertEqual(pending["attempts"], 0)
        self.assertEqual(pending["explicit_pending"], pending["fingerprint"])
        self.assertEqual(other.notification_errors, [])


class CapturedDisplayRestoreTest(unittest.TestCase):
    def setUp(self):
        environment = mock.patch.dict("os.environ")
        environment.start().pop("NO_COLOR", None)
        self.addCleanup(environment.stop)

    def test_all_24_original_first_loss_bindings_are_reclaimable(self):
        # The same read-only 2026-11-06 affected reports retain first_unowned_row
        # and prior_owned_row. Reconstruct those historical bindings as active
        # at first loss; do not rewrite a CURRENT repo identity to manufacture
        # 24 automatic candidates after a repository origin changed.
        cases = json.loads((Path(__file__).parent / "fixtures" / "display_restore_first_loss.json").read_text())
        with tempfile.TemporaryDirectory(prefix="ham-first-loss-") as directory, \
                mock.patch("subprocess.run", side_effect=AssertionError("real subprocess forbidden")):
            stores = {}
            for index, case in enumerate(cases):
                host = case["current"]["host"]
                if host not in stores:
                    stores[host] = LedgerStore(Path(directory) / str(index), canonical_name=host)
                with stores[host].transaction() as connection:
                    for row in (case["owner"], {**case["current"], "active": 1}):
                        keys = tuple(row)
                        connection.execute(
                            f"INSERT INTO agents({','.join(keys)}) VALUES ({','.join('?' for _ in keys)})",
                            tuple(row[key] for key in keys),
                        )
                    current = case["current"]
                    connection.execute("INSERT INTO terminal_slots VALUES (?,?,?,?,1,1)", (
                        host, current["terminal_id"], current["pane_id"], current["workspace_id"],
                    ))
            self.assertEqual(len(cases), 24)
            for case in cases:
                current = {**case["current"], "active": 1}
                with self.subTest(host=current["host"], pane=current["pane_id"]), \
                        stores[current["host"]].read() as connection:
                    recovered = recoverable_display_owner(connection, current, case["border"])
                    self.assertIsNotNone(recovered)
                    self.assertEqual(recovered["agent_id"], case["owner"]["agent_id"])
                    self.assertNotEqual(recovered["terminal_id"], current["terminal_id"])

    def test_current_captured_borders_reclaim_without_touching_unaffected_panes(self):
        # Provenance: read-only 2026-11-06 border-regress capture, originally
        # winmini-affected.json, mac-studio-affected.json, winmini-history.json,
        # studio-history-raw.json and winmini-panes.json. The committed fixture
        # retains matching historical owners, every active row and complete live
        # pane bindings; no absolute scratch location or live service is needed.
        fixture = json.loads((Path(__file__).parent / "fixtures" / "display_restore_2026_11_06.json").read_text())

        with tempfile.TemporaryDirectory(prefix="ham-captured-restore-") as directory, \
                mock.patch("subprocess.run", side_effect=AssertionError("real subprocess forbidden")), \
                mock.patch("ham.ledger.status.live_snapshot", side_effect=AssertionError("live snapshot forbidden")):
            total = 0
            for evidence in fixture["hosts"]:
                with self.subTest(host=evidence["host"]):
                    store = LedgerStore(Path(directory) / evidence["name"], canonical_name=evidence["host"])
                    affected = {row["pane_id"]: row for row in evidence["expected_candidates"]}
                    # w3X changed from a local repo identity to its Git origin
                    # after the first loss. Default reclaim must NOT bypass the
                    # same-repository guard merely to claim all 24 current rows.
                    refused = {"w3X:p1"} if evidence["name"] == "winmini" else set()
                    expected = {pane: row for pane, row in affected.items() if pane not in refused}
                    with store.transaction() as connection:
                        for row in evidence["agents"]:
                            keys = tuple(row)
                            connection.execute(
                                f"INSERT INTO agents({','.join(keys)}) VALUES ({','.join('?' for _ in keys)})",
                                tuple(row[key] for key in keys),
                            )
                            status = affected.get(row["pane_id"], {}).get("status") if row["active"] else None
                            status = status or {}
                            connection.execute("INSERT INTO agent_status VALUES (?,?,?,?,?)", (
                                row["agent_id"], status.get("turn_state", "idle"), status.get("phase", ""),
                                status.get("summary", ""), status.get("updated_at", row["last_seen"]),
                            ))
                        for pane in evidence["panes"]:
                            connection.execute("INSERT INTO terminal_slots VALUES (?,?,?,?,1,1)", (
                                store.host, pane["terminal_id"], pane["pane_id"], pane["workspace_id"],
                            ))
                    herdr = HerdrDisplay(
                        [pane["pane_id"] for pane in evidence["panes"]],
                        terminals={pane["pane_id"]: pane["terminal_id"] for pane in evidence["panes"]},
                    )
                    herdr.labels.update({pane["pane_id"]: pane.get("label", "") for pane in evidence["panes"]})
                    display = LedgerDisplay(store, runner=herdr)
                    snapshot = {"panes": copy.deepcopy(evidence["panes"]), "agents": []}
                    unchanged = set(evidence["unchanged_pane_ids"]) | refused
                    for pane in evidence["panes"]:
                        herdr.metadata[pane["pane_id"]]["tokens"] = copy.deepcopy(pane.get("tokens", {}))
                        if "display_agent" in pane:
                            herdr.metadata[pane["pane_id"]]["herdr.agent-messenger"] = {
                                "display_agent": pane["display_agent"],
                            }
                    untouched_labels = {pane: herdr.labels[pane] for pane in unchanged}
                    untouched_metadata = {pane: copy.deepcopy(herdr.metadata[pane]) for pane in untouched_labels}
                    with mock.patch("ham.ledger.settings.RepoSettings.role",
                                    side_effect=lambda row, **kwargs: affected.get(row["pane_id"], {}).get("role")):
                        with store.read() as connection:
                            before = list(connection.iterdump())
                        # The affected report's expected_border was captured
                        # before some status updates. Preserve that evidence,
                        # but project the current recorded status for apply.
                        current_labels = {
                            row["pane_id"]: display.projection(row).label
                            for row in display.registry.list()
                        }
                        dry = display.reclaim(snapshot=snapshot)
                        self.assertEqual({row["pane_id"] for row in dry["panes"]}, set(affected))
                        self.assertEqual(len(dry["panes"]), len(affected))
                        for candidate in dry["panes"]:
                            original = affected[candidate["pane_id"]]
                            self.assertEqual(candidate["agent_id"], original["agent_id"])
                            self.assertEqual(candidate["owner_agent_id"], original["owner_agent_id"])
                            self.assertEqual(candidate["before"], {"border": original["border"], "last_ham_label": None})
                            self.assertEqual(candidate["status"],
                                             "explicit_only" if candidate["pane_id"] in refused else "candidate")
                            projected = current_labels[candidate["pane_id"]]
                            self.assertEqual(candidate["after"], {
                                "border": projected, "last_ham_label": projected,
                            })
                        with store.read() as connection:
                            self.assertEqual(list(connection.iterdump()), before)
                        self.assertEqual(herdr.commands, [])
                        applied = display.reclaim(apply=True, snapshot=snapshot)
                        self.assertEqual(set(applied["updated"]), set(expected))
                        self.assertEqual(applied["failed"], [])
                        self.assertEqual(set(applied["skipped"]), refused)
                        with store.read() as connection:
                            current = {row["pane_id"]: dict(row) for row in connection.execute("SELECT * FROM agents WHERE active=1")}
                        for pane, original in expected.items():
                            self.assertEqual(herdr.labels[pane], current_labels[pane])
                            self.assertEqual(current[pane]["last_ham_label"], current_labels[pane])
                        self.assertEqual({pane: herdr.labels[pane] for pane in untouched_labels}, untouched_labels)
                        self.assertEqual({pane: herdr.metadata[pane] for pane in untouched_labels}, untouched_metadata)
                        self.assertTrue(all(command[2] in expected for command in herdr.commands))
                        snapshot["panes"] = [{**pane, "label": herdr.labels[pane["pane_id"]]} for pane in snapshot["panes"]]
                        herdr.commands.clear()
                        with store.read() as connection:
                            after = list(connection.iterdump())
                        remaining = display.reclaim(apply=True, snapshot=snapshot)
                        self.assertEqual({row["pane_id"] for row in remaining["panes"]}, refused)
                        self.assertTrue(all(row["status"] == "explicit_only" for row in remaining["panes"]))
                        with store.read() as connection:
                            self.assertEqual(list(connection.iterdump()), after)
                        self.assertEqual(herdr.commands, [])
                        if refused:
                            # The real w3X current row is refused by automatic
                            # recovery but can be explicitly selected by a human.
                            pane = "w3X:p1"
                            with store.read() as connection:
                                before_explicit = list(connection.iterdump())
                                active = dict(connection.execute(
                                    "SELECT * FROM agents WHERE active=1 AND pane_id=?", (pane,)).fetchone())
                                owner = dict(connection.execute(
                                    "SELECT * FROM agents WHERE agent_id=?",
                                    (affected[pane]["owner_agent_id"],)).fetchone())
                            self.assertNotEqual(active["repo_id"], owner["repo_id"])
                            explicit = display.reclaim(snapshot=snapshot, pane_ids=[pane])
                            self.assertEqual(len(explicit["panes"]), 1)
                            candidate = explicit["panes"][0]
                            self.assertEqual(candidate["status"], "explicit_only")
                            self.assertEqual(candidate["prior_repo_id"], owner["repo_id"])
                            self.assertEqual(candidate["current_repo_id"], active["repo_id"])
                            self.assertIn(owner["repo_id"], candidate["reason"])
                            self.assertIn(active["repo_id"], candidate["reason"])
                            with store.read() as connection:
                                self.assertEqual(list(connection.iterdump()), before_explicit)
                            self.assertEqual(herdr.commands, [])
                            applied = display.reclaim(apply=True, snapshot=snapshot, pane_ids=[pane])
                            self.assertEqual(applied["updated"], [pane])
                            self.assertEqual(applied["failed"], [])
                            self.assertEqual(herdr.labels[pane], current_labels[pane])
                            snapshot["panes"] = [{**row, "label": herdr.labels[row["pane_id"]]}
                                                 for row in snapshot["panes"]]
                            herdr.commands.clear()
                            with store.read() as connection:
                                after_explicit = list(connection.iterdump())
                                self.assertEqual(connection.execute(
                                    "SELECT last_ham_label FROM agents WHERE agent_id=?",
                                    (active["agent_id"],)).fetchone()[0], herdr.labels[pane])
                            self.assertEqual(display.reclaim(
                                apply=True, snapshot=snapshot, pane_ids=[pane])["panes"], [])
                            with store.read() as connection:
                                self.assertEqual(list(connection.iterdump()), after_explicit)
                            self.assertEqual(herdr.commands, [])
                    total += len(expected)
            self.assertEqual(total, 23)


if __name__ == "__main__":
    unittest.main()
