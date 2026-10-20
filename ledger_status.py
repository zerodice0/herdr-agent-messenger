"""Terminal/session identity, Git identity and local agent-status snapshots."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import time
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import urlsplit
import uuid

from agent_directory import agent_label, herdr_executable, run_bounded_command
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

def _claude_ancestor_pid() -> int | None:
    """Return the nearest actual native root, not a foreground grandparent."""
    try:
        result = subprocess.run(["ps", "-axo", "pid=,ppid=,comm="],
                                capture_output=True, text=True, check=False, timeout=5)
        if result.returncode:
            return None
        processes = {int(parts[0]): (int(parts[1]), parts[2])
                     for line in result.stdout.splitlines()
                     if len(parts := line.split(None, 2)) == 3}
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None
    seen: set[int] = set()
    current = os.getpid()
    while current > 1 and current not in seen:
        seen.add(current)
        process = processes.get(current)
        if process is None:
            return None
        if Path(process[1]).name == "claude":
            return current
        current = process[0]
    return None


def claude_foreground_pid(agent: Mapping[str, Any],
                          environment: Mapping[str, str] | None = None, *,
                          require_ancestor: bool = True) -> int | None:
    """Verify the actual foreground root, never a hook/env-claimed process ID."""
    try:
        def query(*arguments: str) -> Mapping[str, Any]:
            result = run_bounded_command(
                [herdr_executable(environment), *arguments], timeout=5, environment=environment)
            payload = json.loads(result.stdout[result.stdout.index('{'):])
            if result.returncode or payload.get("error") or payload.get("ok") is False:
                raise ValueError("Unreadable native process")
            return payload["result"]
        pane = query("pane", "get", str(agent["pane_id"]))["pane"]
        if (pane["pane_id"] != agent["pane_id"]
                or pane["terminal_id"] != agent["terminal_id"]):
            return None
        info = query("pane", "process-info", "--pane", str(agent["pane_id"]))["process_info"]
        group = info["foreground_process_group_id"]
        if (info["pane_id"] != agent["pane_id"] or not isinstance(group, int)
                or isinstance(group, bool) or group <= 1):
            return None
        roots = [process for process in info["foreground_processes"]
                 if process.get("name") == "claude"
                 or (isinstance(process.get("argv"), list) and process["argv"]
                     and Path(str(process["argv"][0])).name == "claude")]
        if len(roots) != 1:
            return None
        pid = roots[0]["pid"]
        if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 1:
            return None
        return pid if not require_ancestor or _claude_ancestor_pid() == pid else None
    except (OSError, ValueError, TypeError, KeyError, AttributeError, subprocess.SubprocessError):
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
    def __init__(self, store: LedgerStore, *,
                 display_runner: Callable[[Sequence[str]], subprocess.CompletedProcess[str]] | None = None) -> None:
        self.store = store
        self.display_runner = display_runner

    def refresh(self, environment: Mapping[str, str] | None = None, *,
                refresh_display: bool = True,
                prune_device_claims: bool = True) -> tuple[dict[str, Any], dict[str, Any]]:
        # Lock BEFORE snapshot acquisition: an older event cannot overwrite a
        # newer session snapshot merely because its Git probes took longer.
        with self.store.lock("registry"):
            snapshot = live_snapshot(environment)
            changed_panes: set[str] = set()
            result = self._reconcile(snapshot, changed_panes=changed_panes, environment=environment,
                                     prune_device_claims=prune_device_claims)
            if refresh_display:
                if changed_panes:
                    self._refresh_display(changed_panes, environment=environment)
        return snapshot, result

    def reconcile(self, snapshot: Mapping[str, Any], *, now: float | None = None,
                  refresh_display: bool = True,
                  environment: Mapping[str, str] | None = None) -> dict[str, Any]:
        """Commit a snapshot, then project changed panes; DB-only callers may opt out."""
        changed_panes: set[str] = set()
        result = self._reconcile(snapshot, now=now, changed_panes=changed_panes, environment=environment)
        if refresh_display and changed_panes:
            self._refresh_display(changed_panes, environment=environment)
        return result

    def _reconcile(self, snapshot: Mapping[str, Any], *, changed_panes: set[str],
                   now: float | None = None,
                   environment: Mapping[str, str] | None = None,
                   prune_device_claims: bool = True) -> dict[str, Any]:
        at = time.time() if now is None else now
        panes = snapshot.get("panes")
        raw_agents = snapshot.get("agents")
        if not isinstance(panes, list) or not isinstance(raw_agents, list):
            raise LedgerError("invalid_snapshot", "A complete panes+agents snapshot is required.")
        raw_workspaces = snapshot.get("workspaces", [])
        if not isinstance(raw_workspaces, list):
            raise LedgerError("invalid_snapshot", "Workspace metadata must be a list.")
        workspaces: dict[str, tuple[str, int, str]] = {}
        for raw in raw_workspaces:
            if not isinstance(raw, dict) or not raw.get("workspace_id"):
                raise LedgerError("invalid_snapshot", "Every workspace must identify its location.")
            workspace_id = str(raw["workspace_id"])
            if workspace_id in workspaces:
                raise LedgerError("invalid_snapshot", "Duplicate workspace in snapshot.")
            worktree = raw.get("worktree")
            repo_key = str(worktree.get("repo_key") or "") if isinstance(worktree, dict) else ""
            linked = int(bool(worktree.get("is_linked_worktree"))) if isinstance(worktree, dict) else 0
            workspaces[workspace_id] = (repo_key, linked, str(raw.get("label") or workspace_id))
        locations: dict[str, dict[str, str]] = {}
        for raw in panes:
            if not isinstance(raw, dict) or not raw.get("terminal_id") or not raw.get("pane_id"):
                raise LedgerError("invalid_snapshot", "Every pane must identify its terminal and location.")
            terminal = str(raw["terminal_id"])
            if terminal in locations:
                raise LedgerError("invalid_snapshot", "Duplicate terminal in snapshot.")
            locations[terminal] = {key: str(raw.get(key) or '') for key in ("terminal_id", "pane_id", "workspace_id")}
        # Resolve Git outside the write lock. Never accept caller-injected repo_id from events.
        with self.store.read() as connection:
            pending = {row["terminal_id"]: dict(row) for row in connection.execute(
                "SELECT * FROM agents WHERE host=? AND active=1 AND client='claude' "
                "AND native_session_pid IS NOT NULL", (self.store.host,))}
        observed: dict[str, dict[str, Any]] = {}
        for raw in raw_agents:
            if not isinstance(raw, dict) or not raw.get("agent"):
                continue
            terminal = str(raw.get("terminal_id") or '')
            if terminal not in locations:
                raise LedgerError("invalid_snapshot", "An agent has no matching terminal pane.")
            session = raw.get("agent_session")
            session_ref = str(session.get("value") or '') if isinstance(session, dict) else ''
            native_session_pid = None
            # Detection/display revisions are not session identities: status and
            # metadata reports may change them without replacing the client.
            if not session_ref:
                session_ref = "unreported"
                binding = pending.get(terminal)
                if (binding and raw["agent"] == "claude"
                        and claude_foreground_pid({**binding, **locations[terminal]}, environment,
                                                  require_ancestor=False) == binding["native_session_pid"]):
                    session_ref = binding["session_ref"]
                    native_session_pid = binding["native_session_pid"]
            cwd = str(raw.get("cwd") or '')
            if not cwd:
                raise LedgerError("invalid_snapshot", "An agent must report its working directory.")
            identity = repository_identity(cwd, self.store.host)
            alias, _target = agent_label(raw)
            observed[terminal] = {**locations[terminal], **identity, "session_ref": session_ref,
                                  "client": str(raw["agent"]), "alias": alias,
                                  "turn_state": str(raw.get("agent_status") or 'unknown'),
                                  "native_session_pid": native_session_pid}
        with self.store.transaction(tuple(locations)) as connection:
            from ledger_settings import RepoSettings
            settings = RepoSettings(self.store)
            affected_scopes: set[tuple[str, str, str]] = set()
            old_scope_by_workspace: dict[tuple[str, str, str], str] = {}
            old_scope_terminals: dict[tuple[str, str, str], set[str]] = {}
            for agent in connection.execute("SELECT * FROM agents WHERE host=? AND active=1", (self.store.host,)):
                scope = (agent["repo_id"], agent["host"], agent["workspace_id"])
                anchor = settings.scope_workspace(connection, *scope)
                old_scope_by_workspace[scope] = anchor
                old_scope_terminals.setdefault((*scope[:2], anchor), set()).add(agent["terminal_id"])
            # This is a complete open-workspace snapshot, including empty anchors.
            # Missing worktree metadata deliberately restores independent scopes.
            connection.execute("DELETE FROM workspaces WHERE host=?", (self.store.host,))
            connection.executemany("INSERT INTO workspaces VALUES (?,?,?,?,?)",
                                   [(self.store.host, workspace_id, *metadata)
                                    for workspace_id, metadata in workspaces.items()])
            for scope, old_anchor in old_scope_by_workspace.items():
                if settings.scope_workspace(connection, *scope) != old_anchor:
                    affected_scopes.add(scope)
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
                    affected_scopes.add((old["repo_id"], old["host"], old["workspace_id"]))
                    connection.execute("UPDATE agents SET active=0,last_seen=? WHERE agent_id=?", (at, old["agent_id"]))
                    self._status(connection, old["agent_id"], "turn_state", "unknown", at)
            for terminal, value in observed.items():
                old = active.get(terminal)
                same = old and all(old[key] == value[key] for key in ("session_ref", "client", "repo_id"))
                if same:
                    if any(old[key] != value[key] for key in ("workspace_id", "is_main_checkout")):
                        affected_scopes.add((old["repo_id"], old["host"], old["workspace_id"]))
                        affected_scopes.add((value["repo_id"], self.store.host, value["workspace_id"]))
                    agent_id = old["agent_id"]
                    if any(old[key] != value[key] for key in
                           ("workspace_id", "pane_id", "alias", "is_main_checkout")):
                        changed_panes.add(value["pane_id"])
                    connection.execute(
                        "UPDATE agents SET workspace_id=?,pane_id=?,alias=?,is_main_checkout=?,last_seen=?,"
                        "native_session_pid=? WHERE agent_id=?",
                        (value["workspace_id"], value["pane_id"], value["alias"], value["is_main_checkout"], at,
                         value["native_session_pid"], agent_id))
                else:
                    changed_panes.add(value["pane_id"])
                    affected_scopes.add((value["repo_id"], self.store.host, value["workspace_id"]))
                    generation = connection.execute(
                        "SELECT coalesce(max(generation),0)+1 FROM agents WHERE host=? AND terminal_id=?",
                        (self.store.host, terminal)).fetchone()[0]
                    agent_id = str(uuid.uuid4())
                    inserted = connection.execute("INSERT OR IGNORE INTO repos VALUES (?,NULL,?)",
                                                  (value["repo_id"], at)).rowcount
                    if inserted:
                        settings._seed_new_repo(connection, value["repo_id"], at)
                    connection.execute(
                        "INSERT INTO agents (agent_id,host,terminal_id,workspace_id,pane_id,session_ref,generation,"
                        "client,repo_id,is_main_checkout,alias,first_seen,last_seen) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (agent_id, self.store.host, terminal, value["workspace_id"], value["pane_id"], value["session_ref"],
                         generation, value["client"], value["repo_id"], value["is_main_checkout"], value["alias"], at, at))
                    connection.execute("INSERT INTO agent_status(agent_id,turn_state,updated_at) VALUES (?,?,?)",
                                       (agent_id, "unknown", at))
                if self._status(connection, agent_id, "turn_state", value["turn_state"], at):
                    changed_panes.add(value["pane_id"])
            # A move, arrival or departure can change the derived main for every
            # survivor in both scopes, even when their own registry rows did not change.
            current_panes = {row["terminal_id"]: row["pane_id"] for row in connection.execute(
                "SELECT terminal_id,pane_id FROM agents WHERE host=? AND active=1", (self.store.host,))}
            for repo_id, host, workspace_id in affected_scopes:
                old_anchor = old_scope_by_workspace.get((repo_id, host, workspace_id), workspace_id)
                changed_panes.update(current_panes[terminal] for terminal in
                                     old_scope_terminals.get((repo_id, host, old_anchor), ())
                                     if terminal in current_panes)
                changed_panes.update(agent["pane_id"] for agent in settings.scope_agents(
                    connection, repo_id, host, workspace_id))
            changed_ids: list[str] = []
            for row in connection.execute(
                    "SELECT d.message_id,d.recipient_terminal_id,d.recipient_repo_id,d.state FROM deliveries d "
                    "WHERE d.recipient_host=? AND d.state IN ('queued','delivering')",
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
                elif target["repo_id"] != row["recipient_repo_id"]:
                    reason = "recipient_changed"
                else:
                    reason = None
                location = locations.get(terminal, {})
                connection.execute("UPDATE deliveries SET hold_reason=?,workspace_id=?,pane_id=? WHERE message_id=?",
                                   (reason, location.get("workspace_id", ''), location.get("pane_id", ''), row["message_id"]))
            for message_id in changed_ids:
                save_receipt(connection, message_id)
            if prune_device_claims:
                from ledger_devices import prune_stale
                changed_panes.update(prune_stale(connection))
        return {"agents": self.list(), "terminals": len(locations)}

    @staticmethod
    def _status(connection: sqlite3.Connection, agent_id: str, field: str, value: str, at: float) -> bool:
        # Field is selected by internal code, never interpolated from external input.
        if field not in {"turn_state", "phase", "summary"}:
            raise LedgerError("invalid_status_field", "Unknown agent status field.")
        old = connection.execute(f"SELECT {field} FROM agent_status WHERE agent_id=?", (agent_id,)).fetchone()
        if old is not None and old[0] != value:
            connection.execute(f"UPDATE agent_status SET {field}=?,updated_at=? WHERE agent_id=?", (value, at, agent_id))
            connection.execute("INSERT INTO status_log(agent_id,field,value,at) VALUES (?,?,?,?)", (agent_id, field, value, at))
            return True
        return False

    def list(self, repo_id: str | None = None) -> list[dict[str, Any]]:
        with self.store.read() as connection:
            query = ("SELECT a.*,s.turn_state,s.phase,s.summary,s.updated_at AS status_updated_at "
                     "FROM agents a JOIN agent_status s USING(agent_id) WHERE a.active=1")
            parameters: tuple[Any, ...] = ()
            if repo_id is not None:
                query += " AND a.repo_id=?"
                parameters = (repo_id,)
            from ledger_settings import RepoSettings
            settings = RepoSettings(self.store)
            return [{**dict(row), "role": settings.role(row, connection=connection),
                     "role_workspace_id": settings.scope_workspace(
                         connection, row["repo_id"], row["host"], row["workspace_id"])}
                    for row in connection.execute(query + " ORDER BY a.host,a.alias,a.terminal_id", parameters)]

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
                      repo_id: str, extension_present: bool = True,
                      expected_agent_id: str | None = None, expected_generation: int | None = None,
                      environment: Mapping[str, str] | None = None) -> dict[str, Any]:
        # OMP waits for Herdr's authoritative path. A Claude hook may fill only
        # an unreported UUID; serialize that narrow upgrade with reconciliation.
        with self.store.lock("registry"):
            native_pid = None
            if client == "claude":
                with self.store.read() as connection:
                    candidate = current_agent(connection, self.store.host, terminal_id)
                native_pid = claude_foreground_pid(candidate, environment)
                if native_pid is None:
                    raise LedgerError("native_root_unverified", "Only the current foreground Claude root may register.")
                if candidate["session_ref"] == "unreported" and session_ref != "unreported":
                    latest = live_snapshot(environment)
                    current = next((raw for raw in latest["agents"]
                                    if raw.get("terminal_id") == terminal_id
                                    and raw.get("pane_id") == candidate["pane_id"]
                                    and raw.get("agent") == "claude"), None)
                    if current is None:
                        raise LedgerError("native_root_unverified", "The current Claude root is no longer reported.")
                    reported = current.get("agent_session")
                    reference = reported.get("value") if isinstance(reported, dict) else None
                    if reference and reference != session_ref:
                        raise LedgerError("stale_session", "A different native session is already reported.")
            with self.store.transaction((terminal_id,)) as connection:
                agent = current_agent(connection, self.store.host, terminal_id)
                if expected_agent_id is not None or expected_generation is not None:
                    check_generation(connection, self.store.host, terminal_id,
                                     expected_agent_id, expected_generation)
                if agent["client"] != client or agent["repo_id"] != repo_id:
                    raise LedgerError("stale_session", "Runtime registration does not match the current Herdr session.")
                if agent["session_ref"] != session_ref:
                    if client == "omp" and agent["session_ref"] == "unreported":
                        raise LedgerError("native_session_pending", "Waiting for Herdr to report the native OMP session.")
                    if (client != "claude" or agent["session_ref"] != "unreported"
                            or expected_agent_id is None or expected_generation is None):
                        raise LedgerError("stale_session", "Runtime registration cannot replace a reported native session.")
                    try:
                        if str(uuid.UUID(session_ref)) != session_ref:
                            raise ValueError
                    except (ValueError, TypeError, AttributeError):
                        raise LedgerError("invalid_session", "Claude registration requires its real canonical hook UUID.") from None
                    connection.execute("UPDATE agents SET session_ref=?,native_session_pid=? WHERE agent_id=?",
                                       (session_ref, native_pid, agent["agent_id"]))
                    agent["session_ref"] = session_ref
                    agent["native_session_pid"] = native_pid
                elif (client == "claude" and agent["native_session_pid"] is not None
                      and agent["native_session_pid"] != native_pid):
                    raise LedgerError("stale_session", "The pending native root process has changed.")
                connection.execute("UPDATE agents SET extension_present=? WHERE agent_id=?",
                                   (bool(extension_present), agent["agent_id"]))
                agent["extension_present"] = bool(extension_present)
        return agent

    def set_status(self, agent: Mapping[str, Any], *, phase: str, summary: str,
                   feature_invoked: bool = False,
                   environment: Mapping[str, str] | None = None) -> dict[str, Any]:
        """Publish committed status; explicit feature use also refreshes unchanged values."""
        with self.store.transaction((agent["terminal_id"],)) as connection:
            current = check_generation(
                connection, self.store.host, agent["terminal_id"], agent["agent_id"], agent["generation"])
            at = time.time()
            phase_changed = self._status(connection, agent["agent_id"], "phase", phase, at)
            summary_changed = self._status(connection, agent["agent_id"], "summary", summary, at)
        result = {"agent_id": agent["agent_id"], "phase": phase, "summary": summary}
        if phase_changed or summary_changed or feature_invoked:
            result["display"] = self._refresh_display(
                {current["pane_id"]}, environment=environment, feature_invoked=feature_invoked)
        return result

    def _refresh_display(self, pane_ids: set[str], *,
                         environment: Mapping[str, str] | None = None,
                         feature_invoked: bool = False) -> dict[str, list[str]]:
        # Import lazily: the projector reads this registry, but never reconciles it.
        # It alone owns border ownership checks and last_ham_label compare-and-set.
        result: dict[str, list[str]] = {"updated": [], "failed": []}
        try:
            from ledger_display import LedgerDisplay
            runner = self.display_runner
            if runner is None:
                runner = lambda command: run_bounded_command(
                    [herdr_executable(environment), *command], timeout=10, environment=environment)
            display = LedgerDisplay(self.store, runner=runner)
            for pane_id in sorted(pane_ids):
                refreshed = display.refresh(pane_id, feature_invoked=feature_invoked)
                for key in result:
                    result[key].extend(refreshed[key])
        except Exception as error:
            # The registry transaction has committed. A failed projection cannot
            # truthfully be reported as a failed/rolled-back status mutation.
            result["failed"].extend(sorted(pane_ids - set(result["updated"]) - set(result["failed"])))
            self._display_failure(str(error))
        else:
            if result["failed"]:
                self._display_failure("Could not refresh panes: " + ", ".join(result["failed"]))
        return result

    def _display_failure(self, detail: str) -> None:
        message = "HAM display refresh failed after commit: " + detail
        warning = {"code": "notification_failed", "message": message,
                   "committed": True, "recovery": "event_or_safety_sweep"}
        if warning not in self.store.notification_errors:
            self.store.notification_errors.append(warning)
        try:
            self.store.note_notification_failure(message)
        except Exception:
            # Even persisting the diagnostic is advisory after the durable write.
            pass

    def set_display_label(self, agent_id: str, expected_last: str | None,
                          label: str) -> dict[str, Any]:
        if not isinstance(agent_id, str) or not agent_id or (
                expected_last is not None and not isinstance(expected_last, str)) or not isinstance(label, str):
            raise LedgerError("invalid_display_label", "Agent ID, compare value, and label must be text.")
        desired = label
        with self.store.transaction() as connection:
            row = connection.execute("SELECT host,terminal_id,active,last_ham_label FROM agents "
                                     "WHERE agent_id=?", (agent_id,)).fetchone()
            if row is None:
                return {"updated": False, "last_ham_label": None}
            current = connection.execute("SELECT agent_id FROM agents WHERE host=? AND terminal_id=? AND active=1",
                                         (row["host"], row["terminal_id"])).fetchone()
            last = row["last_ham_label"]
            if not row["active"] or current is None or current["agent_id"] != agent_id or last != expected_last:
                return {"updated": False, "last_ham_label": last}
            changed = connection.execute(
                "UPDATE agents SET last_ham_label=? WHERE agent_id=? AND active=1 AND last_ham_label IS ?",
                (desired, agent_id, expected_last),
            ).rowcount
            return {"updated": changed == 1, "last_ham_label": desired if changed == 1 else last}

    def repo_show(self, repo_id: str, host: str, workspace_id: str) -> dict[str, Any]:
        with self.store.read() as connection:
            row = connection.execute("SELECT * FROM repos WHERE repo_id=?", (repo_id,)).fetchone()
            if row is None:
                raise LedgerError("repo_not_found", "No local agents have registered this repository.")
        from ledger_settings import RepoSettings
        return {**RepoSettings(self.store).show(repo_id, host=host, workspace_id=workspace_id),
                "agents": self.list(repo_id)}
