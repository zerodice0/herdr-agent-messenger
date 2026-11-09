"""Pure device reconciliation, bounded probes and read-only CLI boundaries."""
import tests.ham_test_support as ham_test_support  # Guard live state before product imports.
import contextlib
import copy
import io
import json
import subprocess
import threading
import unittest
from unittest import mock

from ham import cli
from ham.ledger.device_board import classify_device, device_board, format_device_board
from ham.ledger.devices import DeviceClaims, device_operation, emulator_limit, scan_devices
from ham.ledger.remote import RemoteLedger
from ham.ledger.status import AgentRegistry
from ham.ledger.store import LedgerError, LedgerStore
from ham.ops import resource_scan
from ham.processes import ProcessInfo, ProcessLookupFailure

UUID = "D7E14B5E-4130-4C9E-A066-E81743DB3C40"
UDID = "00008110-001234567890ABCD"


def claim(device_id, kind="phone", *, stale=False, alias="worker"):
    return {"device_id": device_id, "kind": kind, "label": "Fixture device", "claimed_at": 10,
            "stale": stale, "holder": {"alias": alias, "pane_id": "p1", "repo_id": "repo",
                                        "host": "alpha", "live": not stale, "active": not stale}}


def observed(**overrides):
    return {"adb": [], "emulator_processes": [], "ios_simulators_booted": [],
            "ios_devices": None, "error": None, **overrides}


def snapshot(claims=(), observations=None, **host_fields):
    return {"hosts": [{"host": "alpha", "stale": False, "error": None,
                       "resources": {"device_claims": list(claims), "emu_limit": 3,
                                     "devices_observed": observed() if observations is None else observations},
                       **host_fields}]}


