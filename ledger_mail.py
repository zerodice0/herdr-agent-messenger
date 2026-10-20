"""Local mailbox operations: immutable messages, view-only reads and CAS claims."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import math
import os
import re
import time
from typing import Any, Mapping, Sequence
import uuid

from ledger_status import check_generation
from ledger_store import (FINAL_STATES, LedgerError, LedgerStore, json_text,
                          receipt, require_uuid, save_receipt)

MAX_BODY_BYTES = 16 * 1024
IMMUTABLE_FIELDS = ("message_id", "thread_id", "repo_id", "reply_to", "origin_host", "sender",
                    "sender_label", "kind", "hop", "body", "subject", "created_at",
                    "recipient_host", "recipient_terminal_id", "recipient_address")
REPO_FIELDS = ("sender_repo_id", "recipient_repo_id")
MAX_HISTORY_CURSOR_SIZE = 32768


def _history_scope(repo_id: str | None, limit: int) -> None:
    if type(limit) is not int or not 1 <= limit <= 1000:
        raise LedgerError("invalid_limit", "History limit must be an integer between 1 and 1000.")
    if repo_id is not None and (not isinstance(repo_id, str) or not repo_id or len(repo_id) > 4096):
        raise LedgerError("invalid_repo", "History repository must be a bounded nonempty identity.")


def _cursor_encode_part(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _cursor_decode_part(value: str) -> bytes:
    decoded = base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
    if _cursor_encode_part(decoded) != value:
        raise ValueError("Noncanonical cursor encoding.")
    return decoded


def _history_cursor(store: LedgerStore, kind: str, repo_id: str | None,
                    updated_at: float, identity: str) -> str:
    value = {"v": 1, "ledger": store.host_uuid, "kind": kind, "repo": repo_id,
             "updated_at": updated_at, "id": identity}
    payload = json_text(value).encode("utf-8")
    signature = hmac.digest(store._history_cursor_key, payload, "sha256")
    return _cursor_encode_part(payload) + "." + _cursor_encode_part(signature)


def _history_boundary(store: LedgerStore, kind: str, repo_id: str | None,
                      cursor: str | None) -> tuple[float, str] | None:
    if cursor is None:
        return None
    try:
        if (not isinstance(cursor, str) or len(cursor) > MAX_HISTORY_CURSOR_SIZE or
                re.fullmatch(r"[A-Za-z0-9_-]+\.[A-Za-z0-9_-]{43}", cursor) is None):
            raise ValueError("Malformed cursor.")
        encoded_payload, encoded_signature = cursor.split(".")
        payload = _cursor_decode_part(encoded_payload)
        signature = _cursor_decode_part(encoded_signature)
        if not hmac.compare_digest(signature, hmac.digest(store._history_cursor_key, payload, "sha256")):
            raise ValueError("Invalid cursor signature.")
        value = json.loads(payload)
        if (not isinstance(value, dict) or
                set(value) != {"v", "ledger", "kind", "repo", "updated_at", "id"} or
                type(value["v"]) is not int or value["v"] != 1 or
                value["ledger"] != store.host_uuid or value["kind"] != kind or
                value["repo"] != repo_id or
                type(value["updated_at"]) not in (int, float) or
                not math.isfinite(value["updated_at"]) or
                not isinstance(value["id"], str) or str(uuid.UUID(value["id"])) != value["id"] or
                json_text(value).encode("utf-8") != payload):
            raise ValueError("Invalid cursor scope or schema.")
        return value["updated_at"], value["id"]
    except (ValueError, TypeError, KeyError, OverflowError, RecursionError, binascii.Error):
        raise LedgerError("invalid_cursor", "History cursor is invalid for this ledger, query, or repository.") from None


def validate_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    if set(payload) not in (set(IMMUTABLE_FIELDS), set(IMMUTABLE_FIELDS + REPO_FIELDS)):
        raise LedgerError("invalid_message", "Message fields do not match a supported protocol 1 payload.")
    value = dict(payload)
    for key in ("message_id", "thread_id"):
        require_uuid(value[key], key)
    if value["reply_to"] is not None:
        require_uuid(value["reply_to"], "reply_to")
    for key in ("repo_id", "origin_host", "sender", "sender_label", "recipient_host", "recipient_address"):
        if not isinstance(value[key], str) or not value[key] or len(value[key]) > 4096:
            raise LedgerError("invalid_message", f"{key} must be a bounded nonempty string.")
    for key in REPO_FIELDS:
        if key in value and (not isinstance(value[key], str) or not value[key] or len(value[key]) > 4096):
            raise LedgerError("invalid_message", f"{key} must be a bounded nonempty string.")
    if value["recipient_terminal_id"] is not None and (
            not isinstance(value["recipient_terminal_id"], str) or not value["recipient_terminal_id"]):
        raise LedgerError("invalid_message", "recipient_terminal_id must be a terminal ID or null.")
    if not isinstance(value["kind"], str) or value["kind"] not in {"fyi", "action"}:
        raise LedgerError("invalid_kind", "Message kind must be fyi or action.")
    if not isinstance(value["hop"], int) or isinstance(value["hop"], bool) or value["hop"] < 0:
        raise LedgerError("invalid_message", "hop must be a nonnegative integer.")
    if (not isinstance(value["created_at"], (int, float)) or isinstance(value["created_at"], bool)
            or not math.isfinite(value["created_at"])):
        raise LedgerError("invalid_message", "created_at must be a finite timestamp.")
    if not isinstance(value["subject"], str) or len(value["subject"].encode('utf-8')) > 1024:
        raise LedgerError("invalid_message", "Subject must fit within 1024 UTF-8 bytes.")
    if not isinstance(value["body"], str) or not value["body"].strip():
        raise LedgerError("invalid_body", "A message body must not be empty.")
    if len(value["body"].encode('utf-8')) > MAX_BODY_BYTES:
        raise LedgerError("body_too_large", "A message body must fit within 16 KiB UTF-8.")
    return value


def payload_hash(payload: Mapping[str, Any]) -> str:
    # Hash the accepted wire shape, never a normalized/backfilled database row:
    # legacy payloads omit REPO_FIELDS and must retain their original digest.
    return hashlib.sha256(json_text(payload).encode('utf-8')).hexdigest()


class Mailbox:
    def __init__(self, store: LedgerStore, environment: Mapping[str, str] | None = None) -> None:
        self.store = store
        self.environment = environment

    def send(self, sender: Mapping[str, Any], recipient: Mapping[str, Any], *, body: str,
             kind: str = "fyi", reply_to: str | None = None, subject: str = "") -> dict[str, Any]:
        message_id = str(uuid.uuid4())
        payload = {"message_id": message_id, "thread_id": message_id, "repo_id": sender["repo_id"],
                   "sender_repo_id": sender["repo_id"], "recipient_repo_id": recipient["repo_id"],
                   "reply_to": reply_to, "origin_host": self.store.host, "sender": sender["agent_id"],
                   "sender_label": sender["alias"] or sender["terminal_id"], "kind": kind, "hop": 0,
                   "body": body, "subject": subject, "created_at": time.time(),
                   "recipient_host": recipient["host"], "recipient_terminal_id": recipient["terminal_id"],
                   "recipient_address": recipient.get("recipient_address") or
                   f"{recipient['host']}/{recipient['alias'] or recipient['terminal_id']}"}
        return self._submit_message(payload, sender=sender, recipient=recipient, auto_handle_parent=True)

    def send_user(self, repo_id: str, recipient: Mapping[str, Any], *, body: str,
                  kind: str = "fyi", reply_to: str | None = None, subject: str = "") -> dict[str, Any]:
        """Send as the operator from repo_id context, including to another repo.

        Replies keep the retained thread origin; repo_id still records this
        sender's context. User replies neither impersonate nor auto-handle agents.
        """
        if not repo_id:
            raise LedgerError("invalid_repo", "The sender repository context must be nonempty.")
        message_id = str(uuid.uuid4())
        payload = {"message_id": message_id, "thread_id": message_id, "repo_id": repo_id,
                   "sender_repo_id": repo_id, "recipient_repo_id": recipient["repo_id"],
                   "reply_to": reply_to, "origin_host": self.store.host, "sender": "user",
                   "sender_label": "user", "kind": kind, "hop": 0, "body": body, "subject": subject,
                   "created_at": time.time(), "recipient_host": recipient["host"],
                   "recipient_terminal_id": recipient["terminal_id"],
                   "recipient_address": recipient.get("recipient_address") or
                   f"{recipient['host']}/{recipient.get('alias') or recipient['terminal_id']}"}
        result = self._submit_message(payload, sender=None, recipient=recipient, auto_handle_parent=False)
        values = os.environ if self.environment is None else self.environment
        if values.get("HERDR_AGENT_LEDGER_AUTOSTART", "1") != "0":
            try:
                from ledger_worker import ensure_worker
                ensure_worker(self.store, self.environment)
            except (LedgerError, OSError) as error:
                result = {**result, "warnings": [*result.get("warnings", []),
                         {"code": "worker_start_failed", "message": str(error), "committed": True}]}
        return result

    def _submit_message(self, payload: dict[str, Any], *, sender: Mapping[str, Any] | None,
                        recipient: Mapping[str, Any], auto_handle_parent: bool) -> dict[str, Any]:
        sender_terminal = sender.get("terminal_id") if sender is not None else None
        warning_recipient = recipient
        recipient_terminal = recipient.get("terminal_id")
        terminals = tuple(terminal for terminal in (recipient_terminal, sender_terminal) if terminal)
        with self.store.transaction(terminals) as connection:
            if sender is not None:
                check_generation(connection, self.store.host, sender["terminal_id"],
                                 sender["agent_id"], sender["generation"])
            if recipient["host"] == self.store.host and recipient_terminal:
                current = check_generation(connection, self.store.host, recipient_terminal,
                                           recipient["agent_id"], recipient["generation"])
                status = connection.execute("SELECT turn_state FROM agent_status WHERE agent_id=?",
                                            (current["agent_id"],)).fetchone()
                from ledger_settings import RepoSettings
                warning_recipient = {**current, "turn_state": status[0] if status else "unknown",
                                     "role": RepoSettings(self.store).role(current, connection=connection)}
            parent = None
            reply_to = payload["reply_to"]
            if reply_to:
                require_uuid(reply_to, "reply_to")
                parent = connection.execute("SELECT m.*,d.recipient_terminal_id,d.recipient_host,d.recipient_repo_id,d.state "
                                            "FROM messages m JOIN deliveries d USING(message_id) "
                                            "WHERE message_id=?", (reply_to,)).fetchone()
                if parent is None:
                    raise LedgerError("reply_not_found", "Reply target is not a retained message.")
                if sender is not None:
                    participant = connection.execute(
                        "SELECT 1 FROM messages m JOIN deliveries d USING(message_id) "
                        "WHERE m.thread_id=? AND ((m.origin_host=? AND m.sender_repo_id=? AND "
                        "m.sender IN (SELECT agent_id FROM agents WHERE host=? AND terminal_id=?)) OR "
                        "(d.recipient_host=? AND d.recipient_terminal_id=? AND d.recipient_repo_id=?)) LIMIT 1",
                        (parent["thread_id"], self.store.host, sender["repo_id"], self.store.host,
                         sender["terminal_id"], self.store.host, sender["terminal_id"], sender["repo_id"]),
                    ).fetchone()
                    if participant is None:
                        raise LedgerError("not_participant", "Only a participant of this thread may reply.")
                payload.update(thread_id=parent["thread_id"], repo_id=parent["repo_id"], hop=parent["hop"] + 1)
                thread = connection.execute("SELECT subject FROM threads WHERE thread_id=?",
                                            (parent["thread_id"],)).fetchone()
                payload["subject"] = thread[0]
                if (sender is not None and auto_handle_parent and
                        parent["recipient_host"] == self.store.host and
                        parent["recipient_repo_id"] == sender["repo_id"] and
                        parent["recipient_terminal_id"] == sender["terminal_id"] and
                        parent["state"] == "delivering"):
                    raise LedgerError("delivery_in_progress",
                                      "Resolve an uncertain delivery before marking it handled by reply.")
            value = validate_payload(payload)
            from ledger_remote import RemoteLedger
            result = RemoteLedger(self.store, self.environment).submit(connection, value)
            if (sender is not None and auto_handle_parent and parent and
                    parent["recipient_host"] == self.store.host and
                    parent["recipient_repo_id"] == sender["repo_id"] and
                    parent["recipient_terminal_id"] == sender["terminal_id"] and
                    parent["state"] in {"queued", "delivered"}):
                self._handled(connection, reply_to, time.time())
        submission = self.store.annotate({"message_id": payload["message_id"],
                                         "thread_id": payload["thread_id"], **result})
        client = recipient.get("client")
        if client in {"omp", "claude"} and recipient.get("extension_present") in (False, 0):
            submission.setdefault("warnings", []).append({
                "code": "recipient_no_receiver", "client": client,
                "message": f"{client} recipient has no registered HAM receiver; the message remains queued. "
                           f"Run runtime status on {recipient['host']}; with user consent install the runtime "
                           "if missing, then restart the selected session."})
        from ledger_runtime import WAKE_READY_STATES
        if (payload["kind"] == "fyi" and warning_recipient.get("active") == 1 and
                not warning_recipient.get("stale") and
                warning_recipient.get("client") == "omp" and
                warning_recipient.get("extension_present") == 1 and
                warning_recipient.get("role") in {"main", "worker"} and
                warning_recipient.get("turn_state") in WAKE_READY_STATES):
            submission.setdefault("warnings", []).append({
                "code": "fyi_no_wake", "client": "omp",
                "message": "FYI does not wake an idle recipient; use --kind action if it must act now."})
        return submission

    def enqueue(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Idempotent local acceptance; remote authentication lives at the transport boundary."""
        value = validate_payload(payload)
        if value["recipient_host"] != self.store.host or not value["recipient_terminal_id"]:
            raise LedgerError("invalid_recipient", "Local acceptance requires this host and a concrete terminal.")
        with self.store.transaction((value["recipient_terminal_id"],)) as connection:
            return self._enqueue(connection, value)

    def _enqueue(self, connection: Any, value: Mapping[str, Any], *,
                 resolved: Mapping[str, Any] | None = None, upstream: str | None = None,
                 forward: str | None = None) -> dict[str, Any]:
        digest = payload_hash(value)
        existing = connection.execute("SELECT payload_hash FROM dedupe_receipts WHERE message_id=?", (value["message_id"],)).fetchone()
        if existing:
            if existing[0] != digest:
                raise LedgerError("message_conflict", "This message ID already identifies a different immutable payload.")
            return receipt(connection, value["message_id"])
        tombstone = connection.execute("SELECT origin_host FROM withdraw_tombstones WHERE message_id=?", (value["message_id"],)).fetchone()
        if tombstone and tombstone[0] != value["origin_host"]:
            raise LedgerError("message_conflict", "Withdrawal and message disagree about their origin.")
        target = dict(resolved or {key: value[key] for key in
                                  ("recipient_host", "recipient_terminal_id", "recipient_address")})
        target["recipient_repo_id"] = target.get("recipient_repo_id", value.get("recipient_repo_id", value["repo_id"]))
        authoritative = target["recipient_host"] == self.store.host and target["recipient_terminal_id"] is not None
        connection.execute("INSERT INTO message_routes VALUES (?,?,?,?,?,?,?,?,?)",
                           (value["message_id"], value["recipient_host"], value["recipient_terminal_id"],
                            value["recipient_address"], target["recipient_host"], target["recipient_terminal_id"],
                            target["recipient_address"], upstream, forward))
        thread = connection.execute("SELECT repo_id,subject FROM threads WHERE thread_id=?", (value["thread_id"],)).fetchone()
        if thread and (thread["repo_id"] != value["repo_id"] or thread["subject"] != value["subject"]):
            raise LedgerError("thread_conflict", "Thread repository and subject are immutable.")
        connection.execute("INSERT OR IGNORE INTO threads VALUES (?,?,?)", (value["thread_id"], value["repo_id"], value["subject"]))
        connection.execute(
            "INSERT INTO messages(message_id,thread_id,repo_id,sender_repo_id,reply_to,origin_host,sender,sender_label,kind,hop,body,created_at,payload_hash) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            tuple(value[key] for key in ("message_id", "thread_id", "repo_id")) +
            (value.get("sender_repo_id", value["repo_id"]),) +
            tuple(value[key] for key in ("reply_to", "origin_host", "sender", "sender_label",
                                        "kind", "hop", "body", "created_at")) + (digest,))
        slot = connection.execute("SELECT * FROM terminal_slots WHERE host=? AND terminal_id=?",
                                  (self.store.host, target["recipient_terminal_id"])).fetchone() if authoritative else None
        state, hold = ("queued" if authoritative else "pending"), None
        if tombstone:
            state = "withdrawn"
        elif authoritative and (slot is None or not slot["present"]):
            state, hold = "undeliverable", "terminal_closed"
        elif authoritative:
            target_agent = connection.execute("SELECT repo_id FROM agents WHERE host=? AND terminal_id=? AND active=1",
                                             (self.store.host, target["recipient_terminal_id"])).fetchone()
            hold = "recipient_absent" if target_agent is None else ("recipient_changed" if target_agent[0] != target["recipient_repo_id"] else None)
        at = time.time()
        connection.execute(
            "INSERT INTO deliveries(message_id,recipient_address,recipient_host,recipient_terminal_id,recipient_repo_id,workspace_id,pane_id,"
            "state,hold_reason,authoritative,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (value["message_id"], target["recipient_address"], target["recipient_host"], target["recipient_terminal_id"],
             target["recipient_repo_id"], slot["workspace_id"] if slot else '', slot["pane_id"] if slot else '', state, hold, authoritative, at))
        if state in FINAL_STATES:
            connection.execute("UPDATE messages SET final_at=? WHERE message_id=?", (at, value["message_id"]))
        return save_receipt(connection, value["message_id"])

    def read(self, agent: Mapping[str, Any], *, message_id: str | None = None,
             all_states: bool = False, limit: int = 100) -> dict[str, Any]:
        if not 1 <= limit <= 1000:
            raise LedgerError("invalid_limit", "Inbox limit must be between 1 and 1000.")
        if message_id:
            require_uuid(message_id)
        with self.store.read() as connection:
            check_generation(connection, self.store.host, agent["terminal_id"], agent["agent_id"], agent["generation"])
            query = ("SELECT m.*,d.* FROM messages m JOIN deliveries d USING(message_id) "
                     "WHERE d.recipient_host=? AND d.recipient_terminal_id=? AND d.recipient_repo_id=?")
            parameters: list[Any] = [self.store.host, agent["terminal_id"], agent["repo_id"]]
            if message_id:
                query += " AND m.message_id=?"
                parameters.append(message_id)
            elif not all_states:
                query += " AND d.state IN ('queued','delivering','delivered')"
            rows = connection.execute(query + " ORDER BY m.created_at,m.message_id LIMIT ?", (*parameters, limit)).fetchall()
            messages = [dict(row) for row in rows]
        return {"terminal_id": agent["terminal_id"], "messages": messages}

    def show_thread(self, thread_id: str) -> dict[str, Any]:
        require_uuid(thread_id, "thread_id")
        with self.store.read() as connection:
            thread = connection.execute("SELECT * FROM threads WHERE thread_id=?", (thread_id,)).fetchone()
            if thread is None:
                raise LedgerError("thread_not_found", "No retained thread with that ID exists.")
            rows = connection.execute("SELECT m.*,d.* FROM messages m JOIN deliveries d USING(message_id) "
                                      "WHERE thread_id=? ORDER BY m.created_at,m.message_id", (thread_id,)).fetchall()
        return {**dict(thread), "messages": [dict(row) for row in rows]}
    def list_threads(self, repo_id: str | None = None, limit: int = 100, *,
                     cursor: str | None = None) -> dict[str, Any]:
        """Live keyset pages, not a retained historical snapshot.

        Updates between calls can move rows across the cursor, skipping or
        repeating changed rows. Deletions disappear; new rows after the boundary
        can appear. An unchanged dataset has no omissions or duplicates.
        """
        _history_scope(repo_id, limit)
        boundary = _history_boundary(self.store, "threads", repo_id, cursor)
        with self.store.read() as connection:
            # Keep the selected thread summaries and their messages consistent
            # within this call, without holding a transaction between pages.
            connection.execute("BEGIN")
            query = ("SELECT t.thread_id,t.repo_id,t.subject,max(d.updated_at) AS updated_at "
                     "FROM threads t JOIN messages m USING(thread_id) "
                     "JOIN deliveries d USING(message_id)")
            parameters: list[Any] = []
            if repo_id is not None:
                query += (" WHERE (t.repo_id=? OR EXISTS (SELECT 1 FROM messages pm "
                          "JOIN deliveries pd USING(message_id) WHERE pm.thread_id=t.thread_id "
                          "AND (pm.sender_repo_id=? OR pd.recipient_repo_id=?)))")
                parameters.extend((repo_id, repo_id, repo_id))
            query += " GROUP BY t.thread_id,t.repo_id,t.subject"
            if boundary is not None:
                query += " HAVING (max(d.updated_at)<? OR (max(d.updated_at)=? AND t.thread_id>?))"
                parameters.extend((boundary[0], boundary[0], boundary[1]))
            rows = connection.execute(query + " ORDER BY updated_at DESC,t.thread_id LIMIT ?",
                                      (*parameters, limit + 1)).fetchall()
            more = len(rows) > limit
            rows = rows[:limit]
            if not rows:
                return {"repo_id": repo_id, "threads": [], "next_cursor": None}
            thread_ids = [row["thread_id"] for row in rows]
            placeholders = ",".join("?" for _ in thread_ids)
            messages = connection.execute(
                f"SELECT m.*,d.* FROM messages m JOIN deliveries d USING(message_id) "
                f"WHERE m.thread_id IN ({placeholders}) ORDER BY m.created_at,m.message_id",
                thread_ids,
            ).fetchall()
        grouped = {thread_id: [] for thread_id in thread_ids}
        for row in messages:
            grouped[row["thread_id"]].append(dict(row))
        next_cursor = (_history_cursor(self.store, "threads", repo_id, rows[-1]["updated_at"],
                                       rows[-1]["thread_id"]) if more else None)
        return {"repo_id": repo_id,
                "threads": [{**dict(row), "messages": grouped[row["thread_id"]]} for row in rows],
                "next_cursor": next_cursor}

    @staticmethod
    def _submission_record(row: Any) -> dict[str, Any]:
        record = dict(row)
        via = record["via"]
        sender: Any = record["sender"]
        if via == "spawn_initial":
            sender = json.loads(sender)
        acceptance = record["acceptance"]
        return {
            "submission_id": record["submission_id"],
            "operation_id": record["operation_id"],
            "repo_id": record["repo_id"],
            "via": via,
            "acceptance": acceptance,
            "sender": sender,
            "sender_kind": record["sender_kind"],
            "target": json.loads(record["target"]),
            "body": record["body"],
            "subject": record["subject"],
            "body_hash": record["body_sha256"],
            "readiness_evidence": record["readiness_evidence"],
            "evidence": record["evidence"],
            "created_at": record["created_at"],
            "updated_at": record["updated_at"],
        }

    def begin_direct(self, repo_id: str, recipient: Mapping[str, Any], *,
                     body: str, subject: str = "") -> dict[str, Any]:
        if not isinstance(repo_id, str) or not repo_id:
            raise LedgerError("invalid_repo", "Direct submission needs a repository identity.")
        if not isinstance(recipient, Mapping):
            raise LedgerError("invalid_target", "Direct submission target must be an object.")
        if not isinstance(body, str) or not body.strip():
            raise LedgerError("invalid_body", "A direct submission body must not be empty.")
        if not isinstance(subject, str) or len(subject.encode("utf-8")) > 1024:
            raise LedgerError("invalid_subject", "Direct subject must fit within 1024 UTF-8 bytes.")
        target = dict(recipient)
        created = time.time()
        submission_id = str(uuid.uuid4())
        digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
        with self.store.transaction() as connection:
            connection.execute(
                "INSERT INTO submissions(submission_id,operation_id,repo_id,via,acceptance,sender,sender_kind,"
                "target,body,subject,body_sha256,created_at,updated_at) "
                "VALUES (?,NULL,?,'direct','submitting','user','user',?,?,?,?,?,?)",
                (submission_id, repo_id, json_text(target), body, subject, digest, created, created),
            )
            row = connection.execute("SELECT * FROM submissions WHERE submission_id=?",
                                     (submission_id,)).fetchone()
        return self._submission_record(row)

    def finish_direct(self, submission_id: str, *, acceptance: str, evidence: str) -> dict[str, Any]:
        require_uuid(submission_id, "submission_id")
        if acceptance not in {"accepted", "unknown", "failed"}:
            raise LedgerError("invalid_acceptance", "Direct acceptance must be accepted, unknown, or failed.")
        if not isinstance(evidence, str):
            raise LedgerError("invalid_evidence", "Direct submission evidence must be text.")
        with self.store.transaction() as connection:
            row = connection.execute("SELECT * FROM submissions WHERE submission_id=? AND via='direct'",
                                     (submission_id,)).fetchone()
            if row is None:
                raise LedgerError("submission_not_found", "No direct submission with that ID exists.")
            finished = self._finish_submission(connection, row, acceptance, evidence)
        return self._submission_record(finished)

    def list_direct(self, repo_id: str | None = None, limit: int = 100, *,
                    cursor: str | None = None) -> dict[str, Any]:
        """Live keyset pages, not a snapshot: concurrent updates can move rows
        across the cursor and skip or repeat changed rows. Restart to refresh.
        """
        _history_scope(repo_id, limit)
        boundary = _history_boundary(self.store, "direct", repo_id, cursor)
        with self.store.read() as connection:
            query = "SELECT * FROM submissions WHERE via='direct'"
            parameters: list[Any] = []
            if repo_id is not None:
                query += " AND repo_id=?"
                parameters.append(repo_id)
            if boundary is not None:
                query += " AND (updated_at<? OR (updated_at=? AND submission_id>?))"
                parameters.extend((boundary[0], boundary[0], boundary[1]))
            rows = connection.execute(query + " ORDER BY updated_at DESC,submission_id LIMIT ?",
                                      (*parameters, limit + 1)).fetchall()
        more = len(rows) > limit
        rows = rows[:limit]
        next_cursor = (_history_cursor(self.store, "direct", repo_id, rows[-1]["updated_at"],
                                       rows[-1]["submission_id"]) if more else None)
        return {"repo_id": repo_id, "submissions": [self._submission_record(row) for row in rows],
                "next_cursor": next_cursor}

    @staticmethod
    def _finish_submission(connection: Any, row: Any, acceptance: str, evidence: str) -> Any:
        if row["acceptance"] == "accepted" or row["acceptance"] == acceptance:
            return row
        connection.execute("UPDATE submissions SET acceptance=?,evidence=?,updated_at=? WHERE submission_id=? "
                           "AND acceptance<>'accepted'",
                           (acceptance, evidence, time.time(), row["submission_id"]))
        return connection.execute("SELECT * FROM submissions WHERE submission_id=?",
                                  (row["submission_id"],)).fetchone()

    def claim_spawn_submission(self, operation_id: str, *, target: Mapping[str, Any], body: str,
                               sender: Mapping[str, Any], readiness_evidence: str) -> dict[str, Any]:
        if not isinstance(operation_id, str) or not operation_id or len(operation_id) > 4096:
            raise LedgerError("invalid_operation", "Spawn operation ID must be a bounded nonempty string.")
        if not isinstance(target, Mapping) or not isinstance(sender, Mapping):
            raise LedgerError("invalid_submission", "Spawn target and sender must be objects.")
        target_value = dict(target)
        required_target = {"transport_host", "host", "local", "terminal_id", "session_ref",
                           "client", "pane_id", "address", "workspace_id", "tab_id", "created"}
        if required_target - target_value.keys():
            raise LedgerError("invalid_target", "Spawn target is missing a bound Herdr identity field.")
        if not isinstance(body, str) or not body.strip():
            raise LedgerError("invalid_body", "Spawn prompt body must not be empty.")
        if not isinstance(readiness_evidence, str) or not readiness_evidence.strip():
            raise LedgerError("missing_evidence", "Actual Herdr readiness evidence is required.")
        sender_value = dict(sender)
        sender_kind = sender_value.get("kind")
        if sender_kind == "agent":
            required_sender = ("host", "terminal_id", "session_ref", "client", "agent_id")
            if any(not isinstance(sender_value.get(key), str) or not sender_value[key]
                   for key in required_sender):
                raise LedgerError("invalid_sender", "Agent spawn initiator must include its verified identity.")
        elif sender_kind in {"unknown", "non-agent"}:
            pass
        else:
            raise LedgerError("invalid_sender", "Spawn initiator kind must be agent, non-agent, or unknown.")
        target_text = json_text(target_value)
        sender_text = json_text(sender_value)
        digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
        repo_id = sender_value.get("repo_id") or target_value.get("repo_id")
        with self.store.transaction() as connection:
            existing = connection.execute("SELECT * FROM submissions WHERE operation_id=?",
                                          (operation_id,)).fetchone()
            if existing is not None:
                if (existing["via"] != "spawn_initial" or existing["target"] != target_text or
                        existing["body_sha256"] != digest or existing["sender"] != sender_text):
                    raise LedgerError("submission_conflict",
                                      "This spawn operation ID is already bound to different immutable inputs.")
                return {"submission_id": existing["submission_id"], "should_submit": False,
                        "record": self._submission_record(existing)}
            submission_id = str(uuid.uuid4())
            created = time.time()
            connection.execute(
                "INSERT INTO submissions(submission_id,operation_id,repo_id,via,acceptance,sender,sender_kind,"
                "target,body,subject,body_sha256,readiness_evidence,created_at,updated_at) "
                "VALUES (?,?,?,'spawn_initial','submitting',?,?,?,?,'',?,?,?,?)",
                (submission_id, operation_id, repo_id, sender_text, sender_kind, target_text, body,
                 digest, readiness_evidence, created, created),
            )
            row = connection.execute("SELECT * FROM submissions WHERE submission_id=?",
                                     (submission_id,)).fetchone()
        return {"submission_id": submission_id, "should_submit": True,
                "record": self._submission_record(row)}

    def finish_spawn_submission(self, operation_id: str, *, acceptance: str,
                                evidence: str) -> dict[str, Any]:
        if not isinstance(operation_id, str) or not operation_id:
            raise LedgerError("invalid_operation", "Spawn operation ID must be nonempty.")
        if acceptance not in {"accepted", "unknown", "failed"}:
            raise LedgerError("invalid_acceptance", "Spawn acceptance must be accepted, unknown, or failed.")
        if not isinstance(evidence, str):
            raise LedgerError("invalid_evidence", "Spawn submission evidence must be text.")
        with self.store.transaction() as connection:
            row = connection.execute("SELECT * FROM submissions WHERE operation_id=? AND via='spawn_initial'",
                                     (operation_id,)).fetchone()
            if row is None:
                raise LedgerError("submission_not_found", "No initial spawn submission with that operation ID exists.")
            finished = self._finish_submission(connection, row, acceptance, evidence)
        return self._submission_record(finished)

    def get_spawn_submission(self, operation_id: str) -> dict[str, Any]:
        if not isinstance(operation_id, str) or not operation_id:
            raise LedgerError("invalid_operation", "Spawn operation ID must be nonempty.")
        with self.store.read() as connection:
            row = connection.execute("SELECT * FROM submissions WHERE operation_id=? AND via='spawn_initial'",
                                     (operation_id,)).fetchone()
        if row is None:
            raise LedgerError("submission_not_found", "No initial spawn submission with that operation ID exists.")
        return self._submission_record(row)

    def handle(self, agent: Mapping[str, Any], message_ids: Sequence[str]) -> dict[str, Any]:
        if not message_ids:
            raise LedgerError("invalid_id", "At least one message ID is required.")
        for message_id in message_ids:
            require_uuid(message_id)
        with self.store.transaction((agent["terminal_id"],)) as connection:
            check_generation(connection, self.store.host, agent["terminal_id"], agent["agent_id"], agent["generation"])
            rows = []
            for message_id in dict.fromkeys(message_ids):
                row = connection.execute("SELECT * FROM deliveries WHERE message_id=?", (message_id,)).fetchone()
                if row is None or row["recipient_host"] != self.store.host or row["recipient_terminal_id"] != agent["terminal_id"] or row["recipient_repo_id"] != agent["repo_id"]:
                    raise LedgerError("message_not_found", "Message is not in this recipient's repository inbox.")
                if row["state"] not in {"queued", "delivered", "handled"}:
                    raise LedgerError("delivery_in_progress" if row["state"] == "delivering" else "message_final", "This delivery cannot be marked handled.")
                rows.append(row)
            at = time.time()
            results = [self._handled(connection, row["message_id"], at) if row["state"] != "handled"
                       else receipt(connection, row["message_id"]) for row in rows]
        return {"receipts": results}

    @staticmethod
    def _handled(connection: Any, message_id: str, at: float) -> dict[str, Any]:
        connection.execute("UPDATE deliveries SET state='handled',handled_at=?,updated_at=?,hold_reason=NULL "
                           "WHERE message_id=?", (at, at, message_id))
        connection.execute("UPDATE messages SET final_at=? WHERE message_id=?", (at, message_id))
        return save_receipt(connection, message_id)

    def withdraw(self, message_id: str, *, sender: Mapping[str, Any] | None = None,
                 origin_host: str | None = None) -> dict[str, Any]:
        require_uuid(message_id)
        origin = origin_host or self.store.host
        terminals: list[str] = []
        with self.store.transaction(terminals) as connection:
            result = self._withdraw(connection, message_id, origin_host=origin, sender=sender)
            if result.get("recipient_terminal_id"):
                terminals.append(result["recipient_terminal_id"])
        return result

    def _withdraw(self, connection: Any, message_id: str, *, origin_host: str,
                  sender: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """Arbitrate withdrawal inside the caller's serialized write transaction."""
        row = connection.execute("SELECT m.origin_host,m.sender,d.* FROM messages m JOIN deliveries d USING(message_id) "
                                 "WHERE message_id=?", (message_id,)).fetchone()
        if row is not None:
            if row["origin_host"] != origin_host:
                raise LedgerError("wrong_origin", "Only the original host can withdraw this message.")
            if sender is not None:
                check_generation(connection, self.store.host, sender["terminal_id"], sender["agent_id"], sender["generation"])
                owner = connection.execute("SELECT terminal_id,repo_id FROM agents WHERE agent_id=?", (row["sender"],)).fetchone()
                if owner is None or owner["terminal_id"] != sender["terminal_id"] or owner["repo_id"] != sender["repo_id"]:
                    raise LedgerError("not_sender", "Only the originating terminal can withdraw this message.")
            if not row["authoritative"]:
                from ledger_remote import RemoteLedger
                return RemoteLedger(self.store).withdraw_copy(connection, message_id)
            if row["state"] not in {"queued", "withdrawn"}:
                return {**receipt(connection, message_id), "withdrawn": False, "reason": "already_claimed_or_final"}
            if row["state"] == "withdrawn":
                return {**receipt(connection, message_id), "withdrawn": True}
            at = time.time()
            connection.execute("UPDATE deliveries SET state='withdrawn',hold_reason=NULL,updated_at=? "
                               "WHERE message_id=? AND state='queued'", (at, message_id))
            connection.execute("UPDATE messages SET final_at=? WHERE message_id=?", (at, message_id))
            result = save_receipt(connection, message_id)
        else:
            with_retention = connection.execute("SELECT origin_host FROM dedupe_receipts WHERE message_id=?", (message_id,)).fetchone()
            if with_retention:
                if with_retention[0] != origin_host:
                    raise LedgerError("wrong_origin", "Only the original host can withdraw this message.")
                final = receipt(connection, message_id)
                if final["state"] == "withdrawn":
                    return {**final, "withdrawn": True}
                return {**final, "withdrawn": False, "reason": "retained_final"}
            tombstone = connection.execute("SELECT origin_host FROM withdraw_tombstones WHERE message_id=?",
                                           (message_id,)).fetchone()
            if tombstone and tombstone[0] != origin_host:
                raise LedgerError("wrong_origin", "Withdrawal origin does not match the existing tombstone.")
            result = {"message_id": message_id, "state": "withdrawn", "origin_host": origin_host}
        connection.execute("INSERT OR IGNORE INTO withdraw_tombstones VALUES (?,?,?)", (message_id, origin_host, time.time()))
        return {**result, "withdrawn": True}

    def claim(self, *, terminal_id: str, agent_id: str, generation: int,
              kinds: Sequence[str] = ("fyi", "action"), via: str = "before_agent_start") -> dict[str, Any]:
        if not kinds or any(kind not in {"fyi", "action"} for kind in kinds):
            raise LedgerError("invalid_kind", "Claim kinds must contain fyi and/or action.")
        if via not in {"wake", "before_agent_start", "hook"}:
            raise LedgerError("invalid_via", "Only runtime adapters can claim native context delivery.")
        with self.store.transaction((terminal_id,)) as connection:
            agent = check_generation(connection, self.store.host, terminal_id, agent_id, generation)
            placeholders = ','.join('?' for _ in kinds)
            rows = connection.execute(
                "SELECT m.*,d.recipient_address,d.recipient_repo_id FROM messages m JOIN deliveries d USING(message_id) WHERE "
                "d.recipient_host=? AND d.recipient_terminal_id=? AND d.state='queued' AND d.recipient_repo_id=? "
                f"AND m.kind IN ({placeholders}) ORDER BY m.created_at,m.message_id LIMIT 100",
                (self.store.host, terminal_id, agent["repo_id"], *kinds)).fetchall()
            result = self._claim_messages(connection, rows, agent_id, generation, via)
        return result

    @staticmethod
    def _claim_messages(connection: Any, rows: Sequence[Any], agent_id: str,
                        generation: int, via: str) -> dict[str, Any]:
        """Shared atomic claim writer; caller owns selection/generation transaction."""
        claim_id = str(uuid.uuid4())
        for row in rows:
            changed = connection.execute(
                "UPDATE deliveries SET state='delivering',claim_id=?,claimed_agent_id=?,claimed_generation=?,"
                "via=?,hold_reason='acceptance_unknown',confirmation_source=NULL,evidence=NULL,updated_at=? "
                "WHERE message_id=? AND state='queued'",
                (claim_id, agent_id, generation, via, time.time(), row["message_id"])).rowcount
            if changed != 1:
                raise LedgerError("claim_conflict", "Delivery state changed during claim.")
        return {"claim_id": claim_id if rows else None, "agent_id": agent_id, "generation": generation,
                "messages": [dict(row) for row in rows]}

    def started(self, *, claim_id: str, agent_id: str, generation: int, evidence: str) -> dict[str, Any]:
        require_uuid(claim_id, "claim_id")
        if not isinstance(evidence, str) or not evidence.strip():
            raise LedgerError("missing_evidence", "Actual context application evidence is required.")
        terminals: list[str] = []
        with self.store.transaction(terminals) as connection:
            rows = self._claim_rows(connection, claim_id, agent_id, generation)
            for row in rows:
                check_generation(connection, self.store.host, row["recipient_terminal_id"], agent_id, generation)
            at = time.time()
            result = []
            for row in rows:
                terminals.append(row["recipient_terminal_id"])
                connection.execute("UPDATE deliveries SET state='delivered',delivered_at=?,updated_at=?,hold_reason=NULL,"
                                   "delivered_agent_id=?,evidence=?,confirmation_source='native' "
                                   "WHERE message_id=? AND state='delivering'",
                                   (at, at, agent_id, evidence, row["message_id"]))
                result.append(save_receipt(connection, row["message_id"]))
            if rows and rows[0]["via"] == "wake":
                connection.execute("INSERT INTO wake_log(agent_id,at,message_ids) VALUES (?,?,?)",
                                   (agent_id, at, json_text([row["message_id"] for row in rows])))
        return {"receipts": result}

    def release(self, *, claim_id: str, agent_id: str, generation: int,
                disposition: str, evidence: str) -> dict[str, int]:
        require_uuid(claim_id, "claim_id")
        if disposition not in {"not_applied", "unknown"} or not isinstance(evidence, str) or not evidence.strip():
            raise LedgerError("invalid_disposition", "Release needs not_applied/unknown and explicit evidence.")
        terminals: list[str] = []
        with self.store.transaction(terminals) as connection:
            rows = self._claim_rows(connection, claim_id, agent_id, generation)
            for row in rows:
                terminals.append(row["recipient_terminal_id"])
                if disposition == "not_applied":
                    connection.execute("UPDATE deliveries SET state='queued',claim_id=NULL,claimed_agent_id=NULL,"
                                       "claimed_generation=NULL,via=NULL,hold_reason=NULL,evidence=?,updated_at=? "
                                       "WHERE message_id=? AND state='delivering'", (evidence, time.time(), row["message_id"]))
                else:
                    connection.execute("UPDATE deliveries SET hold_reason='acceptance_unknown',evidence=? WHERE message_id=?",
                                       (evidence, row["message_id"]))
        return {"released": len(rows) if disposition == "not_applied" else 0,
                "uncertain": len(rows) if disposition == "unknown" else 0}
    def resolve_claim(self, *, claim_id: str, agent_id: str, generation: int,
                      disposition: str, evidence: str) -> dict[str, Any]:
        require_uuid(claim_id, "claim_id")
        if disposition not in {"applied", "not_applied"}:
            raise LedgerError("invalid_disposition", "Operator resolution must be applied or not_applied.")
        if not isinstance(evidence, str) or not evidence.strip():
            raise LedgerError("missing_evidence", "Operator resolution needs explicit nonempty evidence.")
        with self.store.transaction() as connection:
            prior = connection.execute("SELECT * FROM claim_resolutions WHERE claim_id=? ORDER BY seq",
                                       (claim_id,)).fetchall()
            if prior:
                if any(row["agent_id"] != agent_id or row["generation"] != generation or
                       row["disposition"] != disposition or row["evidence"] != evidence for row in prior):
                    raise LedgerError("claim_resolution_conflict", "This claim already has a different operator resolution.")
                return {"receipts": [receipt(connection, row["message_id"]) for row in prior]}
            rows = connection.execute(
                "SELECT d.* FROM deliveries d WHERE d.claim_id=? ORDER BY d.message_id", (claim_id,),
            ).fetchall()
            if not rows:
                raise LedgerError("claim_not_found", "Claim is absent or was already released.")
            if any(row["claimed_agent_id"] != agent_id or row["claimed_generation"] != generation or
                   row["state"] != "delivering" for row in rows):
                raise LedgerError("stale_claim", "Claim agent/generation/state does not match the pending claim.")
            at = time.time()
            durable_evidence = f"operator:{disposition}:{evidence}"
            result = []
            for row in rows:
                connection.execute(
                    "INSERT INTO claim_resolutions(claim_id,message_id,agent_id,generation,disposition,evidence,resolved_at) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (claim_id, row["message_id"], agent_id, generation, disposition, evidence, at),
                )
                if disposition == "applied":
                    connection.execute(
                        "UPDATE deliveries SET state='delivered',delivered_at=?,updated_at=?,hold_reason=NULL,"
                        "delivered_agent_id=?,evidence=?,confirmation_source='operator' "
                        "WHERE message_id=? AND state='delivering'",
                        (at, at, row["claimed_agent_id"], durable_evidence, row["message_id"]),
                    )
                else:
                    slot = connection.execute(
                        "SELECT * FROM terminal_slots WHERE host=? AND terminal_id=?",
                        (row["recipient_host"], row["recipient_terminal_id"]),
                    ).fetchone()
                    if slot is None or not slot["present"]:
                        state, hold = "undeliverable", "terminal_closed"
                    else:
                        occupant = connection.execute(
                            "SELECT repo_id FROM agents WHERE host=? AND terminal_id=? AND active=1",
                            (row["recipient_host"], row["recipient_terminal_id"]),
                        ).fetchone()
                        state = "queued"
                        hold = ("recipient_absent" if occupant is None else
                                "recipient_changed" if occupant["repo_id"] != row["recipient_repo_id"] else None)
                    connection.execute(
                        "UPDATE deliveries SET state=?,claim_id=NULL,claimed_agent_id=NULL,claimed_generation=NULL,"
                        "via=NULL,hold_reason=?,workspace_id=?,pane_id=?,evidence=?,"
                        "confirmation_source='operator',updated_at=? "
                        "WHERE message_id=? AND state='delivering'",
                        (state, hold, slot["workspace_id"] if slot else "",
                         slot["pane_id"] if slot else "", durable_evidence, at, row["message_id"]),
                    )
                    if state == "undeliverable":
                        connection.execute("UPDATE messages SET final_at=coalesce(final_at,?) WHERE message_id=?",
                                           (at, row["message_id"]))
                result.append(save_receipt(connection, row["message_id"]))
        return {"receipts": result}


    @staticmethod
    def _claim_rows(connection: Any, claim_id: str, agent_id: str, generation: int) -> list[Any]:
        all_rows = connection.execute("SELECT * FROM deliveries WHERE claim_id=?", (claim_id,)).fetchall()
        if not all_rows:
            raise LedgerError("claim_not_found", "Claim is absent or was already released.")
        if any(row["claimed_agent_id"] != agent_id or row["claimed_generation"] != generation for row in all_rows):
            raise LedgerError("stale_claim", "Claim agent/generation does not match.")
        # Exact repeated acknowledgement is harmless; final messages never regress.
        if any(row["state"] not in {"delivering", "delivered", "handled"} for row in all_rows):
            raise LedgerError("stale_claim", "Claim is no longer applicable.")
        return [row for row in all_rows if row["state"] == "delivering"]
