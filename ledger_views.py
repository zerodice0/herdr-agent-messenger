"""Read-only local and verified-peer views of the mailbox ledger."""

from __future__ import annotations

from typing import Any, Mapping

from ledger_remote import RemoteLedger
from ledger_store import LedgerError, LedgerStore


def status_board(store: LedgerStore, *, repo_id: str | None = None,
                 host: str | None = None, environment: Mapping[str, str] | None = None,
                 previous: Mapping[str, Any] | None = None, transport: Any = None,
                 resource_scanner: Any = None) -> dict[str, Any]:
    """Read stored status without registry refresh, replication, or durable caching.

    ``previous`` is an optional in-memory board snapshot. Only rows belonging to
    the same registered host UUID can survive a failed peer read, marked stale
    with unknown unread counts. Without it, a failed host has no agent rows.
    """
    remote = RemoteLedger(store, environment, transport)
    include_local = host is None or host in {"local", store.host}
    peers = remote.peers() if host is None else ([] if include_local else [remote.peer(host)])
    hosts: list[dict[str, Any]] = []
    agents: list[dict[str, Any]] = []
    targets = ([{"canonical_name": store.host, "uuid": store.host_uuid}] if include_local else []) + peers
    for target in targets:
        canonical = target["canonical_name"]
        local = canonical == store.host
        error = None
        resources = None
        try:
            exported = (remote.status_export(repo_id, resources=True, resource_scanner=resource_scanner) if local else
                        remote.query_status(canonical, repo_id, refresh=False, resources=True))
            resources = exported.get("resources")
            rows = [{**row, "stale": False} for row in exported["agents"]
                    if repo_id is None or row["repo_id"] == repo_id]
            if any(row["host"] != canonical for row in rows):
                raise LedgerError("peer_identity_conflict", "Status rows do not belong to the verified host.")
            unread: int | str = sum(row["unread"] for row in rows)
        except LedgerError as failure:
            if local:
                raise
            error = {"code": failure.code, "message": failure.message}
            verified_previous = previous is not None and any(
                item.get("host") == canonical and item.get("uuid") == target["uuid"]
                for item in previous.get("hosts", []))
            rows = ([{**row, "stale": True, "unread": "?"}
                     for row in previous.get("agents", [])
                     if row.get("host") == canonical and
                     (repo_id is None or row.get("repo_id") == repo_id)]
                    if verified_previous else [])
            if verified_previous:
                resources = next((item.get("resources") for item in previous.get("hosts", [])
                                  if item.get("host") == canonical), None)
            unread = "?"
        hosts.append({"host": canonical, "uuid": target["uuid"], "local": local,
                      "stale": error is not None, "error": error, "unread": unread,
                      "agents": rows, **({"resources": resources} if resources is not None else {})})
        agents.extend(rows)
    return {"repo_id": repo_id, "agents": agents, "hosts": hosts}