class DeviceProjectionTest(unittest.TestCase):
    def test_classification(self):
        cases = [("emu", "emulator-5554", "android-emulator"),
                 ("phone", "emulator-5554", "android-emulator"),
                 ("emu", UUID, "ios-simulator"), ("emu", UUID.lower(), "ios-simulator"),
                 ("phone", UDID, "ios-device"), ("phone", "a" * 40, "ios-device"),
                 ("phone", "Pixel-serial", "android-device"),
                 ("phone", "192.168.1.2:5555", "android-device"),
                 ("emu", "5554", "unknown"), ("emu", "emulator-foo", "unknown"),
                 ("other", UUID, "unknown"), ("emu", "", "unknown")]
        for kind, identity, expected in cases:
            with self.subTest(kind=kind, identity=identity):
                self.assertEqual(classify_device(kind, identity), expected)

    def test_union_findings_and_source_purity(self):
        source = snapshot([claim("held"), claim("missing"), claim("stale", stale=True),
                           claim("emulator-5554", "emu"), claim(UUID.lower(), "emu"), claim(UDID)],
                          observed(adb=[{"serial": "held", "state": "device"},
                                        {"serial": "free", "state": "device"},
                                        {"serial": "stale", "state": "offline"}],
                                   emulator_processes=[{"pid": 40, "port": 5554},
                                                       {"pid": 41, "port": None}],
                                   ios_simulators_booted=[{"udid": UUID, "name": "iPhone"}]))
        before = copy.deepcopy(source)
        host = device_board(source, now=40)["hosts"][0]
        rows = {row["device_id"]: row for row in host["devices"]}
        self.assertEqual(rows["held"]["observed_state"], "attached")
        self.assertIsNone(rows["held"]["finding"])
        self.assertEqual(rows["held"]["age"], 30)
        self.assertEqual(rows["missing"]["finding"], "missing")
        self.assertEqual(rows["stale"]["finding"], "stale")
        self.assertEqual(rows["free"]["finding"], "unclaimed")
        self.assertEqual(rows["emulator-5554"]["observed_state"], "running")
        self.assertIsNone(rows["emulator-5554"]["finding"])
        self.assertEqual(rows[UUID.lower()]["observed_state"], "booted")
        self.assertNotIn(UUID, rows)
        self.assertEqual(rows[UDID]["observed_state"], "unknown")
        self.assertIsNone(rows[UDID]["finding"])
        self.assertEqual(rows[None]["platform"], "unknown")
        self.assertEqual(rows[None]["process_pid"], 41)
        self.assertIsNone(rows[None]["finding"])  # A missing port cannot disprove a matching claim.
        self.assertEqual(host["emu_limit"], {"limit": 3, "in_use": 2, "claim_count": 1,
                                              "process_count": 2, "known": True})
        self.assertIsNone(host["counts"]["ios-device"])
        self.assertEqual(source, before)

    def test_ios_identity_casefold_does_not_casefold_android(self):
        board = device_board(snapshot([claim("Android"), claim("android")],
                                      observed(adb=[{"serial": "Android", "state": "device"}])))
        rows = {row["device_id"]: row for row in board["hosts"][0]["devices"]}
        self.assertEqual(rows["Android"]["observed_state"], "attached")
        self.assertEqual(rows["android"]["finding"], "missing")
        self.assertEqual(classify_device("phone", UDID.lower()), "ios-device")

    def test_offline_and_unauthorized_are_seen_not_missing(self):
        for state in ("offline", "unauthorized"):
            for identity, kind in (("phone-serial", "phone"), ("emulator-5554", "emu")):
                with self.subTest(state=state, kind=kind):
                    source = snapshot([claim(identity, kind)],
                                      observed(adb=[{"serial": identity, "state": state}]))
                    row = device_board(source)["hosts"][0]["devices"][0]
                    self.assertEqual(row["observed_state"], "offline")
                    self.assertIsNone(row["finding"])
                    source["hosts"][0]["resources"]["device_claims"][0]["stale"] = True
                    self.assertEqual(device_board(source)["hosts"][0]["devices"][0]["finding"], "stale")

    def test_null_probes_never_mean_missing(self):
        host = device_board(snapshot([claim("serial"), claim(UUID, "emu"), claim("emulator-5554", "emu")],
                                     observed(adb=None, emulator_processes=None,
                                              ios_simulators_booted=None, error="tools unavailable")))["hosts"][0]
        self.assertTrue(all(row["observed_state"] == "unknown" for row in host["devices"]))
        self.assertTrue(all(row["finding"] is None for row in host["devices"]))
        self.assertEqual(host["error"], "tools unavailable")
        self.assertEqual(host["emu_limit"]["in_use"], 1)
        self.assertFalse(host["emu_limit"]["known"])
        self.assertIsNone(host["emu_limit"]["process_count"])
        self.assertTrue(all(count is None for count in host["counts"].values()))

    def test_process_corroborates_even_offline_or_absent_adb(self):
        for adb in (None, [], [{"serial": "emulator-5554", "state": "offline"}]):
            with self.subTest(adb=adb):
                row = device_board(snapshot([claim("emulator-5554", "emu")],
                                            observed(adb=adb, emulator_processes=[{"pid": 5, "port": 5554}])))["hosts"][0]["devices"][0]
                self.assertEqual(row["observed_state"], "running")
                self.assertIsNone(row["finding"])

    def test_failed_process_probe_does_not_turn_absent_adb_into_missing(self):
        for adb in ([], [{"serial": "emulator-5554", "state": "offline"}]):
            with self.subTest(adb=adb):
                row = device_board(snapshot([claim("emulator-5554", "emu")],
                                            observed(adb=adb, emulator_processes=None)))["hosts"][0]["devices"][0]
                self.assertIsNone(row["finding"])
        missing = device_board(snapshot([claim("emulator-5554", "emu")]))["hosts"][0]["devices"][0]
        self.assertEqual(missing["finding"], "missing")

    def test_unidentified_process_prevents_claim_absence_inference(self):
        source = snapshot([claim("emulator-5554", "emu"), claim("emulator-5556", "emu"),
                           claim("emulator-5558", "emu", stale=True), claim("phone-serial")],
                          observed(emulator_processes=[{"pid": 1, "port": None}, {"pid": 2, "port": 5556}]))
        host = device_board(source)["hosts"][0]
        rows = {row["device_id"]: row for row in host["devices"]}
        self.assertEqual(rows["emulator-5554"]["observed_state"], "unknown")
        self.assertIsNone(rows["emulator-5554"]["finding"])
        self.assertEqual(rows["emulator-5556"]["observed_state"], "running")
        self.assertEqual(rows["emulator-5558"]["observed_state"], "unknown")
        self.assertEqual(rows["emulator-5558"]["finding"], "stale")
        self.assertEqual(rows["phone-serial"]["finding"], "missing")
        self.assertEqual(host["emu_limit"]["process_count"], 2)

    def test_legacy_omission_keeps_counts_unknown_and_claims_visible(self):
        source = {"hosts": [{"host": "alpha"}],
                  "agents": [{"host": "alpha", "alias": "worker", "devices": [claim("serial")]},
                             {"host": "alpha", "devices": None},
                             {"host": "beta", "devices": [claim("other")]}]}
        host = device_board(source)["hosts"][0]
        self.assertEqual([row["device_id"] for row in host["devices"]], ["serial"])
        self.assertIsNone(host["emu_limit"]["limit"])
        self.assertIsNone(host["emu_limit"]["in_use"])
        self.assertTrue(all(value is None for value in host["counts"].values()))
        self.assertEqual(host["devices"][0]["observed_state"], "unknown")

    def test_legacy_and_complete_holders_expose_only_public_identity(self):
        holder = {"agent_id": "a", "alias": "worker", "pane_id": "p1", "host": "alpha",
                  "repo_id": "repo", "terminal_id": "terminal", "generation": 2,
                  "workspace_id": "workspace", "active": True, "live": True,
                  "session_ref": "private-session", "cwd": "/private/path",
                  "native_session_pid": 123, "last_ham_label": "private-label"}
        source = {"hosts": [{"host": "alpha"}],
                  "agents": [{**holder, "devices": [claim("serial")]}]}
        public = {key: value for key, value in holder.items()
                  if key not in {"session_ref", "cwd", "native_session_pid", "last_ham_label"}}
        self.assertEqual(device_board(source)["hosts"][0]["devices"][0]["holder"], public)
        complete = snapshot([{**claim("serial"), "holder": holder}])
        self.assertEqual(device_board(complete)["hosts"][0]["devices"][0]["holder"], public)

    def test_complete_claims_override_filtered_agents_and_include_stale(self):
        source = snapshot([claim("other-repo", stale=True)])
        source["agents"] = [{"host": "alpha", "devices": [claim("irrelevant")]}]
        host = device_board(source)["hosts"][0]
        self.assertEqual([row["device_id"] for row in host["devices"]], ["other-repo"])
        self.assertEqual(host["devices"][0]["finding"], "stale")

    def test_cached_peer_is_stale_not_fresh_observation(self):
        error = {"code": "transport_unavailable", "message": "offline"}
        host = device_board(snapshot([claim("held")], observed(adb=[{"serial": "held", "state": "device"}]),
                                     stale=True, error=error))["hosts"][0]
        self.assertFalse(host["fresh"])
        self.assertEqual(host["error"], error)
        self.assertEqual(host["devices"][0]["observed_state"], "unknown")
        self.assertIsNone(host["emu_limit"]["in_use"])

    def test_admission_count_max_excludes_stale_and_ios_and_includes_unknown_port(self):
        claims = [claim("emulator-5554", "emu"), claim("emulator-5556", "emu"),
                  claim("emulator-5558", "emu", stale=True), claim(UUID, "emu"),
                  claim("emulator-5560", "phone")]
        for process_count in (0, 1, 2, 3, 4):
            with self.subTest(process_count=process_count):
                processes = [{"pid": index + 1, "port": None} for index in range(process_count)]
                usage = device_board(snapshot(claims, observed(emulator_processes=processes)))["hosts"][0]["emu_limit"]
                self.assertEqual(usage["in_use"], max(2, process_count))
                self.assertEqual(usage["claim_count"], 2)
                self.assertTrue(usage["known"])

    def test_empty_and_human_unknown_output(self):
        self.assertEqual(device_board({}), {"hosts": []})
        self.assertEqual(format_device_board({"hosts": []}), "No registered hosts.")
        text = format_device_board(device_board({"hosts": [{"host": "old-peer"}]}))
        self.assertIn("old-peer [fresh]", text)
        self.assertIn("Android emulators: ?/?", text)
        self.assertIn("ios-device (?)", text)

    def test_human_sanitizes_controls_and_mapping_error_without_changing_json(self):
        held = claim("serial\x1b[31m\nidentity")
        held["label"] = "Phone\tlabel\x00\u202e"
        held["holder"].update(alias="agent\nalias\x1b", pane_id="pane\rone", repo_id="repo\tname\u2066")
        error = {"code": "transport\x1b_failure", "message": "peer\nunavailable\u202e"}
        board = device_board(snapshot([held], host="alpha\tname\x1b", error=error))
        before = copy.deepcopy(board)
        rendered = format_device_board(board)
        self.assertIn("alpha name", rendered)
        self.assertIn("serial[31m identity", rendered)
        self.assertIn("Phone label", rendered)
        self.assertIn("agent alias (pane one; repo name)", rendered)
        self.assertIn("error: transport_failure: peer unavailable", rendered)
        self.assertNotIn("{'code':", rendered)
        for control in ("\x1b", "\x00", "\r", "\t", "\u202e", "\u2066"):
            self.assertNotIn(control, rendered)
        self.assertEqual(board, before)
        self.assertEqual(json.loads(json.dumps(board)), before)


