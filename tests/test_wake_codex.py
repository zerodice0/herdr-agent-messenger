"""Codex wake adapter: discovery, `codex queue` failure semantics and rollout evidence."""
import tests.ham_test_support as ham_test_support  # Protect the live ledger before product imports.
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest
from unittest import mock

from ham.ledger import wake_codex
from ham.ledger.wake_clients import Observation, WakeDeliveryFailed, WakeTarget, WakeUnavailable

THREAD = "01a16438-9da0-7702-af99-e82262b971d8"
OTHER_THREAD = "01a16438-aaaa-7702-af99-e82262b971d9"
PANE = "wA:p1"
TERMINAL = "term_1"
AGENT = {"client": "codex", "pane_id": PANE, "terminal_id": TERMINAL}
MARKER = "HAM_CLAIM_7_0123456789abcdef"
CONTENT = MARKER + "\nFrom: someone\n--- bundle ---\nbody \"quoted\" \\ 한글"
SHA = hashlib.sha256(CONTENT.encode()).hexdigest()


def process(pid, name="codex", argv=None):
    return {"pid": pid, "name": name, "argv": argv if argv is not None else [name], "cmdline": name}


def fake_query(processes, *, pane_id=PANE, terminal_id=TERMINAL, group=4242):
    def query(*arguments):
        if arguments[:2] == ("pane", "get"):
            return {"pane": {"pane_id": pane_id, "terminal_id": terminal_id}}
        if arguments[:2] == ("pane", "process-info"):
            return {"process_info": {"pane_id": pane_id, "foreground_process_group_id": group,
                                     "foreground_processes": processes}}
        raise AssertionError(arguments)
    return query


def meta_line(thread=THREAD, **extra):
    payload = {"session_id": thread, "id": thread, "source": "cli", "originator": "codex-tui", **extra}
    return json.dumps({"timestamp": "t", "ordinal": 0, "type": "session_meta", "payload": payload}) + "\n"


def user_message(text, *, ordinal=15, role="user", record="response_item", parts=None):
    content = parts if parts is not None else [{"type": "input_text", "text": text}]
    return json.dumps({"ordinal": ordinal, "type": record,
                       "payload": {"type": "message", "role": role, "content": content}}) + "\n"


def item_completed(text, ordinal=16):
    return json.dumps({"ordinal": ordinal, "type": "event_msg", "payload": {
        "type": "item_completed", "item": {"type": "UserMessage", "content": [{"type": "text", "text": text}]}}}) + "\n"


