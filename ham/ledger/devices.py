"""Terminal-generation-owned device claims and read-only attachment observations."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import os
import re
import sqlite3
import subprocess
import time
import sys
from typing import Any, Mapping

from ham.ledger.device_board import HOLDER_FIELDS

from ham.ledger.status import AgentRegistry, check_generation
from ham.ledger.store import LedgerError, LedgerStore


def normalize_device(kind: str, device: str) -> str:
    if kind not in {"phone", "emu"}:
        raise LedgerError("invalid_device", "Device kind must be phone or emu.")
    value = device.strip()
    if not value or any(character.isspace() for character in value):
        raise LedgerError("invalid_device", "Device ID must be nonempty and contain no whitespace.")
    return "emulator-" + value if kind == "emu" and value.isdigit() else value


def _android_emulator(device_id: str) -> bool:
    """Android emulator serials are emulator-<port>; iOS simulator UUIDs are not limited."""
    port = device_id.removeprefix("emulator-")
    return device_id.startswith("emulator-") and port.isdigit()


def device_rows(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    # A pre-v6 peer/local ledger is unknown to callers, never migrated on a read.
    if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='device_claims'").fetchone() is None:
        return []
    rows = connection.execute(
        "SELECT d.*,a.alias,a.pane_id,a.terminal_id,a.generation,a.active,a.host,a.repo_id,a.workspace_id,"
        "(a.active=1 AND a.generation=(SELECT max(b.generation) FROM agents b "
        "WHERE b.host=a.host AND b.terminal_id=a.terminal_id)) AS live "
        "FROM device_claims d JOIN agents a USING(agent_id) ORDER BY d.kind,d.device_id")
    return [{"device_id": row["device_id"], "kind": row["kind"], "label": row["label"],
             "claimed_at": row["claimed_at"],
             "holder": {**{key: row[key] for key in HOLDER_FIELDS},
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


def emulator_limit(environment: Mapping[str, str] | None = None) -> int:
    values = os.environ if environment is None else environment
    try:
        return int(values.get("HERDR_AGENT_EMU_LIMIT", "3"))
    except ValueError:
        raise LedgerError("invalid_config", "HERDR_AGENT_EMU_LIMIT must be an integer.") from None


def scan_devices(*, runner=None, platform: str | None = None) -> dict[str, Any]:
    """Probe independent observation sources concurrently, each bounded to two seconds.

    Physical iOS devices remain unknown: devicectl can start CoreDevice services,
    request pairing/access, and has no uniform bounded, read-only availability.
    """
    platform = sys.platform if platform is None else platform
    probes = DeviceClaims(None, runner=runner)
    errors = []

    def adb():
        result = probes._run(["adb", "devices"])
        if result is None:
            return None
        rows = []
        for line in result.stdout.splitlines():
            fields = line.split()
            if len(fields) >= 2 and not line.startswith(("List of devices", "*")):
                rows.append({"serial": fields[0], "state": fields[1]})
        return rows

    def simulators():
        result = probes._run(["xcrun", "simctl", "list", "devices", "booted", "--json"])
        if result is None:
            return None
        try:
            devices = json.loads(result.stdout)["devices"]
            if not isinstance(devices, dict) or any(not isinstance(rows, list) for rows in devices.values()):
                return None
            rows = [row for group in devices.values() for row in group]
            if any(not isinstance(row, dict) or not isinstance(row.get("udid"), str)
                   or not isinstance(row.get("name"), str) for row in rows):
                return None
            return [{"udid": row["udid"], "name": row["name"]}
                    for row in rows if row.get("state") == "Booted"]
        except (ValueError, KeyError, TypeError):
            return None

    sources = {"adb": adb, "emulator_processes": probes._emulator_processes}
    if platform == "darwin":
        sources["ios_simulators_booted"] = simulators
    observed = {"adb": None, "emulator_processes": None,
                "ios_simulators_booted": None, "ios_devices": None, "error": None}
    with ThreadPoolExecutor(max_workers=len(sources), thread_name_prefix="ham-devices") as executor:
        futures = {key: executor.submit(probe) for key, probe in sources.items()}
        for key, future in futures.items():
            try:
                observed[key] = future.result()
            except Exception as error:
                errors.append(f"{key}: {error}")
                continue
            if observed[key] is None:
                errors.append(f"{key}: observation unavailable")
    observed["error"] = "; ".join(errors) or None
    return observed


class DeviceClaims:
    def __init__(self, store: LedgerStore | None, *, registry: AgentRegistry | None = None,
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

    def _emulator_processes(self) -> list[dict[str, Any]] | None:
        # The admission check and opt-in host resource snapshot share this parser.
        result = self._run(["ps", "-axww", "-o", "pid=", "-o", "ucomm=", "-o", "args="])
        if result is None:
            return None
        processes = []
        for line in result.stdout.splitlines():
            fields = line.split(None, 2)
            if len(fields) != 3 or not fields[0].isdigit() or not fields[1].startswith("qemu-system-"):
                continue
            # ps joins argv without shell quoting. Quotes and SDK path spaces
            # are literal data; ucomm identifies qemu even on truncated names.
            executable_prefix = fields[2].split("qemu-system-", 1)[0]
            argv = fields[2].split()
            android = "/emulator/" in executable_prefix or any(
                argument.split("=", 1)[0] in {"-avd", "-port", "-ports"} for argument in argv)
            if not android:
                continue
            port = None
            for index, argument in enumerate(argv[1:], 1):
                flag, separator, value = argument.partition("=")
                if flag not in {"-port", "-ports"}:
                    continue
                if not separator:
                    value = argv[index + 1] if index + 1 < len(argv) else ""
                console_port = value.split(",", 1)[0]
                if console_port.isdigit():
                    port = int(console_port)
                    break
            processes.append({"pid": int(fields[0]), "port": port})
        return processes

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
        limit = 0
        processes = None
        if kind == "emu" and _android_emulator(device_id) and not force:
            limit = emulator_limit(environment)
            if limit > 0:
                processes = self._emulator_processes()
        at = time.time() if now is None else now
        with self.store.transaction((agent["terminal_id"],)) as connection:
            current = check_generation(connection, self.store.host, agent["terminal_id"], agent["agent_id"], agent["generation"])
            rows = device_rows(connection)
            previous = next((row for row in rows if row["device_id"] == device_id), None)
            other = previous is not None and previous["holder"]["agent_id"] != current["agent_id"]
            if other and not previous["stale"] and not force:
                holder = previous["holder"]
                error = LedgerError("device_claimed", f"Device {device_id} is held by {holder['alias']} "
                                    f"({holder['host']}/{holder['pane_id']}) since {previous['claimed_at']}.")
                error.holder = {**holder, "claimed_at": previous["claimed_at"]}
                raise error
            own_reclaim = (previous is not None and not other and not previous["stale"]
                           and previous["kind"] == "emu")
            if kind == "emu" and limit > 0 and not own_reclaim:
                claims = [row for row in rows if row["kind"] == "emu" and _android_emulator(row["device_id"])
                          and not row["stale"] and row["holder"]["host"] == self.store.host
                          and row["device_id"] != device_id]
                target_port = device_id.removeprefix("emulator-")
                target_port = int(target_port) if device_id.startswith("emulator-") and target_port.isdigit() else None
                others = ([process for process in processes
                           if target_port is None or process["port"] != target_port]
                          if processes is not None else None)
                process_count = len(others) if others is not None else None
                if max(len(claims), process_count or 0) >= limit:
                    claimed_devices = {row["device_id"] for row in claims}
                    error = LedgerError(
                        "emu_limit", f"Host {self.store.host} has reached the emulator limit ({limit}). "
                        "Wait for an emulator to be released/stopped, or do other work. "
                        "Use --force only on explicit user instruction; existing emulators are not stopped.")
                    error.details = {
                        "limit": limit, "claim_count": len(claims), "process_count": process_count,
                        "holders": [{"alias": row["holder"]["alias"], "pane": row["holder"]["pane_id"],
                                     "device": row["device_id"], "label": row["label"],
                                     "claimed_at": row["claimed_at"]} for row in claims],
                        "untracked_processes": [process for process in others or []
                                                if process["port"] is None
                                                or f"emulator-{process['port']}" not in claimed_devices],
                    }
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
    if arguments.device_command == "board":
        from ham.ledger.cli import status_operation
        from ham.ledger.device_board import device_board
        return device_board(status_operation("board", environment=environment))
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
