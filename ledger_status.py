"""Terminal/session identity, Git identity and local agent-status snapshots."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import time
from typing import Any, Mapping
from urllib.parse import urlsplit
import uuid

from agent_directory import agent_label, herdr_executable
from ledger_store import LedgerError, LedgerStore, save_receipt


def normalize_origin(origin: str) -> str:
    """Join standard Git transport spellings without guessing path/port equivalence."""
    value = origin.strip().rstrip('/')
    if '://' not in value:
        scp = re.fullmatch(r"(?:[^/@:]+@)?(\[[^]]+\]|[^/:]+):(.+)", value)
        if not scp:
            # Filesystem remotes are host-local, not globally addressable URLs.
            raise LedgerError("local_origin", "The Git origin is a local filesystem path.")
        value = f"ssh://{scp[1]}/{scp[2]}"
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https", "ssh", "git"} or not parsed.hostname:
            raise ValueError
        port = parsed.port
    except ValueError:
        raise LedgerError("invalid_repo_origin", "Unsupported or malformed Git origin URL.") from None
    if parsed.query or parsed.fragment:
        raise LedgerError("invalid_repo_origin", "Git origin query/fragment needs an explicit ham.repo-id.")
    default = {"http": 80, "https": 443, "ssh": 22, "git": 9418}[parsed.scheme]
    host = parsed.hostname.lower()
    if ':' in host:
        host = f"[{host}]"
    authority = host if port is None or port == default else f"{host}:{port}"
    path = parsed.path.lstrip('/').rstrip('/')
    if path.endswith('.git'):
        path = path[:-4]
    if not path:
        raise LedgerError("invalid_repo_origin", "A Git origin must identify a repository path.")
    return f"git:{authority}/{path}"


def _git(cwd: str, *args: str) -> str:
    try:
        result = subprocess.run(["git", "-C", cwd, *args], capture_output=True,
                                text=True, timeout=5, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def repository_identity(cwd: str, host: str) -> dict[str, Any]:
    root = _git(cwd, "rev-parse", "--show-toplevel")
    if not root:
        return {"repo_id": f"local:{host}:{Path(cwd).resolve()}",
                "repo_root": str(Path(cwd).resolve()), "is_main_checkout": True}
    configured = _git(cwd, "config", "--get", "ham.repo-id")
    origin = _git(cwd, "config", "--get", "remote.origin.url")
    if configured:
        repo_id = configured
    elif origin:
        try:
            repo_id = normalize_origin(origin)
        except LedgerError as error:
            if error.code != "local_origin":
                raise
            repo_id = f"local:{host}:{Path(root).resolve()}"
    else:
        repo_id = f"local:{host}:{Path(root).resolve()}"
    git_dir = _git(cwd, "rev-parse", "--absolute-git-dir")
    common = _git(cwd, "rev-parse", "--git-common-dir")
    common_path = Path(common) if Path(common).is_absolute() else Path(cwd) / common
    return {"repo_id": repo_id, "repo_root": root,
            "is_main_checkout": bool(git_dir and common and Path(git_dir).resolve() == common_path.resolve())}


def live_snapshot(environment: Mapping[str, str] | None = None) -> dict[str, Any]:
    try:
        result = subprocess.run([herdr_executable(environment), "api", "snapshot"],
                                capture_output=True, text=True, timeout=10, check=False,
                                env=None if environment is None else dict(environment))
    except (OSError, subprocess.TimeoutExpired) as error:
        raise LedgerError("host_unavailable", str(error)) from None
    if result.returncode:
        raise LedgerError("host_unavailable", result.stderr.strip() or "Herdr snapshot failed.")
    try:
        payload = json.loads(result.stdout[result.stdout.index('{'):])
        container = payload["result"].get("snapshot", payload["result"])
        if not isinstance(container.get("agents"), list) or not isinstance(container.get("panes"), list):
            raise ValueError
    except (ValueError, KeyError, TypeError, AttributeError):
        raise LedgerError("invalid_snapshot", "Herdr did not return a complete agent/pane snapshot.") from None
    return container


def process_ancestors() -> set[int]:
    """Find the calling process tree even when every standard stream is piped."""
    try:
        result = subprocess.run(["ps", "-axo", "pid=,ppid="], capture_output=True,
                                text=True, check=False, timeout=5)
        if result.returncode:
            return set()
        parents = {int(parts[0]): int(parts[1]) for line in result.stdout.splitlines()
                   if len(parts := line.split()) == 2}
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return set()
    ancestors: set[int] = set()
    current = os.getpid()
    while current > 1 and current not in ancestors:
        ancestors.add(current)
        current = parents.get(current, 0)
    return ancestors


def pane_shell_pid(pane_id: str, environment: Mapping[str, str] | None = None) -> int | None:
    try:
        result = subprocess.run([herdr_executable(environment), "pane", "process-info", "--pane", pane_id],
                                capture_output=True, text=True, check=False, timeout=5,
                                env=None if environment is None else dict(environment))
        if result.returncode:
            return None
        data = json.loads(result.stdout[result.stdout.index('{'):])["result"]["process_info"]
        if data["pane_id"] != pane_id:
            return None
        return int(data["shell_pid"])
    except (OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired):
        return None


def current_agent(connection: sqlite3.Connection, host: str, terminal_id: str) -> dict[str, Any]:
    row = connection.execute("SELECT * FROM agents WHERE host=? AND terminal_id=? AND active=1",
                             (host, terminal_id)).fetchone()
    if row is None:
        raise LedgerError("recipient_absent", "No current agent occupies this terminal.")
    return dict(row)


def check_generation(connection: sqlite3.Connection, host: str, terminal_id: str,
                     agent_id: str, generation: int) -> dict[str, Any]:
    agent = current_agent(connection, host, terminal_id)
    if agent["agent_id"] != agent_id or agent["generation"] != generation:
        raise LedgerError("stale_session", "The agent session/generation has changed.")
    return agent


class AgentRegistry:
    def __init__(self, store: LedgerStore) -> None:
        self.store = store

    def refresh(self, environment: Mapping[str, str] | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
        # Lock BEFORE snapshot acquisition: an older event cannot overwrite a
        # newer session snapshot merely because its Git probes took longer.
        with self.store.lock("registry"):
            snapshot = live_snapshot(environment)
            result = self.reconcile(snapshot)
        return snapshot, result

    def reconcile(self, snapshot: Mapping[str, Any], *, now: float | None = None) -> dict[str, Any]:
        at = time.time() if now is None else now
        panes = snapshot.get("panes")
        raw_agents = snapshot.get("agents")
        if not isinstance(panes, list) or not isinstance(raw_agents, list):
            raise LedgerError("invalid_snapshot", "A complete panes+agents snapshot is required.")
        locations: dict[str, dict[str, str]] = {}
        for raw in panes:
            if not isinstance(raw, dict) or not raw.get("terminal_id") or not raw.get("pane_id"):
                raise LedgerError("invalid_snapshot", "Every pane must identify its terminal and location.")
            terminal = str(raw["terminal_id"])
            if terminal in locations:
                raise LedgerError("invalid_snapshot", "Duplicate terminal in snapshot.")
            locations[terminal] = {key: str(raw.get(key) or '') for key in ("terminal_id", "pane_id", "workspace_id")}
        # Resolve Git outside the write lock. Never accept caller-injected repo_id from events.
        observed: dict[str, dict[str, Any]] = {}
        for raw in raw_agents:
            if not isinstance(raw, dict) or not raw.get("agent"):
                continue
            terminal = str(raw.get("terminal_id") or '')
            if terminal not in locations:
                raise LedgerError("invalid_snapshot", "An agent has no matching terminal pane.")
            session = raw.get("agent_session")
            session_ref = str(session.get("value") or '') if isinstance(session, dict) else ''
            # Detection/display revisions are not session identities: status and
            # metadata reports may change them without replacing the client.
            if not session_ref:
                session_ref = "unreported"
            cwd = str(raw.get("cwd") or '')
            if not cwd:
                raise LedgerError("invalid_snapshot", "An agent must report its working directory.")
            identity = repository_identity(cwd, self.store.host)
            alias, _target = agent_label(raw)
            observed[terminal] = {**locations[terminal], **identity, "session_ref": session_ref,
                                  "client": str(raw["agent"]), "alias": alias,
                                  "turn_state": str(raw.get("agent_status") or 'unknown')}
        with self.store.transaction(tuple(locations)) as connection:
            old_terminals = {row[0] for row in connection.execute(
                "SELECT terminal_id FROM terminal_slots WHERE host=? AND present=1", (self.store.host,))}
            for terminal, location in locations.items():
                connection.execute(
                    "INSERT INTO terminal_slots VALUES (?,?,?,?,1,?) ON CONFLICT(host,terminal_id) DO UPDATE SET "
                    "pane_id=excluded.pane_id,workspace_id=excluded.workspace_id,present=1,updated_at=excluded.updated_at",
                    (self.store.host, terminal, location["pane_id"], location["workspace_id"], at))
                connection.execute("INSERT OR IGNORE INTO pane_bindings VALUES (?,?,?)",
                                   (self.store.host, location["pane_id"], terminal))
            for terminal in old_terminals - locations.keys():
                connection.execute("UPDATE terminal_slots SET present=0,updated_at=? WHERE host=? AND terminal_id=?",
                                   (at, self.store.host, terminal))
            active = {row["terminal_id"]: dict(row) for row in connection.execute(
                "SELECT * FROM agents WHERE host=? AND active=1", (self.store.host,))}
            for terminal, old in active.items():
                observed_agent = observed.get(terminal)
                if not observed_agent or any(old[key] != observed_agent[key] for key in ("session_ref", "client", "repo_id")):
                    connection.execute("UPDATE agents SET active=0,last_seen=? WHERE agent_id=?", (at, old["agent_id"]))
                    self._status(connection, old["agent_id"], "turn_state", "unknown", at)
            for terminal, value in observed.items():
                old = active.get(terminal)
                same = old and all(old[key] == value[key] for key in ("session_ref", "client", "repo_id"))
                if same:
                    agent_id = old["agent_id"]
                    connection.execute(
                        "UPDATE agents SET workspace_id=?,pane_id=?,alias=?,is_main_checkout=?,last_seen=? WHERE agent_id=?",
                        (value["workspace_id"], value["pane_id"], value["alias"], value["is_main_checkout"], at, agent_id))
                else:
                    generation = connection.execute(
                        "SELECT coalesce(max(generation),0)+1 FROM agents WHERE host=? AND terminal_id=?",
                        (self.store.host, terminal)).fetchone()[0]
                    agent_id = str(uuid.uuid4())
                    connection.execute("INSERT OR IGNORE INTO repos VALUES (?,NULL,?)", (value["repo_id"], at))
                    connection.execute(
                        "INSERT INTO agents (agent_id,host,terminal_id,workspace_id,pane_id,session_ref,generation,"
                        "client,repo_id,is_main_checkout,alias,first_seen,last_seen) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (agent_id, self.store.host, terminal, value["workspace_id"], value["pane_id"], value["session_ref"],
                         generation, value["client"], value["repo_id"], value["is_main_checkout"], value["alias"], at, at))
                    connection.execute("INSERT INTO agent_status(agent_id,turn_state,updated_at) VALUES (?,?,?)",
                                       (agent_id, "unknown", at))
                self._status(connection, agent_id, "turn_state", value["turn_state"], at)
            changed_ids: list[str] = []
            for row in connection.execute(
                    "SELECT d.message_id,d.recipient_terminal_id,m.repo_id,d.state FROM deliveries d "
                    "JOIN messages m USING(message_id) WHERE d.recipient_host=? AND d.state IN ('queued','delivering')",
                    (self.store.host,)).fetchall():
                terminal = row["recipient_terminal_id"]
                target = observed.get(terminal)
                if row["state"] == "delivering":
                    reason = "acceptance_unknown"
                elif terminal not in locations:
                    connection.execute("UPDATE deliveries SET state='undeliverable',hold_reason='terminal_closed',"
                                       "updated_at=? WHERE message_id=? AND state='queued'", (at, row["message_id"]))
                    connection.execute("UPDATE messages SET final_at=? WHERE message_id=?", (at, row["message_id"]))
                    changed_ids.append(row["message_id"])
                    continue
                elif not target:
                    reason = "recipient_absent"
                elif target["repo_id"] != row["repo_id"]:
                    reason = "recipient_changed"
                else:
                    reason = None
                location = locations.get(terminal, {})
                connection.execute("UPDATE deliveries SET hold_reason=?,workspace_id=?,pane_id=? WHERE message_id=?",
                                   (reason, location.get("workspace_id", ''), location.get("pane_id", ''), row["message_id"]))
            for message_id in changed_ids:
                save_receipt(connection, message_id)
        return {"agents": self.list(), "terminals": len(locations)}

    @staticmethod
    def _status(connection: sqlite3.Connection, agent_id: str, field: str, value: str, at: float) -> None:
        # Field is selected by internal code, never interpolated from external input.
        if field not in {"turn_state", "phase", "summary"}:
            raise LedgerError("invalid_status_field", "Unknown agent status field.")
        old = connection.execute(f"SELECT {field} FROM agent_status WHERE agent_id=?", (agent_id,)).fetchone()
        if old is not None and old[0] != value:
            connection.execute(f"UPDATE agent_status SET {field}=?,updated_at=? WHERE agent_id=?", (value, at, agent_id))
            connection.execute("INSERT INTO status_log(agent_id,field,value,at) VALUES (?,?,?,?)", (agent_id, field, value, at))

    def list(self, repo_id: str | None = None) -> list[dict[str, Any]]:
        with self.store.read() as connection:
            query = "SELECT a.*,s.turn_state,s.phase,s.summary FROM agents a JOIN agent_status s USING(agent_id) WHERE a.active=1"
            parameters: tuple[Any, ...] = ()
            if repo_id is not None:
                query += " AND a.repo_id=?"
                parameters = (repo_id,)
            return [dict(row) for row in connection.execute(query + " ORDER BY a.host,a.alias,a.terminal_id", parameters)]

    def resolve_context(self, environment: Mapping[str, str] | None = None,
                        snapshot: Mapping[str, Any] | None = None) -> dict[str, Any]:
        values = os.environ if environment is None else environment
        terminal = values.get("HERDR_TERMINAL_ID")
        inherited_pane = values.get("HERDR_PANE_ID", '')
        if not terminal:
            if snapshot is None:
                snapshot = live_snapshot(environment)
            with self.store.read() as connection:
                cached = {row[0] for row in connection.execute(
                    "SELECT terminal_id FROM pane_bindings WHERE host=? AND pane_id=?",
                    (self.store.host, inherited_pane))}
            # Old pane bindings only prioritize probes. They never prove that a
            # piped tool subprocess belongs to that terminal after a pane move.
            panes = sorted(snapshot["panes"], key=lambda raw: raw["terminal_id"] not in cached)
            ancestors = process_ancestors()
            for raw in panes:
                if pane_shell_pid(raw["pane_id"], environment) in ancestors:
                    terminal = raw["terminal_id"]
                    break
            if not terminal:
                raise LedgerError("sender_unresolved", "Cannot match this process to a live Herdr terminal; refusing inherited pane guesses.")
        with self.store.read() as connection:
            return current_agent(connection, self.store.host, terminal)

    def agent_session(self, *, terminal_id: str, session_ref: str, client: str,
                      repo_id: str, extension_present: bool = True) -> dict[str, Any]:
        with self.store.transaction((terminal_id,)) as connection:
            agent = current_agent(connection, self.store.host, terminal_id)
            if any(agent[key] != value for key, value in (("session_ref", session_ref), ("client", client), ("repo_id", repo_id))):
                raise LedgerError("stale_session", "Runtime registration does not match the current Herdr session.")
            connection.execute("UPDATE agents SET extension_present=? WHERE agent_id=?",
                               (bool(extension_present), agent["agent_id"]))
            agent["extension_present"] = bool(extension_present)
        return agent

    def set_status(self, agent: Mapping[str, Any], *, phase: str, summary: str) -> dict[str, Any]:
        with self.store.transaction((agent["terminal_id"],)) as connection:
            check_generation(connection, self.store.host, agent["terminal_id"], agent["agent_id"], agent["generation"])
            at = time.time()
            self._status(connection, agent["agent_id"], "phase", phase, at)
            self._status(connection, agent["agent_id"], "summary", summary, at)
        return {"agent_id": agent["agent_id"], "phase": phase, "summary": summary}

    def repo_show(self, repo_id: str) -> dict[str, Any]:
        with self.store.read() as connection:
            row = connection.execute("SELECT * FROM repos WHERE repo_id=?", (repo_id,)).fetchone()
            if row is None:
                raise LedgerError("repo_not_found", "No local agents have registered this repository.")
            settings = {row["field"]: {"value": json.loads(row["value"]), "version": row["version"],
                                       "origin_host": row["origin_host"], "changed_at": row["changed_at"]}
                        for row in connection.execute("SELECT * FROM repo_settings WHERE repo_id=?", (repo_id,))}
        return {"repo_id": repo_id, "home_host": row["home_host"], "mode": settings.get("mode", {}).get("value", "off"),
                "settings": settings, "agents": self.list(repo_id)}
