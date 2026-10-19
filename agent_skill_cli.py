"""Non-interactive label and verified-route interface for agent messaging."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict
from typing import Any

import agent_delivery
import ledger_cli
from collaboration_policy import CollaborationPolicy
from ledger_settings import RepoSettings
from ledger_status import AgentRegistry, repository_identity
from ledger_store import LedgerError, LedgerStore
import request_lifecycle
from agent_batch_dispatch import (
    CANCELLED,
    DEFAULT_BATCH_WORKERS,
    DEFERRED,
    FAILED,
    SUBMITTED,
    SUCCEEDED,
    TIMED_OUT,
    BatchDispatchError,
    BatchRequest,
    DispatchOutcome,
    dispatch_batch,
    parse_batch_json,
)
from agent_directory import (
    REMOTE_DISCOVERY_TIMEOUT_SECONDS,
    AgentRecord,
    herdr_executable,
    probe_local_agents,
    query_remote_agents,
    run_bounded_command,
    ssh_command,
    ssh_config_path,
    ssh_hosts,
    submission_failure_is_uncertain,
)
from agent_route import (
    AgentRouteError,
    ROUTE_TOKEN_VERSION,
    RouteResolution,
    encode_agent_route,
    resolve_agent_route,
    route_host,
)
from agent_output import (
    InvalidOutputCursor,
    agent_address,
    clean_agent_text,
    compact_batch_item,
    compact_read_payload,
    compact_request_payload,
    compact_status_payload,
    compact_agent_payload,
    compact_output,
    output_stream_id,
)
from agent_spawn import SpawnError, add_spawn_parser, spawn_command
from request_lifecycle import (
    JsonRequestStateStore,
    OutputSnapshot,
    RequestLifecycleMachine,
    RequestStateStore,
    RequestTransport,
    SubmissionResult,
    limit_output,
)

DEFAULT_WAIT_TIMEOUT_MS = 120_000
DEFAULT_DEFER_TIMEOUT_MS = 180_000
DEFAULT_READ_LINES = 120
DEFAULT_REQUEST_OUTPUT_CHARS = 12_000
DEFAULT_REQUEST_POLL_INTERVAL_SECONDS = 0.5
DEFAULT_READ_MAX_BYTES = 64 * 1024


class SkillCommandError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _write_json(payload: Mapping[str, Any], *, compact: bool = False) -> None:
    json.dump(
        payload,
        sys.stdout,
        ensure_ascii=False,
        separators=(",", ":") if compact else None,
    )
    sys.stdout.write("\n")


def _record_payload(agent: AgentRecord) -> dict[str, Any]:
    payload = asdict(agent)
    payload["label"] = agent.name
    payload["qualified_name"] = agent.qualified_name
    payload["address"] = agent_address(agent)
    return payload


def _is_local_host(host: str) -> bool:
    return host == "local"


def discover_agents(
    host: str,
    environment: Mapping[str, str] | None = None,
) -> list[AgentRecord]:
    if _is_local_host(host):
        result = probe_local_agents(environment)
        if not result.success:
            raise SkillCommandError(
                "host_unavailable",
                result.error or "The local Herdr server is unavailable.",
            )
        return list(result.agents)

    configured_hosts = ssh_hosts(environment)
    if host not in configured_hosts:
        raise SkillCommandError(
            "host_not_configured",
            f"SSH host {host!r} is not a concrete alias in the configured SSH config.",
        )
    result = query_remote_agents(host, config_path=ssh_config_path(environment))
    if not result.success:
        raise SkillCommandError(
            "host_unavailable",
            result.error or f"SSH host {host!r} is unavailable.",
        )
    return list(result.agents)


def resolve_labeled_agent(
    host: str,
    label: str,
    environment: Mapping[str, str] | None = None,
) -> AgentRecord:
    matches = [
        agent
        for agent in discover_agents(host, environment)
        if agent.name == label
    ]
    if not matches:
        raise SkillCommandError(
            "agent_not_found",
            f"No current agent with label {label!r} was found on host {host!r}.",
        )
    if len(matches) > 1:
        raise SkillCommandError(
            "agent_ambiguous",
            f"More than one current agent uses label {label!r} on host {host!r}.",
        )
    return matches[0]


def resolve_routed_agent(
    route: str,
    environment: Mapping[str, str] | None = None,
) -> AgentRecord:
    """Resolve or safely refresh the occupant selected by Agent Messenger."""

    return resolve_or_refresh_routed_agent(route, environment).agent


def resolve_or_refresh_routed_agent(
    route: str,
    environment: Mapping[str, str] | None = None,
) -> RouteResolution:
    try:
        host = route_host(route)
        return resolve_agent_route(route, discover_agents(host, environment))
    except AgentRouteError as error:
        raise SkillCommandError(error.code, error.message) from None


def parse_agent_address(address: str, host: str = "local") -> tuple[str, str]:
    """Split a list-provided address without guessing a host or label."""

    parts = address.split("/")
    if len(parts) != 2 or not all(parts) or any(
        character.isspace() for character in address
    ):
        raise SkillCommandError(
            "invalid_address", "An address must have the form HOST/LABEL."
        )
    address_host, label = parts
    if host != "local" and host != address_host:
        raise SkillCommandError(
            "conflicting_address", "--host disagrees with the address host."
        )
    return address_host, label


def resolve_recipient(
    *,
    host: str,
    label: str | None,
    route: str | None,
    environment: Mapping[str, str] | None = None,
) -> AgentRecord:
    return resolve_or_refresh_recipient(
        host=host,
        label=label,
        route=route,
        environment=environment,
    ).agent


def resolve_or_refresh_recipient(
    *,
    host: str,
    label: str | None,
    route: str | None,
    environment: Mapping[str, str] | None = None,
) -> RouteResolution:
    if route:
        if label:
            raise SkillCommandError(
                "conflicting_address",
                "Use either an agent route token or a host/label address, not both.",
            )
        return resolve_or_refresh_routed_agent(route, environment)
    if not label:
        raise SkillCommandError(
            "missing_address",
            "An agent route token or label is required.",
        )
    recipient = resolve_labeled_agent(host, label, environment)
    return RouteResolution(
        recipient,
        route_refreshed=False,
        route=encode_agent_route(recipient),
    )


def current_sender(
    environment: Mapping[str, str] | None = None,
) -> AgentRecord:
    values = os.environ if environment is None else environment
    resolved_pane_id = values.get("HERDR_PANE_ID", "")
    if not resolved_pane_id:
        raise SkillCommandError(
            "sender_unavailable",
            "HERDR_PANE_ID is unavailable. Run this command from the current "
            "agent's Herdr pane.",
        )

    probe = probe_local_agents(values)
    if not probe.success:
        detail = probe.error or "The local Herdr server is unavailable."
        raise SkillCommandError(
            "sender_probe_failed",
            "Could not verify the current sender before prompt submission: "
            f"{detail}. No prompt was submitted.",
        )

    sender = next(
        (agent for agent in probe.agents if agent.pane_id == resolved_pane_id),
        None,
    )
    if sender is None:
        raise SkillCommandError(
            "sender_unavailable",
            f"Herdr pane {resolved_pane_id!r} does not contain a current agent. "
            "No prompt was submitted.",
        )
    return sender


def _legacy_policy(
    sender: AgentRecord, environment: Mapping[str, str] | None,
) -> CollaborationPolicy | None:
    """Check local opt-in first; ordinary off traffic needs no peer setup."""
    store = LedgerStore(environment=environment)
    repo_id = repository_identity(sender.cwd, store.host)["repo_id"]
    if RepoSettings(store).show(repo_id)["mode"] == "off":
        return None
    registry = AgentRegistry(store)
    snapshot, _ = registry.refresh(environment)
    current = registry.resolve_context(environment, snapshot)
    if (current["terminal_id"] != sender.terminal_id
            or current["session_ref"] != (sender.session_id or "unreported")
            or current["client"] != sender.agent_kind or current["repo_id"] != repo_id):
        raise LedgerError("policy_unverifiable", "The current coordination sender changed; no prompts were submitted.")
    return CollaborationPolicy(store, current, environment=environment)


def _preflight_resolver(
    resolver: Callable[[], AgentRecord | RouteResolution], policy: CollaborationPolicy | None,
) -> Callable[[], AgentRecord | RouteResolution]:
    if policy is None or not policy.active:
        return resolver
    # Resolve before RequestLifecycleMachine can save even its initial record.
    initial = resolver()
    policy.check(initial.agent if isinstance(initial, RouteResolution) else initial)
    first = True

    def resolve() -> AgentRecord | RouteResolution:
        nonlocal first
        if first:
            first = False
            return initial
        current = resolver()
        policy.check(current.agent if isinstance(current, RouteResolution) else current)
        return current

    return resolve


def _agent_command(
    agent: AgentRecord,
    arguments: Sequence[str],
    environment: Mapping[str, str] | None = None,
) -> list[str]:
    if agent.local:
        return [herdr_executable(environment), *arguments]
    return ssh_command(
        agent.host,
        arguments,
        config_path=ssh_config_path(environment),
    )


def _request_state_store(
    environment: Mapping[str, str] | None = None,
) -> JsonRequestStateStore:
    return request_lifecycle.open_request_store(environment)


class AgentRequestTransport(RequestTransport[AgentRecord]):
    """Route-aware adapter for the transport-independent request state machine."""

    def __init__(
        self,
        *,
        resolver: Callable[[], AgentRecord | RouteResolution],
        sender_identity: str,
        environment: Mapping[str, str] | None,
        command_runner: Callable[..., subprocess.CompletedProcess[str]],
        cancel_event: threading.Event | None = None,
    ) -> None:
        self._resolver = resolver
        self._sender_identity = sender_identity
        self._environment = environment
        self._command_runner = command_runner
        self._cancel_event = cancel_event
        self._resolution: RouteResolution | None = None

    def resolve(self) -> AgentRecord:
        resolved = self._resolver()
        if isinstance(resolved, RouteResolution):
            self._resolution = resolved
        else:
            self._resolution = RouteResolution(
                resolved,
                route_refreshed=False,
                route=encode_agent_route(resolved),
            )
        return self._resolution.agent

    def describe(self, target: AgentRecord) -> Mapping[str, Any]:
        payload = compact_agent_payload(target)
        if self._resolution is not None:
            payload.update(
                {
                    "route_refreshed": self._resolution.route_refreshed,
                    "route": self._resolution.route,
                }
            )
        return payload

    @property
    def resolution(self) -> RouteResolution | None:
        return self._resolution

    def is_focused(self, target: AgentRecord) -> bool:
        return target.focused

    def recipient_is_sender(self, target: AgentRecord) -> bool:
        return target.identity == self._sender_identity

    def submit(self, target: AgentRecord, prompt: str) -> SubmissionResult:
        if self.recipient_is_sender(target):
            return SubmissionResult.failed(
                "The current sender cannot also be the recipient.",
                code="recipient_is_sender",
            )
        command = _agent_command(
            target,
            ["agent", "prompt", target.target, prompt],
            self._environment,
        )
        try:
            options: dict[str, Any] = {
                "timeout": REMOTE_DISCOVERY_TIMEOUT_SECONDS,
            }
            if self._cancel_event is not None:
                options["cancel_event"] = self._cancel_event
            result = self._command_runner(command, **options)
        except subprocess.TimeoutExpired:
            return SubmissionResult.unknown(
                "Prompt submission timed out before acceptance could be confirmed.",
                timed_out=True,
            )
        if result.returncode == 0:
            return SubmissionResult.submitted()
        detail = result.stderr.strip() or result.stdout.strip() or "prompt failed"
        if submission_failure_is_uncertain(result, remote=not target.local):
            timed_out = "timed out after" in result.stderr
            return SubmissionResult.unknown(
                (
                    "Prompt submission timed out before acceptance could be confirmed."
                    if timed_out
                    else "Prompt submission was interrupted after it may have run."
                ),
                timed_out=timed_out,
            )
        return SubmissionResult.failed(detail)

    def read_output(
        self,
        target: AgentRecord,
        *,
        max_lines: int,
        max_chars: int,
    ) -> OutputSnapshot:
        result = self._command_runner(
            _agent_command(
                target,
                [
                    "agent",
                    "read",
                    target.target,
                    "--source",
                    "recent-unwrapped",
                    "--lines",
                    str(max_lines + 1),
                    "--format",
                    "text",
                ],
                self._environment,
            ),
            timeout=REMOTE_DISCOVERY_TIMEOUT_SECONDS * 2,
        )
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip() or "read failed"
            raise RuntimeError(detail)
        return limit_output(
            result.stdout,
            max_lines=max_lines,
            max_chars=max_chars,
        )


def list_command(
    host: str,
    environment: Mapping[str, str] | None = None,
    *,
    verbose: bool = False,
) -> dict[str, Any]:
    agents = [agent for agent in discover_agents(host, environment) if agent.name]
    return {
        "host": host,
        "agents": [
            _record_payload(agent) if verbose else compact_agent_payload(agent)
            for agent in agents
        ],
    }


def status_command(
    *,
    host: str,
    label: str | None = None,
    route: str | None = None,
    verbose: bool = False,
    environment: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    resolution = resolve_or_refresh_recipient(
        host=host,
        label=label,
        route=route,
        environment=environment,
    )
    recipient = resolution.agent
    payload = {
        "agent": _record_payload(recipient),
        "route_refreshed": resolution.route_refreshed,
        "route": resolution.route,
    }
    return payload if verbose else compact_status_payload(payload)


def send_command(
    *,
    host: str,
    label: str | None = None,
    route: str | None = None,
    message: str,
    wait: bool,
    timeout_ms: int,
    defer_timeout_ms: int = DEFAULT_DEFER_TIMEOUT_MS,
    verbose: bool = False,
    environment: Mapping[str, str] | None = None,
    cancel_event: threading.Event | None = None,
    on_report: Callable[[Callable[[], None]], None] | None = None,
    _policy: CollaborationPolicy | None = None,
) -> dict[str, Any]:
    if not message.strip():
        raise SkillCommandError("empty_message", "The message must not be empty.")
    if timeout_ms <= 0:
        raise SkillCommandError(
            "invalid_timeout",
            "--timeout must be greater than zero.",
        )
    _validate_defer_timeout(defer_timeout_ms)

    sender = current_sender(environment)
    policy = _policy or _legacy_policy(sender, environment)
    recipient_resolver = _preflight_resolver(
        lambda: resolve_or_refresh_recipient(
            host=host, label=label, route=route, environment=environment,
        ), policy,
    )
    prompt = f"Message from {sender.qualified_name}:\n\n{message.strip()}"
    transport = AgentRequestTransport(
        resolver=recipient_resolver,
        sender_identity=sender.identity,
        environment=environment,
        command_runner=run_bounded_command,
        cancel_event=cancel_event,
    )
    store = _request_state_store(environment)
    try:
        queued = RequestLifecycleMachine(transport, store).enqueue(
            request_id=uuid.uuid4().hex,
            prompt=prompt,
            max_output_lines=DEFAULT_READ_LINES,
            max_output_chars=DEFAULT_REQUEST_OUTPUT_CHARS,
            defer_timeout_seconds=defer_timeout_ms / 1000,
            metadata=_request_metadata(sender, host=host, label=label, route=route),
            on_deferred=lambda record: agent_delivery.ensure_worker(environment),
        )
        if queued is not None:
            if queued.get("error_code") == "recipient_is_sender":
                raise SkillCommandError(
                    "recipient_is_sender", "The current sender cannot also be the recipient."
                )
            _record_request_route(queued, transport, store)
            if queued.get("state") in {"deferred", "awaiting_decision"}:
                agent_delivery.ensure_worker(environment)
            return _report_request(
                queued, store, verbose=verbose, on_report=on_report
            )
    except (OSError, ValueError) as error:
        raise SkillCommandError("request_state_unavailable", str(error)) from error
    resolution = transport.resolution
    assert resolution is not None  # enqueue resolved the immediate recipient.
    recipient = resolution.agent
    arguments = ["agent", "prompt", recipient.target, prompt]
    if wait:
        arguments.extend(["--wait", "--timeout", str(timeout_ms)])
    command_timeout = (
        (timeout_ms / 1000) + REMOTE_DISCOVERY_TIMEOUT_SECONDS
        if wait
        else REMOTE_DISCOVERY_TIMEOUT_SECONDS
    )
    run_options: dict[str, Any] = {"timeout": command_timeout}
    if cancel_event is not None:
        run_options["cancel_event"] = cancel_event
    result = run_bounded_command(
        _agent_command(recipient, arguments, environment),
        **run_options,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "prompt failed"
        lowered_detail = detail.lower()
        if lowered_detail == "cancelled":
            error_code = "prompt_cancelled"
        elif lowered_detail == "timeout" or "timed out after" in lowered_detail:
            error_code = "prompt_timeout"
        elif submission_failure_is_uncertain(result, remote=not recipient.local):
            error_code = "prompt_acceptance_unknown"
            detail = "Prompt submission was interrupted after it may have run."
        else:
            error_code = "prompt_failed"
        raise SkillCommandError(error_code, detail)
    payload: dict[str, Any] = {
        "sent": True,
        "recipient": agent_address(recipient),
        "waited": wait,
        "route_refreshed": resolution.route_refreshed,
        "route": resolution.route,
    }
    if wait:
        payload["wait_can_track_submitted_turn"] = recipient.status != "working"
    payload["warnings"] = (
        ["recipient_already_working"]
        if wait and recipient.status == "working"
        else []
    )
    if verbose:
        return payload
    compact = {
        "sent": True,
        "address": payload["recipient"],
        "waited": wait,
    }
    if payload["warnings"]:
        compact["warnings"] = payload["warnings"]
    return compact


def _batch_outcome_from_payload(
    payload: Mapping[str, Any],
    *,
    waited: bool,
) -> DispatchOutcome:
    """Normalize current and future single-send lifecycle payloads."""

    route = payload.get("route")
    request_id = payload.get("request_id")
    response = payload.get("response")
    response_payload = response if isinstance(response, Mapping) else {}

    def outcome(
        status: str,
        error: str = "",
        detail: str = "",
    ) -> DispatchOutcome:
        return DispatchOutcome(
            status,
            error,
            detail,
            route if isinstance(route, str) else "",
            request_id if isinstance(request_id, str) else "",
            str(response_payload.get("output") or ""),
            bool(response_payload.get("truncated")),
            bool(response_payload.get("correlated")),
            state=str(payload.get("state") or ""),
            address=str(payload.get("address") or (
                compact_request_payload(payload, response_text="")["address"]
            )),
        )

    if payload.get("state") in {"deferred", "awaiting_decision"}:
        return outcome(DEFERRED)

    if (
        "submitted" in payload
        and payload["submitted"] is None
        and payload.get("state") != "submitted_settled"
    ):
        return outcome(TIMED_OUT, "prompt_acceptance_unknown")
    if payload.get("cancelled") is True:
        if payload.get("submitted") is True:
            return outcome(SUBMITTED, detail="wait_cancelled")
        return outcome(CANCELLED, "prompt_cancelled")
    if payload.get("timed_out") is True:
        lifecycle_status = payload.get("state")
        if (
            lifecycle_status in {"submitted_working", "submitted_unknown"}
            and payload.get("submitted") is True
        ):
            return outcome(SUBMITTED)
        if payload.get("submitted") is None:
            return outcome(TIMED_OUT, "prompt_acceptance_unknown")
        return outcome(TIMED_OUT, "prompt_timeout")
    lifecycle_status = payload.get("status") or payload.get("state")
    if isinstance(lifecycle_status, str):
        normalized = lifecycle_status.lower().replace("-", "_")
        if normalized in {
            "success",
            "succeeded",
            "completed",
            "done",
            "submitted_settled",
        }:
            return outcome(SUCCEEDED)
        if normalized in {
            "submitted",
            "submitted_in_progress",
            "submitted_working",
            "submitted_unknown",
            "in_progress",
            "working",
        }:
            return outcome(SUBMITTED)
        if normalized in {"timeout", "timed_out"}:
            return outcome(TIMED_OUT, "prompt_timeout")
        if normalized in {"cancelled", "canceled"}:
            return outcome(CANCELLED, "prompt_cancelled")
        if normalized in {"failed", "failure", "blocked", "submission_failed"}:
            return outcome(
                FAILED,
                str(payload.get("error_code") or payload.get("error") or "prompt_failed"),
                str(payload.get("error") or ""),
            )
    if (
        not waited
        or payload.get("wait_can_track_submitted_turn") is False
        or payload.get("wait_trackable") is False
    ):
        return outcome(SUBMITTED)
    return outcome(SUCCEEDED)


def batch_command(
    *,
    requests_json: str,
    wait: bool,
    timeout_ms: int,
    max_workers: int,
    defer_timeout_ms: int = DEFAULT_DEFER_TIMEOUT_MS,
    verbose: bool = False,
    environment: Mapping[str, str] | None = None,
    cancel_event: threading.Event | None = None,
    on_report: Callable[[Callable[[], None]], None] | None = None,
) -> dict[str, Any]:
    """Mechanically dispatch pre-tailored route/message pairs."""

    if timeout_ms <= 0:
        raise SkillCommandError(
            "invalid_timeout",
            "--timeout must be greater than zero.",
        )
    _validate_defer_timeout(defer_timeout_ms)
    items = parse_batch_json(requests_json)
    policy: CollaborationPolicy | None = None
    policy_initialized = False
    resolution_errors: list[Exception] = []

    def resolve_request(request: BatchRequest) -> tuple[BatchRequest, str]:
        nonlocal policy, policy_initialized
        try:
            if request.address:
                host, label = parse_agent_address(request.address)
                resolution = resolve_or_refresh_recipient(
                    host=host, label=label, route=None, environment=environment,
                )
            else:
                resolution = resolve_or_refresh_routed_agent(request.route, environment)
        except Exception as error:
            resolution_errors.append(error)
            raise
        if not policy_initialized:
            try:
                # An entirely off ledger preserves ordinary per-item resolution
                # without a new sender/peer prerequisite for invalid batches.
                with LedgerStore(environment=environment).read() as connection:
                    enabled = connection.execute(
                        "SELECT 1 FROM repo_settings WHERE field='mode' AND "
                        "json_extract(value,'$') IN ('manual','auto') LIMIT 1").fetchone()
                if enabled:
                    policy = _legacy_policy(current_sender(environment), environment)
                policy_initialized = True
            except Exception as error:
                resolution_errors.append(error)
                raise
        if policy is not None:
            policy.check(resolution.agent)
        return (
            BatchRequest(
                resolution.route, request.message, address=agent_address(resolution.agent)
            ),
            resolution.agent.identity,
        )

    def before_dispatch() -> None:
        if policy is not None:
            for error in resolution_errors:
                policy.record_error(error)
            policy.before_dispatch()

    def send_one(request: BatchRequest) -> DispatchOutcome:
        try:
            if wait:
                payload = request_command(
                    host="local",
                    route=request.route,
                    message=request.message,
                    timeout_ms=timeout_ms,
                    output_lines=DEFAULT_READ_LINES,
                    output_chars=DEFAULT_REQUEST_OUTPUT_CHARS,
                    defer_timeout_ms=defer_timeout_ms,
                    verbose=True,
                    environment=environment,
                    cancel_event=cancel_event,
                    on_report=lambda acknowledge: None,
                    _policy=policy,
                )
            else:
                payload = send_command(
                    host="local",
                    route=request.route,
                    message=request.message,
                    wait=False,
                    timeout_ms=timeout_ms,
                    defer_timeout_ms=defer_timeout_ms,
                    verbose=True,
                    environment=environment,
                    cancel_event=cancel_event,
                    on_report=lambda acknowledge: None,
                    _policy=policy,
                )
        except SkillCommandError as error:
            if error.code == "prompt_timeout":
                return DispatchOutcome(TIMED_OUT, error.code, error.message)
            if error.code == "prompt_acceptance_unknown":
                return DispatchOutcome(TIMED_OUT, error.code, error.message)
            if error.code == "prompt_cancelled":
                return DispatchOutcome(CANCELLED, error.code, error.message)
            return DispatchOutcome(FAILED, error.code, error.message)
        if not verbose and isinstance(payload.get("response"), Mapping):
            record = _request_state_store(environment).load(payload["request_id"])
            prompt = record.get("_prompt") if record is not None else None
            response = dict(payload["response"])
            response["output"] = clean_agent_text(
                str(response.get("output") or ""), prompt=prompt
            )
            payload = dict(payload, response=response)
        return _batch_outcome_from_payload(payload, waited=wait)

    payload = dispatch_batch(
        items,
        send_one,
        max_workers=max_workers,
        cancel_event=cancel_event,
        resolve_request=resolve_request,
        before_dispatch=before_dispatch,
    )
    if not verbose:
        payload["results"] = [compact_batch_item(item) for item in payload["results"]]
    return payload


def read_command(
    *,
    host: str,
    label: str | None = None,
    route: str | None = None,
    lines: int,
    max_bytes: int = DEFAULT_READ_MAX_BYTES,
    cursor: str | None = None,
    verbose: bool = False,
    environment: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    if lines <= 0:
        raise SkillCommandError("invalid_lines", "--lines must be greater than zero.")
    if max_bytes <= 0:
        raise SkillCommandError(
            "invalid_max_bytes",
            "--max-bytes must be greater than zero.",
        )
    resolution = resolve_or_refresh_recipient(
        host=host,
        label=label,
        route=route,
        environment=environment,
    )
    recipient = resolution.agent
    result = run_bounded_command(
        _agent_command(
            recipient,
            [
                "agent",
                "read",
                recipient.target,
                "--source",
                "recent-unwrapped",
                "--lines",
                str(lines),
                "--format",
                "text",
            ],
            environment,
        ),
        timeout=REMOTE_DISCOVERY_TIMEOUT_SECONDS * 2,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "read failed"
        raise SkillCommandError("read_failed", detail)
    try:
        delta = compact_output(
            result.stdout,
            stream=output_stream_id(recipient.identity),
            max_bytes=max_bytes,
            cursor=cursor,
        )
    except InvalidOutputCursor as error:
        raise SkillCommandError("invalid_cursor", str(error)) from None
    payload = {
        "address": agent_address(recipient),
        "output": delta.output,
        "truncated": delta.truncated,
        "delta": delta.delta,
        "cursor_status": delta.cursor_status,
        "cursor": delta.cursor,
        "route_refreshed": resolution.route_refreshed,
        "route": resolution.route,
    }
    return payload if verbose else compact_read_payload(
        payload, cleaned_output=clean_agent_text(delta.output)
    )


def request_command(
    *,
    host: str,
    label: str | None = None,
    route: str | None = None,
    message: str,
    timeout_ms: int,
    output_lines: int,
    output_chars: int,
    defer_timeout_ms: int = DEFAULT_DEFER_TIMEOUT_MS,
    verbose: bool = False,
    environment: Mapping[str, str] | None = None,
    resolver: Callable[[], AgentRecord | RouteResolution] | None = None,
    state_store: RequestStateStore | None = None,
    command_runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
    request_id: str | None = None,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    cancel_event: threading.Event | None = None,
    on_report: Callable[[Callable[[], None]], None] | None = None,
    _policy: CollaborationPolicy | None = None,
) -> dict[str, Any]:
    """Run one prompt through submission, observation, and bounded output read."""

    if not message.strip():
        raise SkillCommandError("empty_message", "The message must not be empty.")
    if timeout_ms <= 0:
        raise SkillCommandError(
            "invalid_timeout",
            "--timeout must be greater than zero.",
        )
    _validate_defer_timeout(defer_timeout_ms)
    if output_lines <= 0:
        raise SkillCommandError(
            "invalid_output_lines",
            "--output-lines must be greater than zero.",
        )
    if output_chars <= 0:
        raise SkillCommandError(
            "invalid_output_chars",
            "--output-chars must be greater than zero.",
        )
    if cancel_event is not None and cancel_event.is_set():
        raise SkillCommandError("prompt_cancelled", "Request was cancelled.")

    sender = current_sender(environment)
    recipient_resolver = resolver or (
        lambda: resolve_or_refresh_recipient(
            host=host,
            label=label,
            route=route,
            environment=environment,
        )
    )
    policy = _policy or _legacy_policy(sender, environment)
    recipient_resolver = _preflight_resolver(recipient_resolver, policy)
    transport = AgentRequestTransport(
        resolver=recipient_resolver,
        sender_identity=sender.identity,
        environment=environment,
        command_runner=command_runner or run_bounded_command,
        cancel_event=cancel_event,
    )
    resolved_request_id = request_id or uuid.uuid4().hex
    store = state_store or _request_state_store(environment)
    machine = RequestLifecycleMachine(
        transport,
        store,
        monotonic=monotonic,
        sleep=sleep,
    )
    prompt = f"Message from {sender.qualified_name}:\n\n{message.strip()}"
    try:
        payload = machine.run(
            request_id=resolved_request_id,
            prompt=prompt,
            timeout_seconds=timeout_ms / 1000,
            poll_interval_seconds=DEFAULT_REQUEST_POLL_INTERVAL_SECONDS,
            max_output_lines=output_lines,
            max_output_chars=output_chars,
            metadata=_request_metadata(sender, host=host, label=label, route=route),
            defer_timeout_seconds=defer_timeout_ms / 1000,
            on_deferred=lambda record: agent_delivery.ensure_worker(environment),
            cancelled=cancel_event.is_set if cancel_event is not None else None,
        )
        _record_request_route(payload, transport, store)
        if payload.get("state") in {"deferred", "awaiting_decision"}:
            agent_delivery.ensure_worker(environment)
        return _report_request(
            payload, store, verbose=verbose, on_report=on_report
        )
    except (OSError, ValueError) as error:
        raise SkillCommandError(
            "request_state_unavailable",
            str(error) or "Request state storage is unavailable.",
        ) from error


def transport_for_record(
    record: Mapping[str, Any],
    environment: Mapping[str, str] | None = None,
) -> AgentRequestTransport:
    """Rebuild the recipient route without depending on a current sender pane."""
    context = record.get("_context")
    if not isinstance(context, Mapping):
        raise SkillCommandError(
            "request_state_invalid", "The stored request context is invalid."
        )
    host, label, route = (
        context.get("host"), context.get("label"), context.get("route")
    )
    if (
        not isinstance(host, str) or not host.strip()
        or (label is not None and (not isinstance(label, str) or not label.strip()))
        or (route is not None and (not isinstance(route, str) or not route.strip()))
        or bool(label) == bool(route)
    ):
        raise SkillCommandError(
            "request_state_invalid", "The stored request address is invalid."
        )
    return AgentRequestTransport(
        resolver=lambda: resolve_or_refresh_recipient(
            host=host, label=label, route=route, environment=environment,
        ),
        sender_identity="",
        environment=environment,
        command_runner=run_bounded_command,
    )


def request_status_command(
    *,
    request_id: str,
    wait_ms: int = 0,
    verbose: bool = False,
    environment: Mapping[str, str] | None = None,
    state_store: RequestStateStore | None = None,
    resolver: Callable[[], AgentRecord | RouteResolution] | None = None,
    command_runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    on_report: Callable[[Callable[[], None]], None] | None = None,
) -> dict[str, Any]:
    if wait_ms < 0:
        raise SkillCommandError("invalid_timeout", "--wait must not be negative.")
    store = state_store or _request_state_store(environment)
    try:
        payload = store.load(request_id)
    except ValueError as error:
        raise SkillCommandError("invalid_request_id", str(error)) from error
    except OSError as error:
        raise SkillCommandError("request_state_unavailable", str(error)) from error
    if payload is None:
        raise SkillCommandError(
            "request_not_found",
            f"No stored request state was found for {request_id!r}.",
        )
    previous = payload.get("_last_reported")
    start = _request_signature(payload)[:3]
    deadline = monotonic() + wait_ms / 1000
    try:
        if not payload.get("terminal"):
            transport = transport_for_record(payload, environment)
            if resolver is not None or command_runner is not None:
                transport = AgentRequestTransport(
                    resolver=resolver or transport._resolver,
                    sender_identity="",
                    environment=environment,
                    command_runner=command_runner or run_bounded_command,
                )
            machine = RequestLifecycleMachine(
                transport, store, monotonic=monotonic, sleep=sleep
            )
            while True:
                payload = machine.refresh(request_id=request_id)
                _record_request_route(payload, transport, store)
                if (
                    payload.get("terminal")
                    or _request_signature(payload)[:3] != start
                    or monotonic() >= deadline
                ):
                    break
                sleep(max(0, min(
                    DEFAULT_REQUEST_POLL_INTERVAL_SECONDS, deadline - monotonic()
                )))
        return _report_request(
            payload, store, verbose=verbose,
            changed=_request_signature(payload) != previous,
            on_report=on_report,
        )
    except (OSError, ValueError) as error:
        raise SkillCommandError("request_state_unavailable", str(error)) from error


def _validate_defer_timeout(timeout_ms: int) -> None:
    if timeout_ms <= 0:
        raise SkillCommandError(
            "invalid_defer_timeout", "--defer-timeout must be greater than zero."
        )


def _request_metadata(
    sender: AgentRecord, *, host: str, label: str | None, route: str | None,
) -> dict[str, Any]:
    return {
        "sender": _record_payload(sender),
        "_context": {"host": host, "label": label, "route": route},
    }


def _request_text(record: Mapping[str, Any]) -> str:
    response = record.get("response")
    output = response.get("output") if isinstance(response, Mapping) else ""
    return clean_agent_text(str(output or ""), prompt=record.get("_prompt"))


def _request_signature(record: Mapping[str, Any]) -> list[Any]:
    return [
        record.get("state"), record.get("phase"), record.get("error_code"),
        hashlib.sha256(_request_text(record).encode("utf-8")).hexdigest(),
    ]


def _report_request(
    record: dict[str, Any], store: RequestStateStore, *,
    verbose: bool, changed: bool | None = None,
    on_report: Callable[[Callable[[], None]], None] | None = None,
) -> dict[str, Any]:
    """Acknowledge helper returns immediately, or CLI output after its JSON flush."""
    signature = _request_signature(record)
    if verbose:
        payload = _public_request_payload(record)
    elif changed is False:
        payload = {
            key: record.get(key)
            for key in ("request_id", "state", "terminal", "submitted")
        }
        payload["changed"] = False
    else:
        payload = compact_request_payload(record, response_text=_request_text(record))
        if changed is not None:
            payload["changed"] = changed
    request_id = record["request_id"]

    def persist_report() -> None:
        with store.lock(request_id):
            latest = store.load(request_id) or record
            latest["_last_reported"] = signature
            store.save(request_id, latest)

    if on_report is None:
        persist_report()
    else:
        on_report(persist_report)
    return payload


def _record_request_route(
    payload: dict[str, Any],
    transport: AgentRequestTransport,
    store: RequestStateStore,
) -> None:
    """Persist the current safe route alongside one request lifecycle record."""

    resolution = transport.resolution
    if resolution is None:
        return
    recipient = payload.get("recipient")
    if resolution.agent.identity != payload.get("_target_identity"):
        # A replacement observed during run/refresh is not the request's target.
        # Keep the route saved with the original recipient, including on the
        # first run where no top-level route has yet been persisted.
        if not isinstance(recipient, dict) or not isinstance(recipient.get("route"), str):
            return
        route = recipient["route"]
        route_refreshed = bool(recipient.get("route_refreshed"))
    else:
        route = resolution.route
        route_refreshed = resolution.route_refreshed
    payload["route_refreshed"] = route_refreshed
    payload["route"] = route
    if isinstance(recipient, dict):
        recipient["route_refreshed"] = route_refreshed
        recipient["route"] = route
    context = payload.get("_context")
    if isinstance(context, dict):
        context["route"] = route
        # A verified route supersedes the mutable host/label address. Keeping
        # both makes request-status reject its own persisted context.
        context["label"] = None
    request_id = payload.get("request_id")
    if isinstance(request_id, str):
        with store.lock(request_id):
            # The worker may have delivered or cancelled since this observation.
            # Persist only routing metadata, never an older lifecycle state.
            latest = store.load(request_id) or payload
            latest["route_refreshed"] = route_refreshed
            latest["route"] = route
            latest_recipient = latest.get("recipient")
            if isinstance(latest_recipient, dict):
                latest_recipient["route_refreshed"] = route_refreshed
                latest_recipient["route"] = route
            latest_context = latest.get("_context")
            if isinstance(latest_context, dict):
                latest_context["route"] = route
                latest_context["label"] = None
            store.save(request_id, latest)


# Agent tools often truncate long single-line JSON, so decision fields come
# first and bulky output, identity, and route data come last.
_REQUEST_PAYLOAD_TRAILING_KEYS = ("response", "recipient", "sender", "route")


def _public_request_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Hide private data and put lifecycle decision fields before bulky data."""

    public = {
        key: value
        for key, value in payload.items()
        if not key.startswith("_")
    }
    ordered = {
        key: value
        for key, value in public.items()
        if key not in _REQUEST_PAYLOAD_TRAILING_KEYS
    }
    ordered.update(
        (key, public[key]) for key in _REQUEST_PAYLOAD_TRAILING_KEYS if key in public
    )
    return ordered


