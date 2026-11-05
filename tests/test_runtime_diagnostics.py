"""Native receiver deadlines and fail-closed caller diagnostics on private state."""
import tests.ham_test_support as ham_test_support  # Protect the live ledger before product imports.

from contextlib import redirect_stderr, redirect_stdout
import fcntl
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from ham import cli as public_cli
from ham.ledger import status as ledger_status
from ham.ledger import store as ledger_store
from ham.ledger.status import AgentRegistry
from ham.ledger.store import LedgerError, LedgerStore, budget_timeout, runtime_budget
from tests.test_ledger_core import snapshot


ROOT = Path(__file__).resolve().parents[1]


class RuntimeDiagnosticTest(unittest.TestCase):
    def setUp(self):
        helper = Path.home() / ".config/herdr/scripts/agent_work.py"
        if helper.is_file():
            work = Path(subprocess.run(
                [sys.executable, str(helper), "path", "--checkout", str(ROOT),
                 "--name", "runtime-diagnostic-tests"],
                check=True, capture_output=True, text=True).stdout.strip())
        else:
            work = Path(os.environ.get("AGENT_WORK_ROOT", Path.home() / ".cache/agent-work")) / "runtime-diagnostic-tests"
            work.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=work)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        for name in ("home", "repo", "tmp"):
            (self.root / name).mkdir()
        self.environment = {
            **os.environ,
            "HOME": str(self.root / "home"),
            "XDG_CONFIG_HOME": str(self.root / "home/.config"),
            "XDG_STATE_HOME": str(self.root / "home/.local/state"),
            "XDG_CACHE_HOME": str(self.root / "home/.cache"),
            "TMPDIR": str(self.root / "tmp"),
            "HERDR_AGENT_LEDGER_DIR": str(self.root / "ledger"),
            "HERDR_AGENT_REQUEST_STATE_DIR": str(self.root / "requests"),
            "HERDR_AGENT_LEDGER_AUTOSTART": "0",
            "HERDR_SOCKET_PATH": str(self.root / "absent-herdr.sock"),
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        self.environment.pop("HERDR_TERMINAL_ID", None)
        self.environment.pop("HERDR_PANE_ID", None)
        self.store = LedgerStore(environment=self.environment, canonical_name="fixture")
        self.registry = AgentRegistry(self.store)
        self.snapshot = snapshot(self.root / "repo")
        self.registry.reconcile(self.snapshot, refresh_display=False, environment=self.environment)

    def child_cli(self, operation, *, seconds=0.3):
        # Only shorten the work budget; exercise real parsing, bootstrap, SQLite,
        # flock acquisition and public JSON serialization in an isolated child.
        program = (
            "import tests.ham_test_support\n"
            "import sys\n"
            "from ham.ledger import cli, store\n"
            "seconds = float(sys.argv[1])\n"
            "cli.runtime_budget = lambda: store.runtime_budget(seconds)\n"
            "from ham.__main__ import main\n"
            "raise SystemExit(main(sys.argv[2:]))\n"
        )
        started = time.monotonic()
        result = subprocess.run(
            [sys.executable, "-c", program, str(seconds), "cli", "ledger", "runtime", operation],
            cwd=ROOT, env=self.environment, input="{}", capture_output=True, text=True, timeout=5)
        elapsed = time.monotonic() - started
        self.assertEqual(result.stderr, "", result.stderr)
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertLess(elapsed, 3, result.stdout)
        return json.loads(result.stdout)

    def assert_lookup_error(self, error, lookup, reason):
        self.assertEqual(error.code, "sender_lookup_failed")
        self.assertEqual(error.details["lookup"], lookup)
        self.assertEqual(error.details["reason"], reason)
        self.assertIsInstance(error.details["elapsed_ms"], int)
        self.assertGreaterEqual(error.details["elapsed_ms"], 0)

    @staticmethod
    def ps_result(stdout=None):
        ancestry = f"{os.getpid()} 10\n10 1\n" if stdout is None else stdout
        return subprocess.CompletedProcess(["ps"], 0, ancestry, "")

    @staticmethod
    def pane_result(pane="w1:p1", pid=10):
        return subprocess.CompletedProcess(
            ["herdr", "pane", "process-info"], 0,
            json.dumps({"result": {"process_info": {"pane_id": pane, "shell_pid": pid}}}), "")

    def test_register_and_inspect_emit_bounded_ledger_busy_json_under_sqlite_contention(self):
        blocker = sqlite3.connect(self.store.path, isolation_level=None)
        self.addCleanup(blocker.close)
        blocker.execute("BEGIN IMMEDIATE")
        try:
            for operation in ("register", "inspect"):
                with self.subTest(operation=operation):
                    payload = self.child_cli(operation)
                    self.assertEqual(payload["error"]["code"], "ledger_busy")
        finally:
            blocker.rollback()

    def test_register_and_inspect_budget_includes_bootstrap_flock(self):
        with (self.store.directory / "bootstrap.lock").open("r+b") as blocker:
            fcntl.flock(blocker, fcntl.LOCK_EX)
            try:
                for operation in ("register", "inspect"):
                    with self.subTest(operation=operation):
                        self.assertEqual(self.child_cli(operation)["error"]["code"], "ledger_busy")
            finally:
                fcntl.flock(blocker, fcntl.LOCK_UN)

    def test_register_budget_includes_registry_flock_before_snapshot(self):
        with (self.store.directory / "registry.lock").open("a+b") as blocker:
            fcntl.flock(blocker, fcntl.LOCK_EX)
            try:
                self.assertEqual(self.child_cli("register")["error"]["code"], "ledger_busy")
            finally:
                fcntl.flock(blocker, fcntl.LOCK_UN)

    def test_default_shared_deadline_is_twelve_seconds_and_resets_after_error(self):
        clock = [100.0]
        with mock.patch.object(ledger_store.time, "monotonic", side_effect=lambda: clock[0]):
            self.assertEqual(budget_timeout(30), 30)
            with self.assertRaises(LedgerError) as caught:
                with runtime_budget():
                    self.assertEqual(budget_timeout(30), 12)
                    self.assertEqual(budget_timeout(2), 2)
                    clock[0] += 11.75
                    self.assertEqual(budget_timeout(5), 0.25)
                    clock[0] += 0.25
                    budget_timeout(5)
            self.assertEqual(caught.exception.code, "runtime_timeout")
            self.assertEqual(budget_timeout(30), 30)

    def test_sqlite_nonruntime_timeout_stays_thirty_seconds(self):
        with self.store.read() as connection:
            self.assertEqual(connection.execute("PRAGMA busy_timeout").fetchone()[0], 30000)
        environment = {**self.environment, "HERDR_AGENT_LEDGER_DIR": str(self.store.directory)}
        readonly = LedgerStore.open_readonly(environment)
        self.assertIsNotNone(readonly)
        with readonly.read() as connection:
            self.assertEqual(connection.execute("PRAGMA busy_timeout").fetchone()[0], 30000)

    def test_runtime_sqlite_timeout_refreshes_on_every_operation(self):
        clock = [100.0]
        with mock.patch.object(ledger_store.time, "monotonic", side_effect=lambda: clock[0]):
            with runtime_budget():
                with self.store.read() as connection:
                    # Bypass the override when observing PRAGMA so the assertion
                    # cannot accidentally refresh the very timeout being tested.
                    def timeout_ms():
                        return sqlite3.Connection.execute(connection, "PRAGMA busy_timeout").fetchone()[0]

                    self.assertLessEqual(timeout_ms(), 2000)
                    self.assertGreater(timeout_ms(), 1900)
                    connection.execute("CREATE TEMP TABLE budget_probe(value INTEGER)")
                    for elapsed, operation in (
                        (10.5, lambda: connection.execute("SELECT 1")),
                        (11.0, lambda: connection.executemany("INSERT INTO budget_probe VALUES (?)", [(1,), (2,)])),
                        (11.5, lambda: connection.executescript("SELECT 1; SELECT 2;")),
                        (11.75, connection.commit),
                    ):
                        with self.subTest(elapsed=elapsed):
                            clock[0] = 100 + elapsed
                            operation()
                            self.assertLessEqual(timeout_ms(), int((12 - elapsed) * 1000))
                            self.assertGreater(timeout_ms(), 0)

    def test_sqlite_progress_handler_interrupts_work_at_the_shared_deadline(self):
        clock = [100.0]
        with mock.patch.object(ledger_store.time, "monotonic", side_effect=lambda: clock[0]):
            with runtime_budget():
                with self.store.read() as connection:
                    def expire():
                        clock[0] = 112.0
                        return 1

                    connection.create_function("expire_budget", 0, expire)
                    with self.assertRaises(sqlite3.OperationalError) as caught:
                        connection.execute(
                            "WITH RECURSIVE n(x) AS (VALUES(1) UNION ALL SELECT x+1 FROM n WHERE x<10000) "
                            "SELECT sum(x + expire_budget()) FROM n").fetchone()
                    self.assertEqual(str(caught.exception), "interrupted")
                    if hasattr(caught.exception, "sqlite_errorcode"):
                        self.assertEqual(caught.exception.sqlite_errorcode & 0xff, 9)

    def test_sqlite_errors_without_python311_code_attributes_keep_json_codes(self):
        for message, expected in (
            ("database is locked", "ledger_busy"),
            ("database table is locked: agents", "ledger_busy"),
            ("database schema is locked: main", "ledger_busy"),
            ("interrupted", "runtime_timeout"),
            ("no such table: missing", "internal_error"),
        ):
            with self.subTest(message=message), \
                    mock.patch("ham.ledger.cli._dispatch_command", side_effect=sqlite3.OperationalError(message)), \
                    redirect_stdout(io.StringIO()) as output:
                self.assertEqual(public_cli.main(["ledger", "runtime", "inspect"], self.environment), 1)
                self.assertEqual(json.loads(output.getvalue())["error"]["code"], expected)

    def test_git_and_snapshot_timeouts_do_not_mask_shared_deadline_expiry(self):
        for probe, limit in (
            (lambda: ledger_status._git(str(self.root), "rev-parse", "--show-toplevel"), 5),
            (lambda: ledger_status.live_snapshot(self.environment), 10),
        ):
            clock = [100.0]

            def expire(*args, **kwargs):
                self.assertEqual(kwargs["timeout"], 0.5)
                clock[0] = 100.5
                raise subprocess.TimeoutExpired(args[0], kwargs["timeout"])

            with self.subTest(limit=limit), \
                    mock.patch.object(ledger_store.time, "monotonic", side_effect=lambda: clock[0]), \
                    mock.patch.object(ledger_status.subprocess, "run", side_effect=expire), \
                    runtime_budget(0.5):
                with self.assertRaises(LedgerError) as caught:
                    probe()
                self.assertEqual(caught.exception.code, "runtime_timeout")

    def test_flock_wait_is_capped_at_two_seconds_without_sleeping_in_real_time(self):
        clock = [100.0]
        waits = []

        def sleep(seconds):
            waits.append(seconds)
            clock[0] += seconds

        with mock.patch.object(ledger_store.time, "monotonic", side_effect=lambda: clock[0]), \
                mock.patch.object(ledger_store.time, "sleep", side_effect=sleep), \
                mock.patch.object(ledger_store.fcntl, "flock", side_effect=BlockingIOError):
            with runtime_budget():
                with self.assertRaises(LedgerError) as caught:
                    ledger_store._acquire_lock(123)
        self.assertEqual(caught.exception.code, "ledger_busy")
        self.assertAlmostEqual(sum(waits), 2)
        self.assertTrue(all(0 < seconds <= 0.02 for seconds in waits))

    def test_ps_lookup_failures_report_evidence_not_a_proven_mismatch(self):
        cases = (
            (subprocess.TimeoutExpired(["ps"], 5), "timeout"),
            (FileNotFoundError("private ps fixture"), "spawn_failed"),
            (subprocess.CompletedProcess(["ps"], 7, "", "denied"), "nonzero_exit"),
            (self.ps_result("not process output\n"), "invalid_output"),
            (self.ps_result(""), "invalid_output"),
            (self.ps_result("10 1\n"), "invalid_output"),
            (self.ps_result(f"{os.getpid()} 10\n"), "invalid_output"),
            (self.ps_result(f"{os.getpid()} 10\n10 {os.getpid()}\n"), "invalid_output"),
            (self.ps_result(f"{os.getpid()} 10\n10 1\n10 1\n"), "invalid_output"),
        )
        for result, reason in cases:
            with self.subTest(reason=reason, result=result):
                options = {"side_effect": result} if isinstance(result, Exception) else {"return_value": result}
                with mock.patch.object(ledger_status.subprocess, "run", **options):
                    with self.assertRaises(LedgerError) as caught:
                        ledger_status.process_ancestors()
                self.assert_lookup_error(caught.exception, "ps", reason)

    def test_complete_ps_ancestry_is_still_verified(self):
        with mock.patch.object(ledger_status.subprocess, "run", return_value=self.ps_result()):
            self.assertEqual(ledger_status.process_ancestors(), {os.getpid(), 10})

    def test_process_info_failures_report_structured_diagnostics(self):
        cases = (
            (subprocess.TimeoutExpired(["herdr"], 5), "timeout"),
            (FileNotFoundError("private herdr fixture"), "spawn_failed"),
            (subprocess.CompletedProcess(["herdr"], 2, "", "unavailable"), "nonzero_exit"),
            (subprocess.CompletedProcess(["herdr"], 0, "not json", ""), "invalid_output"),
            (subprocess.CompletedProcess(["herdr"], 0, "{}", ""), "invalid_output"),
            (self.pane_result(pane="wrong-pane"), "invalid_output"),
            (self.pane_result(pid=True), "invalid_output"),
            (self.pane_result(pid=0), "invalid_output"),
            (self.pane_result(pid=-1), "invalid_output"),
            (self.pane_result(pid=1.5), "invalid_output"),
            (self.pane_result(pid="unknown"), "invalid_output"),
        )
        for result, reason in cases:
            with self.subTest(reason=reason, result=result):
                options = {"side_effect": result} if isinstance(result, Exception) else {"return_value": result}
                with mock.patch.object(ledger_status.subprocess, "run", **options):
                    with self.assertRaises(LedgerError) as caught:
                        ledger_status.pane_shell_pid("w1:p1", self.environment)
                self.assert_lookup_error(caught.exception, "process-info", reason)

    def test_true_terminal_mismatch_remains_sender_unresolved(self):
        for claimed in (False, True):
            with self.subTest(claimed=claimed):
                environment = {**self.environment, **({"HERDR_TERMINAL_ID": "term-a"} if claimed else {})}
                responses = [self.ps_result(), self.pane_result(pid=999999)]
                if not claimed:
                    responses.append(self.pane_result(pane="w1:p2", pid=999998))
                with mock.patch.object(ledger_status.subprocess, "run", side_effect=responses):
                    with self.assertRaises(LedgerError) as caught:
                        self.registry.resolve_context(environment, self.snapshot)
                self.assertEqual(caught.exception.code, "sender_unresolved")

    def test_unrelated_failed_probe_does_not_hide_a_genuinely_verified_match(self):
        with mock.patch.object(ledger_status.subprocess, "run", side_effect=[
                self.ps_result(), subprocess.TimeoutExpired(["herdr"], 5),
                self.pane_result(pane="w1:p2")]) as runner:
            resolved = self.registry.resolve_context(self.environment, self.snapshot)
        self.assertEqual(resolved["terminal_id"], "term-b")
        self.assertEqual(runner.call_count, 3)

    def test_discovery_fails_closed_if_no_match_and_any_probe_lacks_evidence(self):
        with mock.patch.object(ledger_status.subprocess, "run", side_effect=[
                self.ps_result(), subprocess.TimeoutExpired(["herdr"], 5),
                self.pane_result(pane="w1:p2", pid=999999)]):
            with self.assertRaises(LedgerError) as caught:
                self.registry.resolve_context(self.environment, self.snapshot)
        self.assert_lookup_error(caught.exception, "process-info", "timeout")

    def test_multiple_probes_consume_one_deadline_instead_of_resetting_it(self):
        clock = [100.0]
        timeouts = []

        def run(command, **options):
            timeouts.append(options["timeout"])
            clock[0] += 4
            if command[0] == "ps":
                return self.ps_result()
            if command[-1] == "w1:p1":
                return self.pane_result(pid=999999)
            raise subprocess.TimeoutExpired(command, options["timeout"])

        with mock.patch.object(ledger_store.time, "monotonic", side_effect=lambda: clock[0]), \
                mock.patch.object(ledger_status.subprocess, "run", side_effect=run):
            with runtime_budget():
                with self.assertRaises(LedgerError) as caught:
                    self.registry.resolve_context(self.environment, self.snapshot)
                self.assert_lookup_error(caught.exception, "process-info", "timeout")
                with self.assertRaises(LedgerError) as expired:
                    budget_timeout(5)
        self.assertEqual(timeouts, [5, 5, 4])
        self.assertEqual(expired.exception.code, "runtime_timeout")

    def test_public_register_cli_serializes_sender_lookup_details(self):
        cases = (
            ([subprocess.TimeoutExpired(["ps"], 5)], "ps", "timeout"),
            ([self.ps_result("10 1\n")], "ps", "invalid_output"),
            ([self.ps_result(), subprocess.CompletedProcess(["herdr"], 8, "", "failed")],
             "process-info", "nonzero_exit"),
            ([self.ps_result(), subprocess.TimeoutExpired(["herdr"], 5)], "process-info", "timeout"),
            ([self.ps_result(), self.pane_result(pid=True)], "process-info", "invalid_output"),
        )
        for responses, lookup, reason in cases:
            with self.subTest(lookup=lookup, reason=reason):
                output, errors = io.StringIO(), io.StringIO()
                with mock.patch.object(AgentRegistry, "refresh", return_value=(self.snapshot, {})), \
                        mock.patch.object(ledger_status.subprocess, "run", side_effect=responses), \
                        mock.patch("sys.stdin", io.StringIO("{}")), \
                        redirect_stdout(output), redirect_stderr(errors):
                    result = public_cli.main(
                        ["ledger", "runtime", "register"],
                        environment={**self.environment, "HERDR_TERMINAL_ID": "term-a"})
                self.assertEqual(result, 1)
                self.assertEqual(errors.getvalue(), "")
                diagnostic = json.loads(output.getvalue())["error"]
                self.assertEqual(diagnostic["code"], "sender_lookup_failed")
                self.assertEqual((diagnostic["lookup"], diagnostic["reason"]), (lookup, reason))
                self.assertIsInstance(diagnostic["elapsed_ms"], int)
                self.assertGreaterEqual(diagnostic["elapsed_ms"], 0)


if __name__ == "__main__":
    unittest.main()
