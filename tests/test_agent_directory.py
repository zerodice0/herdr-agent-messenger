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


_LIFECYCLE_OBSERVER = r'''
from contextlib import ExitStack
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
from unittest import mock
import tests.ham_test_support
from ham.agents import directory

scenario, failure, error_mode = sys.argv[1:]
real_popen = subprocess.Popen
children = []
wait_timeouts = []
timers = []
injected_error = OSError("fixture selector failure")
observed_errors = []
real_communicate = directory._communicate_bounded

def communicate(*args, **kwargs):
    try:
        return real_communicate(*args, **kwargs)
    except BaseException as error:
        observed_errors.append(error)
        raise

def interrupted(*_):
    raise RuntimeError("external observer deadline")

signal.signal(signal.SIGTERM, interrupted)

class DelayedExit:
    def __init__(self, *args, **kwargs):
        self.child = real_popen(*args, **kwargs)
        children.append(self.child)
    def __getattr__(self, name):
        return getattr(self.child, name)
    def kill(self):
        pass
    def wait(self, timeout=None):
        wait_timeouts.append(timeout)
        assert timeout is not None, "unbounded cleanup wait"
        return self.child.wait(timeout=timeout)

def launch(*args, **kwargs):
    child = real_popen(*args, **kwargs)
    children.append(child)
    return child

def assert_reaped():
    for child in children:
        assert child.returncode is not None, child.pid
        try:
            os.waitpid(child.pid, os.WNOHANG)
        except ChildProcessError:
            pass
        else:
            raise AssertionError("child was not reaped: " + str(child.pid))
        assert child.stdout is None or child.stdout.closed
        assert child.stderr is None or child.stderr.closed

report = {}
try:
    if scenario == "stalled":
        cancelled = threading.Event()
        if failure == "cancel":
            timer = threading.Timer(0.04, cancelled.set)
            timers.append(timer)
            timer.start()
        source = "import time; time.sleep(30)"
        if failure == "cap":
            source = "import os,time; os.write(1,b'x'*(3*1024*1024)); time.sleep(30)"
        if failure == "closed":
            source = "import os,time; os.close(1); os.close(2); time.sleep(30)"
        command = [sys.executable, "-c", source]
        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(directory.subprocess, "Popen", DelayedExit))
            stack.enter_context(mock.patch.object(directory, "_communicate_bounded", communicate))
            if failure == "oserror":
                stack.enter_context(mock.patch.object(directory.selectors, "DefaultSelector",
                                                      side_effect=injected_error))
            started = time.monotonic()
            try:
                result = directory.run_bounded_command(
                    command, timeout=0.3, cancel_event=cancelled,
                    raise_errors=error_mode == "raise",
                )
            except (OSError, subprocess.SubprocessError) as error:
                assert error_mode == "raise", repr(error)
                assert error is observed_errors[0]
                expected = {
                    "timeout": subprocess.TimeoutExpired,
                    "closed": subprocess.TimeoutExpired,
                    "cancel": directory.CommandCancelled,
                    "cap": directory.OutputLimitExceeded,
                    "oserror": OSError,
                }[failure]
                assert isinstance(error, expected), repr(error)
                if failure == "oserror":
                    assert error is injected_error
                if failure in ("timeout", "closed"):
                    assert error.cmd == command and error.timeout == 0.3
                report["error"] = type(error).__name__
            else:
                assert error_mode == "completed", repr(result)
                assert result.returncode == 1 and result.stdout == ""
                expected = {"cancel": "cancelled", "cap": "output_limit_exceeded",
                            "oserror": str(injected_error)}.get(failure)
                if expected is not None:
                    assert result.stderr == expected, result
                else:
                    assert "timed out" in result.stderr, result
                report["error"] = result.stderr
            report["elapsed"] = time.monotonic() - started
            assert report["elapsed"] < 0.5, report
            assert wait_timeouts and all(0 < value <= 0.3 for value in wait_timeouts)
            assert len(children) == 1 and children[0].poll() is None
            for _ in range(5):
                result = directory.run_bounded_command(command, timeout=0.3)
                assert result.returncode == 1 and "has not exited" in result.stderr, result
            assert len(children) == 1, "unreaped executable spawned again"
        # Let the fixture child really exit. Its zombie must be reclaimed even
        # when no further command runs in this Python process.
        children[0].kill()
        until = time.monotonic() + 2
        while children[0].returncode is None and time.monotonic() < until:
            time.sleep(0.01)
        assert_reaped()
        result = directory.run_bounded_command([sys.executable, "-c", "print('recovered')"])
        assert result.returncode == 0 and result.stdout == "recovered\n", result
        report["spawn_count_while_unreaped"] = len(children)
    elif scenario == "discovery":
        discovery = directory.RemoteDiscovery(["fixture"], config_path=Path("unused"), timeout=0.3)
        discovery.deadline = time.monotonic() + discovery.timeout
        source = "import time; time.sleep(30)"
        if failure == "cap":
            source = "import os,time; os.write(1,b'x'*(3*1024*1024)); time.sleep(30)"
        if failure == "cancel":
            timer = threading.Timer(0.04, discovery._cancelled.set)
            timers.append(timer)
            timer.start()
        with mock.patch.object(directory.subprocess, "Popen", DelayedExit), mock.patch.object(
            directory, "ssh_command", return_value=[sys.executable, "-c", source],
        ):
            started = time.monotonic()
            discovery._probe_host("fixture")
            assert time.monotonic() - started < 0.5
            result, = discovery.poll()
            assert not result.success
            assert result.error == {"timeout": "timeout", "cancel": "cancelled",
                                    "cap": "output_limit_exceeded"}[failure], result
            assert not discovery._processes
            assert wait_timeouts and all(value is not None for value in wait_timeouts)
            assert len(children) == 1
    elif scenario == "resources":
        fd_directory = Path("/proc/self/fd") if sys.platform == "linux" else Path("/dev/fd")
        before_fds = len(list(fd_directory.iterdir()))
        with mock.patch.object(directory.subprocess, "Popen", launch):
            for _ in range(8):
                normal = directory.run_bounded_command(
                    [sys.executable, "-c", "import sys; print('out'); print('err',file=sys.stderr)"],
                    timeout=1, raise_errors=True,
                )
                assert normal.returncode == 0 and normal.stdout == "out\n" and normal.stderr == "err\n"
                for closed in (False, True):
                    source = "import time; time.sleep(30)"
                    if closed:
                        source = "import os,time; os.close(1); os.close(2); time.sleep(30)"
                    result = directory.run_bounded_command(
                        [sys.executable, "-c", source], timeout=0.15,
                    )
                    assert result.returncode == 1 and "timed out" in result.stderr
                result = directory.run_bounded_command(
                    [sys.executable, "-c", "import os,time; os.write(2,b'x'*(3*1024*1024)); time.sleep(30)"],
                    timeout=1,
                )
                assert result.stderr == "output_limit_exceeded", result
                cancelled = threading.Event()
                timer = threading.Timer(0.04, cancelled.set)
                timers.append(timer)
                timer.start()
                result = directory.run_bounded_command(
                    [sys.executable, "-c", "import time; time.sleep(30)"],
                    timeout=1, cancel_event=cancelled,
                )
                timer.join()
                assert result.stderr == "cancelled", result
                assert_reaped()
                assert len(list(fd_directory.iterdir())) <= before_fds, "file descriptors leaked"
        report["reaped_children"] = len(children)
        report["fd_growth"] = len(list(fd_directory.iterdir())) - before_fds
    elif scenario == "descendant":
        # The command closes its own outputs while its real descendant retains
        # both pipes. Keep their parent alive to reap it on every platform.
        release_read, release_write = os.pipe()
        descendant_source = (
            "import os; os.read(" + str(release_read) + ",1); "
            "os.write(1,b'out\\n'); os.write(2,b'err\\n')"
        )
        source = (
            "import os,subprocess,sys; "
            "child=subprocess.Popen([sys.executable,'-c'," + repr(descendant_source) + "],"
            "pass_fds=(" + str(release_read) + ",)); "
            "os.close(1); os.close(2); child.wait()"
        )
        # Delay kill only in this fixture so its parent can reap the descendant
        # after we release it, even when the captured pipes time out.
        direct = DelayedExit([sys.executable, "-c", source], stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, pass_fds=(release_read,))
        os.close(release_read)
        try:
            if failure == "release":
                timer = threading.Timer(0.05, lambda: os.write(release_write, b"x"))
                timers.append(timer)
                timer.start()
            started = time.monotonic()
            try:
                stdout, stderr = directory._communicate_bounded(direct, timeout=0.3)
            except subprocess.TimeoutExpired:
                assert failure == "timeout"
            else:
                assert failure == "release"
                assert stdout == "out\n" and stderr == "err\n"
            assert time.monotonic() - started < 0.8
        finally:
            for timer in timers:
                timer.join()
            if failure != "release":
                os.write(release_write, b"x")
            os.close(release_write)
            direct.child.wait(timeout=2)
        assert_reaped()
    elif scenario == "exited_parent":
        # Linux's subreaper lets the observer own/reap the real orphan rather
        # than leaving a fixture grandchild or zombie to the host's PID 1.
        import ctypes
        assert ctypes.CDLL(None, use_errno=True).prctl(36, 1, 0, 0, 0) == 0
        release_read, release_write = os.pipe()
        pid_read, pid_write = os.pipe()
        descendant_pid = None
        try:
            descendant_source = (
                "import os; os.read(" + str(release_read) + ",1); "
                "os.write(1,b'out\\n'); os.write(2,b'err\\n')"
            )
            source = (
                "import os,subprocess,sys; "
                "child=subprocess.Popen([sys.executable,'-c'," + repr(descendant_source) + "],"
                "pass_fds=(" + str(release_read) + ",)); "
                "os.write(" + str(pid_write) + ",str(child.pid).encode()+b'\\n')"
            )
            direct = real_popen([sys.executable, "-c", source], stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, pass_fds=(release_read, pid_write))
            children.append(direct)
            descendant_pid = int(os.read(pid_read, 64))
            direct.wait(timeout=2)
            if failure == "release":
                timer = threading.Timer(0.05, lambda: os.write(release_write, b"x"))
                timers.append(timer)
                timer.start()
            started = time.monotonic()
            try:
                stdout, stderr = directory._communicate_bounded(direct, timeout=0.3)
            except subprocess.TimeoutExpired:
                assert failure == "timeout"
            else:
                assert failure == "release"
                assert stdout == "out\n" and stderr == "err\n"
            assert time.monotonic() - started < 0.5
            assert_reaped()
        finally:
            for timer in timers:
                timer.cancel()
                timer.join()
            if descendant_pid is not None:
                try:
                    os.kill(descendant_pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                until = time.monotonic() + 2
                while os.waitpid(descendant_pid, os.WNOHANG)[0] == 0:
                    assert time.monotonic() < until, "fixture descendant did not exit"
                    time.sleep(0.01)
                try:
                    os.kill(descendant_pid, 0)
                except ProcessLookupError:
                    pass
                else:
                    raise AssertionError("fixture descendant remains")
            for fd in (release_read, release_write, pid_read, pid_write):
                os.close(fd)
    print(json.dumps(report), flush=True)
finally:
    for timer in timers:
        timer.cancel()
        timer.join()
    for child in children:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=2)
        for stream in (child.stdout, child.stderr):
            if stream is not None:
                stream.close()
        try:
            os.kill(child.pid, 0)
        except ProcessLookupError:
            pass
        else:
            raise AssertionError("fixture child remains: " + str(child.pid))
'''


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

    def _observe_lifecycle(self, scenario, failure="", error_mode=""):
        program = subprocess.Popen(
            [sys.executable, "-c", _LIFECYCLE_OBSERVER, scenario, failure, error_mode],
            cwd=Path(__file__).resolve().parents[1],
            env=dict(os.environ), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True,
        )
        try:
            try:
                stdout, stderr = program.communicate(timeout=15)
            except subprocess.TimeoutExpired:
                # Interrupt only our observer, whose finally block owns and
                # reaps all fixture children. Never signal unrelated processes.
                program.terminate()
                stdout, stderr = program.communicate(timeout=4)
                self.fail(f"observer exceeded deadline:\n{stdout}\n{stderr}")
            self.assertEqual(program.returncode, 0, f"{stdout}\n{stderr}")
            return json.loads(stdout)
        finally:
            if program.poll() is None:
                program.terminate()
                try:
                    program.wait(timeout=4)
                except subprocess.TimeoutExpired:
                    program.kill()
                    program.wait(timeout=2)
            for stream in (program.stdout, program.stderr):
                stream.close()
            with self.assertRaises(ProcessLookupError):
                os.kill(program.pid, 0)

    def test_delayed_exit_cleanup_is_bounded_and_fences_repeated_executable(self):
        for failure in ("timeout", "closed", "cancel", "cap", "oserror"):
            for error_mode in ("raise", "completed"):
                with self.subTest(failure=failure, error_mode=error_mode):
                    report = self._observe_lifecycle("stalled", failure, error_mode)
                    self.assertEqual(report["spawn_count_while_unreaped"], 1)

    def test_repeated_commands_reap_children_without_fd_growth(self):
        report = self._observe_lifecycle("resources")
        self.assertEqual(report["reaped_children"], 40)
        self.assertLessEqual(report["fd_growth"], 0)

    def test_descendant_inherited_output_pipes_obey_the_same_deadline(self):
        for failure in ("release", "timeout"):
            with self.subTest(failure=failure):
                self._observe_lifecycle("descendant", failure)

    @unittest.skipUnless(sys.platform == "linux", "requires Linux child-subreaper ownership")
    def test_exited_parent_does_not_bypass_inherited_pipe_deadline(self):
        for failure in ("release", "timeout"):
            with self.subTest(failure=failure):
                self._observe_lifecycle("exited_parent", failure)

    def test_remote_discovery_uses_bounded_cleanup_for_each_failure(self):
        for failure in ("timeout", "cancel", "cap"):
            with self.subTest(failure=failure):
                self._observe_lifecycle("discovery", failure)

    def test_raise_errors_preserves_spawn_error_and_precancelled_failure(self):
        error = OSError("cannot start fixture")
        with mock.patch.object(agent_directory.subprocess, "Popen", side_effect=error):
            with self.assertRaises(OSError) as raised:
                agent_directory.run_bounded_command(["not-installed"], raise_errors=True)
            self.assertIs(raised.exception, error)
            result = agent_directory.run_bounded_command(["not-installed"])
            self.assertEqual(result.returncode, 1)
            self.assertEqual(result.stderr, str(error))
        cancelled = threading.Event()
        cancelled.set()
        with mock.patch.object(agent_directory.subprocess, "Popen") as launch:
            with self.assertRaises(agent_directory.CommandCancelled):
                agent_directory.run_bounded_command(
                    ["not-started"], cancel_event=cancelled, raise_errors=True,
                )
            launch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
