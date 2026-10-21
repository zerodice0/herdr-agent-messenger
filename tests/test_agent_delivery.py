#!/usr/bin/env python3

import tests.ham_test_support as ham_test_support  # Protect the live ledger before product imports.
from contextlib import ExitStack, contextmanager, nullcontext, redirect_stdout
import importlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from ham.agents import delivery as agent_delivery
from ham.agents.request_lifecycle import JsonRequestStateStore, RequestLifecycleMachine, OutputSnapshot, SubmissionResult


class MemoryStore:
    def __init__(self, records=()):
        self.records = {record["request_id"]: record for record in records}
        self.deliveries = []
        self.cancelled = []

    def load(self, request_id):
        return self.records.get(request_id)

    def save(self, request_id, record):
        self.records[request_id] = record

    def list_request_ids(self):
        return list(self.records)

    def lock(self, request_id):
        return nullcontext()

    def pending(self):
        return [record for record in self.records.values()
                if record.get("state") in {"deferred", "awaiting_decision"}]

    def set_decision(self, request_id, decision):
        record = self.records[request_id]
        if record["state"] in {"deferred", "awaiting_decision"}:
            record["_defer"]["decision"] = decision
        return record


class FakeMachine:
    def __init__(self, transport, store, **kwargs):
        self.transport = transport
        self.store = store

    def deliver_deferred(self, request_id, *, force=False):
        record = self.store.records[request_id]
        self.store.deliveries.append((request_id, force))
        if record.get("broken"):
            raise ValueError("invalid record")
        if force:
            record["state"] = "submitted_unknown"
        elif record.get("expire"):
            record["state"] = "awaiting_decision"
        return record

    def cancel_deferred(self, request_id):
        record = self.store.records[request_id]
        self.store.cancelled.append(request_id)
        if record["state"] in {"deferred", "awaiting_decision"}:
            record.update(state="submission_failed", phase="delivery_cancelled", submitted=False,
                          terminal=True, error_code="delivery_cancelled")
        return record


def held_record(number=1, *, state="deferred", remote=False, decision=None):
    return {
        "request_id": f"{number:032x}",
        "state": state,
        "sender": {"host": "sender", "name": "writer", "address": "sender/writer"},
        "recipient": {"host": "remote" if remote else "sender", "name": "reader",
                      "local": not remote},
        "_context": {"host": "remote" if remote else "local", "label": "reader"},
        "_prompt": "Message from sender/writer:\n\nReview the change.\nNext line.",
        "_defer": {"since": 60.0, "deadline": 240.0, "timeout_ms": 180000, "decision": decision},
        "defer": {"since": "1970-01-01T00:01:00Z", "deadline": "1970-01-01T00:04:00Z",
                  "reason": "recipient_focused", "notified": None},
    }


