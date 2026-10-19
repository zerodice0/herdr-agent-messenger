import ham_test_support  # Protect the live ledger before product imports.
from contextlib import contextmanager, nullcontext
from copy import deepcopy
import fcntl
import os
import subprocess
import tempfile
import unittest
from dataclasses import dataclass, replace
from pathlib import Path
from unittest import mock
from datetime import datetime

import agent_skill_cli
from request_lifecycle import (
    JsonRequestStateStore,
    OutputSnapshot,
    RequestLifecycleMachine,
    SubmissionResult,
    capture_new_output,
    limit_output,
    mark_deferred_notified,
    open_request_store,
    pending_deferred,
    request_state_directory,
    set_deferred_decision,
)


@dataclass(frozen=True)
class FakeTarget:
    identity: str
    status: str
    output: str = ""
    focused: bool = False


class MemoryStore:
    def __init__(self):
        self.payloads = {}
        self.history = []

    def save(self, request_id, payload):
        copied = deepcopy(payload)
        self.payloads[request_id] = copied
        self.history.append(copied)

    def load(self, request_id):
        return deepcopy(self.payloads.get(request_id))

    def lock(self, request_id):
        return nullcontext()

    def list_request_ids(self):
        return list(self.payloads)


class FakeClock:
    def __init__(self):
        self.value = 0.0

    def monotonic(self):
        return self.value

    def sleep(self, seconds):
        self.value += seconds


class FakeTransport:
    def __init__(self, targets, submission):
        self.targets = list(targets)
        self.last_target = self.targets[-1]
        self.submission = submission
        self.prompts = []

    def resolve(self):
        if self.targets:
            self.last_target = self.targets.pop(0)
        if isinstance(self.last_target, Exception):
            raise self.last_target
        return self.last_target

    def describe(self, target):
        return {"identity": target.identity, "status": target.status}

    def is_focused(self, target):
        return target.focused

    def recipient_is_sender(self, target):
        return False

    def submit(self, target, prompt):
        self.prompts.append((target, prompt))
        if isinstance(self.submission, Exception):
            raise self.submission
        return self.submission

    def read_output(self, target, *, max_lines, max_chars):
        return limit_output(
            target.output,
            max_lines=max_lines,
            max_chars=max_chars,
        )


def run_machine(
    targets, submission, *, timeout=3.0, lines=20, chars=500, prompt="do it",
):
    store = MemoryStore()
    clock = FakeClock()
    transport = FakeTransport(targets, submission)
    machine = RequestLifecycleMachine(
        transport,
        store,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )
    result = machine.run(
        request_id="1" * 32,
        prompt=prompt,
        timeout_seconds=timeout,
        poll_interval_seconds=1.0,
        max_output_lines=lines,
        max_output_chars=chars,
    )
    return result, store, transport


