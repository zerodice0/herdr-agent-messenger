"""Private per-host mailbox storage. This module alone owns the SQLite schema."""

from __future__ import annotations

from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import re
import socket
import sqlite3
import time
from typing import Any, Iterator, Mapping, Sequence
from urllib.parse import quote
import uuid

SCHEMA_VERSION = 12
FINAL_STATES = frozenset({"handled", "withdrawn", "undeliverable"})
STATE_RANK = {"pending": 0, "queued": 1, "delivered": 2, "handled": 3,
              "withdrawn": 4, "undeliverable": 4}


class LedgerError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def require_uuid(value: str, field: str = "message_id") -> str:
    try:
        parsed = str(uuid.UUID(value))
    except (ValueError, TypeError, AttributeError):
        raise LedgerError("invalid_id", f"{field} must be a UUID.") from None
    if parsed != value:
        raise LedgerError("invalid_id", f"{field} must be a canonical UUID.")
    return value


def ledger_directory(environment: Mapping[str, str] | None = None) -> Path:
    values = os.environ if environment is None else environment
    override = values.get("HERDR_AGENT_LEDGER_DIR")
    if override:
        return Path(override).expanduser().resolve()
    home = Path(values.get("HOME") or str(Path.home()))
    state = Path(values.get("XDG_STATE_HOME") or home / ".local/state")
    return state / "herdr-agent-messenger/ledger"


