import ham_test_support  # Protect the live ledger before product imports.
import threading
import time
import unittest
from concurrent.futures import CancelledError

import agent_batch_dispatch
from agent_output import compact_batch_item


class CodedError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class AgentBatchDispatchTest(unittest.TestCase):
    def test_results_keep_input_order_with_bounded_concurrency(self):
        active = 0
        peak = 0
        lock = threading.Lock()

        def send_one(request):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            time.sleep(0.004 * (6 - int(request.route)))
            with lock:
                active -= 1
            return agent_batch_dispatch.DispatchOutcome(
                agent_batch_dispatch.SUCCEEDED
            )

        items = [
            {"route": str(index), "message": f"instruction {index}"}
            for index in range(6)
        ]
        payload = agent_batch_dispatch.dispatch_batch(
            items,
            send_one,
            max_workers=2,
        )

        self.assertEqual(
            [result["route"] for result in payload["results"]],
            [str(index) for index in range(6)],
        )
        self.assertEqual(peak, 2)
        self.assertEqual(payload["status"], agent_batch_dispatch.SUCCEEDED)

    def test_all_duplicate_routes_fail_without_dispatch(self):
        calls = []

        def send_one(request):
            calls.append(request)
            return agent_batch_dispatch.DispatchOutcome(
                agent_batch_dispatch.SUCCEEDED
            )

        payload = agent_batch_dispatch.dispatch_batch(
            [
                {"route": "same", "message": "first"},
                {"route": "unique", "message": "valid"},
                {"route": "same", "message": "second"},
            ],
            send_one,
        )

        self.assertEqual([request.route for request in calls], ["unique"])
        self.assertEqual(payload["status"], "partial")
        self.assertEqual(
            payload["results"],
            [
                {
                    "route": "same",
                    "status": agent_batch_dispatch.FAILED,
                    "error": "duplicate_route",
                },
                {
                    "route": "unique",
                    "status": agent_batch_dispatch.SUCCEEDED,
                },
                {
                    "route": "same",
                    "status": agent_batch_dispatch.FAILED,
                    "error": "duplicate_route",
                },
            ],
        )

    def test_empty_message_and_sender_route_are_per_target_failures(self):
        def send_one(request):
            if request.route == "self":
                raise CodedError(
                    "recipient_is_sender",
                    "The current sender cannot also be the recipient.",
                )
            return agent_batch_dispatch.DispatchOutcome(
                agent_batch_dispatch.SUCCEEDED
            )

        payload = agent_batch_dispatch.dispatch_batch(
            [
                {"route": "empty", "message": " \n"},
                {"route": "self", "message": "do not send"},
            ],
            send_one,
        )

        self.assertEqual(payload["status"], agent_batch_dispatch.FAILED)
        self.assertEqual(payload["results"][0]["error"], "empty_message")
        self.assertEqual(payload["results"][1]["error"], "recipient_is_sender")

    def test_timeout_cancel_and_submission_are_preserved(self):
        def send_one(request):
            if request.route == "timeout":
                raise TimeoutError("deadline reached")
            if request.route == "cancel":
                raise CancelledError()
            return agent_batch_dispatch.DispatchOutcome(
                agent_batch_dispatch.SUBMITTED
            )

        payload = agent_batch_dispatch.dispatch_batch(
            [
                {"route": "timeout", "message": "one"},
                {"route": "submit", "message": "two"},
                {"route": "cancel", "message": "three"},
            ],
            send_one,
        )

        self.assertEqual(payload["status"], "partial")
        self.assertEqual(
            [result["status"] for result in payload["results"]],
            [
                agent_batch_dispatch.TIMED_OUT,
                agent_batch_dispatch.SUBMITTED,
                agent_batch_dispatch.CANCELLED,
            ],
        )

    def test_deferred_is_delivered_once_and_keeps_request_metadata(self):
        calls = []

        def send_one(request):
            calls.append(request)
            return agent_batch_dispatch.DispatchOutcome(
                agent_batch_dispatch.DEFERRED,
                request_id="held-request",
                state="awaiting_decision",
                address="remote/blue-frog",
            )

        payload = agent_batch_dispatch.dispatch_batch(
            [{"route": "held-route", "message": "work"}],
            send_one,
        )

        self.assertEqual(len(calls), 1)
        self.assertEqual(payload["status"], agent_batch_dispatch.SUCCEEDED)
        self.assertEqual(compact_batch_item(payload["results"][0]), {
            "address": "remote/blue-frog",
            "status": agent_batch_dispatch.DEFERRED,
            "request_id": "held-request",
            "state": "awaiting_decision",
            "response": {"text": "", "truncated": False},
        })

    def test_deferred_and_submitted_count_as_delivered_in_summary(self):
        statuses = {
            "settled": agent_batch_dispatch.SUCCEEDED,
            "submitted": agent_batch_dispatch.SUBMITTED,
            "held": agent_batch_dispatch.DEFERRED,
        }

        def send_one(request):
            return agent_batch_dispatch.DispatchOutcome(statuses[request.route])

        items = [{"route": route, "message": "work"} for route in statuses]
        payload = agent_batch_dispatch.dispatch_batch(items, send_one)
        self.assertEqual(payload["status"], agent_batch_dispatch.SUCCEEDED)
        self.assertEqual(
            [item["status"] for item in payload["results"]], list(statuses.values()),
        )

        payload = agent_batch_dispatch.dispatch_batch(
            [{"route": "held", "message": "work"}, {"route": "bad", "message": ""}],
            send_one,
        )
        self.assertEqual(payload["status"], "partial")
        self.assertEqual(
            [item["status"] for item in payload["results"]],
            [agent_batch_dispatch.DEFERRED, agent_batch_dispatch.FAILED],
        )

    def test_deferred_sender_does_not_bypass_duplicate_target_checks(self):
        calls = []

        def send_one(request):
            calls.append(request)
            return agent_batch_dispatch.DispatchOutcome(agent_batch_dispatch.DEFERRED)

        payload = agent_batch_dispatch.dispatch_batch(
            [
                {"route": "duplicate", "message": "one"},
                {"route": "duplicate", "message": "two"},
                {"route": "held", "message": "three"},
            ],
            send_one,
        )

        self.assertEqual([request.route for request in calls], ["held"])
        self.assertEqual(payload["status"], "partial")
        self.assertEqual(
            [item["status"] for item in payload["results"]],
            [agent_batch_dispatch.FAILED, agent_batch_dispatch.FAILED,
             agent_batch_dispatch.DEFERRED],
        )

    def test_compact_result_preserves_response_and_failure_metadata(self):
        def resolve(request):
            return agent_batch_dispatch.BatchRequest(
                request.address, request.message, request.address,
            ), request.address

        def send_one(request):
            if request.address == "local/bad":
                return agent_batch_dispatch.DispatchOutcome(
                    agent_batch_dispatch.FAILED,
                    error="submission_failed",
                    detail="Cannot submit.",
                    request_id="failed-request",
                    state="submission_failed",
                )
            return agent_batch_dispatch.DispatchOutcome(
                agent_batch_dispatch.SUCCEEDED,
                request_id="settled-request",
                state="submitted_settled",
                output="Ready.",
                truncated=True,
                correlated=True,
            )

        payload = agent_batch_dispatch.dispatch_batch(
            [{"address": address, "message": "work"}
             for address in ("local/good", "local/bad")],
            send_one,
            resolve_request=resolve,
        )
        self.assertEqual(compact_batch_item(payload["results"][0]), {
            "address": "local/good",
            "status": "succeeded",
            "request_id": "settled-request",
            "state": "submitted_settled",
            "response": {"text": "Ready.", "truncated": True},
        })
        failed = compact_batch_item(payload["results"][1])
        self.assertEqual(failed["address"], "local/bad")
        self.assertEqual(failed["state"], "submission_failed")
        self.assertEqual(failed["error_code"], "submission_failed")
        self.assertEqual(failed["error"], "Cannot submit.")

    def test_cancel_event_marks_requests_that_never_started(self):
        cancel_event = threading.Event()

        def send_one(_request):
            cancel_event.set()
            return agent_batch_dispatch.DispatchOutcome(
                agent_batch_dispatch.CANCELLED,
                "cancelled",
            )

        payload = agent_batch_dispatch.dispatch_batch(
            [
                {"route": "first", "message": "one"},
                {"route": "second", "message": "two"},
            ],
            send_one,
            max_workers=1,
            cancel_event=cancel_event,
        )

        self.assertEqual(
            [result["status"] for result in payload["results"]],
            [agent_batch_dispatch.CANCELLED, agent_batch_dispatch.CANCELLED],
        )

    def test_worker_limit_is_enforced(self):
        items = [{"route": "one", "message": "instruction"}]
        for workers in (0, agent_batch_dispatch.MAX_BATCH_WORKERS + 1):
            with (
                self.subTest(workers=workers),
                self.assertRaises(agent_batch_dispatch.BatchDispatchError) as raised,
            ):
                agent_batch_dispatch.dispatch_batch(
                    items,
                    lambda _request: agent_batch_dispatch.DispatchOutcome(
                        agent_batch_dispatch.SUCCEEDED
                    ),
                    max_workers=workers,
                )
            self.assertEqual(raised.exception.code, "invalid_workers")

    def test_address_items_are_resolved_before_dispatch(self):
        sent = []

        def resolve(request):
            self.assertEqual(request.address, "local/white-bison")
            return agent_batch_dispatch.BatchRequest("verified", request.message), "agent-1"

        def send(request):
            sent.append(request)
            return agent_batch_dispatch.DispatchOutcome(agent_batch_dispatch.SUBMITTED)

        payload = agent_batch_dispatch.dispatch_batch(
            [{"address": "local/white-bison", "message": "work"}],
            send,
            resolve_request=resolve,
        )
        self.assertEqual([request.route for request in sent], ["verified"])
        self.assertEqual(payload["results"][0]["status"], "submitted")
        self.assertEqual(payload["results"][0]["route"], "verified")
        self.assertEqual(payload["results"][0]["address"], "local/white-bison")

    def test_both_or_neither_address_keys_are_invalid_requests(self):
        sent = []
        payload = agent_batch_dispatch.dispatch_batch(
            [
                {"route": "token", "address": "local/label", "message": "work"},
                {"message": "work"},
            ],
            lambda request: sent.append(request),
        )
        self.assertEqual(
            [item["error"] for item in payload["results"]],
            ["invalid_request", "invalid_request"],
        )
        self.assertFalse(sent)

    def test_distinct_routes_and_addresses_for_one_identity_are_duplicates(self):
        sent = []

        def resolve(request):
            return (
                agent_batch_dispatch.BatchRequest(
                    request.route or "resolved-address", request.message
                ),
                "same-occupant",
            )

        payload = agent_batch_dispatch.dispatch_batch(
            [
                {"route": "old-route", "message": "one"},
                {"route": "new-route", "message": "two"},
                {"address": "local/label", "message": "three"},
            ],
            lambda request: sent.append(request),
            resolve_request=resolve,
        )
        self.assertEqual(
            [item["error"] for item in payload["results"]], ["duplicate_route"] * 3
        )
        self.assertFalse(sent)

    def test_resolution_errors_preserve_codes_and_do_not_block_other_items(self):
        sent = []

        def resolve(request):
            if request.address:
                raise CodedError("agent_ambiguous", "multiple occupants")
            return request, "valid-occupant"

        def send(request):
            sent.append(request)
            return agent_batch_dispatch.DispatchOutcome(agent_batch_dispatch.SUCCEEDED)

        payload = agent_batch_dispatch.dispatch_batch(
            [
                {"address": "local/label", "message": "one"},
                {"route": "valid", "message": "two"},
            ],
            send,
            resolve_request=resolve,
        )
        self.assertEqual(payload["results"][0]["error"], "agent_ambiguous")
        self.assertEqual(payload["results"][0]["address"], "local/label")
        self.assertEqual(payload["results"][1]["status"], "succeeded")
        self.assertEqual([request.route for request in sent], ["valid"])

    def test_pre_dispatch_gate_observes_all_resolutions_before_any_submission(self):
        resolved = []
        sent = []

        def resolve(request):
            resolved.append(request.route)
            return request, request.route

        def gate():
            self.assertEqual(resolved, ["one", "two", "three"])
            self.assertEqual(sent, [])

        payload = agent_batch_dispatch.dispatch_batch(
            [{"route": route, "message": "work"} for route in ("one", "two", "three")],
            lambda request: (
                sent.append(request.route)
                or agent_batch_dispatch.DispatchOutcome(agent_batch_dispatch.SUBMITTED)
            ),
            resolve_request=resolve, before_dispatch=gate,
        )
        self.assertEqual(payload["status"], "succeeded")
        self.assertCountEqual(sent, ["one", "two", "three"])

    def test_pre_dispatch_exception_aborts_peers_despite_per_item_resolution_failure(self):
        resolved = []
        sent = []

        def resolve(request):
            resolved.append(request.route)
            if request.route == "blocked":
                raise CodedError("use_mailbox", "Use mailbox")
            return request, request.route

        refusal = CodedError("use_mailbox", "Use mailbox")

        def gate():
            self.assertEqual(resolved, ["allowed", "blocked", "also-allowed"])
            raise refusal

        with self.assertRaises(CodedError) as caught:
            agent_batch_dispatch.dispatch_batch(
                [{"route": route, "message": "work"} for route in ("allowed", "blocked", "also-allowed")],
                lambda request: sent.append(request), resolve_request=resolve,
                before_dispatch=gate,
            )
        self.assertIs(caught.exception, refusal)
        self.assertEqual(sent, [])

    def test_json_parser_accepts_array_and_requests_wrapper(self):
        expected = [{"route": "one", "message": "instruction"}]
        self.assertEqual(
            agent_batch_dispatch.parse_batch_json(
                '[{"route":"one","message":"instruction"}]'
            ),
            expected,
        )
        self.assertEqual(
            agent_batch_dispatch.parse_batch_json(
                '{"requests":[{"route":"one","message":"instruction"}]}'
            ),
            expected,
        )


if __name__ == "__main__":
    unittest.main()
