"""Read-only resource discovery fixtures and host/session ownership boundaries."""

from datetime import datetime
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import unittest
import time
from unittest import mock

import resource_scan


SS = '''LISTEN 0 511 127.0.0.1:5180 0.0.0.0:* users:(("node-MainThread",pid=2367686,fd=24))
LISTEN 0 511 [::1]:5180 [::]:* users:(("node-MainThread",pid=2367686,fd=25))
LISTEN 0 128 0.0.0.0:22 0.0.0.0:*
LISTEN 0 512 *:5292 *:* users:(("bun",pid=3981497,fd=12))
LISTEN 0 512 127.0.0.1:5037 0.0.0.0:* users:(("adb",pid=2304728,fd=12))
LISTEN 0 128 127.0.0.1:9999 0.0.0.0:* users:(("root-server",pid=90,fd=3))
LISTEN 0 128 127.0.0.1:9998 0.0.0.0:* users:(("exited",pid=91,fd=3))
'''
LSOF = f'''p51740
cnode
u{os.getuid()}
n127.0.0.1:5180
n[::1]:5180
p52000
cbun
u{os.getuid()}
n*:7317
p53000
comp
u{os.getuid()}
n127.0.0.1:41001
p53001
cadb
u{os.getuid()}
n127.0.0.1:5037
p54000
cpython3
u{os.getuid()}
n127.0.0.1:9990
p90
croot-server
u{os.getuid() + 1}
n127.0.0.1:9999
'''
SERVE = {
    "TCP": {"443": {"HTTPS": True}, "8477": {"HTTPS": True}},
    "Web": {
        "winmini.example.ts.net:443": {"Handlers": {
            "/": {"Proxy": "http://127.0.0.1:5180"},
            "/files/": {"Path": "/Users/test/review captures"},
        }},
        "winmini.example.ts.net:8477": {"Handlers": {
            "/review": {"Proxy": "http://localhost:5292"},
            "/dead": {"Proxy": "http://127.0.0.1:5288"},
            "/ipv6": {"Proxy": "http://[::1]:5318"},
            "/remote": {"Proxy": "http://192.168.1.50:8080"},
        }},
    },
}


class FixtureRunner:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def __call__(self, command, **kwargs):
        self.calls.append((command, kwargs))
        response = self.responses[tuple(command)]
        if isinstance(response, Exception):
            raise response
        if isinstance(response, tuple):
            code, stdout, stderr = response
        else:
            code, stdout, stderr = 0, response, ""
        return subprocess.CompletedProcess(command, code, stdout, stderr)


