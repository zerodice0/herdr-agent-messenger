"""Local mailbox operations: immutable messages, view-only reads and CAS claims."""

from __future__ import annotations

import hashlib
import math
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


def validate_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    if set(payload) != set(IMMUTABLE_FIELDS):
        raise LedgerError("invalid_message", "Message fields do not match protocol 1.")
    value = dict(payload)
    for key in ("message_id", "thread_id"):
        require_uuid(value[key], key)
    if value["reply_to"] is not None:
        require_uuid(value["reply_to"], "reply_to")
    for key in ("repo_id", "origin_host", "sender", "sender_label", "recipient_host", "recipient_address"):
        if not isinstance(value[key], str) or not value[key] or len(value[key]) > 4096:
            raise LedgerError("invalid_message", f"{key} must be a bounded nonempty string.")
    if value["recipient_terminal_id"] is not None and (
            not isinstance(value["recipient_terminal_id"], str) or not value["recipient_terminal_id"]):
        raise LedgerError("invalid_message", "recipient_terminal_id must be a terminal ID or null.")
    if value["kind"] not in {"fyi", "action"}:
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
    return hashlib.sha256(json_text(payload).encode('utf-8')).hexdigest()


class Mailbox:
    def __init__(self, store: LedgerStore) -> None:
        self.store = store

    def send(self, sender: Mapping[str, Any], recipient: Mapping[str, Any], *, body: str,
             kind: str = "fyi", reply_to: str | None = None, subject: str = "") -> dict[str, Any]:
        if sender["repo_id"] != recipient["repo_id"]:
            raise LedgerError("repo_mismatch", "Mailbox recipients must occupy the sender's repository identity.")
        message_id = str(uuid.uuid4())
        payload = {"message_id": message_id, "thread_id": message_id, "repo_id": sender["repo_id"],
                   "reply_to": reply_to, "origin_host": self.store.host, "sender": sender["agent_id"],
                   "sender_label": sender["alias"] or sender["terminal_id"], "kind": kind, "hop": 0,
                   "body": body, "subject": subject, "created_at": time.time(),
                   "recipient_host": recipient["host"], "recipient_terminal_id": recipient["terminal_id"],
                   "recipient_address": f"{recipient['host']}/{recipient['alias'] or recipient['terminal_id']}"}
        with self.store.transaction((recipient["terminal_id"], sender["terminal_id"])) as connection:
            check_generation(connection, self.store.host, sender["terminal_id"], sender["agent_id"], sender["generation"])
            check_generation(connection, self.store.host, recipient["terminal_id"], recipient["agent_id"], recipient["generation"])
            parent = None
            if reply_to:
                require_uuid(reply_to, "reply_to")
                parent = connection.execute("SELECT m.*,d.recipient_terminal_id,d.state FROM messages m "
                                            "JOIN deliveries d USING(message_id) WHERE message_id=?", (reply_to,)).fetchone()
                if parent is None or parent["repo_id"] != sender["repo_id"]:
                    raise LedgerError("reply_not_found", "Reply target is not a retained message in this repository.")
                payload.update(thread_id=parent["thread_id"], hop=parent["hop"] + 1)
                thread = connection.execute("SELECT subject FROM threads WHERE thread_id=?", (parent["thread_id"],)).fetchone()
                payload["subject"] = thread[0]
                if parent["recipient_terminal_id"] == sender["terminal_id"] and parent["state"] == "delivering":
                    raise LedgerError("delivery_in_progress", "Resolve an uncertain delivery before marking it handled by reply.")
            value = validate_payload(payload)
            result = self._enqueue(connection, value)
            if parent and parent["recipient_terminal_id"] == sender["terminal_id"] and parent["state"] in {"queued", "delivered"}:
                self._handled(connection, reply_to, time.time())
        return {"message_id": message_id, "thread_id": payload["thread_id"], **result}

    def enqueue(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Idempotent local acceptance; remote authentication lives at the transport boundary."""
        value = validate_payload(payload)
        if value["recipient_host"] != self.store.host or not value["recipient_terminal_id"]:
            raise LedgerError("invalid_recipient", "Local acceptance requires this host and a concrete terminal.")
        with self.store.transaction((value["recipient_terminal_id"],)) as connection:
            return self._enqueue(connection, value)

    def _enqueue(self, connection: Any, value: Mapping[str, Any]) -> dict[str, Any]:
        digest = payload_hash(value)
        existing = connection.execute("SELECT payload_hash FROM dedupe_receipts WHERE message_id=?", (value["message_id"],)).fetchone()
        if existing:
            if existing[0] != digest:
                raise LedgerError("message_conflict", "This message ID already identifies a different immutable payload.")
            return receipt(connection, value["message_id"])
        tombstone = connection.execute("SELECT origin_host FROM withdraw_tombstones WHERE message_id=?", (value["message_id"],)).fetchone()
        if tombstone and tombstone[0] != value["origin_host"]:
            raise LedgerError("message_conflict", "Withdrawal and message disagree about their origin.")
        thread = connection.execute("SELECT repo_id,subject FROM threads WHERE thread_id=?", (value["thread_id"],)).fetchone()
        if thread and (thread["repo_id"] != value["repo_id"] or thread["subject"] != value["subject"]):
            raise LedgerError("thread_conflict", "Thread repository and subject are immutable.")
        connection.execute("INSERT OR IGNORE INTO threads VALUES (?,?,?)", (value["thread_id"], value["repo_id"], value["subject"]))
        connection.execute(
            "INSERT INTO messages(message_id,thread_id,repo_id,reply_to,origin_host,sender,sender_label,kind,hop,body,created_at,payload_hash) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            tuple(value[key] for key in ("message_id", "thread_id", "repo_id", "reply_to", "origin_host", "sender", "sender_label",
                                        "kind", "hop", "body", "created_at")) + (digest,))
        slot = connection.execute("SELECT * FROM terminal_slots WHERE host=? AND terminal_id=?",
                                  (self.store.host, value["recipient_terminal_id"])).fetchone()
        state, hold = "queued", None
        if tombstone:
            state = "withdrawn"
        elif slot is None or not slot["present"]:
            state, hold = "undeliverable", "terminal_closed"
        else:
            target = connection.execute("SELECT repo_id FROM agents WHERE host=? AND terminal_id=? AND active=1",
                                        (self.store.host, value["recipient_terminal_id"])).fetchone()
            hold = "recipient_absent" if target is None else ("recipient_changed" if target[0] != value["repo_id"] else None)
        at = time.time()
        connection.execute(
            "INSERT INTO deliveries(message_id,recipient_address,recipient_host,recipient_terminal_id,workspace_id,pane_id,"
            "state,hold_reason,authoritative,updated_at) VALUES (?,?,?,?,?,?,?,?,1,?)",
            (value["message_id"], value["recipient_address"], self.store.host, value["recipient_terminal_id"],
             slot["workspace_id"] if slot else '', slot["pane_id"] if slot else '', state, hold, at))
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
                     "WHERE d.recipient_host=? AND d.recipient_terminal_id=? AND m.repo_id=?")
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

    def handle(self, agent: Mapping[str, Any], message_ids: Sequence[str]) -> dict[str, Any]:
        if not message_ids:
            raise LedgerError("invalid_id", "At least one message ID is required.")
        for message_id in message_ids:
            require_uuid(message_id)
        with self.store.transaction((agent["terminal_id"],)) as connection:
            check_generation(connection, self.store.host, agent["terminal_id"], agent["agent_id"], agent["generation"])
            rows = []
            for message_id in dict.fromkeys(message_ids):
                row = connection.execute("SELECT d.*,m.repo_id FROM deliveries d JOIN messages m USING(message_id) "
                                         "WHERE message_id=?", (message_id,)).fetchone()
                if row is None or row["recipient_host"] != self.store.host or row["recipient_terminal_id"] != agent["terminal_id"] or row["repo_id"] != agent["repo_id"]:
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
            row = connection.execute("SELECT m.origin_host,m.sender,d.* FROM messages m JOIN deliveries d USING(message_id) "
                                     "WHERE message_id=?", (message_id,)).fetchone()
            if row is not None:
                if row["origin_host"] != origin:
                    raise LedgerError("wrong_origin", "Only the original host can withdraw this message.")
                if sender is not None:
                    check_generation(connection, self.store.host, sender["terminal_id"], sender["agent_id"], sender["generation"])
                    owner = connection.execute("SELECT terminal_id,repo_id FROM agents WHERE agent_id=?", (row["sender"],)).fetchone()
                    if owner is None or owner["terminal_id"] != sender["terminal_id"] or owner["repo_id"] != sender["repo_id"]:
                        raise LedgerError("not_sender", "Only the originating terminal can withdraw this message.")
                terminals.append(row["recipient_terminal_id"])
                if row["state"] not in {"queued", "withdrawn"}:
                    return {**receipt(connection, message_id), "withdrawn": False, "reason": "already_claimed_or_final"}
                at = time.time()
                connection.execute("UPDATE deliveries SET state='withdrawn',hold_reason=NULL,updated_at=? "
                                   "WHERE message_id=? AND state='queued'", (at, message_id))
                connection.execute("UPDATE messages SET final_at=? WHERE message_id=?", (at, message_id))
                result = save_receipt(connection, message_id)
            else:
                with_retention = connection.execute("SELECT origin_host FROM dedupe_receipts WHERE message_id=?", (message_id,)).fetchone()
                if with_retention:
                    if with_retention[0] != origin:
                        raise LedgerError("wrong_origin", "Only the original host can withdraw this message.")
                    return {**receipt(connection, message_id), "withdrawn": False, "reason": "retained_final"}
                result = {"message_id": message_id, "state": "withdrawn", "origin_host": origin}
            connection.execute("INSERT OR IGNORE INTO withdraw_tombstones VALUES (?,?,?)", (message_id, origin, time.time()))
        return {**result, "withdrawn": True}

    def claim(self, *, terminal_id: str, agent_id: str, generation: int,
              kinds: Sequence[str] = ("fyi", "action"), via: str = "before_agent_start") -> dict[str, Any]:
        if not kinds or any(kind not in {"fyi", "action"} for kind in kinds):
            raise LedgerError("invalid_kind", "Claim kinds must contain fyi and/or action.")
        if via not in {"wake", "before_agent_start", "hook"}:
            raise LedgerError("invalid_via", "Only runtime adapters can claim native context delivery.")
        claim_id = str(uuid.uuid4())
        with self.store.transaction((terminal_id,)) as connection:
            agent = check_generation(connection, self.store.host, terminal_id, agent_id, generation)
            placeholders = ','.join('?' for _ in kinds)
            rows = connection.execute(
                "SELECT m.*,d.recipient_address FROM messages m JOIN deliveries d USING(message_id) WHERE "
                "d.recipient_host=? AND d.recipient_terminal_id=? AND d.state='queued' AND m.repo_id=? "
                f"AND m.kind IN ({placeholders}) ORDER BY m.created_at,m.message_id LIMIT 100",
                (self.store.host, terminal_id, agent["repo_id"], *kinds)).fetchall()
            for row in rows:
                changed = connection.execute(
                    "UPDATE deliveries SET state='delivering',claim_id=?,claimed_agent_id=?,claimed_generation=?,"
                    "via=?,hold_reason='acceptance_unknown',updated_at=? WHERE message_id=? AND state='queued'",
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
                                   "delivered_agent_id=?,evidence=? WHERE message_id=? AND state='delivering'",
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
