"""Authenticated-by-config SSH wire service and durable per-peer FIFO transport.

Peers are accident-prevention boundaries for the same user's SSH account, not an
independent authorization system. No SQLite or AGENTS replication occurs.
"""

from __future__ import annotations

import json
import os
from pathlib import Path, PurePosixPath
import subprocess
import time
from typing import Any, Mapping

from agent_directory import ssh_command, ssh_config_path, ssh_hosts, ssh_program_command
from ledger_mail import Mailbox, payload_hash, validate_payload
from ledger_settings import REPO_FIELDS, RepoSettings
from ledger_status import AgentRegistry
from ledger_store import (FINAL_STATES, STATE_RANK, LedgerError, LedgerStore, json_text,
                          public_receipt, queue_outbox, receipt, require_uuid, save_receipt)

PROTOCOL = 1
TRANSIENT_CODES = frozenset({"transport_unavailable", "host_unavailable", "ledger_unavailable", "busy"})
WIRE_COMMANDS = frozenset({"accept", "receipt", "setting", "withdraw"})


class SSHTransport:
    def __init__(self, store: LedgerStore, environment: Mapping[str, str] | None = None) -> None:
        self.store = store
        self.environment = environment

    def _run(self, command: list[str], data: Mapping[str, Any] | None = None) -> dict[str, Any]:
        try:
            result = subprocess.run(command, input=None if data is None else json_text(data),
                                    capture_output=True, text=True, timeout=20, check=False)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise LedgerError("transport_unavailable", str(error)) from None
        try:
            response = json.loads(result.stdout)
            if not isinstance(response, dict):
                raise ValueError
        except (ValueError, RecursionError):
            raise LedgerError("transport_unavailable" if result.returncode == 255 else "invalid_wire_response",
                              result.stderr.strip() or "Remote endpoint did not return one JSON object.") from None
        if "error" in response:
            error = response["error"]
            if not isinstance(error, dict):
                raise LedgerError("invalid_wire_response", "Remote error must use the public JSON error object.")
            raise LedgerError(str(error.get("code", "remote_error")), str(error.get("message", "Remote refusal.")))
        if result.returncode:
            raise LedgerError("transport_unavailable", result.stderr.strip() or "SSH command failed.")
        return response

    def _allowed(self, alias: str) -> None:
        if alias not in ssh_hosts(self.environment):
            raise LedgerError("host_not_configured", "SSH alias is not in HAM's configured concrete-host allowlist.")

    def discover(self, alias: str, *, remote_root: str | None = None,
                 remote_ledger_dir: str = "", remote_socket: str = "") -> dict[str, Any]:
        self._allowed(alias)
        if remote_root is None:
            payload = self._run(ssh_command(alias, ["plugin", "list", "--json", "--plugin", "herdr.agent-messenger"],
                                           config_path=ssh_config_path(self.environment)))
            try:
                remote_root = payload["result"]["plugins"][0]["plugin_root"]
            except (KeyError, IndexError, TypeError):
                raise LedgerError("remote_root_unavailable", "Remote Herdr did not report result.plugins[0].plugin_root.") from None
        if not isinstance(remote_root, str) or not PurePosixPath(remote_root).is_absolute():
            raise LedgerError("invalid_remote_root", "Remote HAM root must be an absolute checkout path.")
        if remote_ledger_dir and not PurePosixPath(remote_ledger_dir).is_absolute():
            raise LedgerError("invalid_remote_ledger", "Remote isolated ledger directory must be absolute.")
        if remote_socket and not PurePosixPath(remote_socket).is_absolute():
            raise LedgerError("invalid_remote_socket", "Remote isolated socket must be absolute.")
        peer = {"ssh_alias": alias, "remote_ham_root": remote_root,
                "remote_ledger_dir": remote_ledger_dir, "remote_socket": remote_socket}
        doctor = self._invoke(peer, "doctor")
        if doctor.get("protocol") != PROTOCOL:
            raise LedgerError("unsupported_protocol", "Remote checkout does not support mailbox protocol 1.")
        try:
            identity = doctor["host"]
            require_uuid(identity["uuid"], "host_uuid")
            if not isinstance(identity["canonical_name"], str) or not identity["canonical_name"]:
                raise KeyError
        except (KeyError, TypeError):
            raise LedgerError("invalid_host", "Remote doctor did not provide a canonical host and UUID.") from None
        return {**peer, "canonical_name": identity["canonical_name"], "uuid": identity["uuid"]}

    def _invoke(self, peer: Mapping[str, Any], command: str,
                data: Mapping[str, Any] | None = None) -> dict[str, Any]:
        self._allowed(peer["ssh_alias"])
        arguments = ["env"]
        if peer.get("remote_ledger_dir"):
            arguments.append("HERDR_AGENT_LEDGER_DIR=" + peer["remote_ledger_dir"])
        if peer.get("remote_socket"):
            arguments.append("HERDR_SOCKET_PATH=" + peer["remote_socket"])
        values = self.environment if self.environment is not None else os.environ
        if peer.get("remote_ledger_dir") and values.get("HERDR_AGENT_LEDGER_AUTOSTART") == "0":
            arguments.append("HERDR_AGENT_LEDGER_AUTOSTART=0")
        arguments += ["python3", str(PurePosixPath(peer["remote_ham_root"]) / "ledger_cli.py"), "ledger", command]
        if command == "status-export":
            arguments.append("--wire")
        return self._run(ssh_program_command(peer["ssh_alias"], arguments,
                                            config_path=ssh_config_path(self.environment)), data)

    def call(self, peer: Mapping[str, Any], command: str,
             payload: Mapping[str, Any]) -> dict[str, Any]:
        envelope = {"protocol": PROTOCOL, "transport_host": self.store.host,
                    "transport_uuid": self.store.host_uuid, "payload": payload}
        result = self._invoke(peer, command, envelope)
        if result.get("protocol") != PROTOCOL:
            raise LedgerError("unsupported_protocol", "Remote endpoint did not return mailbox protocol 1.")
        return result


