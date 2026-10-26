#!/usr/bin/env python3

import tests.ham_test_support as ham_test_support  # Protect the live ledger before product imports.
import base64
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from ham.agents import directory as agent_directory


class AgentDirectoryTest(unittest.TestCase):
    def test_snapshot_pane_focus_is_preserved_for_local_and_remote_agents(self):
        payload = {"result": {"snapshot": {
            "agents": [
                {"pane_id": "w:p1", "agent": "codex"},
                {"pane_id": "w:p2", "agent": "omp"},
            ],
            "panes": [
                {"pane_id": "w:p1", "focused": True},
                {"pane_id": "w:p2", "focused": False},
            ],
            "focused_pane_id": "w:p1",
        }}}
        completed = subprocess.CompletedProcess(["herdr"], 0, json.dumps(payload), "")
        with mock.patch.object(agent_directory, "_run_command", return_value=completed):
            probes = (
                agent_directory.probe_local_agents({}),
                agent_directory.query_remote_agents("remote", config_path=Path("/tmp/config")),
            )
        for probe in probes:
            with self.subTest(host=probe.host):
                self.assertTrue(probe.success)
                self.assertEqual([agent.focused for agent in probe.agents], [True, False])

    def test_focus_falls_back_to_snapshot_focused_pane_id(self):
        payload = {"result": {"snapshot": {
            "agents": [{"pane_id": "w:p1", "agent": "codex"}],
            "focused_pane_id": "w:p1",
        }}}
        agents = agent_directory.parse_agent_payload(payload, host="local", local=True)
        self.assertTrue(agents[0].focused)
        payload["result"]["snapshot"]["panes"] = [{"pane_id": "w:p1"}]
        agents = agent_directory.parse_agent_payload(payload, host="local", local=True)
        self.assertTrue(agents[0].focused)
        payload["result"]["snapshot"]["panes"] = [{"pane_id": "w:p1", "focused": False}]
        agents = agent_directory.parse_agent_payload(payload, host="local", local=True)
        self.assertFalse(agents[0].focused)

    def test_focus_only_change_preserves_identity_route_and_occupant(self):
        from ham.agents.route import encode_agent_route, resolve_agent_route

        for session_id in ("session", ""):
            with self.subTest(session_id=session_id):
                original = agent_directory.AgentRecord(
                    "remote", "worker", "w:p1", "w", "work", "idle",
                    session_id, "/work", False, revision=1,
                    agent_kind="codex", terminal_id="terminal",
                )
                focused = agent_directory.replace(original, focused=True)
                route = encode_agent_route(original)
                self.assertEqual(original, focused)
                self.assertEqual(original.identity, focused.identity)
                self.assertEqual(route, encode_agent_route(focused))
                self.assertFalse(resolve_agent_route(route, [focused]).route_refreshed)
                self.assertIs(agent_directory._matching_agent(original, [focused]), focused)

    def test_malformed_revisions_fail_local_and_remote_probes_cleanly(self):
        for revision in ("bad", [], {}, True, 1.5, -1):
            payload = {"result": {"agents": [
                {"pane_id": "w:p1", "agent": "codex", "revision": revision},
            ]}}
            result = subprocess.CompletedProcess(["herdr"], 0, json.dumps(payload), "")
            with self.subTest(revision=revision), mock.patch.object(
                agent_directory, "_run_command", return_value=result,
            ):
                local = agent_directory.probe_local_agents({})
                remote = agent_directory.query_remote_agents("remote", config_path=Path("/tmp/config"))
            for probe in (local, remote):
                self.assertFalse(probe.success)
                self.assertEqual(probe.error, "invalid_response")

    def test_async_discovery_reports_malformed_agent_data(self):
        discovery = agent_directory.RemoteDiscovery(["remote"], config_path=Path("/tmp/config"))
        discovery.deadline = time.monotonic() + 5
        process = mock.Mock(returncode=0)
        payload = json.dumps({"result": {"agents": [
            {"pane_id": "w:p1", "agent": "codex", "revision": "bad"},
        ]}})
        with (
            mock.patch.object(agent_directory.subprocess, "Popen", return_value=process),
            mock.patch.object(agent_directory, "_communicate_bounded", return_value=(payload, "")),
        ):
            discovery._probe_host("remote")
        self.assertEqual(discovery.poll(), [
            agent_directory.ProbeResult("remote", (), False, "invalid_response"),
        ])

    def test_cache_rejects_invalid_fields_and_transport_scope(self):
        record = agent_directory.AgentRecord(
            "remote", "worker", "w:p1", "w", "work", "idle", "session", "/work", False,
        )
        invalid_fields = (
            {"name": []}, {"revision": "1"}, {"revision": True},
            {"host": "another-host"}, {"local": True},
        )
        with tempfile.TemporaryDirectory() as directory:
            cache = agent_directory.AgentCache(Path(directory) / "cache.json")
            for changed in invalid_fields:
                with self.subTest(changed=changed):
                    cache.update(agent_directory.ProbeResult("remote", (record,), True))
                    cache.host_entry("remote")["agents"][0].update(changed)
                    self.assertEqual(cache.agents("remote"), [])

    def test_non_utf8_cache_and_ssh_files_are_ignored(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "invalid"
            path.write_bytes(b"\xff")
            self.assertEqual(agent_directory.AgentCache(path).agents("remote"), [])
            self.assertEqual(agent_directory.parse_ssh_hosts(path), [])
            self.assertEqual(agent_directory.parse_ssh_hosts_allowlist(path), [])

    def test_future_cache_timestamp_requires_a_new_probe(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = agent_directory.AgentCache(Path(directory) / "cache.json")
            cache.update(agent_directory.ProbeResult("remote", (), True))
            updated = cache.host_entry("remote")["updated_at"]
            self.assertFalse(cache.is_fresh("remote", updated - 100))

    def test_local_probe_preserves_snapshot_failure_details(self):
        completed = subprocess.CompletedProcess(
            ["herdr", "api", "snapshot"],
            1,
            "",
            "Operation not permitted",
        )
        with mock.patch.object(
            agent_directory,
            "_run_command",
            return_value=completed,
        ):
            result = agent_directory.probe_local_agents({})

        self.assertFalse(result.success)
        self.assertEqual(result.agents, ())
        self.assertEqual(result.error, "Operation not permitted")

    def test_local_probe_rejects_invalid_snapshot_without_hiding_failure(self):
        completed = subprocess.CompletedProcess(
            ["herdr", "api", "snapshot"],
            0,
            '{"result": {}}',
            "",
        )
        with mock.patch.object(
            agent_directory,
            "_run_command",
            return_value=completed,
        ):
            result = agent_directory.probe_local_agents({})

        self.assertFalse(result.success)
        self.assertEqual(result.error, "invalid_response")

    def test_local_agent_query_keeps_empty_on_failure_compatibility(self):
        failed = agent_directory.ProbeResult(
            "local",
            (),
            False,
            "Operation not permitted",
        )
        with mock.patch.object(
            agent_directory,
            "probe_local_agents",
            return_value=failed,
        ):
            self.assertEqual(agent_directory.query_local_agents({}), [])

    def test_fetch_local_agent_reuses_probe_and_selects_invoking_pane(self):
        first = agent_directory.AgentRecord(
            host="local",
            name="blue-eagle",
            pane_id="w1:p1",
            workspace_id="w1",
            workspace_label="project",
            status="idle",
            session_id="session-1",
            cwd="/work/project",
            local=True,
        )
        second = agent_directory.replace(
            first,
            name="red-gecko",
            pane_id="w1:p2",
            session_id="session-2",
        )
        probe = agent_directory.ProbeResult("local", (first, second), True)
        cancel = threading.Event()
        with mock.patch.object(
            agent_directory,
            "probe_local_agents",
            return_value=probe,
        ) as local_probe:
            self.assertEqual(
                agent_directory.fetch_local_agent(
                    "w1:p2",
                    {"HERDR_BIN_PATH": "/opt/herdr"},
                    cancel_event=cancel,
                ),
                second,
            )

        local_probe.assert_called_once_with(
            {"HERDR_BIN_PATH": "/opt/herdr"},
            cancel_event=cancel,
        )

    def test_parse_ssh_hosts_keeps_only_concrete_aliases(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "config"
            config.write_text(
                """
                Host macbook-pro desktop
                  User tester
                Host *.example.com !blocked
                  User deploy
                Host -unsafe
                Host rogallyx
                """,
                encoding="utf-8",
            )
            self.assertEqual(
                agent_directory.parse_ssh_hosts(config),
                ["macbook-pro", "desktop", "rogallyx"],
            )

    def test_parse_ssh_hosts_follows_recursive_includes_without_cycles(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            included = root / "hosts"
            config = root / "config"
            config.write_text(
                "Include hosts\nHost root-host\n",
                encoding="utf-8",
            )
            included.write_text(
                "Include config\nHost included-host\n",
                encoding="utf-8",
            )
            self.assertEqual(
                agent_directory.parse_ssh_hosts(config),
                ["included-host", "root-host"],
            )

    def test_ssh_host_allowlist_restricts_discovery_scope(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "config"
            allowlist = root / "ssh-hosts"
            config.write_text(
                "Host macbook-pro desktop rogallyx\n",
                encoding="utf-8",
            )
            environment = {
                "HERDR_AGENT_MESSENGER_SSH_CONFIG": str(config),
                "HERDR_AGENT_MESSENGER_SSH_HOSTS_FILE": str(allowlist),
            }

            self.assertEqual(
                agent_directory.ssh_hosts(environment),
                ["macbook-pro", "desktop", "rogallyx"],
            )
            allowlist.write_text(
                "# Personal Herdr peers\nrogallyx macbook-pro\nunknown-host\n",
                encoding="utf-8",
            )
            self.assertEqual(
                agent_directory.ssh_hosts(environment),
                ["macbook-pro", "rogallyx"],
            )
            allowlist.write_text("# Local only\n", encoding="utf-8")
            self.assertEqual(agent_directory.ssh_hosts(environment), [])

    def test_new_ssh_environment_names_precede_legacy_names(self):
        environment = {
            "HERDR_AGENT_MESSENGER_SSH_CONFIG": "/new/config",
            "HERDR_AGENT_LABELS_SSH_CONFIG": "/legacy/config",
            "HERDR_AGENT_MESSENGER_SSH_HOSTS_FILE": "/new/hosts",
            "HERDR_AGENT_LABELS_SSH_HOSTS_FILE": "/legacy/hosts",
        }

        self.assertEqual(
            agent_directory.ssh_config_path(environment),
            Path("/new/config"),
        )
        self.assertEqual(
            agent_directory.ssh_hosts_allowlist_path(environment),
            Path("/new/hosts"),
        )

    def test_allowlist_default_falls_back_to_legacy_plugin_config(self):
        with tempfile.TemporaryDirectory() as directory:
            config_root = Path(directory)
            current = config_root / agent_directory.PLUGIN_ID
            legacy = config_root / agent_directory.LEGACY_PLUGIN_ID
            legacy.mkdir()
            legacy_allowlist = legacy / "ssh-hosts"
            legacy_allowlist.write_text("macbook-pro\n", encoding="utf-8")

            self.assertEqual(
                agent_directory.ssh_hosts_allowlist_path(
                    {"HERDR_PLUGIN_CONFIG_DIR": str(current)}
                ),
                legacy_allowlist,
            )

    def test_cache_default_falls_back_to_legacy_state_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            legacy = home / ".local/state/herdr-agent-labels"
            legacy.mkdir(parents=True)
            with mock.patch.object(agent_directory.Path, "home", return_value=home):
                cache = agent_directory.AgentCache.from_environment({})

            self.assertEqual(cache.path, legacy / "agent-directory.json")

    def test_cache_runtime_state_falls_back_to_legacy_plugin_sibling(self):
        with tempfile.TemporaryDirectory() as directory:
            state_root = Path(directory)
            current = state_root / agent_directory.PLUGIN_ID
            legacy = state_root / agent_directory.LEGACY_PLUGIN_ID
            current.mkdir()
            legacy.mkdir()
            legacy_cache = legacy / "agent-directory.json"
            legacy_cache.write_text(
                '{"version":1,"hosts":{}}',
                encoding="utf-8",
            )

            cache = agent_directory.AgentCache.from_environment(
                {"HERDR_PLUGIN_STATE_DIR": str(current)}
            )

            self.assertEqual(cache.path, legacy_cache)

    def test_parse_agent_payload_builds_addressable_records(self):
        payload = {
            "result": {
                "agents": [
                    {
                        "agent": "codex",
                        "agent_session": {"value": "session-1"},
                        "agent_status": "idle",
                        "cwd": "/work/project",
                        "name": "blue-raven",
                        "pane_id": "w1:p1",
                        "workspace_id": "w1",
                    }
                ]
            }
        }
        records = agent_directory.parse_agent_payload(payload, host="macbook", local=False)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].qualified_name, "macbook/blue-raven")
        self.assertEqual(records[0].session_id, "session-1")
        self.assertEqual(records[0].workspace_label, "project")

    def test_snapshot_uses_unicode_workspace_label_and_worktree_metadata(self):
        payload = {
            "result": {
                "snapshot": {
                    "agents": [
                        {
                            "agent": "codex",
                            "agent_status": "idle",
                            "cwd": "/work/repo/.herdr/worktrees/feature-a",
                            "name": "blue-raven",
                            "pane_id": "w1:p1",
                            "workspace_id": "w1",
                        }
                    ],
                    "workspaces": [
                        {
                            "workspace_id": "w1",
                            "label": "결제 기능 작업트리",
                            "worktree": {
                                "checkout_path": "/work/repo/.herdr/worktrees/feature-a",
                                "is_linked_worktree": True,
                            },
                        }
                    ],
                }
            }
        }

        records = agent_directory.parse_agent_payload(
            payload,
            host="local",
            local=True,
        )

        self.assertEqual(records[0].workspace_label, "결제 기능 작업트리")
        self.assertTrue(records[0].workspace_is_worktree)

    def test_snapshot_does_not_badge_the_primary_checkout_as_a_worktree(self):
        payload = {
            "result": {
                "snapshot": {
                    "agents": [
                        {
                            "agent": "codex",
                            "cwd": "/work/repo",
                            "pane_id": "w1:p1",
                            "workspace_id": "w1",
                        }
                    ],
                    "workspaces": [
                        {
                            "workspace_id": "w1",
                            "label": "기본 체크아웃",
                            "worktree": {
                                "checkout_path": "/work/repo",
                                "is_linked_worktree": False,
                            },
                        }
                    ],
                }
            }
        }

        records = agent_directory.parse_agent_payload(
            payload,
            host="local",
            local=True,
        )

        self.assertEqual(records[0].workspace_label, "기본 체크아웃")
        self.assertFalse(records[0].workspace_is_worktree)

    def test_display_metadata_alias_routes_through_current_pane(self):
        records = agent_directory.parse_agent_payload(
            {
                "result": {
                    "agents": [
                        {
                            "agent": "codex",
                            "display_agent": "🟪 purple-koala",
                            "pane_id": "w1:p4",
                            "tokens": {
                                "alias": "purple-koala",
                                "color": "purple",
                            },
                        }
                    ]
                }
            },
            host="macbook-pro",
            local=False,
        )

        self.assertEqual(records[0].name, "purple-koala")
        self.assertEqual(records[0].target, "w1:p4")
        self.assertEqual(records[0].qualified_name, "macbook-pro/purple-koala")

    def test_unverified_display_metadata_is_not_an_addressable_label(self):
        records = agent_directory.parse_agent_payload(
            {
                "result": {
                    "agents": [
                        {
                            "agent": "codex",
                            "display_agent": "someone else",
                            "pane_id": "w1:p4",
                            "tokens": {"alias": "purple-koala"},
                        }
                    ]
                }
            },
            host="macbook-pro",
            local=False,
        )

        self.assertEqual(records[0].name, "")
        self.assertEqual(records[0].target, "w1:p4")

    def test_agent_list_payload_requires_expected_result_shape(self):
        self.assertTrue(
            agent_directory.is_agent_list_payload({"result": {"agents": []}})
        )
        self.assertFalse(agent_directory.is_agent_list_payload({"result": {}}))
        self.assertFalse(agent_directory.is_agent_list_payload({"message": "ok"}))

    def test_cache_uses_different_success_and_failure_ttls(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = agent_directory.AgentCache(Path(directory) / "cache.json")
            cache.update(agent_directory.ProbeResult("host", (), True))
            now = time.time()
            self.assertTrue(cache.is_fresh("host", now + 9))
            self.assertFalse(cache.is_fresh("host", now + 11))

            cache.update(agent_directory.ProbeResult("host", (), False, "timeout"))
            now = time.time()
            self.assertTrue(cache.is_fresh("host", now + 29))
            self.assertFalse(cache.is_fresh("host", now + 31))

    def test_cache_round_trip_preserves_agents(self):
        record = agent_directory.AgentRecord(
            host="macbook",
            name="blue-raven",
            pane_id="w1:p1",
            workspace_id="w1",
            workspace_label="project",
            status="idle",
            session_id="session-1",
            cwd="/work/project",
            local=False,
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cache.json"
            cache = agent_directory.AgentCache(path)
            cache.update(agent_directory.ProbeResult("macbook", (record,), True))
            cache.save()
            loaded = agent_directory.AgentCache(path)
            self.assertEqual(loaded.agents("macbook"), [record])

    def test_failed_probe_preserves_last_successful_agents_as_stale(self):
        record = agent_directory.AgentRecord(
            "macbook",
            "blue-raven",
            "w1:p1",
            "w1",
            "project",
            "idle",
            "session-1",
            "/work/project",
            False,
        )
        with tempfile.TemporaryDirectory() as directory:
            cache = agent_directory.AgentCache(Path(directory) / "cache.json")
            cache.update(agent_directory.ProbeResult("macbook", (record,), True))
            cache.update(
                agent_directory.ProbeResult("macbook", (), False, "timeout")
            )
            stale = cache.agents("macbook", stale=True)

        self.assertEqual(len(stale), 1)
        self.assertEqual(stale[0].identity, record.identity)
        self.assertTrue(stale[0].stale)

    def test_noisy_remote_output_extracts_last_agent_list_json_line(self):
        payload = {
            "result": {
                "agents": [
                    {
                        "agent": "codex",
                        "pane_id": "w1:p1",
                        "name": "blue-raven",
                    }
                ]
            }
        }
        completed = subprocess.CompletedProcess(
            ["ssh"],
            0,
            f"profile banner\n{json.dumps(payload)}\n",
            "",
        )
        with mock.patch.object(agent_directory, "_run_command", return_value=completed):
            result = agent_directory.query_remote_agents(
                "macbook",
                config_path=Path("/tmp/config"),
            )
        self.assertTrue(result.success)
        self.assertEqual(result.agents[0].name, "blue-raven")

    def test_remote_command_quotes_prompt_as_one_argument(self):
        prompt = "Review this; do not expand $HOME"
        command = agent_directory._remote_herdr_command(
            ["agent", "prompt", "blue-raven", prompt]
        )
        shell, login_flag, decoder, name, encoded = shlex.split(command)
        self.assertEqual((shell, login_flag, name), ("sh", "-c", "ham"))
        self.assertNotIn(prompt, decoder)
        self.assertIn(shlex.quote(prompt), base64.b64decode(encoded).decode("utf-8"))

    def test_ssh_command_hardens_probe_without_overriding_host_key_policy(self):
        command = agent_directory.ssh_command(
            "macbook",
            ["agent", "list"],
            config_path=Path("/tmp/ssh-config"),
        )
        self.assertNotIn("StrictHostKeyChecking=accept-new", command)
        self.assertIn("ClearAllForwardings=yes", command)
        self.assertIn("ForwardAgent=no", command)
        self.assertIn("ForwardX11=no", command)
        self.assertIn("PermitLocalCommand=no", command)

    def test_ssh_program_command_reuses_transport_and_quotes_cwd(self):
        command = agent_directory.ssh_program_command(
            "macbook",
            ["python3", "-m", "unittest", "-q"],
            config_path=Path("/tmp/ssh-config"),
            cwd="/tmp/plugin checkout",
        )

        self.assertEqual(command[-2], "macbook")
        self.assertIn("ClearAllForwardings=yes", command)
        self.assertIn("ForwardAgent=no", command)
        self.assertNotIn("StrictHostKeyChecking=accept-new", command)
        shell, login_flag, decoder, name, encoded = shlex.split(command[-1])
        self.assertEqual((shell, login_flag, name), ("sh", "-c", "ham"))
        script = base64.b64decode(encoded).decode("utf-8")
        self.assertNotIn("/tmp/plugin checkout", decoder)
        self.assertIn("cd -- '/tmp/plugin checkout'", script)
        self.assertIn("exec python3 -m unittest -q", script)

    def test_dispatch_revalidates_agent_before_local_prompt(self):
        sender = agent_directory.AgentRecord(
            "local", "blue-raven", "w1:p1", "w1", "project", "idle", "s1", "/work", True
        )
        recipient = agent_directory.AgentRecord(
            "local", "red-fox", "w1:p2", "w1", "project", "idle", "s2", "/work", True
        )
        sent = agent_directory.SendResult(recipient, True)
        with (
            mock.patch.object(agent_directory, "query_local_agents", return_value=[recipient]),
            mock.patch.object(agent_directory, "_send_local_prompt", return_value=sent) as send,
        ):
            results = agent_directory.dispatch_prompts(
                sender,
                [recipient],
                "Review it",
                config_path=Path("/tmp/ssh-config"),
            )
        self.assertEqual(results, [sent])
        self.assertIn("Message from local/blue-raven", send.call_args.args[1])

    def test_dispatch_separates_local_and_ssh_agents_with_same_host_name(self):
        sender = agent_directory.AgentRecord(
            "shared", "sender", "w:p0", "w", "project", "idle", "s0", "/work", True
        )
        local = agent_directory.AgentRecord(
            "shared", "local", "w:p1", "w", "project", "idle", "s1", "/work", True
        )
        remote = agent_directory.AgentRecord(
            "shared", "remote", "w:p2", "w", "project", "idle", "s2", "/work", False
        )
        local_sent = agent_directory.SendResult(local, True)
        remote_sent = agent_directory.SendResult(remote, True)
        with (
            mock.patch.object(agent_directory, "query_local_agents", return_value=[local]),
            mock.patch.object(
                agent_directory,
                "query_remote_agents",
                return_value=agent_directory.ProbeResult("shared", (remote,), True),
            ),
            mock.patch.object(
                agent_directory,
                "_send_local_prompt",
                return_value=local_sent,
            ) as send_local,
            mock.patch.object(
                agent_directory,
                "_send_remote_prompt",
                return_value=remote_sent,
            ) as send_remote,
        ):
            results = agent_directory.dispatch_prompts(
                sender,
                [local, remote],
                "Review",
                config_path=Path("/tmp/config"),
            )

        self.assertCountEqual(results, [local_sent, remote_sent])
        send_local.assert_called_once()
        send_remote.assert_called_once()

    def test_user_dispatch_uses_only_the_configured_remote_socket(self):
        target = agent_directory.AgentRecord(
            "shared", "worker", "w1:p2", "w1", "work", "idle", "selected-session", "/work", False,
            revision=2, agent_kind="codex", terminal_id="term-selected",
        )
        snapshot = {"result": {"snapshot": {
            "agents": [{
                "agent": "codex", "agent_session": {"value": "selected-session"},
                "agent_status": "idle", "cwd": "/work", "name": "worker",
                "pane_id": "w1:p2", "revision": 2, "terminal_id": "term-selected",
                "workspace_id": "w1",
            }],
            "panes": [{"pane_id": "w1:p2", "terminal_id": "term-selected"}],
        }}}
        results = [
            subprocess.CompletedProcess(["ssh"], 0, json.dumps(snapshot), ""),
            subprocess.CompletedProcess(["ssh"], 0, "", ""),
        ]
        selected_socket = "/var/tmp/ham-selected.sock"
        default_socket = "/var/tmp/default-server.sock"
        environment = {
            "PATH": os.environ.get("PATH", ""),
            "HERDR_SOCKET_PATH": default_socket,
        }
        with mock.patch.object(agent_directory, "_run_command", side_effect=results) as run:
            sent = agent_directory.dispatch_user_prompts(
                [target], "exact user body",
                config_path=Path("/owned/ssh-config"),
                environment=environment,
                remote_sockets={"shared": selected_socket},
            )

        self.assertEqual(len(sent), 1)
        self.assertTrue(sent[0].success)
        self.assertEqual(run.call_count, 2)
        expected_prompt = agent_directory.user_prompt_text("exact user body")
        for call in run.call_args_list:
            command = call.args[0]
            self.assertEqual(command[0], "ssh")
            remote_script = base64.b64decode(shlex.split(command[-1])[-1]).decode("utf-8")
            self.assertIn(f"export HERDR_SOCKET_PATH={shlex.quote(selected_socket)};", remote_script)
            self.assertNotIn(default_socket, remote_script)
        prompt_script = base64.b64decode(
            shlex.split(run.call_args_list[1].args[0][-1])[-1]
        ).decode("utf-8")
        self.assertIn(shlex.quote(expected_prompt), prompt_script)

    def test_bounded_command_uses_explicit_environment(self):
        result = agent_directory.run_bounded_command(
            [sys.executable, "-c", "import os; print(os.environ['HAM_TEST_VALUE'])"],
            environment={"PATH": os.environ.get("PATH", ""), "HAM_TEST_VALUE": "isolated"},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "isolated")
    def test_sessionless_agent_requires_full_occupant_fingerprint(self):
        selected = agent_directory.AgentRecord(
            "local", "", "w:p1", "w", "project", "idle", "", "/work", True,
            revision=2, agent_kind="codex", terminal_id="term-1",
        )
        replacement = agent_directory.replace(selected, revision=3)
        self.assertIsNone(agent_directory._matching_agent(selected, [replacement]))
        self.assertEqual(
            agent_directory._matching_agent(selected, [selected]),
            selected,
        )

    def test_direct_delivery_rejects_duplicate_occupant_matches(self):
        for session_id in ("shared-session", ""):
            selected = agent_directory.AgentRecord(
                "local", "worker", "w:p1", "w", "work", "idle", session_id, "/work", True,
                revision=1, agent_kind="codex", terminal_id="term",
            )
            with self.subTest(session_id=session_id):
                self.assertIsNone(agent_directory._matching_agent(selected, [selected, selected]))

    def test_direct_remote_delivery_preserves_unknown_acceptance(self):
        target = agent_directory.AgentRecord(
            "remote", "worker", "w:p1", "w", "work", "idle", "session", "/work", False,
        )
        with mock.patch.object(agent_directory, "_run_command", return_value=subprocess.CompletedProcess(
            ["ssh"], 255, "", "Connection lost",
        )):
            result = agent_directory._send_remote_prompt(target, "work", config_path=Path("/tmp/config"))
        self.assertFalse(result.success)
        self.assertTrue(result.submission_unknown)

    def test_remote_discovery_creates_at_most_max_workers_threads(self):
        discovery = agent_directory.RemoteDiscovery(
            [f"host-{index}" for index in range(100)],
            config_path=Path("/tmp/config"),
        )
        with mock.patch.object(agent_directory.threading, "Thread") as thread:
            discovery.start()
        self.assertEqual(thread.call_count, agent_directory.MAX_REMOTE_WORKERS)

    def test_bounded_command_can_be_cancelled(self):
        cancelled = threading.Event()
        cancelled.set()
        started_at = time.monotonic()
        result = agent_directory._run_command(
            [sys.executable, "-c", "import time; time.sleep(5)"],
            timeout=5,
            cancel_event=cancelled,
        )
        self.assertLess(time.monotonic() - started_at, 1)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stderr, "cancelled")

    def test_herdr_executable_trims_deleted_suffix_from_recorded_path(self):
        with tempfile.TemporaryDirectory() as directory:
            binary = Path(directory, "herdr")
            binary.write_text("#!/bin/sh\n", encoding="utf-8")
            binary.chmod(0o700)
            environment = {"HERDR_BIN_PATH": f"{os.fspath(binary)} (deleted)"}

            self.assertEqual(
                agent_directory.herdr_executable(environment),
                os.fspath(binary),
            )

    def test_herdr_executable_ignores_a_recorded_path_that_no_longer_exists(self):
        with tempfile.TemporaryDirectory() as directory:
            environment = {"HERDR_BIN_PATH": os.fspath(Path(directory, "uninstalled", "herdr"))}

            self.assertEqual(agent_directory.herdr_executable(environment), "herdr")

    def test_herdr_fallback_paths_keep_the_remote_installation_order(self):
        self.assertEqual(agent_directory.herdr_fallback_paths("/sandbox/home"), (
            "/sandbox/home/.local/bin/herdr",
            "/opt/homebrew/bin/herdr",
            "/usr/local/bin/herdr",
            "/sandbox/home/.local/share/mise/shims/herdr",
        ))

    def test_herdr_executable_and_remote_shell_share_fallback_discovery(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "home with spaces 'and quotes'"
            path = Path(directory) / "path"
            path.mkdir()
            environment = {**os.environ, "HOME": str(home), "PATH": str(path)}
            environment.pop("HERDR_BIN_PATH", None)
            binary = home / ".local/bin/herdr"
            binary.parent.mkdir(parents=True)
            binary.write_text("#!/bin/sh\nprintf '%s\\n' \"$@\"\n", encoding="utf-8")
            binary.chmod(0o700)

            self.assertEqual(agent_directory.herdr_executable(environment), str(binary))
            command = agent_directory._remote_herdr_command(["agent", "list"])
            result = subprocess.run(["/bin/sh", *shlex.split(command)[1:]], env=environment,
                                    capture_output=True, text=True, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "agent\nlist\n")

    def test_herdr_executable_prefers_path_and_ignores_nonexecutable_fallbacks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = root / "home"
            path = root / "path"
            path.mkdir()
            binary = home / ".local/bin/herdr"
            binary.parent.mkdir(parents=True)
            binary.write_text("#!/bin/sh\n", encoding="utf-8")
            alternate = home / ".local/share/mise/shims/herdr"
            alternate.parent.mkdir(parents=True)
            alternate.write_text("#!/bin/sh\n", encoding="utf-8")
            alternate.chmod(0o700)
            environment = {"HOME": str(home), "PATH": str(path)}
            # Isolate system-wide candidates so this assertion is host-independent.
            with mock.patch.object(agent_directory, "herdr_fallback_paths",
                                   return_value=(str(binary), str(alternate))):
                self.assertEqual(agent_directory.herdr_executable(environment), str(alternate))
                on_path = path / "herdr"
                on_path.write_text("#!/bin/sh\n", encoding="utf-8")
                on_path.chmod(0o700)
                self.assertEqual(agent_directory.herdr_executable(environment), "herdr")

    def test_remote_shell_honors_recorded_binary_with_deleted_suffix(self):
        with tempfile.TemporaryDirectory() as directory:
            binary = Path(directory) / "recorded herdr"
            binary.write_text("#!/bin/sh\nprintf '%s\\n' recorded\n", encoding="utf-8")
            binary.chmod(0o700)
            environment = {**os.environ, "HERDR_BIN_PATH": str(binary) + " (deleted)"}
            command = agent_directory._remote_herdr_command(["agent", "list"])
            result = subprocess.run(shlex.split(command), env=environment,
                                    capture_output=True, text=True, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "recorded\n")

    def test_cancelled_command_never_starts_a_process(self):
        cancelled = threading.Event()
        cancelled.set()
        with mock.patch.object(agent_directory.subprocess, "Popen") as launch:
            result = agent_directory.run_bounded_command(
                ["herdr", "agent", "prompt", "worker", "do work"],
                cancel_event=cancelled,
            )
        launch.assert_not_called()
        self.assertEqual(result.stderr, "cancelled")

    def test_cancellation_still_works_after_both_output_pipes_close(self):
        cancelled = threading.Event()
        timer = threading.Timer(0.2, cancelled.set)
        timer.start()
        try:
            result = agent_directory.run_bounded_command(
                [sys.executable, "-c", "import os,time; os.close(1); os.close(2); time.sleep(10)"],
                timeout=2,
                cancel_event=cancelled,
            )
        finally:
            timer.cancel()
            timer.join()
        self.assertEqual(result.stderr, "cancelled")


if __name__ == "__main__":
    unittest.main()