class AgentDeliveryTest(unittest.TestCase):
    def test_module_import_does_not_require_skill_cli(self):
        with mock.patch.dict(sys.modules, {"ham.cli": None}):
            importlib.reload(agent_delivery)

    def patch_queue(self, store):
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(mock.patch.object(agent_delivery, "open_request_store", return_value=store))
        stack.enter_context(mock.patch.object(agent_delivery, "pending_deferred", side_effect=lambda queue: queue.pending()))
        stack.enter_context(mock.patch.object(agent_delivery, "RequestLifecycleMachine", FakeMachine))
        stack.enter_context(mock.patch.object(
            agent_delivery, "set_deferred_decision",
            side_effect=lambda queue, request_id, decision: queue.set_decision(request_id, decision),
        ))

    def test_ensure_worker_does_not_spawn_when_lock_is_held(self):
        with tempfile.TemporaryDirectory() as directory:
            environment = {"HERDR_AGENT_REQUEST_STATE_DIR": directory}
            spawn = mock.Mock()
            with agent_delivery._worker_lock(environment) as acquired:
                self.assertTrue(acquired)
                self.assertFalse(agent_delivery.ensure_worker(environment, popen_factory=spawn))
            spawn.assert_not_called()

    def test_ensure_worker_spawns_fully_detached_and_releases_probe_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            environment = {"HERDR_AGENT_REQUEST_STATE_DIR": directory}
            spawn = mock.Mock()
            self.assertTrue(agent_delivery.ensure_worker(environment, popen_factory=spawn))
            arguments, options = spawn.call_args
            self.assertEqual(arguments[0][1:], ["-m", "ham", "delivery", "worker"])
            self.assertTrue(options["start_new_session"])
            self.assertTrue(options["close_fds"])
            self.assertEqual(options["cwd"], str(Path(__file__).resolve().parents[1]))
            self.assertEqual(options["env"]["HERDR_AGENT_REQUEST_STATE_DIR"], directory)
            for stream in ("stdin", "stdout", "stderr"):
                self.assertEqual(options[stream], subprocess.DEVNULL)
            with agent_delivery._worker_lock(environment) as acquired:
                self.assertTrue(acquired)

    def test_push_decision_forces_delivery_while_deferred_is_unforced(self):
        pushing = held_record(1, state="awaiting_decision", decision="push")
        waiting = held_record(2)
        store = MemoryStore([pushing, waiting])
        self.patch_queue(store)
        transport_factory = mock.Mock(return_value=object())
        delay = agent_delivery.worker_tick(store, {}, transport_factory=transport_factory)
        self.assertEqual(store.deliveries, [(pushing["request_id"], True), (waiting["request_id"], False)])
        self.assertEqual(pushing["state"], "submitted_unknown")
        self.assertEqual(delay, 1.0)
        self.assertEqual(transport_factory.call_count, 2)

    def test_newly_awaiting_notifies_once_on_local_and_recipient_hosts(self):
        record = held_record(remote=True)
        record["expire"] = True
        store = MemoryStore([record])
        self.patch_queue(store)
        commands = []

        def runner(command):
            commands.append(command)
            reason = "shown" if len(commands) == 1 else "no_foreground_client"
            return subprocess.CompletedProcess(command, 0, json.dumps({"result": {"reason": reason}}), "")

        with (
            mock.patch.object(agent_delivery, "local_host_name", return_value="sender-machine"),
            mock.patch.object(agent_delivery, "herdr_executable", return_value="/fake/herdr"),
        ):
            delay = agent_delivery.worker_tick(
                store, {"HERDR_AGENT_MESSENGER_SSH_CONFIG": "/fake/config"},
                transport_factory=lambda record, environment: object(), command_runner=runner, now=lambda: 240.0,
            )
            agent_delivery.worker_tick(store, {}, command_runner=runner)
        self.assertEqual(delay, 3.0)
        self.assertEqual(len(commands), 2)
        self.assertEqual(commands[0][:4], ["/fake/herdr", "notification", "show", "HAM: Held Messages"])
        self.assertEqual(commands[1][0], "ssh")
        self.assertEqual(commands[1][-2], "remote")
        self.assertIn("held 3 min", commands[0][-1])
        self.assertIn("sender-machine", commands[1][-1])
        self.assertEqual(record["defer"]["notified"], {"local": "shown", "remote": "no_foreground_client"})

    def test_awaiting_local_request_notifies_without_constructing_transport(self):
        record = held_record(state="awaiting_decision")
        store = MemoryStore([record])
        self.patch_queue(store)
        factory = mock.Mock(side_effect=AssertionError("no delivery needed"))
        runner = mock.Mock(return_value=subprocess.CompletedProcess([], 0, '{"result":{"reason":"disabled"}}', ""))
        delay = agent_delivery.worker_tick(store, {}, transport_factory=factory, command_runner=runner)
        self.assertEqual(delay, 1.0)
        factory.assert_not_called()
        self.assertEqual(runner.call_count, 1)
        self.assertEqual(record["defer"]["notified"], {"local": "disabled"})

    def test_notification_failure_does_not_repeat_or_prevent_remote_attempt(self):
        record = held_record(remote=True, state="awaiting_decision")
        store = MemoryStore([record])
        self.patch_queue(store)
        runner = mock.Mock(side_effect=[OSError("unavailable"), subprocess.CompletedProcess(
            [], 0, '{"result":{"reason":"rate_limited"}}', "",
        )])
        agent_delivery.worker_tick(store, {}, command_runner=runner)
        agent_delivery.worker_tick(store, {}, command_runner=runner)
        self.assertEqual(runner.call_count, 2)
        self.assertEqual(record["defer"]["notified"], {"local": "unavailable", "remote": "rate_limited"})

    def test_one_bad_record_does_not_stop_other_deliveries(self):
        broken = held_record(1)
        broken["broken"] = True
        good = held_record(2, decision="push")
        store = MemoryStore([broken, good])
        self.patch_queue(store)
        delay = agent_delivery.worker_tick(store, {}, transport_factory=lambda record, environment: object())
        self.assertEqual(good["state"], "submitted_unknown")
        self.assertEqual(delay, 1.0)
        self.assertEqual(len(store.deliveries), 2)

    def test_remote_pending_recipient_uses_longer_poll_interval(self):
        store = MemoryStore([held_record(remote=True)])
        self.patch_queue(store)
        self.assertEqual(agent_delivery.worker_tick(
            store, {}, transport_factory=lambda record, environment: object(),
        ), 3.0)

    def test_idle_worker_exits_without_sleeping_and_releases_lock(self):
        store = MemoryStore()
        self.patch_queue(store)
        sleep = mock.Mock(side_effect=AssertionError("idle worker must exit"))
        with tempfile.TemporaryDirectory() as directory:
            environment = {"HERDR_AGENT_REQUEST_STATE_DIR": directory}
            agent_delivery.run_worker(environment, sleep=sleep)
            with agent_delivery._worker_lock(environment) as acquired:
                self.assertTrue(acquired)
        sleep.assert_not_called()

    def test_worker_busy_lock_does_not_read_queue(self):
        with tempfile.TemporaryDirectory() as directory:
            environment = {"HERDR_AGENT_REQUEST_STATE_DIR": directory}
            with agent_delivery._worker_lock(environment), mock.patch.object(agent_delivery, "open_request_store") as open_store:
                agent_delivery.run_worker(environment)
            open_store.assert_not_called()

    def test_worker_loop_sleeps_between_actionable_ticks(self):
        self.patch_queue(MemoryStore())
        sleep = mock.Mock()
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            agent_delivery, "worker_tick", side_effect=[3.0, None],
        ):
            agent_delivery.run_worker({"HERDR_AGENT_REQUEST_STATE_DIR": directory}, sleep=sleep)
        sleep.assert_called_once_with(3.0)

    def test_enqueue_during_idle_exit_is_not_stranded(self):
        store = MemoryStore()
        self.patch_queue(store)
        record = held_record(decision="push")
        original_lock = agent_delivery._worker_lock
        released = False

        @contextmanager
        def enqueue_after_unlock(environment):
            nonlocal released
            with original_lock(environment) as acquired:
                yield acquired
            if not released:
                released = True
                store.records[record["request_id"]] = record

        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            agent_delivery, "_worker_lock", enqueue_after_unlock,
        ):
            agent_delivery.run_worker(
                {"HERDR_AGENT_REQUEST_STATE_DIR": directory},
                transport_factory=lambda record, environment: object(),
            )
        self.assertEqual(store.deliveries, [(record["request_id"], True)])
        self.assertEqual(record["state"], "submitted_unknown")

    def test_worker_releases_lock_on_unexpected_exit(self):
        with tempfile.TemporaryDirectory() as directory:
            environment = {"HERDR_AGENT_REQUEST_STATE_DIR": directory}
            with mock.patch.object(agent_delivery, "worker_tick", side_effect=RuntimeError("stop")), mock.patch.object(
                agent_delivery, "open_request_store",
            ), self.assertRaises(RuntimeError):
                agent_delivery.run_worker(environment)
            with agent_delivery._worker_lock(environment) as acquired:
                self.assertTrue(acquired)

    def test_held_messages_extracts_first_nonempty_message_line_and_bounds_preview(self):
        first = held_record(1)
        first["_prompt"] = "Message from sender/writer:\n\n   \n  " + "x" * 130 + "\nsecond"
        second = held_record(2, remote=True, state="awaiting_decision")
        second["_prompt"] = "\nordinary message\n"
        second.pop("recipient")
        store = MemoryStore([first, second])
        self.patch_queue(store)
        messages = agent_delivery.held_messages({})
        self.assertEqual(len(messages), 2)
        self.assertEqual(messages[0].request_id, first["request_id"])
        self.assertEqual(messages[0].sender, "sender/writer")
        self.assertEqual(messages[0].recipient, "sender/reader")
        self.assertEqual(messages[0].since, first["defer"]["since"])
        self.assertEqual(messages[0].deadline, first["defer"]["deadline"])
        self.assertEqual(messages[0].preview, "x" * 120)
        self.assertEqual(messages[1].recipient, "remote/reader")
        self.assertEqual(messages[1].preview, "ordinary message")

    def test_decide_cancel_is_terminal_without_starting_worker(self):
        record = held_record(state="awaiting_decision")
        store = MemoryStore([record])
        self.patch_queue(store)
        with mock.patch.object(agent_delivery, "ensure_worker") as ensure:
            result = agent_delivery.decide(
                record["request_id"], "cancel", {}, transport_factory=lambda record, environment: object(),
            )
        self.assertEqual(result["state"], "submission_failed")
        self.assertEqual(result["error_code"], "delivery_cancelled")
        self.assertTrue(result["terminal"])
        self.assertFalse(result["submitted"])
        ensure.assert_not_called()
        self.assertEqual(store.deliveries, [])

    def test_decide_push_persists_decision_and_starts_worker_without_sending(self):
        record = held_record(state="awaiting_decision")
        store = MemoryStore([record])
        self.patch_queue(store)
        environment = {"HERDR_AGENT_REQUEST_STATE_DIR": "/fake/state"}
        with mock.patch.object(agent_delivery, "ensure_worker") as ensure:
            result = agent_delivery.decide(record["request_id"], "push", environment)
        self.assertEqual(result["_defer"]["decision"], "push")
        self.assertEqual(result["state"], "awaiting_decision")
        self.assertEqual(store.deliveries, [])
        ensure.assert_called_once_with(environment)

    def test_decide_rejects_unknown_decision(self):
        with self.assertRaises(ValueError):
            agent_delivery.decide("1" * 32, "retry", {})

    def test_reconcile_starts_worker_only_for_pending_requests(self):
        store = MemoryStore()
        self.patch_queue(store)
        with mock.patch.object(agent_delivery, "ensure_worker") as ensure:
            self.assertEqual(agent_delivery.main(["reconcile"]), 0)
            ensure.assert_not_called()
            record = held_record(state="awaiting_decision")
            store.records[record["request_id"]] = record
            self.assertEqual(agent_delivery.main(["reconcile"]), 0)
            ensure.assert_called_once_with()

    def test_stale_decisions_return_latest_without_transport_or_worker(self):
        for decision in ("push", "cancel"):
            with self.subTest(decision=decision), tempfile.TemporaryDirectory() as directory:
                environment = {"HERDR_AGENT_REQUEST_STATE_DIR": directory}
                store = JsonRequestStateStore(Path(directory))
                record = held_record(state="submitted_settled")
                record.update(terminal=True, submitted=True)
                record.pop("_context")
                store.save(record["request_id"], record)
                factory = mock.Mock(side_effect=AssertionError("transport constructed"))
                with mock.patch.object(agent_delivery, "ensure_worker") as ensure:
                    result = agent_delivery.decide(
                        record["request_id"], decision, environment, transport_factory=factory,
                    )
                self.assertEqual(result, record)
                ensure.assert_not_called()
                factory.assert_not_called()

    def test_cancel_does_not_construct_transport(self):
        with tempfile.TemporaryDirectory() as directory:
            environment = {"HERDR_AGENT_REQUEST_STATE_DIR": directory}
            store = JsonRequestStateStore(Path(directory))
            record = held_record()
            record.pop("_context")
            store.save(record["request_id"], record)
            factory = mock.Mock(side_effect=AssertionError("transport constructed"))
            result = agent_delivery.decide(
                record["request_id"], "cancel", environment, transport_factory=factory,
            )
            self.assertEqual(result["error_code"], "delivery_cancelled")
            factory.assert_not_called()

    def test_malformed_held_records_terminate_without_notifications_or_spin(self):
        for state in ("deferred", "awaiting_decision"):
            with self.subTest(state=state), tempfile.TemporaryDirectory() as directory:
                environment = {"HERDR_AGENT_REQUEST_STATE_DIR": directory}
                store = JsonRequestStateStore(Path(directory))
                record = held_record(state=state)
                record.pop("_defer")
                store.save(record["request_id"], record)
                factory = mock.Mock(side_effect=AssertionError("transport constructed"))
                runner = mock.Mock(side_effect=AssertionError("notification sent"))
                sleep = mock.Mock(side_effect=AssertionError("worker looped"))
                agent_delivery.run_worker(
                    environment, transport_factory=factory, command_runner=runner, sleep=sleep,
                )
                result = store.load(record["request_id"])
                self.assertEqual(result["state"], "submission_failed")
                self.assertEqual(result["error_code"], "request_state_invalid")
                factory.assert_not_called()
                runner.assert_not_called()
                sleep.assert_not_called()

    def test_notification_rechecks_record_after_concurrent_delivery(self):
        record = held_record(state="awaiting_decision")
        store = MemoryStore([record])
        original_lock = store.lock
        calls = 0

        @contextmanager
        def concurrent_push(request_id):
            nonlocal calls
            calls += 1
            if calls == 2:
                record.update(state="submitted_unknown", submitted=True)
                record.pop("defer")
                record.pop("_defer")
            with original_lock(request_id):
                yield

        store.lock = concurrent_push
        runner = mock.Mock()
        self.assertIsNone(agent_delivery.worker_tick(
            store, {}, command_runner=runner,
        ))
        runner.assert_not_called()

    def test_worker_observes_fast_turn_and_resumes_watch_after_restart(self):
        for push in (False, True):
            with self.subTest(push=push), tempfile.TemporaryDirectory() as directory:
                environment = {"HERDR_AGENT_REQUEST_STATE_DIR": directory}
                store = JsonRequestStateStore(Path(directory))
                clock = [100.0]

                class Target:
                    identity = "reader"
                    status = "idle"
                    focused = True

                target = Target()

                class Transport:
                    submitted = False

                    def resolve(self):
                        return target

                    def describe(self, resolved):
                        return {"host": "local", "name": "reader", "local": True}

                    def recipient_is_sender(self, resolved):
                        return False

                    def is_focused(self, resolved):
                        return resolved.focused

                    def read_output(self, resolved, **limits):
                        return OutputSnapshot("before\nanswer\n" if self.submitted else "before\n")

                    def submit(self, resolved, prompt):
                        self.submitted = True
                        target.status = "working"
                        return SubmissionResult.submitted()

                transport = Transport()
                machine = RequestLifecycleMachine(transport, store, now=lambda: clock[0])
                request_id = "f" * 32
                machine.enqueue(
                    request_id=request_id, prompt="work", max_output_lines=20,
                    max_output_chars=1000, defer_timeout_seconds=1,
                )
                if push:
                    clock[0] += 2
                    machine.deliver_deferred(request_id)
                    with mock.patch.object(agent_delivery, "ensure_worker") as ensure:
                        agent_delivery.decide(request_id, "push", environment)
                    ensure.assert_called_once_with(environment)
                else:
                    target.focused = False
                delay = agent_delivery.worker_tick(
                    store, environment, transport_factory=lambda *args: transport,
                    now=lambda: clock[0],
                )
                self.assertEqual(delay, 0.5)
                # No request-status runs during the working turn.
                target.status = "idle"
                clock[0] += 0.5
                restarted_store = JsonRequestStateStore(Path(directory))
                self.assertIsNone(agent_delivery.worker_tick(
                    restarted_store, environment, transport_factory=lambda *args: transport,
                    now=lambda: clock[0],
                ))
                result = restarted_store.load(request_id)
                self.assertEqual(result["state"], "submitted_settled")
                self.assertTrue(result["terminal"])
                self.assertTrue(result["response"]["correlated"])
                self.assertEqual(result["response"]["output"], "answer\n")

    def test_expired_watch_does_not_refresh_or_keep_worker_running(self):
        record = held_record(state="submitted_unknown")
        record["_watch_until"] = 100.0
        store = MemoryStore([record])
        factory = mock.Mock(side_effect=AssertionError("expired watch refreshed"))
        self.assertIsNone(agent_delivery.worker_tick(
            store, {}, transport_factory=factory, now=lambda: 100.0,
        ))
        factory.assert_not_called()