class RemoteLedger:
    def __init__(self, store: LedgerStore, environment: Mapping[str, str] | None = None,
                 transport: Any = None) -> None:
        self.store = store
        self.environment = environment
        self.transport = transport or SSHTransport(store, environment)

    def peers(self) -> list[dict[str, Any]]:
        with self.store.read() as connection:
            return [dict(row) for row in connection.execute(
                "SELECT p.*,coalesce(o.remote_ledger_dir,'') AS remote_ledger_dir,"
                "coalesce(o.remote_socket,'') AS remote_socket FROM peers p "
                "LEFT JOIN peer_options o USING(canonical_name) ORDER BY p.canonical_name")]

    def add_peer(self, *, canonical_name: str, uuid: str, ssh_alias: str,
                 remote_ham_root: str, remote_ledger_dir: str = "", remote_socket: str = "") -> dict[str, Any]:
        require_uuid(uuid, "host_uuid")
        if canonical_name == self.store.host or uuid == self.store.host_uuid:
            raise LedgerError("self_peer", "The local host cannot be registered as a peer.")
        if not canonical_name or not remote_ham_root or not PurePosixPath(remote_ham_root).is_absolute():
            raise LedgerError("invalid_peer", "Canonical name and absolute remote checkout root are required.")
        if ((remote_ledger_dir and not PurePosixPath(remote_ledger_dir).is_absolute()) or
                (remote_socket and not PurePosixPath(remote_socket).is_absolute())):
            raise LedgerError("invalid_peer", "Remote ledger/socket overrides must be absolute.")
        if ssh_alias not in ssh_hosts(self.environment):
            raise LedgerError("host_not_configured", "SSH alias is not in the existing HAM SSH allowlist.")
        with self.store.transaction() as connection:
            existing = connection.execute("SELECT uuid FROM peers WHERE canonical_name=?", (canonical_name,)).fetchone()
            reused_uuid = connection.execute("SELECT canonical_name FROM peers WHERE uuid=?", (uuid,)).fetchone()
            if (existing and existing[0] != uuid) or (reused_uuid and reused_uuid[0] != canonical_name):
                raise LedgerError("peer_identity_conflict", "A registered canonical host/UUID cannot silently change.")
            connection.execute("INSERT INTO peers VALUES (?,?,?,?) ON CONFLICT(canonical_name) DO UPDATE SET "
                               "ssh_alias=excluded.ssh_alias,remote_ham_root=excluded.remote_ham_root",
                               (canonical_name, uuid, ssh_alias, remote_ham_root))
            connection.execute("INSERT INTO peer_options VALUES (?,?,?) ON CONFLICT(canonical_name) DO UPDATE SET "
                               "remote_ledger_dir=excluded.remote_ledger_dir,remote_socket=excluded.remote_socket",
                               (canonical_name, remote_ledger_dir, remote_socket))
            # Bootstrap only settings. Agent/status records are always queried live.
            if existing is None:
                repos = [row[0] for row in connection.execute("SELECT repo_id FROM repos WHERE repo_id NOT LIKE 'local:%'")]
                for repo_id in repos:
                    for field, setting in RepoSettings.fields(connection, repo_id).items():
                        if field in REPO_FIELDS:
                            queue_outbox(connection, canonical_name, "setting", setting)
        return next(peer for peer in self.peers() if peer["canonical_name"] == canonical_name)

    def peer(self, address_host: str) -> dict[str, Any]:
        matches = [peer for peer in self.peers() if address_host in {peer["canonical_name"], peer["ssh_alias"]}]
        if len(matches) != 1:
            raise LedgerError("peer_not_configured", "Address host must identify one canonical peer or its SSH alias.")
        return matches[0]

    def envelope(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        return {"protocol": PROTOCOL, "transport_host": self.store.host,
                "transport_uuid": self.store.host_uuid, "payload": dict(payload)}

    def authenticate(self, envelope: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
        if set(envelope) != {"protocol", "transport_host", "transport_uuid", "payload"} or envelope["protocol"] != PROTOCOL:
            raise LedgerError("unsupported_protocol", "Expected a protocol 1 transport envelope.")
        with self.store.read() as connection:
            peer = connection.execute("SELECT uuid FROM peers WHERE canonical_name=?", (envelope["transport_host"],)).fetchone()
        if peer is None or peer[0] != envelope["transport_uuid"]:
            raise LedgerError("peer_identity_conflict", "Transport canonical name/UUID does not match a registered peer.")
        if not isinstance(envelope["payload"], dict):
            raise LedgerError("invalid_request", "Wire payload must be a JSON object.")
        return envelope["transport_host"], envelope["payload"]

    def receive(self, command: str, envelope: Mapping[str, Any]) -> dict[str, Any]:
        source, payload = self.authenticate(envelope)
        if command == "accept":
            result = self.accept(payload, source)
        elif command == "receipt":
            with self.store.transaction() as connection:
                result = self.apply_receipt(connection, payload, source)
        elif command == "setting":
            result = RepoSettings(self.store, environment=self.environment).accept(payload, source)
        elif command == "withdraw":
            result = self.accept_withdraw(payload, source)
        elif command == "status-export":
            result = self.status_export(payload.get("repo_id"), resources=payload.get("resources") is True)
        else:
            raise LedgerError("invalid_command", "Unsupported wire command.")
        return {"protocol": PROTOCOL, **result}

    def status_export(self, repo_id: str | None = None, *, resources: bool = False,
                      resource_scanner: Any = None) -> dict[str, Any]:
        settings = RepoSettings(self.store)
        all_agents = AgentRegistry(self.store).list()
        agents = [agent for agent in all_agents if repo_id is None or agent["repo_id"] == repo_id]
        from ledger_devices import device_rows
        servers: dict[str, list[dict[str, Any]]] = {}
        host_resources = None
        if resources:
            import resource_scan
            scanned = (resource_scanner or resource_scan.scan_host_resources)()
            servers, host_resources = resource_scan.attribute(
                scanned["listeners"], scanned["serve"], all_agents, host=self.store.host,
                listener_endpoints=scanned.get("listener_endpoints"))
        with self.store.read() as connection:
            claims = device_rows(connection)
            counts = {(row["recipient_host"], row["recipient_terminal_id"], row["repo_id"]): row["unread"]
                      for row in connection.execute(
                          "SELECT d.recipient_host,d.recipient_terminal_id,m.repo_id,count(*) AS unread "
                          "FROM deliveries d JOIN messages m USING(message_id) "
                          "WHERE d.state IN ('queued','delivering') GROUP BY d.recipient_host,"
                          "d.recipient_terminal_id,m.repo_id")}
        exported = [{**agent, "role": settings.role(agent),
                     "unread": counts.get((agent["host"], agent["terminal_id"], agent["repo_id"]), 0),
                     "devices": [row for row in claims if not row["stale"]
                                 and row["holder"]["agent_id"] == agent["agent_id"]],
                     **({"servers": servers.get(agent["agent_id"], [])} if resources else {})}
                    for agent in agents]
        result = {"host": {"canonical_name": self.store.host, "uuid": self.store.host_uuid},
                  "agents": exported}
        if host_resources is not None:
            result["resources"] = {"servers_unowned": host_resources["unowned_served"],
                                   "dead_serve": host_resources["dead_serve"],
                                   "stale_devices": [row for row in claims if row["stale"]],
                                   "error": scanned["error"]}
        return result

    def query_status(self, host: str, repo_id: str | None = None, *,
                     refresh: bool = True, resources: bool = False) -> dict[str, Any]:
        peer = self.peer(host)
        payload: dict[str, Any] = {"repo_id": repo_id}
        if not refresh:
            payload["refresh"] = False
        if resources:
            payload["resources"] = True
        result = self.transport.call(peer, "status-export", payload)
        if result.get("host") != {"canonical_name": peer["canonical_name"], "uuid": peer["uuid"]}:
            raise LedgerError("peer_identity_conflict", "Live status endpoint identity has changed.")
        return result

    def resolve_recipient(self, address: str, sender: Mapping[str, Any]) -> dict[str, Any]:
        if address == "main":
            with self.store.read() as connection:
                selected = RepoSettings(self.store).main(
                    connection, sender["repo_id"], sender["host"], sender["workspace_id"])
                agent = (connection.execute(
                    "SELECT * FROM agents WHERE host=? AND terminal_id=? AND active=1",
                    (selected["host"], selected["terminal_id"])).fetchone()
                    if selected is not None else None)
            if agent is None:
                raise LedgerError(
                    "main_unresolved",
                    f"No main is available in workspace {sender['workspace_id']} on {sender['host']}.")
            return {**dict(agent), "recipient_address": "main"}
        parts = address.split('/')
        if len(parts) != 2 or not all(parts):
            raise LedgerError("invalid_address", "Mailbox address must be main or HOST/LABEL.")
        host, label = parts
        if host in {"local", self.store.host}:
            agents = AgentRegistry(self.store).list()
        else:
            agents = self.query_status(host, sender["repo_id"])["agents"]
        matches = [agent for agent in agents if agent["alias"] == label]
        if len(matches) != 1:
            raise LedgerError("agent_not_found" if not matches else "agent_ambiguous", "Recipient label must identify one live agent.")
        return matches[0]

    def submit(self, connection: Any, value: Mapping[str, Any], *, upstream: str | None = None,
               resolved: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """Store once; new main addresses already carry their concrete target."""
        digest = payload_hash(value)
        old = connection.execute("SELECT payload_hash FROM dedupe_receipts WHERE message_id=?", (value["message_id"],)).fetchone()
        if old:
            if old[0] != digest:
                raise LedgerError("message_conflict", "This UUID already identifies a different immutable message.")
            return public_receipt(receipt(connection, value["message_id"]))
        if value["recipient_terminal_id"] is None and value["recipient_address"] == "main":
            raise LedgerError("legacy_symbolic_main",
                              "Legacy symbolic --to main is unsupported; upgrade every host before using --to main across hosts.")
        target = dict(resolved or {key: value[key] for key in ("recipient_host", "recipient_terminal_id", "recipient_address")})
        forward = target["recipient_host"] if target["recipient_host"] != self.store.host else None
        if forward:
            self.peer(forward)
        result = Mailbox(self.store)._enqueue(connection, value, resolved=target, upstream=upstream, forward=forward)
        if forward and result["state"] not in FINAL_STATES:
            queue_outbox(connection, forward, "message", {"message": dict(value), "resolved": target}, value["message_id"])
        return public_receipt(result)

    def accept(self, payload: Mapping[str, Any], source: str) -> dict[str, Any]:
        if set(payload) != {"message", "resolved"} or not isinstance(payload["message"], dict):
            raise LedgerError("invalid_message", "Accept requires immutable message and a routing resolution.")
        value = validate_payload(payload["message"])
        if value["recipient_terminal_id"] is None and value["recipient_address"] == "main":
            raise LedgerError("legacy_symbolic_main",
                              "Legacy symbolic --to main is unsupported; upgrade every host before using --to main across hosts.")
        resolved = payload["resolved"]
        if not isinstance(resolved, dict) or set(resolved) != {"recipient_host", "recipient_terminal_id", "recipient_address"}:
            raise LedgerError("invalid_recipient", "Invalid routing resolution.")
        if resolved["recipient_host"] != self.store.host:
            raise LedgerError("wrong_host", "An accept endpoint cannot forward an arbitrary concrete host.")
        if (value["recipient_terminal_id"] is not None or value["recipient_address"] != "main") and resolved != {
                key: value[key] for key in ("recipient_host", "recipient_terminal_id", "recipient_address")}:
            raise LedgerError("invalid_recipient", "Concrete routing cannot alter the immutable message recipient.")
        if source != value["origin_host"]:
            raise LedgerError("wrong_origin", "Only the message origin can transport this message.")
        with self.store.transaction((resolved["recipient_terminal_id"],)) as connection:
            return self.submit(connection, value, upstream=source, resolved=resolved)

    def apply_receipt(self, connection: Any, incoming: Mapping[str, Any], source: str) -> dict[str, Any]:
        message_id = require_uuid(incoming.get("message_id"))
        old = connection.execute("SELECT * FROM dedupe_receipts WHERE message_id=?", (message_id,)).fetchone()
        if old is None:
            raise LedgerError("message_not_found", "Receipt has no originating/relay message.")
        if incoming.get("payload_hash") != old["payload_hash"] or incoming.get("origin_host") != old["origin_host"]:
            raise LedgerError("message_conflict", "Receipt immutable hash/origin does not match.")
        route = connection.execute("SELECT * FROM message_routes WHERE message_id=?", (message_id,)).fetchone()
        current = receipt(connection, message_id)
        if route is None or route["forward_host"] != source:
            raise LedgerError("wrong_receipt_host", "Receipt did not come through this message's saved route.")
        state = incoming.get("state")
        if state not in STATE_RANK:
            raise LedgerError("invalid_receipt", "Only public delivery states are accepted.")
        confirmation_source = incoming.get("confirmation_source")
        incoming_evidence = incoming.get("evidence")
        if confirmation_source is not None and confirmation_source not in ("native", "operator"):
            raise LedgerError("invalid_receipt", "Receipt confirmation_source must be native, operator, or null.")
        if incoming_evidence is not None and not isinstance(incoming_evidence, str):
            raise LedgerError("invalid_receipt", "Receipt evidence must be text or null.")
        if confirmation_source == "operator" and (
                not isinstance(incoming_evidence, str) or not incoming_evidence.strip()):
            raise LedgerError("invalid_receipt", "Operator confirmation requires evidence.")
        if state != "pending" and incoming.get("authoritative_host") != incoming.get("recipient_host"):
            raise LedgerError("invalid_receipt", "A public advancement requires the authoritative final host.")
        # A local authoritative receiver owns its claim/ACK lifecycle.
        if current["authoritative"] and current["recipient_host"] == self.store.host:
            return public_receipt(current)
        if (route["resolution_terminal"] is not None and
                (incoming.get("recipient_host"), incoming.get("recipient_terminal_id"), incoming.get("recipient_address")) !=
                (route["resolution_host"], route["resolution_terminal"], route["resolution_address"])):
            raise LedgerError("invalid_receipt", "A resolved message cannot change its recipient terminal/address.")
        # Already-final states cannot conflict-transition, even to a higher rank.
        # Internal claim states are not public receipt ranks; preserve them.
        if current["state"] not in STATE_RANK:
            return public_receipt(current)
        if current["state"] in FINAL_STATES or STATE_RANK[state] < STATE_RANK[current["state"]]:
            return public_receipt(current)
        if current["state"] == "delivered" and state not in {"delivered", "handled"}:
            return public_receipt(current)
        if state == current["state"] and incoming.get("updated_at", 0) <= current["updated_at"]:
            return public_receipt(current)
        if incoming.get("recipient_terminal_id") is None and state not in {"pending", "withdrawn"}:
            raise LedgerError("invalid_receipt", "Final recipient resolution must identify a terminal.")
        row = connection.execute("SELECT 1 FROM messages WHERE message_id=?", (message_id,)).fetchone()
        if row is None:
            return public_receipt(current)
        at = incoming.get("updated_at", time.time())
        delivered_at = current["delivered_at"] or incoming.get("delivered_at")
        handled_at = current["handled_at"] or incoming.get("handled_at")
        existing_confirmation = current.get("confirmation_source")
        existing_evidence = current.get("evidence")
        if existing_confirmation == "operator" and confirmation_source != "operator":
            confirmation_source, evidence = existing_confirmation, existing_evidence
        elif confirmation_source is None:
            confirmation_source, evidence = existing_confirmation, existing_evidence
        else:
            evidence = incoming_evidence if incoming_evidence is not None else existing_evidence
        via = current.get("via") or incoming.get("via")
        delivered_agent_id = current.get("delivered_agent_id") or incoming.get("delivered_agent_id")
        connection.execute("UPDATE deliveries SET state=?,recipient_host=?,recipient_terminal_id=?,recipient_address=?,"
                           "hold_reason=?,via=?,delivered_agent_id=?,delivered_at=?,handled_at=?,confirmation_source=?,evidence=?,updated_at=?,"
                           "withdraw_requested=CASE WHEN ? THEN 0 ELSE withdraw_requested END WHERE message_id=?",
                           (state, incoming["recipient_host"], incoming.get("recipient_terminal_id"), incoming["recipient_address"],
                            incoming.get("hold_reason"), via, delivered_agent_id, delivered_at,
                            handled_at, confirmation_source, evidence, at, state in {"delivered", *FINAL_STATES}, message_id))
        if incoming.get("recipient_terminal_id") is not None:
            connection.execute("UPDATE message_routes SET resolution_host=?,resolution_terminal=?,resolution_address=? WHERE message_id=?",
                               (incoming["recipient_host"], incoming["recipient_terminal_id"], incoming["recipient_address"], message_id))
        if state in FINAL_STATES:
            connection.execute("UPDATE messages SET final_at=coalesce(final_at,?) WHERE message_id=?", (at, message_id))
        return public_receipt(save_receipt(connection, message_id))

    def withdraw_copy(self, connection: Any, message_id: str) -> dict[str, Any]:
        current = receipt(connection, message_id)
        if current["state"] in FINAL_STATES:
            return {**public_receipt(current), "withdrawn": current["state"] == "withdrawn"}
        if current["state"] == "delivered":
            return {**public_receipt(current), "withdrawn": False, "reason": "already_delivered"}
        route = connection.execute("SELECT * FROM message_routes WHERE message_id=?", (message_id,)).fetchone()
        items = connection.execute("SELECT * FROM outbox WHERE message_id=? AND kind='message'", (message_id,)).fetchall()
        if items and all(item["attempts"] == 0 and item["state"] == "pending" for item in items):
            connection.execute("UPDATE outbox SET state='cancelled' WHERE message_id=? AND kind='message' AND state='pending'", (message_id,))
            at = time.time()
            connection.execute("UPDATE deliveries SET state='withdrawn',updated_at=?,withdraw_requested=0 WHERE message_id=?", (at, message_id))
            connection.execute("UPDATE messages SET final_at=coalesce(final_at,?) WHERE message_id=?", (at, message_id))
            connection.execute("INSERT OR IGNORE INTO withdraw_tombstones VALUES (?,?,?)", (message_id, current["origin_host"], at))
            return {**public_receipt(save_receipt(connection, message_id)), "withdrawn": True}
        if not current["withdraw_requested"]:
            connection.execute("UPDATE deliveries SET withdraw_requested=1 WHERE message_id=?", (message_id,))
            queue_outbox(connection, route["forward_host"], "withdraw", public_receipt(current), message_id)
        return {**public_receipt(receipt(connection, message_id)), "withdrawn": False, "withdraw_requested": True}

    def accept_withdraw(self, payload: Mapping[str, Any], source: str) -> dict[str, Any]:
        message_id = require_uuid(payload.get("message_id"))
        origin = payload.get("origin_host")
        terminals: list[str] = []
        with self.store.transaction(terminals) as connection:
            route = connection.execute("SELECT * FROM message_routes WHERE message_id=?", (message_id,)).fetchone()
            old = connection.execute("SELECT * FROM dedupe_receipts WHERE message_id=?", (message_id,)).fetchone()
            mailbox = Mailbox(self.store)
            if old:
                if payload.get("payload_hash") != old["payload_hash"] or origin != old["origin_host"]:
                    raise LedgerError("message_conflict", "Withdrawal hash/origin does not match this message.")
                if route and route["receipt_host"] != source:
                    raise LedgerError("wrong_origin", "Withdrawal did not follow the saved upstream route.")
                result = mailbox._withdraw(connection, message_id, origin_host=origin)
                if result.get("recipient_terminal_id"):
                    terminals.append(result["recipient_terminal_id"])
                return public_receipt(result)
            if source != origin:
                raise LedgerError("wrong_origin", "Unknown withdrawal requires the message origin.")
            compact = {key: payload.get(key) for key in ("message_id", "origin_host", "payload_hash", "recipient_address",
                       "recipient_host", "recipient_terminal_id", "via", "delivered_agent_id", "delivered_at", "handled_at")}
            compact.update(state="withdrawn", hold_reason=None, authoritative=1, authoritative_host=self.store.host,
                           recipient_host=self.store.host, updated_at=time.time(), withdraw_requested=0)
            if not isinstance(compact["payload_hash"], str) or len(compact["payload_hash"]) != 64:
                raise LedgerError("invalid_withdraw", "Unknown withdrawal must include its immutable message hash.")
            # The absence decision, origin tombstone and immutable hash receipt
            # commit together; accept/claim cannot slip between these writes.
            mailbox._withdraw(connection, message_id, origin_host=origin)
            connection.execute("INSERT INTO dedupe_receipts VALUES (?,?,?,?,?)",
                               (message_id, compact["payload_hash"], origin, json_text(compact), compact["updated_at"]))
            return {**compact, "withdrawn": True}

    def outbox(self) -> list[dict[str, Any]]:
        with self.store.read() as connection:
            return [dict(row) for row in connection.execute("SELECT * FROM outbox ORDER BY target_host,peer_seq")]

    def retry(self, item_id: str) -> dict[str, Any]:
        require_uuid(item_id, "item_id")
        with self.store.transaction() as connection:
            row = connection.execute("SELECT state FROM outbox WHERE item_id=?", (item_id,)).fetchone()
            if row is None:
                raise LedgerError("outbox_not_found", "No such outbox item exists.")
            if row[0] not in {"failed", "pending"}:
                raise LedgerError("outbox_final", "Sent/cancelled items are not replayed by retry.")
            connection.execute("UPDATE outbox SET state='pending',next_try_at=0,last_error=NULL WHERE item_id=?", (item_id,))
        return {"item_id": item_id, "state": "pending"}

    def pump(self, *, now: float | None = None, limit: int = 100) -> dict[str, int]:
        """One finite FIFO pass. Worker flock is the only transport execution lock."""
        at = time.time() if now is None else now
        sent = failed = deferred = 0
        blocked: set[str] = set()
        for _ in range(limit):
            with self.store.read() as connection:
                rows = connection.execute("SELECT o.* FROM outbox o WHERE o.state='pending' AND NOT EXISTS "
                                          "(SELECT 1 FROM outbox earlier WHERE earlier.target_host=o.target_host AND "
                                          "earlier.state='pending' AND earlier.peer_seq<o.peer_seq) ORDER BY o.target_host").fetchall()
            candidates = [row for row in rows if row["target_host"] not in blocked and row["next_try_at"] <= at]
            if not candidates:
                break
            for item in candidates:
                with self.store.transaction() as connection:
                    attempted = connection.execute("UPDATE outbox SET attempts=attempts+1 WHERE item_id=? AND state='pending'",
                                                   (item["item_id"],)).rowcount
                if not attempted:
                    continue
                try:
                    peer = self.peer(item["target_host"])
                    command = {"message": "accept", "receipt": "receipt", "setting": "setting", "withdraw": "withdraw"}[item["kind"]]
                    payload = json.loads(item["payload"])
                    response = self.transport.call(peer, command, payload)
                    with self.store.transaction() as connection:
                        if item["kind"] in {"message", "withdraw"}:
                            self.apply_receipt(connection, response, item["target_host"])
                        connection.execute("UPDATE outbox SET state='sent',last_error=NULL WHERE item_id=?", (item["item_id"],))
                    sent += 1
                except LedgerError as error:
                    transient = error.code in TRANSIENT_CODES
                    with self.store.transaction() as connection:
                        connection.execute("UPDATE outbox SET state=?,last_error=?,next_try_at=? WHERE item_id=?",
                                           ("pending" if transient else "failed", json_text({"code": error.code, "message": error.message}),
                                            at + min(300, 2 ** min(item["attempts"] + 1, 9)), item["item_id"]))
                    if transient:
                        blocked.add(item["target_host"])
                        deferred += 1
                    else:
                        failed += 1
        return {"sent": sent, "failed": failed, "deferred": deferred}
