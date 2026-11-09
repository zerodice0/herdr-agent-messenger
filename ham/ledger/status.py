"""Terminal/session identity, Git identity and local agent-status snapshots."""

from __future__ import annotations

from contextlib import ExitStack, contextmanager, nullcontext
import hashlib
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
import unicodedata

from ham.agents.directory import agent_label, herdr_executable, run_bounded_command
from ham.ledger.store import LedgerError, LedgerStore, budget_timeout, save_receipt
from ham.processes import ProcessLookupFailure, ProcessSource, process_chain


def sanitize_status_text(value: Any, limit: int) -> str:
    """Keep ordinary Unicode while excluding terminal and bidi controls."""
    text = str(value or "")
    # Preserve separation at line/tab boundaries instead of joining words.
    text = "".join(" " if character.isspace() else character
                   for character in text
                   if character.isspace() or unicodedata.category(character) not in {"Cc", "Cf"})
    return " ".join(text.split())[:limit].rstrip()


def sanitize_agent_status(agent: Mapping[str, Any]) -> dict[str, Any]:
    return {**agent, "phase": sanitize_status_text(agent.get("phase"), 64),
            "summary": sanitize_status_text(agent.get("summary"), 200)}


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
                                text=True, timeout=budget_timeout(5), check=False)
    except (OSError, subprocess.TimeoutExpired):
        budget_timeout(5)
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
                                capture_output=True, text=True, timeout=budget_timeout(10), check=False,
                                env=None if environment is None else dict(environment))
    except (OSError, subprocess.TimeoutExpired) as error:
        budget_timeout(10)
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


def _lookup_failed(lookup: str, reason: str, started: float) -> LedgerError:
    elapsed = max(0, int((time.monotonic() - started) * 1000))
    error = LedgerError("sender_lookup_failed",
                        f"Caller verification failed: {lookup} lookup {reason} ({elapsed}ms); refusing caller.")
    error.details = {"lookup": lookup, "reason": reason, "elapsed_ms": elapsed}
    return error


def process_ancestors(shell_pid: int) -> set[int]:
    """Verify caller parents through the shell reported by live Herdr process-info."""
    started = time.monotonic()
    source = ProcessSource()
    try:
        return {process.pid for process in process_chain(
            source=source, timeout=budget_timeout(5), stop_at=shell_pid)}
    except ProcessLookupFailure as error:
        raise _lookup_failed(source.lookup, error.reason, started) from None


def pane_shell_pid(pane_id: str, environment: Mapping[str, str] | None = None) -> int:
    started = time.monotonic()
    try:
        result = run_bounded_command(
            [herdr_executable(environment), "pane", "process-info", "--pane", pane_id],
            timeout=budget_timeout(5), environment=environment, raise_errors=True)
    except subprocess.TimeoutExpired:
        raise _lookup_failed("process-info", "timeout", started) from None
    except subprocess.SubprocessError:
        raise _lookup_failed("process-info", "invalid_output", started) from None
    except OSError:
        raise _lookup_failed("process-info", "spawn_failed", started) from None
    if result.returncode:
        raise _lookup_failed("process-info", "nonzero_exit", started)
    try:
        payload = json.loads(result.stdout[result.stdout.index('{'):])
        data = payload["result"]["process_info"]
        value = data["shell_pid"]
        if (payload.get("error") or payload.get("ok") is False or data["pane_id"] != pane_id
                or isinstance(value, bool) or not isinstance(value, (str, int))):
            raise ValueError
        pid = int(value)
        if pid <= 1:
            raise ValueError
        return pid
    except (ValueError, KeyError, TypeError, AttributeError):
        raise _lookup_failed("process-info", "invalid_output", started) from None

def _claude_ancestor_pid(shell_pid: int) -> int | None:
    """Return the nearest native root inside the verified Herdr shell boundary."""
    try:
        chain = process_chain(timeout=budget_timeout(5), stop_at=shell_pid)
        if not chain or chain[-1].pid != shell_pid:
            return None
        return next((process.pid for process in chain
                     if Path(process.name).name == "claude"), None)
    except ProcessLookupFailure:
        return None


def claude_foreground_pid(agent: Mapping[str, Any],
                          environment: Mapping[str, str] | None = None, *,
                          require_ancestor: bool = True) -> int | None:
    """Verify the actual foreground root, never a hook/env-claimed process ID."""
    try:
        def query(*arguments: str) -> Mapping[str, Any]:
            result = run_bounded_command(
                [herdr_executable(environment), *arguments], timeout=budget_timeout(5), environment=environment)
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
        if not require_ancestor:
            return pid
        shell_pid = info["shell_pid"]
        if type(shell_pid) is not int or shell_pid <= 1:
            return None
        return pid if _claude_ancestor_pid(shell_pid) == pid else None
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


