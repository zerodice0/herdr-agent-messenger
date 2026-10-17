"""Deterministic bounded dispatch for already-tailored agent requests."""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import CancelledError
from dataclasses import dataclass
import json
import queue
import threading
from typing import Any, Protocol


DEFAULT_BATCH_WORKERS = 4
MAX_BATCH_WORKERS = 8

SUCCEEDED = "succeeded"
SUBMITTED = "submitted"
DEFERRED = "deferred"
FAILED = "failed"
TIMED_OUT = "timeout"
CANCELLED = "cancelled"

DELIVERED_STATUSES = frozenset((SUCCEEDED, SUBMITTED, DEFERRED))
RESULT_STATUSES = frozenset(
    (SUCCEEDED, SUBMITTED, DEFERRED, FAILED, TIMED_OUT, CANCELLED)
)


class BatchDispatchError(Exception):
    """Reject malformed batch-level input before dispatch starts."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class BatchRequest:
    """One route or address and its already-tailored instruction."""

    route: str
    message: str
    address: str = ""


@dataclass(frozen=True)
class DispatchOutcome:
    """Small transport-neutral result returned by an injected single sender."""

    status: str
    error: str = ""
    detail: str = ""
    route: str = ""
    request_id: str = ""
    output: str = ""
    truncated: bool = False
    correlated: bool = False
    state: str = ""
    address: str = ""


class BatchSender(Protocol):
    def __call__(self, request: BatchRequest) -> DispatchOutcome:
        """Send one pre-tailored request and return its normalized outcome."""


@dataclass(frozen=True)
class BatchResult:
    route: str
    status: str
    error: str = ""
    detail: str = ""
    request_id: str = ""
    output: str = ""
    truncated: bool = False
    correlated: bool = False
    state: str = ""
    address: str = ""

    def payload(self) -> dict[str, Any]:
        value: dict[str, Any] = {"route": self.route, "status": self.status}
        if self.address:
            value["address"] = self.address
        if self.state:
            value["state"] = self.state
        if self.error:
            value["error"] = self.error
        if self.detail:
            value["detail"] = self.detail
        if self.request_id:
            value["request_id"] = self.request_id
            value["response"] = {
                "output": self.output,
                "truncated": self.truncated,
                "correlated": self.correlated,
            }
        return value


def parse_batch_json(value: str) -> list[object]:
    """Decode either a request array or ``{"requests": [...]}`` wrapper."""

    try:
        decoded = json.loads(value)
    except (json.JSONDecodeError, TypeError):
        raise BatchDispatchError(
            "invalid_batch_json",
            "Batch input must be valid JSON.",
        ) from None
    if isinstance(decoded, dict):
        decoded = decoded.get("requests")
    if not isinstance(decoded, list):
        raise BatchDispatchError(
            "invalid_batch",
            "Batch input must be a JSON array of route/message objects.",
        )
    if not decoded:
        raise BatchDispatchError(
            "empty_batch",
            "Batch input must contain at least one request.",
        )
    return decoded


def _failure(
    route: str, error: str, detail: str = "", *, address: str = "",
) -> BatchResult:
    return BatchResult(route, FAILED, error, detail, address=address)


def _normalize_sender_outcome(
    request: BatchRequest,
    outcome: object,
) -> BatchResult:
    if not isinstance(outcome, DispatchOutcome) or outcome.status not in RESULT_STATUSES:
        return _failure(
            request.route,
            "invalid_sender_result",
            "The single-request sender returned an invalid outcome.",
            address=request.address,
        )
    return BatchResult(
        outcome.route or request.route,
        outcome.status,
        outcome.error,
        outcome.detail,
        outcome.request_id,
        outcome.output,
        outcome.truncated,
        outcome.correlated,
        outcome.state,
        outcome.address or request.address,
    )


def _exception_result(request: BatchRequest, error: Exception) -> BatchResult:
    if isinstance(error, TimeoutError):
        return BatchResult(
            request.route, TIMED_OUT, "timeout", str(error), address=request.address,
        )
    if isinstance(error, CancelledError):
        return BatchResult(
            request.route, CANCELLED, "cancelled", str(error), address=request.address,
        )
    code = getattr(error, "code", "dispatch_failed")
    message = getattr(error, "message", str(error))
    return _failure(
        request.route,
        code if isinstance(code, str) else "dispatch_failed",
        message if isinstance(message, str) else str(message),
        address=request.address,
    )


def _prepare_requests(
    items: Sequence[object],
    resolve_request: Callable[[BatchRequest], tuple[BatchRequest, str]] | None,
    cancel_event: threading.Event | None,
) -> tuple[list[BatchResult | None], list[tuple[int, BatchRequest]]]:
    routes = [
        item.get("route")
        for item in items
        if isinstance(item, Mapping)
        and "address" not in item
        and isinstance(item.get("route"), str)
        and item.get("route")
    ]
    duplicate_routes = {
        route for route, count in Counter(routes).items() if count > 1
    }
    results: list[BatchResult | None] = [None] * len(items)
    prepared: list[tuple[int, BatchRequest, str]] = []
    for index, item in enumerate(items):
        if not isinstance(item, Mapping) or (
            ("route" in item) == ("address" in item)
        ):
            results[index] = _failure("", "invalid_request")
            continue
        route = item.get("route", "")
        address = item.get("address", "")
        message = item.get("message")
        if "route" in item and (not isinstance(route, str) or not route):
            results[index] = _failure("", "invalid_route")
            continue
        if "address" in item and (not isinstance(address, str) or not address):
            results[index] = _failure("", "invalid_address")
            continue
        if resolve_request is None and route in duplicate_routes:
            results[index] = _failure(route, "duplicate_route")
            continue
        if not isinstance(message, str):
            results[index] = _failure(route, "invalid_message", address=address)
            continue
        if not message.strip():
            results[index] = _failure(route, "empty_message", address=address)
            continue
        request = BatchRequest(route, message, address)
        identity = route or address
        if cancel_event is None or not cancel_event.is_set():
            try:
                if resolve_request is not None:
                    request, identity = resolve_request(request)
                    if not request.address and address:
                        request = BatchRequest(request.route, request.message, address)
                elif address:
                    raise BatchDispatchError(
                        "invalid_address", "Address dispatch requires a resolver."
                    )
            except Exception as error:
                results[index] = _exception_result(request, error)
                continue
        prepared.append((index, request, identity))
    identities = Counter(identity for _index, _request, identity in prepared)
    ready: list[tuple[int, BatchRequest]] = []
    for index, request, identity in prepared:
        if identities[identity] > 1:
            results[index] = _failure(
                request.route, "duplicate_route", address=request.address,
            )
        else:
            ready.append((index, request))
    return results, ready


def _aggregate_status(results: Sequence[BatchResult]) -> str:
    delivered = sum(result.status in DELIVERED_STATUSES for result in results)
    if delivered == len(results):
        return SUCCEEDED
    if delivered:
        return "partial"
    return FAILED


def dispatch_batch(
    items: Sequence[object],
    send_one: BatchSender | Callable[[BatchRequest], DispatchOutcome],
    *,
    max_workers: int = DEFAULT_BATCH_WORKERS,
    cancel_event: threading.Event | None = None,
    resolve_request: Callable[[BatchRequest], tuple[BatchRequest, str]] | None = None,
    before_dispatch: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """Dispatch valid items concurrently and return results in input order.

    The optional resolver converts addresses to routes and supplies a target
    identity for duplicate detection before any prompt is submitted. Transport
    and request lifecycle semantics belong to the injected single-request sender.
    The optional pre-submit gate runs after all resolution and before workers or
    submission side effects. Its exception aborts the entire batch, not one item.
    """

    if isinstance(max_workers, bool) or not isinstance(max_workers, int):
        raise BatchDispatchError(
            "invalid_workers",
            "max_workers must be an integer.",
        )
    if max_workers <= 0 or max_workers > MAX_BATCH_WORKERS:
        raise BatchDispatchError(
            "invalid_workers",
            f"max_workers must be between 1 and {MAX_BATCH_WORKERS}.",
        )
    if not items:
        raise BatchDispatchError(
            "empty_batch",
            "Batch input must contain at least one request.",
        )

    result_slots, ready = _prepare_requests(items, resolve_request, cancel_event)
    if before_dispatch is not None:
        before_dispatch()
    pending: queue.Queue[tuple[int, BatchRequest]] = queue.Queue()
    for prepared in ready:
        pending.put(prepared)

    def worker() -> None:
        while cancel_event is None or not cancel_event.is_set():
            try:
                index, request = pending.get_nowait()
            except queue.Empty:
                return
            if cancel_event is not None and cancel_event.is_set():
                return
            try:
                outcome = send_one(request)
            except Exception as error:  # One transport failure must not abort peers.
                result_slots[index] = _exception_result(request, error)
            else:
                result_slots[index] = _normalize_sender_outcome(request, outcome)

    threads = [
        threading.Thread(target=worker, daemon=True)
        for _index in range(min(max_workers, len(ready)))
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    for index, result in enumerate(result_slots):
        if result is not None:
            continue
        item = items[index]
        route = item.get("route", "") if isinstance(item, Mapping) else ""
        result_slots[index] = BatchResult(
            route if isinstance(route, str) else "",
            CANCELLED,
            "cancelled",
            address=(
                item["address"]
                if isinstance(item, Mapping) and isinstance(item.get("address"), str)
                else ""
            ),
        )

    final_results = [result for result in result_slots if result is not None]
    return {
        "status": _aggregate_status(final_results),
        "results": [result.payload() for result in final_results],
    }