class RequestCancelCliTest(ham_test_support.IsolatedLedgerTest):
    def setUp(self):
        super().setUp()
        from ham import cli as agent_skill_cli

        self.cli = agent_skill_cli
        self.environment["HERDR_PANE_ID"] = "sender-pane"
        self.sender = agent_skill_cli.AgentRecord(
            host="sandbox", name="writer", pane_id="sender-pane",
            workspace_id="workspace", workspace_label="sandbox", status="working",
            session_id="sender-session", cwd="/sandbox", local=True,
            terminal_id="sender-terminal", agent_kind="omp",
        )
        self.store = JsonRequestStateStore(Path(self.environment["HERDR_AGENT_REQUEST_STATE_DIR"]))
        self.record = held_record(state="awaiting_decision")
        self.record.update(
            sender=agent_skill_cli._record_payload(self.sender),
            phase="awaiting_decision", submitted=False, terminal=False,
        )
        self.store.save(self.record["request_id"], self.record)
        self.probe = mock.patch.object(
            agent_skill_cli, "probe_local_agents",
            return_value=mock.Mock(success=True, agents=(self.sender,)),
        )
        self.probe.start()
        self.addCleanup(self.probe.stop)

    def run_cli(self, command, request_id=None):
        output = io.StringIO()
        with redirect_stdout(output):
            code = self.cli.main(
                [command, "--request-id", request_id or self.record["request_id"]],
                environment=self.environment,
            )
        text = output.getvalue()
        self.assertEqual(len(text.splitlines()), 1)
        self.assertNotIn('": ', text)
        return code, json.loads(text)

    def test_sender_cancels_held_request_and_status_reports_cancellation(self):
        for state in ("deferred", "awaiting_decision"):
            with self.subTest(state=state):
                self.record.update(state=state, phase=state)
                self.store.save(self.record["request_id"], self.record)
                with mock.patch.object(
                    self.cli, "transport_for_record", side_effect=AssertionError("recipient contacted"),
                ), mock.patch.object(agent_delivery, "ensure_worker") as worker:
                    code, payload = self.run_cli("request-cancel")
                    status_code, status = self.run_cli("request-status")
                self.assertEqual(code, 0)
                self.assertEqual(payload, {
                    "request_id": self.record["request_id"],
                    "state": "submission_failed", "phase": "delivery_cancelled",
                    "cancelled": True, "error_code": "delivery_cancelled",
                })
                self.assertEqual(status_code, 1)
                self.assertEqual(status["phase"], "delivery_cancelled")
                self.assertEqual(status["error_code"], "delivery_cancelled")
                worker.assert_not_called()

    def test_non_sender_is_refused_without_mutating_held_request(self):
        self.record["sender"]["session_id"] = "another-session"
        self.store.save(self.record["request_id"], self.record)
        code, payload = self.run_cli("request-cancel")
        self.assertEqual(code, 1)
        self.assertEqual(payload["error_code"], "not_sender")
        self.assertFalse(payload["cancelled"])
        self.assertEqual(payload["state"], "awaiting_decision")
        self.assertEqual(self.store.load(self.record["request_id"]), self.record)

    def test_submitted_request_returns_current_state_without_mutation(self):
        for state, expected_code in (
            ("submitted_working", 3), ("submitted_unknown", 3), ("submitted_settled", 1),
        ):
            with self.subTest(state=state):
                self.record.update(
                    state=state, phase="observing", submitted=True,
                    terminal=state == "submitted_settled",
                )
                self.store.save(self.record["request_id"], self.record)
                code, payload = self.run_cli("request-cancel")
                self.assertEqual(code, expected_code)
                self.assertEqual(payload["state"], state)
                self.assertEqual(payload["error_code"], "already_submitted")
                self.assertFalse(payload["cancelled"])
                self.assertEqual(self.store.load(self.record["request_id"]), self.record)

    def test_terminal_cancellation_is_not_repeated_or_mutated(self):
        self.assertEqual(self.run_cli("request-cancel")[0], 0)
        before = self.store.load(self.record["request_id"])
        code, payload = self.run_cli("request-cancel")
        self.assertEqual(code, 1)
        self.assertFalse(payload["cancelled"])
        self.assertEqual(payload["error_code"], "already_terminal")
        self.assertEqual(self.store.load(self.record["request_id"]), before)

    def test_unknown_request_uses_existing_not_found_error(self):
        code, payload = self.run_cli("request-cancel", "f" * 32)
        self.assertEqual(code, 1)
        self.assertEqual(payload["error"]["code"], "request_not_found")

    def test_invalid_request_id_uses_existing_error(self):
        code, payload = self.run_cli("request-cancel", "../invalid")
        self.assertEqual(code, 1)
        self.assertEqual(payload["error"]["code"], "invalid_request_id")

    def test_missing_pane_identity_is_refused_without_mutation(self):
        self.environment.pop("HERDR_PANE_ID")
        code, payload = self.run_cli("request-cancel")
        self.assertEqual(code, 1)
        self.assertEqual(payload["error"]["code"], "sender_unavailable")
        self.assertEqual(self.store.load(self.record["request_id"]), self.record)

    def test_real_cli_entry_preserves_request_not_found_error(self):
        snapshot = {"result": {"snapshot": {"agents": [{
            "agent": "omp", "agent_session": {"value": "sender-session"},
            "agent_status": "working", "cwd": "/sandbox", "name": "writer",
            "pane_id": "sender-pane", "terminal_id": "sender-terminal",
            "workspace_id": "workspace",
        }], "workspaces": []}}}
        executable = self.root / "herdr-fixture"
        executable.write_text(
            "#!/usr/bin/env python3\nimport json, sys\n"
            "assert sys.argv[1:] == ['api', 'snapshot']\n"
            f"print(json.dumps({snapshot!r}))\n",
            encoding="utf-8",
        )
        executable.chmod(0o700)
        result = subprocess.run(
            [sys.executable, "-m", "ham", "cli",
             "request-cancel", "--request-id", "f" * 32],
            env={**os.environ, **self.environment, "HERDR_BIN_PATH": str(executable)},
            cwd=Path(__file__).resolve().parents[1],
            capture_output=True, text=True, timeout=10, check=False,
        )
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stderr, "")
        self.assertEqual(json.loads(result.stdout)["error"]["code"], "request_not_found")


if __name__ == "__main__":
    unittest.main()