class RequestLifecycleMachineTest(unittest.TestCase):
    def test_resolve_failure_preserves_machine_readable_error_code(self):
        for error, code in (
            (RuntimeError("discovery failed"), "resolve_failed"),
            (
                agent_skill_cli.SkillCommandError("agent_not_found", "not found"),
                "agent_not_found",
            ),
        ):
            with self.subTest(code=code):
                store = MemoryStore()
                transport = FakeTransport(
                    [FakeTarget("worker", "idle")], SubmissionResult.submitted(),
                )
                transport.resolve = mock.Mock(side_effect=error)
                result = RequestLifecycleMachine(transport, store).run(
                    request_id="a" * 32,
                    prompt="do it",
                    timeout_seconds=3,
                    poll_interval_seconds=1,
                    max_output_lines=20,
                    max_output_chars=500,
                )

                self.assertEqual(result["state"], "submission_failed")
                self.assertEqual(result["error_code"], code)
                self.assertIsNone(store.history[0]["error"])
                self.assertIsNone(store.history[0]["error_code"])
                self.assertFalse(transport.prompts)

    def test_submission_failure_uses_transport_error_code(self):
        result, _store, _transport = run_machine(
            [FakeTarget("worker", "idle")],
            SubmissionResult.failed("cannot send to self", code="recipient_is_sender"),
        )

        self.assertEqual(result["state"], "submission_failed")
        self.assertEqual(result["error_code"], "recipient_is_sender")

    def test_submission_failure_is_terminal_and_distinct(self):
        result, store, transport = run_machine(
            [FakeTarget("agent-1", "idle", "before")],
            SubmissionResult.failed("rejected"),
        )

        self.assertEqual(result["state"], "submission_failed")
        self.assertFalse(result["submitted"])
        self.assertFalse(result["timed_out"])
        self.assertEqual(result["error"], "rejected")
        self.assertEqual(result["error_code"], "prompt_failed")
        self.assertEqual(len(transport.prompts), 1)
        self.assertEqual(store.load(result["request_id"]), result)

    def test_submission_process_timeout_is_not_reported_as_send_failure(self):
        idle = FakeTarget("agent-1", "idle", "before")
        result, _store, _transport = run_machine(
            [idle, idle, idle, idle],
            SubmissionResult.unknown(
                "command timed out",
                timed_out=True,
                code="prompt_acceptance_unknown",
            ),
        )

        self.assertEqual(result["state"], "submitted_unknown")
        self.assertIsNone(result["submitted"])
        self.assertTrue(result["submission_timed_out"])
        self.assertTrue(result["timed_out"])
        self.assertEqual(result["error_code"], "prompt_acceptance_unknown")

    def test_timeout_exception_is_also_an_unknown_submission(self):
        idle = FakeTarget("agent-1", "idle", "before")
        result, _store, _transport = run_machine(
            [idle, idle, idle, idle],
            subprocess.TimeoutExpired(["herdr"], 5),
        )

        self.assertEqual(result["state"], "submitted_unknown")
        self.assertIsNone(result["submitted"])
        self.assertTrue(result["submission_timed_out"])
        self.assertEqual(result["error_code"], "prompt_acceptance_unknown")

    def test_fresh_working_then_nonworking_is_settled(self):
        result, _store, _transport = run_machine(
            [
                FakeTarget("agent-1", "idle", "before\n"),
                FakeTarget("agent-1", "working", "before\npartial\n"),
                FakeTarget("agent-1", "done", "before\nanswer\n"),
            ],
            SubmissionResult.submitted(),
        )

        self.assertEqual(result["state"], "submitted_settled")
        self.assertTrue(result["submitted"])
        self.assertTrue(result["requested_turn_observed"])
        self.assertEqual(result["response"]["output"], "answer\n")
        self.assertTrue(result["response"]["correlated"])
        self.assertFalse(result["response"]["truncated"])
        self.assertIsNone(result["error"])
        self.assertIsNone(result["error_code"])

    def test_working_at_wait_deadline_remains_a_submitted_working_timeout(self):
        working = FakeTarget("agent-1", "working", "before\npartial\n")
        result, _store, _transport = run_machine(
            [FakeTarget("agent-1", "idle", "before\n"), working, working, working],
            SubmissionResult.submitted(),
        )

        self.assertEqual(result["state"], "submitted_working")
        self.assertTrue(result["timed_out"])
        self.assertTrue(result["requested_turn_observed"])
        self.assertFalse(result["terminal"])
        self.assertEqual(result["error_code"], "wait_timed_out")

    def test_cancel_stops_waiting_without_forgetting_accepted_submission(self):
        store = MemoryStore()
        clock = FakeClock()
        working = FakeTarget("agent-1", "working", "before\npartial\n")
        transport = FakeTransport(
            [FakeTarget("agent-1", "idle", "before\n"), working, working],
            SubmissionResult.submitted(),
        )
        result = RequestLifecycleMachine(
            transport,
            store,
            monotonic=clock.monotonic,
            sleep=clock.sleep,
        ).run(
            request_id="f" * 32,
            prompt="do it",
            timeout_seconds=30,
            poll_interval_seconds=1,
            max_output_lines=20,
            max_output_chars=500,
            cancelled=lambda: clock.value >= 1,
        )

        self.assertTrue(result["cancelled"])
        self.assertTrue(result["submitted"])
        self.assertEqual(result["state"], "submitted_working")
        self.assertEqual(result["phase"], "wait_cancelled")
        self.assertFalse(result["terminal"])
        self.assertEqual(result["error_code"], "cancelled")

    def test_refresh_advances_a_timed_out_working_request_to_settled(self):
        working = FakeTarget("agent-1", "working", "before\npartial\n")
        result, store, _transport = run_machine(
            [FakeTarget("agent-1", "idle", "before\n"), working, working, working],
            SubmissionResult.submitted(),
        )
        refresh_transport = FakeTransport(
            [FakeTarget("agent-1", "done", "before\nanswer\n")],
            SubmissionResult.submitted(),
        )
        refreshed = RequestLifecycleMachine(refresh_transport, store).refresh(
            request_id=result["request_id"]
        )

        self.assertEqual(refreshed["state"], "submitted_settled")
        self.assertTrue(refreshed["terminal"])
        self.assertFalse(refreshed["timed_out"])
        self.assertEqual(refreshed["response"]["output"], "answer\n")
        self.assertIsNone(refreshed["error"])
        self.assertIsNone(refreshed["error_code"])

    def test_refresh_resolve_failure_preserves_error_code_then_clears_it(self):
        working = FakeTarget("worker", "working", "before\npartial\n")
        for error, code in (
            (RuntimeError("discovery failed"), "resolve_failed"),
            (
                agent_skill_cli.SkillCommandError("host_unavailable", "unavailable"),
                "host_unavailable",
            ),
        ):
            with self.subTest(code=code):
                result, store, _transport = run_machine(
                    [FakeTarget("worker", "idle", "before\n"), working],
                    SubmissionResult.submitted(),
                )
                transport = FakeTransport(
                    [working], SubmissionResult.submitted(),
                )
                transport.resolve = mock.Mock(side_effect=[
                    error,
                    working,
                    FakeTarget("worker", "done", "before\nanswer\n"),
                ])
                machine = RequestLifecycleMachine(transport, store)

                unavailable = machine.refresh(request_id=result["request_id"])
                self.assertEqual(unavailable["error_code"], code)
                self.assertFalse(unavailable["terminal"])
                resumed = machine.refresh(request_id=result["request_id"])
                self.assertIsNone(resumed["error"])
                self.assertIsNone(resumed["error_code"])
                settled = machine.refresh(request_id=result["request_id"])
                self.assertEqual(settled["state"], "submitted_settled")
                self.assertIsNone(settled["error"])
                self.assertIsNone(settled["error_code"])

    def test_rewritten_screen_anchors_response_without_changing_correlation(self):
        prompt = "Message from x/y:\n\ndo it"
        reply = "Message from x/y:\n\ndo it\nreply\n"
        screen = "omp startup banner\navailable models\n" + reply
        for baseline in ("old screen\n", ""):
            for settled in (False, True):
                with self.subTest(baseline=baseline, settled=settled):
                    targets = [FakeTarget("worker", "idle", baseline)]
                    if settled:
                        targets.extend([
                            FakeTarget("worker", "working", screen),
                            FakeTarget("worker", "done", screen),
                        ])
                    else:
                        targets.append(FakeTarget("worker", "idle", screen))
                    result, store, _transport = run_machine(
                        targets, SubmissionResult.submitted(), prompt=prompt,
                    )

                    self.assertEqual(result["response"]["output"], reply)
                    self.assertTrue(result["response"]["anchored"])
                    self.assertEqual(result["response"]["correlated"], settled)
                    self.assertEqual(
                        store.load(result["request_id"])["_response_anchor"],
                        "Message from x/y:",
                    )

    def test_refresh_anchors_rewritten_live_and_settled_screens(self):
        working = FakeTarget("worker", "working", "before\npartial\n")
        result, store, _transport = run_machine(
            [FakeTarget("worker", "idle", "before\n"), working],
            SubmissionResult.submitted(),
            prompt="Message from x/y:\n\ndo it",
        )
        transport = FakeTransport(
            [
                FakeTarget(
                    "worker", "working",
                    "banner\nMessage from x/y:\n\ndo it\npartial\n",
                ),
                FakeTarget(
                    "worker", "done",
                    "redrawn banner\nMessage from x/y:\n\ndo it\nreply\n",
                ),
            ],
            SubmissionResult.submitted(),
        )
        machine = RequestLifecycleMachine(transport, store)

        live = machine.refresh(request_id=result["request_id"])
        self.assertEqual(
            live["response"]["output"], "Message from x/y:\n\ndo it\npartial\n",
        )
        self.assertTrue(live["response"]["anchored"])
        self.assertFalse(live["response"]["correlated"])
        settled = machine.refresh(request_id=result["request_id"])
        self.assertEqual(settled["state"], "submitted_settled")
        self.assertEqual(
            settled["response"]["output"], "Message from x/y:\n\ndo it\nreply\n",
        )
        self.assertTrue(settled["response"]["anchored"])
        self.assertTrue(settled["response"]["correlated"])

    def test_preexisting_turn_settlement_is_not_mistaken_for_requested_turn(self):
        old_done = FakeTarget("agent-1", "done", "old complete\n")
        result, _store, _transport = run_machine(
            [
                FakeTarget("agent-1", "working", "old partial\n"),
                old_done,
                old_done,
                old_done,
            ],
            SubmissionResult.submitted(),
        )

        self.assertEqual(result["state"], "submitted_unknown")
        self.assertTrue(result["preexisting_working"])
        self.assertFalse(result["requested_turn_observed"])
        self.assertEqual(result["response"]["output"], "")
        self.assertFalse(result["response"]["correlated"])

    def test_preexisting_turn_requires_a_second_working_settled_cycle(self):
        result, _store, _transport = run_machine(
            [
                FakeTarget("agent-1", "working", "old partial\n"),
                FakeTarget("agent-1", "done", "old complete\n"),
                FakeTarget("agent-1", "working", "old complete\nnew partial\n"),
                FakeTarget("agent-1", "done", "old complete\nnew answer\n"),
            ],
            SubmissionResult.submitted(),
            timeout=5,
        )

        self.assertEqual(result["state"], "submitted_settled")
        self.assertTrue(result["wait_safe"])
        self.assertEqual(result["response"]["output"], "new answer\n")
        self.assertTrue(result["response"]["correlated"])

    def test_refresh_keeps_preexisting_and_requested_turns_separate(self):
        old_working = FakeTarget("agent-1", "working", "old partial\n")
        result, store, _transport = run_machine(
            [old_working, old_working, old_working, old_working],
            SubmissionResult.submitted(),
        )
        refresh_transport = FakeTransport(
            [
                FakeTarget("agent-1", "done", "old complete\n"),
                FakeTarget("agent-1", "working", "old complete\nnew partial\n"),
                FakeTarget("agent-1", "done", "old complete\nnew answer\n"),
            ],
            SubmissionResult.submitted(),
        )
        refresher = RequestLifecycleMachine(refresh_transport, store)

        old_settled_state = refresher.refresh(request_id=result["request_id"])["state"]
        requested_working_state = refresher.refresh(
            request_id=result["request_id"]
        )["state"]
        requested_settled = refresher.refresh(request_id=result["request_id"])

        self.assertEqual(old_settled_state, "submitted_unknown")
        self.assertEqual(requested_working_state, "submitted_working")
        self.assertEqual(requested_settled["state"], "submitted_settled")
        self.assertEqual(
            requested_settled["response"]["output"],
            "new answer\n",
        )

    def test_recipient_replacement_makes_an_accepted_request_unknown(self):
        result, _store, _transport = run_machine(
            [
                FakeTarget("agent-1", "idle", "before"),
                FakeTarget("agent-2", "working", "replacement"),
            ],
            SubmissionResult.submitted(),
        )

        self.assertEqual(result["state"], "submitted_unknown")
        self.assertEqual(result["phase"], "recipient_changed")
        self.assertEqual(result["error_code"], "recipient_changed")

    def test_replacement_stops_output_reads(self):
        store = MemoryStore()
        clock = FakeClock()
        transport = FakeTransport(
            [FakeTarget("old", "idle"), FakeTarget("replacement", "working")],
            SubmissionResult.submitted(),
        )
        transport.read_output = mock.Mock(side_effect=[
            OutputSnapshot("before\n"), OutputSnapshot("replacement's unrelated output\n"),
        ])
        result = RequestLifecycleMachine(
            transport, store, monotonic=clock.monotonic, sleep=clock.sleep,
        ).run(
            request_id="e" * 32, prompt="work", timeout_seconds=3,
            poll_interval_seconds=1, max_output_lines=10, max_output_chars=500,
        )
        transport.read_output.assert_called_once()
        self.assertEqual(result["response"]["output"], "")
        self.assertFalse(result["response"]["correlated"])
        self.assertTrue(result["terminal"])

    def test_cancellation_during_baseline_prevents_submission(self):
        store = MemoryStore()
        target = FakeTarget("worker", "idle")
        transport = FakeTransport([target], SubmissionResult.submitted())
        cancelled = False

        def read_output(*args, **kwargs):
            nonlocal cancelled
            cancelled = True
            return OutputSnapshot("before")

        transport.read_output = read_output
        result = RequestLifecycleMachine(transport, store).run(
            request_id="e" * 32, prompt="work", timeout_seconds=3,
            poll_interval_seconds=1, max_output_lines=10, max_output_chars=500,
            cancelled=lambda: cancelled,
        )
        self.assertFalse(transport.prompts)
        self.assertFalse(result["submitted"])
        self.assertTrue(result["cancelled"])
        self.assertTrue(result["terminal"])
        self.assertEqual(result["error_code"], "cancelled")


