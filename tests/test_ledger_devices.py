"""Device ownership, refusal boundaries and read-only resource exports."""
import tests.ham_test_support as ham_test_support  # Protect the live ledger before product imports.
import contextlib
import copy
from concurrent.futures import ThreadPoolExecutor
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import threading
import unittest
from unittest import mock

from ham import cli as agent_skill_cli
from ham.ledger.devices import DeviceClaims, device_rows, device_operation
from ham.ledger.status import AgentRegistry
from ham.ledger.store import LedgerError, LedgerStore, SCHEMA_VERSION
from ham.ledger.views import status_board


class DeviceClaimsTest(unittest.TestCase):
    def setUp(self):
        environment_patch = mock.patch.dict(os.environ)
        environment_patch.start()
        self.addCleanup(environment_patch.stop)
        os.environ.pop("HERDR_AGENT_EMU_LIMIT", None)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.environment = {**os.environ, "HERDR_AGENT_LEDGER_DIR": str(self.root / "ledger"),
                            "HERDR_AGENT_LEDGER_AUTOSTART": "0", "HERDR_TERMINAL_ID": "a"}
        self.store = LedgerStore(environment=self.environment, canonical_name="alpha")
        self.registry = AgentRegistry(self.store)
        self.snapshot = {"panes": [], "agents": []}
        for terminal in ("a", "b"):
            pane = {"terminal_id": terminal, "pane_id": "p-" + terminal, "workspace_id": "w"}
            self.snapshot["panes"].append(pane)
            self.snapshot["agents"].append({**pane, "agent": "omp", "name": "agent-" + terminal,
                "agent_session": {"value": "session-" + terminal}, "agent_status": "idle", "cwd": str(self.root)})
        self.registry.reconcile(self.snapshot, refresh_display=False)
        self.project = mock.patch.object(AgentRegistry, "_refresh_display", return_value={"updated": [], "failed": []}).start()
        self.addCleanup(mock.patch.stopall)
        for target, value in (("process_ancestors", {10}), ("pane_shell_pid", 10)):
            patch = mock.patch("ham.ledger.status." + target, return_value=value)
            patch.start()
            self.addCleanup(patch.stop)
        self.runner = mock.Mock(return_value=subprocess.CompletedProcess([], 0, "List of devices attached\nserial device\noffline offline\n", ""))
        self.claims = DeviceClaims(self.store, registry=self.registry, runner=self.runner)
        original_run = DeviceClaims._run

        def run_probe(claims, command, *, timeout=2):
            return original_run(self.claims, command, timeout=timeout)

        probe_patch = mock.patch.object(DeviceClaims, "_run", autospec=True, side_effect=run_probe)
        probe_patch.start()
        self.addCleanup(probe_patch.stop)

    def agent(self, terminal="a"):
        return next(row for row in self.registry.list() if row["terminal_id"] == terminal)

    def claim(self, terminal="a", device="serial", kind="phone", **kwargs):
        return self.claims.claim(self.agent(terminal), kind, device, label=kwargs.pop("label", "Pixel"), **kwargs)

    def args(self, *args):
        return agent_skill_cli.parse_cli_arguments(["device", *args])

    def emu_claim(self, terminal="a", device="5554", **kwargs):
        kwargs.setdefault("environment", self.environment)
        return self.claim(terminal, device=device, kind="emu", **kwargs)

    def seed_emulators(self, count=3):
        return [
            self.emu_claim("b", str(5554 + index * 2), label=f"Android {index}",
                           force=True, now=10 + index)["device"]
            for index in range(count)
        ]

    def test_emu_under_limit_probes_once_before_transaction(self):
        with mock.patch.object(self.store, "transaction", wraps=self.store.transaction) as transaction:
            def probe(command, **kwargs):
                transaction.assert_not_called()
                self.assertEqual(command, ["ps", "-axww", "-o", "pid=", "-o", "ucomm=", "-o", "args="])
                self.assertEqual(kwargs, {
                    "capture_output": True, "text": True, "timeout": 2, "check": False,
                })
                return subprocess.CompletedProcess(command, 0, "", "")

            self.runner.side_effect = probe
            result = self.emu_claim()
        self.assertEqual(result["device"]["device_id"], "emulator-5554")
        self.runner.assert_called_once()

    def test_emu_default_limit_refuses_with_all_host_holders(self):
        seeded = self.seed_emulators()
        self.runner.reset_mock()
        self.runner.return_value = subprocess.CompletedProcess([], 0, "", "")
        with self.assertRaises(LedgerError) as raised:
            self.emu_claim(device="5560")
        self.assertEqual(raised.exception.code, "emu_limit")
        self.assertEqual(raised.exception.details, {
            "limit": 3,
            "claim_count": 3,
            "process_count": 0,
            "holders": [
                {"alias": "agent-b", "pane": "p-b", "device": row["device_id"],
                 "label": row["label"], "claimed_at": row["claimed_at"]}
                for row in seeded
            ],
            "untracked_processes": [],
        })
        self.runner.assert_called_once()
        with self.store.read() as connection:
            self.assertEqual([row["device_id"] for row in device_rows(connection)],
                             [row["device_id"] for row in seeded])

    def test_emu_untracked_android_processes_refuse_with_pid_and_port(self):
        self.runner.return_value = subprocess.CompletedProcess([], 0, (
            "  101 qemu-system-x86_64 /home/test/Android/Sdk/emulator/qemu/linux-x86_64/qemu-system-x86_64 -port 5554\n"
            "  102 qemu-system-aarch64 /Users/test/Library/Android/sdk/emulator/qemu/darwin-aarch64/qemu-system-aarch64 -ports=5556,5557\n"
            "  103 qemu-system-x86_64 qemu-system-x86_64 -avd Pixel_9\n"
        ), "")
        with self.assertRaises(LedgerError) as raised:
            self.emu_claim(device="5560")
        self.assertEqual(raised.exception.code, "emu_limit")
        self.assertEqual(raised.exception.details, {
            "limit": 3, "claim_count": 0, "process_count": 3, "holders": [],
            "untracked_processes": [
                {"pid": 101, "port": 5554}, {"pid": 102, "port": 5556},
                {"pid": 103, "port": None},
            ],
        })

    def test_emu_own_reclaim_over_limit_preserves_claim_time(self):
        first = self.seed_emulators(4)[0]
        self.runner.return_value = subprocess.CompletedProcess([], 0, "\n".join(
            f"{100 + index} qemu-system-x86_64 qemu-system-x86_64 -port {5554 + index * 2}"
            for index in range(5)
        ), "")
        result = self.emu_claim("b", label="Renamed Android", now=99)
        self.assertIsNone(result["replaced"])
        self.assertEqual(result["device"]["claimed_at"], first["claimed_at"])
        self.assertEqual(result["device"]["label"], "Renamed Android")

    def test_emu_target_process_excluded_for_separate_and_equals_port_flags(self):
        for flags in ("-port 5554", "-port=5554", "-ports 5554,5555", "-ports=5554,5555"):
            with self.subTest(flags=flags):
                self.runner.reset_mock()
                self.runner.return_value = subprocess.CompletedProcess([], 0, (
                    f"101 qemu-system-x86_64 qemu-system-x86_64 {flags}\n"
                    "102 qemu-system-x86_64 qemu-system-x86_64 -port 5556\n"
                    "103 qemu-system-x86_64 qemu-system-x86_64 -ports 5558,5559\n"
                ), "")
                result = self.emu_claim(device="emulator-5554")
                self.assertEqual(result["device"]["device_id"], "emulator-5554")
                self.runner.assert_called_once()
                self.claims.release(self.agent(), "emu", "5554")

    def test_emu_claims_and_processes_use_max_not_sum_and_report_only_untracked(self):
        self.seed_emulators(2)
        self.runner.return_value = subprocess.CompletedProcess([], 0, (
            "101 qemu-system-x86_64 qemu-system-x86_64 -port 5554\n"
            "102 qemu-system-x86_64 qemu-system-x86_64 -port 5556\n"
        ), "")
        self.emu_claim(device="5558")
        self.runner.return_value = subprocess.CompletedProcess([], 0, (
            "101 qemu-system-x86_64 qemu-system-x86_64 -port 5554\n"
            "102 qemu-system-x86_64 qemu-system-x86_64 -port 5556\n"
            "103 qemu-system-x86_64 qemu-system-x86_64 -port 5562\n"
        ), "")
        with self.assertRaises(LedgerError) as raised:
            self.emu_claim(device="5560")
        self.assertEqual(raised.exception.code, "emu_limit")
        self.assertEqual(raised.exception.details["claim_count"], 3)
        self.assertEqual(raised.exception.details["process_count"], 3)
        self.assertEqual(raised.exception.details["untracked_processes"], [{"pid": 103, "port": 5562}])

    def test_emu_force_bypasses_limit_and_process_probe(self):
        self.seed_emulators()
        self.runner.reset_mock()
        self.runner.side_effect = AssertionError("force must not probe processes")
        result = self.emu_claim(device="5560", force=True)
        self.assertEqual(result["device"]["device_id"], "emulator-5560")
        replacement = self.emu_claim(device="5554", force=True)
        self.assertEqual(replacement["replaced"]["holder"]["alias"], "agent-b")
        self.runner.assert_not_called()

    def test_phone_claims_ignore_emu_limit_and_never_probe_processes(self):
        self.seed_emulators()
        self.runner.reset_mock()
        self.runner.side_effect = AssertionError("explicit phone label must not probe")
        environment = {**self.environment, "HERDR_AGENT_EMU_LIMIT": "invalid"}
        for index in range(5):
            result = self.claim(device=f"phone-{index}", environment=environment)
            self.assertEqual(result["device"]["kind"], "phone")
        self.runner.assert_not_called()
        self.runner.side_effect = None
        self.runner.return_value = subprocess.CompletedProcess([], 0, "Pixel 9\n", "")
        result = self.claims.claim(self.agent(), "phone", "auto-label", environment=environment)
        self.assertEqual(result["device"]["label"], "Pixel 9")
        self.runner.assert_called_once_with(
            ["adb", "-s", "auto-label", "shell", "getprop", "ro.product.model"],
            capture_output=True, text=True, timeout=10, check=False,
        )

    def test_emu_custom_environment_limit_overrides_ambient_value(self):
        with mock.patch.dict(os.environ, {"HERDR_AGENT_EMU_LIMIT": "99"}):
            environment = {**self.environment, "HERDR_AGENT_EMU_LIMIT": "1"}
            self.emu_claim(environment=environment)
            with self.assertRaises(LedgerError) as raised:
                self.emu_claim(device="5556", environment=environment)
        self.assertEqual(raised.exception.code, "emu_limit")
        self.assertEqual(raised.exception.details["limit"], 1)

    def test_emu_absent_environment_uses_process_environment_limit(self):
        with mock.patch.dict(os.environ, {"HERDR_AGENT_EMU_LIMIT": "1"}):
            self.emu_claim(environment=None)
            with self.assertRaises(LedgerError) as raised:
                self.emu_claim(device="5556", environment=None)
        self.assertEqual(raised.exception.code, "emu_limit")
        self.assertEqual(raised.exception.details["limit"], 1)

    def test_emu_nonpositive_environment_limits_are_unlimited_and_skip_probe(self):
        self.seed_emulators(4)
        self.runner.reset_mock()
        self.runner.side_effect = AssertionError("unlimited must not probe processes")
        for index, limit in enumerate(("0", "-1", "-20")):
            with self.subTest(limit=limit):
                environment = {**self.environment, "HERDR_AGENT_EMU_LIMIT": limit}
                result = self.emu_claim(device=str(5562 + index * 2), environment=environment)
                self.assertEqual(result["device"]["kind"], "emu")
        self.runner.assert_not_called()

    def test_emu_invalid_environment_limits_refuse_without_probe_or_mutation(self):
        self.runner.side_effect = AssertionError("invalid config must not probe")
        for limit in ("invalid", "1.5", ""):
            with self.subTest(limit=limit):
                with self.assertRaises(LedgerError) as raised:
                    self.emu_claim(environment={**self.environment, "HERDR_AGENT_EMU_LIMIT": limit})
                self.assertEqual(raised.exception.code, "invalid_config")
        self.runner.assert_not_called()
        with self.store.read() as connection:
            self.assertEqual(device_rows(connection), [])

    def test_emu_probe_failures_fall_back_to_atomic_claim_count(self):
        failures = (
            FileNotFoundError("ps missing"),
            subprocess.TimeoutExpired(["ps"], 2),
            subprocess.CalledProcessError(1, ["ps"]),
            subprocess.CompletedProcess([], 1, "101 qemu-system-x86_64 qemu-system-x86_64 -port 5554\n", "denied"),
        )
        for failure in failures:
            with self.subTest(failure=type(failure).__name__):
                self.seed_emulators(2)
                self.runner.reset_mock()
                self.runner.side_effect = failure if isinstance(failure, Exception) else None
                self.runner.return_value = failure
                self.assertIsNone(self.claims._emulator_processes())
                self.emu_claim(device="5558")
                with self.assertRaises(LedgerError) as raised:
                    self.emu_claim(device="5560")
                self.assertEqual(raised.exception.code, "emu_limit")
                self.assertEqual(raised.exception.details["claim_count"], 3)
                self.assertIsNone(raised.exception.details["process_count"])
                self.assertEqual(raised.exception.details["untracked_processes"], [])
                self.assertEqual(self.runner.call_count, 3)
                for call in self.runner.call_args_list:
                    self.assertEqual(call.args[0], ["ps", "-axww", "-o", "pid=", "-o", "ucomm=", "-o", "args="])
                    self.assertEqual(call.kwargs["timeout"], 2)
                self.claims.release(self.agent("a"), "emu")
                self.claims.release(self.agent("b"), "emu")
                self.runner.side_effect = None

    def test_emu_process_parser_accepts_android_mac_linux_paths_and_flags_only(self):
        self.runner.return_value = subprocess.CompletedProcess([], 0, (
            "  101 qemu-system-x86_64 /home/test/Android/Sdk/emulator/qemu/linux-x86_64/qemu-system-x86_64 -read-only\n"
            "  102 qemu-system-aarch64 /Users/test/Library/Android/sdk/emulator/qemu/darwin-aarch64/qemu-system-aarch64 -port 5556\n"
            "  103 qemu-system-x86_64 qemu-system-x86_64 -avd Pixel_9\n"
            "  104 qemu-system-aarch64 /opt/qemu-system-aarch64 -port=5558\n"
            "  105 qemu-system-x86_64 qemu-system-x86_64 -ports 5560,5561\n"
            "  106 qemu-system-x86_64 qemu-system-x86_64 -ports=5562,5563\n"
            "  107 qemu-system-aarch64 /Users/test/Android SDK/emulator/qemu/darwin-aarch64/qemu-system-aarch64 -port 5564\n"
            "  108 qemu-system-x86_64 qemu-system-x86_64 -avd Kidori's_Pixel -port 5566\n"
            "  109 qemu-system-aarch64 /home/test/Android SDK/emulator/qemu/linux-aarch64/qemu-system-aarch64 -read-only\n"
            "  201 qemu-system-x86_64 /usr/bin/qemu-system-x86_64 -machine q35\n"
            "  202 qemu-system-aarch64 /usr/bin/qemu-system-aarch64 -machine virt\n"
            "  203 python /usr/bin/python worker.py qemu-system-x86_64 -avd Pixel\n"
            "  204 not-qemu-system-x86_64 /opt/emulator/not-qemu-system-x86_64 -port 5570\n"
            "  205 emulator /opt/emulator/emulator -avd Pixel\n"
            "  206 echo /usr/bin/echo /sdk/emulator/qemu-system-x86_64 -port 5572\n"
            "  207 qemu-system-x86_64 /usr/bin/qemu-system-x86_64 -machine q35 -drive file=/home/test/emulator/disk.qcow2\n"
            "  PID UCOMM COMMAND\n"
            "garbage\n"
        ), "")
        self.assertEqual(self.claims._emulator_processes(), [
            {"pid": 101, "port": None}, {"pid": 102, "port": 5556},
            {"pid": 103, "port": None}, {"pid": 104, "port": 5558},
            {"pid": 105, "port": 5560}, {"pid": 106, "port": 5562},
            {"pid": 107, "port": 5564}, {"pid": 108, "port": 5566},
            {"pid": 109, "port": None},
        ])
        self.runner.assert_called_once_with(
            ["ps", "-axww", "-o", "pid=", "-o", "ucomm=", "-o", "args="],
            capture_output=True, text=True, timeout=2, check=False,
        )

    def test_emu_non_android_qemu_does_not_consume_capacity(self):
        self.runner.return_value = subprocess.CompletedProcess([], 0, "\n".join(
            f"{100 + index} qemu-system-x86_64 /usr/bin/qemu-system-x86_64 -machine q35 -name vm{index}"
            for index in range(5)
        ), "")
        self.assertEqual(self.emu_claim()["device"]["device_id"], "emulator-5554")

    def test_emu_stale_and_other_host_claims_do_not_consume_capacity(self):
        self.seed_emulators()
        with self.store.transaction() as connection:
            connection.execute("UPDATE agents SET active=0 WHERE terminal_id='b'")
        self.emu_claim(device="5560")
        with self.store.read() as connection:
            self.assertEqual([row["device_id"] for row in device_rows(connection)], ["emulator-5560"])
        self.registry.reconcile(self.snapshot, refresh_display=False)
        self.seed_emulators()
        with self.store.transaction() as connection:
            connection.execute("UPDATE agents SET host='beta' WHERE terminal_id='b'")
        self.emu_claim(device="5562")
        with self.store.read() as connection:
            rows = device_rows(connection)
        self.assertEqual(sum(not row["stale"] and row["holder"]["host"] == "alpha" for row in rows), 2)
        self.assertEqual(sum(not row["stale"] and row["holder"]["host"] == "beta" for row in rows), 3)

    def test_emu_concurrent_claims_at_limit_minus_one_admit_exactly_one(self):
        self.seed_emulators(2)
        agents = [self.agent("a"), self.agent("b")]
        barrier = threading.Barrier(2)

        def probe(command, **kwargs):
            self.assertEqual(command, ["ps", "-axww", "-o", "pid=", "-o", "ucomm=", "-o", "args="])
            barrier.wait(timeout=5)
            return subprocess.CompletedProcess(command, 0, "", "")

        def claim(index):
            try:
                return self.claims.claim(agents[index], "emu", str(5558 + index * 2),
                                         label="Concurrent", environment=self.environment)
            except LedgerError as error:
                return error

        self.runner.reset_mock()
        self.runner.side_effect = probe
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(claim, range(2)))
        refused = [result for result in results if isinstance(result, LedgerError)]
        self.assertEqual(len(refused), 1)
        self.assertEqual(refused[0].code, "emu_limit")
        self.assertEqual(refused[0].details["claim_count"], 3)
        self.assertEqual(sum(isinstance(result, dict) for result in results), 1)
        self.assertEqual(self.runner.call_count, 2)
        with self.store.read() as connection:
            self.assertEqual(len(device_rows(connection)), 3)

    def test_cli_emu_limit_refusal_is_nonzero_with_flat_full_details(self):
        seeded = self.seed_emulators()
        self.runner.return_value = subprocess.CompletedProcess([], 0, (
            "101 qemu-system-x86_64 qemu-system-x86_64 -port 5554\n"
            "102 qemu-system-x86_64 qemu-system-x86_64 -port 5556\n"
            "103 qemu-system-x86_64 qemu-system-x86_64 -port 5562\n"
        ), "")
        output = io.StringIO()
        with mock.patch("ham.ledger.status.live_snapshot", return_value=self.snapshot), \
             contextlib.redirect_stdout(output):
            code = agent_skill_cli.main(
                ["device", "claim", "emu", "5560", "--label", "Android"], self.environment,
            )
        error = json.loads(output.getvalue())["error"]
        self.assertEqual(code, 1)
        self.assertEqual(error["code"], "emu_limit")
        self.assertTrue(error["message"])
        self.assertNotIn("details", error)
        self.assertEqual({key: value for key, value in error.items() if key not in {"code", "message"}}, {
            "limit": 3, "claim_count": 3, "process_count": 3,
            "holders": [
                {"alias": "agent-b", "pane": "p-b", "device": row["device_id"],
                 "label": row["label"], "claimed_at": row["claimed_at"]}
                for row in seeded
            ],
            "untracked_processes": [{"pid": 103, "port": 5562}],
        })

    def test_phone_claims_do_not_consume_emu_capacity(self):
        for index in range(5):
            self.claim(device=f"phone-{index}")
        result = self.emu_claim()
        self.assertEqual(result["device"]["device_id"], "emulator-5554")

    def test_conflict_force_reclaim_and_release_scope(self):
        first = self.claim(now=10)
        self.assertIsNone(first["replaced"])
        again = self.claim(label="New label", now=20)
        self.assertEqual((again["device"]["label"], again["device"]["claimed_at"]), ("New label", 10))
        with self.assertRaises(LedgerError) as raised:
            self.claim("b")
        self.assertEqual(raised.exception.code, "device_claimed")
        self.assertEqual(raised.exception.holder["pane_id"], "p-a")
        self.assertEqual(self.claims.release(self.agent("b"))["released"], [])
        forced = self.claim("b", force=True, now=30)
        self.assertEqual(forced["replaced"]["holder"]["agent_id"], self.agent()["agent_id"])
        self.assertFalse(forced["replaced_stale"])
        self.project.assert_called_with({"p-a", "p-b"}, environment=None)
        self.claim("b", device="5554", kind="emu", label="Android")
        released = self.claims.release(self.agent("b"), "emu", "5554")["released"]
        self.assertEqual([row["device_id"] for row in released], ["emulator-5554"])
        self.assertEqual([row["device_id"] for row in self.claims.release(self.agent("b"))["released"]], ["serial"])

    def test_stale_holder_replaced_and_other_stale_claims_pruned(self):
        self.claim("b")
        self.claim("b", device="old")
        with self.store.transaction() as connection:
            connection.execute("UPDATE agents SET active=0 WHERE terminal_id='b'")
        result = self.claim(now=40)
        self.assertTrue(result["replaced_stale"])
        self.assertTrue(result["replaced"]["stale"])
        self.assertEqual([row["device_id"] for row in self.claims.list()["devices"]], ["serial"])

    def test_generation_change_refuses_mutation_and_registry_prunes(self):
        previous = self.agent()
        self.claim()
        changed = copy.deepcopy(self.snapshot)
        changed["agents"][0]["agent_session"]["value"] = "replacement"
        self.registry.reconcile(changed, refresh_display=False)
        self.assertEqual(self.claims.list()["devices"], [])
        for action in (lambda: self.claims.claim(previous, "phone", "serial", label="Pixel"),
                       lambda: self.claims.release(previous)):
            with self.assertRaises(LedgerError) as raised:
                action()
            self.assertEqual(raised.exception.code, "stale_session")

    def test_cli_refresh_preserves_stale_target_until_claim_reports_replacement(self):
        self.claim("b")
        changed = copy.deepcopy(self.snapshot)
        changed["agents"] = changed["agents"][:1]
        with mock.patch("ham.ledger.status.live_snapshot", return_value=changed):
            result = device_operation(self.args("claim", "phone", "serial", "--label", "Pixel"), self.environment)
        self.assertTrue(result["replaced_stale"])

    def test_unresolved_caller_and_failed_snapshot_never_replace_holder(self):
        self.claim("b")
        inherited = {**self.environment, "HERDR_PANE_ID": "p-b"}
        inherited.pop("HERDR_TERMINAL_ID")
        with mock.patch("ham.ledger.status.live_snapshot", return_value=self.snapshot), \
             mock.patch("ham.ledger.status.process_ancestors", return_value=set()), \
             mock.patch("ham.ledger.status.pane_shell_pid", return_value=999):
            with self.assertRaises(LedgerError) as raised:
                device_operation(self.args("claim", "phone", "serial", "--force"), inherited)
        self.assertEqual(raised.exception.code, "sender_unresolved")
        with mock.patch("ham.ledger.status.live_snapshot", side_effect=LedgerError("host_unavailable", "offline")):
            with self.assertRaises(LedgerError) as raised:
                device_operation(self.args("claim", "phone", "serial", "--force"), self.environment)
        self.assertEqual(raised.exception.code, "host_unavailable")
        self.assertEqual(self.claims.list()["devices"][0]["holder"]["terminal_id"], "b")

    def test_default_labels_normalization_and_bounded_adb(self):
        self.runner.return_value = subprocess.CompletedProcess([], 0, "Pixel 9\n", "")
        result = self.claims.claim(self.agent(), "phone", "serial")
        self.assertEqual(result["device"]["label"], "Pixel 9")
        self.assertEqual(self.runner.call_args.kwargs["timeout"], 10)
        result = self.claims.claim(self.agent(), "emu", "5554")
        self.assertEqual((result["device"]["device_id"], result["device"]["label"]), ("emulator-5554", "5554"))
        self.runner.side_effect = FileNotFoundError()
        self.assertEqual(self.claims.claim(self.agent(), "phone", "other")["device"]["label"], "other")

    def test_attachment_states_and_read_only_listing(self):
        self.claim()
        self.claim(device="offline")
        ios = "00008110-001234567890ABCD"
        self.claim(device=ios)
        with self.store.transaction() as connection:
            connection.execute("UPDATE agents SET active=0 WHERE terminal_id='a'")
        with self.store.read() as connection:
            before = list(connection.iterdump())
        rows = {row["device_id"]: row for row in self.claims.list()["devices"]}
        self.assertTrue(rows["serial"]["attached"])
        self.assertFalse(rows["offline"]["attached"])
        self.assertIsNone(rows[ios]["attached"])
        self.assertTrue(rows["serial"]["stale"])
        self.assertEqual(self.runner.call_args.kwargs["timeout"], 2)
        self.runner.side_effect = FileNotFoundError()
        self.assertTrue(all(row["attached"] is None for row in self.claims.list()["devices"]))
        with self.store.read() as connection:
            self.assertEqual(list(connection.iterdump()), before)

    def test_list_with_absent_home_never_makes_directories(self):
        missing = self.root / "absent"
        environment = {"HOME": str(missing), "HERDR_AGENT_LEDGER_DIR": str(missing / "ledger")}
        with mock.patch.object(AgentRegistry, "refresh", side_effect=AssertionError("mutation")):
            self.assertEqual(device_operation(self.args("list"), environment), {"devices": []})
        self.assertFalse(missing.exists())

    def test_list_during_first_bootstrap_returns_empty_without_mutation(self):
        directory = self.root / "bootstrapping"
        directory.mkdir()
        path = directory / "ledger.sqlite3"
        environment = {**self.environment, "HERDR_AGENT_LEDGER_DIR": str(directory)}
        with sqlite3.connect(path) as connection:
            pass
        for metadata_table in (False, True):
            with self.subTest(metadata_table=metadata_table):
                if metadata_table:
                    with sqlite3.connect(path) as connection:
                        connection.execute("CREATE TABLE ledger_meta(key TEXT PRIMARY KEY,value TEXT)")
                before = path.read_bytes()
                with mock.patch.object(AgentRegistry, "refresh", side_effect=AssertionError("mutation")), \
                     mock.patch("ham.ledger.devices.subprocess.run", side_effect=AssertionError("device probe")):
                    self.assertEqual(device_operation(self.args("list"), environment), {"devices": []})
                self.assertEqual(path.read_bytes(), before)
                self.assertEqual({item.name for item in directory.iterdir()}, {"ledger.sqlite3"})

    def test_list_does_not_hide_other_ledger_errors(self):
        with mock.patch.object(LedgerStore, "open_readonly",
                               side_effect=LedgerError("invalid_metadata", "broken")):
            with self.assertRaises(LedgerError) as raised:
                device_operation(self.args("list"), self.environment)
        self.assertEqual(raised.exception.code, "invalid_metadata")

    def test_cli_json_conflict_has_holder_and_list_does_not_refresh(self):
        self.claim("b")
        output = io.StringIO()
        with mock.patch("ham.ledger.status.live_snapshot", return_value=self.snapshot), contextlib.redirect_stdout(output):
            code = agent_skill_cli.main(["device", "claim", "phone", "serial", "--label", "Pixel"], self.environment)
        payload = json.loads(output.getvalue())
        self.assertEqual(code, 1)
        self.assertEqual(payload["error"]["holder"]["alias"], "agent-b")
        with mock.patch.object(AgentRegistry, "refresh", side_effect=AssertionError("mutation")), \
             mock.patch("ham.ledger.devices.subprocess.run", return_value=subprocess.CompletedProcess([], 0, "", "")):
            self.assertEqual(len(device_operation(self.args("list"), self.environment)["devices"]), 1)

    def test_v5_migration_preserves_agents_status_and_claim_foreign_key(self):
        self.registry.set_status(self.agent(), phase="review", summary="preserve")
        with self.store.transaction() as connection:
            connection.execute("DROP TABLE device_claims")
            connection.execute("UPDATE ledger_meta SET value='5' WHERE key='schema_version'")
        migrated = LedgerStore(environment=self.environment)
        with migrated.read() as connection:
            self.assertEqual(connection.execute("SELECT value FROM ledger_meta WHERE key='schema_version'").fetchone()[0], str(SCHEMA_VERSION))
            self.assertEqual(connection.execute("SELECT summary FROM agent_status WHERE agent_id=?", (self.agent()["agent_id"],)).fetchone()[0], "preserve")
            self.assertEqual(device_rows(connection), [])
            self.assertEqual(connection.execute("PRAGMA foreign_key_list(device_claims)").fetchone()[2], "agents")

    def test_board_resources_are_readonly_and_old_peers_remain_unknown(self):
        self.claim()
        self.claim("b", device="stale")
        with self.store.transaction() as connection:
            connection.execute("UPDATE agents SET active=0 WHERE terminal_id='b'")
            connection.execute("INSERT INTO peers VALUES ('beta','peer-uuid','beta','/peer')")
        listener = {"pid": 100, "port": 5180, "bind": "127.0.0.1", "process": "node", "argv": "node dev", "cwd": str(self.root), "pane_id": "p-a", "plugin_id": None, "started_at": 1}
        scanned = {"listeners": [listener], "serve": [], "error": None}
        class Transport:
            def call(inner, peer, command, payload):
                self.assertFalse(payload["refresh"])
                return {"host": {"canonical_name": "beta", "uuid": "peer-uuid"}, "agents": []}
        with self.store.read() as connection:
            before = list(connection.iterdump())
        with mock.patch("ham.ops.resource_scan.scan_host_resources", return_value=scanned), \
             mock.patch.object(AgentRegistry, "refresh", side_effect=AssertionError("mutation")):
            board = status_board(self.store, transport=Transport())
        agent = board["agents"][0]
        self.assertEqual([row["device_id"] for row in agent["devices"]], ["serial"])
        self.assertEqual(agent["servers"][0]["port"], 5180)
        self.assertEqual(len(board["hosts"][0]["resources"]["stale_devices"]), 1)
        self.assertNotIn("resources", board["hosts"][1])
        with self.store.read() as connection:
            self.assertEqual(list(connection.iterdump()), before)


if __name__ == "__main__":
    unittest.main()
