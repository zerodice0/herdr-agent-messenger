"""Mailbox CLI adapter shared by the skill launcher, plugin events and runtimes."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
from typing import Any, Mapping

from ham.ledger.mail import Mailbox
from ham.ledger.status import AgentRegistry, claude_foreground_pid, live_snapshot, repository_identity
from ham.ledger.store import LedgerError, LedgerStore
from ham.ledger.remote import RemoteLedger, SSHTransport
from ham.ledger.worker import ensure_worker, run_worker, worker_status

COMMANDS = frozenset({"mail", "inbox", "repo", "ledger", "runtime", "device"})


def add_parsers(subparsers: Any) -> None:
    device = subparsers.add_parser("device", help="Claim and inspect generation-owned device occupancy.")
    devices = device.add_subparsers(dest="device_command", required=True)
    claim = devices.add_parser("claim", help="Claim a device; --force only on explicit user instruction.")
    claim.add_argument("kind", choices=("phone", "emu"))
    claim.add_argument("device")
    claim.add_argument("--label")
    claim.add_argument("--force", action="store_true")
    release = devices.add_parser("release", help="Release only this agent's claims (default: all).")
    release.add_argument("kind", choices=("phone", "emu", "all"), nargs="?", default="all")
    release.add_argument("device", nargs="?")
    devices.add_parser("list", help="Read claims and adb attachment without changing ledger or tokens.")
    installer = subparsers.add_parser("runtime", help="Explicit ownership-checked opt-in runtime installation.")
    install_commands = installer.add_subparsers(dest="runtime_operation", required=True)
    for operation in ("install", "uninstall", "status"):
        description = ("Read installation health and active local receiver counts (no client), or one runtime."
                       if operation == "status" else f"{operation.title()} a HAM-owned client runtime.")
        install = install_commands.add_parser(operation, help=description, description=description)
        install.add_argument("client", choices=("omp", "claude"), **({"nargs": "?"} if operation == "status" else {}))
        install.add_argument("--destination", help="Requires a client. Explicit directory: omp defaults to ~/.omp/agent/extensions; claude to $CLAUDE_CONFIG_DIR or ~/.claude.")
    mail = subparsers.add_parser("mail", help="Leave immutable messages without interrupting an agent.")
    mail_commands = mail.add_subparsers(dest="mail_command", required=True)
    send = mail_commands.add_parser("send", help="Queue local/remote mail across repositories without interrupting work.")
    send.add_argument("--to", required=True, help="Canonical-host/LABEL, configured SSH-alias/LABEL, local/LABEL, main, or [HOST/]main@REPO.")
    send.add_argument("--kind", choices=("fyi", "action"), default="fyi")
    send.add_argument("--reply-to", help="Retained message UUID; reply also marks an addressed parent handled.")
    send.add_argument("--subject", default="", help="New thread subject (up to 1024 UTF-8 bytes).")
    body = send.add_mutually_exclusive_group(required=True)
    body.add_argument("--body", help="Message body, at most 16 KiB UTF-8.")
    body.add_argument("--body-file", help="Read the body from this UTF-8 file.")
    withdraw = mail_commands.add_parser("withdraw", help="Withdraw an unclaimed message from this terminal.")
    withdraw.add_argument("--message", required=True, help="Message UUID.")
    show = mail_commands.add_parser("show", help="View a thread without changing delivery state.")
    show.add_argument("--thread", required=True, help="Thread UUID (first message ID).")
    for operation, description in (
        ("threads", "List retained repository threads from all senders and recipients."),
        ("direct-list", "List this repository's retained user-direct submission results."),
    ):
        history = mail_commands.add_parser(operation, help=description)
        history.add_argument("--repo", help="Exact repository identity; default: current verified caller repository.")
        history.add_argument("--limit", type=int, default=100, help="Page size, 1..1000 (default 100).")
        history.add_argument("--cursor", help="Opaque next_cursor from the same ledger, command and repository.")

    inbox = subparsers.add_parser("inbox", help="Read your terminal's mailbox and explicitly handle messages.")
    inbox_commands = inbox.add_subparsers(dest="inbox_command", required=True)
    read = inbox_commands.add_parser("read", help="View messages only; never acknowledges delivery.")
    read.add_argument("--message", help="Read one message UUID, including final states.")
    read.add_argument("--all", action="store_true", help="Include handled and other final messages.")
    read.add_argument("--limit", type=int, default=100, help="Maximum rows, 1..1000 (default 100).")
    read.add_argument("--verbose", action="store_true",
                      help="Include full message and delivery diagnostic fields.")
    handle = inbox_commands.add_parser("handle", help="Explicitly mark messages handled, without inventing delivered_at.")
    handle.add_argument("--message", required=True, action="append", help="Message UUID; repeat for atomic batch handling.")

    repo = subparsers.add_parser("repo", help="Read coordination repository settings.")
    repo.add_subparsers(dest="repo_command", required=True).add_parser("show", help="Show this agent's repository and settings.")

    ledger = subparsers.add_parser("ledger", help="Internal Python-owned mailbox services (JSON stdin where needed).")
    commands = ledger.add_subparsers(dest="ledger_command", required=True)
    commands.add_parser("doctor", help="Show host identity, schema and counts without contacting Herdr.")
    setup = commands.add_parser("setup", help="Create/check this ledger's fixed canonical host identity.")
    setup.add_argument("--name", required=True, help="Canonical host name; existing identities cannot be renamed.")
    peer = commands.add_parser("peer", help="Register canonical peers using the existing SSH allowlist.")
    peer_commands = peer.add_subparsers(dest="peer_command", required=True)
    peer_commands.add_parser("list", help="Show canonical UUID, transport alias, checkout root and isolated-ledger override.")
    add_peer = peer_commands.add_parser("add", help="Discover a structured plugin_root and verify the remote protocol/identity.")
    add_peer.add_argument("--ssh-alias", required=True)
    add_peer.add_argument("--remote-root", help="Explicit absolute protocol-capable checkout (for isolated work-copy testing).")
    add_peer.add_argument("--remote-ledger-dir", default="", help="Explicit absolute remote ledger directory for isolated testing.")
    add_peer.add_argument("--remote-socket", default="", help="Explicit absolute remote Herdr socket for isolated testing.")
    add_peer.add_argument("--canonical-name", help="Require this remote canonical name.")
    add_peer.add_argument("--uuid", help="Require this remote host UUID.")
    for name in ("accept", "receipt", "setting", "withdraw"):
        commands.add_parser(name, help=f"JSON stdin protocol 1 peer envelope: {name}.")
    export = commands.add_parser("status-export", help="Export live local agents/roles only; never replicates remote AGENTS.")
    export.add_argument("--repo", help="Filter by exact repository identity.")
    export.add_argument("--wire", action="store_true", help="Read an authenticated protocol envelope from stdin (SSH transport).")
    outbox = commands.add_parser("outbox", help="Inspect FIFO pending, sent and failed items including error codes.")
    outbox.add_argument("--limit", type=int, default=100)
    retry = commands.add_parser("retry", help="Explicitly retry one failed/pending outbox item with its original identity.")
    retry.add_argument("--item", required=True, help="Outbox item UUID.")
    worker = commands.add_parser("worker", help="Detached per-ledger flock worker.")
    worker_commands = worker.add_subparsers(dest="worker_command", required=True)
    for operation in ("ensure", "run", "once", "status"):
        worker_commands.add_parser(operation)
    commands.add_parser("reconcile", help="Register a complete live Herdr snapshot and sweep terminal slots.")
    commands.add_parser("event", help="One-shot plugin event: reconcile from a fresh complete snapshot.")
    commands.add_parser("retention", help="Delete expired bodies/logs while preserving compact dedupe receipts.")
    commands.add_parser("agent-session", help="JSON stdin: register a matching runtime session and extension presence.")
    delivery = commands.add_parser("delivery", help="Internal runtime claim/application/release services.")
    operations = delivery.add_subparsers(dest="delivery_command", required=True)
    for operation in ("claim", "started", "release"):
        operations.add_parser(operation, help=f"JSON stdin: {operation} one runtime context-delivery claim.")
    runtime = commands.add_parser("runtime", help="Root native receiver registration, bounded admission and journal ACK.")
    runtime_commands = runtime.add_subparsers(dest="runtime_command", required=True)
    for operation in ("register", "inspect", "claim", "applied", "claude-hook"):
        runtime_commands.add_parser(operation, help=f"JSON stdin native receiver {operation}.")


def _stdin_object() -> dict[str, Any]:
    data = sys.stdin.read(1024 * 1024 + 1)
    if len(data) > 1024 * 1024:
        raise LedgerError("invalid_request", "Internal JSON request exceeds 1 MiB.")
    try:
        value = json.loads(data)
    except (ValueError, RecursionError):
        raise LedgerError("invalid_request", "Expected one JSON object on stdin.") from None
    if not isinstance(value, dict):
        raise LedgerError("invalid_request", "Expected one JSON object on stdin.")
    return value


def _live_context(registry: AgentRegistry, snapshot: Mapping[str, Any],
                  environment: Mapping[str, str] | None) -> dict[str, Any]:
    agent = registry.resolve_context(environment, snapshot)
    live = next((raw for raw in snapshot["agents"]
                 if raw.get("terminal_id") == agent["terminal_id"] and raw.get("agent")), None)
    if live is None:
        raise LedgerError("recipient_absent", "The calling terminal no longer has an agent.")
    session = live.get("agent_session")
    reference = str(session.get("value") or '') if isinstance(session, dict) else ''
    reference = reference or "unreported"
    identity = repository_identity(str(live.get("cwd") or ''), registry.store.host)
    matching_session = reference == agent["session_ref"]
    if (not matching_session and reference == "unreported"
            and live["agent"] == agent["client"] == "claude"
            and agent.get("native_session_pid") is not None):
        matching_session = claude_foreground_pid(agent, environment) == agent["native_session_pid"]
    if not matching_session or live["agent"] != agent["client"] or identity["repo_id"] != agent["repo_id"]:
        raise LedgerError("stale_session", "Run ledger reconcile for the current Herdr session before receiving mail.")
    location = next((pane for pane in snapshot["panes"]
                     if pane.get("terminal_id") == agent["terminal_id"]), None)
    if location is None:
        raise LedgerError("recipient_absent", "The calling terminal no longer has a live pane.")
    return {**agent, "pane_id": str(location.get("pane_id") or ""),
            "workspace_id": str(location.get("workspace_id") or "")}


def dispatch(arguments: argparse.Namespace, environment: Mapping[str, str] | None = None) -> dict[str, Any]:
    if arguments.command == "device":
        from ham.ledger.devices import device_operation
        return device_operation(arguments, environment)
    if arguments.command == "runtime":
        from ham.ops.runtime_installer import destinations, health_summary, manage
        if arguments.runtime_operation == "status" and arguments.client is None:
            if arguments.destination is not None:
                raise LedgerError("invalid_destination", "--destination requires an explicit runtime client.")
            return health_summary(environment)
        destination = arguments.destination
        if destination is None:
            destination = destinations(environment)[arguments.client]
        return manage(arguments.runtime_operation, destination, client=arguments.client)
    name = arguments.name if arguments.command == "ledger" and arguments.ledger_command == "setup" else None
    read_only_export = False
    if (arguments.command == "ledger" and arguments.ledger_command == "status-export"
            and arguments.wire):
        arguments.wire_envelope = _stdin_object()
        wire_payload = arguments.wire_envelope.get("payload")
        read_only_export = isinstance(wire_payload, dict) and wire_payload.get("refresh") is False
    store = (LedgerStore.open_readonly(environment) if read_only_export else
             LedgerStore(environment=environment, canonical_name=name))
    if store is None:
        raise LedgerError("ledger_not_initialized", "No existing HAM ledger is available to read.")
    payload = _dispatch(arguments, environment, store)
    values = os.environ if environment is None else environment
    mutating = ((arguments.command == "mail" and arguments.mail_command not in {"show", "threads", "direct-list"}) or
                (arguments.command == "inbox" and arguments.inbox_command == "handle") or
                (arguments.command == "ledger" and arguments.ledger_command in
                 {"reconcile", "event", "accept", "receipt", "setting", "withdraw", "retry", "agent-session", "delivery", "peer", "runtime"}))
    if mutating and values.get("HERDR_AGENT_LEDGER_AUTOSTART", "1") != "0":
        try:
            ensure_worker(store, environment)
        except (OSError, LedgerError) as error:
            message = error.message if isinstance(error, LedgerError) else str(error)
            payload = {**payload, "warnings": [*payload.get("warnings", []),
                       {"code": "worker_start_failed", "message": message, "committed": True}]}
    return store.annotate(payload)
def status_operation(operation: str, *, environment: Mapping[str, str] | None = None,
                     phase: str | None = None, summary: str = "",
                     repo_id: str | None = None, host: str | None = None) -> dict[str, Any]:
    if operation in {"list", "board"}:
        try:
            store = LedgerStore.open_readonly(environment)
        except LedgerError as error:
            if error.code != "ledger_not_initialized":
                raise
            store = None
        if store is None:
            return {"repo_id": repo_id, "agents": [], **({"hosts": []} if operation == "board" else {})}
    else:
        store = LedgerStore(environment=environment)
    registry = AgentRegistry(store)
    if operation in {"list", "board"}:
        if repo_id == "":
            repo_id = registry.resolve_context(environment, live_snapshot(environment))["repo_id"]
        if operation == "board":
            from ham.ledger.views import status_board
            return status_board(store, repo_id=repo_id, host=host, environment=environment)
        return {"repo_id": repo_id, "agents": registry.list(repo_id)}
    if operation not in {"set", "clear"}:
        raise LedgerError("invalid_status_operation", "Status operation must be set, clear, list, or board.")
    snapshot, _ = registry.refresh(environment)
    agent = registry.resolve_context(environment, snapshot)
    return registry.set_status(
        agent,
        phase="" if operation == "clear" else str(phase or ""),
        summary="" if operation == "clear" else summary,
        feature_invoked=True,
        environment=environment,
    )




def _dispatch(arguments: argparse.Namespace, environment: Mapping[str, str] | None,
              store: LedgerStore) -> dict[str, Any]:
    registry, mailbox = AgentRegistry(store), Mailbox(store)
    remote = RemoteLedger(store, environment)
    if arguments.command == "ledger":
        command = arguments.ledger_command
        if command in {"doctor", "setup"}:
            return {**store.doctor(), "peers": remote.peers(), "worker": worker_status(store)}
        if command == "peer":
            if arguments.peer_command == "list":
                return {"peers": remote.peers()}
            peer = SSHTransport(store, environment).discover(
                arguments.ssh_alias, remote_root=arguments.remote_root, remote_ledger_dir=arguments.remote_ledger_dir,
                remote_socket=arguments.remote_socket)
            if ((arguments.canonical_name and peer["canonical_name"] != arguments.canonical_name) or
                    (arguments.uuid and peer["uuid"] != arguments.uuid)):
                raise LedgerError("peer_identity_conflict", "Discovered remote identity differs from the requested identity.")
            return {"peer": remote.add_peer(**peer)}
        if command == "worker":
            operation = arguments.worker_command
            if operation == "ensure":
                return ensure_worker(store, environment)
            if operation == "status":
                return worker_status(store)
            return run_worker(store, environment, once=operation == "once")
        if command == "outbox":
            if not 1 <= arguments.limit <= 1000:
                raise LedgerError("invalid_limit", "Outbox limit must be 1..1000.")
            return {"items": remote.outbox()[:arguments.limit]}
        if command == "retry":
            return remote.retry(arguments.item)
        if command == "retention":
            return store.retention()
        if command in {"reconcile", "event"}:
            result = registry.refresh(environment)[1]
            try:
                from ham.ops.receiver_notifications import notify_missing_runtime
                notify_missing_runtime(store, environment)
            except Exception as error:
                message = "HAM receiver notification hook failed after refresh: " + str(error)
                store.notification_errors.append({
                    "code": "notification_failed", "message": message,
                    "committed": True, "recovery": "receiver_runtime_set_change"})
                try:
                    store.note_notification_failure(message)
                except Exception:
                    pass
            return store.annotate(result)
        if command in {"accept", "receipt", "setting", "withdraw"} or (command == "status-export" and arguments.wire):
            envelope = getattr(arguments, "wire_envelope", None)
            if envelope is None:
                envelope = _stdin_object()
            if command == "status-export":
                _, payload = remote.authenticate(envelope)
                refresh = payload.get("refresh", True)
                if not isinstance(refresh, bool):
                    raise LedgerError("invalid_request", "Status export refresh must be boolean.")
                if refresh:
                    registry.refresh(environment)
            return remote.receive(command, envelope)
        if command == "status-export":
            registry.refresh(environment)
            return {"protocol": 1, **remote.status_export(arguments.repo)}
        if command == "runtime":
            from ham.ledger.runtime import NativeReceiver
            data = _stdin_object()
            receiver = NativeReceiver(store)
            try:
                if arguments.runtime_command == "register":
                    snapshot = registry.refresh(environment)[0]
                    agent = _live_context(registry, snapshot, environment)
                    return receiver.register(agent, **data)
                if arguments.runtime_command == "claude-hook":
                    snapshot = registry.refresh(environment)[0]
                    agent = _live_context(registry, snapshot, environment)
                    subagent_id = data.get("agent_id")
                    if subagent_id is not None and not isinstance(subagent_id, str):
                        raise LedgerError("invalid_request", "Claude hook agent_id must be text.")
                    return receiver.claude_hook(
                        agent,
                        hook_event_name=data.get("hook_event_name"),
                        session_id=data.get("session_id"),
                        transcript_path=data.get("transcript_path"),
                        subagent_id=subagent_id,
                        environment=environment,
                    )
                return getattr(receiver, arguments.runtime_command)(**data)
            except TypeError as error:
                raise LedgerError("invalid_request", str(error)) from None
        data = _stdin_object()
        if command == "agent-session":
            registry.refresh(environment)
            method = registry.agent_session
        else:
            method = getattr(mailbox, arguments.delivery_command)
        try:
            return method(**data)
        except TypeError as error:
            raise LedgerError("invalid_request", str(error)) from None
    if arguments.command == "mail" and arguments.mail_command == "show":
        return mailbox.show_thread(arguments.thread)
    if arguments.command == "mail" and arguments.mail_command in {"threads", "direct-list"}:
        repo_id = arguments.repo
        if repo_id is None:
            repo_id = _live_context(registry, live_snapshot(environment), environment)["repo_id"]
        if not repo_id:
            raise LedgerError("invalid_repo", "History requires a nonempty repository identity.")
        method = mailbox.list_threads if arguments.mail_command == "threads" else mailbox.list_direct
        return method(repo_id, arguments.limit, cursor=arguments.cursor)
    # Reads never refresh last_seen/status/bells. Mutations acquire the snapshot
    # before updating the registry under its serialization lock.
    mutating = arguments.command == "mail" or (arguments.command == "inbox" and arguments.inbox_command == "handle")
    snapshot = registry.refresh(environment)[0] if mutating else live_snapshot(environment)
    sender = _live_context(registry, snapshot, environment)
    if arguments.command == "repo":
        return registry.repo_show(sender["repo_id"], sender["host"], sender["workspace_id"])
    if arguments.command == "inbox":
        if arguments.inbox_command == "read":
            return mailbox.read(sender, message_id=arguments.message, all_states=arguments.all, limit=arguments.limit)
        return mailbox.handle(sender, arguments.message)
    if arguments.mail_command == "withdraw":
        return mailbox.withdraw(arguments.message, sender=sender)
    recipient = remote.resolve_recipient(arguments.to, sender)
    try:
        body = arguments.body if arguments.body is not None else Path(arguments.body_file).read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise LedgerError("body_file_unreadable", str(error)) from None
    return mailbox.send(sender, recipient, body=body, kind=arguments.kind,
                        reply_to=arguments.reply_to, subject=arguments.subject)