class ListenerScanTest(unittest.TestCase):
    def test_linux_proc_metadata_current_user_start_time_and_dual_stack_deduplication(self):
        runner = FixtureRunner({("ss", "-ltnpH"): SS})
        stat_fields = ["S"] + ["0"] * 18 + ["12345"] + ["0"] * 8
        contents = {
            "/fixture/stat": "cpu 1 2 3\nbtime 1700000000\n",
            "/fixture/2367686/environ": "OTHER=HERDR_PANE_ID=wrong\0HERDR_PANE_ID=wA:p1\0HOME=/Users/test\0",
            "/fixture/2367686/cmdline": "node\0/Users/test/dev server/vite.js\0--host\0",
            # Comm may itself contain spaces and closing parentheses.
            "/fixture/2367686/stat": "2367686 (node (worker)) " + " ".join(stat_fields),
            "/fixture/3981497/environ": "HERDR_PANE_ID=wA:p1\0HERDR_PLUGIN_ID=herdr-web-ui\0",
            "/fixture/3981497/cmdline": "bun\0server.ts\0",
            "/fixture/2304728/environ": "HERDR_PANE_ID=wA:p2\0",
        }

        def stat(path):
            if str(path).endswith("/91"):
                raise FileNotFoundError
            return SimpleNamespace(st_uid=os.getuid() + (1 if str(path).endswith("/90") else 0))

        with mock.patch.object(Path, "read_text", autospec=True,
                               side_effect=lambda path, **kwargs: contents.get(str(path), "")), \
             mock.patch.object(Path, "stat", autospec=True, side_effect=stat), \
             mock.patch.object(Path, "readlink", autospec=True,
                               side_effect=lambda path: Path("/Users/test/dev server")), \
             mock.patch.object(resource_scan.os, "sysconf", return_value=100):
            endpoints = []
            rows = resource_scan.scan_listeners(runner=runner, platform="linux", now=999,
                                                proc_root="/fixture", listener_endpoints=endpoints)
        self.assertEqual([row["port"] for row in rows], [5037, 5180, 5292])
        server = next(row for row in rows if row["port"] == 5180)
        self.assertEqual(server, {"pid": 2367686, "port": 5180, "bind": "127.0.0.1",
                                 "binds": ["127.0.0.1", "::1"],
                                 "process": "node-MainThread", "argv": "node /Users/test/dev server/vite.js --host",
                                 "cwd": "/Users/test/dev server", "pane_id": "wA:p1",
                                 "plugin_id": None, "started_at": 1700000123.45})
        self.assertEqual(next(row for row in rows if row["port"] == 5292)["plugin_id"], "herdr-web-ui")
        self.assertTrue(all(call[1]["timeout"] <= 2 for call in runner.calls))
        self.assertEqual({endpoint["port"] for endpoint in endpoints}, {22, 5037, 5180, 5292, 9998, 9999})
        routes = [{"kind": "proxy", "port": port, "target": f"http://127.0.0.1:{port}", "url": f"/{port}"}
                  for port in (22, 9999)]
        _, resources = resource_scan.attribute(rows, routes, [], listener_endpoints=endpoints)
        self.assertEqual(resources["dead_serve"], [])

    def test_linux_unreadable_process_metadata_is_unowned_not_a_board_failure(self):
        runner = FixtureRunner({("ss", "-ltnpH"): SS.splitlines()[0]})
        with mock.patch.object(Path, "read_text", side_effect=PermissionError), \
             mock.patch.object(Path, "stat", return_value=SimpleNamespace(st_uid=os.getuid())), \
             mock.patch.object(Path, "readlink", side_effect=PermissionError):
            rows = resource_scan.scan_listeners(runner=runner, platform="linux")
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]["pane_id"])
        self.assertIsNone(rows[0]["cwd"])
        self.assertIsNone(rows[0]["started_at"])

    def mac_runner(self):
        responses = {("lsof", "-nP", "-iTCP", "-sTCP:LISTEN", "-F", "pucn"): LSOF}
        commands = {
            51740: ("/opt/homebrew/bin/node /Users/test/review/vite.js --note HERDR_PANE_ID=argv-only",
                    "OTHER=HERDR_PANE_ID=bad NOT_HERDR_PANE_ID=bad HERDR_PANE_ID=wM:p1 HOME=/Users/test"),
            52000: ("bun server.ts", "HERDR_PANE_ID=wM:p1 HERDR_PLUGIN_ID=herdr-web-ui"),
            53000: ("/Users/test/.local/bin/omp", "HERDR_PANE_ID=wM:p1"),
            53001: ("adb -L tcp:5037 fork-server server", "HERDR_PANE_ID=wM:p1"),
            54000: ("python3 script.py --note=HERDR_PANE_ID=bad HERDR_PANE_ID=literal-argv", "OTHER=HERDR_PANE_ID=bad"),
        }
        pids = ",".join(str(pid) for pid in sorted(commands))
        plain, with_env, cwds = [], [], []
        for pid, (command, environment) in commands.items():
            # Deliberately padded single-digit days, as emitted by ps lstart.
            prefix = f" {pid} Sun Oct  8 12:34:56 2026 "
            plain.append(prefix + command)
            with_env.append(prefix + command + " " + environment)
            cwds.append(f"p{pid}\nn/Users/test/review tree/{pid}")
        responses[("ps", "-ww", "-p", pids, "-o", "pid=,lstart=,command=")] = "\n".join(plain) + "\n"
        responses[("ps", "-E", "-ww", "-p", pids, "-o", "pid=,lstart=,command=")] = "\n".join(with_env) + "\n"
        responses[("lsof", "-a", "-p", pids, "-d", "cwd", "-Fpn")] = "\n".join(cwds) + "\n"
        return FixtureRunner(responses)

    def test_macos_lsof_records_environment_suffix_and_start_times(self):
        runner = self.mac_runner()
        endpoints = []
        rows = resource_scan.scan_listeners(runner=runner, platform="darwin", now=0,
                                            listener_endpoints=endpoints)
        self.assertEqual([row["port"] for row in rows], [5037, 5180, 7317, 9990, 41001])
        node = next(row for row in rows if row["port"] == 5180)
        self.assertEqual(node["pane_id"], "wM:p1")
        self.assertEqual(node["cwd"], "/Users/test/review tree/51740")
        self.assertEqual(node["started_at"], datetime(2026, 10, 8, 12, 34, 56).timestamp())
        self.assertEqual(node["binds"], ["127.0.0.1", "::1"])
        self.assertIn("HERDR_PANE_ID=argv-only", node["argv"])
        self.assertEqual(next(row for row in rows if row["port"] == 7317)["plugin_id"], "herdr-web-ui")
        self.assertIsNone(next(row for row in rows if row["port"] == 9990)["pane_id"])
        self.assertEqual(len(runner.calls), 4)
        self.assertEqual(sum(command[:1] == ["ps"] and "-E" in command for command, _ in runner.calls), 1)
        self.assertTrue(all(kwargs["timeout"] <= 2 and kwargs["env"]["LC_ALL"] == "C"
                            and kwargs["errors"] == "replace" for _, kwargs in runner.calls))
        self.assertEqual({endpoint["port"] for endpoint in endpoints}, {5037, 5180, 7317, 9990, 9999, 41001})
        self.assertTrue(all("90" not in command[command.index("-p") + 1].split(",")
                            for command, _ in runner.calls if "-p" in command))
        route = {"kind": "proxy", "port": 9999, "target": "http://127.0.0.1:9999", "url": "/root"}
        _, resources = resource_scan.attribute(rows, [route], [], listener_endpoints=endpoints)
        self.assertEqual(resources["dead_serve"], [])

    def test_macos_process_metadata_failure_is_tolerated(self):
        runner = self.mac_runner()
        for key in list(runner.responses):
            if key[0] == "ps" or "-d" in key:
                runner.responses[key] = subprocess.TimeoutExpired(key, 2)
        rows = resource_scan.scan_listeners(runner=runner, platform="darwin")
        node = next(row for row in rows if row["port"] == 5180)
        self.assertIsNone(node["pane_id"])
        self.assertIsNone(node["started_at"])
        self.assertTrue(all(row["pane_id"] is None for row in rows))

    def test_macos_batch_metadata_stays_with_its_pid_when_output_is_reordered_or_missing(self):
        runner = self.mac_runner()
        for key, value in list(runner.responses.items()):
            if key[0] == "ps" or "-d" in key:
                if key[0] == "ps":
                    runner.responses[key] = "\n".join(reversed([line for line in value.splitlines()
                                                                if line.split()[0] != "52000"]))
                else:
                    runner.responses[key] = value.replace("p52000\nn/Users/test/review tree/52000\n", "")
        rows = resource_scan.scan_listeners(runner=runner, platform="darwin")
        by_pid = {row["pid"]: row for row in rows}
        self.assertEqual(by_pid[51740]["pane_id"], "wM:p1")
        self.assertEqual(by_pid[53001]["cwd"], "/Users/test/review tree/53001")
        self.assertEqual(by_pid[52000]["argv"], "")
        self.assertIsNone(by_pid[52000]["pane_id"])
        self.assertIsNone(by_pid[52000]["started_at"])
        self.assertIsNone(by_pid[52000]["cwd"])

    def test_macos_c_locale_dates_do_not_use_the_callers_localized_strptime(self):
        # Earlier TUI tests change LC_ALL process-wide. A C-locale ps date must
        # still parse when the caller's localized strptime rejects English names.
        with mock.patch.object(resource_scan, "datetime", wraps=datetime) as dates:
            dates.strptime.side_effect = ValueError("English names rejected by LC_TIME")
            rows = resource_scan.scan_listeners(runner=self.mac_runner(), platform="darwin")
        self.assertTrue(all(row["started_at"] == datetime(2026, 10, 8, 12, 34, 56).timestamp()
                            for row in rows))
        dates.strptime.assert_not_called()

    def test_macos_start_time_parser_all_months_invalid_dates_and_local_timezone(self):
        months = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
        for month, name in enumerate(months, 1):
            with self.subTest(month=name):
                self.assertEqual(resource_scan._mac_start(f"Thu {name}  8 01:02:03 2026"),
                                 datetime(2026, month, 8, 1, 2, 3).timestamp())
        for value in ("", "not a date", "Sun Foo 18 12:34:56 2026",
                      "Sun Feb 30 12:34:56 2026", "Sun Oct 18 25:34:56 2026"):
            with self.subTest(value=value):
                self.assertIsNone(resource_scan._mac_start(value))
        try:
            with mock.patch.dict(os.environ, {"TZ": "UTC+07"}):
                time.tzset()
                self.assertEqual(resource_scan._mac_start("Sun Oct 18 12:34:56 2026"),
                                 datetime(2026, 10, 18, 12, 34, 56).timestamp())
        finally:
            time.tzset()

    def test_no_macos_listeners_is_not_an_error(self):
        runner = FixtureRunner({("lsof", "-nP", "-iTCP", "-sTCP:LISTEN", "-F", "pucn"): (1, "", "")})
        self.assertEqual(resource_scan.scan_listeners(runner=runner, platform="darwin"), [])

    def test_macos_discovery_error_is_not_mistaken_for_no_listeners(self):
        runner = self.mac_runner()
        discovery = next(key for key in runner.responses if "-iTCP" in key)
        runner.responses[discovery] = (1, "", "lsof: permission denied")
        with self.assertRaises(resource_scan.ResourceScanError):
            resource_scan.scan_listeners(runner=runner, platform="darwin")

    def test_discovery_failure_and_unsupported_platform_are_explicit(self):
        for error in (FileNotFoundError("ss"), subprocess.TimeoutExpired("ss", 2)):
            with self.subTest(error=error):
                runner = FixtureRunner({("ss", "-ltnpH"): error})
                with self.assertRaises(resource_scan.ResourceScanError):
                    resource_scan.scan_listeners(runner=runner, platform="linux")
        with self.assertRaises(resource_scan.ResourceScanError):
            resource_scan.scan_listeners(platform="win32")

    def test_subprocess_output_replaces_undecodable_bytes(self):
        output = resource_scan._command(subprocess.run, [sys.executable, "-c",
                                                        "import os; os.write(1, b'node\\xff\\n')"])
        self.assertEqual(output, "node\ufffd\n")


