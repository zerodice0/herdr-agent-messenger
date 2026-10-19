#!/usr/bin/env python3

import ham_test_support  # Protect the live ledger before product imports.
from contextlib import redirect_stderr, redirect_stdout
import importlib.util
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock


MODULE_PATH = Path(__file__).with_name("agent_labels.py")
MANIFEST_PATH = Path(__file__).with_name("herdr-plugin.toml")
SPEC = importlib.util.spec_from_file_location("agent_labels", MODULE_PATH)
assert SPEC and SPEC.loader
agent_labels = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(agent_labels)


class AgentLabelsTest(ham_test_support.IsolatedLedgerTest):
    def test_run_herdr_uses_injected_binary(self):
        completed = mock.Mock(returncode=0, stdout="", stderr="")
        with tempfile.TemporaryDirectory() as directory:
            binary = Path(directory, "herdr")
            binary.write_text("#!/bin/sh\n", encoding="utf-8")
            binary.chmod(0o700)
            with (
                mock.patch.dict(os.environ, {"HERDR_BIN_PATH": os.fspath(binary)}),
                mock.patch.object(
                    agent_labels.subprocess,
                    "run",
                    return_value=completed,
                ) as run,
            ):
                self.assertIs(agent_labels.run_herdr("agent", "list"), completed)

        run.assert_called_once_with(
            [os.fspath(binary), "agent", "list"],
            stdin=agent_labels.subprocess.DEVNULL,
            capture_output=True,
            check=False,
            text=True,
            timeout=10,
        )

    def test_command_launch_failure_and_timeout_are_reportable_results(self):
        failures = (
            OSError("herdr unavailable"),
            agent_labels.subprocess.TimeoutExpired(["herdr", "agent", "list"], 10),
        )
        for error in failures:
            with self.subTest(error=error), mock.patch.object(
                agent_labels.subprocess, "run", side_effect=error,
            ):
                result = agent_labels.run_herdr("agent", "list")
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stderr, str(error))

    def test_aliases_are_valid_and_unique(self):
        names = [alias.name for alias in agent_labels.ALIASES]
        self.assertEqual(len(names), 256)
        self.assertEqual(len(names), len(set(names)))
        self.assertTrue(all(len(name) <= 32 and name.replace("-", "").islower() for name in names))

    def test_candidates_visit_every_alias(self):
        candidates = list(agent_labels.candidates("term-1:session-1"))
        self.assertEqual(len(candidates), len(agent_labels.ALIASES))
        self.assertEqual(set(candidates), set(agent_labels.ALIASES))

    def test_conflict_retries_without_listing_agents(self):
        calls = []

        def fake_run(*args):
            calls.append(args)
            if args[:2] == ("agent", "get"):
                return mock.Mock(
                    returncode=0,
                    stdout='{"result":{"agent":{"agent":"codex","terminal_id":"term-1"}}}',
                    stderr="",
                )
            if args[:2] == ("agent", "rename") and sum(
                call[:2] == ("agent", "rename") for call in calls
            ) == 1:
                return mock.Mock(returncode=1, stdout="", stderr="agent_name_taken")
            return mock.Mock(returncode=0, stdout="{}", stderr="")

        output = io.StringIO()
        with (
            mock.patch.object(agent_labels, "run_herdr", side_effect=fake_run),
            redirect_stdout(output),
        ):
            self.assertEqual(agent_labels.assign_label("w1:p2"), 0)

        self.assertFalse(any(call[:2] == ("agent", "list") for call in calls))
        self.assertEqual(sum(call[:2] == ("agent", "rename") for call in calls), 2)
        renamed = [call for call in calls if call[:2] == ("agent", "rename")]
        self.assertEqual(output.getvalue(), f"{renamed[-1][3]}\n")
        self.assertTrue(any(call[:2] == ("pane", "report-metadata") for call in calls))

    def test_metadata_migration_clears_legacy_source_before_reporting_new(self):
        completed = mock.Mock(returncode=0, stdout="{}", stderr="")
        with mock.patch.object(
            agent_labels,
            "run_herdr",
            return_value=completed,
        ) as run:
            agent_labels.report_agent_label_metadata(
                "w1:p3",
                "codex",
                "blue-otter",
                "🟦",
                "blue",
            )

        self.assertEqual(
            [call.args[4] for call in run.call_args_list],
            [agent_labels.LEGACY_SOURCE, agent_labels.SOURCE],
        )

    def test_metadata_failure_is_visible_to_assignment(self):
        info = {"agent": "codex", "terminal_id": "term-1"}
        renamed = mock.Mock(returncode=0, stdout="", stderr="")
        output = io.StringIO()
        with (
            mock.patch.object(agent_labels, "fetch_agent_info", return_value=info),
            mock.patch.object(agent_labels, "run_herdr", return_value=renamed) as run,
            mock.patch.object(
                agent_labels,
                "report_agent_label_metadata",
                return_value=False,
            ),
            redirect_stdout(output),
        ):
            self.assertEqual(agent_labels.assign_label("w1:p3"), 1)
        self.assertEqual(output.getvalue(), f"{run.call_args.args[3]}\n")

    def test_invalid_agent_response_shape_is_ignored(self):
        result = mock.Mock(returncode=0, stdout='{"result":null}', stderr="")
        errors = io.StringIO()
        with (
            mock.patch.object(agent_labels, "run_herdr", return_value=result),
            redirect_stderr(errors),
        ):
            self.assertEqual(agent_labels.fetch_agent_info("w1:p3"), {})
        self.assertIn("w1:p3", errors.getvalue())
        self.assertTrue(errors.getvalue().strip())

    def test_invalid_agent_session_shape_is_ignored(self):
        info = {
            "agent": "codex",
            "terminal_id": "term-1",
            "agent_session": "invalid",
        }
        renamed = mock.Mock(returncode=0, stdout="", stderr="")
        output = io.StringIO()
        with (
            mock.patch.object(agent_labels, "fetch_agent_info", return_value=info),
            mock.patch.object(agent_labels, "run_herdr", return_value=renamed) as run,
            redirect_stdout(output),
        ):
            self.assertEqual(agent_labels.assign_label("w1:p3"), 0)
        self.assertEqual(output.getvalue(), f"{run.call_args_list[0].args[3]}\n")

    def test_event_envelope_is_supported(self):
        value = '{"event":"pane.agent_detected","data":{"pane_id":"w1:p4","agent":"codex"}}'
        with mock.patch.dict(os.environ, {"HERDR_PLUGIN_EVENT_JSON": value}):
            self.assertEqual(agent_labels.event_data()["pane_id"], "w1:p4")

    def test_status_event_rechecks_label_without_clearing_metadata(self):
        value = (
            '{"event":"pane.agent_status_changed","data":'
            '{"pane_id":"w1:p4","agent_status":"idle"}}'
        )
        with (
            mock.patch.dict(
                os.environ,
                {
                    "HERDR_PLUGIN_EVENT": "pane.agent_status_changed",
                    "HERDR_PLUGIN_EVENT_JSON": value,
                },
                clear=True,
            ),
            mock.patch.object(agent_labels, "assign_label", return_value=0) as assign,
            mock.patch.object(agent_labels, "clear_agent_label_metadata") as clear,
        ):
            self.assertEqual(agent_labels.handle_event(), 0)

        assign.assert_called_once_with("w1:p4")
        clear.assert_not_called()

    def test_released_detection_event_clears_current_and_legacy_metadata(self):
        value = (
            '{"event":"pane.agent_detected","data":'
            '{"pane_id":"w1:p4","released":true}}'
        )
        with (
            mock.patch.dict(
                os.environ,
                {
                    "HERDR_PLUGIN_EVENT": "pane.agent_detected",
                    "HERDR_PLUGIN_EVENT_JSON": value,
                },
                clear=True,
            ),
            mock.patch.object(
                agent_labels,
                "clear_agent_label_metadata",
                return_value=True,
            ) as clear,
            mock.patch.object(agent_labels, "assign_label") as assign,
        ):
            self.assertEqual(agent_labels.handle_event(), 0)

        clear.assert_called_once_with("w1:p4")
        assign.assert_not_called()

    def test_reconcile_continues_after_one_assignment_failure(self):
        agents = [
            {"agent": "codex", "pane_id": "w1:p1"},
            {"agent": "codex", "pane_id": "w1:p2"},
        ]
        with (
            mock.patch.object(agent_labels, "fetch_agents", return_value=agents),
            mock.patch.object(
                agent_labels,
                "assign_label",
                side_effect=[1, 0],
            ) as assign,
        ):
            self.assertEqual(agent_labels.reconcile_labels(), 1)

        self.assertEqual(assign.call_count, 2)

    def test_reconcile_reports_agent_list_failure(self):
        failure = mock.Mock(returncode=1, stdout="", stderr="socket unavailable")
        errors = io.StringIO()
        with (
            mock.patch.object(agent_labels, "run_herdr", return_value=failure),
            mock.patch("sys.stderr", errors),
        ):
            self.assertEqual(agent_labels.reconcile_labels(), 1)

        self.assertIn("agent list failed: socket unavailable", errors.getvalue())

    def test_manifest_reconciles_on_startup_and_agent_status_changes(self):
        manifest = MANIFEST_PATH.read_text(encoding="utf-8")
        self.assertIn(
            '[[startup]]\ncommand = ["python3", "agent_labels.py", "reconcile"]',
            manifest,
        )
        self.assertIn(
            'on = "pane.agent_status_changed"\n'
            'command = ["python3", "agent_labels.py", "event"]',
            manifest,
        )

    def test_parse_event_command(self):
        arguments = agent_labels.parse_cli_arguments(["event"])
        self.assertEqual(arguments.command, "event")

    def test_parse_reconcile_command(self):
        arguments = agent_labels.parse_cli_arguments(["reconcile"])
        self.assertEqual(arguments.command, "reconcile")

    def test_parse_label_command_with_pane_id(self):
        arguments = agent_labels.parse_cli_arguments(["label", "w1:p5"])
        self.assertEqual(arguments.command, "label")
        self.assertEqual(arguments.pane_id, "w1:p5")

    def test_parse_label_command_without_pane_id(self):
        arguments = agent_labels.parse_cli_arguments(["label"])
        self.assertEqual(arguments.command, "label")
        self.assertIsNone(arguments.pane_id)

    def test_run_herdr_reaches_the_binary_when_it_is_not_on_path(self):
        """The herdr server hands plugin commands its own PATH, which may lack the binary."""

        with tempfile.TemporaryDirectory() as directory:
            binary = Path(directory, "herdr")
            binary.write_text('#!/bin/sh\necho "$@"\n', encoding="utf-8")
            binary.chmod(0o700)
            environment = {
                **os.environ,
                "HERDR_BIN_PATH": os.fspath(binary),
                "PATH": "/nonexistent",
            }

            with mock.patch.dict(os.environ, environment, clear=True):
                result = agent_labels.run_herdr("agent", "get", "w1:p1")

        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout.strip(), "agent get w1:p1")


if __name__ == "__main__":
    unittest.main()
