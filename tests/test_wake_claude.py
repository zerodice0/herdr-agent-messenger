import tests.ham_test_support as ham_test_support  # Protect the live ledger before product imports.
import json
import os
from pathlib import Path
import socket
import tempfile
import unittest
from unittest import mock
import uuid

from ham.ledger import wake_claude
from ham.ledger.wake_clients import WakeDeliveryFailed, WakeUnavailable

MARKER = "HAM_CLAIM_" + "1" * 36 + "_" + "2" * 32
TEXT = MARKER + "\nHAM mail: coordination.\n[1] action hi 한글\nReply later.\n"
SHA = "f" * 64


class ClaudeFixture(unittest.TestCase):
    """A fake Claude config dir: registry entry, real socket file, transcript."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = self.root / "claude"
        self.session = str(uuid.uuid4())
        self.pid = os.getpid()  # A live pid, so alive() is real.
        (self.config / "sessions").mkdir(parents=True)
        self.project = self.config / "projects" / "-work-repo"
        self.project.mkdir(parents=True)
        self.transcript = self.project / f"{self.session}.jsonl"
        self.transcript.write_text(self.line({"type": "permission-mode", "permissionMode": "default"}))
        self.socket_path = str(self.root / "s.sock")
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(self.listener.close)
        previous = os.getcwd()
        os.chdir(self.root)  # Bind relative: the absolute path may exceed sun_path.
        try:
            self.listener.bind("s.sock")
        finally:
            os.chdir(previous)
        self.registry = self.config / "sessions" / f"{self.pid}.json"
        self.write_registry()
        self.agent = {"client": "claude", "extension_present": 1, "session_ref": self.session,
                      "native_session_pid": self.pid, "pane_id": "p1", "terminal_id": "t1"}
        self.environment = {"CLAUDE_CONFIG_DIR": str(self.config), "HOME": str(self.root)}

    @staticmethod
    def line(record):
        return json.dumps(record) + "\n"

    def write_registry(self, **changes):
        entry = {"pid": self.pid, "sessionId": self.session, "kind": "interactive",
                 "messagingSocketPath": self.socket_path, "procStart": "777", **changes}
        self.registry.write_text(json.dumps(entry))

    def discover(self, agent=None, **options):
        options.setdefault("foreground_pid", lambda *_args, **_kwargs: self.pid)
        options.setdefault("proc_start", lambda pid: "777")
        return wake_claude.discover(agent or self.agent, environment=self.environment, **options)

    def append(self, *records, raw=False):
        with open(self.transcript, "a", encoding="utf-8") as handle:
            for record in records:
                handle.write(record if raw else self.line(record))

    def observe(self, target, offset, text=TEXT, pid=None):
        return wake_claude.observe(
            target, journal_path=str(self.transcript), offset=offset, marker=MARKER, content=text,
            content_sha256=SHA, session_ref=self.session, pid=self.pid if pid is None else pid)

    def enqueue(self, text=TEXT):
        return {"type": "queue-operation", "operation": "enqueue", "sessionId": self.session, "content": text}

    def peer_turn(self, text=TEXT, origin="peer", **changes):
        return {"type": "user", "uuid": "turn-uuid-1", "sessionId": self.session, "origin": {"kind": origin},
                "message": {"role": "user",
                            "content": "Another Claude session sent a message:\n" + text + "\n\nThis came from..."},
                **changes}

    def queued_command(self, prompt=TEXT, **changes):
        return {"type": "attachment", "uuid": "att-uuid-1", "sessionId": self.session,
                "attachment": {"type": "queued_command", "prompt": prompt, "delivery_id": "delivery-1",
                               "origin": {"kind": "peer", "from": "ham"}, **changes}}


class DiscoverTest(ClaudeFixture):
    def reason(self, agent=None, **options):
        with self.assertRaises(WakeUnavailable) as caught:
            self.discover(agent, **options)
        return caught.exception.code

    def test_verified_session_yields_target_at_transcript_end(self):
        target = self.discover()
        self.assertEqual((target.client, target.session_ref, target.pid), ("claude", self.session, self.pid))
        self.assertEqual(target.journal_path, str(self.transcript))
        self.assertEqual(target.journal_offset, self.transcript.stat().st_size)
        self.assertEqual(target.extra["socket_path"], self.socket_path)

    def test_agent_prerequisites(self):
        cases = [({"client": "codex"}, "claude_not_claude"), ({"extension_present": 0}, "claude_hook_missing"),
                 ({"session_ref": "not-a-uuid"}, "claude_session_unbound"),
                 ({"session_ref": None}, "claude_session_unbound"),
                 ({"native_session_pid": True}, "claude_pid_unbound")]
        for changes, code in cases:
            with self.subTest(changes=changes):
                self.assertEqual(self.reason({**self.agent, **changes}), code)

    def test_foreground_process_must_be_the_bound_root(self):
        self.assertEqual(self.reason(foreground_pid=lambda *_a, **_k: None), "claude_process_unverified")
        self.assertEqual(self.reason(foreground_pid=lambda *_a, **_k: self.pid + 1), "claude_pid_mismatch")
        seen = {}

        def foreground(agent, environment, **options):
            seen.update(options)
            return self.pid
        self.discover(foreground_pid=foreground)
        self.assertEqual(seen, {"require_ancestor": False})

    def test_missing_stored_pid_uses_exact_foreground_registry_identity_without_mutating_agent(self):
        for omitted in (False, True):
            with self.subTest(omitted=omitted):
                agent = {**self.agent, "native_session_pid": None}
                if omitted:
                    del agent["native_session_pid"]
                before = dict(agent)
                target = self.discover(agent)
                self.assertEqual((target.session_ref, target.pid), (self.session, self.pid))
                self.assertEqual(target.extra["socket_path"], self.socket_path)
                self.assertEqual(agent, before)

    def test_missing_stored_pid_requires_one_foreground_root(self):
        agent = {**self.agent, "native_session_pid": None}
        root = {"name": "claude", "argv": ["claude"], "pid": self.pid}
        for roots in ([], [root, {**root, "pid": self.pid + 1}]):
            with self.subTest(roots=roots):
                responses = [
                    {"pane": {"pane_id": agent["pane_id"], "terminal_id": agent["terminal_id"]}},
                    {"process_info": {"pane_id": agent["pane_id"], "foreground_process_group_id": self.pid,
                                      "foreground_processes": roots}},
                ]
                with mock.patch("ham.ledger.status.run_bounded_command",
                                side_effect=[mock.Mock(returncode=0, stdout=json.dumps({"result": result}))
                                             for result in responses]):
                    self.assertEqual(self.reason(agent, foreground_pid=None), "claude_process_unverified")

    def test_missing_stored_pid_does_not_override_session_or_process_identity(self):
        agent = {**self.agent, "native_session_pid": None}
        for changes, code in (
            ({"sessionId": str(uuid.uuid4())}, "claude_session_mismatch"),
            ({"pid": self.pid + 1}, "claude_session_mismatch"),
            ({"procStart": "778"}, "claude_pid_recycled"),
            ({"procStart": None}, "claude_pid_unbound"),
            ({"kind": "headless"}, "claude_not_interactive"),
            ({"messagingSocketPath": None}, "claude_socket_missing"),
        ):
            with self.subTest(changes=changes):
                self.write_registry(**changes)
                self.assertEqual(self.reason(agent), code)
        self.write_registry()
        self.assertEqual(self.reason(agent, proc_start=lambda pid: None), "claude_pid_unbound")
        self.assertEqual(self.reason(agent, uid=os.getuid() + 1), "claude_registry_missing")
        for session, code in ((None, "claude_session_unbound"), ("unreported", "claude_session_unbound"),
                              ("not-a-uuid", "claude_session_unbound"),
                              (str(uuid.uuid4()), "claude_session_mismatch")):
            with self.subTest(session=session):
                self.assertEqual(self.reason({**agent, "session_ref": session}), code)

    def test_stored_pid_conflict_is_not_replaced_by_a_matching_registry(self):
        agent = {**self.agent, "native_session_pid": self.pid + 1}
        self.assertEqual(self.reason(agent), "claude_pid_mismatch")
        self.assertEqual(agent["native_session_pid"], self.pid + 1)

    def test_registry_entry_rejections(self):
        self.write_registry(sessionId=str(uuid.uuid4()))
        self.assertEqual(self.reason(), "claude_session_mismatch")
        self.write_registry(pid=self.pid + 1)
        self.assertEqual(self.reason(), "claude_session_mismatch")
        self.write_registry(kind="headless")
        self.assertEqual(self.reason(), "claude_not_interactive")
        self.write_registry(procStart="778")
        self.assertEqual(self.reason(), "claude_pid_recycled")
        self.write_registry(messagingSocketPath=None)
        self.assertEqual(self.reason(), "claude_socket_missing")
        self.registry.write_text("{ not json")
        self.assertEqual(self.reason(), "claude_registry_missing")
        self.registry.unlink()
        self.assertEqual(self.reason(), "claude_registry_missing")

    def test_registry_owned_by_someone_else_is_not_trusted(self):
        self.assertEqual(self.reason(uid=os.getuid() + 1), "claude_registry_missing")

    def test_unavailable_proc_start_does_not_reject(self):
        self.assertEqual(self.discover(proc_start=lambda pid: None).pid, self.pid)

    def test_socket_must_exist_and_be_a_socket(self):
        plain = self.root / "plain"
        plain.write_text("x")
        self.write_registry(messagingSocketPath=str(plain))
        self.assertEqual(self.reason(), "claude_socket_untrusted")
        self.write_registry(messagingSocketPath=str(self.root / "gone.sock"))
        self.assertEqual(self.reason(), "claude_socket_missing")
        self.write_registry(messagingSocketPath="relative.sock")
        self.assertEqual(self.reason(), "claude_socket_missing")

    def test_transcript_must_be_exactly_one_plain_file(self):
        original = self.transcript.read_bytes()
        self.transcript.unlink()
        self.assertEqual(self.reason(), "claude_transcript_missing")
        other = self.config / "projects" / "-other"
        other.mkdir()
        elsewhere = self.root / "real.jsonl"
        elsewhere.write_bytes(original)
        (self.project / f"{self.session}.jsonl").symlink_to(elsewhere)
        self.assertEqual(self.reason(), "claude_transcript_untrusted")
        (self.project / f"{self.session}.jsonl").unlink()
        (self.project / f"{self.session}.jsonl").write_bytes(original)
        (other / f"{self.session}.jsonl").write_bytes(original)
        self.assertEqual(self.reason(), "claude_transcript_ambiguous")

    def test_bypass_permissions_holds_peer_messages_unless_accepted(self):
        self.append({"type": "user", "permissionMode": "bypassPermissions"})
        self.assertEqual(self.reason(), "claude_holds_peer_messages")
        (self.config / "settings.json").write_text(json.dumps({"crossSessionInbound": "ask"}))
        self.assertEqual(self.reason(), "claude_holds_peer_messages")
        (self.config / "settings.json").write_text(json.dumps({"crossSessionInbound": "accept"}))
        self.assertEqual(self.discover().session_ref, self.session)

    def test_only_the_latest_permission_mode_counts(self):
        self.append({"type": "user", "permissionMode": "bypassPermissions"},
                    {"type": "permission-mode", "permissionMode": "auto"})
        self.assertEqual(wake_claude.permission_mode(self.transcript), "auto")
        self.assertEqual(self.discover().session_ref, self.session)

    def test_permission_mode_ignores_a_cut_first_line_of_a_large_tail(self):
        self.transcript.write_text(self.line({"type": "permission-mode", "permissionMode": "bypassPermissions"})
                                   + "x" * (wake_claude.MODE_TAIL_BYTES + 10) + "\n")
        self.assertIsNone(wake_claude.permission_mode(self.transcript))


class DeliverTest(ClaudeFixture):
    def target(self, **extra):
        target = self.discover()
        return type(target)(target.client, target.session_ref, target.pid, target.journal_path,
                            target.journal_offset, {**target.extra, **extra})

    def test_writes_one_json_line_the_session_can_parse(self):
        near, far = socket.socketpair()
        self.addCleanup(far.close)
        wake_claude.deliver(self.target(), TEXT, connect=lambda path, timeout: near)
        far.settimeout(1)
        data = far.recv(65536)
        self.assertTrue(data.endswith(b"\n") and data.count(b"\n") == 1)
        message = json.loads(data)
        self.assertEqual(message, {"type": "user", "from": "ham", "message": {"role": "user", "content": TEXT}})
        self.assertEqual(far.recv(1), b"")  # The connection is closed after the single message.

    def test_connect_failures_are_definitely_not_delivered(self):
        for error in (FileNotFoundError(), ConnectionRefusedError(), socket.timeout()):
            def refuse(path, timeout, error=error):
                raise error
            with self.subTest(error=type(error).__name__), self.assertRaises(WakeDeliveryFailed) as caught:
                wake_claude.deliver(self.target(), TEXT, connect=refuse)
            self.assertTrue(caught.exception.definitely_not_delivered)
            self.assertEqual(caught.exception.code, "claude_socket_unreachable")

    def test_write_failure_after_connect_may_have_been_delivered(self):
        class Broken:
            closed = False

            def sendall(self, data):
                raise BrokenPipeError()

            def close(self):
                self.closed = True
        broken = Broken()
        with self.assertRaises(WakeDeliveryFailed) as caught:
            wake_claude.deliver(self.target(), TEXT, connect=lambda path, timeout: broken)
        self.assertFalse(caught.exception.definitely_not_delivered)
        self.assertTrue(broken.closed)

    def test_missing_socket_or_oversized_message_never_connects(self):
        def never(path, timeout):
            raise AssertionError("must not connect")
        for target, text, code in ((self.target(socket_path=""), TEXT, "claude_socket_missing"),
                                   (self.target(), "x" * wake_claude.MAX_MESSAGE_BYTES, "claude_message_too_large")):
            with self.subTest(code=code), self.assertRaises(WakeDeliveryFailed) as caught:
                wake_claude.deliver(target, text, connect=never)
            self.assertTrue(caught.exception.definitely_not_delivered)
            self.assertEqual(caught.exception.code, code)


class ObserveTest(ClaudeFixture):
    def setUp(self):
        super().setUp()
        self.target = self.discover()
        self.offset = self.target.journal_offset

    def test_enqueue_alone_is_not_acceptance(self):
        self.append(self.enqueue())
        result = self.observe(self.target, self.offset)
        self.assertEqual(result.status, "pending")
        self.assertEqual(result.offset, self.transcript.stat().st_size)

    def test_peer_turn_with_the_exact_text_is_accepted_with_evidence(self):
        self.append(self.enqueue(), self.peer_turn())
        result = self.observe(self.target, self.offset)
        self.assertEqual(result.status, "accepted")
        self.assertEqual(result.evidence, f"claude:peer-journal:{self.session}#turn-uuid-1;sha256={SHA}")

    def test_mid_turn_absorbed_queued_command_is_accepted(self):
        self.append(self.queued_command())
        result = self.observe(self.target, self.offset)
        self.assertEqual(result.status, "accepted")
        self.assertIn("#att-uuid-1;", result.evidence)

    def test_wrong_text_near_misses_are_not_acceptance(self):
        for record in (self.peer_turn(TEXT[:-2] + "!\n"), self.peer_turn(TEXT.replace("hi", "ho")),
                       self.peer_turn(origin="human"), self.peer_turn(origin="unknown"),
                       self.peer_turn(sessionId=str(uuid.uuid4())),
                       {**self.peer_turn(), "type": "assistant"},
                       self.queued_command(TEXT.replace("hi", "ho")),
                       self.queued_command(TEXT + "extra"),
                       self.queued_command(origin={"kind": "human"}),
                       {"type": "queue-operation", "operation": "remove", "content": TEXT,
                        "reason": "absorbed_mid_turn"}):
            with self.subTest(record=record):
                self.assertEqual(self.observe(self.target, self.offset).status, "pending")
                self.append(record)
                self.assertEqual(self.observe(self.target, self.offset).status, "pending")
                self.transcript.write_bytes(self.transcript.read_bytes()[:self.offset])

    def test_a_partial_trailing_line_waits_and_is_rescanned_whole(self):
        record = self.line(self.peer_turn())
        self.append(record[:60], raw=True)
        first = self.observe(self.target, self.offset)
        self.assertEqual((first.status, first.offset), ("pending", self.offset))
        self.append(record[60:-1], raw=True)
        second = self.observe(self.target, first.offset)
        self.assertEqual((second.status, second.offset), ("pending", self.offset))
        self.append("\n", raw=True)
        self.assertEqual(self.observe(self.target, second.offset).status, "accepted")

    def test_dead_session_drains_large_backlog_with_bounded_reads_before_acceptance(self):
        noise = self.line({"type": "assistant", "message": {"content": "x" * (64 * 1024)}})
        for _ in range(wake_claude.MAX_SCAN_BYTES // len(noise.encode()) + 2):
            self.append(noise, raw=True)
        self.append(self.peer_turn())
        with self.transcript.open("rb") as stream, mock.patch.object(
                wake_claude, "open", create=True) as opened:
            reader = mock.Mock(wraps=stream)
            opened.return_value.__enter__.return_value = reader
            first = self.observe(self.target, self.offset, pid=2**22 + 12345)
            self.assertEqual(first.status, "pending")
            self.assertGreater(first.offset, self.offset)
            self.assertLessEqual(first.offset - self.offset, wake_claude.MAX_SCAN_BYTES)
            second = self.observe(self.target, first.offset, pid=2**22 + 12345)
        self.assertEqual(second.status, "accepted")
        self.assertIn("#turn-uuid-1;", second.evidence)
        sizes = [call.args[0] for call in reader.read.call_args_list]
        self.assertEqual(len(sizes), 4)  # One boundary byte and one bounded window per scan.
        self.assertTrue(all(0 <= size <= wake_claude.MAX_SCAN_BYTES for size in sizes))

    def test_dead_session_reports_gone_only_after_progress_is_exhausted(self):
        self.registry.unlink()
        self.append(self.enqueue())
        first = self.observe(self.target, self.offset)
        self.assertEqual((first.status, first.offset), ("pending", self.transcript.stat().st_size))
        self.assertEqual(self.observe(self.target, first.offset).status, "gone")

    def test_journal_only_recovery_accepts_after_an_oversized_preceding_record(self):
        with self.transcript.open("ab") as stream:
            stream.write(b'{"type":"assistant","padding":"')
            chunk = b"x" * (64 * 1024)
            for _ in range(wake_claude.MAX_SCAN_BYTES // len(chunk) + 2):
                stream.write(chunk)
            stream.write(b'"}\n')
        self.append(self.peer_turn())
        arguments = dict(journal_path=str(self.transcript), marker=MARKER, content=TEXT,
                         content_sha256=SHA, session_ref=self.session, pid=None)
        first = wake_claude.observe(None, offset=self.offset, **arguments)
        self.assertEqual((first.status, first.offset),
                         ("pending", self.offset + wake_claude.MAX_SCAN_BYTES))
        self.assertEqual(wake_claude.observe(None, offset=first.offset, **arguments).status, "accepted")

    def test_oversized_record_final_fragment_is_not_a_separate_record(self):
        with self.transcript.open("ab") as stream:
            stream.write(b"x" * wake_claude.MAX_SCAN_BYTES)
        self.append(self.peer_turn())
        first = self.observe(self.target, self.offset)
        second = self.observe(self.target, first.offset)
        self.assertEqual((first.status, second.status), ("pending", "pending"))
        self.assertEqual(second.offset, self.transcript.stat().st_size)
        self.append(self.peer_turn(uuid="real-turn"))
        self.assertIn("#real-turn;", self.observe(self.target, second.offset).evidence)

    def test_partial_tail_after_oversized_record_waits_for_its_newline(self):
        with self.transcript.open("ab") as stream:
            stream.write(b"x" * wake_claude.MAX_SCAN_BYTES + b"\n")
        record = self.line(self.peer_turn())
        self.append(record[:-1], raw=True)
        first = self.observe(self.target, self.offset)
        second = self.observe(self.target, first.offset)
        self.assertEqual(second.status, "pending")
        self.assertEqual(second.offset, first.offset + 1)
        third = self.observe(self.target, second.offset)
        self.assertEqual(third.offset, second.offset)
        self.append("\n", raw=True)
        self.assertEqual(self.observe(self.target, third.offset).status, "accepted")

    def test_unterminated_complete_json_is_not_trusted_until_its_newline(self):
        self.append(self.line(self.peer_turn()).rstrip("\n"), raw=True)
        self.assertEqual(self.observe(self.target, self.offset).status, "pending")

    def test_corrupt_lines_are_skipped_and_scan_progress_is_returned(self):
        self.append("{ broken " + MARKER + "\n", raw=True)
        first = self.observe(self.target, self.offset)
        self.assertEqual((first.status, first.offset), ("pending", self.transcript.stat().st_size))
        self.append(self.peer_turn())
        self.assertEqual(self.observe(self.target, first.offset).status, "accepted")

    def test_evidence_before_the_claim_offset_is_ignored(self):
        self.append(self.peer_turn())
        stale = self.transcript.stat().st_size
        self.assertEqual(self.observe(self.target, stale).status, "pending")
        self.assertEqual(self.observe(self.target, self.offset).status, "accepted")

    def test_a_rewritten_shorter_transcript_is_rescanned_from_the_start(self):
        self.transcript.write_text(self.line(self.peer_turn()))
        self.assertEqual(self.observe(self.target, 10**6).status, "accepted")

    def test_claude_trimming_surrounding_whitespace_still_matches(self):
        self.append(self.peer_turn(TEXT.strip()))
        self.assertEqual(self.observe(self.target, self.offset).status, "accepted")

    def test_content_block_lists_are_read(self):
        record = self.peer_turn()
        record["message"]["content"] = [{"type": "text", "text": "intro\n" + TEXT}]
        self.append(record)
        self.assertEqual(self.observe(self.target, self.offset).status, "accepted")

    def test_session_that_is_gone_is_reported_only_without_evidence(self):
        self.assertEqual(self.observe(self.target, self.offset).status, "pending")
        self.registry.unlink()
        self.assertEqual(self.observe(self.target, self.offset).status, "gone")
        self.append(self.peer_turn())
        self.assertEqual(self.observe(self.target, self.offset).status, "accepted")
        self.write_registry()
        self.write_registry(sessionId=str(uuid.uuid4()))
        self.transcript.write_bytes(self.transcript.read_bytes()[:self.offset])
        self.assertEqual(self.observe(self.target, self.offset).status, "gone")

    def test_dead_process_is_gone_and_unknown_pid_is_gone(self):
        self.assertEqual(self.observe(self.target, self.offset, pid=2**22 + 12345).status, "gone")
        self.assertFalse(wake_claude.alive(0, self.session, config=self.config))
        self.assertFalse(wake_claude.alive(self.pid, self.session, config=self.root / "elsewhere"))
        self.assertTrue(wake_claude.alive(self.pid, self.session.upper(), config=self.config))

    def test_a_missing_journal_keeps_the_offset_and_does_not_accept(self):
        self.transcript.unlink()
        result = self.observe(self.target, self.offset)
        self.assertEqual((result.status, result.offset), ("pending", self.offset))


if __name__ == "__main__":
    unittest.main()
