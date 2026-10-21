"""Accident-prevention preflight for legacy agent CLI prompt submission.

Callers reconcile the local registry and supply its verified sender row. One
instance covers one command/batch. Remote status is unfiltered and cached only
for that instance; remote paths are never interpreted on this host. This is not
a security boundary and is deliberately absent from user direct-input services.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ham.agents.directory import AgentRecord
from ham.ledger.remote import RemoteLedger
from ham.ledger.settings import RepoSettings
from ham.ledger.store import LedgerError, LedgerStore


class CollaborationPolicy:
    def __init__(self, store: LedgerStore, sender: Mapping[str, Any], *,
                 environment: Mapping[str, str] | None = None,
                 remote: RemoteLedger | None = None) -> None:
        self.store = store
        self.sender = sender
        self.active = RepoSettings(store).role(sender) in {"main", "worker"}
        self.remote = remote or RemoteLedger(store, environment)
        self._exports: dict[tuple[bool, str], Mapping[str, Any] | LedgerError] = {}
        self._error: LedgerError | None = None

    def record_error(self, error: Exception) -> None:
        """Retain a coordinated resolver failure for the batch commit gate.

        Batch resolution catches per-item exceptions. Its caller must record
        remote discovery failures here too, before re-raising them, so an
        unverifiable recipient cannot leave other items eligible for submission.
        Ordinary off/nonparticipant batches retain their existing partial results.
        """
        if self.active and self._error is None:
            self._error = error if isinstance(error, LedgerError) else LedgerError(
                str(getattr(error, "code", "policy_unverifiable")),
                str(getattr(error, "message", str(error))),
            )

    def before_dispatch(self) -> None:
        """Raise outside the batch resolver's per-item exception handler."""
        if self._error is not None:
            raise self._error

    def check(self, recipient: AgentRecord) -> None:
        """Reject same-repository participants before any request is persisted."""
        if not self.active:
            return
        try:
            target = self._target(recipient)
            if target["repo_id"] == self.sender["repo_id"] and target["role"] in {"main", "worker"}:
                raise LedgerError(
                    "use_mailbox",
                    "Coordination participants in the same repository must use "
                    f"mail send --to {recipient.qualified_name} --body TEXT instead "
                    "of legacy send/request/batch. No prompt was submitted.",
                )
        except Exception as error:
            refusal = error if isinstance(error, LedgerError) else LedgerError(
                "policy_unverifiable", f"Could not verify recipient coordination: {error}. No prompts were submitted.",
            )
            self.record_error(refusal)
            raise refusal from None

    def _target(self, recipient: AgentRecord) -> Mapping[str, Any]:
        key = (recipient.local, self.store.host if recipient.local else recipient.host)
        if key not in self._exports:
            try:
                self._exports[key] = (
                    self.remote.status_export() if recipient.local
                    else self.remote.query_status(recipient.host)
                )
            except LedgerError as error:
                self._exports[key] = error
        exported = self._exports[key]
        if isinstance(exported, LedgerError):
            raise exported
        host = exported.get("host")
        agents = exported.get("agents")
        if not isinstance(host, Mapping) or not isinstance(agents, list):
            raise LedgerError("policy_unverifiable", "HAM status did not report host and agents; no prompts were submitted.")
        canonical = host.get("canonical_name")
        if not isinstance(canonical, str) or not canonical:
            raise LedgerError("policy_unverifiable", "HAM status did not report a canonical host; no prompts were submitted.")
        matches = [row for row in agents if isinstance(row, Mapping)
                   and recipient.terminal_id and row.get("terminal_id") == recipient.terminal_id]
        if len(matches) != 1:
            raise LedgerError("policy_unverifiable", "HAM status must identify exactly one recipient terminal; no prompts were submitted.")
        target = matches[0]
        if (target.get("host") != canonical
                or target.get("client") != recipient.agent_kind
                or target.get("session_ref") != (recipient.session_id or "unreported")
                or not isinstance(target.get("repo_id"), str) or not target["repo_id"]
                or "role" not in target
                or target["role"] not in (None, "main", "worker")):
            raise LedgerError("policy_unverifiable", "HAM recipient repository, participation or session could not be verified; no prompts were submitted.")
        return target