class CodexWakeCase(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()

    def make_thread(self, thread=THREAD, *, home=None, meta=True, lock=True, body=""):
        home = Path(home) if home else self.root / "codex-home"
        rollout = home / "sessions/2026/10/23" / f"rollout-2026-10-23T00-26-34-{thread}.jsonl"
        rollout.parent.mkdir(parents=True, exist_ok=True)
        rollout.write_text((meta_line(thread) if meta is True else (meta or "")) + body)
        lock_path = home / "thread-writer-locks" / f"{thread}.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        if lock:
            lock_path.touch()
        return rollout, lock_path

    def discover(self, processes, fds, *, binary="/fake/codex", query=None, agent=AGENT):
        listed = fds if callable(fds) else (lambda pid: fds.get(pid))
        return wake_codex.discover(
            agent, query=query or fake_query(processes), list_open_paths=listed,
            find_binary=lambda environment: binary)

    def assertUnavailable(self, code, *args, **kwargs):
        with self.assertRaises(WakeUnavailable) as caught:
            self.discover(*args, **kwargs)
        self.assertEqual(caught.exception.code, code, caught.exception.message)


class DiscoverTest(CodexWakeCase):
    def test_discovers_thread_home_pid_and_offset_from_open_files(self):
        rollout, lock = self.make_thread()
        target = self.discover([process(100)], {100: ["/dev/pts/3", str(lock), str(rollout), "/etc/hosts"]})
        self.assertEqual(target.client, "codex")
        self.assertEqual(target.session_ref, THREAD)
        self.assertEqual(target.pid, 100)
        self.assertEqual(target.journal_path, str(rollout))
        self.assertEqual(target.journal_offset, rollout.stat().st_size)
        self.assertEqual(dict(target.extra), {"codex_home": str(self.root / "codex-home"), "binary": "/fake/codex"})

    def test_codex_home_is_derived_from_the_rollout_path_not_the_environment(self):
        home = self.root / "elsewhere" / ".custom-codex"
        rollout, lock = self.make_thread(home=home)
        with mock.patch.dict(os.environ, {"CODEX_HOME": str(self.root / "ignored")}):
            target = self.discover([process(100)], {100: [str(rollout), str(lock)]})
        self.assertEqual(target.extra["codex_home"], str(home))

    def test_thread_lock_and_rollout_in_different_homes_is_refused(self):
        rollout, _ = self.make_thread()
        _, stray_lock = self.make_thread(home=self.root / "other-home")
        self.assertUnavailable("codex_home_mismatch", [process(100)], {100: [str(rollout), str(stray_lock)]})

    def test_unique_root_rule(self):
        rollout, lock = self.make_thread()
        held = [str(rollout), str(lock)]
        # Two roots that both hold a thread is ambiguous.
        self.assertUnavailable("codex_ambiguous_root", [process(100), process(101)], {100: held, 101: held})
        # A launcher that holds nothing next to the native process that does is fine.
        node = process(100, "node", ["node", "/usr/lib/node_modules/@openai/codex/bin/codex.js"])
        target = self.discover([node, process(101)], {100: ["/dev/pts/3"], 101: held})
        self.assertEqual(target.pid, 101)
        # Unrelated foreground processes are never roots.
        self.assertUnavailable("codex_not_foreground", [process(100, "node", ["node", "server.js"])], {100: held})
        self.assertUnavailable("codex_not_foreground", [], {})

    def test_pane_and_terminal_identity_must_match(self):
        rollout, lock = self.make_thread()
        fds = {100: [str(rollout), str(lock)]}
        self.assertUnavailable("codex_pane_mismatch", [process(100)], fds,
                               query=fake_query([process(100)], terminal_id="term_other"))
        self.assertUnavailable("codex_pane_mismatch", [process(100)], fds,
                               query=fake_query([process(100)], pane_id="wA:p9"))
        self.assertUnavailable("codex_not_foreground", [process(100)], fds,
                               query=fake_query([process(100)], group=0))

    def test_unreadable_herdr_answers_are_unavailable(self):
        def broken(*arguments):
            raise ValueError("no herdr")
        self.assertUnavailable("codex_process_unreadable", [], {}, query=broken)
        self.assertUnavailable("codex_pane_unknown", [], {}, agent={"client": "codex"})

    def test_missing_rollout_is_reported_not_guessed(self):
        _, lock = self.make_thread()
        # No thread files at all (before the first turn, or a shared app-server daemon).
        self.assertUnavailable("codex_no_rollout", [process(100)], {100: ["/dev/pts/3"]})
        # Lock held but the rollout is not open.
        self.assertUnavailable("codex_no_rollout", [process(100)], {100: [str(lock)]})
        # Rollout exists but is still empty.
        rollout, lock = self.make_thread(OTHER_THREAD, meta=False)
        self.assertUnavailable("codex_no_rollout", [process(100)], {100: [str(rollout), str(lock)]})

    def test_missing_binary(self):
        rollout, lock = self.make_thread()
        self.assertUnavailable("codex_missing", [process(100)], {100: [str(rollout), str(lock)]}, binary=None)

    def test_unreadable_open_files_are_unavailable(self):
        self.assertUnavailable("codex_fd_unreadable", [process(100)], {})

    def test_session_meta_must_agree_with_the_rollout_thread(self):
        rollout, lock = self.make_thread(meta=meta_line(OTHER_THREAD))
        self.assertUnavailable("codex_session_meta_mismatch", [process(100)], {100: [str(rollout), str(lock)]})
        rollout, lock = self.make_thread(OTHER_THREAD, meta=json.dumps({"type": "event_msg", "payload": {}}) + "\n")
        self.assertUnavailable("codex_session_meta_missing", [process(100)], {100: [str(rollout), str(lock)]})
        rollout, lock = self.make_thread("01a16438-bbbb-7702-af99-e82262b971d0", meta="not json")
        self.assertUnavailable("codex_session_meta_missing", [process(100)], {100: [str(rollout), str(lock)]})

    def test_sub_agent_threads_are_not_the_wake_target_but_two_top_level_threads_are_ambiguous(self):
        main_rollout, main_lock = self.make_thread()
        sub_rollout, sub_lock = self.make_thread(
            OTHER_THREAD, meta=meta_line(OTHER_THREAD, source={"subagent": {"parent": THREAD}}))
        both = [str(main_rollout), str(main_lock), str(sub_rollout), str(sub_lock)]
        self.assertEqual(self.discover([process(100)], {100: both}).session_ref, THREAD)
        second_rollout, second_lock = self.make_thread("01a16438-bbbb-7702-af99-e82262b971d0")
        self.assertUnavailable("codex_ambiguous_thread", [process(100)],
                               {100: both + [str(second_rollout), str(second_lock)]})

    def test_rollout_that_is_not_ours_is_refused(self):
        rollout, lock = self.make_thread()
        with mock.patch("ham.ledger.wake_codex.os.getuid", return_value=os.getuid() + 1):
            self.assertUnavailable("codex_rollout_untrusted", [process(100)], {100: [str(rollout), str(lock)]})

    def test_wrong_client(self):
        self.assertUnavailable("codex_not_codex", [], {}, agent={**AGENT, "client": "claude"})


class OpenFilesTest(unittest.TestCase):
    LSOF = ("p4242\nfcwd\nn/Users/me/work\nf5\nn/Users/me/.codex/sessions/2026/10/23/"
            f"rollout-2026-10-23T00-26-34-{THREAD}.jsonl\nf6\nn/Users/me/.codex/thread-writer-locks/{THREAD}.lock\n"
            "f7\nn->0x1234 (socket)\n")

    def test_lsof_names_feed_the_same_thread_rules(self):
        names = wake_codex.parse_lsof_names(self.LSOF)
        rollouts, locks = wake_codex._held_threads(names)
        self.assertEqual(list(rollouts), [THREAD])
        self.assertEqual(list(locks), [THREAD])
        self.assertEqual(next(iter(rollouts[THREAD])).parents[4], Path("/Users/me/.codex"))

    def test_lsof_distinguishes_a_gone_process_from_a_failed_probe(self):
        self.assertEqual(wake_codex.parse_lsof_names("", returncode=1), [])
        self.assertIsNone(wake_codex.parse_lsof_names("lsof: not permitted\n", returncode=2))
        self.assertEqual(wake_codex.parse_lsof_names("p9\n", returncode=0), [])

    def test_macos_path_is_used_when_there_is_no_proc(self):
        completed = subprocess.CompletedProcess([], 0, self.LSOF, "")
        with mock.patch("ham.ledger.wake_codex.os.path.isdir", return_value=False), \
                mock.patch("ham.ledger.wake_codex.subprocess.run", return_value=completed) as run:
            names = wake_codex.open_paths(4242)
        self.assertEqual(run.call_args.args[0], ["lsof", "-nP", "-a", "-p", "4242", "-Fn"])
        self.assertIn(f"/Users/me/.codex/thread-writer-locks/{THREAD}.lock", names)

    def test_lsof_missing_means_unknown_not_gone(self):
        with mock.patch("ham.ledger.wake_codex.os.path.isdir", return_value=False), \
                mock.patch("ham.ledger.wake_codex.subprocess.run", side_effect=FileNotFoundError):
            self.assertIsNone(wake_codex.open_paths(4242))

    def test_linux_proc_fd_listing_of_a_live_and_a_dead_process(self):
        if not os.path.isdir("/proc/self/fd"):
            self.skipTest("no /proc")
        with tempfile.NamedTemporaryFile() as held:
            self.assertIn(os.path.realpath(held.name), wake_codex.open_paths(os.getpid()))
        child = subprocess.Popen(["true"])
        child.wait()
        self.assertEqual(wake_codex.open_paths(child.pid), [])


class FindBinaryTest(CodexWakeCase):
    def executable(self, directory, name="codex"):
        path = Path(directory) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("#!/bin/sh\n")
        path.chmod(0o755)
        return str(path)

    def test_codex_bin_beats_path_and_non_executables_are_skipped(self):
        configured = self.executable(self.root / "configured")
        on_path = self.executable(self.root / "bin")
        env = {"CODEX_BIN": configured, "PATH": str(self.root / "bin")}
        self.assertEqual(wake_codex.find_codex_binary(env), configured)
        self.assertEqual(wake_codex.find_codex_binary({"PATH": str(self.root / "bin")}), on_path)
        (self.root / "configured/codex").chmod(0o644)
        self.assertEqual(wake_codex.find_codex_binary(env), on_path)

    def test_fixed_install_directories_and_absence(self):
        home_bin = self.executable(self.root / "dir")
        with mock.patch.object(wake_codex, "_BINARY_DIRECTORIES", (str(self.root / "dir"),)):
            self.assertEqual(wake_codex.find_codex_binary({"PATH": str(self.root / "empty")}), home_bin)
        with mock.patch.object(wake_codex, "_BINARY_DIRECTORIES", (str(self.root / "nothing"),)):
            self.assertIsNone(wake_codex.find_codex_binary({"PATH": str(self.root / "empty")}))


class DeliverTest(CodexWakeCase):
    def fake_codex(self, script):
        path = self.root / "codex"
        path.write_text("#!/bin/sh\n" + script)
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
        return str(path)

    def target(self, binary, home=None):
        return WakeTarget("codex", THREAD, 100, str(self.root / "rollout.jsonl"), 0,
                          {"binary": binary, "codex_home": str(home or self.root / "codex-home")})

    def test_queue_gets_thread_message_and_the_derived_codex_home(self):
        record = self.root / "record.json"
        binary = self.fake_codex(
            f'printf \'%s\\n\' "$CODEX_HOME" "$@" > "{record}"\necho "Queued message x for thread."\n')
        text = "-leading dash\nsecond line \"q\" 한글"
        with mock.patch.dict(os.environ, {"CODEX_HOME": "/wrong"}):
            wake_codex.deliver(self.target(binary), text)
        lines = record.read_text().split("\n", 5)
        self.assertEqual(lines[0], str(self.root / "codex-home"))
        self.assertEqual(lines[1:4], ["queue", "--thread", THREAD])
        self.assertEqual(lines[4] + "\n" + lines[5].rstrip("\n"), "--message=" + text)

    def test_nonzero_exit_is_definitely_not_delivered(self):
        binary = self.fake_codex("echo 'thread/queue/add failed: no rollout found' >&2\nexit 1\n")
        with self.assertRaises(WakeDeliveryFailed) as caught:
            wake_codex.deliver(self.target(binary), "text")
        self.assertTrue(caught.exception.definitely_not_delivered)
        self.assertEqual(caught.exception.code, "codex_queue_failed")
        self.assertIn("no rollout found", caught.exception.message)

    def test_timeout_may_have_delivered(self):
        binary = self.fake_codex("sleep 30\n")
        with self.assertRaises(WakeDeliveryFailed) as caught:
            wake_codex.deliver(self.target(binary), "text", timeout=0.3)
        self.assertFalse(caught.exception.definitely_not_delivered)
        self.assertEqual(caught.exception.code, "codex_queue_timeout")

    def test_death_by_signal_may_have_delivered(self):
        binary = self.fake_codex("kill -9 $$\n")
        with self.assertRaises(WakeDeliveryFailed) as caught:
            wake_codex.deliver(self.target(binary), "text")
        self.assertFalse(caught.exception.definitely_not_delivered)

    def test_binary_that_cannot_start_is_definitely_not_delivered(self):
        with self.assertRaises(WakeDeliveryFailed) as caught:
            wake_codex.deliver(self.target(str(self.root / "gone")), "text")
        self.assertTrue(caught.exception.definitely_not_delivered)
        self.assertEqual(caught.exception.code, "codex_missing")

    def test_oversized_message_is_refused_before_running_anything(self):
        runner = mock.Mock()
        with self.assertRaises(WakeDeliveryFailed) as caught:
            wake_codex.deliver(self.target("/fake"), "x" * 100_001, run_queue=runner)
        self.assertTrue(caught.exception.definitely_not_delivered)
        runner.assert_not_called()

    def test_default_timeout_is_thirty_seconds_and_stdin_is_closed(self):
        runner = mock.Mock(return_value=subprocess.CompletedProcess([], 0, "", ""))
        wake_codex.deliver(self.target("/fake"), "text", run_queue=runner)
        self.assertEqual(runner.call_args.args[2], 30.0)
        with mock.patch("ham.ledger.wake_codex.subprocess.run",
                        return_value=subprocess.CompletedProcess([], 0, "", "")) as run:
            wake_codex._run_queue(["x"], {}, 5)
        self.assertEqual(run.call_args.kwargs["stdin"], subprocess.DEVNULL)

    def test_target_without_binary_or_home_is_refused(self):
        with self.assertRaises(WakeDeliveryFailed) as caught:
            wake_codex.deliver(WakeTarget("codex", THREAD, 100, "j", 0), "text")
        self.assertTrue(caught.exception.definitely_not_delivered)


class ObserveTest(CodexWakeCase):
    def setUp(self):
        super().setUp()
        self.rollout, _ = self.make_thread(body=user_message("earlier turn", ordinal=3))
        self.start = self.rollout.stat().st_size
        self.live = lambda pid: [str(self.rollout)]
        self.dead = lambda pid: []

    def append(self, text):
        with open(self.rollout, "a", encoding="utf-8") as handle:
            handle.write(text)

    def observe(self, offset=None, *, content=CONTENT, lister=None, pid=os.getpid()):
        return wake_codex.observe(
            None, journal_path=str(self.rollout), offset=self.start if offset is None else offset,
            marker=MARKER, content=content, content_sha256=SHA, session_ref=THREAD, pid=pid,
            list_open_paths=lister or self.live)

    def test_exact_user_message_is_accepted_with_evidence(self):
        self.append(user_message("assistant noise", role="assistant"))
        self.append(user_message(CONTENT, ordinal=21))
        result = self.observe()
        self.assertEqual(result.status, "accepted")
        self.assertEqual(result.evidence, f"codex:rollout:{THREAD}#21;sha256={SHA}")
        self.assertEqual(result.offset, self.rollout.stat().st_size)

    def test_event_msg_user_message_forms_are_evidence_too(self):
        self.append(item_completed(CONTENT, ordinal=22))
        self.assertEqual(self.observe().evidence, f"codex:rollout:{THREAD}#22;sha256={SHA}")
        self.rollout.write_text(meta_line() + json.dumps(
            {"ordinal": 5, "type": "event_msg", "payload": {"type": "user_message", "message": CONTENT}}) + "\n")
        self.assertEqual(self.observe(0).evidence, f"codex:rollout:{THREAD}#5;sha256={SHA}")

    def test_missing_ordinal_falls_back_to_the_line_byte_offset(self):
        before = self.rollout.stat().st_size
        self.append(json.dumps({"type": "response_item", "payload": {
            "type": "message", "role": "user", "content": [{"type": "input_text", "text": CONTENT}]}}) + "\n")
        self.assertEqual(self.observe().evidence, f"codex:rollout:{THREAD}#offset={before};sha256={SHA}")

    def test_split_content_parts_that_join_to_the_text_match(self):
        half = len(CONTENT) // 2
        self.append(user_message("", parts=[{"type": "input_text", "text": CONTENT[:half]},
                                            {"type": "input_text", "text": CONTENT[half:]}]))
        self.assertEqual(self.observe().status, "accepted")

    def test_near_misses_are_not_acceptance(self):
        near = [
            user_message(CONTENT + " "),                              # extra trailing text
            user_message(CONTENT[:-3]),                               # truncated
            user_message(MARKER),                                     # marker only
            user_message("quote: " + CONTENT),                        # user quoting the bundle
            user_message(CONTENT, role="assistant"),                  # assistant echo
            user_message(CONTENT, role="developer"),
            user_message(CONTENT.replace("someone", "someone-else")),  # same marker, different body
            json.dumps({"type": "event_msg", "payload": {"type": "agent_message", "message": CONTENT}}) + "\n",
            json.dumps({"type": "response_item", "payload": {"type": "function_call_output", "output": CONTENT}}) + "\n",
            "garbage line containing " + MARKER + "\n",
            user_message(CONTENT, parts=[{"type": "image", "image_url": "x"}]),
        ]
        self.append("".join(near))
        result = self.observe()
        self.assertEqual((result.status, result.evidence), ("pending", None))
        self.assertEqual(result.offset, self.rollout.stat().st_size)  # complete lines are consumed

    def test_text_before_the_start_offset_is_ignored(self):
        self.append(user_message(CONTENT, ordinal=9))
        offset_after = self.rollout.stat().st_size
        result = self.observe(offset_after)
        self.assertEqual((result.status, result.offset), ("pending", offset_after))

    def test_partial_trailing_line_is_not_consumed_and_completes_later(self):
        complete = user_message(CONTENT, ordinal=30)
        noise = user_message("noise", ordinal=29)
        self.append(noise + complete[:40])
        first = self.observe()
        self.assertEqual(first.status, "pending")
        self.assertEqual(first.offset, self.start + len(noise.encode()))
        self.append(complete[40:])
        second = self.observe(first.offset)
        self.assertEqual(second.status, "accepted")
        self.assertEqual(second.evidence, f"codex:rollout:{THREAD}#30;sha256={SHA}")

    def test_dead_session_drains_large_backlog_with_bounded_reads_before_acceptance(self):
        noise = user_message("x" * (64 * 1024), role="assistant")
        for _ in range(wake_codex.MAX_SCAN_BYTES // len(noise.encode()) + 2):
            self.append(noise)
        self.append(user_message(CONTENT, ordinal=32))
        with self.rollout.open("rb") as stream, mock.patch.object(
                wake_codex, "open", create=True) as opened:
            reader = mock.Mock(wraps=stream)
            opened.return_value.__enter__.return_value = reader
            first = self.observe(pid=2**22 + 12345)
            self.assertEqual(first.status, "pending")
            self.assertGreater(first.offset, self.start)
            self.assertLessEqual(first.offset - self.start, wake_codex.MAX_SCAN_BYTES)
            second = self.observe(first.offset, pid=2**22 + 12345)
        self.assertEqual(second.status, "accepted")
        self.assertEqual(second.evidence, f"codex:rollout:{THREAD}#32;sha256={SHA}")
        sizes = [call.args[0] for call in reader.read.call_args_list]
        self.assertEqual(len(sizes), 4)  # One boundary byte and one bounded window per scan.
        self.assertTrue(all(0 <= size <= wake_codex.MAX_SCAN_BYTES for size in sizes))

    def test_dead_session_reports_gone_only_after_progress_is_exhausted(self):
        self.append(user_message("unrelated turn"))
        first = self.observe(lister=self.dead)
        self.assertEqual((first.status, first.offset), ("pending", self.rollout.stat().st_size))
        self.assertEqual(self.observe(first.offset, lister=self.dead).status, "gone")

    def test_journal_only_recovery_accepts_after_an_oversized_preceding_record(self):
        with self.rollout.open("ab") as stream:
            stream.write(b'{"type":"event_msg","padding":"')
            chunk = b"x" * (64 * 1024)
            for _ in range(wake_codex.MAX_SCAN_BYTES // len(chunk) + 2):
                stream.write(chunk)
            stream.write(b'"}\n')
        self.append(user_message(CONTENT, ordinal=33))
        first = self.observe(pid=None)
        self.assertEqual((first.status, first.offset),
                         ("pending", self.start + wake_codex.MAX_SCAN_BYTES))
        second = self.observe(first.offset, pid=None)
        self.assertEqual(second.status, "accepted")
        self.assertEqual(second.evidence, f"codex:rollout:{THREAD}#33;sha256={SHA}")

    def test_oversized_record_final_fragment_is_not_a_separate_record(self):
        with self.rollout.open("ab") as stream:
            stream.write(b"x" * wake_codex.MAX_SCAN_BYTES)
        self.append(user_message(CONTENT, ordinal=34))
        first = self.observe()
        second = self.observe(first.offset)
        self.assertEqual((first.status, second.status), ("pending", "pending"))
        self.assertEqual(second.offset, self.rollout.stat().st_size)
        self.append(user_message(CONTENT, ordinal=35))
        self.assertEqual(self.observe(second.offset).evidence,
                         f"codex:rollout:{THREAD}#35;sha256={SHA}")

    def test_partial_tail_after_oversized_record_waits_for_its_newline(self):
        with self.rollout.open("ab") as stream:
            stream.write(b"x" * wake_codex.MAX_SCAN_BYTES + b"\n")
        self.append(user_message(CONTENT, ordinal=36).rstrip("\n"))
        first = self.observe()
        second = self.observe(first.offset)
        self.assertEqual(second.status, "pending")
        self.assertEqual(second.offset, first.offset + 1)
        third = self.observe(second.offset)
        self.assertEqual(third.offset, second.offset)
        self.append("\n")
        self.assertEqual(self.observe(third.offset).evidence,
                         f"codex:rollout:{THREAD}#36;sha256={SHA}")

    def test_partial_line_that_already_looks_like_a_match_waits_for_its_newline(self):
        self.append(user_message(CONTENT, ordinal=31).rstrip("\n"))  # valid JSON, no newline yet
        pending = self.observe()
        self.assertEqual((pending.status, pending.offset), ("pending", self.start))
        self.append("\n")
        self.assertEqual(self.observe().status, "accepted")

    def test_gone_only_when_no_evidence_and_the_process_no_longer_holds_the_thread(self):
        self.assertEqual(self.observe(lister=self.dead).status, "gone")
        self.assertEqual(self.observe(lister=lambda pid: ["/dev/null"]).status, "gone")
        self.assertEqual(self.observe(pid=None).status, "gone")
        self.append(user_message(CONTENT))
        self.assertEqual(self.observe(lister=self.dead).status, "accepted")  # last-moment evidence wins

    def test_unreadable_journal_depends_on_liveness(self):
        self.rollout.unlink()
        self.assertEqual(self.observe().status, "pending")
        self.assertEqual(self.observe(lister=self.dead).status, "gone")

    def test_marker_needing_json_escapes_still_matches(self):
        text = 'HAM_CLAIM_"x"\\y\nbody'
        self.append(user_message(text))
        result = wake_codex.observe(None, journal_path=str(self.rollout), offset=self.start,
                                    marker='HAM_CLAIM_"x"\\y', content=text, content_sha256=SHA,
                                    session_ref=THREAD, pid=os.getpid(), list_open_paths=self.live)
        self.assertEqual(result.status, "accepted")


class AliveTest(CodexWakeCase):
    def test_alive_requires_the_process_to_hold_this_thread(self):
        rollout, lock = self.make_thread()
        mine = os.getpid()
        self.assertTrue(wake_codex.alive(mine, THREAD, list_open_paths=lambda pid: [str(rollout)]))
        self.assertTrue(wake_codex.alive(mine, THREAD.upper(), list_open_paths=lambda pid: [str(lock)]))
        other, _ = self.make_thread(OTHER_THREAD)
        self.assertFalse(wake_codex.alive(mine, THREAD, list_open_paths=lambda pid: [str(other)]))
        self.assertFalse(wake_codex.alive(mine, THREAD, list_open_paths=lambda pid: []))

    def test_unknowable_files_do_not_declare_the_session_gone(self):
        self.assertTrue(wake_codex.alive(os.getpid(), THREAD, list_open_paths=lambda pid: None))

    def test_dead_or_invalid_pids(self):
        child = subprocess.Popen(["true"])
        child.wait()
        self.assertFalse(wake_codex.alive(child.pid, THREAD, list_open_paths=lambda pid: self.fail("not probed")))
        for pid in (0, 1, -5, True):
            self.assertFalse(wake_codex.alive(pid, THREAD))


class ObservationTypeTest(unittest.TestCase):
    def test_observation_defaults(self):
        self.assertEqual(Observation("pending", 3), Observation("pending", 3, None))


if __name__ == "__main__":
    unittest.main()