_SCHEMA = """
CREATE TABLE IF NOT EXISTS ledger_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS peers (
 canonical_name TEXT PRIMARY KEY, uuid TEXT NOT NULL UNIQUE,
 ssh_alias TEXT NOT NULL, remote_ham_root TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS repos (
 repo_id TEXT PRIMARY KEY, home_host TEXT, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS repo_settings (
 repo_id TEXT NOT NULL REFERENCES repos(repo_id), field TEXT NOT NULL,
 value TEXT NOT NULL, version INTEGER NOT NULL, origin_host TEXT NOT NULL,
 changed_by TEXT NOT NULL DEFAULT 'user', changed_at REAL NOT NULL,
 PRIMARY KEY(repo_id,field));
CREATE TABLE IF NOT EXISTS workspace_settings (
 repo_id TEXT NOT NULL REFERENCES repos(repo_id), host TEXT NOT NULL, workspace_id TEXT NOT NULL,
 field TEXT NOT NULL CHECK(field IN ('main','members')), value TEXT NOT NULL,
 version INTEGER NOT NULL, changed_by TEXT NOT NULL, changed_at REAL NOT NULL,
 PRIMARY KEY(repo_id,host,workspace_id,field));
CREATE TABLE IF NOT EXISTS workspaces (
 host TEXT NOT NULL, workspace_id TEXT NOT NULL, repo_key TEXT NOT NULL,
 is_linked_worktree INTEGER NOT NULL CHECK(is_linked_worktree IN (0,1)),
 label TEXT NOT NULL, PRIMARY KEY(host,workspace_id));
CREATE INDEX IF NOT EXISTS workspace_repo_key ON workspaces(host,repo_key,is_linked_worktree,workspace_id);
CREATE TABLE IF NOT EXISTS setting_conflicts (
 seq INTEGER PRIMARY KEY, repo_id TEXT NOT NULL, field TEXT NOT NULL,
 version INTEGER NOT NULL, winner_host TEXT NOT NULL, loser_host TEXT NOT NULL,
 winner_value TEXT NOT NULL, loser_value TEXT NOT NULL, at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS agents (
 agent_id TEXT PRIMARY KEY, host TEXT NOT NULL, terminal_id TEXT NOT NULL,
 workspace_id TEXT NOT NULL, pane_id TEXT NOT NULL, session_ref TEXT NOT NULL,
 generation INTEGER NOT NULL, client TEXT NOT NULL, repo_id TEXT NOT NULL,
 is_main_checkout INTEGER NOT NULL, alias TEXT NOT NULL,
 active INTEGER NOT NULL DEFAULT 1, extension_present INTEGER NOT NULL DEFAULT 0,
 last_ham_label TEXT, first_seen REAL NOT NULL, last_seen REAL NOT NULL,
 native_session_pid INTEGER,
 UNIQUE(host,terminal_id,generation));
CREATE TABLE IF NOT EXISTS terminal_slots (
 host TEXT NOT NULL, terminal_id TEXT NOT NULL, pane_id TEXT NOT NULL,
 workspace_id TEXT NOT NULL, present INTEGER NOT NULL, updated_at REAL NOT NULL,
 PRIMARY KEY(host,terminal_id));
CREATE TABLE IF NOT EXISTS pane_bindings (
 host TEXT NOT NULL, pane_id TEXT NOT NULL, terminal_id TEXT NOT NULL,
 PRIMARY KEY(host,pane_id,terminal_id));
CREATE UNIQUE INDEX IF NOT EXISTS active_terminal ON agents(host,terminal_id) WHERE active=1;
CREATE TABLE IF NOT EXISTS agent_status (
 agent_id TEXT PRIMARY KEY REFERENCES agents(agent_id), turn_state TEXT NOT NULL,
 phase TEXT NOT NULL DEFAULT '', summary TEXT NOT NULL DEFAULT '', updated_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS device_claims (
 device_id TEXT PRIMARY KEY, kind TEXT NOT NULL CHECK(kind IN ('phone','emu')),
 label TEXT NOT NULL, agent_id TEXT NOT NULL REFERENCES agents(agent_id),
 claimed_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS status_log (
 seq INTEGER PRIMARY KEY, agent_id TEXT NOT NULL, field TEXT NOT NULL,
 value TEXT NOT NULL, at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS threads (
 thread_id TEXT PRIMARY KEY, repo_id TEXT NOT NULL, subject TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS messages (
 message_id TEXT PRIMARY KEY, thread_id TEXT NOT NULL REFERENCES threads(thread_id),
 repo_id TEXT NOT NULL, sender_repo_id TEXT NOT NULL, reply_to TEXT, origin_host TEXT NOT NULL,
 sender TEXT NOT NULL, sender_label TEXT NOT NULL, kind TEXT NOT NULL CHECK(kind IN ('fyi','action')),
 hop INTEGER NOT NULL, body TEXT NOT NULL, created_at REAL NOT NULL, final_at REAL,
 payload_hash TEXT NOT NULL, sender_context TEXT);
CREATE TABLE IF NOT EXISTS deliveries (
 message_id TEXT PRIMARY KEY REFERENCES messages(message_id) ON DELETE CASCADE,
 recipient_address TEXT NOT NULL, recipient_host TEXT NOT NULL, recipient_repo_id TEXT NOT NULL,
 recipient_terminal_id TEXT, workspace_id TEXT NOT NULL DEFAULT '', pane_id TEXT NOT NULL DEFAULT '',
 state TEXT NOT NULL CHECK(state IN ('pending','queued','delivering','delivered','handled','withdrawn','undeliverable')),
 hold_reason TEXT, claim_id TEXT, claimed_agent_id TEXT, claimed_generation INTEGER,
 via TEXT, delivered_agent_id TEXT, authoritative INTEGER NOT NULL,
 delivered_at REAL, handled_at REAL, updated_at REAL NOT NULL,
 withdraw_requested INTEGER NOT NULL DEFAULT 0, evidence TEXT,
 confirmation_source TEXT CHECK(confirmation_source IN ('native','operator')),
 fyi_turn_end INTEGER NOT NULL DEFAULT 0 CHECK(fyi_turn_end IN (0,1)));
CREATE INDEX IF NOT EXISTS inbox_slot ON deliveries(recipient_host,recipient_terminal_id,state);
CREATE TABLE IF NOT EXISTS dedupe_receipts (
 message_id TEXT PRIMARY KEY, payload_hash TEXT NOT NULL, origin_host TEXT NOT NULL,
 receipt TEXT NOT NULL, updated_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS withdraw_tombstones (
 message_id TEXT PRIMARY KEY, origin_host TEXT NOT NULL, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS outbox (
 item_id TEXT PRIMARY KEY, target_host TEXT NOT NULL, peer_seq INTEGER NOT NULL,
 kind TEXT NOT NULL CHECK(kind IN ('message','receipt','setting','withdraw')),
 message_id TEXT, payload TEXT NOT NULL,
 state TEXT NOT NULL CHECK(state IN ('pending','sent','failed','cancelled')),
 attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT, next_try_at REAL NOT NULL,
 UNIQUE(target_host,peer_seq));
CREATE INDEX IF NOT EXISTS pending_outbox ON outbox(target_host,state,peer_seq);
CREATE TABLE IF NOT EXISTS wake_log (
 seq INTEGER PRIMARY KEY, agent_id TEXT NOT NULL, at REAL NOT NULL,
 message_ids TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS peer_options (
 canonical_name TEXT PRIMARY KEY REFERENCES peers(canonical_name) ON DELETE CASCADE,
 remote_ledger_dir TEXT NOT NULL DEFAULT '', remote_socket TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS message_routes (
 message_id TEXT PRIMARY KEY,
 original_host TEXT NOT NULL, original_terminal TEXT, original_address TEXT NOT NULL,
 resolution_host TEXT NOT NULL, resolution_terminal TEXT, resolution_address TEXT NOT NULL,
 receipt_host TEXT, forward_host TEXT);
CREATE TABLE IF NOT EXISTS notification_failures (
 seq INTEGER PRIMARY KEY, at REAL NOT NULL, detail TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS native_claims (
 claim_id TEXT PRIMARY KEY, client TEXT NOT NULL, session_ref TEXT NOT NULL,
 marker TEXT NOT NULL, content_sha256 TEXT NOT NULL, created_at REAL NOT NULL,
 transcript_path TEXT, transcript_start_offset INTEGER, content_bytes INTEGER);
CREATE TABLE IF NOT EXISTS submissions (
 submission_id TEXT PRIMARY KEY, operation_id TEXT UNIQUE, repo_id TEXT,
 via TEXT NOT NULL CHECK(via IN ('direct','spawn_initial')),
 acceptance TEXT NOT NULL CHECK(acceptance IN ('submitting','accepted','unknown','failed')),
 sender TEXT NOT NULL, sender_kind TEXT NOT NULL CHECK(sender_kind IN ('user','agent','non-agent','unknown')),
 target TEXT NOT NULL, body TEXT NOT NULL, subject TEXT NOT NULL DEFAULT '',
 body_sha256 TEXT NOT NULL, readiness_evidence TEXT NOT NULL DEFAULT '',
 evidence TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL, updated_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS submissions_repo_updated ON submissions(repo_id,via,updated_at DESC);
CREATE TABLE IF NOT EXISTS claim_resolutions (
 seq INTEGER PRIMARY KEY, claim_id TEXT NOT NULL, message_id TEXT NOT NULL,
 agent_id TEXT NOT NULL, generation INTEGER NOT NULL,
 disposition TEXT NOT NULL CHECK(disposition IN ('applied','not_applied')),
 evidence TEXT NOT NULL, resolved_at REAL NOT NULL,
 UNIQUE(claim_id,message_id));
CREATE INDEX IF NOT EXISTS claim_resolution_lookup ON claim_resolutions(claim_id,seq);
CREATE TABLE IF NOT EXISTS mail_dismissals (
 message_id TEXT PRIMARY KEY, origin_host TEXT NOT NULL,
 sender_agent_id TEXT NOT NULL, sender_terminal_id TEXT NOT NULL, sender_repo_id TEXT NOT NULL,
 sender_workspace_id TEXT NOT NULL, sender_scope_workspace_id TEXT NOT NULL,
 actor_agent_id TEXT NOT NULL, actor_terminal_id TEXT NOT NULL, actor_generation INTEGER NOT NULL,
 actor_repo_id TEXT NOT NULL, actor_workspace_id TEXT NOT NULL, actor_alias TEXT NOT NULL,
 reason TEXT NOT NULL, dismissed_at REAL NOT NULL, failure TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS auto_main_incumbents (
 repo_id TEXT NOT NULL, host TEXT NOT NULL, anchor_workspace_id TEXT NOT NULL,
 terminal_id TEXT NOT NULL, recorded_at REAL NOT NULL,
 PRIMARY KEY(repo_id,host,anchor_workspace_id));
"""