class ServeScanTest(unittest.TestCase):
    def test_serve_web_handlers_ports_paths_and_https_authorities(self):
        runner = FixtureRunner({("/usr/bin/tailscale", "serve", "status", "--json"): json.dumps(SERVE)})
        with mock.patch.object(resource_scan.shutil, "which", return_value="/usr/bin/tailscale"):
            routes = resource_scan.scan_serve(runner=runner, platform="linux", now=123)
        by_path = {route["path"]: route for route in routes}
        self.assertEqual(by_path["/"], {"url": "https://winmini.example.ts.net/", "port": 5180,
                                       "path": "/", "target": "http://127.0.0.1:5180", "kind": "proxy"})
        self.assertEqual(by_path["/review"]["url"], "https://winmini.example.ts.net:8477/review")
        self.assertEqual(by_path["/ipv6"]["port"], 5318)
        self.assertEqual(by_path["/files/"]["kind"], "path")
        self.assertIsNone(by_path["/files/"]["port"])
        self.assertIsNone(by_path["/remote"]["port"])
        self.assertLessEqual(runner.calls[0][1]["timeout"], 2)

    def test_macos_application_binary_fallback_and_path_precedence(self):
        for on_path, expected in ((None, resource_scan._MAC_TAILSCALE), ("/opt/homebrew/bin/tailscale", "/opt/homebrew/bin/tailscale")):
            with self.subTest(on_path=on_path):
                runner = FixtureRunner({(expected, "serve", "status", "--json"): json.dumps(SERVE)})
                with mock.patch.object(resource_scan.shutil, "which", return_value=on_path), \
                     mock.patch.object(Path, "is_file", return_value=True):
                    self.assertEqual(len(resource_scan.scan_serve(runner=runner, platform="darwin")), 6)

    def test_missing_binary_means_no_routes_on_either_platform(self):
        for platform in ("linux", "darwin"):
            with self.subTest(platform=platform), \
                 mock.patch.object(resource_scan.shutil, "which", return_value=None), \
                 mock.patch.object(Path, "is_file", return_value=False):
                runner = FixtureRunner({})
                self.assertEqual(resource_scan.scan_serve(runner=runner, platform=platform), [])
                self.assertEqual(runner.calls, [])

    def test_disabled_serve_and_malformed_or_failed_status(self):
        for text in ("{}", '{"TCP":{}}'):
            runner = FixtureRunner({("tailscale", "serve", "status", "--json"): text})
            with mock.patch.object(resource_scan.shutil, "which", return_value="tailscale"):
                self.assertEqual(resource_scan.scan_serve(runner=runner), [])
        for response in ("not json", '{"Web":[]}', (1, "", "backend unavailable"), subprocess.TimeoutExpired("tailscale", 2)):
            with self.subTest(response=response):
                runner = FixtureRunner({("tailscale", "serve", "status", "--json"): response})
                with mock.patch.object(resource_scan.shutil, "which", return_value="tailscale"), \
                     self.assertRaises(resource_scan.ResourceScanError):
                    resource_scan.scan_serve(runner=runner)

    def test_host_scan_preserves_other_source_and_combines_errors(self):
        runner = FixtureRunner({("ss", "-ltnpH"): FileNotFoundError("ss"),
                                ("tailscale", "serve", "status", "--json"): json.dumps(SERVE)})
        with mock.patch.object(resource_scan.shutil, "which", return_value="tailscale"):
            result = resource_scan.scan_host_resources(runner=runner, platform="linux")
        self.assertEqual(result["listeners"], [])
        self.assertEqual(len(result["serve"]), 6)
        self.assertIn("ss", result["error"])
        runner.responses[("tailscale", "serve", "status", "--json")] = (1, "", "offline")
        with mock.patch.object(resource_scan.shutil, "which", return_value="tailscale"):
            result = resource_scan.scan_host_resources(runner=runner, platform="linux")
        self.assertIn("ss", result["error"])
        self.assertIn("offline", result["error"])

    def test_host_scan_reports_unexpected_exceptions_and_keeps_the_other_source(self):
        decode_error = UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")
        for error in (decode_error, RuntimeError("unexpected scanner failure")):
            with self.subTest(error=error), \
                 mock.patch.object(resource_scan, "scan_listeners", side_effect=error), \
                 mock.patch.object(resource_scan, "scan_serve", return_value=[{"url": "/live"}]):
                result = resource_scan.scan_host_resources()
            self.assertEqual(result["listeners"], [])
            self.assertEqual(result["serve"], [{"url": "/live"}])
            self.assertIn("listeners:", result["error"])
            self.assertIn(str(error), result["error"])
        with mock.patch.object(resource_scan, "scan_listeners", return_value=[]), \
             mock.patch.object(resource_scan, "scan_serve", side_effect=RuntimeError("bad serve")):
            result = resource_scan.scan_host_resources()
        self.assertIn("serve: bad serve", result["error"])

    def test_host_scan_exposes_all_listener_endpoints_without_process_metadata(self):
        runner = FixtureRunner({("ss", "-ltnpH"): SS.splitlines()[2]})
        with mock.patch.object(resource_scan.shutil, "which", return_value=None), \
             mock.patch.object(Path, "read_text", return_value=""):
            result = resource_scan.scan_host_resources(runner=runner, platform="linux")
        self.assertEqual(result["listeners"], [])
        self.assertEqual(result["listener_endpoints"], [{"bind": "0.0.0.0", "port": 22}])
        self.assertIsNone(result["error"])

    def test_host_scan_does_not_swallow_process_interrupts(self):
        with mock.patch.object(resource_scan, "scan_listeners", side_effect=KeyboardInterrupt), \
             self.assertRaises(KeyboardInterrupt):
            resource_scan.scan_host_resources()