class DeviceProbeTest(unittest.TestCase):
    def runner(self, responses):
        def run(command, **kwargs):
            self.assertLessEqual(kwargs["timeout"], 3)
            self.assertFalse(kwargs["check"])
            value = responses[tuple(command)]
            if isinstance(value, Exception):
                raise value
            return subprocess.CompletedProcess(command, 0, value, "")
        return mock.Mock(side_effect=run)

    def test_adb_and_ps_parsing_and_no_physical_ios_probe(self):
        runner = self.runner({("adb", "devices"): "List of devices attached\nSERIAL\tdevice\nnetwork:5555 offline\nlocked unauthorized\n",
                              ("ps", "-axww", "-o", "pid=", "-o", "ucomm=", "-o", "args="):
                              "1 qemu-system-x86_64 /sdk/emulator/qemu/qemu-system-x86_64 -port=5554\n"
                              "2 qemu-system-aarch64 /sdk/emulator/qemu/qemu-system-aarch64 -ports 5556,5557\n"
                              "3 qemu-system-x86_64 /sdk/emulator/qemu/qemu-system-x86_64 -avd Test\n"
                              "4 qemu-system-x86_64 /usr/bin/qemu-system-x86_64 -m 512\n"})
        result = scan_devices(runner=runner, platform="linux")
        self.assertEqual(result["adb"], [{"serial": "SERIAL", "state": "device"},
                                          {"serial": "network:5555", "state": "offline"},
                                          {"serial": "locked", "state": "unauthorized"}])
        self.assertEqual(result["emulator_processes"], [{"pid": 1, "port": 5554},
                                                        {"pid": 2, "port": 5556}, {"pid": 3, "port": None}])
        self.assertIsNone(result["ios_devices"])
        self.assertIsNone(result["ios_simulators_booted"])

    def test_mac_simctl_booted_runtime_dictionary(self):
        responses = {("adb", "devices"): "List of devices attached\n",
                     ("ps", "-axww", "-o", "pid=", "-o", "ucomm=", "-o", "args="): "",
                     ("xcrun", "simctl", "list", "devices", "booted", "--json"):
                     json.dumps({"devices": {"runtime": [{"udid": UUID, "name": "iPhone 18 Pro", "state": "Booted"},
                                                          {"udid": "other", "name": "Off", "state": "Shutdown"}]}})}
        runner = self.runner(responses)
        source = mock.Mock()
        source.pids.return_value = []
        result = scan_devices(runner=runner, platform="darwin", process_source=source)
        self.assertEqual(result["ios_simulators_booted"], [{"udid": UUID, "name": "iPhone 18 Pro"}])
        self.assertIsNone(result["ios_devices"])
        self.assertIsNone(result["error"])
        self.assertFalse(any("devicectl" in call.args[0] for call in runner.call_args_list))
        responses[("xcrun", "simctl", "list", "devices", "booted", "--json")] = '{"devices":{"runtime":[]}}'
        self.assertEqual(scan_devices(runner=runner, platform="darwin",
                                      process_source=source)["ios_simulators_booted"], [])

    def test_missing_tools_timeout_and_malformed_json_are_null(self):
        for payload in ("not json", '{"devices": []}', '{"devices":{"runtime":[{}]}}'):
            with self.subTest(payload=payload):
                runner = self.runner({("adb", "devices"): FileNotFoundError("adb"),
                                      ("ps", "-axww", "-o", "pid=", "-o", "ucomm=", "-o", "args="):
                                      subprocess.TimeoutExpired("ps", 2),
                                      ("xcrun", "simctl", "list", "devices", "booted", "--json"): payload})
                result = scan_devices(runner=runner, platform="darwin")
                self.assertIsNone(result["adb"])
                self.assertIsNone(result["emulator_processes"])
                self.assertIsNone(result["ios_simulators_booted"])
                self.assertIn("adb", result["error"])
                self.assertIn("ios_simulators_booted", result["error"])

    def test_mac_native_candidates_keep_android_count_and_unknown_port(self):
        records = {
            101: ProcessInfo(101, 1, "qemu-system-aarch64", (1, 0)),
            102: ProcessInfo(102, 1, "qemu-system-x86_64", (2, 0)),
            103: ProcessInfo(103, 1, "qemu-system-x86_64", (3, 0)),
            104: ProcessInfo(104, 1, "unrelated", (4, 0)),
        }
        source = mock.Mock()
        source.pids.return_value = list(records)
        source.read.side_effect = records.__getitem__
        runner = self.runner({
            ("ps", "-ww", "-p", "101", "-o", "args="):
                "/Users/test/Android SDK/emulator/qemu-system-aarch64 -ports=5554,5555",
            ("ps", "-ww", "-p", "102", "-o", "args="):
                "/sdk/emulator/qemu-system-x86_64 -avd Test",
            ("ps", "-ww", "-p", "103", "-o", "args="):
                "/usr/bin/qemu-system-x86_64 -m 512",
        })
        probe = DeviceClaims(None, runner=runner, platform="darwin", process_source=source)
        self.assertEqual(probe._emulator_processes(), [
            {"pid": 101, "port": 5554}, {"pid": 102, "port": None}])

    def test_mac_incomplete_or_reused_candidates_are_unknown_not_zero(self):
        original = ProcessInfo(101, 1, "qemu-system-aarch64", (1, 0))
        reused = ProcessInfo(101, 1, "qemu-system-aarch64", (2, 0))
        for responses in ([original, reused], [original, ProcessLookupFailure("exited")],
                          [ProcessLookupFailure("unavailable")]):
            with self.subTest(responses=responses):
                source = mock.Mock()
                source.pids.return_value = [101]
                source.read.side_effect = responses
                runner = self.runner({("ps", "-ww", "-p", "101", "-o", "args="):
                                      "/sdk/emulator/qemu-system-aarch64 -port 5554"})
                probe = DeviceClaims(None, runner=runner, platform="darwin", process_source=source)
                self.assertIsNone(probe._emulator_processes())

    def test_mac_injected_runner_without_source_never_reads_live_host(self):
        with mock.patch("ham.ledger.devices.ProcessSource", side_effect=AssertionError("live source")):
            self.assertIsNone(DeviceClaims(None, runner=self.runner({}), platform="darwin")._emulator_processes())

    def test_probes_are_parallel_and_reuse_admission_parser(self):
        barrier = threading.Barrier(3, timeout=2)
        def runner(command, **kwargs):
            barrier.wait()
            output = '{"devices":{}}' if command[0] == "xcrun" else ""
            return subprocess.CompletedProcess(command, 0, output, "")
        with mock.patch.object(DeviceClaims, "_emulator_processes", autospec=True,
                               side_effect=lambda probe: probe._run(["ps"]) and []) as processes:
            result = scan_devices(runner=runner, platform="darwin")
        self.assertIsNone(result["error"])


    def test_limit_parser_matches_admission_configuration(self):
        for environment, expected in (({}, 3), ({"HERDR_AGENT_EMU_LIMIT": "0"}, 0),
                                      ({"HERDR_AGENT_EMU_LIMIT": "-1"}, -1),
                                      ({"HERDR_AGENT_EMU_LIMIT": "5"}, 5)):
            self.assertEqual(emulator_limit(environment), expected)
        with self.assertRaises(LedgerError) as raised:
            emulator_limit({"HERDR_AGENT_EMU_LIMIT": "invalid"})
        self.assertEqual(raised.exception.code, "invalid_config")