class DeferredRequestTest(unittest.TestCase):
    request_id = "d" * 32
    prompt = "Message from sender/agent:\n\nDo the work."

    def setUp(self):
        self.store = MemoryStore()
        self.clock = FakeClock()
        self.focused = FakeTarget("worker", "idle", "before\n", focused=True)

    def machine(self, targets, submission=None):
        transport = FakeTransport(
            targets, submission if submission is not None else SubmissionResult.submitted()
        )
        machine = RequestLifecycleMachine(
            transport,
            self.store,
            monotonic=self.clock.monotonic,
            sleep=self.clock.sleep,
            now=lambda: 1700000000 + self.clock.value,
        )
        return machine, transport

    def enqueue(self, machine, **kwargs):
        return machine.enqueue(
            request_id=self.request_id,
            prompt=self.prompt,
            max_output_lines=20,
            max_output_chars=500,
            defer_timeout_seconds=kwargs.pop("defer_timeout_seconds", 3),
            **kwargs,
        )

    def run_request(self, machine, **kwargs):
        return machine.run(
            request_id=self.request_id,
            prompt=self.prompt,
            timeout_seconds=kwargs.pop("timeout_seconds", 5),
            poll_interval_seconds=1,
            max_output_lines=20,
            max_output_chars=500,
            defer_timeout_seconds=kwargs.pop("defer_timeout_seconds", 3),
            **kwargs,
        )

    def test_focused_run_defers_then_delivers_and_observes_settlement(self):
        unfocused = replace(self.focused, focused=False)
        machine, transport = self.machine([
            self.focused,
            self.focused,
            unfocused,
            replace(unfocused, status="working", output="before\npartial\n"),
            replace(unfocused, status="done", output="before\nanswer\n"),
        ])
        deferred = []

        result = self.run_request(
            machine, on_deferred=lambda record: deferred.append(deepcopy(record))
        )

        self.assertEqual(deferred[0]["state"], "deferred")
        self.assertFalse(deferred[0]["submitted"])
        self.assertEqual(result["state"], "submitted_settled")
        self.assertEqual(result["response"]["output"], "answer\n")
        self.assertTrue(result["response"]["correlated"])
        self.assertEqual(result["_prompt"], self.prompt)
        self.assertEqual(len(transport.prompts), 1)

    def test_deadline_enters_decision_and_focus_leaving_does_not_push(self):
        machine, transport = self.machine([self.focused])
        record = self.enqueue(machine)
        self.assertEqual(record["_defer"]["since"], 1700000000)
        self.assertEqual(record["_defer"]["deadline"], 1700000003)
        self.assertEqual(record["_defer"]["timeout_ms"], 3000)
        for key in ("since", "deadline"):
            self.assertTrue(record["defer"][key].endswith("Z"))
            self.assertEqual(
                datetime.fromisoformat(record["defer"][key].replace("Z", "+00:00"))
                .timestamp(),
                record["_defer"][key],
            )
        self.clock.sleep(2.999)
        self.assertEqual(machine.deliver_deferred(self.request_id)["state"], "deferred")
        self.clock.value = 3
        record = machine.deliver_deferred(self.request_id)
        self.assertEqual(record["state"], "awaiting_decision")
        transport.last_target = replace(self.focused, focused=False)
        self.clock.sleep(100)
        record = machine.deliver_deferred(self.request_id)
        self.assertEqual(record["state"], "awaiting_decision")
        self.assertFalse(record["submitted"])
        self.assertEqual(transport.prompts, [])

    def test_push_decision_delivers_despite_focus_and_is_idempotent(self):
        machine, transport = self.machine([self.focused])
        self.enqueue(machine, defer_timeout_seconds=0)
        self.assertEqual(
            machine.deliver_deferred(self.request_id)["state"], "awaiting_decision"
        )
        set_deferred_decision(self.store, self.request_id, "push")

        result = machine.deliver_deferred(self.request_id)
        again = machine.deliver_deferred(self.request_id)

        self.assertTrue(result["submitted"])
        self.assertEqual(again, result)
        self.assertEqual(transport.prompts, [(self.focused, self.prompt)])
        self.assertEqual(result["_prompt"], self.prompt)

    def test_forced_delivery_does_not_require_a_stored_push_decision(self):
        machine, transport = self.machine([self.focused])
        self.enqueue(machine)
        result = machine.deliver_deferred(self.request_id, force=True)
        self.assertTrue(result["submitted"])
        self.assertEqual(len(transport.prompts), 1)

    def test_cancel_is_terminal_without_transport_calls(self):
        machine, transport = self.machine([self.focused])
        self.enqueue(machine)
        with mock.patch.object(transport, "resolve") as resolve, \
                mock.patch.object(transport, "is_focused") as is_focused:
            result = machine.cancel_deferred(self.request_id)
            again = machine.cancel_deferred(self.request_id)
            resolve.assert_not_called()
            is_focused.assert_not_called()
        self.assertEqual(result["state"], "submission_failed")
        self.assertEqual(result["phase"], "delivery_cancelled")
        self.assertEqual(result["error_code"], "delivery_cancelled")
        self.assertTrue(result["terminal"])
        self.assertFalse(result["submitted"])
        self.assertEqual(again, result)
        self.assertEqual(transport.prompts, [])

    def test_cancel_checks_ownership_under_lock_before_mutating_corrupt_state(self):
        machine, _transport = self.machine([self.focused])
        record = self.enqueue(machine)
        record.pop("_defer")
        self.store.save(self.request_id, record)
        locked = False

        @contextmanager
        def lock(request_id):
            nonlocal locked
            self.assertEqual(request_id, self.request_id)
            locked = True
            try:
                yield
            finally:
                locked = False

        def refuse(current):
            self.assertTrue(locked)
            self.assertEqual(current, record)
            return False

        with mock.patch.object(self.store, "lock", lock):
            result = machine.cancel_deferred(self.request_id, can_cancel=refuse)
        self.assertEqual(result, record)
        self.assertEqual(self.store.load(self.request_id), record)

    def test_cancel_returns_actual_state_when_delivery_wins_lock(self):
        machine, transport = self.machine([self.focused])
        self.enqueue(machine)
        submitted = None
        original_lock = self.store.lock

        @contextmanager
        def delivery_wins(request_id):
            nonlocal submitted
            with mock.patch.object(self.store, "lock", original_lock):
                submitted = machine.deliver_deferred(request_id, force=True)
            yield

        with mock.patch.object(self.store, "lock", delivery_wins):
            result = machine.cancel_deferred(self.request_id, can_cancel=lambda record: True)
        self.assertEqual(result, submitted)
        self.assertEqual(self.store.load(self.request_id), submitted)
        self.assertTrue(result["submitted"])
        self.assertEqual(len(transport.prompts), 1)

    def test_cancel_never_rewrites_held_state_already_marked_submitted_or_terminal(self):
        machine, _transport = self.machine([self.focused])
        held = self.enqueue(machine)
        for flags in ({"submitted": True}, {"terminal": True}):
            with self.subTest(flags=flags):
                record = {**held, **flags}
                self.store.save(self.request_id, record)
                result = machine.cancel_deferred(self.request_id)
                self.assertEqual(result, record)
                self.assertEqual(self.store.load(self.request_id), record)

    def test_deferred_recipient_disappearing_or_changing_fails_before_submission(self):
        for target, code in (
            (replace(self.focused, identity="replacement"), "recipient_changed"),
            (
                agent_skill_cli.SkillCommandError("agent_not_found", "not found"),
                "agent_not_found",
            ),
        ):
            with self.subTest(code=code):
                machine, transport = self.machine([self.focused, target])
                self.enqueue(machine)
                result = machine.deliver_deferred(self.request_id, force=True)
                self.assertEqual(result["state"], "submission_failed")
                self.assertEqual(result["error_code"], code)
                self.assertTrue(result["terminal"])
                self.assertFalse(result["submitted"])
                self.assertEqual(transport.prompts, [])

    def test_run_timeout_returns_deferred_without_wait_timeout_mutation(self):
        machine, transport = self.machine([self.focused])
        result = self.run_request(
            machine, timeout_seconds=2, defer_timeout_seconds=30
        )
        self.assertEqual(result["state"], "deferred")
        self.assertFalse(result["submitted"])
        self.assertFalse(result["terminal"])
        self.assertFalse(result["timed_out"])
        self.assertEqual(self.clock.value, 2)
        self.assertEqual(transport.prompts, [])

    def test_run_timeout_while_awaiting_decision_does_not_deliver(self):
        machine, transport = self.machine([self.focused])
        result = self.run_request(
            machine, timeout_seconds=4, defer_timeout_seconds=1
        )
        self.assertEqual(result["state"], "awaiting_decision")
        self.assertFalse(result["submitted"])
        self.assertFalse(result["timed_out"])
        self.assertEqual(transport.prompts, [])

    def test_run_after_deferred_delivery_uses_remaining_wait_budget(self):
        idle = replace(self.focused, focused=False)
        machine, transport = self.machine([
            self.focused, self.focused, idle,
            replace(idle, status="working", output="before\npartial\n"),
        ])
        result = self.run_request(machine, timeout_seconds=2)
        self.assertEqual(result["state"], "submitted_working")
        self.assertEqual(result["phase"], "wait_timed_out")
        self.assertTrue(result["timed_out"])
        self.assertEqual(result["error_code"], "wait_timed_out")
        self.assertEqual(self.clock.value, 2)
        self.assertEqual(len(transport.prompts), 1)

    def test_refresh_delivers_when_focus_leaves_and_observes_requested_turn(self):
        idle = replace(self.focused, focused=False)
        machine, transport = self.machine([
            self.focused, idle, replace(idle, status="working"),
            replace(idle, status="done", output="before\nanswer\n"),
        ])
        self.enqueue(machine)
        result = machine.refresh(request_id=self.request_id)
        self.assertEqual(result["state"], "submitted_working")
        result = machine.refresh(request_id=self.request_id)
        self.assertEqual(result["state"], "submitted_settled")
        self.assertEqual(len(transport.prompts), 1)

    def test_enqueue_unfocused_does_not_take_ownership_or_submit(self):
        machine, transport = self.machine([replace(self.focused, focused=False)])
        callback = mock.Mock()
        self.assertIsNone(self.enqueue(machine, on_deferred=callback))
        self.assertIsNone(self.store.load(self.request_id))
        callback.assert_not_called()
        self.assertEqual(transport.prompts, [])

    def test_enqueue_resolve_failure_is_persisted(self):
        error = agent_skill_cli.SkillCommandError("agent_not_found", "not found")
        machine, transport = self.machine([error])
        result = self.enqueue(machine)
        self.assertEqual(result["state"], "submission_failed")
        self.assertEqual(result["error_code"], "agent_not_found")
        self.assertFalse(result["submitted"])
        self.assertEqual(result["_prompt"], self.prompt)
        self.assertEqual(self.store.load(self.request_id), result)
        self.assertEqual(transport.prompts, [])

    def test_notification_and_decision_updates_preserve_the_deferred_record(self):
        machine, _transport = self.machine([self.focused])
        self.enqueue(machine)
        notified = {"local": "shown", "remote": "disabled"}
        record = mark_deferred_notified(self.store, self.request_id, notified)
        self.assertEqual(record["defer"]["notified"], notified)
        self.assertEqual(record["_prompt"], self.prompt)
        record = set_deferred_decision(self.store, self.request_id, "push")
        self.assertEqual(record["_defer"]["decision"], "push")
        self.assertEqual(record["defer"]["notified"], notified)
        self.assertEqual(pending_deferred(self.store), [record])
        machine.cancel_deferred(self.request_id)
        self.assertEqual(pending_deferred(self.store), [])

    def test_decision_and_notification_do_not_change_submitted_records(self):
        machine, _transport = self.machine([self.focused])
        self.enqueue(machine)
        submitted = machine.deliver_deferred(self.request_id, force=True)
        self.assertEqual(
            set_deferred_decision(self.store, self.request_id, "push"), submitted
        )
        self.assertEqual(
            mark_deferred_notified(self.store, self.request_id, {"local": "shown"}),
            submitted,
        )
        self.assertEqual(machine.cancel_deferred(self.request_id), submitted)

    def test_prompt_is_retained_when_deferral_is_disabled(self):
        machine, transport = self.machine([self.focused])
        transport.is_focused = mock.Mock(side_effect=AssertionError("focus queried"))
        result = self.run_request(machine, defer_timeout_seconds=None)
        self.assertEqual(result["_prompt"], self.prompt)
        self.assertEqual(len(transport.prompts), 1)
        transport.is_focused.assert_not_called()

    def test_focused_self_target_fails_before_focus_or_enqueue(self):
        for operation in (self.run_request, self.enqueue):
            with self.subTest(operation=operation.__name__):
                machine, transport = self.machine([self.focused])
                transport.recipient_is_sender = lambda target: True
                transport.is_focused = mock.Mock(side_effect=AssertionError("focus queried"))
                callback = mock.Mock()
                result = operation(machine, on_deferred=callback)
                self.assertEqual(result["error_code"], "recipient_is_sender")
                self.assertEqual(result["state"], "submission_failed")
                self.assertTrue(result["terminal"])
                self.assertEqual(pending_deferred(self.store), [])
                callback.assert_not_called()
                transport.is_focused.assert_not_called()
                self.assertEqual(transport.prompts, [])

    def test_interrupted_submit_is_not_held_or_resubmitted(self):
        machine, transport = self.machine([self.focused])
        self.enqueue(machine)
        transport.submit = mock.Mock(side_effect=KeyboardInterrupt)
        with self.assertRaises(KeyboardInterrupt):
            machine.deliver_deferred(self.request_id, force=True)
        record = self.store.load(self.request_id)
        self.assertEqual(record["state"], "submitted_unknown")
        self.assertIsNone(record["submitted"])
        self.assertEqual(record["phase"], "submitting")
        self.assertNotIn("_defer", record)
        self.assertNotIn("defer", record)
        machine.deliver_deferred(self.request_id, force=True)
        transport.submit.assert_called_once()
        for saved in self.store.history:
            if saved.get("state") in {"deferred", "awaiting_decision"}:
                self.assertIn("_defer", saved)
                self.assertIn("defer", saved)

    def test_malformed_held_records_fail_without_transport_calls(self):
        for state in ("deferred", "awaiting_decision"):
            for operation in ("refresh", "cancel", "push"):
                with self.subTest(state=state, operation=operation):
                    machine, transport = self.machine([self.focused])
                    self.enqueue(machine)
                    record = self.store.load(self.request_id)
                    record["state"] = state
                    record.pop("_defer")
                    self.store.save(self.request_id, record)
                    transport.resolve = mock.Mock(side_effect=AssertionError("contacted recipient"))
                    if operation == "refresh":
                        result = machine.refresh(request_id=self.request_id)
                    elif operation == "cancel":
                        result = machine.cancel_deferred(self.request_id)
                    else:
                        result = set_deferred_decision(self.store, self.request_id, "push")
                    self.assertEqual(result["error_code"], "request_state_invalid")
                    self.assertEqual(result["state"], "submission_failed")
                    self.assertTrue(result["terminal"])
                    transport.resolve.assert_not_called()


    def test_wait_deadline_does_not_overwrite_worker_settlement(self):
        idle = replace(self.focused, focused=False)
        machine, _transport = self.machine([
            self.focused, self.focused, idle, replace(idle, status="working"),
        ])

        def worker_settles_during_sleep(seconds):
            self.clock.sleep(seconds)
            record = self.store.load(self.request_id)
            if record.get("state") == "submitted_working":
                record.update(state="submitted_settled", phase="settled", terminal=True)
                self.store.save(self.request_id, record)

        machine.sleep = worker_settles_during_sleep
        result = self.run_request(machine, timeout_seconds=2)
        self.assertEqual(result["state"], "submitted_settled")
        self.assertTrue(result["terminal"])
        self.assertFalse(result["timed_out"])


