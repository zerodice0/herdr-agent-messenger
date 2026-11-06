"""Pure, device-centric projection of a local and one-hop status board."""
from __future__ import annotations

import re
import time
from typing import Any, Mapping

PLATFORMS = ("android-device", "android-emulator", "ios-device", "ios-simulator", "unknown")
HOLDER_FIELDS = ("agent_id", "alias", "pane_id", "terminal_id", "generation", "host",
                 "repo_id", "workspace_id", "active", "live")
_UUID = re.compile(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}")
_IOS_DEVICE = re.compile(r"(?:[0-9a-fA-F]{8}-[0-9a-fA-F]{16}|[0-9a-fA-F]{40})")


def classify_device(kind: str, device_id: str) -> str:
    if not isinstance(device_id, str) or not device_id:
        return "unknown"
    if re.fullmatch(r"emulator-[0-9]+", device_id):
        return "android-emulator"
    if kind == "emu" and _UUID.fullmatch(device_id):
        return "ios-simulator"
    if kind == "phone":
        return "ios-device" if _IOS_DEVICE.fullmatch(device_id) else "android-device"
    return "unknown"


def _claims(host: Mapping[str, Any], resources: Mapping[str, Any]) -> tuple[list[dict[str, Any]], bool]:
    complete = resources.get("device_claims")
    if isinstance(complete, list):
        return complete, True
    # Legacy exports still provide useful claim rows, but cannot prove absence.
    rows = list(resources.get("stale_devices") or [])
    for agent in host.get("agents") or []:
        holder = {key: agent[key] for key in HOLDER_FIELDS if key in agent}
        rows.extend({**claim, "holder": holder, "stale": False}
                    for claim in agent.get("devices") or [])
    return rows, False


def device_board(status_snapshot: Mapping[str, Any], *, now: float | None = None) -> dict[str, Any]:
    """Reconcile claims and observations without I/O or mutation of the snapshot.

    Counts are null when a source is unavailable. Admission falls back to live
    Android claims when ps is unavailable, exactly as the claim gate does, but
    exposes ``known=False`` rather than presenting that fallback as a full count.
    """
    at = time.time() if now is None else now
    hosts = []
    for host in status_snapshot.get("hosts", []):
        canonical = host["host"]
        fresh = not bool(host.get("stale"))
        resources = host.get("resources") or {}
        observations = resources.get("devices_observed") or {}
        claim_host = {**host, "agents": host.get("agents") or [
            agent for agent in status_snapshot.get("agents", []) if agent.get("host") == canonical]}
        claims, complete = _claims(claim_host, resources)
        adb = observations.get("adb") if fresh else None
        processes = observations.get("emulator_processes") if fresh else None
        simulators = observations.get("ios_simulators_booted") if fresh else None
        can_exclude_emulator = processes is not None and all(process.get("port") is not None
                                                            for process in processes)
        rows: dict[tuple[str, str | int], dict[str, Any]] = {}

        def row_for(device_id, platform, *, process_pid=None):
            identity = device_id.lower() if platform in {"ios-device", "ios-simulator"} else device_id
            key = ("process", process_pid) if device_id is None else ("device", identity)
            if key not in rows:
                rows[key] = {"host": canonical, "platform": platform, "device_id": device_id,
                             "label": None, "holder": None, "claimed_at": None, "age": None,
                             "claim_state": None, "observed_state": "unknown", "finding": None}
                if process_pid is not None:
                    rows[key]["process_pid"] = process_pid
            return rows[key]

        live_android_claims = 0
        for claim in claims:
            device_id = claim["device_id"]
            platform = classify_device(claim.get("kind"), device_id)
            row = row_for(device_id, platform)
            holder = claim.get("holder")
            if holder is not None:
                holder = {key: holder[key] for key in HOLDER_FIELDS if key in holder}
            stale = bool(claim.get("stale")) or (holder is not None and
                    (holder.get("live") is False or holder.get("active") is False))
            claimed_at = claim.get("claimed_at")
            row.update(label=claim.get("label"), holder=holder, claimed_at=claimed_at,
                       age=max(0, at - claimed_at) if isinstance(claimed_at, (int, float)) else None,
                       claim_state="stale" if stale else "held")
            if (platform == "android-emulator" and claim.get("kind") == "emu"
                    and not stale and holder is not None and holder.get("host") == canonical):
                live_android_claims += 1
            source = (adb if platform in {"android-device", "android-emulator"} else
                      simulators if platform == "ios-simulator" else None)
            if source is not None and (platform != "android-emulator" or can_exclude_emulator):
                row["observed_state"] = "not seen"

        for observation in adb or []:
            serial = observation["serial"]
            platform = classify_device("phone", serial)
            row = row_for(serial, platform)
            state = observation.get("state")
            row["observed_state"] = "attached" if state == "device" else "offline"

        for process in processes or []:
            port = process.get("port")
            device_id = f"emulator-{port}" if port is not None else None
            row = row_for(device_id, "android-emulator" if device_id else "unknown",
                          process_pid=process["pid"])
            if device_id is None:
                row["label"] = f"Android emulator process {process['pid']} (port unknown)"
            row["observed_state"] = "running" if row["observed_state"] != "attached" else "attached"

        for simulator in simulators or []:
            row = row_for(simulator["udid"], "ios-simulator")
            row["observed_state"] = "booted"
            if not row["label"]:
                row["label"] = simulator.get("name")

        for row in rows.values():
            state = row["observed_state"]
            if row["claim_state"] == "stale":
                row["finding"] = "stale"
            elif row["holder"] is not None and state == "not seen":
                row["finding"] = "missing"
            elif (row["holder"] is None and row["device_id"] is not None
                  and state in {"attached", "booted", "running", "offline"} and complete):
                row["finding"] = "unclaimed"

        counts = {platform: None for platform in PLATFORMS}
        available = {"android-device": adb is not None,
                     "android-emulator": adb is not None and processes is not None,
                     "ios-device": observations.get("ios_devices") is not None and fresh,
                     "ios-simulator": simulators is not None,
                     "unknown": processes is not None}
        if complete:
            for platform, known in available.items():
                if known:
                    counts[platform] = sum(row["platform"] == platform for row in rows.values())
        process_count = len(processes) if processes is not None else None
        in_use = max(live_android_claims, process_count or 0) if complete and fresh else None
        hosts.append({"host": canonical, "fresh": fresh,
                      "error": host.get("error") or observations.get("error"),
                      "emu_limit": {"limit": resources.get("emu_limit"), "in_use": in_use,
                                    "claim_count": live_android_claims if complete and fresh else None,
                                    "process_count": process_count,
                                    "known": complete and fresh and processes is not None},
                      "counts": counts,
                      "devices": sorted(rows.values(), key=lambda row: (
                          row["platform"], row["device_id"] or "", row.get("process_pid", 0)))})
    return {"hosts": hosts}


