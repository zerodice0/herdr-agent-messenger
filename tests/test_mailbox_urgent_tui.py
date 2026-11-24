import tests.ham_test_support as ham_test_support  # Protect the live ledger before product imports.
import unittest

from ham.ui.mailbox import describe_message, execute_job
import tests.test_mailbox_tui as tui_fixture


class MailboxUrgentTuiTest(unittest.TestCase):
    """Operator amend (u) and cancel (c) of the operator's own mail in Threads."""

    # Reuse the isolated Mailbox UI fixture without re-running its test cases.
    setUp = tui_fixture.MailboxUiTest.setUp
    app = tui_fixture.MailboxUiTest.app
    accept = tui_fixture.MailboxUiTest.accept
    capture = tui_fixture.MailboxUiTest.capture
    type_text = tui_fixture.MailboxUiTest.type_text

    def select_thread(self, app, thread_id):
        app.focus = app.preview = "threads"
        items = app.items("threads")
        app.selections["threads"] = next(index for index, item in enumerate(items)
                                         if item.get("type") == "thread" and item["thread_id"] == thread_id)
        return app.selected("threads")

    def deliver(self, message_id):
        with self.store.transaction() as connection:
            connection.execute("UPDATE deliveries SET state='delivered' WHERE message_id=?", (message_id,))

    def test_cancel_key_confirms_then_runs_the_cancel_job(self):
        sent = self.mail.send_user(self.repo_id, self.recipient, body="Deploy to prod")
        app = self.app()
        self.select_thread(app, sent["thread_id"])
        app.key("c")
        self.assertEqual(app.mode, "confirm")
        self.assertEqual(app.confirm["operation"], "cancel")
        self.assertEqual(app.confirm["values"], {"message_id": sent["message_id"]})
        self.accept(app)
        self.assertEqual(app.jobs["mutation"].operation, "cancel")
        result = execute_job(app.jobs["mutation"].request, self.environment)
        self.assertEqual(result["cancelled"], "withdrawn")

    def test_cancel_of_delivered_operator_mail_sends_urgent_cancel(self):
        sent = self.mail.send_user(self.repo_id, self.recipient, body="Deploy to prod")
        self.deliver(sent["message_id"])
        result = execute_job({"operation": "cancel", "repo_id": self.repo_id,
                              "message_id": sent["message_id"]}, self.environment)
        self.assertEqual((result["urgency"], result["target_message_id"]), ("cancel", sent["message_id"]))
        thread = self.mail.show_thread(sent["thread_id"])["messages"]
        cancel = next(message for message in thread if message.get("urgency") == "cancel")
        self.assertIn("CANCEL", describe_message(cancel))

    def test_amend_key_opens_an_amend_compose_and_submits_the_amend_job(self):
        sent = self.mail.send_user(self.repo_id, self.recipient, body="Write the report")
        self.deliver(sent["message_id"])
        app = self.app()
        self.select_thread(app, sent["thread_id"])
        app.key("u")
        self.assertEqual(app.mode, "input")
        self.assertEqual((app.urgency, app.reply_to), ("amend", sent["message_id"]))
        self.assertIn("AMEND", "\n".join(self.capture(app, 24, 100).lines()))
        self.type_text(app, "Include numbers")
        app.key("\r")
        self.assertEqual(app.jobs["mutation"].operation, "amend")
        request = app.jobs["mutation"].request
        self.assertEqual((request["message_id"], request["body"]), (sent["message_id"], "Include numbers"))
        result = execute_job(request, self.environment)
        self.assertEqual(result["urgency"], "amend")

    def test_urgent_keys_require_operator_mail_in_the_selected_thread(self):
        sender = next(agent for agent in self.registry.list() if agent["terminal_id"] == "term-a")
        sent = self.mail.send(sender, self.recipient, kind="action", body="agent mail")
        app = self.app()
        self.select_thread(app, sent["thread_id"])
        for key in ("c", "u"):
            app.key(key)
            self.assertEqual(app.mode, "nav")
            self.assertEqual(app.status_level, "error")
        self.assertNotIn("mutation", app.jobs)


if __name__ == "__main__":
    unittest.main()
