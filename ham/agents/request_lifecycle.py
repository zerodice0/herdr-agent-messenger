"""Conservative request lifecycle state machine for Herdr agents.

The state machine deliberately knows nothing about labels, route token versions,
SSH, or Herdr subprocess syntax.  Those details are supplied by a transport so a
resolver can be replaced without changing request-state semantics.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import json
import os
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
import re
import subprocess
import tempfile
import time
from typing import Any, Callable, ContextManager, Generic, Iterator, Mapping, Protocol, TypeVar


class RequestState(str, Enum):
    SUBMISSION_FAILED = "submission_failed"
    SUBMITTED_WORKING = "submitted_working"
    SUBMITTED_SETTLED = "submitted_settled"
    SUBMITTED_UNKNOWN = "submitted_unknown"
    DEFERRED = "deferred"
    AWAITING_DECISION = "awaiting_decision"


@dataclass(frozen=True)
class SubmissionResult:
    """Whether the prompt command definitely accepted or rejected a request."""

    accepted: bool | None
    error: str = ""
    timed_out: bool = False
    code: str = ""

    @classmethod
    def submitted(cls) -> "SubmissionResult":
        return cls(True)

    @classmethod
    def failed(cls, error: str, *, code: str = "") -> "SubmissionResult":
        return cls(False, error=error, code=code)

    @classmethod
    def unknown(
        cls,
        error: str,
        *,
        timed_out: bool = False,
        code: str = "",
    ) -> "SubmissionResult":
        return cls(None, error=error, timed_out=timed_out, code=code)


@dataclass(frozen=True)
class OutputSnapshot:
    """A bounded tail of terminal output, never an unbounded transcript."""

    text: str
    truncated: bool = False
    anchored: bool = False


class RequestTarget(Protocol):
    identity: str
    status: str


TargetT = TypeVar("TargetT", bound=RequestTarget)


class RequestTransport(Protocol, Generic[TargetT]):
    """Small boundary between request semantics and the current route version."""

    def resolve(self) -> TargetT:
        """Resolve the current recipient occupant."""

    def describe(self, target: TargetT) -> Mapping[str, Any]:
        """Return JSON-safe recipient metadata."""

    def is_focused(self, target: TargetT) -> bool:
        """Return whether the recipient pane currently has focus."""

    def recipient_is_sender(self, target: TargetT) -> bool:
        """Return whether the resolved recipient is the initiating sender."""

    def submit(self, target: TargetT, prompt: str) -> SubmissionResult:
        """Submit without using Herdr's settled-state wait."""

    def read_output(
        self,
        target: TargetT,
        *,
        max_lines: int,
        max_chars: int,
    ) -> OutputSnapshot:
        """Read a bounded output tail for the resolved occupant."""


class RequestStateStore(Protocol):
    def save(self, request_id: str, payload: Mapping[str, Any]) -> None:
        """Atomically save the latest queryable request state."""

    def load(self, request_id: str) -> dict[str, Any] | None:
        """Load one request state, returning None when it does not exist."""

    def lock(self, request_id: str) -> ContextManager[None]:
        """Hold an exclusive lock while updating one request."""

    def list_request_ids(self) -> list[str]:
        """List the stored request IDs."""


_REQUEST_ID_PATTERN = re.compile(r"[0-9a-f]{32}")


