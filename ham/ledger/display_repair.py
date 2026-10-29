"""Bounded host-local projection retry state, stored without a schema change."""

from __future__ import annotations

from dataclasses import asdict
import hashlib
import json
import sqlite3
from typing import Any, Mapping

from ham.ledger.store import json_text

KEY = "display_repair"
MAX_ATTEMPTS = 5
MAX_PANES = 256
RETRY_SECONDS = 5


def load(connection: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    row = connection.execute("SELECT value FROM ledger_meta WHERE key=?", (KEY,)).fetchone()
    return json.loads(row[0]) if row else {}


def save(connection: sqlite3.Connection, entries: Mapping[str, Any]) -> None:
    if entries:
        connection.execute("INSERT INTO ledger_meta VALUES (?,?) ON CONFLICT(key) "
                           "DO UPDATE SET value=excluded.value", (KEY, json_text(entries)))
    else:
        connection.execute("DELETE FROM ledger_meta WHERE key=?", (KEY,))


def fingerprint(agent: Mapping[str, Any], projection: Any = None) -> str:
    # Revisions, turn state, and last_ham_label do not change desired output.
    value = {key: agent.get(key) for key in ("host", "terminal_id", "pane_id", "agent_id")}
    value["projection"] = asdict(projection) if projection is not None else {"phone": "", "emu": ""}
    return hashlib.sha256(json_text(value).encode()).hexdigest()


def reserve(entries: dict[str, dict[str, Any]], pane_id: str, desired: str,
            now: float, *, force: bool = False) -> tuple[bool, str | None]:
    """Reserve before writing; verify even successful commands on the next snapshot."""
    entry = entries.get(pane_id)
    if entry is not None and entry["fingerprint"] != desired:
        del entries[pane_id]
        entry = None
    if entry is None:
        if len(entries) >= MAX_PANES:
            return False, "Display repair queue full; projection deferred for " + pane_id
        entry = entries[pane_id] = {"fingerprint": desired, "attempts": 0,
                                    "next_try_at": now, "state": "pending", "last_error": ""}
    if entry["state"] == "exhausted":
        return False, None
    if entry["attempts"] >= MAX_ATTEMPTS:
        entry["state"] = "exhausted"
        entry["last_error"] = entry["last_error"] or "Projection commands succeeded but snapshot still differs"
        return False, f"Display repair exhausted for {pane_id} after {MAX_ATTEMPTS} attempts: {entry['last_error']}"
    if not force and now < entry["next_try_at"]:
        return False, None
    entry["attempts"] += 1
    entry["next_try_at"] = now + RETRY_SECONDS * 2 ** (entry["attempts"] - 1)
    entry["last_error"] = ""
    return True, None


def failed(entry: dict[str, Any], detail: str) -> str | None:
    entry["last_error"] = detail
    if entry["attempts"] >= MAX_ATTEMPTS:
        entry["state"] = "exhausted"
        return f"Display repair exhausted after {MAX_ATTEMPTS} attempts: {detail}"
    return None
