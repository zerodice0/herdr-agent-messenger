import tests.ham_test_support as ham_test_support  # Protect the live ledger before product imports.
import argparse
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from ham.agents.output import compact_board_payload, compact_send_payload, send_advisories
from ham.ledger import cli as ledger_cli
from ham.ledger.wake_clients import WakeUnavailable
from tests.test_wake_claude import ClaudeFixture


class SendGuidanceTest(unittest.TestCase):
    def setUp(self):
        self.receipt = {"message_id": "m", "thread_id": "t", "recipient_address": "main",
                        "state": "queued", "evidence": None}
        self.repo = "git:github.com/org/project"
        self.other_repo = "git:github.com/org/other-project"

    def advised(self, *, body="Report", selector="main", recipient_repo=None):
        return send_advisories(self.receipt, body=body, selector=selector,
                               sender_repo=self.repo, recipient_repo=recipient_repo or self.repo)

    def test_ordinary_send_shape_is_unchanged(self):
        original = dict(self.receipt)
        result = self.advised()
        self.assertEqual(result, original)
        self.assertEqual(compact_send_payload(result), {
            "message_id": "m", "thread_id": "t", "recipient_address": "main", "state": "queued"})
        self.assertEqual(self.receipt, original)

    def test_body_hint_counts_utf8_bytes_and_preserves_receipt(self):
        self.assertNotIn("hint", self.advised(body="a" * 2048))
        self.assertNotIn("hint", self.advised(body="é" * 1024))
        for body in ("a" * 2049, "é" * 1024 + "a"):
            with self.subTest(body_bytes=len(body.encode("utf-8"))):
                result = self.advised(body=body)
                self.assertEqual(result["hint"], "body>2KB truncated in wakes")
                self.assertEqual(compact_send_payload(result)["hint"], result["hint"])
                self.assertLessEqual(self.json_size(result) - self.json_size(self.receipt), 40)
        self.assertNotIn("hint", self.receipt)

    def test_explicit_other_repo_main_has_short_resolved_repo(self):
        for selector in ("main@other-project", "beta/main@other-project",
                         "main@" + self.other_repo, "beta/main@" + self.other_repo):
            with self.subTest(selector=selector):
                result = self.advised(selector=selector, recipient_repo=self.other_repo)
                self.assertEqual(result["cross_repo"], "other-project")
                self.assertEqual(compact_send_payload(result)["cross_repo"], "other-project")
                self.assertLessEqual(self.json_size(result) - self.json_size(self.receipt), 40)

    def test_same_repo_explicit_main_has_no_cross_repo_hint(self):
        for selector in ("main@project", "beta/main@" + self.repo):
            self.assertNotIn("cross_repo", self.advised(selector=selector))

    def test_non_main_selector_has_no_cross_repo_hint(self):
        for selector in ("main", "beta/owl", "beta/not-main@other-project"):
            self.assertNotIn("cross_repo", self.advised(selector=selector, recipient_repo=self.other_repo))

    def test_both_hints_can_coexist(self):
        result = compact_send_payload(self.advised(body="a" * 2049, selector="main@other-project",
                                                   recipient_repo=self.other_repo))
        self.assertEqual(result["hint"], "body>2KB truncated in wakes")
        self.assertEqual(result["cross_repo"], "other-project")

    def dispatch(self, *, body=None, body_file=None, selector="main", recipient_repo=None):
        arguments = argparse.Namespace(command="mail", mail_command="send", to=selector,
                                       body=body, body_file=body_file, kind="fyi", reply_to=None, subject="")
        sender = {"repo_id": self.repo}
        recipient = {"repo_id": recipient_repo or self.repo}
        with mock.patch.object(ledger_cli, "AgentRegistry") as registry_class, \
                mock.patch.object(ledger_cli, "Mailbox") as mailbox_class, \
                mock.patch.object(ledger_cli, "RemoteLedger") as remote_class, \
                mock.patch.object(ledger_cli, "_live_context", return_value=sender):
            registry_class.return_value.refresh.return_value = ({}, {})
            remote_class.return_value.resolve_recipient.return_value = recipient
            mailbox_class.return_value.send.return_value = self.receipt
            result = ledger_cli._dispatch(arguments, {}, mock.sentinel.store)
            sent = mailbox_class.return_value.send.call_args
        self.assertEqual(sent.args, (sender, recipient))
        return result, sent.kwargs

    def test_cli_dispatch_adds_hints_without_altering_sent_body(self):
        body = "é" * 1025
        result, sent = self.dispatch(body=body, selector="beta/main@other-project",
                                     recipient_repo=self.other_repo)
        self.assertEqual(result["hint"], "body>2KB truncated in wakes")
        self.assertEqual(result["cross_repo"], "other-project")
        self.assertEqual(sent["body"], body)
        self.assertNotIn("hint", self.receipt)

    def test_cli_body_file_uses_decoded_utf8_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report.txt"
            path.write_text("é" * 1025, encoding="utf-8")
            result, sent = self.dispatch(body_file=str(path))
        self.assertEqual(result["hint"], "body>2KB truncated in wakes")
        self.assertEqual(sent["body"], "é" * 1025)

    def test_cli_dispatch_ordinary_receipt_is_unchanged(self):
        result, _ = self.dispatch(body="Decision first")
        self.assertEqual(result, self.receipt)

    def test_compact_board_preserves_optional_outbox_backlog(self):
        ordinary = {"agents": [], "hosts": [{"host": "alpha", "unread": 0}]}
        self.assertEqual(compact_board_payload(ordinary), ordinary)
        backlog = {"count": 3, "oldest_age_s": 61}
        result = compact_board_payload({"agents": [], "hosts": [
            {"host": "alpha", "unread": 0, "outbox_pending": backlog, "agents": []}]})
        self.assertEqual(result["hosts"], [{"host": "alpha", "unread": 0, "outbox_pending": backlog}])

    @staticmethod
    def json_size(value):
        return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


class ClaudeWarningTest(ClaudeFixture):
    def test_bypass_warning_does_not_recommend_accept(self):
        self.append({"type": "user", "permissionMode": "bypassPermissions"})
        with self.assertRaises(WakeUnavailable) as caught:
            self.discover()
        self.assertEqual(caught.exception.code, "claude_holds_peer_messages")
        self.assertIn('crossSessionInbound: "accept"', caught.exception.message)
        self.assertIn("any same-user socket writer", caught.exception.message)
        self.assertIn("do not enable it for bypass sessions", caught.exception.message)
        self.assertNotIn("set crossSessionInbound to", caught.exception.message)


if __name__ == "__main__":
    unittest.main()
