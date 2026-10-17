"""Mailbox CLI adapter shared by the skill launcher, plugin events and runtimes."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
from typing import Any, Mapping

from ledger_mail import Mailbox
from ledger_status import AgentRegistry, live_snapshot, repository_identity
from ledger_store import LedgerError, LedgerStore

COMMANDS = frozenset({"mail", "inbox", "repo", "ledger"})


def add_parsers(subparsers: Any) -> None:
    mail = subparsers.add_parser("mail", help="Leave immutable messages without interrupting an agent.")
    mail_commands = mail.add_subparsers(dest="mail_command", required=True)
    send = mail_commands.add_parser("send", help="Queue a local same-repository message.")
    send.add_argument("--to", required=True, help="Current local/LABEL or canonical-host/LABEL address.")
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

    inbox = subparsers.add_parser("inbox", help="Read your terminal's mailbox and explicitly handle messages.")
    inbox_commands = inbox.add_subparsers(dest="inbox_command", required=True)
    read = inbox_commands.add_parser("read", help="View messages only; never acknowledges delivery.")
    read.add_argument("--message", help="Read one message UUID, including final states.")
    read.add_argument("--all", action="store_true", help="Include handled and other final messages.")
    read.add_argument("--limit", type=int, default=100, help="Maximum rows, 1..1000 (default 100).")
    handle = inbox_commands.add_parser("handle", help="Explicitly mark messages handled, without inventing delivered_at.")
    handle.add_argument("--message", required=True, action="append", help="Message UUID; repeat for atomic batch handling.")

    repo = subparsers.add_parser("repo", help="Read coordination repository settings.")
    repo.add_subparsers(dest="repo_command", required=True).add_parser("show", help="Show this agent's repository and settings.")

    ledger = subparsers.add_parser("ledger", help="Internal Python-owned mailbox services (JSON stdin where needed).")
    commands = ledger.add_subparsers(dest="ledger_command", required=True)
    commands.add_parser("doctor", help="Show host identity, schema and counts without contacting Herdr.")
    commands.add_parser("reconcile", help="Register a complete live Herdr snapshot and sweep terminal slots.")
    commands.add_parser("event", help="One-shot plugin event: reconcile from a fresh complete snapshot.")
    commands.add_parser("retention", help="Delete expired bodies/logs while preserving compact dedupe receipts.")
    commands.add_parser("agent-session", help="JSON stdin: register a matching runtime session and extension presence.")
    delivery = commands.add_parser("delivery", help="Internal runtime claim/application/release services.")
    operations = delivery.add_subparsers(dest="delivery_command", required=True)
    for operation in ("claim", "started", "release"):
        operations.add_parser(operation, help=f"JSON stdin: {operation} one runtime context-delivery claim.")


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
    if reference != agent["session_ref"] or live["agent"] != agent["client"] or identity["repo_id"] != agent["repo_id"]:
        raise LedgerError("stale_session", "Run ledger reconcile for the current Herdr session before receiving mail.")
    return agent


def dispatch(arguments: argparse.Namespace, environment: Mapping[str, str] | None = None) -> dict[str, Any]:
    store = LedgerStore(environment=environment)
    registry = AgentRegistry(store)
    mailbox = Mailbox(store)
    if arguments.command == "ledger":
        command = arguments.ledger_command
        if command == "doctor":
            return store.doctor()
        if command == "retention":
            return store.retention()
        if command in {"reconcile", "event"}:
            return registry.refresh(environment)[1]
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
    # Reads do not even refresh last_seen/status/bells. Events or explicit
    # reconcile register sessions; process ancestry resolves moved CLI callers.
    mutating = arguments.command == "mail" or (arguments.command == "inbox" and arguments.inbox_command == "handle")
    snapshot = registry.refresh(environment)[0] if mutating else live_snapshot(environment)
    sender = _live_context(registry, snapshot, environment)
    if arguments.command == "repo":
        return registry.repo_show(sender["repo_id"])
    if arguments.command == "inbox":
        if arguments.inbox_command == "read":
            return mailbox.read(sender, message_id=arguments.message, all_states=arguments.all, limit=arguments.limit)
        return mailbox.handle(sender, arguments.message)
    if arguments.mail_command == "withdraw":
        return mailbox.withdraw(arguments.message, sender=sender)
    address = arguments.to.split('/')
    if len(address) != 2 or not all(address) or address[0] not in {"local", store.host}:
        raise LedgerError("invalid_address", "This local mailbox slice requires local/LABEL or canonical-host/LABEL.")
    candidates = [agent for agent in registry.list() if agent["alias"] == address[1]]
    if len(candidates) != 1:
        raise LedgerError("agent_not_found" if not candidates else "agent_ambiguous", "Recipient label must identify one current local agent.")
    try:
        body = arguments.body if arguments.body is not None else Path(arguments.body_file).read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise LedgerError("body_file_unreadable", str(error)) from None
    return mailbox.send(sender, candidates[0], body=body, kind=arguments.kind,
                        reply_to=arguments.reply_to, subject=arguments.subject)


def main(argv: list[str] | None = None) -> int:
    # Plugin/runtime standalone entrypoint uses the exact public launcher parser.
    from agent_skill_cli import main as skill_main
    return skill_main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