class JsonRequestStateStore:
    """Private, atomic JSON state files used by the CLI's status command."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory

    @staticmethod
    def _validate_request_id(request_id: str) -> None:
        if not _REQUEST_ID_PATTERN.fullmatch(request_id):
            raise ValueError("The request ID is invalid.")

    def _path(self, request_id: str) -> Path:
        self._validate_request_id(request_id)
        return self.directory / f"{request_id}.json"

    def save(self, request_id: str, payload: Mapping[str, Any]) -> None:
        destination = self._path(request_id)
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            dir=self.directory,
            prefix=f".{request_id}.",
            suffix=".tmp",
        )
        temporary = Path(temporary_name)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                descriptor = -1
                json.dump(payload, stream, ensure_ascii=False, sort_keys=True)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, destination)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    def load(self, request_id: str) -> dict[str, Any] | None:
        path = self._path(request_id)
        try:
            with path.open(encoding="utf-8") as stream:
                payload = json.load(stream)
        except FileNotFoundError:
            return None
        if not isinstance(payload, dict):
            raise ValueError("The stored request state is invalid.")
        return payload

    @contextmanager
    def lock(self, request_id: str) -> Iterator[None]:
        self._validate_request_id(request_id)
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor = os.open(
            self.directory / f"{request_id}.lock", os.O_CREAT | os.O_RDWR, 0o600
        )
        try:
            os.fchmod(descriptor, 0o600)
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            os.close(descriptor)

    def list_request_ids(self) -> list[str]:
        if not self.directory.exists():
            return []
        return sorted(
            path.stem
            for path in self.directory.glob("*.json")
            if _REQUEST_ID_PATTERN.fullmatch(path.stem) and path.is_file()
        )


def request_state_directory(
    environment: Mapping[str, str] | None = None,
) -> Path:
    """Locate the shared queue independently of the plugin launch context."""

    values = os.environ if environment is None else environment
    configured = values.get("HERDR_AGENT_REQUEST_STATE_DIR")
    if configured:
        return Path(configured).expanduser()
    state_home = values.get("XDG_STATE_HOME")
    configured_home = values.get("HOME")
    home = Path(configured_home).expanduser() if configured_home else Path.home()
    root = Path(state_home).expanduser() if state_home else home / ".local/state"
    directory = root / "herdr-agent-messenger" / "requests"
    legacy = root / "herdr-agent-labels" / "requests"
    return legacy if not directory.exists() and legacy.exists() else directory


def open_request_store(
    environment: Mapping[str, str] | None = None,
) -> JsonRequestStateStore:
    return JsonRequestStateStore(request_state_directory(environment))


def _is_deferred(record: Mapping[str, Any]) -> bool:
    return record.get("state") in {
        RequestState.DEFERRED.value, RequestState.AWAITING_DECISION.value
    }


def _load_request(store: RequestStateStore, request_id: str) -> dict[str, Any]:
    record = store.load(request_id)
    if record is None:
        raise KeyError(request_id)
    return record


def _validate_deferred_record(
    store: RequestStateStore, record: dict[str, Any],
) -> dict[str, Any]:
    if _is_deferred(record) and (
        not isinstance(record.get("_defer"), dict)
        or not isinstance(record["_defer"].get("deadline"), (int, float))
        or not isinstance(record.get("defer"), dict)
    ):
        record.update(
            state=RequestState.SUBMISSION_FAILED.value,
            phase="request_state_invalid", terminal=True, submitted=False,
            error="The stored deferred request data is invalid.",
            error_code="request_state_invalid",
        )
        store.save(str(record["request_id"]), record)
    return record


def validate_deferred_record(
    store: RequestStateStore, request_id: str,
) -> dict[str, Any]:
    """Load held state and fail corrupt queue entries under the record lock."""
    with store.lock(request_id):
        return _validate_deferred_record(store, _load_request(store, request_id))


def set_deferred_decision(
    store: RequestStateStore, request_id: str, decision: str,
) -> dict[str, Any]:
    """Persist a push decision without submitting from the caller."""

    if decision != "push":
        raise ValueError("The deferred decision must be push.")
    with store.lock(request_id):
        record = _validate_deferred_record(store, _load_request(store, request_id))
        if _is_deferred(record):
            record["_defer"]["decision"] = decision
            store.save(request_id, record)
        return record


def mark_deferred_notified(
    store: RequestStateStore, request_id: str, notified: Mapping[str, Any],
) -> dict[str, Any]:
    with store.lock(request_id):
        record = _validate_deferred_record(store, _load_request(store, request_id))
        if _is_deferred(record):
            record["defer"]["notified"] = dict(notified)
            store.save(request_id, record)
        return record


def pending_deferred(store: RequestStateStore) -> list[dict[str, Any]]:
    records = []
    for request_id in store.list_request_ids():
        try:
            record = store.load(request_id)
        except (OSError, ValueError, UnicodeError):
            continue
        if record is not None and _is_deferred(record):
            records.append(record)
    return records


def limit_output(text: str, *, max_lines: int, max_chars: int) -> OutputSnapshot:
    """Keep only the newest configured lines and characters."""

    if max_lines <= 0 or max_chars <= 0:
        raise ValueError("Output limits must be greater than zero.")
    truncated = False
    lines = text.splitlines(keepends=True)
    if len(lines) > max_lines:
        lines = lines[-max_lines:]
        truncated = True
    bounded = "".join(lines)
    if len(bounded) > max_chars:
        bounded = bounded[-max_chars:]
        truncated = True
    return OutputSnapshot(bounded, truncated)


def _suffix_prefix_overlap(before: str, after: str) -> int:
    """Return the longest suffix of before that is a prefix of after."""

    maximum = min(len(before), len(after))
    if maximum == 0:
        return 0
    pattern = after[:maximum]
    values: list[str | None] = [*pattern, None, *before[-maximum:]]
    prefix = [0] * len(values)
    for index in range(1, len(values)):
        candidate = prefix[index - 1]
        while candidate and values[index] != values[candidate]:
            candidate = prefix[candidate - 1]
        if values[index] == values[candidate]:
            candidate += 1
        prefix[index] = candidate
    return min(prefix[-1], maximum)


def capture_new_output(
    before: OutputSnapshot,
    after: OutputSnapshot,
    *,
    max_lines: int,
    max_chars: int,
    anchor: str = "",
) -> OutputSnapshot:
    """Capture a bounded delta, falling back to the last prompt header."""

    anchored = False
    overlap = _suffix_prefix_overlap(before.text, after.text)
    if (
        overlap
        and overlap < len(before.text)
        and "\n" not in before.text[-overlap:]
    ):
        # A few coincidental characters are not a reliable rolling-tail anchor.
        overlap = 0
    if not before.text:
        delta = after.text
        correlation_lost = before.truncated
    elif overlap == 0:
        delta = after.text
        correlation_lost = before.text != after.text
    else:
        delta = after.text[overlap:]
        correlation_lost = False
    if not overlap and anchor:
        normalized_anchor = " ".join(anchor.split())
        offset = 0
        anchor_offset = None
        for line in after.text.splitlines(keepends=True):
            if " ".join(line.split()) == normalized_anchor:
                anchor_offset = offset
            offset += len(line)
        if anchor_offset is not None:
            delta = after.text[anchor_offset:]
            anchored = True
    bounded = limit_output(delta, max_lines=max_lines, max_chars=max_chars)
    return OutputSnapshot(
        bounded.text,
        bounded.truncated or (
            not anchored and not overlap and (correlation_lost or after.truncated)
        ),
        anchored=anchored,
    )


class RequestLifecycleMachine(Generic[TargetT]):
    """Submit, observe a fresh turn, and capture its bounded new output."""

    def __init__(
        self,
        transport: RequestTransport[TargetT],
        store: RequestStateStore,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], float] = time.time,
    ) -> None:
        self.transport = transport
        self.store = store
        self.monotonic = monotonic
        self.sleep = sleep
        self.now = now

    def run(
        self,
        *,
        request_id: str,
        prompt: str,
        timeout_seconds: float,
        poll_interval_seconds: float,
        max_output_lines: int,
        max_output_chars: int,
        metadata: Mapping[str, Any] | None = None,
        cancelled: Callable[[], bool] | None = None,
        defer_timeout_seconds: float | None = None,
        on_deferred: Callable[[Mapping[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        if timeout_seconds <= 0 or poll_interval_seconds <= 0:
            raise ValueError("Timeouts and poll intervals must be greater than zero.")
        self._validate_limits(max_output_lines, max_output_chars)
        if defer_timeout_seconds is not None and defer_timeout_seconds < 0:
            raise ValueError("The defer timeout must not be negative.")

        record = self._new_record(
            request_id, prompt, max_output_lines, max_output_chars, metadata
        )
        self.store.save(request_id, record)

        try:
            initial = self.transport.resolve()
        except Exception as error:
            return self._finish_without_target(
                record,
                state=RequestState.SUBMISSION_FAILED,
                phase="resolve_failed",
                error=str(error) or error.__class__.__name__,
                error_code=getattr(error, "code", None) or "resolve_failed",
            )

        if self.transport.recipient_is_sender(initial):
            return self._reject_self_send(record)

        if defer_timeout_seconds is not None and self.transport.is_focused(initial):
            deadline = self.monotonic() + timeout_seconds
            self._defer(record, initial, defer_timeout_seconds, on_deferred)
            return self._wait_deferred(
                record, deadline, poll_interval_seconds, cancelled
            )

        self._submit(record, initial, cancelled=cancelled)
        if record.get("terminal"):
            return record
        baseline_data = record["_baseline"]
        baseline = OutputSnapshot(
            baseline_data["text"], baseline_data["truncated"]
        )
        baseline_error = baseline_data["error"]
        initial_working = record["preexisting_working"]

        boundary_seen = not initial_working
        requested_working_seen = False
        last_target = initial
        deadline = self.monotonic() + timeout_seconds
        last_resolve_error = ""

        while self.monotonic() < deadline:
            if cancelled is not None and cancelled():
                record["cancelled"] = True
                state = (
                    RequestState.SUBMITTED_WORKING
                    if last_target.status == "working"
                    else RequestState.SUBMITTED_UNKNOWN
                )
                return self._finish(
                    record,
                    target=last_target,
                    baseline=baseline,
                    baseline_reliable=not baseline_error,
                    state=state,
                    phase="wait_cancelled",
                    error="Waiting was cancelled after prompt submission.",
                    error_code="cancelled",
                    max_lines=max_output_lines,
                    max_chars=max_output_chars,
                )
            try:
                current = self.transport.resolve()
            except Exception as error:
                last_resolve_error = str(error) or error.__class__.__name__
                self._sleep_until(deadline, poll_interval_seconds)
                continue
            if current.identity != initial.identity:
                return self._finish_recipient_changed(record)

            last_target = current
            record["recipient_status"] = current.status
            if not boundary_seen:
                if current.status == "working":
                    record["state"] = RequestState.SUBMITTED_WORKING.value
                    self.store.save(request_id, record)
                    self._sleep_until(deadline, poll_interval_seconds)
                    continue
                boundary_seen = True
                baseline, baseline_error = self._read_output(
                    current,
                    max_lines=max_output_lines,
                    max_chars=max_output_chars,
                )
                self._store_baseline(record, baseline, baseline_error)
                record.update(
                    {
                        "state": RequestState.SUBMITTED_UNKNOWN.value,
                        "phase": "waiting_for_requested_turn",
                        "wait_safe": True,
                        "response": {
                            "output": "",
                            "truncated": bool(baseline_error),
                            "correlated": False,
                        },
                    }
                )
                if baseline_error:
                    record["baseline_error"] = baseline_error
                self.store.save(request_id, record)
                self._sleep_until(deadline, poll_interval_seconds)
                continue

            if current.status == "working":
                requested_working_seen = True
                record.update(
                    {
                        "state": RequestState.SUBMITTED_WORKING.value,
                        "phase": "waiting_for_requested_settlement",
                        "requested_turn_observed": True,
                    }
                )
                self.store.save(request_id, record)
            elif requested_working_seen:
                record["recipient_status"] = current.status
                return self._finish(
                    record,
                    target=current,
                    baseline=baseline,
                    baseline_reliable=not baseline_error,
                    state=RequestState.SUBMITTED_SETTLED,
                    phase="settled",
                    error=None,
                    error_code=None,
                    max_lines=max_output_lines,
                    max_chars=max_output_chars,
                    correlated=True,
                )
            self._sleep_until(deadline, poll_interval_seconds)

        timeout_state = (
            RequestState.SUBMITTED_WORKING
            if last_target.status == "working"
            else RequestState.SUBMITTED_UNKNOWN
        )
        error = last_resolve_error or (
            "Timed out while the recipient was working."
            if timeout_state is RequestState.SUBMITTED_WORKING
            else "Timed out before a fresh requested turn could be confirmed."
        )
        record["timed_out"] = True
        return self._finish(
            record,
            target=last_target,
            baseline=baseline,
            baseline_reliable=not baseline_error,
            state=timeout_state,
            phase="wait_timed_out",
            error=error,
            error_code=(
                "prompt_acceptance_unknown"
                if record["submitted"] is None
                else "wait_timed_out"
            ),
            max_lines=max_output_lines,
            max_chars=max_output_chars,
        )

    @staticmethod
    def _validate_limits(max_lines: int, max_chars: int) -> None:
        if max_lines <= 0 or max_chars <= 0:
            raise ValueError("Output limits must be greater than zero.")

    @staticmethod
    def _new_record(
        request_id: str,
        prompt: str,
        max_lines: int,
        max_chars: int,
        metadata: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        record: dict[str, Any] = {
            "request_id": request_id,
            "state": None,
            "phase": "resolving",
            "terminal": False,
            "submitted": None,
            "submission_timed_out": False,
            "timed_out": False,
            "preexisting_working": None,
            "requested_turn_observed": False,
            "wait_safe": None,
            "recipient_status": None,
            "response": {"output": "", "truncated": False, "correlated": False},
            "error": None,
            "error_code": None,
            "_response_anchor": prompt.splitlines()[0] if prompt else "",
            "_limits": {"lines": max_lines, "chars": max_chars},
        }
        if metadata:
            record.update(metadata)
        record["_prompt"] = prompt
        return record

    def _defer(
        self,
        record: dict[str, Any],
        target: TargetT,
        timeout_seconds: float,
        on_deferred: Callable[[Mapping[str, Any]], None] | None,
    ) -> dict[str, Any]:
        since = self.now()
        deadline = since + timeout_seconds
        record.update(
            {
                "state": RequestState.DEFERRED.value,
                "phase": "deferred_recipient_focused",
                "submitted": False,
                "recipient": dict(self.transport.describe(target)),
                "recipient_status": target.status,
                "_target_identity": target.identity,
                "_defer": {
                    "since": since,
                    "deadline": deadline,
                    "timeout_ms": int(timeout_seconds * 1000),
                    "decision": None,
                },
                "defer": {
                    "reason": "recipient_focused",
                    "since": datetime.fromtimestamp(
                        since, timezone.utc
                    ).isoformat().replace("+00:00", "Z"),
                    "deadline": datetime.fromtimestamp(
                        deadline, timezone.utc
                    ).isoformat().replace("+00:00", "Z"),
                    "notified": None,
                },
            }
        )
        self.store.save(str(record["request_id"]), record)
        if on_deferred is not None:
            on_deferred(record)
        return record

    def enqueue(
        self,
        *,
        request_id: str,
        prompt: str,
        max_output_lines: int,
        max_output_chars: int,
        defer_timeout_seconds: float,
        metadata: Mapping[str, Any] | None = None,
        on_deferred: Callable[[Mapping[str, Any]], None] | None = None,
    ) -> dict[str, Any] | None:
        """Own a focused recipient's delivery, or leave immediate delivery alone."""

        self._validate_limits(max_output_lines, max_output_chars)
        if defer_timeout_seconds < 0:
            raise ValueError("The defer timeout must not be negative.")
        record = self._new_record(
            request_id, prompt, max_output_lines, max_output_chars, metadata
        )
        try:
            target = self.transport.resolve()
        except Exception as error:
            return self._finish_without_target(
                record,
                state=RequestState.SUBMISSION_FAILED,
                phase="resolve_failed",
                error=str(error) or error.__class__.__name__,
                error_code=getattr(error, "code", None) or "resolve_failed",
            )
        if self.transport.recipient_is_sender(target):
            return self._reject_self_send(record)
        if not self.transport.is_focused(target):
            return None
        return self._defer(record, target, defer_timeout_seconds, on_deferred)

    def deliver_deferred(
        self, request_id: str, *, force: bool = False,
    ) -> dict[str, Any]:
        """Recheck the occupant and deliver only after focus or an explicit push."""

        with self.store.lock(request_id):
            record = _validate_deferred_record(
                self.store, _load_request(self.store, request_id)
            )
            if not _is_deferred(record):
                return record
            try:
                target = self.transport.resolve()
            except Exception as error:
                return self._finish_without_target(
                    record,
                    state=RequestState.SUBMISSION_FAILED,
                    phase="resolve_failed",
                    error=str(error) or error.__class__.__name__,
                    error_code=getattr(error, "code", None) or "resolve_failed",
                )
            if target.identity != record.get("_target_identity"):
                return self._finish_without_target(
                    record,
                    state=RequestState.SUBMISSION_FAILED,
                    phase="recipient_changed",
                    error="The resolved recipient occupant changed during the request.",
                    error_code="recipient_changed",
                )
            push = force or record["_defer"].get("decision") == "push"
            if record["state"] == RequestState.AWAITING_DECISION.value and not push:
                return record
            if not push and self.transport.is_focused(target):
                if self.now() >= record["_defer"]["deadline"]:
                    record.update(
                        {
                            "state": RequestState.AWAITING_DECISION.value,
                            "phase": "awaiting_decision",
                        }
                    )
                    self.store.save(request_id, record)
                return record
            return self._submit(record, target)

    def cancel_deferred(
        self,
        request_id: str,
        *,
        can_cancel: Callable[[Mapping[str, Any]], bool] | None = None,
    ) -> dict[str, Any]:
        """Cancel undelivered work, optionally checking ownership under its lock."""

        with self.store.lock(request_id):
            record = _load_request(self.store, request_id)
            if can_cancel is not None and not can_cancel(record):
                return record
            if not _is_deferred(record) or record.get("submitted") or record.get("terminal"):
                return record
            record = _validate_deferred_record(self.store, record)
            if not _is_deferred(record):
                return record
            return self._finish_without_target(
                record,
                state=RequestState.SUBMISSION_FAILED,
                phase="delivery_cancelled",
                error="Deferred delivery was cancelled.",
                error_code="delivery_cancelled",
            )

    def _reject_self_send(self, record: dict[str, Any]) -> dict[str, Any]:
        return self._finish_without_target(
            record, state=RequestState.SUBMISSION_FAILED,
            phase="submission_failed", error_code="recipient_is_sender",
            error="The current sender cannot also be the recipient.",
        )

    def _wait_deferred(
        self,
        record: dict[str, Any],
        deadline: float,
        poll_interval_seconds: float,
        cancelled: Callable[[], bool] | None,
    ) -> dict[str, Any]:
        request_id = str(record["request_id"])
        while self.monotonic() < deadline:
            if cancelled is not None and cancelled():
                record = self.cancel_deferred(request_id)
                if record.get("terminal"):
                    return record
                with self.store.lock(request_id):
                    record = _load_request(self.store, request_id)
                    if not record.get("terminal"):
                        record.update(
                            cancelled=True, phase="wait_cancelled",
                            error="Waiting was cancelled after prompt submission.",
                            error_code="cancelled",
                        )
                        self.store.save(request_id, record)
                    return record
            record = self.refresh(request_id=request_id)
            if record.get("terminal"):
                return record
            self._sleep_until(deadline, poll_interval_seconds)
        with self.store.lock(request_id):
            record = _load_request(self.store, request_id)
            if _is_deferred(record) or record.get("terminal"):
                return record
            return self._finish_deferred_wait_timeout(record)

    def _finish_deferred_wait_timeout(self, record: dict[str, Any]) -> dict[str, Any]:
        request_id = str(record["request_id"])
        working = record.get("recipient_status") == "working"
        record.update(
            {
                "state": (
                    RequestState.SUBMITTED_WORKING.value
                    if working else RequestState.SUBMITTED_UNKNOWN.value
                ),
                "phase": "wait_timed_out",
                "timed_out": True,
                "error": record.get("error") or (
                    "Timed out while the recipient was working."
                    if working
                    else "Timed out before a fresh requested turn could be confirmed."
                ),
                "error_code": (
                    "prompt_acceptance_unknown"
                    if record.get("submitted") is None else "wait_timed_out"
                ),
            }
        )
        self.store.save(request_id, record)
        return record

    def _submit(
        self,
        record: dict[str, Any],
        initial: TargetT,
        *,
        cancelled: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        request_id = str(record["request_id"])
        prompt = record["_prompt"]
        max_output_lines = record["_limits"]["lines"]
        max_output_chars = record["_limits"]["chars"]
        initial_working = initial.status == "working"
        record.update(
            {
                "phase": "capturing_baseline",
                "recipient": dict(self.transport.describe(initial)),
                "recipient_status": initial.status,
                "preexisting_working": initial_working,
                "wait_safe": not initial_working,
                "_target_identity": initial.identity,
            }
        )
        self.store.save(request_id, record)
        baseline, baseline_error = self._read_output(
            initial,
            max_lines=max_output_lines,
            max_chars=max_output_chars,
        )
        self._store_baseline(record, baseline, baseline_error)

        if cancelled is not None and cancelled():
            record["cancelled"] = True
            return self._finish_without_target(
                record,
                state=RequestState.SUBMISSION_FAILED,
                phase="submission_cancelled",
                error="Request was cancelled before prompt submission.",
                error_code="cancelled",
            )

        record["phase"] = "submitting"
        # Commit uncertain submission before the side effect. A killed submitter
        # must never leave a held record that another process can submit again.
        if _is_deferred(record):
            record["_watch_until"] = self.now() + 30 * 60
        record.update(state=RequestState.SUBMITTED_UNKNOWN.value, submitted=None)
        record.pop("defer", None)
        record.pop("_defer", None)
        if baseline_error:
            record["baseline_error"] = baseline_error
        self.store.save(request_id, record)
        try:
            submission = self.transport.submit(initial, prompt)
        except subprocess.TimeoutExpired:
            submission = SubmissionResult.unknown(
                "Prompt submission timed out before acceptance could be confirmed.",
                timed_out=True,
            )
        except Exception as error:
            submission = SubmissionResult.failed(
                str(error) or error.__class__.__name__
            )

        record.update(
            {
                "submitted": submission.accepted,
                "submission_timed_out": submission.timed_out,
                "submission_error": submission.error or None,
            }
        )
        if submission.accepted is False:
            return self._finish(
                record,
                target=initial,
                baseline=baseline,
                baseline_reliable=not baseline_error,
                state=RequestState.SUBMISSION_FAILED,
                phase="submission_failed",
                error=submission.error or "Prompt submission failed.",
                error_code=submission.code or "prompt_failed",
                max_lines=max_output_lines,
                max_chars=max_output_chars,
            )
        record.update(
            {
                "state": (
                    RequestState.SUBMITTED_WORKING.value
                    if initial_working
                    else RequestState.SUBMITTED_UNKNOWN.value
                ),
                "phase": (
                    "waiting_for_preexisting_turn"
                    if initial_working
                    else "waiting_for_requested_turn"
                ),
            }
        )
        self.store.save(request_id, record)
        return record

    def refresh(self, *, request_id: str) -> dict[str, Any]:
        """Advance one stored nonterminal request using a fresh observation."""

        record = self.deliver_deferred(request_id)
        if _is_deferred(record):
            return record
        # The worker and request-status can observe the same request. Serialize
        # observations so a stale snapshot cannot erase an observed turn.
        with self.store.lock(request_id):
            record = _load_request(self.store, request_id)
            return self._refresh_record(record)

    def _refresh_record(self, record: dict[str, Any]) -> dict[str, Any]:
        request_id = str(record["request_id"])
        if record.get("terminal"):
            return record
        limits = record.get("_limits")
        if not isinstance(limits, dict):
            raise ValueError("The stored request output limits are invalid.")
        try:
            max_lines = int(limits.get("lines") or 0)
            max_chars = int(limits.get("chars") or 0)
        except (TypeError, ValueError) as error:
            raise ValueError(
                "The stored request output limits are invalid."
            ) from error
        if max_lines <= 0 or max_chars <= 0:
            raise ValueError("The stored request output limits are invalid.")
        baseline_data = record.get("_baseline")
        if not isinstance(baseline_data, dict):
            raise ValueError("The stored request baseline is invalid.")
        baseline = OutputSnapshot(
            str(baseline_data.get("text") or ""),
            bool(baseline_data.get("truncated")),
        )
        baseline_error = str(baseline_data.get("error") or "")

        try:
            current = self.transport.resolve()
        except Exception as error:
            record.update(
                {
                    "phase": "refresh_unavailable",
                    "error": str(error) or error.__class__.__name__,
                    "error_code": getattr(error, "code", None) or "resolve_failed",
                }
            )
            self.store.save(request_id, record)
            return record
        expected_identity = record.get("_target_identity")
        if (
            not isinstance(expected_identity, str)
            or current.identity != expected_identity
        ):
            return self._finish_recipient_changed(record)

        record["recipient_status"] = current.status
        if not record.get("wait_safe"):
            if current.status == "working":
                record.update(
                    {
                        "state": RequestState.SUBMITTED_WORKING.value,
                        "phase": "waiting_for_preexisting_turn",
                    }
                )
                self._capture_live_response(
                    record,
                    current,
                    baseline,
                    baseline_reliable=not baseline_error,
                    max_lines=max_lines,
                    max_chars=max_chars,
                )
                self.store.save(request_id, record)
                return record
            baseline, baseline_error = self._read_output(
                current,
                max_lines=max_lines,
                max_chars=max_chars,
            )
            self._store_baseline(record, baseline, baseline_error)
            record.update(
                {
                    "state": RequestState.SUBMITTED_UNKNOWN.value,
                    "phase": "waiting_for_requested_turn",
                    "wait_safe": True,
                    "error": None,
                    "error_code": None,
                    "timed_out": False,
                    "response": {
                        "output": "",
                        "truncated": bool(baseline_error),
                        "correlated": False,
                    },
                }
            )
            self.store.save(request_id, record)
            return record

        if current.status == "working":
            record.update(
                {
                    "state": RequestState.SUBMITTED_WORKING.value,
                    "phase": "waiting_for_requested_settlement",
                    "requested_turn_observed": True,
                    "error": None,
                    "error_code": None,
                    "timed_out": False,
                }
            )
            self._capture_live_response(
                record,
                current,
                baseline,
                baseline_reliable=not baseline_error,
                max_lines=max_lines,
                max_chars=max_chars,
            )
            self.store.save(request_id, record)
            return record
        if record.get("requested_turn_observed"):
            record["timed_out"] = False
            return self._finish(
                record,
                target=current,
                baseline=baseline,
                baseline_reliable=not baseline_error,
                state=RequestState.SUBMITTED_SETTLED,
                phase="settled",
                error=None,
                error_code=None,
                max_lines=max_lines,
                max_chars=max_chars,
                correlated=True,
            )

        record.update(
            {
                "state": RequestState.SUBMITTED_UNKNOWN.value,
                "phase": "waiting_for_requested_turn",
                "error": None,
                "error_code": None,
            }
        )
        self._capture_live_response(
            record,
            current,
            baseline,
            baseline_reliable=not baseline_error,
            max_lines=max_lines,
            max_chars=max_chars,
        )
        self.store.save(request_id, record)
        return record

    def _sleep_until(self, deadline: float, interval: float) -> None:
        remaining = deadline - self.monotonic()
        if remaining > 0:
            self.sleep(min(interval, remaining))

    def _read_output(
        self,
        target: TargetT,
        *,
        max_lines: int,
        max_chars: int,
    ) -> tuple[OutputSnapshot, str]:
        try:
            return (
                self.transport.read_output(
                    target,
                    max_lines=max_lines,
                    max_chars=max_chars,
                ),
                "",
            )
        except Exception as error:
            return OutputSnapshot("", truncated=True), (
                str(error) or error.__class__.__name__
            )

    @staticmethod
    def _store_baseline(
        record: dict[str, Any],
        baseline: OutputSnapshot,
        error: str,
    ) -> None:
        record["_baseline"] = {
            "text": baseline.text,
            "truncated": baseline.truncated,
            "error": error or None,
        }

    def _capture_live_response(
        self,
        record: dict[str, Any],
        target: TargetT,
        baseline: OutputSnapshot,
        *,
        baseline_reliable: bool,
        max_lines: int,
        max_chars: int,
    ) -> None:
        final_output, read_error = self._read_output(
            target,
            max_lines=max_lines,
            max_chars=max_chars,
        )
        captured = capture_new_output(
            baseline,
            final_output,
            max_lines=max_lines,
            max_chars=max_chars,
            anchor=str(record.get("_response_anchor") or ""),
        )
        response: dict[str, Any] = {
            "output": captured.text,
            "truncated": captured.truncated or (not baseline_reliable and not captured.anchored),
            "correlated": False,
        }
        if captured.anchored:
            response["anchored"] = True
        if read_error:
            response["error"] = read_error
        record["response"] = response

    def _finish_recipient_changed(self, record: dict[str, Any]) -> dict[str, Any]:
        # The old address may now point at another agent. Retain only output
        # already captured while the requested occupant could still be verified.
        record.update(
            {
                "state": RequestState.SUBMITTED_UNKNOWN.value,
                "phase": "recipient_changed",
                "terminal": True,
                "error": "The resolved recipient occupant changed during the request.",
                "error_code": "recipient_changed",
                "response": {
                    **record["response"],
                    "truncated": True,
                    "correlated": False,
                },
            }
        )
        self.store.save(str(record["request_id"]), record)
        return record

    def _finish_without_target(
        self,
        record: dict[str, Any],
        *,
        state: RequestState,
        phase: str,
        error: str,
        error_code: str,
    ) -> dict[str, Any]:
        record.update(
            {
                "state": state.value,
                "phase": phase,
                "terminal": True,
                "submitted": False,
                "error": error,
                "error_code": error_code,
            }
        )
        self.store.save(str(record["request_id"]), record)
        return record

    def _finish(
        self,
        record: dict[str, Any],
        *,
        target: TargetT,
        baseline: OutputSnapshot,
        baseline_reliable: bool,
        state: RequestState,
        phase: str,
        error: str | None,
        error_code: str | None,
        max_lines: int,
        max_chars: int,
        correlated: bool = False,
        terminal: bool | None = None,
    ) -> dict[str, Any]:
        final_output, read_error = self._read_output(
            target,
            max_lines=max_lines,
            max_chars=max_chars,
        )
        captured = capture_new_output(
            baseline,
            final_output,
            max_lines=max_lines,
            max_chars=max_chars,
            anchor=str(record.get("_response_anchor") or ""),
        )
        response = {
            "output": captured.text,
            "truncated": captured.truncated or (not baseline_reliable and not captured.anchored),
            "correlated": correlated and baseline_reliable and not read_error,
        }
        if captured.anchored:
            response["anchored"] = True
        if read_error:
            response["error"] = read_error
        record.update(
            {
                "state": state.value,
                "phase": phase,
                "terminal": (
                    terminal
                    if terminal is not None
                    else state
                    in {
                        RequestState.SUBMISSION_FAILED,
                        RequestState.SUBMITTED_SETTLED,
                    }
                ),
                "recipient_status": target.status,
                "response": response,
                "error": error,
                "error_code": error_code,
            }
        )
        self.store.save(str(record["request_id"]), record)
        return record