def format_device_board(board: Mapping[str, Any]) -> str:
    """Render host/platform groups without implying unavailable counts are zero."""
    from ham.ledger.status import sanitize_status_text

    def text(value):
        value = str(value or "")
        return sanitize_status_text(value, len(value))

    lines = []
    for host in board.get("hosts", []):
        limit = host["emu_limit"]
        used = "?" if limit["in_use"] is None else str(limit["in_use"])
        if limit["in_use"] is not None and not limit["known"]:
            used += " (claims only; processes unknown)"
        maximum = "?" if limit["limit"] is None else ("unlimited" if limit["limit"] <= 0 else str(limit["limit"]))
        lines.append(f"{text(host['host'])} [{'fresh' if host['fresh'] else 'stale'}] Android emulators: {used}/{maximum}")
        if host["error"]:
            error = host["error"]
            if isinstance(error, Mapping):
                error = f"{text(error.get('code'))}: {text(error.get('message'))}"
            lines.append(f"  error: {text(error)}")
        for platform in PLATFORMS:
            devices = [row for row in host["devices"] if row["platform"] == platform]
            count = host["counts"][platform]
            lines.append(f"  {platform} ({'?' if count is None else count})")
            for row in devices:
                holder = row["holder"]
                owner = (f"{text(holder.get('alias', '?'))} ({text(holder.get('pane_id', '?'))}; "
                         f"{text(holder.get('repo_id', '?'))})" if holder else "-")
                identity = text(row["device_id"] or f"pid {row['process_pid']} (port unknown)")
                label = f" [{text(row['label'])}]" if row["label"] else ""
                finding = f" !{text(row['finding'])}" if row["finding"] else ""
                age = f"; {row['age']:.0f}s" if row["age"] is not None else ""
                lines.append(f"    {identity}{label}: {text(row['observed_state'])}; {owner}{age}{finding}")
    return "\n".join(lines) if lines else "No registered hosts."