class LedgerStore:
    def __init__(self, directory: Path | str | None = None, *,
                 environment: Mapping[str, str] | None = None,
                 canonical_name: str | None = None) -> None:
        self.directory = Path(directory) if directory is not None else ledger_directory(environment)
        self.notification_errors: list[dict[str, Any]] = []
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.directory.chmod(0o700)
        self.path = self.directory / "ledger.sqlite3"
        descriptor = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(descriptor)
        self.path.chmod(0o600)
        self.bell_directory = self.directory / "bell"
        self.bell_directory.mkdir(exist_ok=True, mode=0o700)
        self.bell_directory.chmod(0o700)
        values = os.environ if environment is None else environment
        chosen_name = canonical_name or values.get("HERDR_AGENT_CANONICAL_HOST")
        if chosen_name is not None and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", chosen_name):
            raise LedgerError("invalid_host", "Canonical host must be a nonempty simple host name.")
        # Serialize the initial WAL switch as well as schema/identity setup.
        with os.fdopen(os.open(self.directory / "bootstrap.lock", os.O_CREAT | os.O_RDWR, 0o600), "r+b") as bootstrap:
            fcntl.flock(bootstrap.fileno(), fcntl.LOCK_EX)
            connection = self.connect()
            try:
                connection.execute("PRAGMA journal_mode=WAL")
                connection.executescript(_SCHEMA)
                connection.execute("BEGIN IMMEDIATE")
                existing = connection.execute(
                    "SELECT value FROM ledger_meta WHERE key='schema_version'").fetchone()
                existing_version = int(existing[0]) if existing else 0
                if existing and existing_version not in {1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, SCHEMA_VERSION}:
                    raise LedgerError("unsupported_schema", "This ledger requires a different HAM version.")
                columns = {row["name"] for row in connection.execute("PRAGMA table_info(deliveries)")}
                if "fyi_turn_end" not in columns:
                    # Historical enqueue-time state cannot be reconstructed safely.
                    connection.execute("ALTER TABLE deliveries ADD COLUMN fyi_turn_end INTEGER NOT NULL "
                                       "DEFAULT 0 CHECK(fyi_turn_end IN (0,1))")
                if "recipient_repo_id" not in columns:
                    connection.execute("ALTER TABLE deliveries ADD COLUMN recipient_repo_id TEXT NOT NULL DEFAULT ''")
                    connection.execute(
                        "UPDATE deliveries SET recipient_repo_id=(SELECT repo_id FROM messages m "
                        "WHERE m.message_id=deliveries.message_id)")
                message_columns = {row["name"] for row in connection.execute("PRAGMA table_info(messages)")}
                if "sender_repo_id" not in message_columns:
                    connection.execute("ALTER TABLE messages ADD COLUMN sender_repo_id TEXT NOT NULL DEFAULT ''")
                    connection.execute("UPDATE messages SET sender_repo_id=repo_id")
                if "sender_context" not in message_columns:
                    # Historical role groups cannot be reconstructed after their workspace closes.
                    connection.execute("ALTER TABLE messages ADD COLUMN sender_context TEXT")
                if "confirmation_source" not in columns:
                    connection.execute("ALTER TABLE deliveries ADD COLUMN confirmation_source TEXT "
                                       "CHECK(confirmation_source IN ('native','operator'))")
                    connection.execute(
                        "UPDATE deliveries SET confirmation_source='operator' WHERE EXISTS ("
                        "SELECT 1 FROM claim_resolutions r WHERE r.message_id=deliveries.message_id "
                        "AND r.claim_id=deliveries.claim_id AND r.disposition='applied')")
                native_columns = {row["name"] for row in connection.execute("PRAGMA table_info(native_claims)")}
                for name, kind in (("transcript_path", "TEXT"), ("transcript_start_offset", "INTEGER"),
                                   ("content_bytes", "INTEGER")):
                    if name not in native_columns:
                        connection.execute(f"ALTER TABLE native_claims ADD COLUMN {name} {kind}")
                agent_columns = {row["name"] for row in connection.execute("PRAGMA table_info(agents)")}
                if "native_session_pid" not in agent_columns:
                    connection.execute("ALTER TABLE agents ADD COLUMN native_session_pid INTEGER")
                default_name = re.sub(r"[^A-Za-z0-9_.-]", "-", socket.gethostname().split('.')[0]) or "host"
                connection.execute("INSERT INTO ledger_meta VALUES ('schema_version',?) "
                                   "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                                   (str(SCHEMA_VERSION),))
                for key, value in (("self_canonical_name", chosen_name or default_name),
                                   ("self_uuid", str(uuid.uuid4())),
                                   ("history_cursor_key", os.urandom(32).hex())):
                    connection.execute("INSERT OR IGNORE INTO ledger_meta VALUES (?,?)", (key, value))
                metadata = dict(connection.execute("SELECT key,value FROM ledger_meta"))
                if chosen_name and chosen_name != metadata["self_canonical_name"]:
                    raise LedgerError("host_identity_conflict", "An existing ledger's canonical name cannot change.")
                self.host = metadata["self_canonical_name"]
                self.host_uuid = metadata["self_uuid"]
                if existing_version < 7:
                    self._migrate_workspace_settings(connection)
                cursor_key = metadata["history_cursor_key"]
                if not re.fullmatch(r"[0-9a-f]{64}", cursor_key):
                    raise LedgerError("invalid_metadata", "Ledger history cursor key is malformed.")
                self._history_cursor_key = bytes.fromhex(cursor_key)
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
            finally:
                connection.close()

    def _migrate_workspace_settings(self, connection: sqlite3.Connection) -> None:
        """Map legacy replicated targets only where this host has authoritative agents."""
        dropped: list[dict[str, Any]] = []
        for row in connection.execute(
                "SELECT * FROM repo_settings WHERE field IN ('main','members')").fetchall():
            value = json.loads(row["value"])
            targets = ([value] if value else []) if row["field"] == "main" else value
            grouped: dict[str, list[dict[str, str]]] = {}
            for target in targets:
                if target["host"] != self.host:
                    continue
                agent = connection.execute(
                    "SELECT workspace_id FROM agents WHERE repo_id=? AND host=? AND terminal_id=? AND active=1",
                    (row["repo_id"], self.host, target["terminal_id"])).fetchone()
                if agent is None:
                    dropped.append({"repo_id": row["repo_id"], "field": row["field"], "target": target})
                    continue
                grouped.setdefault(agent["workspace_id"], []).append(target)
            for workspace_id, members in grouped.items():
                scoped_value = members[0] if row["field"] == "main" else members
                connection.execute(
                    "INSERT OR IGNORE INTO workspace_settings VALUES (?,?,?,?,?,?,?,?)",
                    (row["repo_id"], self.host, workspace_id, row["field"], json_text(scoped_value),
                     row["version"], row["changed_by"], row["changed_at"]))
        if dropped:
            connection.execute("INSERT OR REPLACE INTO ledger_meta VALUES ('workspace_settings_migration_dropped',?)",
                               (json_text(dropped),))
        connection.execute("DELETE FROM repo_settings WHERE field IN ('main','members','computed_main')")
        connection.execute("DELETE FROM setting_conflicts WHERE field IN ('main','members','computed_main')")
        connection.execute("DELETE FROM outbox WHERE kind='setting' AND "
                           "json_extract(payload,'$.field') IN ('main','members','computed_main')")
        connection.execute("DROP TABLE IF EXISTS repo_members")
        connection.execute("DROP TABLE IF EXISTS setting_conditions")

    @classmethod
    def open_readonly(cls, environment: Mapping[str, str] | None = None) -> LedgerStore | None:
        """Open an existing ledger without bootstrap, migrations, or mkdir."""
        directory = ledger_directory(environment)
        path = directory / "ledger.sqlite3"
        if not path.is_file():
            return None
        store = cls.__new__(cls)
        store.directory, store.path = directory, path
        store.bell_directory = directory / "bell"
        store.notification_errors = []
        store._readonly = True
        try:
            with store.read() as connection:
                metadata = dict(connection.execute("SELECT key,value FROM ledger_meta"))
            store.host = metadata["self_canonical_name"]
            store.host_uuid = metadata["self_uuid"]
            store._history_cursor_key = bytes.fromhex(metadata["history_cursor_key"])
            version = metadata.get("schema_version", "")
            if version != str(SCHEMA_VERSION):
                if version.isdecimal() and int(version) < SCHEMA_VERSION:
                    raise LedgerError("schema_upgrade_required",
                                      "HAM ledger schema upgrade required; run ledger reconcile on this host before read-only commands.")
                raise LedgerError("unsupported_schema", "This ledger requires a different HAM version.")
        except (sqlite3.Error, KeyError) as error:
            raise LedgerError("ledger_not_initialized", "No initialized HAM ledger is available to read.") from error
        return store

    def connect(self) -> sqlite3.Connection:
        connection = (sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True, timeout=30,
                                      isolation_level=None)
                      if getattr(self, "_readonly", False) else
                      sqlite3.connect(str(self.path), timeout=30, isolation_level=None))
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def transaction(self, terminals: Sequence[str] = ()) -> Iterator[sqlite3.Connection]:
        """Commit board changes; callers supply only terminals whose receiver state changed.

        A mutable terminal list may be filled while holding the transaction lock.
        Heartbeats and board-only writes must not broadcast to native receivers.
        """
        connection = self.connect()
        changed = False
        try:
            connection.execute("BEGIN IMMEDIATE")
            before = connection.total_changes
            yield connection
            changed = connection.total_changes != before
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()
        if changed:
            try:
                self.ring(terminals)
            except OSError as error:
                # The transaction is already durable. Never report a rollback or
                # induce a new-message retry because the advisory bell failed.
                warning = {"code": "notification_failed", "message": str(error),
                           "committed": True, "recovery": "event_or_safety_sweep"}
                if warning not in self.notification_errors:
                    self.notification_errors.append(warning)
                try:
                    self.note_notification_failure(str(error))
                except sqlite3.Error:
                    pass

    def note_notification_failure(self, detail: str) -> None:
        """One last-seen row per diagnostic, retained by the existing 30-day policy.

        Diagnostics bypass bells: reporting a broken bell must not recurse.
        """
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT min(seq) FROM notification_failures WHERE detail=?", (detail,)).fetchone()
            if row[0] is None:
                connection.execute("INSERT INTO notification_failures(at,detail) VALUES (?,?)", (time.time(), detail))
            else:
                connection.execute("UPDATE notification_failures SET at=? WHERE seq=?", (time.time(), row[0]))
                connection.execute("DELETE FROM notification_failures WHERE detail=? AND seq<>?", (detail, row[0]))
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def ring(self, terminals: Sequence[str] = ()) -> None:
        for name in {"_board", *(quote(terminal, safe="._-:") for terminal in terminals if terminal)}:
            path = self.bell_directory / name
            descriptor = os.open(path, os.O_CREAT | os.O_WRONLY, 0o600)
            os.close(descriptor)
            path.touch()

    def doctor(self) -> dict[str, Any]:
        with self.read() as connection:
            counts = {table: connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                      for table in ("agents", "messages", "deliveries", "outbox", "dedupe_receipts")}
        with self.read() as connection:
            failures = [dict(row) for row in connection.execute(
                "SELECT * FROM notification_failures ORDER BY seq DESC LIMIT 10")]
        return {"protocol": 1, "schema_version": SCHEMA_VERSION, "directory": str(self.directory),
                "host": {"canonical_name": self.host, "uuid": self.host_uuid},
                "counts": counts, "notification_failures": failures}

    def retention(self, *, now: float | None = None) -> dict[str, int]:
        at = time.time() if now is None else now
        with self.transaction() as connection:
            # Final message payloads must not survive via sent/failed OUTBOX JSON.
            connection.execute("DELETE FROM outbox WHERE message_id IN (SELECT message_id FROM messages WHERE "
                               "final_at IS NOT NULL AND final_at<?) AND state IN ('sent','failed','cancelled')",
                               (at - 90 * 86400,))
            expired = connection.execute(
                "DELETE FROM messages WHERE final_at IS NOT NULL AND final_at<?",
                (at - 90 * 86400,)).rowcount
            connection.execute("DELETE FROM native_claims WHERE NOT EXISTS "
                               "(SELECT 1 FROM deliveries d WHERE d.claim_id=native_claims.claim_id)")
            # Threads have no message bodies and remain useful as stable reply identities.
            status = connection.execute("DELETE FROM status_log WHERE at<?", (at - 30 * 86400,)).rowcount
            wake = connection.execute("DELETE FROM wake_log WHERE at<?", (at - 30 * 86400,)).rowcount
            connection.execute("DELETE FROM notification_failures WHERE at<?", (at - 30 * 86400,))
        return {"messages": expired, "status_log": status, "wake_log": wake}

    def annotate(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        result = dict(payload)
        if self.notification_errors:
            result["warnings"] = list(result.get("warnings", []))
            for warning in self.notification_errors:
                if warning not in result["warnings"]:
                    result["warnings"].append(warning)
        return result

    @contextmanager
    def lock(self, name: str) -> Iterator[None]:
        if not re.fullmatch(r"[a-z-]+", name):
            raise ValueError("invalid lock name")
        descriptor = os.open(self.directory / f"{name}.lock", os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)


def receipt(connection: sqlite3.Connection, message_id: str) -> dict[str, Any]:
    row = connection.execute(
        "SELECT d.*,m.origin_host,m.payload_hash FROM deliveries d JOIN messages m USING(message_id) "
        "WHERE message_id=?", (message_id,)).fetchone()
    if row is None:
        old = connection.execute("SELECT receipt FROM dedupe_receipts WHERE message_id=?", (message_id,)).fetchone()
        if old is None:
            raise LedgerError("message_not_found", "No message with that ID exists in this ledger.")
        return json.loads(old[0])
    result = {key: row[key] for key in (
        "message_id", "origin_host", "payload_hash", "recipient_address", "recipient_host",
        "recipient_terminal_id", "state", "hold_reason", "via", "delivered_agent_id",
        "delivered_at", "handled_at", "updated_at", "withdraw_requested", "authoritative",
        "confirmation_source", "evidence")}
    result["authoritative_host"] = row["recipient_host"] if row["state"] != "pending" else None
    return result


def queue_outbox(connection: sqlite3.Connection, target_host: str, kind: str,
                 payload: Mapping[str, Any], message_id: str | None = None) -> str:
    item_id = str(uuid.uuid4())
    sequence = connection.execute("SELECT coalesce(max(peer_seq),0)+1 FROM outbox WHERE target_host=?",
                                  (target_host,)).fetchone()[0]
    connection.execute("INSERT INTO outbox(item_id,target_host,peer_seq,kind,message_id,payload,state,next_try_at) "
                       "VALUES (?,?,?,?,?,?,'pending',?)",
                       (item_id, target_host, sequence, kind, message_id, json_text(payload), time.time()))
    return item_id


def public_receipt(value: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(value)
    if result["state"] == "delivering":
        result["state"], result["hold_reason"] = "queued", None
    result["authoritative_host"] = result["recipient_host"] if result["state"] != "pending" else None
    return result


def save_receipt(connection: sqlite3.Connection, message_id: str) -> dict[str, Any]:
    current = receipt(connection, message_id)
    public = public_receipt(current)
    connection.execute(
        "INSERT INTO dedupe_receipts VALUES (?,?,?,?,?) ON CONFLICT(message_id) DO UPDATE SET "
        "receipt=excluded.receipt,updated_at=excluded.updated_at",
        (message_id, current["payload_hash"], current["origin_host"], json_text(public), current["updated_at"]))
    route = connection.execute("SELECT receipt_host FROM message_routes WHERE message_id=?", (message_id,)).fetchone()
    if route and route[0]:
        queue_outbox(connection, route[0], "receipt", public, message_id)
    return current
