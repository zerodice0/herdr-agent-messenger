"""Private-ledger regression coverage for bounded peer latency and durable backlogs."""
import tests.ham_test_support as ham_test_support  # Seal live roots before product imports.

import io
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from unittest import mock
from types import SimpleNamespace
import uuid

from ham.ledger import views, worker
from ham import paths
from ham.agents import directory
from ham.ledger.wake_codex import find_codex_binary
from ham.ledger.remote import RemoteLedger, SSHTransport
from ham.ledger.store import LedgerError, LedgerStore, queue_outbox


class NetworkLatencyTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.environment = {**os.environ,
                            "HOME": str(self.root / "home"),
                            "XDG_STATE_HOME": str(self.root / "state"),
                            "HERDR_AGENT_LEDGER_DIR": str(self.root / "ledger"),
                            "HERDR_AGENT_MESSENGER_REQUEST_DIR": str(self.root / "requests"),
                            "HERDR_AGENT_LEDGER_AUTOSTART": "0"}
        self.store = LedgerStore(self.root / "ledger", canonical_name="alpha",
                                 environment=self.environment)
        with self.store.transaction() as connection:
            for name in ("beta", "gamma", "zeta"):
                connection.execute("INSERT INTO peers VALUES (?,?,?,?)",
                                   (name, str(uuid.uuid4()), name, str(self.root)))

    def queue(self, host, *, count=1):
        with self.store.transaction() as connection:
            return [queue_outbox(connection, host, "setting", {"fixture": index})
                    for index in range(count)]

    def test_two_hung_board_peers_share_one_deadline_and_keep_verified_stale_rows(self):
        release = threading.Event()
        started = set()
        lock = threading.Lock()

        class HungTransport:
            def call(inner, peer, command, payload):
                with lock:
                    started.add(peer["canonical_name"])
                release.wait(2)
                raise LedgerError("transport_unavailable", "isolated hung peer")

        peers = RemoteLedger(self.store).peers()[:2]
        previous = {"hosts": [{"host": peer["canonical_name"], "uuid": peer["uuid"]} for peer in peers],
                    "agents": [{"host": "beta", "repo_id": "fixture", "unread": 3,
                                "phase": "ready", "summary": "old"}], "workspaces": []}
        try:
            with mock.patch.object(RemoteLedger, "peers", return_value=peers), \
                    mock.patch.object(RemoteLedger, "status_export", return_value={"agents": []}), \
                    mock.patch.object(views, "PEER_STATUS_TIMEOUT_SECONDS", 0.15):
                before = time.monotonic()
                board = views.status_board(self.store, environment=self.environment,
                                           transport=HungTransport(), previous=previous)
                elapsed = time.monotonic() - before
            self.assertLess(elapsed, 0.28)  # Two serial deadlines would cost at least 0.30 s.
            self.assertGreaterEqual(elapsed, 0.13)
            self.assertEqual(started, {"beta", "gamma"})
            self.assertEqual([host["host"] for host in board["hosts"]], ["alpha", "beta", "gamma"])
            self.assertEqual([host["unread"] for host in board["hosts"]], [0, "?", "?"])
            self.assertTrue(board["agents"][0]["stale"])
            self.assertEqual(board["agents"][0]["unread"], "?")
            self.assertTrue(all(host["error"]["code"] == "transport_unavailable"
                                for host in board["hosts"][1:]))
        finally:
            release.set()

    def test_pump_deadline_keeps_peer_fifo_without_delaying_healthy_peer(self):
        slow = self.queue("beta", count=2)
        healthy = self.queue("gamma")
        release = threading.Event()
        calls = []

        class MixedTransport:
            def call(inner, peer, command, payload):
                calls.append((peer["canonical_name"], payload["fixture"]))
                if peer["canonical_name"] == "beta":
                    release.wait(2)
                return {}

        remote = RemoteLedger(self.store, self.environment, MixedTransport())
        try:
            before = time.monotonic()
            result = remote.pump(budget_seconds=0.15)
            self.assertLess(time.monotonic() - before, 0.28)
            self.assertEqual(result, {"sent": 1, "failed": 0, "deferred": 1})
            items = {item["item_id"]: item for item in remote.outbox()}
            self.assertEqual(items[healthy[0]]["state"], "sent")
            self.assertEqual([items[item]["state"] for item in slow], ["pending", "pending"])
            self.assertEqual([items[item]["attempts"] for item in slow], [1, 0])
            self.assertEqual(sorted(calls), [("beta", 0), ("gamma", 0)])
            self.assertEqual(remote.pump(now=time.time(), budget_seconds=0.1),
                             {"sent": 0, "failed": 0, "deferred": 0})
        finally:
            release.set()

    def test_pump_attempt_limit_is_total_not_per_peer(self):
        self.queue("beta", count=2)
        self.queue("gamma", count=2)
        transport = mock.Mock()
        transport.call.return_value = {}
        remote = RemoteLedger(self.store, self.environment, transport)
        self.assertEqual(remote.pump(limit=2), {"sent": 2, "failed": 0, "deferred": 0})
        self.assertEqual(transport.call.call_count, 2)
        self.assertEqual(sum(item["state"] == "pending" for item in remote.outbox()), 2)

    def test_local_refresh_observation_and_native_wake_precede_slow_pump(self):
        events = []

        class SlowPump:
            def pump(inner):
                self.assertEqual(events, ["refresh", "observe", "wake"])
                events.append("pump")
                time.sleep(0.03)
                return {"sent": 0, "failed": 0, "deferred": 0}

        with mock.patch.object(worker, "maintenance_pending", return_value=True), \
                mock.patch.object(worker.AgentRegistry, "refresh", side_effect=lambda env: events.append("refresh")), \
                mock.patch("ham.ledger.runtime.NativeReceiver.observe_claude", side_effect=lambda: events.append("observe")), \
                mock.patch("ham.ledger.native_wake.NativeWaker.sweep", side_effect=lambda: events.append("wake")), \
                mock.patch("sys.stderr", new_callable=io.StringIO):
            result = worker.run_worker(self.store, self.environment, once=True, remote=SlowPump())
        self.assertEqual(events, ["refresh", "observe", "wake", "pump"])
        self.assertEqual(result["reason"], "one_pass")

    def test_pending_backlog_is_optional_on_board_and_doctor_and_survives_retention(self):
        items = self.queue("beta", count=2)
        self.queue("gamma")
        now = time.time()
        with self.store.transaction() as connection:
            connection.execute("UPDATE ledger_meta SET value=? WHERE key=?",
                               (str(now - 900), "outbox_enqueued:" + items[0]))
            connection.execute("UPDATE outbox SET state='sent' WHERE target_host='gamma'")
        self.store.retention(now=now + 365 * 86400)
        pending = RemoteLedger(self.store).pending_backlog(now=now)
        self.assertEqual(pending, {"beta": {"count": 2, "oldest_age_s": 900}})
        self.assertEqual(self.store.doctor()["outbox_pending"]["beta"]["count"], 2)
        transport = mock.Mock()
        transport.call.return_value = {"protocol": 1, "host": {"canonical_name": "beta", "uuid":
            next(peer["uuid"] for peer in RemoteLedger(self.store).peers() if peer["canonical_name"] == "beta")},
            "agents": [], "workspaces": []}
        board = views.status_board(self.store, host="beta", environment=self.environment, transport=transport)
        self.assertEqual(board["hosts"][0]["outbox_pending"]["count"], 2)
        with self.store.transaction() as connection:
            connection.execute("UPDATE outbox SET state='cancelled'")
        self.assertNotIn("outbox_pending", self.store.doctor())
        self.assertEqual(RemoteLedger(self.store).pending_backlog(), {})

    def test_retry_preserves_original_item_and_enqueue_age(self):
        item = self.queue("beta")[0]
        with self.store.transaction() as connection:
            connection.execute("UPDATE ledger_meta SET value='100' WHERE key=?", ("outbox_enqueued:" + item,))
            connection.execute("UPDATE outbox SET state='failed',next_try_at=999,attempts=5 WHERE item_id=?", (item,))
        remote = RemoteLedger(self.store)
        self.assertEqual(remote.retry(item), {"item_id": item, "state": "pending"})
        self.assertEqual(remote.pending_backlog(now=200), {"beta": {"count": 1, "oldest_age_s": 100}})
        self.assertEqual(remote.outbox()[0]["attempts"], 5)

    def test_worker_environment_pins_tools_and_ignores_relative_and_writable_path(self):
        home = self.root / "tool-home"
        installed = home / ".local/bin"
        installed.mkdir(parents=True)
        unsafe = self.root / "unsafe"
        unsafe.mkdir(mode=0o777)
        unsafe.chmod(0o777)
        for name in ("herdr", "codex"):
            binary = installed / name
            binary.write_text("#!/bin/sh\nexit 0\n")
            binary.chmod(0o700)
        environment = worker.worker_environment({"HOME": str(home), "PATH": f".:{unsafe}",
                                                 "HERDR_BIN_PATH": "relative-herdr", "CODEX_BIN": "relative-codex"})
        self.assertEqual(environment["HERDR_BIN_PATH"], str(installed / "herdr"))
        self.assertEqual(environment["CODEX_BIN"], str(installed / "codex"))
        self.assertNotIn(".", environment["PATH"].split(os.pathsep))
        self.assertNotIn(str(unsafe), environment["PATH"].split(os.pathsep))
        self.assertTrue(Path(shutil.which("ssh", path=environment["PATH"])).is_absolute())

    def test_ssh_transport_executes_resolved_absolute_binary_and_forwards_deadline_environment(self):
        transport = SSHTransport(self.store, self.environment)
        with mock.patch("ham.ledger.remote.subprocess.run", return_value=subprocess.CompletedProcess([], 0, '{}', '')) as run:
            self.assertEqual(transport._run(["ssh", "fixture"], timeout=0.25), {})
        self.assertTrue(Path(run.call_args.args[0][0]).is_absolute())
        self.assertEqual(run.call_args.kwargs["timeout"], 0.25)
        self.assertEqual(run.call_args.kwargs["env"], self.environment)

    def test_mac_admin_writable_tool_prefix_remains_trusted(self):
        prefix = self.root / "homebrew-bin"
        prefix.mkdir()
        prefix.chmod(0o775)
        binary = prefix / "herdr"
        binary.write_text("#!/bin/sh\nexit 0\n")
        binary.chmod(0o755)
        environment = {"HOME": str(self.root / "home"), "PATH": str(prefix)}
        admin = mock.Mock(gr_gid=os.getgid())
        with mock.patch.object(paths.sys, "platform", "darwin"), \
                mock.patch.object(paths.grp, "getgrnam", return_value=admin):
            self.assertEqual(worker.worker_environment(environment)["HERDR_BIN_PATH"], str(binary))
        with mock.patch.object(paths.sys, "platform", "linux"), \
                mock.patch.object(paths.grp, "getgrgid", return_value=
                                  SimpleNamespace(gr_name="shared", gr_mem=["other"])):
            self.assertNotIn(str(prefix), worker.worker_environment(environment)["PATH"].split(os.pathsep))

    def test_user_private_group_writable_prefix_preserves_path_and_pinned_tools(self):
        prefix = self.root / "linuxbrew/bin"
        prefix.mkdir(parents=True)
        prefix.parent.chmod(0o775)
        prefix.chmod(0o775)
        for name in ("herdr", "codex"):
            binary = prefix / name
            binary.write_text("#!/bin/sh\nexit 0\n")
            binary.chmod(0o775)
        user = SimpleNamespace(pw_name="private-user", pw_uid=os.getuid(), pw_gid=os.getgid())
        group = SimpleNamespace(gr_name=user.pw_name, gr_mem=[])
        environment = {"HOME": str(self.root / "home"), "PATH": str(prefix),
                       "HERDR_BIN_PATH": str(prefix / "herdr"), "CODEX_BIN": str(prefix / "codex")}
        with mock.patch.object(paths.sys, "platform", "linux"), \
                mock.patch.object(paths.grp, "getgrgid", return_value=group), \
                mock.patch.object(paths.pwd, "getpwuid", return_value=user), \
                mock.patch.object(paths.pwd, "getpwall", return_value=[user]):
            pinned = worker.worker_environment(environment)
        self.assertIn(str(prefix), pinned["PATH"].split(os.pathsep))
        self.assertEqual(pinned["HERDR_BIN_PATH"], str(prefix / "herdr"))
        self.assertEqual(pinned["CODEX_BIN"], str(prefix / "codex"))

    def test_shared_group_writable_prefix_rejected_for_all_shared_group_forms(self):
        prefix = self.root / "shared-bin"
        prefix.mkdir()
        prefix.chmod(0o775)
        binary = prefix / "herdr"
        binary.write_text("#!/bin/sh\nexit 0\n")
        binary.chmod(0o755)
        user = SimpleNamespace(pw_name="private-user", pw_uid=os.getuid(), pw_gid=os.getgid())
        another = SimpleNamespace(pw_name="other", pw_uid=os.getuid() + 1, pw_gid=os.getgid())
        for group, users in (
                (SimpleNamespace(gr_name="shared", gr_mem=[]), [user]),
                (SimpleNamespace(gr_name=user.pw_name, gr_mem=["other"]), [user]),
                (SimpleNamespace(gr_name=user.pw_name, gr_mem=[]), [user, another])):
            with self.subTest(group=group, users=users), \
                    mock.patch.object(paths.sys, "platform", "linux"), \
                    mock.patch.object(paths.grp, "getgrgid", return_value=group), \
                    mock.patch.object(paths.pwd, "getpwuid", return_value=user), \
                    mock.patch.object(paths.pwd, "getpwall", return_value=users):
                pinned = worker.worker_environment({"HOME": str(self.root / "home"), "PATH": str(prefix),
                                                    "HERDR_BIN_PATH": str(binary)})
                self.assertNotIn(str(prefix), pinned["PATH"].split(os.pathsep))
                self.assertNotEqual(pinned.get("HERDR_BIN_PATH"), str(binary))

    def test_installation_fallbacks_share_worker_private_group_trust(self):
        home = self.root / "fallback-home"
        prefix = home / ".local/bin"
        prefix.mkdir(parents=True)
        prefix.chmod(0o775)
        for name in ("herdr", "codex"):
            binary = prefix / name
            binary.write_text("#!/bin/sh\nexit 0\n")
            binary.chmod(0o755)
        user = SimpleNamespace(pw_name="private-user", pw_uid=os.getuid(), pw_gid=os.getgid())
        environment = {"HOME": str(home), "PATH": str(self.root / "absent")}
        with mock.patch.object(paths.sys, "platform", "linux"), \
                mock.patch.object(paths.pwd, "getpwuid", return_value=user), \
                mock.patch.object(paths.pwd, "getpwall", return_value=[user]), \
                mock.patch("ham.ledger.wake_codex._BINARY_DIRECTORIES", ("~/.local/bin",)), \
                mock.patch.object(directory, "herdr_fallback_paths", return_value=(str(prefix / "herdr"),)):
            with mock.patch.object(paths.grp, "getgrgid", return_value=
                                   SimpleNamespace(gr_name=user.pw_name, gr_mem=[])):
                self.assertEqual(directory.herdr_executable(environment), str(prefix / "herdr"))
                self.assertEqual(find_codex_binary(environment), str(prefix / "codex"))
            with mock.patch.object(paths.grp, "getgrgid", return_value=
                                   SimpleNamespace(gr_name=user.pw_name, gr_mem=["other"])):
                self.assertEqual(directory.herdr_executable(environment), "herdr")
                self.assertIsNone(find_codex_binary(environment))


if __name__ == "__main__":
    unittest.main()
