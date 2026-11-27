import tests.ham_test_support as ham_test_support  # Protect the live ledger before product imports.
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from unittest import mock

from ham.agents.collaboration_policy import CollaborationPolicy
from ham.ledger.mail import Mailbox, payload_hash, validate_payload
from ham.ledger.runtime import NativeReceiver, render_bundle, wake_capable
from ham.ledger.settings import DEFAULT_THRESHOLDS, RepoSettings
from ham.ledger.status import AgentRegistry
from ham.ledger.store import LedgerError, LedgerStore
from tests.test_ledger_core import snapshot


def pi_snapshot(directory, *, recipient_session, **options):
    live = snapshot(directory, recipient_session=recipient_session, **options)
    for agent in live["agents"]:
        if agent["terminal_id"] == "term-b":
            agent["agent"] = "pi"
    return live


class PiMailboxLedgerTest(unittest.TestCase):
    def setUp(self):
        environment = mock.patch.dict("os.environ")
        environment.start().pop("NO_COLOR", None)
        self.addCleanup(environment.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.session = str(self.root / "pi-session.jsonl")
        self.store = LedgerStore(self.root / "ledger", canonical_name="alpha")
        self.registry = AgentRegistry(self.store)
        self.registry.reconcile(pi_snapshot(self.repo, recipient_session=self.session), refresh_display=False)
        agents = self.registry.list()
        self.sender = next(a for a in agents if a["terminal_id"] == "term-a")
        self.recipient = next(a for a in agents if a["terminal_id"] == "term-b")
        self.mail = Mailbox(self.store, {"HERDR_AGENT_LEDGER_AUTOSTART": "0"})
        self.receiver = NativeReceiver(self.store)
        self.environment = {**os.environ, "HERDR_SOCKET_PATH": str(self.root / "herdr-isolated.sock"),
                            "HERDR_AGENT_LEDGER_AUTOSTART": "0"}
        self.settings = RepoSettings(self.store, environment=self.environment, display_runner=self.display_runner)
        self.thresholds = {**DEFAULT_THRESHOLDS, "idle_minutes": 0, "coalesce_seconds": 0}
        self.settings.mutate(self.recipient["repo_id"], "thresholds", self.thresholds)
        self.settings.mutate(self.recipient["repo_id"], "mode", "auto")
        self.settings.mutate(self.recipient["repo_id"], "main", {"host": "alpha", "terminal_id": "term-a"},
                             host="alpha", workspace_id="w1")
        registered = self.receiver.register(self.recipient, session_ref=self.session, client="pi")
        self.recipient = {**self.recipient, **registered["agent"]}
        self.identity = {key: self.recipient[key] for key in ("terminal_id", "agent_id", "generation")}

    @staticmethod
    def display_runner(command):
        return subprocess.CompletedProcess(list(command), 1, "", "isolated fixture")

    # helpers ---------------------------------------------------------------------------------
    def send(self, kind="action", body="Implement the feature", **options):
        return self.mail.send(self.sender, self.recipient, kind=kind, body=body, **options)

    def state(self, message_id):
        with self.store.read() as connection:
            return connection.execute("SELECT state FROM deliveries WHERE message_id=?", (message_id,)).fetchone()[0]

    def message(self, message_id):
        with self.store.read() as connection:
            return dict(connection.execute("SELECT m.*,d.state,d.evidence FROM messages m JOIN deliveries d "
                                           "USING(message_id) WHERE message_id=?", (message_id,)).fetchone())

    def deliver(self, message_id):
        claim = self.receiver.claim(**self.identity)
        self.assertIn(message_id, [row["message_id"] for row in claim["messages"]])
        self.applied(claim)
        self.assertEqual(self.state(message_id), "delivered")
        return claim

    def applied(self, claim):
        data = {key: claim[key] for key in ("claim_id", "agent_id", "generation", "session_ref", "marker", "content_sha256")}
        return self.receiver.applied(**data, entry_id="pi-entry-1")

    def set_turn_state(self, state):
        with self.store.transaction() as connection:
            connection.execute("UPDATE agent_status SET turn_state=? WHERE agent_id=?", (state, self.recipient["agent_id"]))

    # registration ----------------------------------------------------------------------------
    def test_pi_registers_with_its_own_client_and_never_as_omp(self):
        self.assertEqual(self.recipient["client"], "pi")
        self.assertTrue(self.recipient["extension_present"])
        self.assertTrue(wake_capable(self.recipient))
        with self.assertRaises(LedgerError) as caught:
            self.receiver.register(self.recipient, session_ref=self.session, client="omp")
        self.assertEqual(caught.exception.code, "unsupported_client")

    def test_unreported_pi_session_waits_for_herdr_report(self):
        fresh = LedgerStore(self.root / "fresh", canonical_name="alpha")
        registry = AgentRegistry(fresh)
        live = pi_snapshot(self.repo, recipient_session=self.session)
        live["agents"][1].pop("agent_session")
        registry.reconcile(live, refresh_display=False)
        recipient = next(a for a in registry.list() if a["terminal_id"] == "term-b")
        with self.assertRaises(LedgerError) as caught:
            NativeReceiver(fresh).register(recipient, session_ref=self.session, client="pi")
        self.assertEqual(caught.exception.code, "native_session_pending")

    def test_worker_never_wakes_pi_and_pi_claim_acks_with_pi_evidence(self):
        sent = self.send()
        claim = self.receiver.claim(**self.identity, wake=True)
        self.assertEqual(claim["via"], "wake")
        self.applied(claim)
        message = self.message(sent["message_id"])
        self.assertEqual(message["state"], "delivered")
        self.assertTrue(message["evidence"].startswith("pi:accepted-journal:" + self.session + "#pi-entry-1"))
        with self.store.read() as connection:
            self.assertEqual(connection.execute("SELECT client FROM native_claims WHERE claim_id=?",
                                                (claim["claim_id"],)).fetchone()[0], "pi")

    # payload/protocol --------------------------------------------------------------------------
    def test_urgency_is_optional_on_the_wire_and_ordinary_payload_hash_is_unchanged(self):
        sent = self.send()
        ordinary = self.message(sent["message_id"])
        self.assertIsNone(ordinary["urgency"])
        base = {"message_id": str(uuid.uuid4()), "thread_id": str(uuid.uuid4()), "repo_id": "r", "reply_to": None,
                "origin_host": "alpha", "sender": "a", "sender_label": "a", "kind": "action", "hop": 0, "body": "b",
                "subject": "", "created_at": 1.0, "recipient_host": "alpha", "recipient_terminal_id": "term-b",
                "recipient_address": "alpha/b", "sender_repo_id": "r", "recipient_repo_id": "r"}
        self.assertEqual(payload_hash(validate_payload(base)), payload_hash(base))
        for urgency, kind, reply_to, code in (("steer", "action", base["message_id"], "invalid_urgency"),
                                              ("cancel", "fyi", base["message_id"], "invalid_urgency"),
                                              ("amend", "action", None, "invalid_urgency")):
            with self.subTest(urgency=urgency, kind=kind):
                with self.assertRaises(LedgerError) as caught:
                    validate_payload({**base, "urgency": urgency, "kind": kind, "reply_to": reply_to})
                self.assertEqual(caught.exception.code, code)
        legacy = {key: value for key, value in base.items() if key not in {"sender_repo_id", "recipient_repo_id"}}
        with self.assertRaises(LedgerError):
            validate_payload({**legacy, "urgency": "amend", "reply_to": base["message_id"]})

    def test_bundle_labels_urgent_messages_and_adds_handling_guidance(self):
        marker = "HAM_CLAIM_" + "0" * 36 + "_" + "0" * 32
        base = {"origin_host": "alpha", "sender_label": "main", "kind": "action",
                "thread_id": "00000000-0000-4000-8000-000000000000", "hop": 1, "body": "stop"}
        content = render_bundle([{**base, "message_id": "00000001-0000-4000-8000-000000000000",
                                  "urgency": "cancel", "reply_to": "00000009-0000-4000-8000-000000000000"}], marker)
        self.assertIn("[action · cancel of 00000009-0000-4000-8000-000000000000]", content)
        self.assertIn("CANCEL: the sender cancelled the referenced message.", content)
        self.assertNotIn("AMEND:", content)
        ordinary = render_bundle([{**base, "message_id": "00000001-0000-4000-8000-000000000000"}], marker)
        self.assertNotIn("CANCEL", ordinary)
        self.assertIn("From alpha/main [action]\n", ordinary)

    # amend/cancel authorization -------------------------------------------------------------
    def test_main_amends_its_own_message_on_the_same_thread_and_route(self):
        original = self.send()
        result = self.mail.urgent(self.sender, original["message_id"], urgency="amend", body="Also add tests")
        amend = self.message(result["message_id"])
        self.assertEqual((amend["urgency"], amend["kind"], amend["reply_to"]), ("amend", "action", original["message_id"]))
        self.assertEqual(amend["thread_id"], original["thread_id"])
        self.assertEqual(amend["hop"], 1)
        self.assertEqual(result["target_message_id"], original["message_id"])
        with self.store.read() as connection:
            route = connection.execute("SELECT recipient_terminal_id FROM deliveries WHERE message_id=?",
                                       (result["message_id"],)).fetchone()[0]
        self.assertEqual(route, "term-b")
        self.assertEqual(self.state(original["message_id"]), "queued")

    def test_only_main_or_user_may_send_urgent_mail_and_only_for_their_own_mail(self):
        reply = self.mail.send(self.recipient, self.sender, kind="action", body="worker mail")
        with self.assertRaises(LedgerError) as caught:
            self.mail.urgent(self.recipient, reply["message_id"], urgency="amend", body="more")
        self.assertEqual(caught.exception.code, "not_main")
        original = self.send()
        for sender in (self.recipient, None):
            with self.subTest(sender=sender and sender["terminal_id"]):
                with self.assertRaises(LedgerError) as caught:
                    self.mail.urgent(sender, original["message_id"], urgency="cancel")
                self.assertEqual(caught.exception.code, "not_sender")
        self.assertEqual(self.state(original["message_id"]), "queued")
        amend = self.mail.urgent(self.sender, original["message_id"], urgency="amend", body="x")
        with self.assertRaises(LedgerError) as caught:
            self.mail.urgent(self.sender, amend["message_id"], urgency="cancel")
        self.assertEqual(caught.exception.code, "invalid_urgency")
        with self.assertRaises(LedgerError) as caught:
            self.mail.urgent(self.sender, original["message_id"], urgency="amend", body="  ")
        self.assertEqual(caught.exception.code, "invalid_body")

    def test_direct_send_with_urgency_rechecks_ownership_and_recipient(self):
        original = self.send()
        other = self.mail.send(self.recipient, self.sender, kind="action", body="to main")
        with self.assertRaises(LedgerError) as caught:
            self.mail.send(self.sender, self.recipient, kind="action", body="x", reply_to=other["message_id"],
                           urgency="amend")
        self.assertIn(caught.exception.code, {"not_sender", "invalid_recipient"})
        with self.assertRaises(LedgerError) as caught:
            self.mail.send(self.sender, self.sender, kind="action", body="x", reply_to=original["message_id"],
                           urgency="amend")
        self.assertEqual(caught.exception.code, "invalid_recipient")

    def test_cancel_of_queued_mail_only_withdraws_and_sends_nothing(self):
        original = self.send()
        result = self.mail.urgent(self.sender, original["message_id"], urgency="cancel")
        self.assertEqual(result["cancelled"], "withdrawn")
        self.assertEqual(self.state(original["message_id"]), "withdrawn")
        with self.store.read() as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM messages WHERE urgency IS NOT NULL").fetchone()[0], 0)
        again = self.mail.urgent(self.sender, original["message_id"], urgency="cancel")
        self.assertEqual(again["cancelled"], "withdrawn")
        with self.assertRaises(LedgerError) as caught:
            self.mail.urgent(self.sender, original["message_id"], urgency="amend", body="late")
        self.assertEqual(caught.exception.code, "nothing_to_cancel")

    def test_cancel_of_delivered_or_handled_mail_sends_an_urgent_cancel(self):
        original = self.send()
        self.deliver(original["message_id"])
        result = self.mail.urgent(self.sender, original["message_id"], urgency="cancel", body="Wrong target")
        cancel = self.message(result["message_id"])
        self.assertEqual((cancel["urgency"], cancel["body"], cancel["state"]), ("cancel", "Wrong target", "queued"))
        self.assertEqual(self.state(original["message_id"]), "delivered")
        self.mail.handle(self.recipient, [original["message_id"]])
        default = self.mail.urgent(self.sender, original["message_id"], urgency="cancel")
        self.assertIn("cancelled message " + original["message_id"], self.message(default["message_id"])["body"])

    def test_operator_cancels_and_amends_only_operator_mail(self):
        original = self.mail.send_user(self.recipient["repo_id"], self.recipient, body="user task")
        self.deliver(original["message_id"])
        cancel = self.mail.urgent(None, original["message_id"], urgency="cancel")
        self.assertEqual(self.message(cancel["message_id"])["sender"], "user")
        amend = self.mail.urgent(None, original["message_id"], urgency="amend", body="details")
        self.assertEqual(self.message(amend["message_id"])["urgency"], "amend")
        with self.assertRaises(LedgerError) as caught:
            self.mail.urgent(self.sender, original["message_id"], urgency="cancel")
        self.assertEqual(caught.exception.code, "not_sender")

    def test_undeliverable_original_cannot_be_cancelled(self):
        original = self.send()
        with self.store.transaction() as connection:
            connection.execute("UPDATE deliveries SET state='undeliverable' WHERE message_id=?", (original["message_id"],))
        with self.assertRaises(LedgerError) as caught:
            self.mail.urgent(self.sender, original["message_id"], urgency="cancel")
        self.assertEqual(caught.exception.code, "message_final")

    # urgent inspection and claims --------------------------------------------------------------
    def test_inspect_lists_urgent_items_and_a_view_only_inbox_preview(self):
        original = self.send(body="line one\nline two\x07")
        self.deliver(original["message_id"])
        fyi = self.send(kind="fyi", body="note")
        amend = self.mail.urgent(self.sender, original["message_id"], urgency="amend", body="supplement")
        state = self.receiver.inspect(**self.identity)
        self.assertEqual(state["urgent"], [{"message_id": amend["message_id"], "urgency": "amend",
                                            "target_message_id": original["message_id"],
                                            "thread_id": original["thread_id"]}])
        self.assertEqual([item["message_id"] for item in state["inbox"]], [fyi["message_id"], amend["message_id"]])
        self.assertEqual(state["queued_total"], 2)
        self.assertEqual(state["inbox"][1]["urgency"], "amend")
        self.assertEqual(state["inbox"][0]["sender"], "alpha/orange-rabbit")
        self.assertEqual(self.state(fyi["message_id"]), "queued")

    def test_urgent_claim_skips_turn_coalesce_and_hourly_gates_but_not_hop(self):
        original = self.send()
        self.deliver(original["message_id"])
        self.set_turn_state("working")
        self.settings.mutate(self.recipient["repo_id"], "thresholds",
                             {**self.thresholds, "coalesce_seconds": 600, "wakes_per_hour": 0})
        ordinary = self.send(kind="action", body="unrelated")
        amend = self.mail.urgent(self.sender, original["message_id"], urgency="amend", body="more")
        self.assertEqual(self.receiver.claim(**self.identity, wake=True)["reason"], "not_idle")
        claim = self.receiver.claim(**self.identity, urgent=["amend"])
        self.assertEqual(claim["via"], "urgent")
        self.assertEqual([row["message_id"] for row in claim["messages"]], [amend["message_id"]])
        self.assertIn("AMEND:", claim["message"]["content"])
        self.applied(claim)
        self.assertEqual(self.state(ordinary["message_id"]), "queued")
        with self.store.read() as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM wake_log").fetchone()[0], 0)
        self.settings.mutate(self.recipient["repo_id"], "thresholds", {**self.thresholds, "max_hop": 0})
        self.mail.urgent(self.sender, original["message_id"], urgency="cancel")
        self.assertEqual(self.receiver.claim(**self.identity, urgent=["cancel"])["reason"], "empty")
        self.assertEqual(self.receiver.inspect(**self.identity)["urgent"], [])

    def test_urgent_claim_brings_a_still_queued_original_and_can_be_narrowed(self):
        first = self.send(body="first")
        second = self.send(body="second")
        amend_first = self.mail.urgent(self.sender, first["message_id"], urgency="amend", body="first+")
        amend_second = self.mail.urgent(self.sender, second["message_id"], urgency="amend", body="second+")
        claim = self.receiver.claim(**self.identity, urgent=["amend"], message_ids=[amend_second["message_id"]])
        self.assertEqual([row["message_id"] for row in claim["messages"]],
                         [second["message_id"], amend_second["message_id"]])
        self.assertEqual(self.state(first["message_id"]), "queued")
        self.assertEqual(self.state(amend_first["message_id"]), "queued")
        for invalid in ({"urgent": ["steer"]}, {"urgent": "amend"}, {"urgent": []},
                        {"urgent": ["amend"], "wake": True}, {"message_ids": [first["message_id"]]},
                        {"urgent": ["amend"], "message_ids": ["not-a-uuid"]}):
            with self.subTest(invalid=invalid):
                with self.assertRaises(LedgerError):
                    self.receiver.claim(**self.identity, **invalid)

    def test_cancel_whose_original_was_withdrawn_is_settled_without_delivery(self):
        original = self.send()
        # A cross-host cancel can arrive after its FIFO withdrawal won; emulate that ordering locally.
        cancel = self.mail.send(self.sender, self.recipient, kind="action", body="cancel",
                                reply_to=original["message_id"], urgency="cancel")
        self.mail.withdraw(original["message_id"], sender=self.sender)
        self.assertEqual(self.receiver.inspect(**self.identity)["urgent"], [])
        self.assertEqual(self.receiver.claim(**self.identity, urgent=["cancel"])["claim_id"], None)
        settled = self.message(cancel["message_id"])
        self.assertEqual((settled["state"], settled["evidence"]), ("handled", "cancel:target_withdrawn"))

    # pi observed idle ------------------------------------------------------------------------
    def observed(self, age=0.0):
        return {"observed_idle": True, "observed_at": time.time() - age}

    def wake_log_count(self):
        with self.store.read() as connection:
            return connection.execute("SELECT count(*) FROM wake_log WHERE agent_id=?",
                                      (self.recipient["agent_id"],)).fetchone()[0]

    def test_inspect_advertises_pi_observed_idle(self):
        self.assertIn("pi_observed_idle", self.receiver.inspect(**self.identity)["capabilities"])

    def test_fresh_observed_idle_replaces_only_the_turn_state_gate(self):
        sent = self.send()
        for state in ("working", "unknown", "blocked"):
            with self.subTest(state=state):
                self.set_turn_state(state)
                self.assertEqual(self.receiver.claim(**self.identity, wake=True)["reason"], "not_idle")
        claim = self.receiver.claim(**self.identity, wake=True, **self.observed(age=30))
        self.assertEqual((claim["via"], claim["idle_source"]), ("wake", "pi_observed_idle"))
        self.assertEqual([row["message_id"] for row in claim["messages"]], [sent["message_id"]])
        self.applied(claim)
        self.assertEqual(self.state(sent["message_id"]), "delivered")
        self.assertEqual(self.wake_log_count(), 1)
        self.set_turn_state("idle")
        self.send(body="second")
        plain = self.receiver.claim(**self.identity, wake=True)
        self.assertEqual(plain["idle_source"], "turn_state")
        self.assertNotIn("idle_source", self.receiver.claim(**self.identity))

    def test_observed_idle_keeps_role_hourly_coalescing_and_hop_gates(self):
        self.set_turn_state("working")
        sent = self.send()
        self.settings.mutate(self.recipient["repo_id"], "thresholds", {**self.thresholds, "wakes_per_hour": 0})
        self.assertEqual(self.receiver.claim(**self.identity, wake=True, **self.observed())["reason"], "hourly_limit")
        self.settings.mutate(self.recipient["repo_id"], "thresholds", {**self.thresholds, "coalesce_seconds": 600})
        self.assertEqual(self.receiver.claim(**self.identity, wake=True, **self.observed())["reason"], "coalescing")
        self.settings.mutate(self.recipient["repo_id"], "thresholds", self.thresholds)
        with self.store.transaction() as connection:
            connection.execute("UPDATE messages SET hop=? WHERE message_id=?",
                               (self.thresholds["max_hop"] + 1, sent["message_id"]))
        self.assertEqual(self.receiver.claim(**self.identity, wake=True, **self.observed())["reason"], "no_action")
        with self.store.transaction() as connection:
            connection.execute("UPDATE messages SET hop=0 WHERE message_id=?", (sent["message_id"],))
        self.settings.mutate(self.recipient["repo_id"], "mode", "off")
        self.assertEqual(self.receiver.claim(**self.identity, wake=True, **self.observed())["reason"],
                         "not_participating")
        self.assertEqual(self.wake_log_count(), 0)

    def test_stale_or_future_observation_refuses_without_turn_state_fallback(self):
        sent = self.send()
        self.set_turn_state("idle")
        for age in (61, -6):
            with self.subTest(age=age):
                claim = self.receiver.claim(**self.identity, wake=True, **self.observed(age=age))
                self.assertEqual((claim["claim_id"], claim["reason"]), (None, "observed_stale"))
        self.assertEqual(self.state(sent["message_id"]), "queued")

    def test_invalid_observed_idle_requests_are_rejected(self):
        self.send()
        now = time.time()
        for invalid, code in (({"wake": True, "observed_idle": False, "observed_at": now}, "invalid_request"),
                              ({"wake": True, "observed_idle": True}, "invalid_request"),
                              ({"wake": True, "observed_at": now}, "invalid_request"),
                              ({"wake": True, "observed_idle": True, "observed_at": "now"}, "invalid_request"),
                              ({"wake": True, "observed_idle": True, "observed_at": True}, "invalid_request"),
                              ({"wake": True, "observed_idle": True, "observed_at": float("nan")}, "invalid_request"),
                              ({"observed_idle": True, "observed_at": now}, "invalid_request"),
                              ({"urgent": ["amend"], "observed_idle": True, "observed_at": now}, "invalid_request")):
            with self.subTest(invalid=invalid):
                with self.assertRaises(LedgerError) as caught:
                    self.receiver.claim(**self.identity, **invalid)
                self.assertEqual(caught.exception.code, code)
        with self.assertRaises(LedgerError) as caught:
            NativeReceiver._observed_idle(True, now, wake=True, client="omp", now=now)
        self.assertEqual(caught.exception.code, "unsupported_client")

    def test_receiver_capability_by_client(self):
        self.assertTrue(wake_capable({"client": "omp", "extension_present": 1}))
        self.assertFalse(wake_capable({"client": "pi", "extension_present": 0}))


    def test_actual_cli_register_inspect_and_urgent_claim_for_pi(self):
        original = self.send()
        self.deliver(original["message_id"])
        cancel = self.mail.urgent(self.sender, original["message_id"], urgency="cancel")
        live = pi_snapshot(self.repo, recipient_session=self.session)
        herdr = self.root / "herdr"
        herdr.write_text(
            "#!" + sys.executable + "\n"
            "import json, sys\n"
            f"live = {live!r}\n"
            "args = sys.argv[1:]\n"
            "if args == ['api', 'snapshot']:\n"
            "    print(json.dumps({'result':{'snapshot':live}}))\n"
            "elif args[:2] == ['pane', 'process-info']:\n"
            "    pane = args[args.index('--pane') + 1]\n"
            f"    pid = {os.getpid()} if pane in ('w1:p1', 'w1:p2') else 1\n"
            "    print(json.dumps({'result':{'process_info':{'pane_id':pane,'shell_pid':pid}}}))\n"
            "else:\n"
            "    raise SystemExit(2)\n", encoding="utf-8")
        herdr.chmod(0o700)
        environment = {**self.environment, "HERDR_BIN_PATH": str(herdr), "HERDR_TERMINAL_ID": "term-b",
                       "HERDR_PANE_ID": "w1:p2", "HERDR_AGENT_LEDGER_DIR": str(self.root / "ledger")}
        root = Path(__file__).resolve().parents[1]

        def call(*arguments, data=None):
            completed = subprocess.run([sys.executable, "-m", "ham", "cli", *arguments], cwd=root,
                                       input=json.dumps(data or {}), text=True, capture_output=True, env=environment)
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
            return json.loads(completed.stdout)

        registered = call("ledger", "runtime", "register", data={"session_ref": self.session, "client": "pi"})
        self.assertEqual(registered["agent"]["client"], "pi")
        self.assertEqual(registered["urgent"][0]["message_id"], cancel["message_id"])
        state = call("ledger", "runtime", "inspect", data=self.identity)
        self.assertEqual(state["urgent"][0]["urgency"], "cancel")
        self.assertIn("pi_observed_idle", state["capabilities"])
        claim = call("ledger", "runtime", "claim", data={**self.identity, "urgent": ["cancel"]})
        self.assertEqual(claim["via"], "urgent")
        self.assertEqual([row["message_id"] for row in claim["messages"]], [cancel["message_id"]])
        self.assertIn("CANCEL:", claim["message"]["content"])

        main = {**environment, "HERDR_TERMINAL_ID": "term-a", "HERDR_PANE_ID": "w1:p1"}
        follow = self.send(body="second task")
        self.deliver(follow["message_id"])
        for command, expected in ((["mail", "amend", "--message", follow["message_id"], "--body", "detail"], "amend"),
                                  (["mail", "cancel", "--message", follow["message_id"]], "cancel")):
            completed = subprocess.run([sys.executable, "-m", "ham", "cli", *command], cwd=root, text=True,
                                       capture_output=True, env=main)
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
            payload = json.loads(completed.stdout)
            self.assertEqual((payload["urgency"], payload["target_message_id"]), (expected, follow["message_id"]))
            self.assertEqual(self.message(payload["message_id"])["urgency"], expected)
        queued = self.send(body="third task")
        completed = subprocess.run([sys.executable, "-m", "ham", "cli", "mail", "cancel", "--message",
                                    queued["message_id"]], cwd=root, text=True, capture_output=True, env=main)
        self.assertEqual(json.loads(completed.stdout), {"message_id": queued["message_id"], "cancelled": "withdrawn",
                                                        "state": "withdrawn"})


class PiLegacyPolicyTest(unittest.TestCase):
    def test_registered_pi_receiver_refuses_legacy_prompts_across_repos(self):
        policy = CollaborationPolicy.__new__(CollaborationPolicy)
        policy.active = True
        policy.sender = {"repo_id": "repo-a"}
        policy._error = None
        recipient = mock.Mock(qualified_name="alpha/blue-owl")
        policy._target = lambda _recipient: {"repo_id": "repo-b", "role": None, "client": "pi", "extension_present": 1}
        with self.assertRaises(LedgerError) as caught:
            policy.check(recipient)
        self.assertEqual(caught.exception.code, "use_mailbox")
        policy._error = None
        policy._target = lambda _recipient: {"repo_id": "repo-b", "role": None, "client": "pi", "extension_present": 0}
        policy.check(recipient)


class PiCliTest(unittest.TestCase):
    def test_mail_amend_and_cancel_parsers(self):
        import argparse
        from ham.ledger import cli as ledger_cli
        parser = argparse.ArgumentParser()
        ledger_cli.add_parsers(parser.add_subparsers(dest="command"))
        message = str(uuid.uuid4())
        amend = parser.parse_args(["mail", "amend", "--message", message, "--body", "more"])
        self.assertEqual((amend.mail_command, amend.message, amend.body), ("amend", message, "more"))
        cancel = parser.parse_args(["mail", "cancel", "--message", message])
        self.assertEqual((cancel.mail_command, cancel.body), ("cancel", None))


if __name__ == "__main__":
    unittest.main()
