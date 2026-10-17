"""Registry mutations project committed rows through the shared display boundary."""

import copy
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from ledger_display import LEGACY_SOURCE, RECAP_SOURCE, SOURCE
from ledger_settings import RepoSettings
from ledger_status import AgentRegistry
from ledger_store import LedgerStore
from test_ledger_display import HerdrDisplay


class LedgerStatusDisplayTest(unittest.TestCase):

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.store = LedgerStore(self.root / "ledger", canonical_name="status-display-test")
        self.herdr = HerdrDisplay(["p-current", "p-moved"])
        self.observations = []
        self.registry = AgentRegistry(self.store, display_runner=self.capture)
        pane = {"terminal_id": "terminal", "pane_id": "p-current", "workspace_id": "workspace"}
        self.snapshot = {"panes": [pane], "agents": [
            {**pane, "agent": "omp", "name": "orange-rabbit", "agent_status": "idle",
             "agent_session": {"value": "session"}, "cwd": str(self.repo)},
        ]}

    def capture(self, command):
        # A separate connection sees only committed mutations. No refresh or
        # reconciliation is needed to obtain the projector's authoritative rows.
        with self.store.read() as connection:
            rows = [dict(row) for row in connection.execute(
                "SELECT a.*,s.turn_state,s.phase,s.summary FROM agents a "
                "JOIN agent_status s USING(agent_id) WHERE a.active=1"
            )]
        self.herdr.terminals.update({row["pane_id"]: row["terminal_id"] for row in rows})
        self.observations.append((list(command), rows))
        return self.herdr(command)

    def seed(self):
        self.registry.reconcile(self.snapshot, refresh_display=False)
        self.assertEqual(self.herdr.commands, [])
        return self.registry.list()[0]

    def clear_commands(self):
        self.herdr.commands.clear()
        self.observations.clear()

    def reports(self):
        return [command for command in self.herdr.commands
                if command[:2] == ["pane", "report-metadata"] and command[4] == SOURCE]

    def test_new_snapshot_rows_are_committed_before_shared_redraw(self):
        result = self.registry.reconcile(self.snapshot)
        self.assertEqual(result["terminals"], 1)
        self.assertEqual(len(self.reports()), 1)
        agent = result["agents"][0]
        for command, rows in self.observations:
            self.assertEqual(len(rows), 1, command)
            self.assertEqual(rows[0]["agent_id"], agent["agent_id"])
            self.assertEqual(rows[0]["pane_id"], "p-current")
            self.assertEqual(rows[0]["turn_state"], "idle")
        self.assertEqual(self.herdr.metadata["p-current"][SOURCE]["tokens"]["alias"], "orange-rabbit")

    def test_status_redraw_observes_fresh_phase_summary_and_log(self):
        agent = self.seed()
        with mock.patch.object(AgentRegistry, "refresh", side_effect=AssertionError("recursive refresh")), \
                mock.patch.object(AgentRegistry, "reconcile", side_effect=AssertionError("recursive reconcile")):
            result = self.registry.set_status(agent, phase="review", summary="Inspect committed status")
        self.assertEqual(result["agent_id"], agent["agent_id"])
        self.assertEqual(result["display"], {"updated": ["p-current"], "failed": []})
        for command, rows in self.observations:
            self.assertEqual((rows[0]["phase"], rows[0]["summary"]),
                             ("review", "Inspect committed status"), command)
        metadata = self.herdr.metadata["p-current"][SOURCE]
        self.assertEqual(metadata["tokens"]["recap"], "review")
        self.assertEqual(self.herdr.labels["p-current"], "Inspect committed status")
        with self.store.read() as connection:
            status = [tuple(row) for row in connection.execute(
                "SELECT field,value FROM status_log WHERE field IN ('phase','summary') ORDER BY seq"
            )]
        self.assertEqual(status, [("phase", "review"), ("summary", "Inspect committed status")])

    def test_unchanged_status_and_snapshot_never_redraw(self):
        agent = self.seed()
        self.registry.set_status(agent, phase="review", summary="Stable")
        self.clear_commands()
        result = self.registry.set_status(agent, phase="review", summary="Stable")
        self.assertNotIn("display", result)
        self.registry.reconcile(self.snapshot, now=100)
        self.registry.reconcile(self.snapshot, now=200)
        self.assertEqual(self.herdr.commands, [])

    def test_same_source_metadata_and_detection_revisions_do_not_feedback(self):
        self.registry.reconcile(self.snapshot)
        self.clear_commands()
        updated = copy.deepcopy(self.snapshot)
        raw = updated["agents"][0]
        raw.update({"revision": 42, "display_agent": "🟧 orange-rabbit",
                    "tokens": {"alias": "orange-rabbit", "color": "orange", "recap": "review"}})
        self.registry.reconcile(updated)
        raw["revision"] = 43
        self.registry.reconcile(updated)
        self.assertEqual(self.herdr.commands, [])
        self.assertEqual(self.registry.list()[0]["generation"], 1)

    def test_alias_change_projects_once_without_new_session(self):
        agent = self.seed()
        updated = copy.deepcopy(self.snapshot)
        updated["agents"][0]["name"] = "blue-fox"
        self.registry.reconcile(updated)
        self.assertEqual(len(self.reports()), 1)
        self.assertEqual(self.herdr.metadata["p-current"][SOURCE]["tokens"]["alias"], "blue-fox")
        self.assertTrue(all(rows[0]["alias"] == "blue-fox" for _, rows in self.observations))
        self.assertEqual(self.registry.list()[0]["agent_id"], agent["agent_id"])
        self.clear_commands()
        self.registry.reconcile(updated)
        self.assertEqual(self.herdr.commands, [])

    def test_turn_state_change_projects_committed_row_once(self):
        self.seed()
        updated = copy.deepcopy(self.snapshot)
        updated["agents"][0]["agent_status"] = "working"
        self.registry.reconcile(updated)
        self.assertEqual(len(self.reports()), 1)
        self.assertTrue(all(rows[0]["turn_state"] == "working" for _, rows in self.observations))
        self.clear_commands()
        self.registry.reconcile(updated)
        self.assertEqual(self.herdr.commands, [])

    def moved_snapshot(self):
        updated = copy.deepcopy(self.snapshot)
        for raw in [*updated["panes"], *updated["agents"]]:
            raw.update(pane_id="p-moved", workspace_id="moved-workspace")
        return updated

    def test_location_update_projects_current_pane_preserving_session(self):
        agent = self.seed()
        self.registry.reconcile(self.moved_snapshot())
        self.assertEqual(len(self.reports()), 1)
        self.assertTrue(all(command[2] == "p-moved" for command in self.herdr.commands))
        self.assertTrue(all(rows[0]["pane_id"] == "p-moved" for _, rows in self.observations))
        current = self.registry.list()[0]
        self.assertEqual((current["agent_id"], current["generation"]), (agent["agent_id"], 1))

    def test_status_uses_committed_location_not_stale_caller_pane(self):
        agent = self.seed()
        self.registry.reconcile(self.moved_snapshot())
        self.clear_commands()
        result = self.registry.set_status(agent, phase="review", summary="Moved task")
        self.assertEqual(result["display"], {"updated": ["p-moved"], "failed": []})
        self.assertTrue(all(command[2] == "p-moved" for command in self.herdr.commands))
        self.assertEqual(self.herdr.labels["p-moved"], "Moved task")

    def test_session_replacement_redraw_sees_only_new_committed_generation(self):
        old = self.seed()
        updated = self.moved_snapshot()
        updated["agents"][0]["agent_session"]["value"] = "replacement"
        self.registry.reconcile(updated)
        current = self.registry.list()[0]
        self.assertNotEqual(current["agent_id"], old["agent_id"])
        self.assertEqual(current["generation"], 2)
        self.assertEqual(len(self.reports()), 1)
        for command, rows in self.observations:
            self.assertEqual(len(rows), 1, command)
            self.assertEqual(rows[0]["agent_id"], current["agent_id"])
            self.assertEqual(rows[0]["session_ref"], "replacement")
            self.assertEqual(command[2], "p-moved")

    def test_runtime_refresh_projects_committed_snapshot_without_recursion(self):
        refresh = self.registry.refresh
        with mock.patch("ledger_status.live_snapshot", return_value=self.snapshot), \
                mock.patch.object(AgentRegistry, "refresh", side_effect=AssertionError("recursive refresh")):
            _, result = refresh({"HERDR_TEST": "runtime"})
        self.assertEqual(result["terminals"], 1)
        self.assertEqual(result["agents"][0]["session_ref"], "session")
        self.assertEqual(self.herdr.metadata["p-current"][SOURCE]["tokens"]["alias"], "orange-rabbit")

    def second_agent_snapshot(self):
        updated = copy.deepcopy(self.snapshot)
        pane = {"terminal_id": "second-terminal", "pane_id": "p-moved", "workspace_id": "workspace"}
        updated["panes"].append(pane)
        updated["agents"].append({
            **pane, "agent": "omp", "name": "blue-fox", "agent_status": "idle",
            "agent_session": {"value": "second-session"}, "cwd": str(self.repo),
        })
        return updated

    def test_first_refresh_with_second_auto_agent_removes_stale_main_role(self):
        agent = self.seed()
        settings = RepoSettings(self.store, display_runner=self.capture)
        settings.mutate(agent["repo_id"], "mode", "auto")
        self.assertIn("[메인]", self.herdr.metadata["p-current"][SOURCE]["display_agent"])
        with mock.patch("ledger_status.live_snapshot", return_value=self.second_agent_snapshot()):
            _, result = self.registry.refresh()
        self.assertEqual(set(result), {"agents", "terminals"})
        with self.store.read() as connection:
            self.assertIsNone(settings.main(connection, agent["repo_id"]))
        for pane_id in ("p-current", "p-moved"):
            self.assertIn("[워커]", self.herdr.metadata[pane_id][SOURCE]["display_agent"])
            self.assertNotIn("[메인]", self.herdr.metadata[pane_id][SOURCE]["display_agent"])

    def test_first_refresh_after_explicit_main_closes_projects_surviving_auto_main(self):
        self.registry.reconcile(self.second_agent_snapshot(), refresh_display=False)
        agents = self.registry.list()
        repo_id = agents[0]["repo_id"]
        settings = RepoSettings(self.store, display_runner=self.capture)
        settings.mutate(repo_id, "mode", "auto")
        settings.mutate(repo_id, "main", {"host": self.store.host, "terminal_id": "terminal"})
        self.assertIn("[워커]", self.herdr.metadata["p-moved"][SOURCE]["display_agent"])
        remaining = self.second_agent_snapshot()
        remaining["panes"] = remaining["panes"][1:]
        remaining["agents"] = remaining["agents"][1:]
        with mock.patch("ledger_status.live_snapshot", return_value=remaining):
            self.registry.refresh()
        self.assertIsNone(settings.show(repo_id)["settings"]["main"]["value"])
        self.assertIn("[메인]", self.herdr.metadata["p-moved"][SOURCE]["display_agent"])
        self.assertNotIn("[워커]", self.herdr.metadata["p-moved"][SOURCE]["display_agent"])

    def test_public_explicit_status_suppresses_initial_redraw_and_projects_once(self):
        with mock.patch("ledger_status.live_snapshot", return_value=self.snapshot) as snapshot:
            observed, _ = self.registry.refresh(refresh_display=False)
        self.assertIs(observed, self.snapshot)
        snapshot.assert_called_once_with(None)
        self.assertEqual(self.herdr.commands, [])
        agent = self.registry.list()[0]
        with mock.patch.object(AgentRegistry, "refresh", side_effect=AssertionError("duplicate scan")), \
                mock.patch.object(AgentRegistry, "reconcile", side_effect=AssertionError("duplicate reconcile")):
            result = self.registry.set_status(agent, phase="", summary="", feature_invoked=True)
        self.assertEqual(result["display"], {"updated": ["p-current"], "failed": []})
        self.assertEqual(len(self.reports()), 1)
        self.assertNotIn("recap", self.herdr.metadata["p-current"][RECAP_SOURCE]["tokens"])
        self.assertEqual(self.herdr.metadata["p-current"][RECAP_SOURCE]["tokens"]["other"], "keep")
        self.assertEqual(self.herdr.metadata["p-current"]["device"]["tokens"]["phone"], "serial")
        self.assertNotIn("alias", self.herdr.metadata["p-current"][LEGACY_SOURCE]["tokens"])

    def test_default_display_runner_uses_explicit_host_environment(self):
        self.seed()
        environment = {"HERDR_TEST": "selected-environment"}
        registry = AgentRegistry(self.store)
        with mock.patch("ledger_status.herdr_executable", return_value="selected-herdr") as executable, \
                mock.patch("ledger_status.run_bounded_command",
                           side_effect=lambda command, **kwargs: self.capture(command[1:])) as run:
            registry.set_status(self.registry.list()[0], phase="review", summary="Selected host",
                                environment=environment)
        self.assertTrue(run.called)
        executable.assert_called_with(environment)
        for call in run.call_args_list:
            self.assertEqual(call.args[0][0], "selected-herdr")
            self.assertEqual(call.kwargs, {"timeout": 10, "environment": environment})

    def test_projection_failure_does_not_fail_committed_status(self):
        agent = self.seed()
        self.herdr.metadata_fails = True
        result = self.registry.set_status(agent, phase="review", summary="Durable despite redraw failure")
        self.assertEqual(result["display"]["failed"], ["p-current"])
        self.assertEqual(self.registry.list()[0]["summary"], "Durable despite redraw failure")
        warnings = self.store.annotate(result)["warnings"]
        self.assertEqual(warnings[0]["code"], "notification_failed")
        self.assertTrue(warnings[0]["committed"])
        self.assertIn("display refresh", self.store.doctor()["notification_failures"][0]["detail"])

    def test_unexpected_projection_exception_is_nonfatal_after_commit(self):
        agent = self.seed()
        registry = AgentRegistry(self.store, display_runner=mock.Mock(side_effect=RuntimeError("projector failed")))
        result = registry.set_status(agent, phase="review", summary="Still committed")
        self.assertEqual(result["display"], {"updated": [], "failed": ["p-current"]})
        self.assertEqual(self.registry.list()[0]["summary"], "Still committed")
        self.assertIn("projector failed", self.store.notification_errors[0]["message"])
        self.assertTrue(self.store.doctor()["notification_failures"])

    def test_failed_diagnostic_persistence_cannot_undo_status(self):
        agent = self.seed()
        self.herdr.metadata_fails = True
        with mock.patch.object(self.store, "note_notification_failure", side_effect=OSError("disk unavailable")):
            result = self.registry.set_status(agent, phase="review", summary="Committed before diagnostics")
        self.assertEqual(result["display"]["failed"], ["p-current"])
        self.assertEqual(self.registry.list()[0]["summary"], "Committed before diagnostics")
        self.assertTrue(self.store.notification_errors[0]["committed"])

    def test_background_snapshot_projection_exception_does_not_fail_commit(self):
        self.registry.display_runner = mock.Mock(side_effect=RuntimeError("snapshot redraw failed"))
        result = self.registry.reconcile(self.snapshot)
        self.assertEqual(result["agents"][0]["session_ref"], "session")
        self.assertTrue(self.store.notification_errors[0]["committed"])
        self.assertTrue(self.store.doctor()["notification_failures"])

    def test_shared_projector_preserves_manual_border_and_saved_cas_label(self):
        agent = self.seed()
        self.registry.set_status(agent, phase="review", summary="HAM title")
        self.herdr.labels["p-current"] = "Human title"
        self.registry.set_status(agent, phase="review", summary="New HAM title")
        self.assertEqual(self.herdr.labels["p-current"], "Human title")
        self.assertEqual(self.registry.list()[0]["last_ham_label"], "HAM title")
        self.assertEqual(self.herdr.metadata["p-current"][SOURCE]["tokens"]["recap"], "review")

    def test_successful_blank_border_ownership_preserves_exact_cas_value(self):
        agent = self.seed()
        self.registry.set_status(agent, phase="review", summary="HAM title")
        self.registry.set_status(agent, phase="", summary="")
        self.assertEqual(self.herdr.labels["p-current"], "")
        self.assertEqual(self.registry.list()[0]["last_ham_label"], "")
        self.registry.set_status(agent, phase="review", summary="Owned after clear")
        self.assertEqual(self.herdr.labels["p-current"], "Owned after clear")
        self.herdr.labels["p-current"] = "Later human rename"
        self.registry.set_status(agent, phase="review", summary="Do not replace human title")
        self.assertEqual(self.herdr.labels["p-current"], "Later human rename")
        self.assertEqual(self.registry.list()[0]["last_ham_label"], "Owned after clear")


if __name__ == "__main__":
    unittest.main()