def recoverable_display_owner(connection: sqlite3.Connection, agent: Mapping[str, Any],
                              border: str, *,
                              live_terminals: set[str] | None = None,
                              allow_repo_mismatch: bool = False) -> dict[str, Any] | None:
    """Recover display ownership, never session/message identity, from exact HAM text.

    Same-terminal older generations keep the original host/terminal/pane and
    exact-label proof even if workspace, client or repository changed. Herdr
    restore instead allocates a new terminal: this path additionally requires
    the same workspace/repository/client and a vanished inactive terminal.
    Session mismatch is allowed because this proves display ownership only.
    Prefixes and pane numbers alone prove nothing. Only explicit pane-selected
    reclaim may relax repository equality on the cross-terminal path.
    """
    if not isinstance(border, str) or agent.get("last_ham_label") is not None:
        return None
    previous = connection.execute(
        "SELECT * FROM agents WHERE host=? AND terminal_id=? AND pane_id=? "
        "AND active=0 AND generation<? AND last_ham_label=? "
        "ORDER BY last_seen DESC,first_seen DESC,agent_id DESC LIMIT 1",
        (agent["host"], agent["terminal_id"], agent["pane_id"], agent["generation"], border)).fetchone()
    if previous is not None:
        return dict(previous)
    if not border:
        return None
    if live_terminals is None:
        live_terminals = {row[0] for row in connection.execute(
            "SELECT terminal_id FROM terminal_slots WHERE host=? AND present=1", (agent["host"],))}
    query = ("SELECT * FROM agents WHERE host=? AND workspace_id=? AND pane_id=? "
             "AND client=? AND active=0 AND last_ham_label=? AND terminal_id<>?")
    parameters = [agent["host"], agent["workspace_id"], agent["pane_id"], agent["client"],
                  border, agent["terminal_id"]]
    if not allow_repo_mismatch:
        query += " AND repo_id=?"
        parameters.append(agent["repo_id"])
    candidates = connection.execute(
        query + " ORDER BY last_seen DESC,first_seen DESC,agent_id DESC", parameters)
    for row in candidates:
        if row["terminal_id"] in live_terminals:
            continue
        if connection.execute("SELECT 1 FROM agents WHERE host=? AND terminal_id=? AND active=1",
                              (agent["host"], row["terminal_id"])).fetchone() is None:
            return dict(row)
    return None


