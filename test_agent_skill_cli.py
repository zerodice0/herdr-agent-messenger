import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from dataclasses import replace
from io import StringIO
from pathlib import Path
from unittest import mock

import agent_directory
import agent_skill_cli

SKILL_WRAPPER_PATH = Path(
    ".agents/skills/herdr-agent-messenger/scripts/herdr_agent_messenger.py"
).resolve()


def load_skill_wrapper():
    spec = importlib.util.spec_from_file_location(
        "herdr_agent_messenger_wrapper_test",
        SKILL_WRAPPER_PATH,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("Could not load the skill wrapper.")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def agent(
    *,
    host: str = "local",
    name: str = "white-bison",
    pane_id: str = "w1:p1",
    local: bool = True,
) -> agent_directory.AgentRecord:
    return agent_directory.AgentRecord(
        host=host,
        name=name,
        pane_id=pane_id,
        workspace_id="w1",
        workspace_label="project",
        status="idle",
        session_id=f"session-{pane_id}",
        cwd="/work/project",
        local=local,
        revision=1,
        agent_kind="codex",
        terminal_id=f"terminal-{pane_id}",
    )


def batch_route_resolution(route, environment=None):
    return agent_skill_cli.RouteResolution(
        agent(name=route, pane_id=route), route_refreshed=False, route=route
    )


class AgentSkillCliTest(unittest.TestCase):
    def test_local_discovery_reports_snapshot_permission_failure(self):
        failed = agent_directory.ProbeResult(
            "local",
            (),
            False,
            "Operation not permitted",
        )
        with (
            mock.patch.object(
                agent_skill_cli,
                "probe_local_agents",
                return_value=failed,
            ),
            self.assertRaises(agent_skill_cli.SkillCommandError) as raised,
        ):
            agent_skill_cli.discover_agents("local", {})

        self.assertEqual(raised.exception.code, "host_unavailable")
        self.assertEqual(raised.exception.message, "Operation not permitted")

    def test_request_state_default_falls_back_to_legacy_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            legacy = home / ".local/state/herdr-agent-labels/requests"
            legacy.mkdir(parents=True)

            store = agent_skill_cli._request_state_store({"HOME": str(home)})

        self.assertEqual(store.directory, legacy)

    def test_request_state_ignores_plugin_runtime_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = agent_skill_cli._request_state_store({
                "HERDR_PLUGIN_STATE_DIR": str(root / "plugin"),
                "XDG_STATE_HOME": str(root / "state"),
                "HOME": str(root / "home"),
            })
        self.assertEqual(
            store.directory, root / "state/herdr-agent-messenger/requests"
        )

    def test_route_token_resolves_exact_current_occupant(self):
        recipient = agent(name="", pane_id="w7:p9")
        token = agent_skill_cli.encode_agent_route(recipient)
        with mock.patch.object(
            agent_skill_cli,
            "discover_agents",
            return_value=[recipient],
        ) as discover:
            self.assertEqual(agent_skill_cli.resolve_routed_agent(token), recipient)

        discover.assert_called_once_with("local", None)

    def test_route_token_preserves_the_ssh_alias(self):
        recipient = agent(
            host="macbook-pro",
            name="",
            pane_id="w7:p9",
            local=False,
        )
        token = agent_skill_cli.encode_agent_route(recipient)
        with mock.patch.object(
            agent_skill_cli,
            "discover_agents",
            return_value=[recipient],
        ) as discover:
            self.assertEqual(agent_skill_cli.resolve_routed_agent(token), recipient)

        discover.assert_called_once_with("macbook-pro", None)

    def test_route_token_expires_when_the_pane_occupant_changes(self):
        original = agent(name="", pane_id="w7:p9")
        replacement = replace(original, session_id="replacement-session")
        token = agent_skill_cli.encode_agent_route(original)
        with (
            mock.patch.object(
                agent_skill_cli,
                "discover_agents",
                return_value=[replacement],
            ),
            self.assertRaises(agent_skill_cli.SkillCommandError) as raised,
        ):
            agent_skill_cli.resolve_routed_agent(token)

        self.assertEqual(raised.exception.code, "route_expired")

    def test_route_token_rejects_invalid_payload(self):
        with self.assertRaises(agent_skill_cli.SkillCommandError) as raised:
            agent_skill_cli.resolve_routed_agent("not-a-route")
        self.assertEqual(raised.exception.code, "invalid_route")

    def test_list_is_compact_and_verbose_preserves_full_record(self):
        recipient = agent(
            host="macbook-pro",
            name="purple-koala",
            pane_id="w1:p4",
            local=False,
        )
        recipient = replace(recipient, route_target="w1:p4")
        with mock.patch.object(
            agent_skill_cli,
            "discover_agents",
            return_value=[recipient],
        ):
            payload = agent_skill_cli.list_command("macbook-pro")
            verbose_payload = agent_skill_cli.list_command(
                "macbook-pro",
                verbose=True,
            )

        self.assertEqual(
            payload["agents"],
            [
                {
                    "address": "macbook-pro/purple-koala",
                    "status": "idle",
                    "workspace": "project",
                }
            ],
        )
        self.assertEqual(verbose_payload["agents"][0]["route_target"], "w1:p4")
        self.assertEqual(verbose_payload["agents"][0]["label"], "purple-koala")

    def test_list_returns_only_addressable_labeled_agents(self):
        labeled = agent(name="white-bison")
        unnamed = agent(name="", pane_id="w1:p2")
        with mock.patch.object(
            agent_skill_cli,
            "discover_agents",
            return_value=[labeled, unnamed],
        ):
            payload = agent_skill_cli.list_command("local")
        self.assertEqual(len(payload["agents"]), 1)
        self.assertEqual(payload["agents"][0]["address"], "local/white-bison")

    def test_list_legacy_parser_alias_enables_verbose_output(self):
        arguments = agent_skill_cli.parse_cli_arguments(["list", "--legacy"])

        self.assertTrue(arguments.verbose)

    def test_status_resolves_one_verified_route(self):
        recipient = agent(name="", pane_id="w7:p9")
        route = agent_skill_cli.encode_agent_route(recipient)
        with mock.patch.object(
            agent_skill_cli,
            "discover_agents",
            return_value=[recipient],
        ) as discover:
            payload = agent_skill_cli.status_command(host="local", route=route)

        discover.assert_called_once_with("local", None)
        self.assertEqual(payload, {"address": "local/w7:p9", "status": "idle"})

    def test_status_returns_a_safely_refreshed_route(self):
        original = agent(name="white-bison", pane_id="w7:p9")
        replacement = replace(original, session_id="replacement-session", revision=2)
        route = agent_skill_cli.encode_agent_route(original)
        with mock.patch.object(
            agent_skill_cli,
            "discover_agents",
            return_value=[replacement],
        ):
            payload = agent_skill_cli.status_command(
                host="local", route=route, verbose=True
            )

        self.assertTrue(payload["route_refreshed"])
        self.assertEqual(
            payload["route"],
            agent_skill_cli.encode_agent_route(replacement),
        )

    def test_hostname_alias_is_remote_even_when_it_matches_local_hostname(self):
        remote = agent(host="shared", local=False)
        with (
            mock.patch.object(agent_skill_cli, "ssh_hosts", return_value=["shared"]),
            mock.patch.object(
                agent_skill_cli,
                "probe_local_agents",
            ) as query_local,
            mock.patch.object(
                agent_skill_cli,
                "query_remote_agents",
                return_value=agent_directory.ProbeResult("shared", (remote,), True),
            ) as query_remote,
        ):
            self.assertEqual(agent_skill_cli.discover_agents("shared", {}), [remote])

        query_local.assert_not_called()
        query_remote.assert_called_once()

    def test_remote_host_must_be_a_concrete_configured_alias(self):
        with (
            mock.patch.object(
                agent_skill_cli,
                "ssh_hosts",
                return_value=["macbook-pro"],
            ),
            self.assertRaises(agent_skill_cli.SkillCommandError) as raised,
        ):
            agent_skill_cli.discover_agents("unlisted-host", {})
        self.assertEqual(raised.exception.code, "host_not_configured")

    def test_resolve_labeled_agent_requires_exact_unique_label(self):
        first = agent(name="white-bison")
        second = agent(name="blue-raven", pane_id="w1:p2")
        with mock.patch.object(
            agent_skill_cli,
            "discover_agents",
            return_value=[first, second],
        ):
            self.assertEqual(
                agent_skill_cli.resolve_labeled_agent("local", "white-bison"),
                first,
            )
            with self.assertRaises(agent_skill_cli.SkillCommandError) as raised:
                agent_skill_cli.resolve_labeled_agent("local", "white")
        self.assertEqual(raised.exception.code, "agent_not_found")

    def test_send_waits_and_includes_verified_sender(self):
        recipient = agent(name="white-bison", pane_id="w1:p2")
        sender = agent(name="blue-raven")
        completed = subprocess.CompletedProcess(["herdr"], 0, '{"result":"done"}\n', "")
        with (
            mock.patch.object(
                agent_skill_cli,
                "resolve_labeled_agent",
                return_value=recipient,
            ),
            mock.patch.object(agent_skill_cli, "current_sender", return_value=sender),
            mock.patch.object(
                agent_skill_cli,
                "run_bounded_command",
                return_value=completed,
            ) as run,
        ):
            payload = agent_skill_cli.send_command(
                host="local",
                label="white-bison",
                message="Review this change.",
                wait=True,
                timeout_ms=90_000,
                environment={},
                verbose=True,
            )

        command = run.call_args.args[0]
        self.assertEqual(command[:4], ["herdr", "agent", "prompt", "white-bison"])
        self.assertIn("Message from local/blue-raven:\n\nReview this change.", command)
        self.assertEqual(command[-3:], ["--wait", "--timeout", "90000"])
        self.assertEqual(run.call_args.kwargs["timeout"], 95)
        self.assertEqual(payload["sent"], True)
        self.assertEqual(payload["recipient"], "local/white-bison")
        self.assertEqual(payload["waited"], True)
        self.assertEqual(payload["wait_can_track_submitted_turn"], True)
        self.assertEqual(payload["warnings"], [])
        self.assertFalse(payload["route_refreshed"])
        self.assertEqual(payload["route"], agent_skill_cli.encode_agent_route(recipient))

    def test_send_accepts_gui_route_without_a_label_or_installed_skill(self):
        recipient = agent(name="", pane_id="w7:p9")
        sender = agent(name="blue-raven")
        token = agent_skill_cli.encode_agent_route(recipient)
        completed = subprocess.CompletedProcess(["herdr"], 0, "{}\n", "")
        with (
            mock.patch.object(
                agent_skill_cli,
                "discover_agents",
                return_value=[recipient],
            ),
            mock.patch.object(agent_skill_cli, "current_sender", return_value=sender),
            mock.patch.object(
                agent_skill_cli,
                "run_bounded_command",
                return_value=completed,
            ) as run,
        ):
            payload = agent_skill_cli.send_command(
                host="local",
                route=token,
                message="Inspect this session.",
                wait=True,
                timeout_ms=90_000,
                environment={},
                verbose=True,
        )

        self.assertEqual(run.call_args.args[0][3], "w7:p9")
        self.assertEqual(payload["recipient"], "local/w7:p9")
        self.assertFalse(payload["route_refreshed"])
        self.assertEqual(payload["route"], token)

    def test_send_refreshes_a_stale_registered_route(self):
        original = agent(name="white-bison", pane_id="w7:p9")
        replacement = replace(original, session_id="replacement-session", revision=2)
        sender = agent(name="blue-raven")
        token = agent_skill_cli.encode_agent_route(original)
        completed = subprocess.CompletedProcess(["herdr"], 0, "{}\n", "")
        with (
            mock.patch.object(
                agent_skill_cli,
                "discover_agents",
                return_value=[replacement],
            ),
            mock.patch.object(agent_skill_cli, "current_sender", return_value=sender),
            mock.patch.object(
                agent_skill_cli,
                "run_bounded_command",
                return_value=completed,
            ),
        ):
            payload = agent_skill_cli.send_command(
                host="local",
                route=token,
                message="Inspect this session.",
                wait=False,
                timeout_ms=90_000,
                environment={},
                verbose=True,
            )

        self.assertTrue(payload["route_refreshed"])
        self.assertEqual(
            payload["route"], agent_skill_cli.encode_agent_route(replacement)
        )

    def test_cli_route_runs_with_an_empty_skill_home(self):
        with tempfile.TemporaryDirectory() as empty_home:
            root = Path(empty_home)
            log_path = root / "prompt.json"
            snapshot = {
                "result": {
                    "snapshot": {
                        "agents": [
                            {
                                "agent": "codex",
                                "agent_session": {"value": "sender-session"},
                                "agent_status": "idle",
                                "cwd": "/work/coordinator",
                                "name": "blue-raven",
                                "pane_id": "w1:p1",
                                "revision": 1,
                                "terminal_id": "sender-terminal",
                                "workspace_id": "w1",
                            },
                            {
                                "agent": "codex",
                                "agent_session": {"value": "recipient-session"},
                                "agent_status": "idle",
                                "cwd": "/work/worker",
                                "pane_id": "w7:p9",
                                "revision": 1,
                                "terminal_id": "recipient-terminal",
                                "workspace_id": "w7",
                            },
                        ],
                        "workspaces": [],
                    }
                }
            }
            records = agent_directory.parse_agent_payload(
                snapshot,
                host=agent_directory.local_host_name(),
                local=True,
            )
            recipient = next(record for record in records if record.pane_id == "w7:p9")
            token = agent_skill_cli.encode_agent_route(recipient)
            fake_herdr = root / "herdr"
            fake_herdr.write_text(
                "#!/usr/bin/env python3\n"
                "import json, pathlib, sys\n"
                f"snapshot = {snapshot!r}\n"
                f"log_path = pathlib.Path({os.fspath(log_path)!r})\n"
                "if sys.argv[1:3] == ['api', 'snapshot']:\n"
                "    print(json.dumps(snapshot))\n"
                "elif sys.argv[1:3] == ['agent', 'prompt']:\n"
                "    log_path.write_text(json.dumps(sys.argv[1:4]))\n"
                "    print('{}')\n"
                "else:\n"
                "    raise SystemExit(2)\n",
                encoding="utf-8",
            )
            fake_herdr.chmod(0o700)
            environment = {
                **os.environ,
                "HOME": empty_home,
                "HERDR_BIN_PATH": os.fspath(fake_herdr),
                "HERDR_PANE_ID": "w1:p1",
            }
            self.assertFalse(root.joinpath(".agents", "skills").exists())
            self.assertFalse(root.joinpath(".claude", "skills").exists())
            completed = subprocess.run(
                [
                    sys.executable,
                    os.fspath(Path(agent_skill_cli.__file__).resolve()),
                    "send",
                    "--route",
                    token,
                    "--message",
                    "Inspect this session.",
                    "--no-wait",
                ],
                capture_output=True,
                check=False,
                text=True,
                timeout=10,
                env=environment,
                cwd=empty_home,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertTrue(json.loads(completed.stdout)["sent"])
            self.assertEqual(
                json.loads(log_path.read_text(encoding="utf-8")),
                ["agent", "prompt", "w7:p9"],
            )

    def test_send_warns_when_waiting_on_an_already_working_agent(self):
        recipient = agent(name="white-bison", pane_id="w1:p2")
        recipient = replace(recipient, status="working")
        sender = agent(name="blue-raven")
        completed = subprocess.CompletedProcess(["herdr"], 0, "{}\n", "")
        with (
            mock.patch.object(
                agent_skill_cli,
                "resolve_labeled_agent",
                return_value=recipient,
            ),
            mock.patch.object(agent_skill_cli, "current_sender", return_value=sender),
            mock.patch.object(
                agent_skill_cli,
                "run_bounded_command",
                return_value=completed,
            ),
        ):
            payload = agent_skill_cli.send_command(
                host="local",
                label="white-bison",
                message="Review this change.",
                wait=True,
                timeout_ms=90_000,
                environment={},
            )
        self.assertEqual(payload["address"], "local/white-bison")
        self.assertEqual(payload["warnings"], ["recipient_already_working"])

    def test_send_rejects_the_current_sender_as_recipient(self):
        sender = agent(name="white-bison")
        with (
            mock.patch.object(
                agent_skill_cli,
                "resolve_labeled_agent",
                return_value=sender,
            ),
            mock.patch.object(agent_skill_cli, "current_sender", return_value=sender),
            self.assertRaises(agent_skill_cli.SkillCommandError) as raised,
        ):
            agent_skill_cli.send_command(
                host="local",
                label="white-bison",
                message="Do this.",
                wait=True,
                timeout_ms=90_000,
                environment={},
            )
        self.assertEqual(raised.exception.code, "recipient_is_sender")

    def test_send_without_wait_keeps_a_short_process_deadline(self):
        recipient = agent(name="white-bison", pane_id="w1:p2")
        sender = agent(name="blue-raven")
        completed = subprocess.CompletedProcess(["herdr"], 0, "{}\n", "")
        with (
            mock.patch.object(
                agent_skill_cli,
                "resolve_labeled_agent",
                return_value=recipient,
            ),
            mock.patch.object(agent_skill_cli, "current_sender", return_value=sender),
            mock.patch.object(
                agent_skill_cli,
                "run_bounded_command",
                return_value=completed,
            ) as run,
        ):
            payload = agent_skill_cli.send_command(
                host="local",
                label="white-bison",
                message="Review this change.",
                wait=False,
                timeout_ms=90_000,
                environment={},
            )
        self.assertNotIn("--wait", run.call_args.args[0])
        self.assertNotIn("wait_can_track_submitted_turn", payload)
        self.assertNotIn("warnings", payload)
        self.assertEqual(
            run.call_args.kwargs["timeout"],
            agent_directory.REMOTE_DISCOVERY_TIMEOUT_SECONDS,
        )

    def test_send_classifies_timeout_and_cancellation(self):
        recipient = agent(name="white-bison", pane_id="w1:p2")
        sender = agent(name="blue-raven")
        failures = (
            ("timeout", "prompt_timeout"),
            ("cancelled", "prompt_cancelled"),
        )
        for detail, expected_code in failures:
            completed = subprocess.CompletedProcess(["herdr"], 1, "", detail)
            with (
                self.subTest(detail=detail),
                mock.patch.object(
                    agent_skill_cli,
                    "resolve_or_refresh_recipient",
                    return_value=agent_skill_cli.RouteResolution(
                        recipient,
                        route_refreshed=False,
                        route=agent_skill_cli.encode_agent_route(recipient),
                    ),
                ),
                mock.patch.object(
                    agent_skill_cli,
                    "current_sender",
                    return_value=sender,
                ),
                mock.patch.object(
                    agent_skill_cli,
                    "run_bounded_command",
                    return_value=completed,
                ),
                self.assertRaises(agent_skill_cli.SkillCommandError) as raised,
            ):
                agent_skill_cli.send_command(
                    host="local",
                    route="opaque",
                    message="Review this change.",
                    wait=True,
                    timeout_ms=90_000,
                    environment={},
                )
            self.assertEqual(raised.exception.code, expected_code)

    @mock.patch.object(
        agent_skill_cli, "resolve_or_refresh_routed_agent", new=batch_route_resolution
    )
    def test_batch_reuses_request_lifecycle_and_aggregates_results(self):
        def request_command(**arguments):
            route = arguments["route"]
            if route == "self":
                raise agent_skill_cli.SkillCommandError(
                    "recipient_is_sender",
                    "The current sender cannot also be the recipient.",
                )
            if route == "slow":
                return {
                    "state": "submitted_working",
                    "submitted": True,
                    "timed_out": True,
                    "request_id": "c" * 32,
                    "route": route,
                    "response": {"output": "partial\n", "truncated": False},
                }
            return {
                "state": "submitted_settled",
                "request_id": "a" * 32,
                "route": route,
                "response": {
                    "output": "answer\n",
                    "truncated": False,
                    "correlated": True,
                },
            }

        with mock.patch.object(
            agent_skill_cli,
            "request_command",
            side_effect=request_command,
        ) as request:
            payload = agent_skill_cli.batch_command(
                requests_json=(
                    '[{"route":"ok","message":"one"},'
                    '{"route":"self","message":"two"},'
                    '{"route":"slow","message":"three"}]'
                ),
                wait=True,
                timeout_ms=90_000,
                max_workers=1,
                environment={"HERDR_PANE_ID": "w1:p1"},
            )

        self.assertEqual(request.call_count, 3)
        self.assertEqual(
            [result["status"] for result in payload["results"]],
            ["succeeded", "failed", "submitted"],
        )
        self.assertEqual(payload["results"][1]["error_code"], "recipient_is_sender")
        self.assertEqual(payload["results"][0]["request_id"], "a" * 32)
        self.assertEqual(payload["results"][0]["response"]["text"], "answer")
        self.assertEqual(payload["status"], "partial")

    @mock.patch.object(
        agent_skill_cli, "resolve_or_refresh_routed_agent", new=batch_route_resolution
    )
    def test_batch_no_wait_reports_submitted(self):
        with mock.patch.object(
            agent_skill_cli,
            "send_command",
            return_value={"sent": True, "waited": False},
        ):
            payload = agent_skill_cli.batch_command(
                requests_json='[{"route":"one","message":"instruction"}]',
                wait=False,
                timeout_ms=90_000,
                max_workers=1,
                environment={},
            )

        self.assertEqual(payload["status"], "succeeded")
        self.assertEqual(payload["results"][0]["status"], "submitted")

    @mock.patch.object(
        agent_skill_cli, "resolve_or_refresh_routed_agent", new=batch_route_resolution
    )
    def test_no_wait_batch_does_not_turn_uncertain_submission_into_failure(self):
        with mock.patch.object(agent_skill_cli, "send_command", side_effect=agent_skill_cli.SkillCommandError(
            "prompt_acceptance_unknown", "Connection lost after possible delivery.",
        )):
            result = agent_skill_cli.batch_command(
                requests_json='[{"route":"worker","message":"work"}]',
                wait=False, timeout_ms=1000, max_workers=1, environment={},
            )
        self.assertEqual(result["results"][0]["status"], "timeout")
        self.assertEqual(result["results"][0]["error_code"], "prompt_acceptance_unknown")

    def test_batch_normalizes_future_request_lifecycle_payloads(self):
        cases = (
            ({"state": "submitted_settled"}, "succeeded"),
            ({"state": "submitted_working"}, "submitted"),
            (
                {
                    "state": "submitted_working",
                    "submitted": True,
                    "timed_out": True,
                },
                "submitted",
            ),
            (
                {
                    "state": "submitted_unknown",
                    "submitted": None,
                    "submission_timed_out": True,
                    "timed_out": True,
                },
                "timeout",
            ),
            ({"state": "submission_failed", "error": "rejected"}, "failed"),
            ({"state": "submitted_unknown", "submitted": None, "terminal": True}, "timeout"),
            ({"state": "submitted_unknown", "submitted": None, "cancelled": True}, "timeout"),
        )
        for lifecycle_payload, expected in cases:
            with self.subTest(payload=lifecycle_payload):
                outcome = agent_skill_cli._batch_outcome_from_payload(
                    lifecycle_payload,
                    waited=True,
                )
                self.assertEqual(outcome.status, expected)

    def test_waiting_batch_forwards_cancellation_to_request_lifecycle(self):
        cancel_event = threading.Event()
        cancel_event.set()
        with mock.patch.object(agent_skill_cli, "request_command") as request:
            request.side_effect = agent_skill_cli.SkillCommandError(
                "prompt_cancelled",
                "Request was cancelled.",
            )
            payload = agent_skill_cli.batch_command(
                requests_json='[{"route":"one","message":"instruction"}]',
                wait=True,
                timeout_ms=90_000,
                max_workers=1,
                environment={},
                cancel_event=cancel_event,
            )

        request.assert_not_called()
        self.assertEqual(payload["results"][0]["status"], "cancelled")

    @mock.patch.object(
        agent_skill_cli, "resolve_or_refresh_routed_agent", new=batch_route_resolution
    )
    def test_batch_returns_a_future_refreshed_route(self):
        with mock.patch.object(
            agent_skill_cli,
            "request_command",
            return_value={
                "state": "submitted_settled",
                "route": "v2-refreshed",
            },
        ):
            payload = agent_skill_cli.batch_command(
                requests_json='[{"route":"v1-stale","message":"instruction"}]',
                wait=True,
                timeout_ms=90_000,
                max_workers=1,
                environment={},
                verbose=True,
            )

        self.assertEqual(payload["results"][0]["route"], "v2-refreshed")

    def test_read_resolves_label_before_fetching_output(self):
        recipient = agent(name="white-bison")
        completed = subprocess.CompletedProcess(["herdr"], 0, "final report\n", "")
        with (
            mock.patch.object(
                agent_skill_cli,
                "resolve_labeled_agent",
                return_value=recipient,
            ),
            mock.patch.object(
                agent_skill_cli,
                "run_bounded_command",
                return_value=completed,
            ) as run,
        ):
            payload = agent_skill_cli.read_command(
                host="local",
                label="white-bison",
                lines=80,
                environment={},
            )

        self.assertEqual(
            run.call_args.args[0],
            [
                "herdr",
                "agent",
                "read",
                "white-bison",
                "--source",
                "recent-unwrapped",
                "--lines",
                "80",
                "--format",
                "text",
            ],
        )
        self.assertEqual(payload["output"], "final report")
        self.assertNotIn("route", payload)
        self.assertEqual(payload["address"], "local/white-bison")
        self.assertFalse(payload["delta"])
        self.assertFalse(payload["truncated"])
        self.assertNotIn("cursor_status", payload)
        self.assertTrue(payload["cursor"])

    def test_read_refreshes_the_same_stale_route_path_as_send(self):
        original = agent(name="white-bison")
        replacement = replace(original, session_id="replacement-session", revision=2)
        token = agent_skill_cli.encode_agent_route(original)
        completed = subprocess.CompletedProcess(["herdr"], 0, "final report\n", "")
        with (
            mock.patch.object(
                agent_skill_cli,
                "discover_agents",
                return_value=[replacement],
            ),
            mock.patch.object(
                agent_skill_cli,
                "run_bounded_command",
                return_value=completed,
            ),
        ):
            payload = agent_skill_cli.read_command(
                host="local",
                route=token,
                lines=80,
                environment={},
                verbose=True,
            )

        self.assertTrue(payload["route_refreshed"])
        self.assertEqual(
            payload["route"], agent_skill_cli.encode_agent_route(replacement)
        )

    def test_read_uses_cursor_for_incremental_output(self):
        recipient = agent(name="white-bison")
        first = subprocess.CompletedProcess(["herdr"], 0, "first\n", "")
        second = subprocess.CompletedProcess(["herdr"], 0, "first\nsecond\n", "")
        with (
            mock.patch.object(
                agent_skill_cli,
                "resolve_labeled_agent",
                return_value=recipient,
            ),
            mock.patch.object(
                agent_skill_cli,
                "run_bounded_command",
                side_effect=[first, second],
            ),
        ):
            initial = agent_skill_cli.read_command(
                host="local",
                label="white-bison",
                lines=80,
                environment={},
            )
            delta = agent_skill_cli.read_command(
                host="local",
                label="white-bison",
                lines=80,
                cursor=initial["cursor"],
                environment={},
            )

        self.assertEqual(delta["output"], "second")
        self.assertTrue(delta["delta"])
        self.assertNotIn("cursor_status", delta)

    def test_read_rejects_an_invalid_cursor(self):
        recipient = agent(name="white-bison")
        completed = subprocess.CompletedProcess(["herdr"], 0, "output\n", "")
        with (
            mock.patch.object(
                agent_skill_cli,
                "resolve_labeled_agent",
                return_value=recipient,
            ),
            mock.patch.object(
                agent_skill_cli,
                "run_bounded_command",
                return_value=completed,
            ),
            self.assertRaises(agent_skill_cli.SkillCommandError) as raised,
        ):
            agent_skill_cli.read_command(
                host="local",
                label="white-bison",
                lines=80,
                cursor="not-a-cursor",
                environment={},
            )

        self.assertEqual(raised.exception.code, "invalid_cursor")

    def test_sender_requires_invoking_pane_context(self):
        with (
            self.assertRaises(agent_skill_cli.SkillCommandError) as raised,
        ):
            agent_skill_cli.current_sender({})
        self.assertEqual(raised.exception.code, "sender_unavailable")
        self.assertIn("HERDR_PANE_ID", raised.exception.message)

    def test_sender_preserves_snapshot_permission_failure(self):
        failed = agent_directory.ProbeResult(
            "local",
            (),
            False,
            "Operation not permitted",
        )
        with (
            mock.patch.object(
                agent_skill_cli,
                "probe_local_agents",
                return_value=failed,
            ),
            self.assertRaises(agent_skill_cli.SkillCommandError) as raised,
        ):
            agent_skill_cli.current_sender({"HERDR_PANE_ID": "w1:p1"})

        self.assertEqual(raised.exception.code, "sender_probe_failed")
        self.assertIn("Operation not permitted", raised.exception.message)
        self.assertIn("No prompt was submitted", raised.exception.message)

    def test_sender_reports_current_pane_without_agent(self):
        other = agent(name="blue-raven", pane_id="w2:p1")
        available = agent_directory.ProbeResult("local", (other,), True)
        with (
            mock.patch.object(
                agent_skill_cli,
                "probe_local_agents",
                return_value=available,
            ),
            self.assertRaises(agent_skill_cli.SkillCommandError) as raised,
        ):
            agent_skill_cli.current_sender({"HERDR_PANE_ID": "w1:p1"})

        self.assertEqual(raised.exception.code, "sender_unavailable")
        self.assertIn("w1:p1", raised.exception.message)
        self.assertIn("No prompt was submitted", raised.exception.message)

    def test_sender_uses_only_the_invoking_pane_environment(self):
        sender = agent(name="blue-raven", pane_id="w1:p1")
        active = agent(name="red-fox", pane_id="w9:p9")
        with mock.patch.object(
            agent_skill_cli,
            "probe_local_agents",
            return_value=agent_directory.ProbeResult(
                "local",
                (active, sender),
                True,
            ),
        ) as probe:
            self.assertEqual(
                agent_skill_cli.current_sender(
                    {
                        "HERDR_PANE_ID": "w1:p1",
                        "HERDR_ACTIVE_PANE_ID": "w9:p9",
                    }
                ),
                sender,
            )
        self.assertEqual(probe.call_args.args[0]["HERDR_PANE_ID"], "w1:p1")

    def test_send_parser_has_no_sender_override(self):
        with (
            mock.patch("sys.stderr"),
            self.assertRaises(SystemExit),
        ):
            agent_skill_cli.parse_cli_arguments(
                [
                    "send",
                    "--label",
                    "white-bison",
                    "--message",
                    "hello",
                    "--sender-pane",
                    "w2:p2",
                ]
            )

    def test_send_parser_accepts_a_verified_route_instead_of_a_label(self):
        arguments = agent_skill_cli.parse_cli_arguments(
            [
                "send",
                "--route",
                "opaque-token",
                "--message",
                "hello",
            ]
        )
        self.assertEqual(arguments.route, "opaque-token")
        self.assertIsNone(arguments.label)

    def test_status_and_read_parsers_accept_routes_and_watermarks(self):
        status = agent_skill_cli.parse_cli_arguments(
            ["status", "--route", "route-token"]
        )
        read = agent_skill_cli.parse_cli_arguments(
            [
                "read",
                "--route",
                "route-token",
                "--max-bytes",
                "512",
                "--watermark",
                "output-cursor",
            ]
        )

        self.assertEqual(status.route, "route-token")
        self.assertEqual(read.max_bytes, 512)
        self.assertEqual(read.cursor, "output-cursor")

    def test_batch_parser_accepts_json_and_worker_limit(self):
        arguments = agent_skill_cli.parse_cli_arguments(
            [
                "batch",
                "--requests-json",
                '[{"route":"one","message":"hello"}]',
                "--max-workers",
                "3",
                "--no-wait",
            ]
        )
        self.assertEqual(arguments.command, "batch")
        self.assertEqual(arguments.max_workers, 3)
        self.assertFalse(arguments.wait)

    def test_request_main_surfaces_sender_probe_failure(self):
        output = StringIO()
        failure = agent_skill_cli.SkillCommandError(
            "sender_probe_failed",
            "Could not verify the current sender: Operation not permitted. "
            "No prompt was submitted.",
        )
        with (
            mock.patch.object(
                agent_skill_cli,
                "request_command",
                side_effect=failure,
            ),
            mock.patch("sys.stdout", output),
        ):
            exit_code = agent_skill_cli.main(
                [
                    "request",
                    "--route",
                    "route-token",
                    "--message",
                    "hello",
                ],
                {},
            )

        self.assertEqual(exit_code, 1)
        self.assertEqual(
            json.loads(output.getvalue()),
            {
                "error": {
                    "code": "sender_probe_failed",
                    "message": failure.message,
                }
            },
        )

    def test_batch_main_returns_nonzero_partial_result_as_compact_json(self):
        result = {
            "status": "partial",
            "results": [
                {"route": "one", "status": "succeeded"},
                {"route": "two", "status": "failed", "error": "route_expired"},
            ],
        }
        output = StringIO()
        with (
            mock.patch.object(agent_skill_cli, "batch_command", return_value=result),
            mock.patch("sys.stdout", output),
        ):
            exit_code = agent_skill_cli.main(
                [
                    "batch",
                    "--requests-json",
                    '[{"route":"one","message":"hello"}]',
                ],
                {},
            )

        self.assertEqual(exit_code, 1)
        self.assertEqual(
            output.getvalue(),
            '{"status":"partial","results":['
            '{"route":"one","status":"succeeded"},'
            '{"route":"two","status":"failed","error":"route_expired"}]}\n',
        )


class AgentSkillContractTest(unittest.TestCase):
    def test_request_and_status_exit_codes_follow_lifecycle_state(self):
        states = {
            "submitted_settled": 0,
            "submission_failed": 1,
            "submitted_working": 3,
            "submitted_unknown": 3,
            "deferred": 3,
            "awaiting_decision": 3,
        }
        commands = (
            ("request", ["--route", "token", "--message", "work"], "request_command"),
            ("request-status", ["--request-id", "a" * 32], "request_status_command"),
        )
        for command, flags, handler in commands:
            for state, expected in states.items():
                payload = {"state": state, "error_code": None}
                output = StringIO()
                with (
                    self.subTest(command=command, state=state),
                    mock.patch.object(agent_skill_cli, handler, return_value=payload),
                    mock.patch("sys.stdout", output),
                ):
                    result = agent_skill_cli.main([command, *flags], {})
                    self.assertEqual(result, expected)
                    self.assertEqual(json.loads(output.getvalue()), payload)

    def test_batch_exit_codes_inspect_items_not_aggregate_delivery(self):
        cases = (
            (["succeeded"], 0),
            (["succeeded", "submitted"], 3),
            (["submitted"], 3),
            (["succeeded", "deferred"], 3),
            (["deferred", "failed"], 1),
            (["succeeded", "failed"], 1),
            (["submitted", "cancelled"], 1),
            (["submitted", "timeout"], 1),
        )
        for statuses, expected in cases:
            payload = {
                "status": "succeeded",
                "results": [
                    {"route": str(index), "status": status}
                    for index, status in enumerate(statuses)
                ],
            }
            output = StringIO()
            with (
                self.subTest(statuses=statuses),
                mock.patch.object(agent_skill_cli, "batch_command", return_value=payload),
                mock.patch("sys.stdout", output),
            ):
                result = agent_skill_cli.main(
                    ["batch", "--requests-json", "[]"], {}
                )
                self.assertEqual(result, expected)
                self.assertEqual(json.loads(output.getvalue()), payload)

    def test_unexpected_exceptions_return_internal_error_envelope(self):
        output = StringIO()
        with (
            mock.patch.object(
                agent_skill_cli, "list_command", side_effect=RuntimeError("broken")
            ),
            mock.patch("sys.stdout", output),
        ):
            result = agent_skill_cli.main(["list"], {})
        self.assertEqual(result, 1)
        self.assertEqual(json.loads(output.getvalue())["error"]["code"], "internal_error")

    def test_address_resolves_exact_remote_label(self):
        recipient = agent(host="remote", name="purple-koala", local=False)
        for flags in ([], ["--host", "remote"], ["--host", "local"]):
            output = StringIO()
            with (
                self.subTest(flags=flags),
                mock.patch.object(
                    agent_skill_cli, "discover_agents", return_value=[recipient]
                ) as discover,
                mock.patch("sys.stdout", output),
            ):
                result = agent_skill_cli.main(
                    ["status", "--address", "remote/purple-koala", *flags], {}
                )
            self.assertEqual(result, 0)
            self.assertEqual(
                json.loads(output.getvalue())["address"], "remote/purple-koala"
            )
            self.assertEqual(discover.call_args.args[0], "remote")

    def test_address_is_available_on_each_recipient_command(self):
        for command, extra in (
            ("status", []),
            ("send", ["--message", "work"]),
            ("read", []),
            ("request", ["--message", "work"]),
        ):
            with self.subTest(command=command):
                arguments = agent_skill_cli.parse_cli_arguments(
                    [command, "--address", "local/white-bison", *extra]
                )
                self.assertEqual(arguments.address, "local/white-bison")
                self.assertIsNone(arguments.label)
                self.assertIsNone(arguments.route)

    def test_address_cannot_be_combined_with_label_or_route(self):
        for flag in ("--label", "--route"):
            with (
                self.subTest(flag=flag),
                mock.patch("sys.stderr"),
                self.assertRaises(SystemExit) as raised,
            ):
                agent_skill_cli.parse_cli_arguments(
                    ["status", "--address", "local/white-bison", flag, "other"]
                )
            self.assertEqual(raised.exception.code, 2)

    def test_invalid_and_conflicting_addresses_fail_before_discovery(self):
        cases = [
            (address, [], "invalid_address")
            for address in ("", "label", "/label", "host/", "a/b/c", "host/ label")
        ]
        cases.append(("remote/label", ["--host", "other"], "conflicting_address"))
        for address, extra, code in cases:
            output = StringIO()
            with (
                self.subTest(address=address, extra=extra),
                mock.patch.object(agent_skill_cli, "discover_agents") as discover,
                mock.patch("sys.stdout", output),
            ):
                result = agent_skill_cli.main(
                    ["status", "--address", address, *extra], {}
                )
            self.assertEqual(result, 1)
            self.assertEqual(json.loads(output.getvalue())["error"]["code"], code)
            discover.assert_not_called()

    def test_batch_address_submits_to_the_label_resolved_occupant(self):
        recipient = agent(pane_id="w1:p2")
        sender = agent(name="blue-raven")
        commands = []

        def run(command, **options):
            commands.append(command)
            return subprocess.CompletedProcess(command, 0, "", "")

        with (
            mock.patch.object(
                agent_skill_cli, "discover_agents", return_value=[recipient]
            ),
            mock.patch.object(agent_skill_cli, "current_sender", return_value=sender),
            mock.patch.object(agent_skill_cli, "run_bounded_command", side_effect=run),
        ):
            result = agent_skill_cli.batch_command(
                requests_json='[{"address":"local/white-bison","message":"work"}]',
                wait=False, timeout_ms=1000, max_workers=1, environment={},
            )
        self.assertEqual(result["results"][0]["status"], "submitted")
        self.assertEqual(len(commands), 1)
        self.assertIn(recipient.target, commands[0])
        self.assertEqual(result["results"][0]["address"], "local/white-bison")

    def test_batch_duplicate_route_and_address_never_submit(self):
        recipient = agent(pane_id="w1:p2")
        route = agent_skill_cli.encode_agent_route(recipient)
        with (
            mock.patch.object(
                agent_skill_cli, "discover_agents", return_value=[recipient]
            ),
            mock.patch.object(agent_skill_cli, "send_command") as send,
        ):
            result = agent_skill_cli.batch_command(
                requests_json=json.dumps([
                    {"route": route, "message": "one"},
                    {"address": "local/white-bison", "message": "two"},
                    {"route": route, "message": "three"},
                ]),
                wait=False, timeout_ms=1000, max_workers=1, environment={},
            )
        self.assertEqual(
            [item["error_code"] for item in result["results"]], ["duplicate_route"] * 3
        )
        send.assert_not_called()

    def test_request_preserves_resolver_error_codes(self):
        errors = (
            agent_skill_cli.SkillCommandError("agent_not_found", "missing"),
            agent_skill_cli.AgentRouteError("route_expired", "expired"),
        )
        for error in errors:
            with (
                self.subTest(code=error.code),
                tempfile.TemporaryDirectory() as directory,
                mock.patch.object(
                    agent_skill_cli, "current_sender", return_value=agent()
                ),
            ):
                result = agent_skill_cli.request_command(
                    host="local", route="token", message="work", timeout_ms=1000,
                    output_lines=10, output_chars=100, environment={},
                    resolver=mock.Mock(side_effect=error),
                    state_store=agent_skill_cli.JsonRequestStateStore(Path(directory)),
                )
            self.assertEqual(result["state"], "submission_failed")
            self.assertEqual(result["error_code"], error.code)

    def test_self_request_reports_recipient_is_sender(self):
        sender = agent()
        runner = mock.Mock(return_value=subprocess.CompletedProcess([], 0, "", ""))
        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch.object(agent_skill_cli, "current_sender", return_value=sender),
        ):
            result = agent_skill_cli.request_command(
                host="local", label=sender.name, message="work", timeout_ms=1000,
                output_lines=10, output_chars=100, environment={},
                resolver=lambda: sender, command_runner=runner,
                state_store=agent_skill_cli.JsonRequestStateStore(Path(directory)),
            )
        self.assertEqual(result["state"], "submission_failed")
        self.assertEqual(result["error_code"], "recipient_is_sender")
        self.assertTrue(all("prompt" not in call.args[0] for call in runner.call_args_list))

    def test_spawn_errors_use_the_standard_error_envelope(self):
        output = StringIO()
        with (
            mock.patch.object(
                agent_skill_cli, "spawn_command",
                side_effect=agent_skill_cli.SpawnError("spawn_failed", "failed"),
            ),
            mock.patch("sys.stdout", output),
        ):
            result = agent_skill_cli.main(["spawn", "--kind", "codex"], {})
        self.assertEqual(result, 1)
        self.assertEqual(json.loads(output.getvalue())["error"]["code"], "spawn_failed")


    def test_default_request_resolver_preserves_invalid_route_code(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch.object(agent_skill_cli, "current_sender", return_value=agent()),
        ):
            result = agent_skill_cli.request_command(
                host="local", route="not-a-route", message="work", timeout_ms=1000,
                output_lines=10, output_chars=100, environment={},
                state_store=agent_skill_cli.JsonRequestStateStore(Path(directory)),
            )
        self.assertEqual(result["state"], "submission_failed")
        self.assertEqual(result["error_code"], "invalid_route")

    def test_batch_rejects_malformed_addresses_without_submission(self):
        with mock.patch.object(agent_skill_cli, "send_command") as send:
            result = agent_skill_cli.batch_command(
                requests_json='[{"address":"missing-host","message":"work"}]',
                wait=False, timeout_ms=1000, max_workers=1, environment={},
            )
        self.assertEqual(result["results"][0]["error_code"], "invalid_address")
        send.assert_not_called()

    def test_spawn_success_returns_payload_and_zero_exit(self):
        payload = {"address": "local/white-bison", "created": "pane"}
        output = StringIO()
        with (
            mock.patch.object(agent_skill_cli, "spawn_command", return_value=payload),
            mock.patch("sys.stdout", output),
        ):
            result = agent_skill_cli.main(["spawn", "--kind", "codex"], {})
        self.assertEqual(result, 0)
        self.assertEqual(json.loads(output.getvalue()), payload)

    def test_spawn_initial_prompt_outcome_keeps_placement_and_truthful_exit(self):
        for acceptance in ("accepted", "unknown", "failed"):
            with self.subTest(acceptance=acceptance):
                payload = {"address": "local/new-worker", "created": "pane", "pane_id": "new-pane",
                           "initial_prompt": {"operation_id": "operation", "submission_id": "submission",
                                              "acceptance": acceptance, "evidence": "Observed transport outcome"}}
                output = StringIO()
                with mock.patch.object(agent_skill_cli, "spawn_command", return_value=payload), \
                        mock.patch("sys.stdout", output):
                    code = agent_skill_cli.main(["spawn", "--kind", "codex", "--message", "Work"], {})
                result = json.loads(output.getvalue())
                self.assertEqual(code, 0 if acceptance == "accepted" else 1)
                self.assertEqual(result["pane_id"], "new-pane")
                self.assertEqual(result["address"], "local/new-worker")
                self.assertEqual(result["initial_prompt"]["acceptance"], acceptance)


class FocusDeferredCliTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.environment = {"HERDR_AGENT_REQUEST_STATE_DIR": directory.name}
        self.store = agent_skill_cli.JsonRequestStateStore(Path(directory.name))
        self.sender = agent(name="blue-raven", pane_id="w1:p1")
        self.recipient = agent(name="white-bison", pane_id="w1:p2")
        self.clock = 0.0
        self.sleeps = []
        sender_patch = mock.patch.object(
            agent_skill_cli, "current_sender", return_value=self.sender
        )
        sender_patch.start()
        self.addCleanup(sender_patch.stop)
        worker_patch = mock.patch.object(
            agent_skill_cli.agent_delivery, "ensure_worker", return_value=True
        )
        self.worker = worker_patch.start()
        self.addCleanup(worker_patch.stop)

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.clock += seconds

    def request(self, **overrides):
        arguments = {
            "host": "local", "label": self.recipient.name,
            "message": "Please review.", "timeout_ms": 1000,
            "output_lines": 10, "output_chars": 1000,
            "environment": self.environment, "state_store": self.store,
            "monotonic": lambda: self.clock, "sleep": self.sleep,
        }
        arguments.update(overrides)
        return agent_skill_cli.request_command(**arguments)

    def settled_request(self, *, verbose=False):
        resolver = mock.Mock(side_effect=[
            self.recipient, replace(self.recipient, status="working"),
            replace(self.recipient, status="done"),
        ])
        response = (
            "Message from local/blue-raven:\n\nPlease review.\n\n"
            "Review complete.\n"
        )
        runner = mock.Mock(side_effect=[
            subprocess.CompletedProcess([], 0, "", ""),
            subprocess.CompletedProcess([], 0, "", ""),
            subprocess.CompletedProcess([], 0, response, ""),
        ])
        return self.request(resolver=resolver, command_runner=runner, verbose=verbose)

    def deferred_send(self, **overrides):
        arguments = {
            "host": "local", "label": self.recipient.name,
            "message": "Please review.", "wait": True, "timeout_ms": 1000,
            "environment": self.environment,
        }
        arguments.update(overrides)
        with (
            mock.patch.object(
                agent_skill_cli, "resolve_labeled_agent",
                return_value=replace(self.recipient, focused=True),
            ),
            mock.patch.object(agent_skill_cli, "run_bounded_command") as runner,
        ):
            payload = agent_skill_cli.send_command(**arguments)
        runner.assert_not_called()
        return payload

    def test_request_compact_and_verbose_payloads(self):
        compact = self.settled_request()
        self.assertEqual(compact["state"], "submitted_settled")
        self.assertEqual(compact["address"], "local/white-bison")
        self.assertEqual(compact["response"]["text"], "Review complete.")
        self.assertFalse({"route", "sender", "recipient"} & compact.keys())
        record = self.store.load(compact["request_id"])
        self.assertEqual(len(record["_last_reported"]), 4)
        self.assertEqual(record["_last_reported"][:3], [
            "submitted_settled", "settled", None,
        ])
        verbose = self.settled_request(verbose=True)
        self.assertIn("route", verbose)
        self.assertIn("sender", verbose)
        self.assertIn("recipient", verbose)
        self.assertIn("output", verbose["response"])
        self.assertFalse(any(key.startswith("_") for key in verbose))

    def test_focused_request_defers_without_prompt_and_ensures_worker(self):
        focused = replace(self.recipient, focused=True)
        runner = mock.Mock()
        payload = self.request(resolver=lambda: focused, command_runner=runner)
        self.assertEqual(payload["state"], "deferred")
        self.assertFalse(payload["submitted"])
        self.assertEqual(payload["defer"]["reason"], "recipient_focused")
        self.assertNotIn("since", payload["defer"])
        runner.assert_not_called()
        self.worker.assert_called_with(self.environment)
        self.assertEqual(self.sleeps, [0.5, 0.5])
        self.assertIn("_last_reported", self.store.load(payload["request_id"]))

    def test_deferred_request_main_returns_three(self):
        output = StringIO()
        with (
            mock.patch.object(
                agent_skill_cli, "resolve_labeled_agent",
                return_value=replace(self.recipient, focused=True),
            ),
            mock.patch.object(agent_skill_cli, "run_bounded_command") as runner,
            mock.patch("sys.stdout", output),
        ):
            code = agent_skill_cli.main([
                "request", "--address", "local/white-bison", "--message", "work",
                "--timeout", "1", "--defer-timeout", "5000",
            ], self.environment)
        self.assertEqual(code, 3)
        self.assertEqual(json.loads(output.getvalue())["state"], "deferred")
        runner.assert_not_called()
        self.worker.assert_called_with(self.environment)

    def test_focused_self_request_and_send_fail_without_queue_or_worker(self):
        focused_sender = replace(self.sender, focused=True)
        runner = mock.Mock()
        request = self.request(
            label=self.sender.name, resolver=lambda: focused_sender, command_runner=runner,
        )
        self.assertEqual(request["error_code"], "recipient_is_sender")
        self.assertEqual(request["state"], "submission_failed")
        self.assertTrue(request["terminal"])
        with (
            mock.patch.object(
                agent_skill_cli, "resolve_labeled_agent", return_value=focused_sender
            ),
            mock.patch.object(agent_skill_cli, "run_bounded_command", runner),
            self.assertRaises(agent_skill_cli.SkillCommandError) as raised,
        ):
            agent_skill_cli.send_command(
                host="local", label=self.sender.name, message="work", wait=False,
                timeout_ms=1000, environment=self.environment,
            )
        self.assertEqual(raised.exception.code, "recipient_is_sender")
        self.assertEqual(agent_skill_cli.request_lifecycle.pending_deferred(self.store), [])
        runner.assert_not_called()
        self.worker.assert_not_called()


    def test_send_deferred_is_owned_and_stores_prompt_context(self):
        payload = self.deferred_send(defer_timeout_ms=54321)
        self.assertEqual(payload["state"], "deferred")
        self.assertFalse(payload["submitted"])
        record = self.store.load(payload["request_id"])
        self.assertEqual(record["_prompt"], (
            "Message from local/blue-raven:\n\nPlease review."
        ))
        self.assertEqual(record["_defer"]["timeout_ms"], 54321)
        self.assertEqual(record["sender"]["address"], "local/blue-raven")
        self.assertTrue(record["_context"]["route"])
        self.worker.assert_called_with(self.environment)

    def test_send_main_returns_three_without_waiting_when_deferred(self):
        output = StringIO()
        with (
            mock.patch.object(
                agent_skill_cli, "resolve_labeled_agent",
                return_value=replace(self.recipient, focused=True),
            ),
            mock.patch.object(agent_skill_cli, "run_bounded_command") as runner,
            mock.patch("sys.stdout", output),
        ):
            code = agent_skill_cli.main([
                "send", "--address", "local/white-bison", "--message", "work",
            ], self.environment)
        self.assertEqual(code, 3)
        self.assertEqual(json.loads(output.getvalue())["state"], "deferred")
        runner.assert_not_called()

    def test_immediate_send_compact_payload_and_no_worker(self):
        with (
            mock.patch.object(
                agent_skill_cli, "resolve_labeled_agent", return_value=self.recipient
            ),
            mock.patch.object(
                agent_skill_cli, "run_bounded_command",
                return_value=subprocess.CompletedProcess([], 0, "", ""),
            ) as runner,
        ):
            payload = agent_skill_cli.send_command(
                host="local", label=self.recipient.name, message="work",
                wait=False, timeout_ms=1000, environment=self.environment,
            )
        self.assertEqual(payload, {
            "sent": True, "address": "local/white-bison", "waited": False,
        })
        self.assertEqual(runner.call_count, 1)
        self.worker.assert_not_called()
        self.assertEqual(self.store.list_request_ids(), [])

    def test_send_resolution_failure_returns_terminal_request(self):
        with mock.patch.object(
            agent_skill_cli, "resolve_labeled_agent",
            side_effect=agent_skill_cli.SkillCommandError("agent_not_found", "missing"),
        ):
            payload = agent_skill_cli.send_command(
                host="local", label="missing", message="work", wait=False,
                timeout_ms=1000, environment=self.environment,
            )
        self.assertEqual(payload["state"], "submission_failed")
        self.assertEqual(payload["error_code"], "agent_not_found")
        self.assertEqual(payload["address"], "local/missing")
        self.worker.assert_not_called()

    def test_status_changed_only_and_verbose_always_full(self):
        initial = self.settled_request()
        request_id = initial["request_id"]
        unchanged = agent_skill_cli.request_status_command(
            request_id=request_id, state_store=self.store
        )
        self.assertEqual(unchanged, {
            "request_id": request_id, "state": "submitted_settled",
            "terminal": True, "submitted": True, "changed": False,
        })
        record = self.store.load(request_id)
        record["response"]["output"] = "A different answer."
        self.store.save(request_id, record)
        changed = agent_skill_cli.request_status_command(
            request_id=request_id, state_store=self.store
        )
        self.assertTrue(changed["changed"])
        self.assertEqual(changed["response"]["text"], "A different answer.")
        self.assertFalse(agent_skill_cli.request_status_command(
            request_id=request_id, state_store=self.store
        )["changed"])
        verbose = agent_skill_cli.request_status_command(
            request_id=request_id, state_store=self.store, verbose=True
        )
        self.assertIn("recipient", verbose)
        self.assertIn("output", verbose["response"])
        self.assertNotIn("_last_reported", verbose)

    def test_status_single_refresh_and_wait_stops_on_phase_change(self):
        initial = self.deferred_send()
        request_id = initial["request_id"]
        record = self.store.load(request_id)
        next_record = dict(record, phase="awaiting_decision", state="awaiting_decision")
        with mock.patch.object(
            agent_skill_cli.RequestLifecycleMachine, "refresh",
            side_effect=[record, record, next_record],
        ) as refresh:
            unchanged = agent_skill_cli.request_status_command(
                request_id=request_id, state_store=self.store
            )
            self.assertFalse(unchanged["changed"])
            self.assertEqual(refresh.call_count, 1)
            changed = agent_skill_cli.request_status_command(
                request_id=request_id, state_store=self.store, wait_ms=2000,
                monotonic=lambda: self.clock, sleep=self.sleep,
            )
        self.assertTrue(changed["changed"])
        self.assertEqual(changed["state"], "awaiting_decision")
        self.assertEqual(self.sleeps, [0.5])
        self.assertEqual(refresh.call_count, 3)

    def test_status_wait_deadline_is_bounded_and_response_hash_is_cleaned(self):
        initial = self.deferred_send()
        request_id = initial["request_id"]
        with mock.patch.object(
            agent_skill_cli.RequestLifecycleMachine, "refresh",
            return_value=self.store.load(request_id),
        ) as refresh:
            payload = agent_skill_cli.request_status_command(
                request_id=request_id, state_store=self.store, wait_ms=750,
                monotonic=lambda: self.clock, sleep=self.sleep,
            )
        self.assertFalse(payload["changed"])
        self.assertEqual(self.sleeps, [0.5, 0.25])
        self.assertEqual(refresh.call_count, 3)
        settled = self.settled_request()
        record = self.store.load(settled["request_id"])
        record["response"]["output"] = "\nReview complete.\n\n"
        self.store.save(settled["request_id"], record)
        payload = agent_skill_cli.request_status_command(
            request_id=settled["request_id"], state_store=self.store
        )
        self.assertFalse(payload["changed"])

    def test_status_wait_stops_on_error_code_change(self):
        initial = self.deferred_send()
        record = self.store.load(initial["request_id"])
        changed = dict(record, error_code="recipient_changed")
        with mock.patch.object(
            agent_skill_cli.RequestLifecycleMachine, "refresh", return_value=changed
        ) as refresh:
            payload = agent_skill_cli.request_status_command(
                request_id=initial["request_id"], state_store=self.store, wait_ms=5000,
                monotonic=lambda: self.clock, sleep=self.sleep,
            )
        self.assertEqual(refresh.call_count, 1)
        self.assertEqual(self.sleeps, [])
        self.assertTrue(payload["changed"])
        self.assertEqual(payload["error_code"], "recipient_changed")

    def test_defer_timeout_and_wait_validation_and_help(self):
        for command, flags in (
            ("send", ["--address", "local/white-bison", "--message", "work"]),
            ("request", ["--address", "local/white-bison", "--message", "work"]),
            ("batch", ["--requests-json", "[]"]),
        ):
            for timeout in ("0", "-1"):
                output = StringIO()
                with mock.patch("sys.stdout", output):
                    code = agent_skill_cli.main([
                        command, *flags, "--defer-timeout", timeout,
                    ], self.environment)
                self.assertEqual(code, 1)
                self.assertEqual(
                    json.loads(output.getvalue())["error"]["code"],
                    "invalid_defer_timeout",
                )
        output = StringIO()
        with mock.patch("sys.stdout", output):
            code = agent_skill_cli.main([
                "request-status", "--request-id", "a" * 32, "--wait", "-1",
            ], self.environment)
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(output.getvalue())["error"]["code"], "invalid_timeout")
        for command in ("send", "read", "request", "request-status", "batch"):
            output = StringIO()
            with mock.patch("sys.stdout", output), self.assertRaises(SystemExit):
                agent_skill_cli.parse_cli_arguments([command, "--help"])
            self.assertIn("--verbose", output.getvalue())
            if command in {"send", "request", "batch"}:
                self.assertIn("--defer-timeout", output.getvalue())
            if command == "request-status":
                self.assertIn("--wait", output.getvalue())

    def test_transport_reconstruction_rejects_bad_context(self):
        for context in (
            None, [], {}, {"host": 1, "label": "bird"},
            {"host": "", "label": "bird"}, {"host": "local"},
            {"host": "local", "label": 1},
            {"host": "local", "route": False},
            {"host": "local", "label": "bird", "route": "route"},
        ):
            with self.subTest(context=context), self.assertRaises(
                agent_skill_cli.SkillCommandError
            ) as raised:
                agent_skill_cli.transport_for_record({"_context": context}, {})
            self.assertEqual(raised.exception.code, "request_state_invalid")

    def test_transport_reconstruction_uses_context_and_no_sender(self):
        for context in (
            {"host": "remote", "label": "bird", "route": None},
            {"host": "local", "label": None, "route": "route"},
        ):
            with mock.patch.object(
                agent_skill_cli, "resolve_or_refresh_recipient",
                return_value=agent_skill_cli.RouteResolution(
                    self.sender, route_refreshed=False, route="route"
                ),
            ) as resolve, mock.patch.object(
                agent_skill_cli, "run_bounded_command",
                return_value=subprocess.CompletedProcess([], 0, "", ""),
            ) as runner:
                transport = agent_skill_cli.transport_for_record(
                    {"_context": context}, self.environment
                )
                target = transport.resolve()
                transport.submit(target, "work")
            resolve.assert_called_once_with(**context, environment=self.environment)
            runner.assert_called_once()
            self.assertFalse(transport.is_focused(target))
            self.assertTrue(transport.is_focused(replace(target, focused=True)))

    def test_batch_deferred_items_are_compact_and_exit_three(self):
        for wait in (True, False):
            output = StringIO()
            handler = "request_command" if wait else "send_command"
            deferred = {
                "request_id": "a" * 32, "state": "deferred", "terminal": False,
                "submitted": False, "phase": "deferred_recipient_focused",
                "recipient": {"host": "local", "name": "one"},
            }
            with (
                mock.patch.object(
                    agent_skill_cli, "resolve_or_refresh_routed_agent",
                    new=batch_route_resolution,
                ),
                mock.patch.object(
                    agent_skill_cli, handler, return_value=deferred
                ) as send,
                mock.patch("sys.stdout", output),
            ):
                code = agent_skill_cli.main([
                    "batch", "--requests-json", '[{"route":"one","message":"work"}]',
                    "--wait" if wait else "--no-wait", "--defer-timeout", "4321",
                ], self.environment)
            self.assertEqual(code, 3)
            self.assertEqual(json.loads(output.getvalue())["results"], [{
                "address": "local/one", "status": "deferred", "request_id": "a" * 32,
                "state": "deferred",
                "response": {"text": "", "truncated": False},
            }])
            self.assertEqual(send.call_args.kwargs["defer_timeout_ms"], 4321)

    def test_status_main_acknowledges_only_after_json_output(self):
        initial = self.settled_request()
        request_id = initial["request_id"]
        record = self.store.load(request_id)
        previous = record["_last_reported"]
        record["response"]["output"] = "New response."
        self.store.save(request_id, record)
        writer = agent_skill_cli._write_json
        output = StringIO()

        def write_json(payload, **options):
            self.assertEqual(
                self.store.load(request_id)["_last_reported"], previous
            )
            writer(payload, **options)

        with (
            mock.patch.object(agent_skill_cli, "_write_json", side_effect=write_json),
            mock.patch("sys.stdout", output),
        ):
            code = agent_skill_cli.main([
                "request-status", "--request-id", request_id,
            ], self.environment)
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(output.getvalue())["changed"])
        self.assertNotEqual(self.store.load(request_id)["_last_reported"], previous)

    def test_batch_compact_removes_prompt_echo_but_verbose_retains_output(self):
        echoed = "Message from winmini/blue-raven:\n\nwork\n\nAnswer.\n"
        lifecycle = {
            "state": "submitted_settled", "request_id": "a" * 32,
            "sender": {"address": "local/blue-raven"},
            "response": {"output": echoed, "truncated": False, "correlated": True},
        }
        self.store.save(lifecycle["request_id"], dict(
            lifecycle, _prompt="Message from winmini/blue-raven:\n\nwork",
        ))
        for verbose in (False, True):
            with (
                mock.patch.object(
                    agent_skill_cli, "resolve_or_refresh_routed_agent",
                    new=batch_route_resolution,
                ),
                mock.patch.object(
                    agent_skill_cli, "request_command", return_value=lifecycle
                ),
            ):
                result = agent_skill_cli.batch_command(
                    requests_json='[{"route":"one","message":"work"}]',
                    wait=True, timeout_ms=1000, max_workers=1, verbose=verbose,
                    environment=self.environment,
                )
            response = result["results"][0]["response"]
            if verbose:
                self.assertEqual(response["output"], echoed)
            else:
                self.assertEqual(response["text"], "Answer.")
                self.assertNotIn("route", result["results"][0])

    def test_first_request_status_after_batch_includes_full_compact_details(self):
        request_command = agent_skill_cli.request_command
        prompt = "Message from winmini/blue-raven:\n\nwork"

        def send_request(**arguments):
            resolver = mock.Mock(side_effect=[
                self.recipient, replace(self.recipient, status="working"),
                replace(self.recipient, status="done"),
            ])
            runner = mock.Mock(side_effect=[
                subprocess.CompletedProcess([], 0, "old screen", ""),
                subprocess.CompletedProcess([], 0, "", ""),
                subprocess.CompletedProcess([], 0, prompt + "\n\nOK\n", ""),
            ])
            return request_command(
                **arguments, resolver=resolver, command_runner=runner,
                state_store=self.store, monotonic=lambda: self.clock, sleep=self.sleep,
            )

        with (
            mock.patch.object(
                agent_skill_cli, "current_sender", return_value=replace(self.sender, host="winmini")
            ),
            mock.patch.object(
                agent_skill_cli, "resolve_or_refresh_routed_agent", new=batch_route_resolution
            ),
            mock.patch.object(agent_skill_cli, "request_command", side_effect=send_request),
        ):
            batch = agent_skill_cli.batch_command(
                requests_json='[{"route":"one","message":"work"}]',
                wait=True, timeout_ms=1000, max_workers=1, environment=self.environment,
            )
        item = batch["results"][0]
        self.assertEqual(item["response"]["text"], "OK")
        self.assertFalse(item["response"]["truncated"])
        self.assertNotIn("_last_reported", self.store.load(item["request_id"]))
        status = agent_skill_cli.request_status_command(
            request_id=item["request_id"], state_store=self.store,
        )
        self.assertTrue(status["changed"])
        self.assertEqual(status["response"]["text"], "OK")
        self.assertTrue(status["response"]["correlated"])
        self.assertTrue(status["response"]["anchored"])
        self.assertFalse(status["response"]["truncated"])
        self.assertFalse(agent_skill_cli.request_status_command(
            request_id=item["request_id"], state_store=self.store,
        )["changed"])

    def test_worker_cancellation_is_not_overwritten_by_route_reporting(self):
        def cancel_pending(environment):
            for request_id in self.store.list_request_ids():
                agent_skill_cli.RequestLifecycleMachine(
                    mock.Mock(), self.store
                ).cancel_deferred(request_id)
            return True

        self.worker.side_effect = cancel_pending
        initial = self.deferred_send()
        status = agent_skill_cli.request_status_command(
            request_id=initial["request_id"], state_store=self.store
        )
        self.assertEqual(status["state"], "submission_failed")
        self.assertEqual(status["error_code"], "delivery_cancelled")
        self.assertTrue(status["changed"])

    def test_state_store_uses_shared_precedence(self):
        root = self.store.directory
        environment = {
            "HERDR_AGENT_REQUEST_STATE_DIR": str(root / "explicit"),
            "HERDR_PLUGIN_STATE_DIR": str(root / "plugin"),
            "XDG_STATE_HOME": str(root / "xdg"), "HOME": str(root / "home"),
        }
        self.assertEqual(
            agent_skill_cli._request_state_store(environment).directory, root / "explicit"
        )
        environment.pop("HERDR_AGENT_REQUEST_STATE_DIR")
        self.assertEqual(
            agent_skill_cli._request_state_store(environment).directory,
            root / "xdg/herdr-agent-messenger/requests",
        )
        with mock.patch.object(
            agent_skill_cli.request_lifecycle, "open_request_store",
            return_value=self.store,
        ) as open_store:
            self.assertIs(agent_skill_cli._request_state_store(environment), self.store)
        open_store.assert_called_once_with(environment)


class AgentSkillWrapperTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.wrapper = load_skill_wrapper()

    def test_bundled_wrapper_finds_repository_plugin_root(self):
        runner = mock.Mock()
        self.assertEqual(
            self.wrapper.find_plugin_root(SKILL_WRAPPER_PATH, runner),
            Path(__file__).resolve().parent,
        )
        runner.assert_not_called()

    def test_standalone_wrapper_uses_installed_plugin_root(self):
        plugin_root = Path(__file__).resolve().parent
        completed = subprocess.CompletedProcess(
            ["herdr"],
            0,
            (
                '{"result":{"plugins":[{"plugin_root":"'
                + str(plugin_root)
                + '"}]}}'
            ),
            "",
        )
        runner = mock.Mock(return_value=completed)

        self.assertEqual(
            self.wrapper.find_plugin_root(
                Path("/tmp/copied-skill/scripts/herdr_agent_messenger.py"),
                runner,
                {},
            ),
            plugin_root,
        )
        self.assertEqual(
            runner.call_args.args[0],
            [
                "herdr",
                "plugin",
                "list",
                "--json",
                "--plugin",
                "herdr.agent-messenger",
            ],
        )

    def test_standalone_wrapper_uses_injected_herdr_binary(self):
        plugin_root = Path(__file__).resolve().parent
        completed = subprocess.CompletedProcess(
            ["/opt/herdr"],
            0,
            json.dumps(
                {
                    "result": {
                        "plugins": [{"plugin_root": str(plugin_root)}],
                    }
                }
            ),
            "",
        )
        runner = mock.Mock(return_value=completed)

        self.assertEqual(
            self.wrapper.find_plugin_root(
                Path("/tmp/copied-skill/scripts/herdr_agent_messenger.py"),
                runner,
                {"HERDR_BIN_PATH": "/opt/herdr"},
            ),
            plugin_root,
        )
        self.assertEqual(runner.call_args.args[0][0], "/opt/herdr")

    def test_standalone_wrapper_falls_back_to_legacy_plugin_id(self):
        plugin_root = Path(__file__).resolve().parent
        missing = subprocess.CompletedProcess(
            ["herdr"],
            0,
            '{"result":{"plugins":[]}}',
            "",
        )
        legacy = subprocess.CompletedProcess(
            ["herdr"],
            0,
            (
                '{"result":{"plugins":[{"plugin_root":"'
                + str(plugin_root)
                + '"}]}}'
            ),
            "",
        )
        runner = mock.Mock(side_effect=(missing, legacy))

        self.assertEqual(
            self.wrapper.find_plugin_root(
                Path("/tmp/copied-skill/scripts/herdr_agent_messenger.py"),
                runner,
            ),
            plugin_root,
        )
        self.assertEqual(
            runner.call_args_list[1].args[0][-1],
            "herdr.agent-labels",
        )

    def test_standalone_wrapper_rejects_invalid_plugin_responses(self):
        cases = (
            subprocess.CompletedProcess(["herdr"], 0, "not-json", ""),
            subprocess.CompletedProcess(["herdr"], 0, '{"result":{"plugins":[]}}', ""),
            subprocess.CompletedProcess(
                ["herdr"],
                0,
                '{"result":{"plugins":[{"plugin_root":"/missing/plugin"}]}}',
                "",
            ),
        )
        for completed in cases:
            output = StringIO()
            with (
                self.subTest(stdout=completed.stdout),
                mock.patch("sys.stdout", output),
                self.assertRaises(SystemExit) as raised,
            ):
                self.wrapper.find_plugin_root(
                    Path("/tmp/copied-skill/scripts/herdr_agent_messenger.py"),
                    mock.Mock(return_value=completed),
                )
            self.assertEqual(raised.exception.code, 1)
            self.assertEqual(
                json.loads(output.getvalue())["error"]["code"], "cli_not_found"
            )

    def test_standalone_wrapper_preserves_plugin_probe_failure(self):
        failed = subprocess.CompletedProcess(
            ["herdr"],
            1,
            "",
            "Operation not permitted",
        )
        output = StringIO()
        with (
            mock.patch("sys.stdout", output),
            self.assertRaises(SystemExit) as raised,
        ):
            self.wrapper.find_plugin_root(
                Path("/tmp/copied-skill/scripts/herdr_agent_messenger.py"),
                mock.Mock(return_value=failed),
            )

        self.assertEqual(raised.exception.code, 1)
        error = json.loads(output.getvalue())["error"]
        self.assertEqual(error["code"], "cli_not_found")
        self.assertIn("Operation not permitted", error["message"])

    def test_standalone_wrapper_preserves_plugin_command_launch_failure(self):
        output = StringIO()
        with (
            mock.patch("sys.stdout", output),
            self.assertRaises(SystemExit) as raised,
        ):
            self.wrapper.find_plugin_root(
                Path("/tmp/copied-skill/scripts/herdr_agent_messenger.py"),
                mock.Mock(side_effect=OSError("herdr unavailable")),
            )

        self.assertEqual(raised.exception.code, 1)
        error = json.loads(output.getvalue())["error"]
        self.assertEqual(error["code"], "cli_not_found")
        self.assertIn("herdr unavailable", error["message"])


if __name__ == "__main__":
    unittest.main()
