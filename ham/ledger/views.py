"""Read-only local and verified-peer views of the mailbox ledger."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, wait
from typing import Any, Mapping

from ham.ledger.remote import RemoteLedger
from ham.ledger.status import sanitize_agent_status
from ham.ledger.store import LedgerError, LedgerStore

PEER_STATUS_TIMEOUT_SECONDS = 20.0


def _peer_exports(remote: RemoteLedger, peers: list[dict[str, Any]],
                  repo_id: str | None) -> dict[str, Any]:
    """One shared deadline, at most four simultaneous read-only SSH queries."""
    if not peers:
        return {}
    executor = ThreadPoolExecutor(max_workers=min(4, len(peers)), thread_name_prefix="ham-board")
    futures = {executor.submit(remote.query_status, peer["canonical_name"], repo_id,
                               refresh=False, resources=True): peer["canonical_name"] for peer in peers}
    try:
        completed, _ = wait(futures, timeout=PEER_STATUS_TIMEOUT_SECONDS)
        exported = {}
        for future, canonical in futures.items():
            if future not in completed:
                future.cancel()
                exported[canonical] = LedgerError("transport_unavailable", "Peer status deadline reached.")
            else:
                try:
                    exported[canonical] = future.result()
                except LedgerError as failure:
                    exported[canonical] = failure
        return exported
    finally:
        # A slow custom transport cannot turn the aggregate deadline into a join.
        # Production SSH has its own 20-second subprocess timeout.
        executor.shutdown(wait=False, cancel_futures=True)


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
    peer_exports = _peer_exports(remote, peers, repo_id)
    backlog = remote.pending_backlog()
    hosts: list[dict[str, Any]] = []
    agents: list[dict[str, Any]] = []
    workspaces: list[dict[str, Any]] = []
    targets = ([{"canonical_name": store.host, "uuid": store.host_uuid}] if include_local else []) + peers
    for target in targets:
        canonical = target["canonical_name"]
        local = canonical == store.host
        error = None
        resources = None
        host_workspaces: list[dict[str, Any]] = []
        try:
            exported = (remote.status_export(repo_id, resources=True, resource_scanner=resource_scanner) if local else
                        peer_exports[canonical])
            if isinstance(exported, LedgerError):
                raise exported
            resources = exported.get("resources")
            rows = [{**sanitize_agent_status(row), "stale": False} for row in exported["agents"]
                    if repo_id is None or row["repo_id"] == repo_id]
            if any(row["host"] != canonical for row in rows):
                raise LedgerError("peer_identity_conflict", "Status rows do not belong to the verified host.")
            host_workspaces = exported.get("workspaces", [])
            if any(row.get("host") != canonical for row in host_workspaces):
                raise LedgerError("peer_identity_conflict", "Workspace rows do not belong to the verified host.")
            unread: int | str = sum(row["unread"] for row in rows)
        except LedgerError as failure:
            host_workspaces = []
            if local:
                raise
            error = {"code": failure.code, "message": failure.message}
            verified_previous = previous is not None and any(
                item.get("host") == canonical and item.get("uuid") == target["uuid"]
                for item in previous.get("hosts", []))
            rows = ([{**sanitize_agent_status(row), "stale": True, "unread": "?"}
                     for row in previous.get("agents", [])
                     if row.get("host") == canonical and
                     (repo_id is None or row.get("repo_id") == repo_id)]
                    if verified_previous else [])
            if verified_previous:
                resources = next((item.get("resources") for item in previous.get("hosts", [])
                                  if item.get("host") == canonical), None)
                host_workspaces = [row for row in previous.get("workspaces", []) if row.get("host") == canonical]
            unread = "?"
        hosts.append({"host": canonical, "uuid": target["uuid"], "local": local,
                      "stale": error is not None, "error": error, "unread": unread,
                      "agents": rows, **({"resources": resources} if resources is not None else {}),
                      **({"outbox_pending": backlog[canonical]} if canonical in backlog else {})})
        agents.extend(rows)
        workspaces.extend(host_workspaces)
    return {"repo_id": repo_id, "agents": agents, "hosts": hosts, "workspaces": workspaces}
