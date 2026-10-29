"""Snapshot-driven display convergence, bounded retries and exact border ownership."""
import tests.ham_test_support as ham_test_support  # Guard live state before product imports.

import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from ham.ledger import display_repair
from ham.ledger import worker
from ham.ledger.display import LedgerDisplay, SOURCE, build_display, metadata_matches, report_metadata
from ham.ledger.settings import RepoSettings
from ham.ledger.status import AgentRegistry
from ham.ledger.store import LedgerStore
from tests.test_ledger_display import HerdrDisplay


class EffectiveDisplay(HerdrDisplay):
    """The next snapshot exposes actual writes, including ignored/racing writes."""

    def __init__(self):
        super().__init__(["pane"], terminals={"pane": "terminal"})
        self.ignore_metadata = False
        self.after_report = None

    def __call__(self, command):
        before = copy.deepcopy(self.metadata)
        result = super().__call__(command)
        if list(command[:2]) == ["pane", "report-metadata"]:
            if self.ignore_metadata:
                self.metadata = before
            if command[4] == SOURCE and self.after_report is not None:
                callback, self.after_report = self.after_report, None
                callback()
        return result

    def snapshot(self, template):
        observed = copy.deepcopy(template)
        for pane in observed["panes"]:
            pane["label"] = self.labels[pane["pane_id"]]
            pane["tokens"] = copy.deepcopy(self.metadata[pane["pane_id"]]["tokens"])
        for agent in observed["agents"]:
            metadata = self.metadata[agent["pane_id"]]
            agent["display_agent"] = metadata.get(SOURCE, {}).get("display_agent", "")
            agent["tokens"] = copy.deepcopy(metadata["tokens"])
        return observed


class DisplayRepairTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="ham-display-repair-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.store = LedgerStore(self.root / "ledger", canonical_name="display-repair-test")
        self.identity = {
            "repo_id": "display-repair-repo", "is_main_checkout": True,
        }
        identity = mock.patch("ham.ledger.status.repository_identity", return_value=self.identity)
        identity.start()
        self.addCleanup(identity.stop)
        # Off mode isolates display maintenance from mail/role maintenance.
        with self.store.transaction() as connection:
            connection.execute("INSERT INTO repos VALUES (?,NULL,?)", (self.identity["repo_id"], 1.0))
        self.herdr = EffectiveDisplay()
        self.registry = self.new_registry()
        pane = {"terminal_id": "terminal", "pane_id": "pane", "workspace_id": "workspace"}
        self.template = {"panes": [pane], "agents": [{
            **pane, "agent": "omp", "name": "orange-rabbit", "agent_status": "idle",
            "agent_session": {"value": "session"}, "cwd": str(self.repo),
        }]}
        # A subprocess tripwire covers Git, snapshot and display fallbacks alike.
        process = mock.patch("subprocess.run", side_effect=AssertionError("real subprocess forbidden"))
        process.start()
        self.addCleanup(process.stop)

    def new_registry(self):
        return AgentRegistry(self.store, display_runner=self.herdr)

    def snapshot(self):
        return self.herdr.snapshot(self.template)

    def entries(self):
        with self.store.read() as connection:
            return display_repair.load(connection)

    def agent(self):
        return self.registry.list()[0]

    def reports(self):
        return [command for command in self.herdr.commands
                if command[:2] == ["pane", "report-metadata"] and command[4] == SOURCE]

    def writes(self):
        return [command for command in self.herdr.commands
                if command[:2] in (["pane", "report-metadata"], ["pane", "rename"])]

    def initialize(self, *, unreported=False):
        if unreported:
            self.template["agents"][0].pop("agent_session")
        self.registry.reconcile(self.snapshot(), now=100)
        self.registry.reconcile(self.snapshot(), now=101)
        self.assertEqual(self.entries(), {})
        self.herdr.commands.clear()
        return self.agent()

    def drift(self, key="display_agent", value="foreign value"):
        if key == "display_agent":
            self.herdr.metadata["pane"][SOURCE][key] = value
        else:
            self.herdr.metadata["pane"]["tokens"][key] = value

    def assert_matching(self):
        projection = LedgerDisplay(self.store, runner=self.herdr).projection(self.agent())
        metadata = self.herdr.metadata["pane"]
        self.assertEqual(metadata[SOURCE]["display_agent"], projection.display_agent)
        for key in ("alias", "color", "recap", "phone", "emu"):
            expected = getattr(projection, key)
            if key in ("phone", "emu") and not expected:
                self.assertNotIn(key, metadata["tokens"])
            else:
                self.assertEqual(metadata["tokens"].get(key), expected)
        self.assertEqual(metadata["tokens"]["other"], "keep")

    def test_border_only_drift_renames_once_without_metadata_write(self):
        self.initialize()
        with mock.patch.object(self.registry, "_refresh_display", return_value={"updated": [], "failed": []}):
            self.registry.set_status(self.agent(), phase="", summary="Changed border only")
        self.registry.reconcile(self.snapshot(), now=200)
        self.assertEqual(self.writes(), [[
            "pane", "rename", "pane", "orange-rabbit · Changed border only",
        ]])
        self.assertEqual(self.herdr.labels["pane"], "orange-rabbit · Changed border only")
        self.herdr.commands.clear()
        self.registry.reconcile(self.snapshot(), now=201)
        self.assertEqual(self.herdr.commands, [])
        self.assertEqual(self.entries(), {})

    def test_each_authoritative_metadata_field_repairs_without_identity_change(self):
        self.initialize()
        original = self.agent()
        for index, key in enumerate(("display_agent", "alias", "color", "recap", "phone", "emu")):
            with self.subTest(field=key):
                self.drift(key)
                self.herdr.commands.clear()
                at = 200 + index * 100
                self.registry.reconcile(self.snapshot(), now=at)
                self.assertEqual(len(self.reports()), 1)
                self.assert_matching()
                self.assertEqual(self.agent()["agent_id"], original["agent_id"])
                self.assertEqual(self.agent()["generation"], original["generation"])
                self.assertEqual(self.entries()["pane"]["state"], "pending")
                # Success is provisional until a subsequent effective snapshot.
                self.herdr.commands.clear()
                self.registry.reconcile(self.snapshot(), now=at + 1)
                self.assertEqual(self.entries(), {})
                self.assertEqual(self.herdr.commands, [])

    def test_missing_authoritative_fields_are_repaired(self):
        self.initialize()
        with mock.patch("ham.ledger.status.time.time", return_value=150):
            self.registry.set_status(self.agent(), phase="review", summary="")
        self.registry.reconcile(self.snapshot(), now=151)
        self.assertEqual(self.entries(), {})
        for index, key in enumerate(("display_agent", "alias", "color", "recap")):
            with self.subTest(field=key):
                target = self.herdr.metadata["pane"][SOURCE if key == "display_agent" else "tokens"]
                target.pop(key)
                self.herdr.commands.clear()
                self.registry.reconcile(self.snapshot(), now=200 + index * 100)
                self.assertEqual(len(self.reports()), 1)
                self.assert_matching()
                self.registry.reconcile(self.snapshot(), now=201 + index * 100)

    def test_held_device_tokens_repaired_and_released_devices_cleared(self):
        self.initialize()
        with self.store.transaction() as connection:
            connection.executemany("INSERT INTO device_claims VALUES (?,?,?,?,?)", [
                ("serial", "phone", "Pixel", self.agent()["agent_id"], 1.0),
                ("emulator-5554", "emu", "5554", self.agent()["agent_id"], 2.0),
            ])
        self.registry.reconcile(self.snapshot(), now=200)
        self.registry.reconcile(self.snapshot(), now=201)
        for index, key in enumerate(("phone", "emu")):
            with self.subTest(kind=key):
                self.herdr.metadata["pane"]["tokens"].pop(key)
                self.herdr.commands.clear()
                self.registry.reconcile(self.snapshot(), now=300 + index * 100)
                self.assertEqual(len(self.reports()), 1)
                self.assert_matching()
                self.registry.reconcile(self.snapshot(), now=301 + index * 100)
        with self.store.transaction() as connection:
            connection.execute("DELETE FROM device_claims")
        self.herdr.commands.clear()
        self.registry.reconcile(self.snapshot(), now=500)
        self.assertEqual(len(self.reports()), 1)
        self.assert_matching()

    def test_matching_revision_and_turn_changes_make_zero_runner_calls(self):
        self.initialize()
        original = self.agent()["agent_id"]
        for revision, turn in ((1, "working"), (2, "idle"), (999, "done"), (1000, "working")):
            observed = self.snapshot()
            observed["agents"][0].update(revision=revision, agent_status=turn)
            self.new_registry().reconcile(observed, now=200 + revision)
            self.assertEqual(self.herdr.commands, [])
            self.assertEqual(self.agent()["agent_id"], original)
            self.assertEqual(self.agent()["turn_state"], turn)
            self.assertEqual(self.entries(), {})

    def test_manual_border_is_not_drift_and_does_not_trigger_runner_calls(self):
        self.initialize()
        self.herdr.labels["pane"] = "Human border"
        self.registry.reconcile(self.snapshot(), now=200)
        self.assertEqual(self.herdr.commands, [])
        self.assertEqual(self.entries(), {})
        self.drift("alias")
        self.registry.reconcile(self.snapshot(), now=300)
        self.assert_matching()
        self.assertEqual(self.herdr.labels["pane"], "Human border")
        self.assertFalse(any(command[:2] == ["pane", "rename"] for command in self.herdr.commands))

    def exhaust(self, *, ignored=False):
        self.initialize()
        self.drift()
        self.herdr.metadata_fails = not ignored
        self.herdr.ignore_metadata = ignored
        previous_fingerprint = None
        at = 200.0
        for attempt in range(1, display_repair.MAX_ATTEMPTS + 1):
            self.herdr.commands.clear()
            self.new_registry().reconcile(self.snapshot(), now=at)
            self.assertEqual(len(self.reports()), 1)
            entry = self.entries()["pane"]
            self.assertEqual(entry["attempts"], attempt)
            if previous_fingerprint is not None:
                self.assertEqual(entry["fingerprint"], previous_fingerprint)
            previous_fingerprint = entry["fingerprint"]
            if attempt < display_repair.MAX_ATTEMPTS:
                self.assertEqual(entry["state"], "pending")
                self.assertEqual(entry["next_try_at"], at + display_repair.RETRY_SECONDS * 2 ** (attempt - 1))
                self.assertTrue(worker.maintenance_pending(self.store))
                self.herdr.commands.clear()
                self.new_registry().reconcile(self.snapshot(), now=entry["next_try_at"] - 0.01)
                self.assertEqual(self.herdr.commands, [])
                self.assertEqual(self.entries()["pane"]["attempts"], attempt)
                at = entry["next_try_at"]
        if ignored:
            self.assertEqual(self.entries()["pane"]["state"], "pending")
            self.herdr.commands.clear()
            at = self.entries()["pane"]["next_try_at"]
            self.new_registry().reconcile(self.snapshot(), now=at)
            self.assertEqual(self.herdr.commands, [])
        self.assertEqual(self.entries()["pane"]["state"], "exhausted")
        self.assertFalse(worker.maintenance_pending(self.store))
        self.herdr.commands.clear()
        self.new_registry().reconcile(self.snapshot(), now=at + 10000)
        self.assertEqual(self.herdr.commands, [])
        self.assertEqual(self.entries()["pane"]["attempts"], display_repair.MAX_ATTEMPTS)
        return at

    def test_failures_back_off_and_cap_persists_across_new_registries(self):
        self.exhaust()
        entry = self.entries()["pane"]
        self.assertTrue(entry["last_error"])
        self.assertEqual(self.store.doctor()["display_repair"], self.entries())
        with self.store.read() as connection:
            saved = connection.execute("SELECT value FROM ledger_meta WHERE key=?", (display_repair.KEY,)).fetchone()
        self.assertEqual(json.loads(saved[0]), self.entries())

    def test_successful_but_ignored_commands_also_back_off_and_exhaust(self):
        self.exhaust(ignored=True)
        self.assertEqual(self.herdr.metadata["pane"][SOURCE]["display_agent"], "foreign value")

    def test_explicit_unchanged_status_cannot_bypass_exhausted_budget(self):
        at = self.exhaust()
        exhausted = self.entries()["pane"]
        self.herdr.metadata_fails = False
        for event in range(3):
            with self.subTest(event=event):
                self.herdr.commands.clear()
                with mock.patch("ham.ledger.status.time.time", return_value=at + 20000 + event):
                    result = self.new_registry().set_status(
                        self.agent(), phase="", summary="", feature_invoked=True)
                self.assertEqual(result["display"], {"updated": [], "failed": ["pane"]})
                self.assertEqual(self.herdr.commands, [])
                self.assertEqual(self.entries()["pane"], exhausted)
                self.assertEqual(self.entries()["pane"]["attempts"], display_repair.MAX_ATTEMPTS)
                self.assertFalse(worker.maintenance_pending(self.store))

    def test_exhaustion_resets_only_for_changed_desired_projection(self):
        at = self.exhaust()
        fingerprint = self.entries()["pane"]["fingerprint"]
        # Different foreign values, revisions and turn state do not create a new budget.
        self.drift(value="different foreign value")
        observed = self.snapshot()
        observed["agents"][0].update(revision=999, agent_status="working")
        self.new_registry().reconcile(observed, now=at + 20000)
        self.assertEqual(self.herdr.commands, [])
        self.assertEqual(self.entries()["pane"]["fingerprint"], fingerprint)
        self.herdr.metadata_fails = False
        with mock.patch("ham.ledger.status.time.time", return_value=at + 30000):
            result = self.registry.set_status(self.agent(), phase="review", summary="New desired task")
        self.assertEqual(result["display"]["failed"], [])
        entry = self.entries()["pane"]
        self.assertNotEqual(entry["fingerprint"], fingerprint)
        self.assertEqual((entry["attempts"], entry["state"]), (1, "pending"))
        self.assertTrue(worker.maintenance_pending(self.store))
        self.registry.reconcile(self.snapshot(), now=at + 30001)
        self.assertEqual(self.entries(), {})

    def test_generation_binding_change_resets_exhausted_budget(self):
        at = self.exhaust()
        fingerprint = self.entries()["pane"]["fingerprint"]
        self.template["agents"][0]["agent_session"]["value"] = "replacement"
        self.registry.reconcile(self.snapshot(), now=at + 10001)
        entry = self.entries()["pane"]
        self.assertNotEqual(entry["fingerprint"], fingerprint)
        self.assertEqual((entry["attempts"], entry["state"]), (1, "pending"))
        self.assertEqual(len(self.reports()), 1)

    def test_direct_status_failure_queues_worker_retry(self):
        self.initialize()
        self.herdr.metadata_fails = True
        with mock.patch("ham.ledger.status.time.time", return_value=200):
            result = self.registry.set_status(self.agent(), phase="review", summary="Committed task")
        self.assertEqual(result["display"]["failed"], ["pane"])
        self.assertEqual(self.agent()["summary"], "Committed task")
        entry = self.entries()["pane"]
        self.assertEqual((entry["attempts"], entry["state"]), (1, "pending"))
        self.assertTrue(worker.maintenance_pending(self.store))
        self.herdr.metadata_fails = False
        self.herdr.commands.clear()
        self.new_registry().reconcile(self.snapshot(), now=entry["next_try_at"])
        self.assertEqual(len(self.reports()), 1)
        self.assert_matching()
        self.assertEqual(self.entries()["pane"]["state"], "pending")
        self.registry.reconcile(self.snapshot(), now=entry["next_try_at"] + 1)
        self.assertEqual(self.entries(), {})
        self.assertFalse(worker.maintenance_pending(self.store))

    def test_two_handlers_racing_metadata_eventually_converge(self):
        self.initialize()
        self.drift("recap")
        # Another handler wins the final write after this handler's successful command.
        self.herdr.after_report = lambda: self.drift("color", "other-handler-color")
        self.registry.reconcile(self.snapshot(), now=200)
        self.assertEqual(self.herdr.metadata["pane"]["tokens"]["color"], "other-handler-color")
        pending = self.entries()["pane"]
        self.assertEqual(pending["state"], "pending")
        self.herdr.commands.clear()
        self.new_registry().reconcile(self.snapshot(), now=pending["next_try_at"])
        self.assertEqual(len(self.reports()), 1)
        self.assert_matching()
        self.herdr.commands.clear()
        self.registry.reconcile(self.snapshot(), now=pending["next_try_at"] + 1)
        self.assertEqual(self.entries(), {})
        self.assertEqual(self.herdr.commands, [])

    def test_identity_only_fallback_race_restores_stored_role_and_recap(self):
        self.initialize()
        RepoSettings(self.store, display_runner=self.herdr).mutate(self.identity["repo_id"], "mode", "auto")
        with mock.patch("ham.ledger.status.time.time", return_value=150):
            self.registry.set_status(self.agent(), phase="review", summary="Authoritative task")
        self.registry.reconcile(self.snapshot(), now=151)
        self.assertEqual(self.entries(), {})
        self.assertIn("[메인]", self.herdr.metadata["pane"][SOURCE]["display_agent"])
        self.assertEqual(self.herdr.metadata["pane"]["tokens"]["recap"], "review")

        def fallback():
            self.assertTrue(report_metadata(
                self.herdr, "pane", build_display("omp", "orange-rabbit")))

        self.drift("alias")
        self.herdr.after_report = fallback
        self.herdr.commands.clear()
        self.registry.reconcile(self.snapshot(), now=200)
        self.assertEqual(len(self.reports()), 2)
        self.assertNotIn("[메인]", self.herdr.metadata["pane"][SOURCE]["display_agent"])
        self.assertEqual(self.herdr.metadata["pane"]["tokens"]["recap"], "")
        self.assertEqual((self.agent()["role"], self.agent()["phase"]), ("main", "review"))
        pending = self.entries()["pane"]
        self.herdr.commands.clear()
        self.new_registry().reconcile(self.snapshot(), now=pending["next_try_at"])
        self.assertEqual(len(self.writes()), 1)
        self.assert_matching()
        self.assertIn("[메인]", self.herdr.metadata["pane"][SOURCE]["display_agent"])
        self.assertEqual(self.herdr.metadata["pane"]["tokens"]["recap"], "review")
        self.herdr.commands.clear()
        self.registry.reconcile(self.snapshot(), now=pending["next_try_at"] + 1)
        self.assertEqual(self.entries(), {})
        self.assertEqual(self.herdr.commands, [])

    def test_shell_device_cleanup_failure_retries_until_fresh_snapshot_confirms(self):
        self.initialize()
        self.template["agents"] = []
        self.herdr.metadata["pane"]["tokens"].update(phone="stale phone", emu="stale emulator")
        self.herdr.metadata_fails = True
        self.registry.reconcile(self.snapshot(), now=200)
        self.assertEqual(self.registry.list(), [])
        pending = self.entries()["pane"]
        self.assertEqual((pending["attempts"], pending["state"]), (1, "pending"))
        self.assertTrue(worker.maintenance_pending(self.store))
        self.herdr.metadata_fails = False
        self.herdr.commands.clear()
        self.new_registry().reconcile(self.snapshot(), now=pending["next_try_at"])
        self.assertEqual(self.writes(), [[
            "pane", "report-metadata", "pane", "--source", SOURCE,
            "--clear-token", "phone", "--clear-token", "emu",
        ]])
        self.assertNotIn("phone", self.herdr.metadata["pane"]["tokens"])
        self.assertNotIn("emu", self.herdr.metadata["pane"]["tokens"])
        self.assertEqual(self.herdr.metadata["pane"]["tokens"]["other"], "keep")
        self.assertEqual(self.entries()["pane"]["state"], "pending")
        self.herdr.commands.clear()
        self.registry.reconcile(self.snapshot(), now=pending["next_try_at"] + 1)
        self.assertEqual(self.entries(), {})
        self.assertEqual(self.herdr.commands, [])
        self.assertFalse(worker.maintenance_pending(self.store))

    def test_direct_shell_failure_and_snapshot_retry_share_binding_fingerprint(self):
        self.initialize()
        self.template["agents"] = []
        self.registry.reconcile(self.snapshot(), now=190, refresh_display=False)
        self.assertEqual(self.registry.list(), [])
        self.herdr.metadata["pane"]["tokens"].update(phone="stale phone", emu="stale emulator")
        self.herdr.metadata_fails = True
        result = self.registry._refresh_display({"pane"}, feature_invoked=True, now=200)
        self.assertEqual(result, {"updated": [], "failed": ["pane"]})
        pending = self.entries()["pane"]
        self.assertEqual((pending["attempts"], pending["state"]), (1, "pending"))
        self.assertTrue(worker.maintenance_pending(self.store))
        self.herdr.metadata_fails = False
        self.herdr.commands.clear()
        self.new_registry().reconcile(self.snapshot(), now=pending["next_try_at"])
        retried = self.entries()["pane"]
        self.assertEqual(retried["fingerprint"], pending["fingerprint"])
        self.assertEqual((retried["attempts"], retried["state"]), (2, "pending"))
        self.assertEqual(self.writes(), [[
            "pane", "report-metadata", "pane", "--source", SOURCE,
            "--clear-token", "phone", "--clear-token", "emu",
        ]])
        self.herdr.commands.clear()
        self.registry.reconcile(self.snapshot(), now=pending["next_try_at"] + 1)
        self.assertEqual(self.entries(), {})
        self.assertEqual(self.herdr.commands, [])
        self.assertFalse(worker.maintenance_pending(self.store))

    def test_unreported_generation_carries_exact_owned_border(self):
        old = self.initialize(unreported=True)
        self.registry.set_status(old, phase="review", summary="Historical task")
        owned = self.agent()["last_ham_label"]
        self.template["agents"][0]["agent_session"] = {"value": "real-session"}
        self.registry.reconcile(self.snapshot(), now=300, refresh_display=False)
        current = self.agent()
        self.assertNotEqual(current["agent_id"], old["agent_id"])
        self.assertEqual(current["generation"], 2)
        self.assertEqual(current["last_ham_label"], owned)
        self.registry.set_status(current, phase="review", summary="New generation task")
        self.assertEqual(self.herdr.labels["pane"], "orange-rabbit · New generation task")

    def test_generation_does_not_carry_ownership_after_manual_rename(self):
        old = self.initialize(unreported=True)
        self.registry.set_status(old, phase="review", summary="Historical task")
        self.herdr.labels["pane"] = "Human title"
        self.template["agents"][0]["agent_session"] = {"value": "real-session"}
        self.registry.reconcile(self.snapshot(), now=300)
        self.assertIsNone(self.agent()["last_ham_label"])
        self.registry.set_status(self.agent(), phase="review", summary="New generation task")
        self.assertEqual(self.herdr.labels["pane"], "Human title")

    def lost_ownership(self):
        old = self.initialize(unreported=True)
        self.registry.set_status(old, phase="review", summary="Historical task")
        owned = self.agent()["last_ham_label"]
        self.template["agents"][0]["agent_session"] = {"value": "real-session"}
        self.registry.reconcile(self.snapshot(), now=300, refresh_display=False)
        with self.store.transaction() as connection:
            connection.execute("UPDATE agents SET last_ham_label=NULL WHERE active=1")
        self.herdr.commands.clear()
        return old, owned

    def test_existing_null_ownership_recovers_exact_historical_label(self):
        _, owned = self.lost_ownership()
        self.assertEqual(self.herdr.labels["pane"], owned)
        self.registry.reconcile(self.snapshot(), now=400)
        self.assertEqual(self.herdr.labels["pane"], "orange-rabbit")
        self.assertEqual(self.agent()["last_ham_label"], "orange-rabbit")
        self.registry.set_status(self.agent(), phase="review", summary="Recovered")
        self.assertEqual(self.herdr.labels["pane"], "orange-rabbit · Recovered")

    def test_prefix_only_historical_border_is_not_owned(self):
        _, owned = self.lost_ownership()
        self.herdr.labels["pane"] = owned + " — human suffix"
        self.registry.reconcile(self.snapshot(), now=400)
        self.assertEqual(self.herdr.labels["pane"], owned + " — human suffix")
        self.assertIsNone(self.agent()["last_ham_label"])

    def test_historical_ownership_does_not_cross_host_terminal_or_pane(self):
        old, owned = self.lost_ownership()
        for column in ("host", "terminal_id", "pane_id"):
            with self.subTest(binding=column):
                with self.store.transaction() as connection:
                    original = connection.execute(
                        f"SELECT {column} FROM agents WHERE agent_id=?", (old["agent_id"],)).fetchone()[0]
                    connection.execute(f"UPDATE agents SET {column}=? WHERE agent_id=?", ("wrong-binding", old["agent_id"]))
                    connection.execute("UPDATE agents SET last_ham_label=NULL WHERE active=1")
                self.herdr.labels["pane"] = owned
                self.registry.reconcile(self.snapshot(), now=400)
                self.assertEqual(self.herdr.labels["pane"], owned)
                self.assertIsNone(self.agent()["last_ham_label"])
                with self.store.transaction() as connection:
                    connection.execute(f"UPDATE agents SET {column}=? WHERE agent_id=?", (original, old["agent_id"]))

    def test_live_wrong_terminal_binding_never_recovers_or_renames(self):
        _, owned = self.lost_ownership()
        self.herdr.terminals["pane"] = "different-live-terminal"
        self.assertFalse(LedgerDisplay(self.store, runner=self.herdr).refresh_agent(self.agent()))
        self.assertEqual(self.herdr.labels["pane"], owned)
        self.assertIsNone(self.agent()["last_ham_label"])
        self.assertFalse(any(command[:2] == ["pane", "rename"] for command in self.herdr.commands))

    def test_current_equals_desired_records_null_ownership_without_rename(self):
        self.initialize()
        self.herdr.labels["pane"] = "orange-rabbit"
        with self.store.transaction() as connection:
            connection.execute("UPDATE agents SET last_ham_label=NULL WHERE active=1")
        self.assertTrue(LedgerDisplay(self.store, runner=self.herdr).refresh_agent(self.agent()))
        self.assertEqual(self.agent()["last_ham_label"], "orange-rabbit")
        self.assertFalse(any(command[:2] == ["pane", "rename"] for command in self.herdr.commands))
        self.registry.set_status(self.agent(), phase="review", summary="CAS now owns this border")
        self.assertEqual(self.herdr.labels["pane"], "orange-rabbit · CAS now owns this border")

    def test_metadata_matches_accepts_herdr_normalized_text(self):
        from dataclasses import replace

        base = build_display("omp", "name", phone="", emu="")
        cases = (
            ("  trimmed value  ", "trimmed value"),
            ("left" + chr(0) + "right" + chr(7), "leftright"),
            ("left" + chr(0x85) + "right", "leftright"),
            ("  " + chr(0) + " " + "x" * 79 + "suffix", "x" * 79),
            ("x" * 79 + " trailing text", "x" * 79),
            ("키😀" * 45, ("키😀" * 45)[:80]),
        )
        for field in ("display_agent", "alias", "color", "recap", "phone", "emu"):
            for raw, normalized in cases:
                with self.subTest(field=field, raw=raw):
                    desired = replace(base, **{field: raw})
                    observed = {
                        "display_agent": base.display_agent,
                        "tokens": {key: getattr(base, key)
                                   for key in ("alias", "color", "recap", "phone", "emu")},
                    }
                    if field == "display_agent":
                        observed[field] = normalized
                    else:
                        observed["tokens"][field] = normalized
                    self.assertTrue(metadata_matches(desired, observed))
                    if field == "display_agent":
                        observed[field] = normalized + "!"
                    else:
                        observed["tokens"][field] = normalized + "!"
                    self.assertFalse(metadata_matches(desired, observed))

    def test_current_equals_desired_concurrent_cas_save_uses_actual_saved_owner(self):
        self.initialize()
        desired = "orange-rabbit"
        self.herdr.labels["pane"] = desired
        for competing_label, metadata_fails, expected in (
                (desired, False, True), ("Other owner's border", False, False), (desired, True, False)):
            with self.subTest(competing_label=competing_label, metadata_fails=metadata_fails):
                with self.store.transaction() as connection:
                    connection.execute("UPDATE agents SET last_ham_label=NULL WHERE active=1")
                self.herdr.commands.clear()
                self.herdr.metadata_fails = metadata_fails
                display = LedgerDisplay(self.store, runner=self.herdr)
                stale = self.agent()
                committed = self.registry.set_display_label(
                    stale["agent_id"], stale["last_ham_label"], competing_label)
                self.assertTrue(committed["updated"])
                self.assertEqual(display.refresh_agent(stale), expected)
                self.assertEqual(self.agent()["last_ham_label"], competing_label)
                self.assertFalse(any(command[:2] == ["pane", "rename"] for command in self.herdr.commands))

    def test_full_one_pass_worker_uses_one_snapshot_and_only_repairs_drift(self):
        self.initialize()
        # Participation keeps a steady worker sweep eligible even without repairs.
        RepoSettings(self.store, display_runner=self.herdr).mutate(self.identity["repo_id"], "mode", "auto")
        self.registry.reconcile(self.snapshot(), now=200)
        environment = {"HOME": str(self.root), "PATH": "/usr/bin:/bin"}
        remote = mock.Mock()
        remote.pump.return_value = {"sent": 0, "failed": 0, "deferred": 0}
        for drifted in (False, True):
            with self.subTest(drifted=drifted):
                if drifted:
                    self.drift("alias")
                self.herdr.commands.clear()
                with mock.patch.object(worker, "worker_environment", return_value=environment), \
                        mock.patch.object(worker, "AgentRegistry", side_effect=lambda store: self.new_registry()), \
                        mock.patch("ham.ledger.status.live_snapshot", side_effect=lambda environment: self.snapshot()) as snapshot, \
                        mock.patch("ham.ledger.runtime.NativeReceiver") as receiver, \
                        mock.patch("ham.ledger.native_wake.NativeWaker") as waker, \
                        mock.patch("ham.agents.request_lifecycle.prune_request_records"), \
                        mock.patch.object(self.store, "retention"), \
                        mock.patch("sys.stderr"):
                    result = worker.run_worker(self.store, environment, once=True, remote=remote)
                self.assertEqual(result["reason"], "one_pass")
                snapshot.assert_called_once_with(environment)
                receiver.return_value.observe_claude.assert_called_once_with()
                waker.return_value.sweep.assert_called_once_with()
                self.assertEqual(len(self.reports()), int(drifted))
                self.assertEqual(len(self.writes()), int(drifted))
                if not drifted:
                    self.assertEqual(self.herdr.commands, [])
                self.assert_matching()


if __name__ == "__main__":
    unittest.main()