def _add_recipient_arguments(parser: argparse.ArgumentParser, *, required: bool = True) -> None:
    parser.add_argument(
        "--host", default="local", help="Herdr host: local or a configured SSH alias."
    )
    address = parser.add_mutually_exclusive_group(required=required)
    address.add_argument("--label", help="Exact current agent label on --host.")
    address.add_argument("--route", help="Opaque verified route returned by HAM.")
    address.add_argument("--address", help="Exact HOST/LABEL address returned by list.")

def parse_cli_arguments(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Route Herdr prompts using an address, label, or verified route."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    list_parser = subparsers.add_parser("list", help="List current labeled agents.")
    list_parser.add_argument(
        "--host", default="local", help="Herdr host: local or a configured SSH alias."
    )
    list_parser.add_argument(
        "--verbose",
        action="store_true",
        help="Include the full AgentRecord-compatible payload.",
    )
    list_parser.add_argument(
        "--legacy",
        dest="verbose",
        action="store_true",
        help="Alias for --verbose for legacy consumers.",
    )

    status_parser = subparsers.add_parser(
        "status",
        help="Read one resolved agent's current status.",
    )
    _add_recipient_arguments(status_parser, required=False)
    status_parser.add_argument(
        "--verbose",
        action="store_true",
        help="Include the full AgentRecord-compatible payload.",
    )
    status_operations = status_parser.add_subparsers(dest="status_operation")
    status_set = status_operations.add_parser("set", help="Set this agent's public status.")
    status_set.add_argument("phase")
    status_set.add_argument("--summary", required=True)
    status_operations.add_parser("clear", help="Clear this agent's public status.")
    status_list = status_operations.add_parser("list", help="List current status by repository.")
    status_list.add_argument("--repo", nargs="?", const="", default=None)
    status_board = status_operations.add_parser("board", help="Read local and registered-peer rich status without refreshing registry state.")
    status_board.add_argument("--repo", nargs="?", const="", default=None,
                              help="Optional exact repository; bare --repo selects the current caller repository.")
    status_board.add_argument("--host", dest="board_host",
                              help="Local/canonical/registered SSH alias; default: local and all registered peers.")

    send_parser = subparsers.add_parser(
        "send",
        help="Send a prompt to one resolved agent.",
    )
    _add_recipient_arguments(send_parser)
    send_parser.add_argument(
        "--message", required=True, help="Prompt text to send unchanged."
    )
    wait_group = send_parser.add_mutually_exclusive_group()
    wait_group.add_argument(
        "--wait", dest="wait", action="store_true", default=True,
        help="Wait for Herdr's completion signal (default).",
    )
    wait_group.add_argument(
        "--no-wait", dest="wait", action="store_false",
        help="Return after prompt acceptance without waiting.",
    )
    send_parser.add_argument(
        "--timeout", type=int, default=DEFAULT_WAIT_TIMEOUT_MS,
        help="Wait deadline in ms (default: 120000).",
    )

    batch_parser = subparsers.add_parser(
        "batch",
        help="Send tailored address/message or route/message JSON concurrently.",
    )
    batch_parser.add_argument(
        "--requests-json",
        required=True,
        help="JSON request array, or '-' to read it from stdin.",
    )
    batch_wait_group = batch_parser.add_mutually_exclusive_group()
    batch_wait_group.add_argument(
        "--wait",
        dest="wait",
        action="store_true",
        default=True,
        help="Observe each submitted request through its lifecycle (default).",
    )
    batch_wait_group.add_argument(
        "--no-wait", dest="wait", action="store_false",
        help="Return after prompt acceptance without observing completion.",
    )
    batch_parser.add_argument(
        "--timeout", type=int, default=DEFAULT_WAIT_TIMEOUT_MS,
        help="Per-request wait deadline in ms (default: 120000).",
    )
    batch_parser.add_argument(
        "--max-workers",
        type=int,
        default=DEFAULT_BATCH_WORKERS,
        help="Concurrent dispatch limit, 1–8 (default: 4).",
    )

    read_parser = subparsers.add_parser(
        "read",
        help="Read one resolved agent's recent output.",
    )
    _add_recipient_arguments(read_parser)
    read_parser.add_argument(
        "--lines", type=int, default=DEFAULT_READ_LINES,
        help="Maximum recent output lines (default: 120).",
    )
    read_parser.add_argument(
        "--max-bytes",
        type=int,
        default=DEFAULT_READ_MAX_BYTES,
        help="Maximum UTF-8 output bytes (default: 65536).",
    )
    read_parser.add_argument(
        "--cursor",
        "--watermark",
        dest="cursor",
        help="Return only output after this prior read cursor when safe.",
    )

    request_parser = subparsers.add_parser(
        "request",
        help="Submit, safely observe, and read one agent request.",
    )
    _add_recipient_arguments(request_parser)
    request_parser.add_argument(
        "--message", required=True, help="Prompt text for this request."
    )
    request_parser.add_argument(
        "--timeout", type=int, default=DEFAULT_WAIT_TIMEOUT_MS,
        help="Lifecycle observation deadline in ms (default: 120000).",
    )
    request_parser.add_argument(
        "--output-lines",
        type=int,
        default=DEFAULT_READ_LINES,
        help="Maximum response lines (default: 120).",
    )
    request_parser.add_argument(
        "--output-chars",
        type=int,
        default=DEFAULT_REQUEST_OUTPUT_CHARS,
        help="Maximum response characters (default: 12000).",
    )

    request_status_parser = subparsers.add_parser(
        "request-status",
        help="Read the latest stored state for a request ID.",
    )
    request_status_parser.add_argument(
        "--request-id", required=True, help="Request ID returned by request or batch."
    )
    request_status_parser.add_argument(
        "--wait", type=int, default=0,
        help="Observe until state or phase changes, terminal, or deadline in ms (default: 0).",
    )
    for command_parser in (
        send_parser, read_parser, request_parser, request_status_parser, batch_parser,
    ):
        command_parser.add_argument(
            "--verbose", action="store_true",
            help="Include the full legacy payload instead of compact output.",
        )
    for command_parser in (send_parser, request_parser, batch_parser):
        command_parser.add_argument(
            "--defer-timeout", type=int, default=DEFAULT_DEFER_TIMEOUT_MS,
            help="Hold focused-recipient delivery before asking for a decision in ms (default: 180000).",
        )
    add_spawn_parser(subparsers)
    ledger_cli.add_parsers(subparsers)
    return parser.parse_args(argv)


def main(
    argv: Sequence[str] | None = None,
    environment: Mapping[str, str] | None = None,
) -> int:
    command = ""
    exit_code = 0
    reports: list[Callable[[], None]] = []
    try:
        arguments = parse_cli_arguments(argv)
        command = arguments.command
        if getattr(arguments, "address", None) is not None:
            arguments.host, arguments.label = parse_agent_address(
                arguments.address, arguments.host
            )
        if arguments.command in ledger_cli.COMMANDS:
            payload = ledger_cli.dispatch(arguments, environment)
        elif arguments.command == "list":
            payload = list_command(
                arguments.host,
                environment,
                verbose=arguments.verbose,
            )
        elif arguments.command == "status":
            if getattr(arguments, "status_operation", None) is not None:
                payload = ledger_cli.status_operation(
                    arguments.status_operation,
                    environment=environment,
                    phase=getattr(arguments, "phase", None),
                    summary=getattr(arguments, "summary", ""),
                    repo_id=getattr(arguments, "repo", None),
                    host=getattr(arguments, "board_host", None),
                )
            else:
                if arguments.label is None and arguments.route is None:
                    raise SkillCommandError("missing_address", "Use status --address HOST/LABEL or status set|clear|list.")
                payload = status_command(
                    host=arguments.host,
                    label=arguments.label,
                    route=arguments.route,
                    verbose=arguments.verbose,
                    environment=environment,
                )
        elif arguments.command == "send":
            payload = send_command(
                host=arguments.host,
                label=arguments.label,
                route=arguments.route,
                message=arguments.message,
                wait=arguments.wait,
                timeout_ms=arguments.timeout,
                defer_timeout_ms=arguments.defer_timeout,
                verbose=arguments.verbose,
                environment=environment,
                on_report=reports.append,
            )
        elif arguments.command == "batch":
            requests_json = (
                sys.stdin.read()
                if arguments.requests_json == "-"
                else arguments.requests_json
            )
            payload = batch_command(
                requests_json=requests_json,
                wait=arguments.wait,
                timeout_ms=arguments.timeout,
                max_workers=arguments.max_workers,
                defer_timeout_ms=arguments.defer_timeout,
                verbose=arguments.verbose,
                environment=environment,
                on_report=reports.append,
            )
            statuses = [item["status"] for item in payload["results"]]
            if any(status in {FAILED, CANCELLED, TIMED_OUT} for status in statuses):
                exit_code = 1
            elif any(status in {SUBMITTED, DEFERRED} for status in statuses):
                exit_code = 3
        elif arguments.command == "read":
            payload = read_command(
                host=arguments.host,
                label=arguments.label,
                route=arguments.route,
                lines=arguments.lines,
                max_bytes=arguments.max_bytes,
                cursor=arguments.cursor,
                verbose=arguments.verbose,
                environment=environment,
            )
        elif arguments.command == "request":
            payload = request_command(
                host=arguments.host,
                label=arguments.label,
                route=arguments.route,
                message=arguments.message,
                timeout_ms=arguments.timeout,
                output_lines=arguments.output_lines,
                output_chars=arguments.output_chars,
                defer_timeout_ms=arguments.defer_timeout,
                verbose=arguments.verbose,
                environment=environment,
                on_report=reports.append,
            )
        elif arguments.command == "request-status":
            payload = request_status_command(
                request_id=arguments.request_id,
                wait_ms=arguments.wait,
                verbose=arguments.verbose,
                environment=environment,
                on_report=reports.append,
            )
        elif arguments.command == "spawn":
            payload = spawn_command(arguments, environment)
            initial_prompt = payload.get("initial_prompt")
            if isinstance(initial_prompt, Mapping) and initial_prompt.get("acceptance") != "accepted":
                exit_code = 1
        else:  # pragma: no cover - argparse rejects unknown commands
            raise SkillCommandError("invalid_command", "Unsupported command.")
        if command in {"request", "request-status"} or (
            command == "send" and "state" in payload
        ):
            exit_code = {
                "submitted_settled": 0,
                "submission_failed": 1,
                "submitted_working": 3,
                "submitted_unknown": 3,
                "deferred": 3,
                "awaiting_decision": 3,
            }[payload["state"]]
        _write_json(payload, compact=command == "batch")
        if reports:
            sys.stdout.flush()
            for report in reports:
                report()
        return exit_code
    except (BatchDispatchError, SkillCommandError, SpawnError, LedgerError) as error:
        _write_json(
            {"error": {"code": error.code, "message": error.message,
                       **({"holder": error.holder} if hasattr(error, "holder") else {})}},
            compact=command == "batch",
        )
        return 1
    except Exception as error:
        _write_json(
            {"error": {"code": "internal_error", "message": str(error)}},
            compact=command == "batch",
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