class DeviceBoardCliTest(unittest.TestCase):
    def test_json_is_full_readonly_board_without_registry_refresh_or_tokens(self):
        source = snapshot([claim("serial")], observed(adb=[{"serial": "serial", "state": "device"}]))
        output = io.StringIO()
        with mock.patch("ham.ledger.cli.status_operation", return_value=source) as status, \
             mock.patch.object(AgentRegistry, "refresh", side_effect=AssertionError("registry mutation")), \
             mock.patch.object(AgentRegistry, "_refresh_display", side_effect=AssertionError("tokens")), \
             mock.patch("ham.ledger.devices.prune_stale", side_effect=AssertionError("prune")), \
             contextlib.redirect_stdout(output):
            code = cli.main(["device", "board", "--json"], {})
        self.assertEqual(code, 0)
        payload = json.loads(output.getvalue())
        self.assertEqual(set(payload), {"hosts"})
        self.assertEqual(payload["hosts"][0]["devices"][0]["device_id"], "serial")
        self.assertEqual(payload["hosts"][0]["emu_limit"]["limit"], 3)
        status.assert_called_once_with("board", environment={})

    def test_human_output_is_grouped_and_default(self):
        output = io.StringIO()
        with mock.patch("ham.ledger.cli.status_operation", return_value=snapshot([claim("serial")])), \
             contextlib.redirect_stdout(output):
            self.assertEqual(cli.main(["device", "board"], {}), 0)
        self.assertIn("alpha [fresh]", output.getvalue())
        self.assertIn("android-device", output.getvalue())
        self.assertIn("serial", output.getvalue())
        self.assertIn("worker", output.getvalue())

    def test_absent_ledger_returns_empty_without_initializing(self):
        arguments = cli.parse_cli_arguments(["device", "board", "--json"])
        with mock.patch.object(LedgerStore, "open_readonly", return_value=None), \
             mock.patch.object(LedgerStore, "__init__", side_effect=AssertionError("bootstrap")), \
             mock.patch.object(AgentRegistry, "refresh", side_effect=AssertionError("refresh")):
            self.assertEqual(device_operation(arguments, {}), {"hosts": []})

    def test_status_export_only_probes_when_resources_are_requested(self):
        store = mock.Mock(host="alpha", host_uuid="uuid")
        store.read.return_value.__enter__ = mock.Mock(return_value=mock.Mock())
        store.read.return_value.__exit__ = mock.Mock(return_value=False)
        store.read.return_value.__enter__.return_value.execute.return_value = []
        service = RemoteLedger(store, {}, transport=mock.Mock())
        claims = [claim("stale", stale=True), claim("current")]
        with mock.patch.object(AgentRegistry, "list", return_value=[]), \
             mock.patch("ham.ledger.devices.device_rows", return_value=claims), \
             mock.patch("ham.ops.resource_scan.scan_host_resources", return_value={
                 "listeners": [], "serve": [], "error": None, "devices_observed": observed()}) as scanner:
            plain = service.status_export()
            scanner.assert_not_called()
            rich = service.status_export(resources=True, wire=True)
        self.assertNotIn("resources", plain)
        self.assertEqual(rich["resources"]["device_claims"], claims)
        self.assertEqual(rich["resources"]["emu_limit"], 3)
        self.assertEqual(rich["resources"]["devices_observed"], observed())
        scanner.assert_called_once_with()

    def test_invalid_limit_is_unknown_with_observation_diagnostic_not_read_failure(self):
        store = mock.Mock(host="alpha", host_uuid="uuid")
        store.read.return_value.__enter__ = mock.Mock(return_value=mock.Mock())
        store.read.return_value.__exit__ = mock.Mock(return_value=False)
        store.read.return_value.__enter__.return_value.execute.return_value = []
        service = RemoteLedger(store, {"HERDR_AGENT_EMU_LIMIT": "invalid"}, transport=mock.Mock())
        for observations in (None, observed(error="adb unavailable")):
            with self.subTest(observations=observations):
                scanned = {"listeners": [], "serve": [], "error": None, "devices_observed": observations}
                before = copy.deepcopy(scanned)
                with mock.patch.object(AgentRegistry, "list", return_value=[]), \
                     mock.patch("ham.ledger.devices.device_rows", return_value=[]):
                    exported = service.status_export(resources=True, resource_scanner=lambda: scanned)
                resources = exported["resources"]
                self.assertIsNone(resources["emu_limit"])
                self.assertIn("invalid_config", resources["devices_observed"]["error"])
                self.assertIn("HERDR_AGENT_EMU_LIMIT", resources["devices_observed"]["error"])
                if observations:
                    self.assertIn("adb unavailable", resources["devices_observed"]["error"])
                host = device_board({"hosts": [{"host": "alpha", "resources": resources}]})["hosts"][0]
                self.assertIsNone(host["emu_limit"]["limit"])
                self.assertIn("invalid_config", host["error"])
                self.assertEqual(scanned, before)


if __name__ == "__main__":
    unittest.main()
