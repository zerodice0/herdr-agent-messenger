"""Operator capabilities, process-bound identity and inert Unicode status text."""
import tests.ham_test_support as ham_test_support

import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock
import uuid

from ham.ledger.display import build_display
from ham.ledger.remote import RemoteLedger
from ham.ledger.status import AgentRegistry, sanitize_agent_status, sanitize_status_text
from ham.ledger.store import LedgerError, LedgerStore
from ham.ledger.views import status_board
from ham.ui import mailbox
from tests.test_ledger_core import snapshot


class LocalAuthorityHardeningTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="local-authority-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.environment = {**os.environ, "HERDR_AGENT_LEDGER_DIR": str(self.root / "ledger"),
                            "HERDR_AGENT_LEDGER_AUTOSTART": "0"}
        self.store = LedgerStore(environment=self.environment, canonical_name="alpha")
        self.registry = AgentRegistry(self.store)
        self.live = snapshot(self.repo)
        self.registry.reconcile(self.live, refresh_display=False)
        self.sender, self.recipient = sorted(self.registry.list(), key=lambda row: row["terminal_id"])

    def capability(self, *, parent_pid=None, token=None, mode=0o600):
        token = token or b"a" * 64
        read_fd, write_fd = os.pipe()
        os.write(write_fd, token)
        os.close(write_fd)
        path = self.root / ("cap-" + uuid.uuid4().hex)
        path.write_text(json.dumps({"parent_pid": os.getppid() if parent_pid is None else parent_pid,
                                    "sha256": hashlib.sha256(token).hexdigest()}))
        path.chmod(mode)
        return {mailbox.JOB_AUTH_FD: str(read_fd), mailbox.JOB_AUTH_FILE: str(path)}, path

    def test_public_job_without_capability_rejected_before_execution(self):
        with mock.patch.dict(os.environ, self.environment, clear=True), \
             mock.patch.object(mailbox.sys, "stdin", io.StringIO('{"operation":"send"}')), \
             mock.patch.object(mailbox.sys, "stdout", io.StringIO()) as output, \
             mock.patch.object(mailbox, "execute_job") as execute:
            self.assertEqual(mailbox.main(["job"]), 1)
        execute.assert_not_called()
        self.assertEqual(json.loads(output.getvalue())["error"]["code"], "ui_job_unauthorized")

    def test_capability_is_one_use_and_not_an_environment_secret(self):
        environment, path = self.capability()
        self.assertNotIn("a" * 64, json.dumps(environment))
        mailbox.authorize_job(environment)
        self.assertFalse(path.exists())
        with self.assertRaises(LedgerError) as raised:
            mailbox.authorize_job(environment)
        self.assertEqual(raised.exception.code, "ui_job_unauthorized")

    def test_capability_rejects_wrong_parent_token_or_permissions(self):
        for mismatch in ("parent", "token", "mode", "symlink"):
            with self.subTest(mismatch=mismatch):
                environment, path = self.capability(parent_pid=-1 if mismatch == "parent" else None,
                                                    mode=0o644 if mismatch == "mode" else 0o600)
                if mismatch == "token":
                    path.write_text(json.dumps({"parent_pid": os.getppid(), "sha256": "0" * 64}))
                if mismatch == "symlink":
                    link = self.root / "link"
                    link.symlink_to(path)
                    environment[mailbox.JOB_AUTH_FILE] = str(link)
                with self.assertRaises(LedgerError) as raised:
                    mailbox.authorize_job(environment)
                self.assertEqual(raised.exception.code, "ui_job_unauthorized")

    def test_real_tui_job_accepts_inherited_capability_and_preserves_process_group(self):
        job = mailbox.Job({"operation": "send", "repo_id": self.sender["repo_id"],
                           "recipient": self.recipient, "body": "Operator send", "kind": "fyi"},
                          self.environment)
        self.addCleanup(job.close)
        self.assertEqual(os.getpgid(job.process.pid), job.process.pid)
        job.process.wait(timeout=10)
        done, result = job.poll()
        self.assertTrue(done)
        self.assertNotIn("error", result)
        with self.store.read() as connection:
            row = connection.execute("SELECT sender,body FROM messages").fetchone()
        self.assertEqual(tuple(row), ("user", "Operator send"))
        job.close()
        self.assertFalse(job.auth_path.exists())
        self.assertTrue(job.input.closed and job.output.closed and job.errors.closed)

    def test_forged_terminal_selector_never_falls_back_to_other_ancestor(self):
        with mock.patch("ham.ledger.status.process_ancestors", return_value={10}), \
             mock.patch("ham.ledger.status.pane_shell_pid", side_effect=lambda pane, env: 10 if pane == "w1:p1" else 20):
            with self.assertRaises(LedgerError) as raised:
                self.registry.resolve_context({"HERDR_TERMINAL_ID": "term-b"}, self.live)
        self.assertEqual(raised.exception.code, "sender_unresolved")

    def test_terminal_selector_accepts_only_live_ancestry_and_survives_move(self):
        moved = snapshot(self.repo, moved=True)
        self.registry.reconcile(moved, refresh_display=False)
        with mock.patch("ham.ledger.status.process_ancestors", return_value={10}), \
             mock.patch("ham.ledger.status.pane_shell_pid", return_value=10) as probe:
            agent = self.registry.resolve_context({"HERDR_TERMINAL_ID": "term-b", "HERDR_PANE_ID": "old"}, moved)
        self.assertEqual(agent["pane_id"], "w2:p9")
        probe.assert_called_once_with("w2:p9", {"HERDR_TERMINAL_ID": "term-b", "HERDR_PANE_ID": "old"})

    def test_unknown_terminal_and_unavailable_ancestry_fail_closed(self):
        for terminal, ancestors in (("missing", {10}), ("term-b", set())):
            with self.subTest(terminal=terminal), \
                 mock.patch("ham.ledger.status.process_ancestors", return_value=ancestors), \
                 mock.patch("ham.ledger.status.pane_shell_pid", return_value=10):
                with self.assertRaises(LedgerError):
                    self.registry.resolve_context({"HERDR_TERMINAL_ID": terminal}, self.live)

    def test_no_selector_uses_process_ancestry_not_inherited_pane(self):
        with mock.patch("ham.ledger.status.process_ancestors", return_value={20}), \
             mock.patch("ham.ledger.status.pane_shell_pid", side_effect=lambda pane, env: 20 if pane == "w1:p2" else 10):
            agent = self.registry.resolve_context({"HERDR_PANE_ID": "w1:p1"}, self.live)
        self.assertEqual(agent["terminal_id"], "term-b")

    def test_session_bound_registration_and_wire_export_do_not_use_caller_selector(self):
        with mock.patch("ham.ledger.status.process_ancestors", side_effect=AssertionError("internal explicit identity")):
            agent = self.registry.agent_session(terminal_id="term-b", session_ref="session-b", client="omp",
                                                repo_id=self.recipient["repo_id"])
            self.assertTrue(agent["extension_present"])
            self.assertEqual(len(RemoteLedger(self.store).status_export()["agents"]), 2)
        with self.assertRaises(LedgerError):
            self.registry.agent_session(terminal_id="term-b", session_ref="forged", client="omp",
                                        repo_id=self.recipient["repo_id"])

    def test_status_controls_whitespace_unicode_and_caps(self):
        text = "한글\n줄\r\n둘\t셋\x1b\x00\x7f\x85\u202e\u2066 café"
        self.assertEqual(sanitize_status_text(text, 200), "한글 줄 둘 셋 café")
        self.assertEqual(sanitize_status_text("가" * 300, 64), "가" * 64)
        with mock.patch.object(self.registry, "_refresh_display", return_value={}):
            result = self.registry.set_status(self.sender, phase=text * 10, summary=text * 20)
        self.assertLessEqual(len(result["phase"]), 64)
        self.assertLessEqual(len(result["summary"]), 200)
        with self.store.read() as connection:
            stored = connection.execute("SELECT phase,summary FROM agent_status WHERE agent_id=?",
                                        (self.sender["agent_id"],)).fetchone()
            log = connection.execute("SELECT value FROM status_log WHERE field IN ('phase','summary')").fetchall()
        self.assertEqual(tuple(stored), (result["phase"], result["summary"]))
        self.assertTrue(all("\x1b" not in row[0] and "\u202e" not in row[0] for row in log))

    def test_legacy_rows_are_sanitized_on_export_and_herdr_projection(self):
        with self.store.transaction() as connection:
            connection.execute("UPDATE agent_status SET phase=?,summary=?", ("\x1b坏\n" * 80, "\u202e한글\n" * 250))
        row = RemoteLedger(self.store).status_export()["agents"][0]
        self.assertEqual(row, sanitize_agent_status(row))
        self.assertLessEqual(len(row["phase"]), 64)
        self.assertLessEqual(len(row["summary"]), 200)
        projected = build_display("omp", "blue-owl", phase="\x1b단계\n다음", summary="\u202e진행\n중")
        self.assertEqual(projected.recap, "단계 다음")
        self.assertIn("진행 중", projected.label)
        self.assertNotIn("\u202e", " ".join(projected.metadata_command("pane")))

    def test_live_and_stale_peer_status_are_sanitized_without_protocol_change(self):
        peer = {"canonical_name": "beta", "uuid": str(uuid.uuid4())}
        row = {**self.sender, "host": "beta", "phase": "\x1b검토\n중", "summary": "\u202e상태" * 100,
               "unread": 0}
        with mock.patch.object(RemoteLedger, "peers", return_value=[peer]), \
             mock.patch.object(RemoteLedger, "status_export", return_value={"agents": [], "workspaces": []}), \
             mock.patch.object(RemoteLedger, "query_status", return_value={"agents": [row], "workspaces": []}):
            live = status_board(self.store)
        self.assertEqual(live["agents"][0]["phase"], "검토 중")
        previous = {"hosts": [{"host": "beta", "uuid": peer["uuid"]}], "agents": [row]}
        with mock.patch.object(RemoteLedger, "peers", return_value=[peer]), \
             mock.patch.object(RemoteLedger, "status_export", return_value={"agents": [], "workspaces": []}), \
             mock.patch.object(RemoteLedger, "query_status", side_effect=LedgerError("offline", "offline")):
            stale = status_board(self.store, previous=previous)
        self.assertEqual(stale["agents"][0]["phase"], "검토 중")
        self.assertTrue(stale["agents"][0]["stale"])
        self.assertLessEqual(len(stale["agents"][0]["summary"]), 200)


if __name__ == "__main__":
    unittest.main()