class RequestOutputTest(unittest.TestCase):
    def test_limit_output_returns_tail_and_truncation_flag(self):
        captured = limit_output("one\ntwo\nthree\n", max_lines=2, max_chars=100)
        self.assertEqual(captured.text, "two\nthree\n")
        self.assertTrue(captured.truncated)

    def test_new_output_uses_rolling_tail_overlap(self):
        captured = capture_new_output(
            OutputSnapshot("one\ntwo\nthree\n", truncated=True),
            OutputSnapshot("three\nfour\n", truncated=True),
            max_lines=10,
            max_chars=100,
        )
        self.assertEqual(captured.text, "four\n")
        self.assertFalse(captured.truncated)

    def test_lost_baseline_marks_output_truncated(self):
        captured = capture_new_output(
            OutputSnapshot("old baseline"),
            OutputSnapshot("new tail"),
            max_lines=10,
            max_chars=100,
        )
        self.assertEqual(captured.text, "new tail")
        self.assertTrue(captured.truncated)

    def test_anchor_matches_normalized_lines_and_uses_last_occurrence(self):
        reply = "  Message\tfrom   x/y:  \nlatest reply\n"
        captured = capture_new_output(
            OutputSnapshot("old screen\n"),
            OutputSnapshot(
                "banner\nMessage from x/y:\nprevious reply\n"
                "quoted Message from x/y:\n" + reply,
            ),
            anchor="Message from x/y:",
            max_lines=20,
            max_chars=500,
        )

        self.assertEqual(captured.text, reply)
        self.assertTrue(captured.anchored)
        self.assertFalse(captured.truncated)

    def test_missing_anchor_keeps_rewritten_screen_behavior(self):
        screen = "banner\nquoted Message from x/y:\nunrelated output\n"
        captured = capture_new_output(
            OutputSnapshot("old screen\n"),
            OutputSnapshot(screen),
            anchor="Message from x/y:",
            max_lines=20,
            max_chars=500,
        )

        self.assertEqual(captured.text, screen)
        self.assertFalse(captured.anchored)
        self.assertTrue(captured.truncated)

    def test_reliable_overlap_does_not_reinclude_prompt_anchor(self):
        baseline = "Message from x/y:\n\ndo it\n"
        captured = capture_new_output(
            OutputSnapshot(baseline),
            OutputSnapshot(baseline + "reply\n"),
            anchor="Message from x/y:",
            max_lines=20,
            max_chars=500,
        )

        self.assertEqual(captured.text, "reply\n")
        self.assertFalse(captured.anchored)
        self.assertFalse(captured.truncated)

    def test_anchor_cannot_bypass_output_bounds(self):
        captured = capture_new_output(
            OutputSnapshot(""),
            OutputSnapshot("banner\nMessage from x/y:\none\ntwo\nthree\n"),
            anchor="Message from x/y:",
            max_lines=2,
            max_chars=8,
        )

        self.assertEqual(captured.text, "o\nthree\n")
        self.assertTrue(captured.anchored)
        self.assertTrue(captured.truncated)

    def test_complete_anchor_is_not_truncated_by_omitted_older_scrollback(self):
        captured = capture_new_output(
            OutputSnapshot("old screen", truncated=True),
            OutputSnapshot("history\nMessage from x/y:\n\nquestion\nOK\n", truncated=True),
            anchor="Message from x/y:", max_lines=10, max_chars=500,
        )
        self.assertTrue(captured.anchored)
        self.assertFalse(captured.truncated)
        self.assertTrue(captured.text.endswith("OK\n"))