class AgentRegistry:
    def __init__(self, store: LedgerStore, *,
                 display_runner: Callable[[Sequence[str]], subprocess.CompletedProcess[str]] | None = None) -> None:
        self.store = store
        self.display_runner = display_runner

    def _reserve_observation(self) -> int:
        with self.store.transaction(notify=False) as connection:
            row = connection.execute(
                "SELECT value FROM ledger_meta WHERE key='registry-observation'").fetchone()
            token = int(row[0]) + 1 if row else 1
            connection.execute("INSERT OR REPLACE INTO ledger_meta VALUES ('registry-observation',?)",
                               (str(token),))
        return token

    @staticmethod
    def _observation_value(connection: sqlite3.Connection, key: str) -> int:
        row = connection.execute("SELECT value FROM ledger_meta WHERE key=?", (key,)).fetchone()
        return int(row[0]) if row else 0

    def _fence_sweep(self, connection: sqlite3.Connection, token: int,
                     observed: Mapping[str, Any], locations: Mapping[str, Any],
                     workspaces: Mapping[str, Any]) -> None:
        # A newer successful caller observation matters even when it changed
        # no business data. Compatible observations must not starve the sweep.
        newer = connection.execute(
            "SELECT key FROM ledger_meta WHERE CAST(value AS INTEGER)>? AND "
            "(key LIKE 'registry-terminal:%' OR key LIKE 'registry-workspace:%')",
            (token,)).fetchall()
        for entry in newer:
            key = entry["key"]
            if key.startswith("registry-workspace:"):
                workspace = key.removeprefix("registry-workspace:")
                row = connection.execute(
                    "SELECT repo_key,is_linked_worktree,label FROM workspaces WHERE host=? AND workspace_id=?",
                    (self.store.host, workspace)).fetchone()
                matches = workspaces.get(workspace) == (tuple(row) if row else None)
            else:
                terminal = key.removeprefix("registry-terminal:")
                row = connection.execute(
                    "SELECT a.*,s.turn_state FROM agents a JOIN agent_status s USING(agent_id) "
                    "WHERE a.host=? AND a.terminal_id=? AND a.active=1",
                    (self.store.host, terminal)).fetchone()
                incoming = observed.get(terminal)
                matches = ((row is None and incoming is None) or
                           (row is not None and incoming is not None and all(
                               row[field] == incoming[field] for field in
                               ("session_ref", "client", "repo_id", "workspace_id", "pane_id",
                                "native_session_pid", "alias", "is_main_checkout", "turn_state"))))
                slot = connection.execute(
                    "SELECT pane_id,workspace_id FROM terminal_slots "
                    "WHERE host=? AND terminal_id=? AND present=1", (self.store.host, terminal)).fetchone()
                location = locations.get(terminal)
                matches = matches and ((slot is None and location is None) or
                    (slot is not None and location is not None and all(
                        slot[field] == location[field] for field in ("pane_id", "workspace_id"))))
            if not matches:
                raise LedgerError("stale_snapshot", "The snapshot conflicts with a newer caller observation.")

    def _commit_observation(self, connection: sqlite3.Connection, token: int,
                            terminals: set[str] | None, *, workspace_ids: set[str] | None = None,
                            changed: bool = True) -> None:
        if terminals is None:
            connection.execute("INSERT OR REPLACE INTO ledger_meta VALUES ('registry-sweep-committed',?)",
                               (str(token),))
            connection.execute("DELETE FROM ledger_meta WHERE key='registry-refresh-pending' "
                               "AND CAST(value AS INTEGER)<=?", (token,))
            connection.execute(
                "DELETE FROM ledger_meta WHERE CAST(value AS INTEGER)<=? AND "
                "(key LIKE 'registry-terminal:%' OR key LIKE 'registry-workspace:%')", (token,))
        else:
            if changed and (self._observation_value(connection, "registry-sweep-committed") > token
                    or any(self._observation_value(connection, "registry-terminal:" + terminal) > token
                           for terminal in terminals)):
                raise LedgerError("stale_snapshot", "A newer registry observation has already committed.")
            if changed:
                pending = max(token, self._observation_value(connection, "registry-refresh-pending"))
                connection.execute("INSERT OR REPLACE INTO ledger_meta VALUES ('registry-refresh-pending',?)",
                                   (str(pending),))
            keys = ["registry-terminal:" + terminal for terminal in terminals]
            keys.extend("registry-workspace:" + workspace for workspace in (workspace_ids or ()))
            for key in keys:
                value = max(token, self._observation_value(connection, key))
                connection.execute("INSERT OR REPLACE INTO ledger_meta VALUES (?,?)", (key, str(value)))

    def _defer_reconciliation(self) -> None:
        token = self._reserve_observation()
        with self.store.transaction(notify=False) as connection:
            pending = max(token, self._observation_value(connection, "registry-refresh-pending"))
            connection.execute("INSERT OR REPLACE INTO ledger_meta VALUES ('registry-refresh-pending',?)",
                               (str(pending),))

    @contextmanager
    def _deferred_reconciliation(self):
        try:
            yield
        except LedgerError as error:
            if error.code in {"ledger_busy", "stale_snapshot", "stale_session", "recipient_absent"}:
                try:
                    self._defer_reconciliation()
                except (LedgerError, sqlite3.Error, OSError):
                    # Preserve the original failure if storage is unavailable.
                    pass
            raise

    def refresh(self, environment: Mapping[str, str] | None = None, *,
                refresh_display: bool = True,
                prune_device_claims: bool = True) -> tuple[dict[str, Any], dict[str, Any]]:
        # Only full sweeps serialize here. Scoped callers fence their commits in
        # SQL instead of waiting for unrelated Git or display I/O.
        with self._deferred_reconciliation(), self.store.lock("registry"):
            token = self._reserve_observation()
            snapshot = live_snapshot(environment)
            changed_panes: set[str] = set()
            result = self._reconcile(snapshot, changed_panes=changed_panes, environment=environment,
                                     prune_device_claims=prune_device_claims, observation=token)
            if refresh_display:
                self._refresh_display(changed_panes, environment=environment, snapshot=snapshot)
        return snapshot, result

    def reconcile(self, snapshot: Mapping[str, Any], *, now: float | None = None,
                  refresh_display: bool = True,
                  environment: Mapping[str, str] | None = None) -> dict[str, Any]:
        """Commit a supplied snapshot; live callers use refresh or refresh_context."""
        with self._deferred_reconciliation(), self.store.lock("registry"):
            token = self._reserve_observation()
            changed_panes: set[str] = set()
            result = self._reconcile(snapshot, now=now, changed_panes=changed_panes,
                                     environment=environment, observation=token)
            if refresh_display:
                self._refresh_display(changed_panes, environment=environment, snapshot=snapshot, now=now)
        return result

    def refresh_context(self, environment: Mapping[str, str] | None = None) -> dict[str, Any]:
        """Reconcile only the ancestry-verified caller, without projecting panes."""
        values = os.environ if environment is None else environment
        terminal = values.get("HERDR_TERMINAL_ID")
        if not terminal:
            terminal = self._resolve_terminal(environment, live_snapshot(environment))
        key = hashlib.sha256(terminal.encode("utf-8")).hexdigest()
        with self._deferred_reconciliation(), self.store.lock("registry-terminal-" + key):
            token = self._reserve_observation()
            snapshot = live_snapshot(environment)
            if self._resolve_terminal(environment, snapshot) != terminal:
                raise LedgerError("sender_unresolved", "The calling terminal changed during registration.")
            self._reconcile(snapshot, changed_panes=set(), environment=environment,
                            prune_device_claims=False, observation=token, caller=terminal)
            with self.store.read() as connection:
                return current_agent(connection, self.store.host, terminal)

    def _reconcile(self, snapshot: Mapping[str, Any], *, changed_panes: set[str],
                   now: float | None = None,
                   environment: Mapping[str, str] | None = None,
                   prune_device_claims: bool = True, observation: int,
                   caller: str | None = None) -> dict[str, Any]:
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
        borders: dict[str, str | None] = {}
        for raw in panes:
            if not isinstance(raw, dict) or not raw.get("terminal_id") or not raw.get("pane_id"):
                raise LedgerError("invalid_snapshot", "Every pane must identify its terminal and location.")
            terminal = str(raw["terminal_id"])
            if terminal in locations:
                raise LedgerError("invalid_snapshot", "Duplicate terminal in snapshot.")
            locations[terminal] = {key: str(raw.get(key) or '') for key in ("terminal_id", "pane_id", "workspace_id")}
            label = raw.get("label", "")
            borders[terminal] = label if isinstance(label, str) else None
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
            if caller is not None and terminal != caller:
                continue
            if terminal not in locations:
                raise LedgerError("invalid_snapshot", "An agent has no matching terminal pane.")
            if raw.get("pane_id") != locations[terminal]["pane_id"]:
                raise LedgerError("invalid_snapshot", "An agent has a mismatched terminal/pane binding.")
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
        if caller is not None:
            if caller not in observed:
                raise LedgerError("recipient_absent", "The calling terminal no longer has an agent.")
            # Git may take time even for the caller. Refuse a replaced session,
            # cwd or binding rather than commit evidence from before that I/O.
            latest = live_snapshot(environment)
            if self._resolve_terminal(environment, latest) != caller:
                raise LedgerError("sender_unresolved", "The calling terminal changed during registration.")
            def caller_evidence(value: Mapping[str, Any]) -> tuple[Any, ...]:
                pane = next((raw for raw in value["panes"] if raw.get("terminal_id") == caller), None)
                agent = next((raw for raw in value["agents"]
                              if raw.get("terminal_id") == caller and raw.get("agent")), None)
                workspace = next((raw for raw in value.get("workspaces", [])
                                  if pane is not None and raw.get("workspace_id") == pane.get("workspace_id")), None)
                worktree = workspace.get("worktree") if workspace is not None else None
                metadata = (None if workspace is None else (
                    str(worktree.get("repo_key") or "") if isinstance(worktree, dict) else "",
                    int(bool(worktree.get("is_linked_worktree"))) if isinstance(worktree, dict) else 0,
                    str(workspace.get("label") or workspace["workspace_id"])))
                return (None if pane is None else tuple(pane.get(key) for key in
                        ("terminal_id", "pane_id", "workspace_id")),
                        None if agent is None else tuple(agent.get(key) for key in
                        ("agent", "agent_session", "cwd", "pane_id", "workspace_id")), metadata)
            if caller_evidence(snapshot) != caller_evidence(latest):
                raise LedgerError("stale_session", "The calling Herdr session changed during registration.")
        wake_terminals: list[str] = []
        with self.store.read() as connection:
            active = [dict(row) for row in connection.execute(
                "SELECT * FROM agents WHERE host=? AND active=1", (self.store.host,))]
        terminals = None if caller is None else {
            caller, *(row["terminal_id"] for row in active
                      if row["pane_id"] == locations[caller]["pane_id"])}
        if terminals is not None and (terminals - observed.keys()) & locations.keys():
            raise LedgerError("stale_snapshot", "A live displaced terminal requires a full binding reconciliation.")
        binding_panes = {row["pane_id"] for row in active
                         if terminals is None or row["terminal_id"] in terminals}
        binding_panes.update(value["pane_id"] for terminal, value in locations.items()
                             if terminals is None or terminal in terminals)
        lock_panes = {self.store.display_lock_key(self.store.host, pane_id): pane_id
                      for pane_id in binding_panes}
        if terminals is not None:
            prior = {row["terminal_id"]: row for row in active}
            if all(not self._binding_changed(prior.get(terminal), observed.get(terminal))
                   for terminal in terminals):
                lock_panes.clear()
        with ExitStack() as locks:
            # Never hold a caller's slot while waiting for another pane. Full
            # sweeps defer atomically when any slot is busy; scoped transitions
            # acquire only their own old/new binding slots, in the same order.
            for _key, pane_id in sorted(lock_panes.items()):
                locks.enter_context(self.store.display_lock(
                    self.store.host, pane_id, timeout=0 if caller is None else 30,
                    error_code="ledger_busy"))
            if caller is not None and lock_panes:
                # A projector may have held these binding slots since the
                # earlier probe. Revalidate after the wait, before touching SQL.
                identity = repository_identity(cwd, self.store.host)
                latest = live_snapshot(environment)
                if self._resolve_terminal(environment, latest) != caller:
                    raise LedgerError("sender_unresolved", "The calling terminal changed during registration.")
                if (caller_evidence(snapshot) != caller_evidence(latest)
                        or any(identity[key] != observed[caller][key] for key in identity)
                        or (observed[caller]["native_session_pid"] is not None
                            and claude_foreground_pid(observed[caller], environment,
                                                      require_ancestor=False)
                            != observed[caller]["native_session_pid"])):
                    raise LedgerError("stale_session", "The calling Herdr identity changed while waiting for its binding.")
            result = self._reconcile_bindings(
                observed, locations, workspaces, borders, at, changed_panes, wake_terminals,
                prune_device_claims=prune_device_claims, observation=observation,
                terminals=terminals, lock_keys=set(lock_panes))
        return result

    @staticmethod
    def _binding_changed(old: Mapping[str, Any] | None, value: Mapping[str, Any] | None) -> bool:
        if old is None or value is None:
            return old is not value
        return any(old[key] != value[key] for key in
                   ("session_ref", "client", "repo_id", "workspace_id", "pane_id", "native_session_pid"))

    def _reconcile_bindings(self, observed: Mapping[str, Any], locations: Mapping[str, Any],
                            workspaces: Mapping[str, Any], borders: Mapping[str, Any],
                            at: float, changed_panes: set[str], wake_terminals: list[str], *,
                            prune_device_claims: bool, observation: int,
                            terminals: set[str] | None, lock_keys: set[str]) -> dict[str, Any]:
        live_terminals = set(locations)
        business_changed = False
        with self.store.transaction(wake_terminals, notify=lambda: business_changed) as connection:
            if terminals is not None:
                caller_panes = {value["pane_id"] for value in observed.values()}
                terminals.update(row["terminal_id"] for row in connection.execute(
                    "SELECT terminal_id,pane_id FROM agents WHERE host=? AND active=1",
                    (self.store.host,)) if row["pane_id"] in caller_panes)
                if (terminals - observed.keys()) & locations.keys():
                    raise LedgerError("stale_snapshot", "A live displaced terminal requires a full binding reconciliation.")
            active_bindings = [dict(row) for row in connection.execute(
                "SELECT * FROM agents WHERE host=? AND active=1", (self.store.host,))
                if terminals is None or row["terminal_id"] in terminals]
            old_by_terminal = {row["terminal_id"]: row for row in active_bindings}
            changed_bindings = {terminal for terminal in old_by_terminal.keys() | observed.keys()
                                if self._binding_changed(old_by_terminal.get(terminal), observed.get(terminal))}
            required_panes = {value["pane_id"] for terminal, value in
                              [*old_by_terminal.items(), *observed.items()]
                              if terminals is None or terminal in changed_bindings}
            if any(self.store.display_lock_key(self.store.host, pane) not in lock_keys
                   for pane in required_panes):
                raise LedgerError("stale_snapshot", "A binding moved before its display lock was acquired.")
            if terminals is None:
                self._fence_sweep(connection, observation, observed, locations, workspaces)
            before = connection.total_changes
            from ham.ledger.settings import RepoSettings
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
            previous_workspaces = {row["workspace_id"]: (row["repo_key"], row["is_linked_worktree"], row["label"])
                                   for row in connection.execute("SELECT * FROM workspaces WHERE host=?",
                                                                 (self.store.host,))}
            workspace_ids = {value["workspace_id"] for value in observed.values()}
            touched_workspaces = previous_workspaces.keys() | workspaces.keys() if terminals is None else workspace_ids
            for workspace_id in touched_workspaces:
                metadata = workspaces.get(workspace_id)
                if (terminals is not None and previous_workspaces.get(workspace_id) != metadata
                        and self._observation_value(connection, "registry-workspace:" + workspace_id) > observation):
                    raise LedgerError("stale_snapshot", "Workspace metadata conflicts with a newer caller observation.")
                if metadata is None:
                    if workspace_id in previous_workspaces:
                        connection.execute("DELETE FROM workspaces WHERE host=? AND workspace_id=?",
                                           (self.store.host, workspace_id))
                    continue
                if previous_workspaces.get(workspace_id) != metadata:
                    connection.execute("INSERT INTO workspaces VALUES (?,?,?,?,?) "
                                       "ON CONFLICT(host,workspace_id) DO UPDATE SET repo_key=excluded.repo_key,"
                                       "is_linked_worktree=excluded.is_linked_worktree,label=excluded.label",
                                       (self.store.host, workspace_id, *metadata))
            for scope, old_anchor in old_scope_by_workspace.items():
                if settings.scope_workspace(connection, *scope) != old_anchor:
                    affected_scopes.add(scope)
            old_terminals = {row[0] for row in connection.execute(
                "SELECT terminal_id FROM terminal_slots WHERE host=? AND present=1", (self.store.host,))}
            for terminal, location in locations.items():
                if terminals is not None and terminal not in terminals:
                    continue
                connection.execute(
                    "INSERT INTO terminal_slots VALUES (?,?,?,?,1,?) ON CONFLICT(host,terminal_id) DO UPDATE SET "
                    "pane_id=excluded.pane_id,workspace_id=excluded.workspace_id,present=1,updated_at=excluded.updated_at "
                    "WHERE terminal_slots.pane_id IS NOT excluded.pane_id "
                    "OR terminal_slots.workspace_id IS NOT excluded.workspace_id OR terminal_slots.present<>1",
                    (self.store.host, terminal, location["pane_id"], location["workspace_id"], at))
                connection.execute("INSERT OR IGNORE INTO pane_bindings VALUES (?,?,?)",
                                   (self.store.host, location["pane_id"], terminal))
            for terminal in old_terminals - locations.keys() if terminals is None else terminals - locations.keys():
                connection.execute("UPDATE terminal_slots SET present=0,updated_at=? WHERE host=? AND terminal_id=?",
                                   (at, self.store.host, terminal))
            active = {row["terminal_id"]: row for row in active_bindings}
            for terminal, old in active.items():
                observed_agent = observed.get(terminal)
                if not observed_agent or any(old[key] != observed_agent[key] for key in ("session_ref", "client", "repo_id")):
                    affected_scopes.add((old["repo_id"], old["host"], old["workspace_id"]))
                    wake_terminals.append(terminal)
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
                    if terminals is None or any(old[key] != value[key] for key in
                            ("workspace_id", "pane_id", "alias", "is_main_checkout", "native_session_pid")):
                        connection.execute(
                            "UPDATE agents SET workspace_id=?,pane_id=?,alias=?,is_main_checkout=?,last_seen=?,"
                            "native_session_pid=? WHERE agent_id=?",
                            (value["workspace_id"], value["pane_id"], value["alias"], value["is_main_checkout"], at,
                             value["native_session_pid"], agent_id))
                else:
                    wake_terminals.append(terminal)
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
                current_border = borders[terminal]
                if current_border is not None and (terminals is None or lock_keys) and (
                        not same or old["last_ham_label"] is None):
                    current = dict(connection.execute(
                        "SELECT * FROM agents WHERE agent_id=?", (agent_id,)).fetchone())
                    previous = recoverable_display_owner(
                        connection, current, current_border, live_terminals=live_terminals)
                    if previous is not None:
                        connection.execute("UPDATE agents SET last_ham_label=? WHERE agent_id=?",
                                           (current_border, agent_id))
                if self._status(connection, agent_id, "turn_state", value["turn_state"], at):
                    wake_terminals.append(terminal)
                    changed_panes.add(value["pane_id"])
            affected_scopes.update(settings.reconcile_auto_main(connection, at))
            # A move, arrival or departure can change the derived main for every
            # survivor in both scopes, even when their own registry rows did not change.
            current_panes = {row["terminal_id"]: row["pane_id"] for row in connection.execute(
                "SELECT terminal_id,pane_id FROM agents WHERE host=? AND active=1", (self.store.host,))}
            for repo_id, host, workspace_id in affected_scopes:
                old_anchor = old_scope_by_workspace.get((repo_id, host, workspace_id), workspace_id)
                wake_terminals.extend(old_scope_terminals.get((repo_id, host, old_anchor), ()))
                wake_terminals.extend(agent["terminal_id"] for agent in settings.scope_agents(
                    connection, repo_id, host, workspace_id))
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
                if terminals is not None and terminal not in terminals:
                    continue
                target = observed.get(terminal)
                if row["state"] == "delivering":
                    reason = "acceptance_unknown"
                elif terminal not in locations:
                    connection.execute("UPDATE deliveries SET state='undeliverable',hold_reason='terminal_closed',"
                                       "updated_at=? WHERE message_id=? AND state='queued'", (at, row["message_id"]))
                    connection.execute("UPDATE messages SET final_at=? WHERE message_id=?", (at, row["message_id"]))
                    changed_ids.append(row["message_id"])
                    wake_terminals.append(terminal)
                    continue
                elif not target:
                    reason = "recipient_absent"
                elif target["repo_id"] != row["recipient_repo_id"]:
                    reason = "recipient_changed"
                else:
                    reason = None
                location = locations.get(terminal, {})
                updated = connection.execute(
                    "UPDATE deliveries SET hold_reason=?,workspace_id=?,pane_id=? WHERE message_id=? "
                    "AND (hold_reason IS NOT ? OR workspace_id IS NOT ? OR pane_id IS NOT ?)",
                    (reason, location.get("workspace_id", ''), location.get("pane_id", ''), row["message_id"],
                     reason, location.get("workspace_id", ''), location.get("pane_id", ''))).rowcount
                if updated:
                    wake_terminals.append(terminal)
            for message_id in changed_ids:
                save_receipt(connection, message_id)
            if prune_device_claims:
                from ham.ledger.devices import prune_stale
                changed_panes.update(prune_stale(connection))
            business_changed = connection.total_changes != before
            self._commit_observation(connection, observation, terminals,
                                     workspace_ids=workspace_ids, changed=business_changed)
        return {"agents": self.list(), "terminals": len(locations)}

    @staticmethod
    def _status(connection: sqlite3.Connection, agent_id: str, field: str, value: str, at: float) -> bool:
        # Field is selected by internal code, never interpolated from external input.
        if field not in {"turn_state", "phase", "summary"}:
            raise LedgerError("invalid_status_field", "Unknown agent status field.")
        if field in {"phase", "summary"}:
            value = sanitize_status_text(value, 64 if field == "phase" else 200)
        old = connection.execute(f"SELECT {field} FROM agent_status WHERE agent_id=?", (agent_id,)).fetchone()
        if old is not None and old[0] != value:
            connection.execute(f"UPDATE agent_status SET {field}=?,updated_at=? WHERE agent_id=?", (value, at, agent_id))
            connection.execute("INSERT INTO status_log(agent_id,field,value,at) VALUES (?,?,?,?)", (agent_id, field, value, at))
            return True
        return False

    def list(self, repo_id: str | None = None, *,
             connection: sqlite3.Connection | None = None) -> list[dict[str, Any]]:
        with self.store.read() if connection is None else nullcontext(connection) as connection:
            query = ("SELECT a.*,s.turn_state,s.phase,s.summary,s.updated_at AS status_updated_at "
                     "FROM agents a JOIN agent_status s USING(agent_id) WHERE a.active=1")
            parameters: tuple[Any, ...] = ()
            if repo_id is not None:
                query += " AND a.repo_id=?"
                parameters = (repo_id,)
            from ham.ledger.settings import RepoSettings
            settings = RepoSettings(self.store)
            return [sanitize_agent_status({**dict(row), "role": settings.role(row, connection=connection),
                     "role_workspace_id": settings.scope_workspace(
                         connection, row["repo_id"], row["host"], row["workspace_id"])})
                    for row in connection.execute(query + " ORDER BY a.host,a.alias,a.terminal_id", parameters)]

    def resolve_context(self, environment: Mapping[str, str] | None = None,
                        snapshot: Mapping[str, Any] | None = None) -> dict[str, Any]:
        if snapshot is None:
            snapshot = live_snapshot(environment)
        terminal = self._resolve_terminal(environment, snapshot)
        with self.store.read() as connection:
            return current_agent(connection, self.store.host, terminal)

    def _resolve_terminal(self, environment: Mapping[str, str] | None,
                          snapshot: Mapping[str, Any]) -> str:
        values = os.environ if environment is None else environment
        terminal = values.get("HERDR_TERMINAL_ID")
        inherited_pane = values.get("HERDR_PANE_ID", '')
        if terminal:
            claimed = next((raw for raw in snapshot["panes"] if raw["terminal_id"] == terminal), None)
            if claimed is None:
                raise LedgerError("sender_unresolved", "The claimed terminal is not an ancestor of this process.")
            shell_pid = pane_shell_pid(claimed["pane_id"], environment)
            if shell_pid not in process_ancestors(shell_pid):
                raise LedgerError("sender_unresolved", "The claimed terminal is not an ancestor of this process.")
        else:
            with self.store.read() as connection:
                cached = {row[0] for row in connection.execute(
                    "SELECT terminal_id FROM pane_bindings WHERE host=? AND pane_id=?",
                    (self.store.host, inherited_pane))}
            # Cached bindings prioritize probes but cannot establish identity.
            panes = sorted(snapshot["panes"], key=lambda raw: raw["terminal_id"] not in cached)
            lookup_error = None
            for raw in panes:
                try:
                    shell_pid = pane_shell_pid(raw["pane_id"], environment)
                    ancestors = process_ancestors(shell_pid)
                except LedgerError as error:
                    if error.code != "sender_lookup_failed":
                        raise
                    lookup_error = error
                    continue
                if shell_pid in ancestors:
                    terminal = raw["terminal_id"]
                    break
            if not terminal:
                if lookup_error is not None:
                    raise lookup_error
                raise LedgerError("sender_unresolved", "Cannot match this process to a live Herdr terminal; refusing inherited pane guesses.")
        return terminal

    def agent_session(self, *, terminal_id: str, session_ref: str, client: str,
                      repo_id: str, extension_present: bool = True,
                      expected_agent_id: str | None = None, expected_generation: int | None = None,
                      environment: Mapping[str, str] | None = None) -> dict[str, Any]:
        # Share only this terminal's capture lock with scoped reconciliation.
        key = hashlib.sha256(terminal_id.encode("utf-8")).hexdigest()
        with self.store.lock("registry-terminal-" + key), ExitStack() as locks:
            native_pid = None
            if client == "claude":
                observation = self._reserve_observation()
                with self.store.read() as connection:
                    candidate = current_agent(connection, self.store.host, terminal_id)
                if candidate["session_ref"] == "unreported" and session_ref != "unreported":
                    locks.enter_context(self.store.display_lock(
                        self.store.host, candidate["pane_id"], timeout=30, error_code="ledger_busy"))
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
                if client == "claude" and any(agent[key] != candidate[key] for key in
                        ("agent_id", "generation", "pane_id", "workspace_id", "repo_id")):
                    raise LedgerError("stale_session", "The native root binding changed during registration.")
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
                    self._commit_observation(connection, observation, {terminal_id})
                elif (client == "claude" and agent["native_session_pid"] is not None
                      and agent["native_session_pid"] != native_pid):
                    raise LedgerError("stale_session", "The pending native root process has changed.")
                connection.execute("UPDATE agents SET extension_present=? WHERE agent_id=? AND extension_present<>?",
                                   (bool(extension_present), agent["agent_id"], bool(extension_present)))
                agent["extension_present"] = bool(extension_present)
        return agent

    def set_status(self, agent: Mapping[str, Any], *, phase: str, summary: str,
                   feature_invoked: bool = False,
                   environment: Mapping[str, str] | None = None) -> dict[str, Any]:
        """Publish committed status; explicit feature use also refreshes unchanged values."""
        phase = sanitize_status_text(phase, 64)
        summary = sanitize_status_text(summary, 200)
        with self.store.transaction() as connection:
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

    def _display_plan(self, display: Any, pane_ids: set[str],
                      snapshot: Mapping[str, Any] | None) -> tuple[dict[str, Any], set[str], set[str]]:
        from ham.ledger.display import border_owned, metadata_matches
        from ham.ledger.display_repair import fingerprint

        agents = {agent["pane_id"]: agent for agent in self.list() if agent["host"] == self.store.host}
        if snapshot is None:
            # Explicit device cleanup and the later safety sweep must use the
            # same binding fingerprint, while a replacement terminal resets it.
            with self.store.read() as connection:
                panes = {row["pane_id"]: dict(row) for row in connection.execute(
                    "SELECT pane_id,terminal_id FROM terminal_slots WHERE host=? AND present=1",
                    (self.store.host,))}
        else:
            panes = {pane["pane_id"]: pane for pane in snapshot["panes"]}
        observed = {} if snapshot is None else {raw["pane_id"]: raw for raw in snapshot["agents"]
                                               if raw.get("agent")}
        targets = pane_ids if snapshot is None else pane_ids | agents.keys() | panes.keys()
        plan: dict[str, Any] = {}
        matching: set[str] = set()
        for pane_id in targets:
            agent = agents.get(pane_id)
            raw = observed.get(pane_id)
            pane = panes.get(pane_id)
            if snapshot is not None and pane is None:
                continue
            if agent is not None:
                desired = display.projection(agent)
                if snapshot is not None:
                    if raw is None or raw.get("terminal_id") != agent["terminal_id"]:
                        continue
                    current = pane.get("label", "")
                    owned = isinstance(current, str) and border_owned(current, agent["last_ham_label"])
                    if (metadata_matches(desired, raw)
                            and ((current == desired.label and agent["last_ham_label"] == desired.label)
                                 or (current != desired.label and not owned))):
                        matching.add(pane_id)
                        continue
                desired_key = fingerprint(agent, desired)
            else:
                # Only device tokens survive an agent's exit. Never project an
                # old identity into a replacement pane or revive other fields.
                if raw is not None:
                    continue
                tokens = {} if pane is None else pane.get("tokens") or {}
                if snapshot is not None and not any(tokens.get(key) for key in ("phone", "emu")):
                    matching.add(pane_id)
                    continue
                desired_key = fingerprint({"host": self.store.host, **(pane or {"pane_id": pane_id})})
            plan[pane_id] = (desired_key, raw)
        return plan, matching, set(panes)

    def defer_display(self, agent: Mapping[str, Any], projection: Any, *,
                      now: float | None = None) -> bool:
        """Retain a contended explicit projection without claiming it finished."""
        from ham.ledger import display_repair

        with self.store.transaction() as connection:
            current = connection.execute(
                "SELECT agent_id,pane_id FROM agents WHERE host=? AND terminal_id=? AND active=1",
                (self.store.host, agent["terminal_id"])).fetchone()
            if (current is None or agent["host"] != self.store.host
                    or current["agent_id"] != agent["agent_id"] or current["pane_id"] != agent["pane_id"]):
                return False
            entries = display_repair.load(connection)
            before = json.dumps(entries, sort_keys=True)
            display_repair.defer(entries, agent["pane_id"], display_repair.fingerprint(agent, projection),
                                 time.time() if now is None else now)
            if json.dumps(entries, sort_keys=True) != before:
                display_repair.save(connection, entries)
            return agent["pane_id"] in entries

    def _refresh_display(self, pane_ids: set[str], *,
                         environment: Mapping[str, str] | None = None,
                         feature_invoked: bool = False,
                         snapshot: Mapping[str, Any] | None = None,
                         now: float | None = None) -> dict[str, list[str]]:
        # Snapshot repairs reserve durably until a later snapshot confirms them.
        # Explicit writes queue only failures, avoiding maintenance for success.
        from ham.ledger import display_repair
        result: dict[str, list[str]] = {"updated": [], "failed": []}
        attempts: dict[str, str] = {}
        skipped: set[str] = set()
        prior_reservations: dict[str, dict[str, Any] | None] = {}
        saved_reservations: dict[str, dict[str, Any]] = {}
        diagnostics: list[str] = []
        at = time.time() if now is None else now
        try:
            from ham.ledger.display import LedgerDisplay
            runner = self.display_runner
            if runner is None:
                runner = lambda command: run_bounded_command(
                    [herdr_executable(environment), *command], timeout=budget_timeout(10), environment=environment)
            display = LedgerDisplay(self.store, runner=runner)
            # Include pending shell device cleanup even without registry changes.
            with self.store.read() as connection:
                queued = display_repair.load(connection)
            targets = pane_ids | queued.keys() if snapshot is not None else pane_ids
            plan, matching, present = self._display_plan(display, targets, snapshot)
            reserve_durably = snapshot is not None and bool(plan or queued)
            with (self.store.transaction() if reserve_durably else self.store.read()) as connection:
                entries = display_repair.load(connection)
                before = json.dumps(entries, sort_keys=True)
                for pane_id in list(entries):
                    # A stale snapshot cannot confirm a concurrently deferred request.
                    if (entries[pane_id] == queued.get(pane_id)
                            and (pane_id in matching or (snapshot is not None and pane_id not in present))):
                        del entries[pane_id]
                for pane_id, (desired, _raw) in sorted(plan.items()):
                    previous = entries.get(pane_id)
                    prior_reservations[pane_id] = None if previous is None else dict(previous)
                    allowed, diagnostic = display_repair.reserve(
                        entries, pane_id, desired, at, force=snapshot is None)
                    if diagnostic:
                        diagnostics.append(diagnostic)
                    if allowed:
                        attempts[pane_id] = desired
                        saved_reservations[pane_id] = dict(entries[pane_id])
                    elif snapshot is None:
                        # An explicit caller must not mistake an exhausted or
                        # capacity-blocked projection for a successful redraw.
                        result["failed"].append(pane_id)
                if reserve_durably and json.dumps(entries, sort_keys=True) != before:
                    display_repair.save(connection, entries)
            for pane_id in attempts:
                try:
                    arguments: dict[str, Any] = {"feature_invoked": feature_invoked}
                    if snapshot is not None:
                        arguments["observed"] = plan[pane_id][1]
                    refreshed = display.refresh(pane_id, **arguments)
                    for key in result:
                        result[key].extend(refreshed[key])
                    skipped.update(refreshed.get("skipped", ()))
                except Exception as error:
                    result["failed"].append(pane_id)
                    diagnostics.append(str(error))
            detail = "Could not refresh panes: " + ", ".join(result["failed"])
            if result["failed"]:
                diagnostics.append(detail)
            if skipped or result["failed"] or (snapshot is None and any(
                    pane_id in queued for pane_id in result["updated"])):
                with self.store.transaction() as connection:
                    entries = display_repair.load(connection)
                    before = json.dumps(entries, sort_keys=True)
                    # An explicit defer marks its desired fingerprint even on
                    # our in-flight reservation. Refund this sweep's attempt,
                    # but never discard that independently committed request.
                    for pane_id in (skipped if reserve_durably else ()):
                        entry = entries.get(pane_id)
                        if entry is None:
                            continue
                        expected = dict(saved_reservations[pane_id])
                        observed = dict(entry)
                        explicit = observed.pop("explicit_pending", None)
                        expected.pop("explicit_pending", None)
                        if observed == expected:
                            previous = prior_reservations[pane_id]
                            if previous is None:
                                del entries[pane_id]
                            else:
                                entries[pane_id] = dict(previous)
                            if explicit is not None:
                                display_repair.defer(entries, pane_id, explicit, at)
                    if snapshot is None:
                        # Status can commit while another projector holds the
                        # pane lock. Keep its deferred display alive even when
                        # that older projector also detects the changed status.
                        for pane_id in skipped:
                            display_repair.defer(entries, pane_id, attempts[pane_id], at)
                        for pane_id in result["updated"]:
                            entry = entries.get(pane_id)
                            previous = queued.get(pane_id, {})
                            if (entry and entry["fingerprint"] in (
                                    attempts.get(pane_id), previous.get("fingerprint"))
                                    and entry.get("explicit_pending") in (None, attempts.get(pane_id))):
                                del entries[pane_id]
                        for pane_id in result["failed"]:
                            if pane_id in attempts:
                                explicit = entries.get(pane_id, {}).get("explicit_pending")
                                display_repair.reserve(
                                    entries, pane_id, attempts[pane_id], at, force=True)
                                if explicit is not None and explicit != attempts[pane_id]:
                                    display_repair.defer(entries, pane_id, explicit, at)
                    for pane_id in result["failed"]:
                        entry = entries.get(pane_id)
                        if entry and entry["fingerprint"] == attempts.get(pane_id):
                            diagnostic = display_repair.failed(entry, detail)
                            if diagnostic:
                                diagnostics.append(diagnostic)
                    if json.dumps(entries, sort_keys=True) != before:
                        display_repair.save(connection, entries)
        except Exception as error:
            # The registry transaction has committed. Projection is advisory.
            result["failed"].extend(sorted(pane_ids - set(result["updated"]) - set(result["failed"])))
            diagnostics.append(str(error))
        for detail in diagnostics:
            self._display_failure(detail)
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
            if not row["active"] or current is None or current["agent_id"] != agent_id:
                return {"updated": False, "last_ham_label": last}
            if last == desired:
                # Another projector may already have saved the exact same
                # ownership while this one was reading the current border.
                return {"updated": True, "last_ham_label": last}
            if last != expected_last:
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
        from ham.ledger.settings import RepoSettings
        return {**RepoSettings(self.store).show(repo_id, host=host, workspace_id=workspace_id),
                "agents": self.list(repo_id)}