class AttributionTest(unittest.TestCase):
    def agent(self, agent_id="agent", **fields):
        return {"agent_id": agent_id, "pane_id": "wA:p1", "host": "winmini", "active": True,
                "client": "omp", "first_seen": 200, **fields}

    def listener(self, port=5180, **fields):
        return {"pid": port + 100, "port": port, "bind": "127.0.0.1", "process": "node",
                "argv": "node vite.js", "cwd": "/home/test/review", "pane_id": "wA:p1",
                "plugin_id": None, "started_at": 220, **fields}

    def route(self, backend_port=5180, **fields):
        return {"url": f"https://winmini.example.ts.net:{backend_port}/", "port": backend_port, "path": "/",
                "target": f"http://127.0.0.1:{backend_port}", "kind": "proxy", **fields}

    def test_active_local_agent_gets_servers_urls_and_inherited_flag(self):
        listeners = [self.listener(5292, started_at=100), self.listener(), self.listener(5173, started_at=None)]
        routes = [self.route(), self.route(url="https://winmini.example.ts.net/second"), self.route(5292)]
        original = json.dumps([listeners, routes], sort_keys=True)
        per_agent, host = resource_scan.attribute(listeners, routes, [self.agent()], host="winmini", now=999)
        servers = per_agent["agent"]
        self.assertEqual([server["port"] for server in servers], [5173, 5180, 5292])
        self.assertEqual([server["inherited"] for server in servers], [False, False, True])
        self.assertEqual(servers[1]["urls"], sorted([routes[0]["url"], routes[1]["url"]]))
        self.assertEqual(set(servers[1]), {"port", "bind", "pid", "process", "cwd", "started_at", "urls", "inherited"})
        self.assertEqual(host, {"unowned_served": [], "dead_serve": []})
        self.assertEqual(json.dumps([listeners, routes], sort_keys=True), original)

    def test_missing_pane_inactive_and_other_host_never_own_a_listener(self):
        for listener, agents in ((self.listener(pane_id=None), [self.agent()]),
                                 (self.listener(pane_id="wA:p9"), [self.agent()]),
                                 (self.listener(), [self.agent(active=False)]),
                                 (self.listener(), [self.agent(host="macstudio")])):
            with self.subTest(listener=listener, agents=agents):
                per_agent, host = resource_scan.attribute([listener], [self.route()], agents, host="winmini")
                self.assertEqual(per_agent, {})
                self.assertEqual(host, {"unowned_served": [self.route()], "dead_serve": []})

    def test_plugin_adb_and_runtime_processes_are_excluded_but_not_dead(self):
        exclusions = [{"plugin_id": "herdr-web-ui"}, {"plugin_id": ""}, {"process": "adb"}, {"argv": "/opt/bin/adb server"},
                      {"process": "omp"}, {"process": "/opt/bin/claude"}, {"argv": "/opt/bin/codex exec"},
                      {"process": "custom-agent"}, {"argv": "/opt/bin/custom-agent --serve"}]
        for fields in exclusions:
            with self.subTest(fields=fields):
                per_agent, host = resource_scan.attribute([self.listener(**fields)], [self.route()],
                                                         [self.agent(client="custom-agent")], host="winmini")
                self.assertEqual(per_agent, {})
                self.assertEqual(host, {"unowned_served": [], "dead_serve": []})
        per_agent, _ = resource_scan.attribute([self.listener(process="omp-preview", argv="node omp-preview.js")],
                                              [], [self.agent()], host="winmini")
        self.assertEqual(len(per_agent["agent"]), 1)

    def test_dead_serve_only_for_missing_loopback_backend_not_static_or_remote(self):
        routes = [self.route(5288), self.route(9999, port=None, target="/home/test/files", kind="path"),
                  self.route(8888, port=None, target="http://192.168.0.50:8888")]
        _, host = resource_scan.attribute([], routes, [self.agent()], host="winmini")
        self.assertEqual(host, {"unowned_served": [], "dead_serve": [routes[0]]})

    def test_bind_matching_wildcards_localhost_and_non_loopback_port_collision(self):
        for bind, target, alive in (("0.0.0.0", "http://127.0.0.1:5180", True),
                                    ("::", "http://[::1]:5180", True),
                                    ("*", "http://127.0.0.1:5180", True),
                                    ("::1", "http://localhost:5180", True),
                                    ("192.168.0.20", "http://127.0.0.1:5180", False)):
            with self.subTest(bind=bind, target=target):
                route = self.route(target=target)
                per_agent, host = resource_scan.attribute([self.listener(bind=bind)], [route], [self.agent()], host="winmini")
                self.assertEqual(bool(per_agent["agent"][0]["urls"]), alive)
                self.assertEqual(host["dead_serve"], [] if alive else [route])

    def test_deduplicated_server_retains_every_bind_for_both_backend_families(self):
        routes = [self.route(), self.route(target="http://[::1]:5180", url="/ipv6")]
        for binds in (["127.0.0.1", "::1"], ["0.0.0.0", "::"]):
            for ordered in (binds, list(reversed(binds))):
                with self.subTest(binds=ordered):
                    listeners = resource_scan._deduplicate([self.listener(bind=bind) for bind in ordered + ordered])
                    self.assertEqual(len(listeners), 1)
                    self.assertEqual(set(listeners[0]["binds"]), set(binds))
                    per_agent, host = resource_scan.attribute(listeners, routes, [self.agent()], host="winmini")
                    self.assertEqual(len(per_agent["agent"]), 1)
                    self.assertEqual(per_agent["agent"][0]["urls"], sorted(route["url"] for route in routes))
                    self.assertEqual(host, {"unowned_served": [], "dead_serve": []})

    def test_all_user_liveness_requires_matching_bind_not_just_matching_port(self):
        route = self.route()
        for bind, dead in (("0.0.0.0", False), ("127.0.0.1", False), ("192.168.0.20", True)):
            with self.subTest(bind=bind):
                per_agent, host = resource_scan.attribute([], [route], [self.agent()], host="winmini",
                                                         listener_endpoints=[{"bind": bind, "port": 5180}])
                self.assertEqual(per_agent, {})
                self.assertEqual(host["dead_serve"], [route] if dead else [])
                self.assertEqual(host["unowned_served"], [])

    def test_current_session_wins_same_pane_and_default_host_is_local(self):
        agents = [self.agent("new", first_seen=300), self.agent("old", first_seen=100),
                  self.agent("foreign", host="macstudio", first_seen=900)]
        with mock.patch.object(resource_scan.socket, "gethostname", return_value="winmini"):
            per_agent, _ = resource_scan.attribute([self.listener()], [], agents)
        self.assertEqual(set(per_agent), {"new"})
        self.assertTrue(per_agent["new"][0]["inherited"])


if __name__ == "__main__":
    unittest.main()
