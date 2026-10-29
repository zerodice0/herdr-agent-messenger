"""Terminal-generation-owned device claims and read-only attachment observations."""
from __future__ import annotations

import re
import sqlite3
import subprocess
import time
from typing import Any, Mapping

from ham.ledger.status import AgentRegistry, check_generation
from ham.ledger.store import LedgerError, LedgerStore


def normalize_device(kind: str, device: str) -> str:
    if kind not in {"phone", "emu"}:
        raise LedgerError("invalid_device", "Device kind must be phone or emu.")
    value = device.strip()
    if not value or any(character.isspace() for character in value):
        raise LedgerError("invalid_device", "Device ID must be nonempty and contain no whitespace.")
    return "emulator-" + value if kind == "emu" and value.isdigit() else value


def device_rows(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    # A pre-v6 peer/local ledger is unknown to callers, never migrated on a read.
    if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='device_claims'").fetchone() is None:
        return []
    rows = connection.execute(
        "SELECT d.*,a.alias,a.pane_id,a.terminal_id,a.generation,a.active,a.host,"
        "(a.active=1 AND a.generation=(SELECT max(b.generation) FROM agents b "
        "WHERE b.host=a.host AND b.terminal_id=a.terminal_id)) AS live "
        "FROM device_claims d JOIN agents a USING(agent_id) ORDER BY d.kind,d.device_id")
    return [{"device_id": row["device_id"], "kind": row["kind"], "label": row["label"],
             "claimed_at": row["claimed_at"],
             "holder": {**{key: row[key] for key in ("agent_id", "alias", "pane_id", "terminal_id", "generation", "host")},
                        "active": bool(row["active"]), "live": bool(row["live"])},
             "stale": not bool(row["live"]), "attached": None} for row in rows]


def prune_stale(connection: sqlite3.Connection) -> set[str]:
    stale = [row for row in device_rows(connection) if row["stale"]]
    connection.executemany("DELETE FROM device_claims WHERE device_id=?",
                           [(row["device_id"],) for row in stale])
    return {row["holder"]["pane_id"] for row in stale}


def _adb_like(device: str) -> bool:
    # iOS UDIDs (old 40-hex and modern 8-16 hex forms) are not adb serials.
    return re.fullmatch(r"(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{8}-[0-9a-fA-F]{16})", device) is None


class DeviceClaims:
    def __init__(self, store: LedgerStore, *, registry: AgentRegistry | None = None,
                 runner=None) -> None:
        self.store = store
        self.registry = registry or AgentRegistry(store)
        self.runner = subprocess.run if runner is None else runner

    def _run(self, command: list[str], *, timeout: int = 2):
        try:
            result = self.runner(command, capture_output=True, text=True, timeout=timeout, check=False)
            return result if result.returncode == 0 else None
        except (OSError, subprocess.SubprocessError):
            return None

    def list(self) -> dict[str, Any]:
        with self.store.read() as connection:
            rows = device_rows(connection)
        android = [row for row in rows if _adb_like(row["device_id"])]
        result = self._run(["adb", "devices"]) if android else None
        attached = {parts[0] for line in result.stdout.splitlines()
                    if len(parts := line.split()) >= 2 and parts[1] == "device"} if result else None
        for row in android:
            row["attached"] = row["device_id"] in attached if attached is not None else None
        return {"devices": rows}

    def claim(self, agent: Mapping[str, Any], kind: str, device: str, *, label: str | None = None,
              force: bool = False, environment: Mapping[str, str] | None = None,
              now: float | None = None) -> dict[str, Any]:
        device_id = normalize_device(kind, device)
        if label is None:
            label = device_id.removeprefix("emulator-") if kind == "emu" else device_id
            if kind == "phone" and _adb_like(device_id):
                result = self._run(["adb", "-s", device_id, "shell", "getprop", "ro.product.model"], timeout=10)
                if result and result.stdout.strip():
                    label = result.stdout.strip()
        at = time.time() if now is None else now
        with self.store.transaction((agent["terminal_id"],)) as connection:
            current = check_generation(connection, self.store.host, agent["terminal_id"], agent["agent_id"], agent["generation"])
            previous = next((row for row in device_rows(connection) if row["device_id"] == device_id), None)
            other = previous is not None and previous["holder"]["agent_id"] != current["agent_id"]
            if other and not previous["stale"] and not force:
                holder = previous["holder"]
                error = LedgerError("device_claimed", f"Device {device_id} is held by {holder['alias']} "
                                    f"({holder['host']}/{holder['pane_id']}) since {previous['claimed_at']}.")
                error.holder = {**holder, "claimed_at": previous["claimed_at"]}
                raise error
            panes = prune_stale(connection) | {current["pane_id"]}
            if other and previous["holder"]["live"]:
                panes.add(previous["holder"]["pane_id"])
            connection.execute(
                "INSERT INTO device_claims VALUES (?,?,?,?,?) ON CONFLICT(device_id) DO UPDATE SET "
                "kind=excluded.kind,label=excluded.label,agent_id=excluded.agent_id,claimed_at=excluded.claimed_at",
                (device_id, kind, label, current["agent_id"], previous["claimed_at"] if previous and not other else at))
            claimed = next(row for row in device_rows(connection) if row["device_id"] == device_id)
        self.registry._refresh_display(panes, environment=environment)
        return {"device": claimed, "replaced": previous if other else None,
                "replaced_stale": bool(other and previous["stale"])}

    def release(self, agent: Mapping[str, Any], kind: str = "all", device: str | None = None, *,
                environment: Mapping[str, str] | None = None) -> dict[str, Any]:
        if kind not in {"phone", "emu", "all"} or (device is not None and kind == "all"):
            raise LedgerError("invalid_device", "A device ID requires phone or emu kind.")
        device_id = normalize_device(kind, device) if device is not None else None
        with self.store.transaction((agent["terminal_id"],)) as connection:
            current = check_generation(connection, self.store.host, agent["terminal_id"], agent["agent_id"], agent["generation"])
            released = [row for row in device_rows(connection)
                        if row["holder"]["agent_id"] == current["agent_id"]
                        and (kind == "all" or row["kind"] == kind)
                        and (device_id is None or row["device_id"] == device_id)]
            panes = prune_stale(connection) | {current["pane_id"]}
            connection.executemany("DELETE FROM device_claims WHERE device_id=?",
                                   [(row["device_id"],) for row in released])
        self.registry._refresh_display(panes, environment=environment)
        return {"released": released}


def device_operation(arguments, environment=None) -> dict[str, Any]:
    if arguments.device_command == "list":
        try:
            store = LedgerStore.open_readonly(environment)
        except LedgerError as error:
            if error.code != "ledger_not_initialized":
                raise
            store = None
        return DeviceClaims(store).list() if store is not None else {"devices": []}
    store = LedgerStore(environment=environment)
    registry = AgentRegistry(store)
    # A failed snapshot must abort; cached claims cannot prove a holder is dead.
    snapshot, _ = registry.refresh(environment, prune_device_claims=False)
    agent = registry.resolve_context(environment, snapshot)
    claims = DeviceClaims(store, registry=registry)
    if arguments.device_command == "claim":
        result = claims.claim(agent, arguments.kind, arguments.device, label=arguments.label,
                              force=arguments.force, environment=environment)
    else:
        result = claims.release(agent, arguments.kind, arguments.device, environment=environment)
    from ham.ledger.worker import ensure_after_mutation
    return store.annotate(ensure_after_mutation(store, result, environment))