class RequestStateStoreTest(unittest.TestCase):
    def test_json_store_round_trips_queryable_state(self):
        with tempfile.TemporaryDirectory() as directory:
            store = JsonRequestStateStore(Path(directory) / "requests")
            request_id = "a" * 32
            payload = {"request_id": request_id, "state": "submitted_unknown"}
            store.save(request_id, payload)

            self.assertEqual(store.load(request_id), payload)
            self.assertEqual(
                (Path(directory) / "requests" / f"{request_id}.json").stat().st_mode
                & 0o777,
                0o600,
            )

    def test_json_store_rejects_path_like_request_id(self):
        with tempfile.TemporaryDirectory() as directory:
            store = JsonRequestStateStore(Path(directory))
            with self.assertRaises(ValueError):
                store.load("../request")

    def test_lock_is_private_and_remains_exclusive_across_atomic_saves(self):
        with tempfile.TemporaryDirectory() as directory:
            store = JsonRequestStateStore(Path(directory) / "requests")
            request_id = "a" * 32
            with store.lock(request_id):
                lock_path = store.directory / f"{request_id}.lock"
                self.assertEqual(lock_path.stat().st_mode & 0o777, 0o600)
                descriptor = os.open(lock_path, os.O_RDWR)
                try:
                    for state in ("deferred", "submitted_unknown"):
                        store.save(request_id, {"state": state})
                        self.assertEqual(store.load(request_id)["state"], state)
                        with self.assertRaises(BlockingIOError):
                            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                finally:
                    os.close(descriptor)
            with store.lock(request_id):
                self.assertEqual(store.load(request_id)["state"], "submitted_unknown")

    def test_list_ids_and_pending_skip_unrelated_and_corrupt_files(self):
        with tempfile.TemporaryDirectory() as directory:
            store = JsonRequestStateStore(Path(directory))
            deferred_id, awaiting_id, settled_id, corrupt_id, invalid_id = (
                character * 32 for character in "abcde"
            )
            store.save(deferred_id, {"state": "deferred"})
            store.save(awaiting_id, {"state": "awaiting_decision"})
            store.save(settled_id, {"state": "submitted_settled"})
            (store.directory / f"{corrupt_id}.json").write_text("{", encoding="utf-8")
            (store.directory / f"{invalid_id}.json").write_text("[]", encoding="utf-8")
            (store.directory / "not-a-request.json").write_text("{}", encoding="utf-8")
            (store.directory / f"{'f' * 32}.lock").touch()
            (store.directory / f"{'0' * 32}.json").mkdir()

            self.assertEqual(
                store.list_request_ids(),
                [deferred_id, awaiting_id, settled_id, corrupt_id, invalid_id],
            )
            self.assertEqual(
                pending_deferred(store),
                [{"state": "deferred"}, {"state": "awaiting_decision"}],
            )

    def test_missing_directory_has_no_pending_requests(self):
        with tempfile.TemporaryDirectory() as directory:
            store = JsonRequestStateStore(Path(directory) / "missing")
            self.assertEqual(store.list_request_ids(), [])
            self.assertEqual(pending_deferred(store), [])

    def test_request_state_directory_explicit_override_wins(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            environment = {
                "HERDR_AGENT_REQUEST_STATE_DIR": str(root / "queue"),
                "HERDR_PLUGIN_STATE_DIR": str(root / "plugin"),
                "XDG_STATE_HOME": str(root / "state"),
                "HOME": str(root / "home"),
            }
            self.assertEqual(request_state_directory(environment), root / "queue")
            self.assertEqual(open_request_store(environment).directory, root / "queue")

    def test_request_state_directory_ignores_plugin_directory_and_uses_xdg(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plugin = root / "plugin"
            (plugin / "requests").mkdir(parents=True)
            environment = {
                "HERDR_PLUGIN_STATE_DIR": str(plugin),
                "XDG_STATE_HOME": str(root / "state"),
                "HOME": str(root / "home"),
            }
            self.assertEqual(
                request_state_directory(environment),
                root / "state/herdr-agent-messenger/requests",
            )

    def test_request_state_directory_uses_home_without_xdg(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertEqual(
                request_state_directory(
                    {"HOME": str(root), "HERDR_PLUGIN_STATE_DIR": str(root / "plugin")}
                ),
                root / ".local/state/herdr-agent-messenger/requests",
            )
        with mock.patch("request_lifecycle.Path.home", return_value=Path("/fallback")):
            self.assertEqual(
                request_state_directory({}),
                Path("/fallback/.local/state/herdr-agent-messenger/requests"),
            )

    def test_request_state_directory_legacy_fallback_only_without_new_directory(self):
        for use_xdg in (False, True):
            with self.subTest(use_xdg=use_xdg), tempfile.TemporaryDirectory() as directory:
                home = Path(directory)
                root = home / "state" if use_xdg else home / ".local/state"
                environment = {"HOME": str(home)}
                if use_xdg:
                    environment["XDG_STATE_HOME"] = str(root)
                legacy = root / "herdr-agent-labels/requests"
                legacy.mkdir(parents=True)
                self.assertEqual(request_state_directory(environment), legacy)
                current = root / "herdr-agent-messenger/requests"
                current.mkdir(parents=True)
                self.assertEqual(request_state_directory(environment), current)


class AgentRequestCliIntegrationTest(ham_test_support.IsolatedLedgerTest):
    def test_request_and_status_arguments_are_available(self):
        request = agent_skill_cli.parse_cli_arguments(
            [
                "request",
                "--route",
                "opaque-route",
                "--message",
                "inspect",
                "--output-lines",
                "40",
            ]
        )
        status = agent_skill_cli.parse_cli_arguments(
            ["request-status", "--request-id", "d" * 32]
        )
        cancel = agent_skill_cli.parse_cli_arguments(
            ["request-cancel", "--request-id", "d" * 32]
        )

        self.assertEqual(request.command, "request")
        self.assertEqual(request.route, "opaque-route")
        self.assertEqual(request.output_lines, 40)
        self.assertEqual(status.command, "request-status")
        self.assertEqual(status.request_id, "d" * 32)
        self.assertEqual(cancel.command, "request-cancel")
        self.assertEqual(cancel.request_id, "d" * 32)

    def test_request_command_uses_injected_resolver_without_herdr_wait(self):
        sender = agent_skill_cli.AgentRecord(
            host="local",
            name="sender",
            pane_id="w1:p1",
            workspace_id="w1",
            workspace_label="one",
            status="idle",
            session_id="sender-session",
            cwd="/work/one",
            local=True,
        )
        idle = agent_skill_cli.AgentRecord(
            host="local",
            name="worker",
            pane_id="w1:p2",
            workspace_id="w1",
            workspace_label="one",
            status="idle",
            session_id="worker-session",
            cwd="/work/one",
            local=True,
        )
        working = replace(idle, status="working")
        done = replace(idle, status="done")
        recipients = iter([idle, working, done])
        output_reads = iter(["before\n", "before\nanswer\n"])
        commands = []

        def runner(command, *, timeout):
            commands.append((command, timeout))
            if command[1:3] == ["agent", "read"]:
                return subprocess.CompletedProcess(command, 0, next(output_reads), "")
            return subprocess.CompletedProcess(command, 0, "{}\n", "")

        store = MemoryStore()
        clock = FakeClock()
        with mock.patch.object(agent_skill_cli, "current_sender", return_value=sender):
            result = agent_skill_cli.request_command(
                host="local",
                label="worker",
                message="inspect",
                timeout_ms=3_000,
                output_lines=10,
                output_chars=500,
                environment=self.environment,
                resolver=lambda: next(recipients),
                state_store=store,
                command_runner=runner,
                request_id="b" * 32,
                monotonic=clock.monotonic,
                sleep=clock.sleep,
            )

        prompt = next(
            command for command, _timeout in commands if command[2] == "prompt"
        )
        self.assertNotIn("--wait", prompt)
        self.assertEqual(result["state"], "submitted_settled")
        self.assertEqual(result["request_id"], "b" * 32)
        self.assertEqual(result["response"]["text"], "answer")
        self.assertFalse(any(key.startswith("_") for key in result))
        self.assertEqual(
            agent_skill_cli.request_status_command(
                request_id="b" * 32,
                state_store=store,
            ),
            {
                "request_id": "b" * 32,
                "state": "submitted_settled",
                "terminal": True,
                "submitted": True,
                "changed": False,
            },
        )

    def test_request_persists_a_safely_refreshed_v2_route(self):
        sender = agent_skill_cli.AgentRecord(
            host="local",
            name="sender",
            pane_id="w1:p1",
            workspace_id="w1",
            workspace_label="one",
            status="idle",
            session_id="sender-session",
            cwd="/work/one",
            local=True,
        )
        original = agent_skill_cli.AgentRecord(
            host="local",
            name="worker",
            pane_id="w1:p2",
            workspace_id="w1",
            workspace_label="one",
            status="idle",
            session_id="old-session",
            cwd="/work/one",
            local=True,
            revision=1,
            agent_kind="codex",
            terminal_id="worker-terminal",
        )
        replacement = replace(
            original,
            session_id="new-session",
            revision=2,
        )
        snapshots = iter(
            [
                [replacement],
                [replace(replacement, status="working")],
                [replace(replacement, status="done")],
            ]
        )
        outputs = iter(["before\n", "before\nanswer\n"])

        def runner(command, *, timeout):
            if command[1:3] == ["agent", "read"]:
                return subprocess.CompletedProcess(command, 0, next(outputs), "")
            return subprocess.CompletedProcess(command, 0, "{}\n", "")

        store = MemoryStore()
        clock = FakeClock()
        token = agent_skill_cli.encode_agent_route(original)
        with (
            mock.patch.object(agent_skill_cli, "current_sender", return_value=sender),
            mock.patch.object(
                agent_skill_cli,
                "discover_agents",
                side_effect=lambda *_arguments: next(snapshots),
            ),
        ):
            result = agent_skill_cli.request_command(
                host="local",
                route=token,
                message="inspect",
                timeout_ms=3_000,
                output_lines=10,
                output_chars=500,
                environment=self.environment,
                state_store=store,
                command_runner=runner,
                request_id="e" * 32,
                monotonic=clock.monotonic,
                sleep=clock.sleep,
                verbose=True,
            )

        refreshed_route = agent_skill_cli.encode_agent_route(replacement)
        self.assertTrue(result["route_refreshed"])
        self.assertEqual(result["route"], refreshed_route)
        self.assertEqual(result["recipient"]["route"], refreshed_route)
        self.assertEqual(
            agent_skill_cli.request_status_command(
                request_id="e" * 32,
                state_store=store,
                verbose=True,
            )["route"],
            refreshed_route,
        )

    def test_transport_classifies_bounded_runner_timeout_as_unknown(self):
        target = mock.Mock(identity="worker", status="idle", target="worker")
        sender = mock.Mock(identity="sender")
        result = subprocess.CompletedProcess(
            ["herdr"],
            1,
            "",
            "Command '['herdr']' timed out after 5.0 seconds",
        )
        transport = agent_skill_cli.AgentRequestTransport(
            resolver=lambda: target,
            sender_identity=sender.identity,
            environment=self.environment,
            command_runner=mock.Mock(return_value=result),
        )

        submission = transport.submit(target, "prompt")

        self.assertIsNone(submission.accepted)
        self.assertTrue(submission.timed_out)
        self.assertNotIn("['herdr']", submission.error)

    def test_status_command_refreshes_a_nonterminal_stored_request(self):
        sender = agent_skill_cli.AgentRecord(
            host="local",
            name="sender",
            pane_id="w1:p1",
            workspace_id="w1",
            workspace_label="one",
            status="idle",
            session_id="sender-session",
            cwd="/work/one",
            local=True,
        )
        idle = agent_skill_cli.AgentRecord(
            host="local",
            name="worker",
            pane_id="w1:p2",
            workspace_id="w1",
            workspace_label="one",
            status="idle",
            session_id="worker-session",
            cwd="/work/one",
            local=True,
        )
        working = replace(idle, status="working")
        done = replace(idle, status="done")
        recipients = iter([idle, working, working, working])
        request_reads = iter(["before\n", "before\npartial\n"])

        def request_runner(command, *, timeout):
            if command[1:3] == ["agent", "read"]:
                return subprocess.CompletedProcess(
                    command,
                    0,
                    next(request_reads),
                    "",
                )
            return subprocess.CompletedProcess(command, 0, "{}\n", "")

        store = MemoryStore()
        clock = FakeClock()
        with mock.patch.object(agent_skill_cli, "current_sender", return_value=sender):
            timed_out = agent_skill_cli.request_command(
                host="local",
                label="worker",
                message="inspect",
                timeout_ms=3_000,
                output_lines=10,
                output_chars=500,
                environment=self.environment,
                resolver=lambda: next(recipients),
                state_store=store,
                command_runner=request_runner,
                request_id="c" * 32,
                monotonic=clock.monotonic,
                sleep=clock.sleep,
            )

        self.assertEqual(timed_out["state"], "submitted_working")
        self.assertFalse(timed_out["terminal"])
        stored = store.load("c" * 32)
        self.assertIsNone(stored["_context"]["label"])
        self.assertIsInstance(stored["_context"]["route"], str)
        status_runner = mock.Mock(
            return_value=subprocess.CompletedProcess(
                ["herdr"],
                0,
                "before\nanswer\n",
                "",
            )
        )
        current_route = agent_skill_cli.encode_agent_route(done)
        with mock.patch.object(
            agent_skill_cli,
            "resolve_or_refresh_recipient",
            return_value=agent_skill_cli.RouteResolution(
                done,
                route_refreshed=False,
                route=current_route,
            ),
        ) as resolve:
            settled = agent_skill_cli.request_status_command(
                request_id="c" * 32,
                environment=self.environment,
                state_store=store,
                command_runner=status_runner,
            )

        self.assertEqual(settled["state"], "submitted_settled")
        self.assertEqual(settled["response"]["text"], "answer")
        self.assertTrue(settled["changed"])
        self.assertFalse(any(key.startswith("_") for key in settled))
        resolve.assert_called_once_with(
            host="local",
            label=None,
            route=stored["_context"]["route"],
            environment=self.environment,
        )


class RequestBoundaryIntegrationTest(ham_test_support.IsolatedLedgerTest):
    @staticmethod
    def target():
        return agent_skill_cli.AgentRecord(
            "local", "worker", "w:p1", "w", "work", "idle", "worker-session", "/work", True,
        )

    def test_replacement_never_overwrites_the_saved_request_route(self):
        for replace_during_wait in (True, False):
            with self.subTest(replace_during_wait=replace_during_wait):
                worker = self.target()
                replacement = replace(worker, session_id="new-session", status="working")
                sender = replace(worker, name="sender", pane_id="w:p2", session_id="sender-session")
                clock = FakeClock()
                store = MemoryStore()
                observations = [worker, replacement] if replace_during_wait else [
                    worker, replace(worker, status="working"), replace(worker, status="working"),
                ]
                runner = mock.Mock(return_value=subprocess.CompletedProcess(["herdr"], 0, "before\n", ""))
                with mock.patch.object(agent_skill_cli, "current_sender", return_value=sender):
                    result = agent_skill_cli.request_command(
                        host="local", label="worker", message="work", timeout_ms=1000,
                        output_lines=10, output_chars=500, environment=self.environment,
                        resolver=mock.Mock(side_effect=observations), state_store=store,
                        command_runner=runner, request_id="a" * 32,
                        monotonic=clock.monotonic, sleep=clock.sleep,
                        verbose=True,
                    )
                if not replace_during_wait:
                    runner.reset_mock()
                    result = agent_skill_cli.request_status_command(
                        request_id="a" * 32, environment=self.environment, state_store=store,
                        resolver=lambda: replacement, command_runner=runner,
                        verbose=True,
                    )
                    runner.assert_not_called()
                else:
                    reads = [call for call in runner.call_args_list if call.args[0][1:3] == ["agent", "read"]]
                    self.assertEqual(len(reads), 1)
                expected_route = agent_skill_cli.encode_agent_route(worker)
                self.assertEqual(result["phase"], "recipient_changed")
                self.assertEqual(result["route"], expected_route)
                self.assertEqual(result["recipient"]["route"], expected_route)
                self.assertEqual(store.load("a" * 32)["_context"]["route"], expected_route)

    def test_interrupted_transport_does_not_claim_submission_failed(self):
        for local, returncode in ((False, 255), (False, -15), (True, -15)):
            with self.subTest(local=local, returncode=returncode):
                target = replace(self.target(), local=local, host="remote" if not local else "local")
                transport = agent_skill_cli.AgentRequestTransport(
                    resolver=lambda: target, sender_identity="sender", environment=self.environment,
                    command_runner=mock.Mock(return_value=subprocess.CompletedProcess(
                        ["herdr"], returncode, "", "Connection lost",
                    )),
                )
                self.assertIsNone(transport.submit(target, "work").accepted)


if __name__ == "__main__":
    unittest.main()
