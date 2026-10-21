"""Deliver focus-held requests and expose the sender's held-message queue."""

from __future__ import annotations

from ham.paths import PLUGIN_ROOT

import argparse
from contextlib import contextmanager
from dataclasses import dataclass, fields
from datetime import datetime
import fcntl
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Any, Callable, Iterator, Mapping, Sequence

from ham.agents.directory import (
    AgentRecord,
    decode_json_object,
    herdr_executable,
    local_host_name,
    run_bounded_command,
    ssh_command,
    ssh_config_path,
)
from ham.agents.request_lifecycle import (
    RequestLifecycleMachine,
    open_request_store,
    pending_deferred,
    request_state_directory,
    set_deferred_decision,
    validate_deferred_record,
)


TransportFactory = Callable[[Mapping[str, Any], Mapping[str, str] | None], Any]
CommandRunner = Callable[..., subprocess.CompletedProcess[str]]


@dataclass(frozen=True)
class HeldMessage:
    request_id: str
    state: str
    sender: str
    recipient: str
    since: str
    deadline: str
    preview: str


def _transport(record: Mapping[str, Any], environment: Mapping[str, str] | None) -> Any:
    from ham.cli import transport_for_record

    return transport_for_record(record, environment)


@contextmanager
def _worker_lock(environment: Mapping[str, str] | None) -> Iterator[bool]:
    directory = request_state_directory(environment)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(directory / "delivery-worker.lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
        else:
            yield True
    finally:
        os.close(descriptor)


def ensure_worker(
    environment: Mapping[str, str] | None = None,
    *,
    popen_factory: Callable[..., Any] | None = None,
) -> bool:
    """Start a detached worker unless another worker already owns the queue."""

    with _worker_lock(environment) as acquired:
        if not acquired:
            return False
    values = None if environment is None else {**os.environ, **environment}
    (popen_factory or subprocess.Popen)(
        [sys.executable, "-m", "ham", "delivery", "worker"],
        cwd=os.fspath(PLUGIN_ROOT),
        env=values,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        close_fds=True,
    )
    return True


def _address(value: Any, fallback: Mapping[str, Any] | None = None) -> str:
    if isinstance(value, str):
        return value
    metadata = value if isinstance(value, Mapping) else fallback or {}
    address = metadata.get("address")
    if address:
        return str(address)
    host = metadata.get("host") or "local"
    name = metadata.get("name") or metadata.get("label") or metadata.get("pane_id") or ""
    return f"{host}/{name}"


def _defer(record: Mapping[str, Any], *, private: bool = False) -> Mapping[str, Any]:
    value = record.get("_defer" if private else "defer")
    return value if isinstance(value, Mapping) else {}


def _remote_host(record: Mapping[str, Any]) -> str | None:
    recipient = record.get("recipient")
    context = record.get("_context")
    context = context if isinstance(context, Mapping) else {}
    if isinstance(recipient, Mapping):
        if recipient.get("local") is True:
            return None
        host = recipient.get("host") or context.get("host")
    else:
        host = context.get("host")
    return str(host) if host and host != "local" else None


def _held_minutes(record: Mapping[str, Any], now: float) -> int:
    private = _defer(record, private=True)
    since = private.get("since")
    if not isinstance(since, (int, float)):
        public = _defer(record)
        try:
            since = datetime.fromisoformat(str(public.get("since")).replace("Z", "+00:00")).timestamp()
        except (ValueError, TypeError, OverflowError):
            since = now
    return max(0, int((now - since) // 60))


def notify_deferred(
    record: Mapping[str, Any],
    environment: Mapping[str, str] | None = None,
    *,
    command_runner: CommandRunner | None = None,
    now: Callable[[], float] = time.time,
) -> dict[str, str]:
    """Notify both hosts when a held request needs the user's decision."""

    runner = command_runner or run_bounded_command
    sender = _address(record.get("sender"))
    recipient = _address(record.get("recipient"), record.get("_context"))
    body = (
        f"{sender} -> {recipient}: held {_held_minutes(record, now())} min because "
        'that pane is focused. Run "HAM: Held Messages" to push or cancel.'
    )
    title = "HAM: Held Messages"
    commands = {"local": [herdr_executable(environment), "notification", "show", title, "--body", body]}
    remote_host = _remote_host(record)
    if remote_host:
        remote_body = body.removesuffix(" to push or cancel.")
        remote_body += f" on {local_host_name()} to push or cancel."
        commands["remote"] = ssh_command(
            remote_host,
            ["notification", "show", title, "--body", remote_body],
            config_path=ssh_config_path(environment),
        )
    notified: dict[str, str] = {}
    for host, command in commands.items():
        try:
            result = runner(command)
            payload = decode_json_object(result.stdout)
            response = payload.get("result")
            reason = response.get("reason") if isinstance(response, Mapping) else None
            notified[host] = str(reason) if reason else (
                "invalid_response" if result.returncode == 0 else "unavailable"
            )
        except Exception:
            notified[host] = "unavailable"
    return notified


def _pending_requests(store: Any, now: float) -> list[dict[str, Any]]:
    records = []
    for request_id in store.list_request_ids():
        try:
            record = store.load(request_id)
        except (OSError, ValueError, UnicodeError):
            continue
        if record is None or record.get("terminal"):
            continue
        watching = record.get("_watch_until")
        if record.get("state") in {"deferred", "awaiting_decision"} or (
            isinstance(watching, (int, float)) and watching > now
        ):
            records.append(record)
    return records


def worker_tick(
    store: Any,
    environment: Mapping[str, str] | None = None,
    *,
    transport_factory: TransportFactory | None = None,
    command_runner: CommandRunner | None = None,
    now: Callable[[], float] = time.time,
) -> float | None:
    """Process each queued request once, returning the next tick's delay."""

    factory = transport_factory or _transport
    for record in _pending_requests(store, now()):
        try:
            request_id = record["request_id"]
            record = validate_deferred_record(store, request_id)
            if record.get("terminal"):
                continue
            force = _defer(record, private=True).get("decision") == "push"
            machine = None
            if force or record.get("state") == "deferred":
                machine = RequestLifecycleMachine(
                    factory(record, environment), store, now=now
                )
                record = machine.deliver_deferred(request_id, force=force)
            if record.get("state") == "awaiting_decision":
                # A push can race this tick. Keep the lock through notification
                # so that we never notify for a request being submitted.
                with store.lock(request_id):
                    record = store.load(request_id)
                    if (
                        record is not None
                        and record.get("state") == "awaiting_decision"
                        and _defer(record).get("notified") is None
                    ):
                        notified = notify_deferred(
                            record, environment, command_runner=command_runner, now=now
                        )
                        record["defer"]["notified"] = notified
                        store.save(request_id, record)
            elif (
                not record.get("terminal")
                and isinstance(record.get("_watch_until"), (int, float))
                and record["_watch_until"] > now()
            ):
                if machine is None:
                    machine = RequestLifecycleMachine(
                        factory(record, environment), store, now=now
                    )
                machine.refresh(request_id=request_id)
        except Exception:
            # An unavailable host must not stop other deliveries.
            continue
    remaining = _pending_requests(store, now())
    if not remaining:
        return None
    if any(record.get("state") not in {"deferred", "awaiting_decision"} for record in remaining):
        return 0.5
    return 3.0 if any(_remote_host(record) for record in remaining) else 1.0


def run_worker(
    environment: Mapping[str, str] | None = None,
    *,
    transport_factory: TransportFactory | None = None,
    command_runner: CommandRunner | None = None,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.time,
) -> None:
    """Own the queue lock while held or submitted-and-watched requests remain."""

    while True:
        with _worker_lock(environment) as acquired:
            if not acquired:
                return
            store = open_request_store(environment)
            while True:
                delay = worker_tick(
                    store, environment, transport_factory=transport_factory,
                    command_runner=command_runner, now=now,
                )
                if delay is None:
                    break
                sleep(delay)
        # A sender may have found our lock busy just before the idle check.
        # Recheck after releasing it so that such an enqueue cannot be stranded.
        if not _pending_requests(store, now()):
            return


def _preview(prompt: str) -> str:
    lines = prompt.splitlines()
    if lines and lines[0].startswith("Message from "):
        lines = lines[1:]
    return next((line.strip()[:120] for line in lines if line.strip()), "")


def held_messages(environment: Mapping[str, str] | None = None) -> list[HeldMessage]:
    """Return deferred and awaiting requests without exposing full prompts."""

    messages = []
    for record in pending_deferred(open_request_store(environment)):
        defer = _defer(record)
        messages.append(HeldMessage(
            request_id=record["request_id"],
            state=record["state"],
            sender=_address(record.get("sender")),
            recipient=_address(record.get("recipient"), record.get("_context")),
            since=str(defer.get("since") or ""),
            deadline=str(defer.get("deadline") or ""),
            preview=_preview(str(record.get("_prompt") or "")),
        ))
    return messages


def decide(
    request_id: str,
    decision: str,
    environment: Mapping[str, str] | None = None,
    *,
    transport_factory: TransportFactory | None = None,
) -> dict[str, Any]:
    """Cancel a held request or queue an explicit forced delivery."""

    if decision not in {"push", "cancel"}:
        raise ValueError("decision must be push or cancel")
    store = open_request_store(environment)
    if decision == "cancel":
        # Cancellation only changes the stored state; it must work offline and
        # even when the recipient context can no longer construct a transport.
        return RequestLifecycleMachine(None, store).cancel_deferred(request_id)
    record = set_deferred_decision(store, request_id, "push")
    if record.get("state") in {"deferred", "awaiting_decision"}:
        ensure_worker(environment)
    return store.load(request_id) or record


def request_cancel_command(
    *,
    request_id: str,
    sender: AgentRecord,
    environment: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Cancel only the verified caller's held request, without a recipient probe."""

    from ham.ledger.store import LedgerError

    sender_fields = {field.name for field in fields(AgentRecord)}
    owned = False
    applicable = False

    def can_cancel(record: Mapping[str, Any]) -> bool:
        nonlocal owned, applicable
        stored_sender = record.get("sender")
        if isinstance(stored_sender, Mapping):
            try:
                owner = AgentRecord(**{
                    key: value for key, value in stored_sender.items()
                    if key in sender_fields
                })
                owned = owner.identity == sender.identity
            except TypeError:
                owned = False
        applicable = (
            record.get("state") in {"deferred", "awaiting_decision"}
            and not record.get("submitted") and not record.get("terminal")
        )
        return owned

    try:
        record = RequestLifecycleMachine(None, open_request_store(environment)).cancel_deferred(
            request_id, can_cancel=can_cancel,
        )
    except KeyError:
        raise LedgerError(
            "request_not_found",
            f"No stored request state was found for {request_id!r}.",
        ) from None
    except ValueError as error:
        raise LedgerError("invalid_request_id", str(error)) from error
    except OSError as error:
        raise LedgerError("request_state_unavailable", str(error)) from error

    cancelled = owned and applicable and record.get("phase") == "delivery_cancelled"
    payload = {
        "request_id": record["request_id"],
        "state": record["state"],
        "phase": record.get("phase"),
        "cancelled": cancelled,
    }
    if not owned:
        payload["error_code"] = "not_sender"
    elif cancelled:
        payload["error_code"] = "delivery_cancelled"
    elif record.get("submitted"):
        payload["error_code"] = "already_submitted"
    elif not applicable:
        payload["error_code"] = "already_terminal"
    else:
        payload["error_code"] = record.get("error_code") or "request_not_held"
    return payload


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("worker", "reconcile"))
    arguments = parser.parse_args(argv)
    if arguments.command == "worker":
        run_worker()
    elif _pending_requests(open_request_store(), time.time()):
        ensure_worker()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
