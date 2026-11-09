"""Caller identity is verified per PID before ancestry consumers trust it."""
import tests.ham_test_support as ham_test_support  # Protect live state before product imports.

from collections import Counter
from dataclasses import replace
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from ham import processes
from ham.ledger import status
from ham.ledger.store import LedgerError
from ham.processes import ProcessInfo, ProcessLookupFailure, ProcessSource, process_chain


CALLER = 9000
CLAUDE = 4242
SHELL = 3000


def stable_records():
    return {
        CALLER: ProcessInfo(CALLER, CLAUDE, "python", (50,)),
        CLAUDE: ProcessInfo(CLAUDE, SHELL, "/opt/Claude Code/claude", (40,)),
        SHELL: ProcessInfo(SHELL, 1, "shell", (30,)),
    }


class ScriptedSource:
    lookup = "procfs"

    def __init__(self, records):
        self.records = {pid: list(value) if isinstance(value, list) else value
                        for pid, value in records.items()}
        self.reads = Counter()

    def read(self, pid):
        self.reads[pid] += 1
        value = self.records.get(pid, ProcessLookupFailure("exited"))
        if isinstance(value, list):
            value = value.pop(0) if len(value) > 1 else value[0]
        if isinstance(value, Exception):
            raise value
        return value

    def pids(self):
        raise AssertionError("Caller verification must not enumerate unrelated PIDs")


class ProcessChainTest(unittest.TestCase):
    def assert_consumers_reject(self, records, reason):
        # Each consumer gets a fresh observation, including any scripted race.
        for consumer in (status.process_ancestors, status._claude_ancestor_pid):
            source = ScriptedSource(records)
            with self.subTest(consumer=consumer.__name__), \
                    mock.patch.object(processes.os, "getpid", return_value=CALLER), \
                    mock.patch.object(processes, "ProcessSource", return_value=source), \
                    mock.patch.object(status, "ProcessSource", return_value=source):
                if consumer is status._claude_ancestor_pid:
                    self.assertIsNone(consumer())
                else:
                    with self.assertRaises(LedgerError) as caught:
                        consumer()
                    self.assertEqual(caught.exception.code, "sender_lookup_failed")
                    self.assertEqual(caught.exception.details["lookup"], source.lookup)
                    self.assertEqual(caught.exception.details["reason"], reason)

    def test_verified_chain_is_caller_first_and_reads_only_its_ancestors(self):
        records = stable_records()
        records[7777] = ProcessInfo(7777, SHELL, "claude", (45,))
        source = ScriptedSource(records)
        with mock.patch.object(processes.os, "getpid", return_value=CALLER):
            chain = process_chain(source=source)
        self.assertEqual(chain, [records[CALLER], records[CLAUDE], records[SHELL]])
        self.assertEqual(set(source.reads), {CALLER, CLAUDE, SHELL})
        self.assertTrue(all(count >= 2 for count in source.reads.values()))

    def test_shared_shell_does_not_admit_a_sibling_claude_as_callers_ancestor(self):
        records = stable_records()
        records[CALLER] = replace(records[CALLER], parent=SHELL)
        source = ScriptedSource(records)
        with mock.patch.object(processes.os, "getpid", return_value=CALLER), \
                mock.patch.object(processes, "ProcessSource", return_value=source), \
                mock.patch.object(status, "ProcessSource", return_value=source):
            self.assertEqual(status.process_ancestors(), {CALLER, SHELL})
            self.assertIsNone(status._claude_ancestor_pid())
        self.assertNotIn(CLAUDE, source.reads)

    def test_missing_parent_fails_closed_even_after_claude_was_seen(self):
        records = stable_records()
        del records[SHELL]
        self.assert_consumers_reject(records, "exited")

    def test_cycles_and_parent_created_after_child_fail_closed(self):
        for changed in (
            replace(stable_records()[SHELL], parent=CALLER),
            replace(stable_records()[SHELL], parent=SHELL),
            replace(stable_records()[SHELL], started=(60,)),
        ):
            with self.subTest(changed=changed):
                records = stable_records()
                records[SHELL] = changed
                self.assert_consumers_reject(records, "invalid_output")

    def test_invalid_source_identity_fields_are_never_trusted(self):
        valid = stable_records()[CLAUDE]
        invalid = [None, {}, replace(valid, pid=99), replace(valid, pid=True),
                   replace(valid, pid=str(CLAUDE))]
        for parent in (0, -1, True, 1.5, "1", None):
            invalid.append(replace(valid, parent=parent))
        for name in ("", None, 42):
            invalid.append(replace(valid, name=name))
        for started in ((), [], (True,), (-1,), ("40",), None):
            invalid.append(replace(valid, started=started))
        for value in invalid:
            with self.subTest(value=value):
                records = stable_records()
                records[CLAUDE] = value
                self.assert_consumers_reject(records, "invalid_output")

    def test_pid_mismatch_in_the_callers_own_record_fails_closed(self):
        records = stable_records()
        records[CALLER] = replace(records[CALLER], pid=CALLER + 1)
        self.assert_consumers_reject(records, "invalid_output")

    def test_reparent_reuse_and_command_change_during_recheck_fail_closed(self):
        for pid in (CALLER, CLAUDE, SHELL):
            original = stable_records()[pid]
            for changed in (replace(original, parent=1 if original.parent != 1 else 2),
                            replace(original, started=(original.started[0] + 1,)),
                            replace(original, name="replacement")):
                with self.subTest(pid=pid, changed=changed):
                    records = stable_records()
                    records[pid] = [original, changed]
                    self.assert_consumers_reject(records, "changed")

    def test_exit_or_unavailable_at_any_initial_or_recheck_read_fails_closed(self):
        for pid in (CALLER, CLAUDE, SHELL):
            for reason in ("exited", "unavailable"):
                for during_recheck in (False, True):
                    with self.subTest(pid=pid, reason=reason, during_recheck=during_recheck):
                        records = stable_records()
                        failure = ProcessLookupFailure(reason)
                        records[pid] = [records[pid], failure] if during_recheck else failure
                        self.assert_consumers_reject(records, reason)

    def test_deadline_is_shared_by_initial_walk_and_recheck(self):
        for expire_after in (1, 3):
            with self.subTest(expire_after=expire_after):
                source = ScriptedSource(stable_records())
                read = source.read
                clock = [100.0]

                def advance(pid):
                    result = read(pid)
                    if sum(source.reads.values()) == expire_after:
                        clock[0] += 10
                    return result

                with mock.patch.object(processes.os, "getpid", return_value=CALLER), \
                        mock.patch.object(processes.time, "monotonic", side_effect=lambda: clock[0]), \
                        mock.patch.object(source, "read", side_effect=advance):
                    with self.assertRaises(ProcessLookupFailure) as caught:
                        process_chain(source=source, timeout=1)
                self.assertEqual(caught.exception.reason, "timeout")


class LinuxProcessSourceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        helper = Path.home() / ".config/herdr/scripts/agent_work.py"
        if helper.is_file():
            cls.work = Path(subprocess.check_output([
                sys.executable, str(helper), "path", "--checkout",
                str(Path(__file__).resolve().parents[1]), "--name", "process-source-tests",
            ], text=True).strip())
        else:
            cls.work = Path(os.environ.get("AGENT_WORK_ROOT", Path.home() / ".cache/agent-work")) / "process-source-tests"
            cls.work.mkdir(parents=True, exist_ok=True)

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=self.work)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = ProcessSource(platform="linux", proc_root=self.root)

    def write_stat(self, pid=CALLER, *, parent=CLAUDE, name="python", started=50, actual=None):
        directory = self.root / str(pid)
        directory.mkdir(exist_ok=True)
        fields = ["S", str(parent)] + ["0"] * 17 + [str(started)]
        raw = f"{pid if actual is None else actual} ({name}) " + " ".join(fields) + "\n"
        (directory / "stat").write_text(raw)
        return directory / "stat"

    def assert_failure(self, reason, pid=CALLER):
        with self.assertRaises(ProcessLookupFailure) as caught:
            self.source.read(pid)
        self.assertEqual(caught.exception.reason, reason)

    def test_proc_stat_preserves_spaces_and_parentheses_in_command_name(self):
        name = "/opt/Claude Code/(worker) claude"
        self.write_stat(name=name)
        self.assertEqual(self.source.read(CALLER), ProcessInfo(CALLER, CLAUDE, name, (50,)))

    def test_missing_pid_and_unreadable_stat_keep_distinct_failure_reasons(self):
        self.assert_failure("exited")
        self.write_stat()
        with mock.patch.object(Path, "read_text", side_effect=PermissionError("private proc fixture")):
            self.assert_failure("unavailable")

    def test_proc_source_drives_both_identity_consumers_without_a_process_table(self):
        records = stable_records()
        for record in records.values():
            self.write_stat(record.pid, parent=record.parent, name=record.name, started=record.started[0])
        with mock.patch.object(processes.os, "getpid", return_value=CALLER), \
                mock.patch.object(processes, "ProcessSource", return_value=self.source), \
                mock.patch.object(status, "ProcessSource", return_value=self.source):
            self.assertEqual(status.process_ancestors(), set(records))
            self.assertEqual(status._claude_ancestor_pid(), CLAUDE)

    def test_malformed_or_inconsistent_proc_stat_fails_closed(self):
        for options in ({"actual": CALLER + 1}, {"parent": -1}, {"parent": "unknown"},
                        {"name": ""}, {"started": -1}, {"started": "unknown"}):
            with self.subTest(options=options):
                self.write_stat(**options)
                self.assert_failure("invalid_output")
        path = self.write_stat()
        for raw in ("", "not a stat record", f"{CALLER} (python) S {CLAUDE}\n"):
            with self.subTest(raw=raw):
                path.write_text(raw)
                self.assert_failure("invalid_output")

    @unittest.skipUnless(sys.platform.startswith("linux"), "Requires real Linux procfs")
    def test_real_caller_ancestry_needs_no_subprocess_or_retained_file_descriptors(self):
        fd_root = Path("/proc/self/fd")
        before = {path.name for path in fd_root.iterdir()}
        with mock.patch.object(subprocess, "Popen", side_effect=AssertionError("Unexpected process spawn")):
            for _ in range(3):
                chain = process_chain()
                self.assertTrue(chain)
                self.assertEqual(chain[0].pid, os.getpid())
                self.assertEqual(chain[0].parent, os.getppid())
                self.assertEqual(len({info.pid for info in chain}), len(chain))
                for child, parent in zip(chain, chain[1:]):
                    self.assertEqual(child.parent, parent.pid)
                    self.assertLessEqual(parent.started, child.started)
                self.assertEqual(chain[-1].parent, 1)
        self.assertEqual({path.name for path in fd_root.iterdir()}, before)


if __name__ == "__main__":
    unittest.main()
