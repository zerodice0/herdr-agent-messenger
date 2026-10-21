#!/usr/bin/env python3
"""Responsive mailbox inspection and explicit user-origin quick send.

Durable mutations belong to ledger services; drafts and previews stay local.
"""
from __future__ import annotations

from ham.paths import PLUGIN_ROOT

import argparse
from contextlib import redirect_stdout
import curses
import fcntl
import json
import locale
import math
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile
import time
import unicodedata
from typing import Any, Mapping, Sequence

from ham.agents.directory import (dispatch_user_prompts, herdr_executable, query_local_agents,
                             query_remote_agents, ssh_config_path, user_prompt_text)
from ham.ui import messenger as agent_messenger
from ham.ui.messenger import focused_pane_id, terminal_flow_control_disabled
from ham.ui import key_input as key_input
from ham.ledger.mail import Mailbox
from ham.ledger.remote import RemoteLedger
from ham.ledger.settings import DEFAULT_THRESHOLDS, RepoSettings
from ham.ledger.status import repository_identity
from ham.ledger.store import LedgerError, LedgerStore, ledger_directory
from ham.ledger.views import status_board
from ham.ui.tui_style import Theme, Hint, content_rect, draw_footer, draw_title, footer_lines
from ham.ui.terminal_text import (character_index_at_display_column, display_width,
                           pad_display_text, truncate_display_text,
                           wrap_display_text, wrap_message_lines)

PLUGIN = "herdr.agent-messenger"
OPENER_PANE = "HERDR_HAM_OPENER_PANE_ID"
OPENER_TERMINAL = "HERDR_HAM_OPENER_TERMINAL_ID"
REPO_ENV = "HERDR_HAM_REPO_ID"
LOCAL_INTERVAL = 1.0
REMOTE_INTERVAL = 30.0
JOB_TIMEOUT = 25.0
METADATA_TTL = 30.0


def herdr(arguments: Sequence[str], environment: Mapping[str, str]) -> dict[str, Any]:
    """Use an explicit environment so isolated panes never reach the live server."""
    try:
        result = subprocess.run([herdr_executable(environment), *arguments], env=dict(environment),
                                stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise LedgerError("herdr_unavailable", str(error)) from error
    try:
        value = json.loads(result.stdout)
    except ValueError:
        value = {}
    if result.returncode or value.get("error") or value.get("ok") is False:
        raise LedgerError("herdr_failed", result.stderr.strip() or result.stdout.strip() or "Herdr command failed.")
    payload = value.get("result")
    if not isinstance(payload, dict):
        raise LedgerError("invalid_herdr_response", "Herdr returned no structured result.")
    return payload


def visible_pane_text(pane_id: str, environment: Mapping[str, str]) -> str:
    """Pane reads in text format return plain stdout, not a JSON envelope."""
    try:
        result = subprocess.run(
            [herdr_executable(environment), "pane", "read", pane_id,
             "--source", "visible", "--format", "text"],
            env=dict(environment), stdin=subprocess.DEVNULL,
            capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise LedgerError("herdr_unavailable", str(error)) from error
    if result.returncode:
        raise LedgerError("herdr_failed", result.stderr.strip() or result.stdout.strip() or "Herdr pane read failed.")
    return result.stdout


def pane_record(pane_id: str, environment: Mapping[str, str]) -> dict[str, Any]:
    result = herdr(["pane", "get", pane_id], environment)
    pane = result.get("pane")
    if not isinstance(pane, dict) or not pane.get("terminal_id"):
        raise LedgerError("pane_unavailable", "Pane has no live terminal identity.")
    return pane


def ui_state_directory(environment: Mapping[str, str]) -> Path:
    path = Path(environment.get("HERDR_PLUGIN_STATE_DIR") or ledger_directory(environment).parent) / "mailbox-ui"
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


def save_pane_state(path: Path, state: Mapping[str, Any]) -> None:
    temporary = path.with_name(f"panes.{os.getpid()}.json")
    temporary.write_text(json.dumps(state, ensure_ascii=False))
    temporary.chmod(0o600)
    temporary.replace(path)


def launch(placement: str, environment: Mapping[str, str] | None = None) -> dict[str, Any]:
    values = dict(os.environ if environment is None else environment)
    opener_id = focused_pane_id(values)
    if not opener_id:
        raise LedgerError("opener_unavailable", "Open HAM from a live workspace pane (an agent is not required).")
    opener = pane_record(opener_id, values)
    workspace = str(opener.get("workspace_id") or values.get("HERDR_WORKSPACE_ID") or opener_id.split(":")[0])
    directory = ui_state_directory(values)
    # Serialize double key presses, not message/schema state.
    with (directory / "panes.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        path = directory / "panes.json"
        try:
            state = json.loads(path.read_text())
        except (OSError, ValueError):
            state = {}
        if not isinstance(state, dict):
            state = {}
        if state:
            snapshot = herdr(["api", "snapshot"], values)
            panes = snapshot.get("snapshot", snapshot).get("panes", [])
            by_terminal = {pane["terminal_id"]: pane for pane in panes}
            moved: dict[str, Any] = {}
            for record in state.values():
                if not isinstance(record, dict):
                    continue
                live = by_terminal.get(record.get("terminal_id"))
                if live is not None:
                    current_workspace = str(live.get("workspace_id") or live["pane_id"].split(":")[0])
                    moved[current_workspace] = {**record, "pane_id": live["pane_id"],
                                                "workspace_id": current_workspace}
            state = moved
            save_pane_state(path, state)
        saved = state.get(workspace)
        previous = None
        if isinstance(saved, dict):
            try:
                live = pane_record(saved["pane_id"], values)
                if live["terminal_id"] == saved["terminal_id"]:
                    if placement != "split" or (opener.get("tab_id") and
                                                live.get("tab_id") == opener["tab_id"]):
                        herdr(["plugin", "pane", "focus", saved["pane_id"]], values)
                        return {"focused": True, **saved}
                    previous = saved
            except (LedgerError, KeyError):
                pass
        store = LedgerStore(environment=values)
        cwd = str(opener.get("cwd") or values.get("PWD") or os.getcwd())
        repo_id = repository_identity(cwd, store.host)["repo_id"]
        arguments = ["plugin", "pane", "open", "--plugin", PLUGIN, "--entrypoint", "mailbox",
                     "--placement", placement,
                     "--cwd", str(PLUGIN_ROOT), "--focus"]
        if placement == "split":
            arguments.extend(["--target-pane", opener_id, "--direction", "right"])
        else:
            arguments.extend(["--workspace", workspace])
        for key, value in ((OPENER_PANE, opener_id), (OPENER_TERMINAL, opener["terminal_id"]),
                           (REPO_ENV, repo_id), ("HERDR_HAM_PLUGIN_OWNED", "1")):
            arguments.extend(["--env", f"{key}={value}"])
        result = herdr(arguments, values)
        pane = result.get("plugin_pane", {}).get("pane", {})
        pane_id = str(pane.get("pane_id") or "")
        if not pane_id:
            raise LedgerError("invalid_herdr_response", f"Plugin open returned no pane ID: {json.dumps(result)}")
        live = pane_record(pane_id, values)
        saved = {"pane_id": pane_id, "terminal_id": live["terminal_id"], "workspace_id": workspace,
                 "opener_pane_id": opener_id, "opener_terminal_id": opener["terminal_id"]}
        state[workspace] = saved
        save_pane_state(path, state)
        result = {"focused": False, **saved}
        if previous is not None:
            old_pane = previous["pane_id"]
            result["kept"] = old_pane
            try:
                old_live = pane_record(old_pane, values)
                if old_live["terminal_id"] == previous["terminal_id"]:
                    lines = visible_pane_text(old_pane, values).splitlines()
                    if lines and "NAV ·" in lines[0]:
                        herdr(["plugin", "pane", "close", old_pane], values)
                        result.pop("kept")
                        result["retired"] = old_pane
            except LedgerError:
                pass
        return result


def close_owned(environment: Mapping[str, str]) -> bool:
    if environment.get("HERDR_HAM_PLUGIN_OWNED") != "1":
        return False
    # Resolve by stable terminal first, since a pane may have moved workspace.
    terminal = environment.get("HERDR_TERMINAL_ID")
    pane_id = environment.get("HERDR_PANE_ID", "")
    if terminal:
        snapshot = herdr(["api", "snapshot"], environment)
        container = snapshot.get("snapshot", snapshot)
        for pane in container.get("panes", []):
            if pane.get("terminal_id") == terminal:
                pane_id = str(pane["pane_id"])
                break
        else:
            return False
    if not pane_id:
        return False
    herdr(["plugin", "pane", "close", pane_id], environment)
    return True


def settings_scope(store: LedgerStore, repo_id: str | None,
                   environment: Mapping[str, str], panes: Sequence[Mapping[str, Any]] = (),
                   *, live: bool = False) -> dict[str, Any]:
    """Resolve the opener's current host-local workspace, never the selected agent's."""
    pane_id = environment.get(OPENER_PANE)
    terminal_id = environment.get(OPENER_TERMINAL)
    workspace_id = None
    if pane_id or terminal_id:
        with store.read() as connection:
            for table, condition in (("agents", "active=1"), ("terminal_slots", "present=1")):
                row = connection.execute(
                    f"SELECT workspace_id FROM {table} WHERE host=? AND {condition} AND "
                    + ("terminal_id=?" if terminal_id else "pane_id=?"),
                    (store.host, terminal_id or pane_id)).fetchone()
                if row is not None:
                    workspace_id = row["workspace_id"]
                    break
    if not workspace_id:
        if live:
            try:
                result = herdr(["api", "snapshot"], environment)
                panes = result.get("snapshot", result).get("panes", [])
            except LedgerError:
                panes = ()
        # Reuse the refresh's live panes. A shell need not be in the ledger,
        # and the Mailbox itself remains in the opener's workspace if it closes.
        opener = next((pane for pane in panes if
                       (pane.get("terminal_id") == terminal_id if terminal_id else
                        pane.get("pane_id") == pane_id)), None) if terminal_id or pane_id else None
        mailbox = next((pane for pane in panes
                        if pane.get("pane_id") == environment.get("HERDR_PANE_ID")), None)
        for pane in (opener, mailbox):
            if pane and pane.get("workspace_id"):
                workspace_id = pane["workspace_id"]
                break
    requested_workspace_id = workspace_id
    if repo_id and workspace_id:
        with store.read() as connection:
            workspace_id = RepoSettings.scope_workspace(connection, repo_id, store.host, workspace_id)
    return {"repo_id": repo_id, "host": store.host, "workspace_id": workspace_id,
            "requested_workspace_id": requested_workspace_id}


class SnapshotCache:
    """Pane-local discovery only; ledger rows and resource attribution stay fresh."""
    def __init__(self, *, ttl: float = METADATA_TTL, clock=time.monotonic):
        self.ttl, self.clock = ttl, clock
        self.herdr_due = self.resources_due = 0.0
        self.herdr_value: dict[str, Any] | None = None
        self.herdr_labels: dict[str, str | None] = {}
        self.resources_value: dict[str, Any] | None = None

    def invalidate(self) -> None:
        self.herdr_due = self.resources_due = 0.0

    def herdr_snapshot(self, environment: Mapping[str, str], *,
                       agents: Sequence[Mapping[str, Any]] = ()) -> dict[str, Any] | None:
        labels = {agent["terminal_id"]: agent.get("last_ham_label") for agent in agents}
        if any(terminal in self.herdr_labels and label != self.herdr_labels[terminal]
               for terminal, label in labels.items()):
            # A HAM label mutation makes the cached pane label incomparable with
            # fresh ledger evidence; heartbeat/status-only changes do not.
            self.herdr_due = 0.0
        if self.clock() >= self.herdr_due:
            try:
                result = herdr(["api", "snapshot"], environment)
                self.herdr_value = result.get("snapshot", result)
            except LedgerError:
                # A missing server must not become a new subprocess on every bell.
                self.herdr_value = None
            self.herdr_due = self.clock() + self.ttl
            self.herdr_labels = labels
        return self.herdr_value

    def resources(self) -> dict[str, Any]:
        if self.resources_value is None or self.clock() >= self.resources_due:
            from ham.ops import resource_scan as resource_scan
            self.resources_value = resource_scan.scan_host_resources()
            self.resources_due = self.clock() + self.ttl
        return self.resources_value


def local_snapshot(store: LedgerStore, repo_id: str | None,
                   environment: Mapping[str, str] | None = None, *,
                   cache: SnapshotCache | None = None, force: bool = False) -> dict[str, Any]:
    settings = RepoSettings(store)
    if cache is not None and force:
        cache.invalidate()
    # Discover every local target, while keeping history and settings scoped to
    # the opener's repository below.
    board_options = {"resource_scanner": cache.resources} if cache is not None else {}
    board = status_board(store, repo_id=None, host="local", environment=environment, **board_options)
    agents = board["agents"]
    panes = []
    workspaces = board.get("workspaces", [])
    if environment is not None:
        try:
            if cache is None:
                result = herdr(["api", "snapshot"], environment)
                live_snapshot = result.get("snapshot", result)
            else:
                live_snapshot = cache.herdr_snapshot(environment, agents=agents)
            if live_snapshot is not None:
                panes = live_snapshot.get("panes", [])
                workspaces = live_snapshot.get("workspaces", workspaces)
                labels = {pane["terminal_id"]: pane.get("label") or "" for pane in panes}
                for agent in agents:
                    current = labels.get(agent["terminal_id"], "")
                    agent["user_border"] = bool(current and current != agent.get("last_ham_label"))
        except LedgerError:
            # The ledger remains inspectable while Herdr is temporarily absent.
            pass
    mail = Mailbox(store)
    snapshot = {"host": store.host, "agents": agents, "threads": mail.list_threads(repo_id)["threads"],
                "submissions": mail.list_direct(repo_id)["submissions"],
                "scope": settings_scope(store, repo_id, environment if environment is not None else os.environ, panes),
                "panes": panes,
                "workspaces": workspaces,
                "settings": None,
                "repo_modes": {repo: settings.show(repo)["mode"]
                               for repo in {agent["repo_id"] for agent in agents}},
                "peers": RemoteLedger(store).peers(), "outbox": RemoteLedger(store).outbox()}
    scope = snapshot["scope"]
    if repo_id:
        snapshot["settings"] = (settings.show(repo_id, scope["host"], scope["requested_workspace_id"])
                                if scope["workspace_id"] else settings.show(repo_id))
    host = next((item for item in board.get("hosts", []) if item["host"] == store.host), {})
    if "resources" in host:
        snapshot["resources"] = host["resources"]
    return snapshot


def dispatch_direct(store: LedgerStore, recipient: Mapping[str, Any], body: str,
                    environment: Mapping[str, str], *, repo_id: str, subject: str = "") -> dict[str, Any]:
    """Persist before transport; reuse the existing verified prompt transport.

    No AgentRecord is invented for the user. Terminal transport acceptance is
    deliberately independent of native mailbox delivery/handling.
    """
    prompt = user_prompt_text(body)
    mailbox = Mailbox(store, environment)
    record = mailbox.begin_direct(repo_id, recipient, body=prompt, subject=subject)
    acceptance, evidence = "failed", "Recipient no longer matches selected terminal/session."
    dispatch_started = False
    remote_sockets: dict[str, str] = {}
    try:
        local = recipient["host"] == store.host
        if local:
            candidates = query_local_agents(environment)
            host = "local"
        else:
            peer = RemoteLedger(store, environment).peer(recipient["host"])
            host = peer["ssh_alias"]
            if peer.get("remote_socket"):
                remote_sockets[host] = peer["remote_socket"]
            probe = query_remote_agents(host, config_path=ssh_config_path(environment),
                                        remote_sockets=remote_sockets)
            if not probe.success:
                raise LedgerError("host_unavailable", probe.error)
            candidates = probe.agents
        session_ref = recipient["session_ref"]
        native_session = "" if session_ref == "unreported" else session_ref
        target = next((agent for agent in candidates
                       if agent.terminal_id == recipient["terminal_id"]
                       and agent.session_id == native_session
                       and agent.agent_kind == recipient["client"]), None)
        if target is not None:
            dispatch_started = True
            results = dispatch_user_prompts([target], body, config_path=ssh_config_path(environment),
                                            environment=environment, remote_sockets=remote_sockets)
            if not results:
                raise RuntimeError("Direct dispatch returned no transport result.")
            result = results[0]
            acceptance = "accepted" if result.success else "unknown" if result.submission_unknown else "failed"
            evidence = result.error or "Herdr terminal prompt transport accepted; native context application is not asserted."
    except Exception as error:
        # If dispatch itself raised, acceptance cannot be inferred. Never replay
        # a direct submission automatically, including one interrupted by q.
        if dispatch_started:
            acceptance = "unknown"
        evidence = str(error)
    return mailbox.finish_direct(record["submission_id"], acceptance=acceptance, evidence=evidence)


def maintain(store: LedgerStore, result: Any, environment: Mapping[str, str]) -> Any:
    if environment.get("HERDR_AGENT_LEDGER_AUTOSTART") != "0":
        from ham.ledger.worker import ensure_worker
        try:
            ensure_worker(store, environment)
        except (LedgerError, OSError) as error:
            return {**result, "warnings": [*result.get("warnings", []),
                    {"code": "worker_start_failed", "message": str(error), "committed": True}]}
    return result


def execute_job(request: Mapping[str, Any], environment: Mapping[str, str]) -> Any:
    store = LedgerStore(environment=environment)
    operation = request["operation"]
    repo_id = request.get("repo_id")
    mail = Mailbox(store, environment)
    if operation == "snapshot":
        return local_snapshot(store, repo_id, environment)
    if operation == "remote":
        return status_board(store, repo_id=None, host=request["host"], environment=environment,
                            previous=request.get("previous"))
    if operation == "send":
        return mail.send_user(repo_id, request["recipient"], body=request["body"], kind=request["kind"],
                              subject=request.get("subject") or "", reply_to=request.get("reply_to"))
    if operation == "direct":
        return dispatch_direct(store, request["recipient"], request["body"], environment,
                               repo_id=repo_id, subject=request.get("subject") or "")
    if operation == "setting":
        if not repo_id:
            raise LedgerError("invalid_settings_scope", "Select a repository before saving Settings.")
        scope = settings_scope(store, repo_id, environment, live=True)
        scoped = request["field"] in {"main", "members"}
        if scoped and (not scope["workspace_id"] or request.get("scope") != scope):
            raise LedgerError("invalid_settings_scope",
                              "Settings scope changed or the opener workspace is unavailable; reload Settings.")
        settings = RepoSettings(store)
        result = settings.mutate(repo_id, request["field"], request["value"],
                                 host=scope["host"] if scope["workspace_id"] else None,
                                 workspace_id=scope["requested_workspace_id"])
        from ham.ledger.display import LedgerDisplay
        result["display"] = LedgerDisplay(store).refresh()
        return maintain(store, result, environment)
    if operation == "resolve":
        return maintain(store, mail.resolve_claim(**request["claim"]), environment)
    if operation == "withdraw":
        return maintain(store, mail.withdraw(request["message_id"]), environment)
    if operation == "retry":
        return maintain(store, RemoteLedger(store, environment).retry(request["item_id"]), environment)
    raise LedgerError("invalid_operation", "Unsupported mailbox UI operation.")


class Job:
    """A bounded process keeps SSH/SQLite waits out of the curses input loop."""
    def __init__(self, request: Mapping[str, Any], environment: Mapping[str, str], *, timeout: float = JOB_TIMEOUT):
        self.started = time.monotonic()
        self.timeout = timeout
        self.operation = str(request["operation"])
        self.closed = False
        self.output = tempfile.TemporaryFile(mode="w+", encoding="utf-8", dir=ui_state_directory(environment))
        self.errors = tempfile.TemporaryFile(mode="w+", encoding="utf-8", dir=ui_state_directory(environment))
        self.input = tempfile.TemporaryFile(mode="w+", encoding="utf-8", dir=ui_state_directory(environment))
        json.dump(request, self.input, ensure_ascii=False)
        self.input.seek(0)
        self.process = subprocess.Popen([sys.executable, "-m", "ham", "mailbox", "job"],
                                        cwd=PLUGIN_ROOT, env=dict(environment), stdin=self.input, stdout=self.output,
                                        stderr=self.errors, text=True, start_new_session=True)

    def poll(self) -> tuple[bool, Any]:
        if self.process.poll() is None:
            if time.monotonic() - self.started < self.timeout:
                return False, None
            self.close()
            return True, {"error": {"code": "ui_timeout", "message": "Operation timed out; inspect durable state before resubmitting."}}
        self.output.seek(0)
        self.errors.seek(0)
        stdout, stderr = self.output.read(), self.errors.read()
        try:
            result = json.loads(stdout)
        except ValueError:
            result = {"error": {"code": "ui_job_failed", "message": stderr.strip() or "Worker returned no result."}}
        return True, result

    def close(self) -> None:
        if getattr(self, "closed", False):
            return
        self.closed = True
        # The worker owns a whole process group, including SSH ProxyCommands.
        # Clean it even after the leader finished: a timed-out SSH can otherwise
        # leave its proxy alive after its completed snapshot has been discarded.
        try:
            os.killpg(self.process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        if self.process.poll() is None:
            try:
                self.process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                pass
        try:
            os.killpg(self.process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        self.process.wait()
        for stream in (self.input, self.output, self.errors):
            stream.close()


class SnapshotJob:
    """One polling lease; completing it does not close the reusable owner."""
    operation = "snapshot"

    def __init__(self, owner: SnapshotWorker):
        self.owner = owner
        self.started = owner.clock()
        self.closed = self.done = False
        self.result: Any = None

    def poll(self) -> tuple[bool, Any]:
        if not self.done:
            self.owner._poll(self)
        return self.done, self.result

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        if not self.done:
            self.owner._finish(self, {"error": {
                "code": "ui_job_failed", "message": "Snapshot request cancelled."}}, stop=True)


class SnapshotWorker:
    """Own one restartable process group, with one bounded snapshot in flight."""

    def __init__(self, environment: Mapping[str, str], *, timeout: float = JOB_TIMEOUT,
                 clock=time.monotonic):
        self.environment, self.timeout, self.clock = dict(environment), timeout, clock
        self.process: subprocess.Popen | None = None
        self.active: SnapshotJob | None = None
        self.buffer = bytearray()
        self.closed = False

    def request(self, request: Mapping[str, Any]) -> SnapshotJob:
        if self.closed or self.active is not None:
            raise RuntimeError("Snapshot worker is closed or already has an active request.")
        if request.get("operation") != "snapshot":
            raise ValueError("Snapshot worker accepts only read-only snapshots.")
        job = SnapshotJob(self)
        self.active = job
        self.buffer.clear()
        try:
            if self.process is not None and self.process.poll() is not None:
                self._stop()
            if self.process is None:
                self.process = subprocess.Popen(
                    [sys.executable, "-m", "ham", "mailbox", "snapshot-worker"],
                    cwd=PLUGIN_ROOT, env=self.environment, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL, bufsize=0, start_new_session=True)
                os.set_blocking(self.process.stdin.fileno(), False)
                os.set_blocking(self.process.stdout.fileno(), False)
            # Stay below PIPE_BUF: one nonblocking atomic write cannot stall keys.
            payload = (json.dumps({"operation": "snapshot", "repo_id": request.get("repo_id"),
                                   "force": bool(request.get("force"))}) + "\n").encode("ascii")
            if len(payload) > 4096:
                raise ValueError("Snapshot request is too large.")
            if os.write(self.process.stdin.fileno(), payload) != len(payload):
                raise OSError("Incomplete snapshot request write.")
        except (OSError, ValueError) as error:
            self._finish(job, {"error": {"code": "ui_job_failed", "message": str(error)}}, stop=True)
        return job

    def _finish(self, job: SnapshotJob, result: Any, *, stop: bool = False) -> None:
        job.done, job.result = True, result
        self.active = None
        if stop:
            self._stop()
        self.buffer.clear()

    def _poll(self, job: SnapshotJob) -> None:
        if self.clock() - job.started >= self.timeout:
            self._finish(job, {"error": {"code": "ui_timeout", "message":
                "Operation timed out; inspect durable state before resubmitting."}}, stop=True)
            return
        try:
            if self.process is None:
                raise ValueError("Snapshot worker exited before returning a result.")
            # Bound each poll's work even for a large ledger; never wait for IO.
            for _ in range(16):
                try:
                    chunk = os.read(self.process.stdout.fileno(), 65536)
                except BlockingIOError:
                    return
                if not chunk:
                    raise ValueError("Snapshot worker closed its output.")
                self.buffer.extend(chunk)
                if b"\n" not in chunk:
                    continue
                line, extra = self.buffer.split(b"\n", 1)
                if extra:
                    raise ValueError("Snapshot worker returned unexpected output.")
                result = json.loads(line)
                if not isinstance(result, dict) or not (
                        isinstance(result.get("error"), dict) or
                        all(isinstance(result.get(key), list) for key in ("agents", "threads", "submissions"))):
                    raise ValueError("Snapshot worker returned an invalid snapshot.")
                self._finish(job, result, stop=self.process.poll() is not None)
                return
        except (OSError, ValueError) as error:
            self._finish(job, {"error": {"code": "ui_job_failed", "message": str(error)}}, stop=True)

    def _stop(self) -> None:
        process, self.process = self.process, None
        if process is None:
            return
        # Kill the group even if the leader exited, including discovery helpers.
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        if process.poll() is None:
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                pass
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            # SIGKILL may await kernel IO; never pin the curses loop to that wait.
            # Popen retains unreaped children for subsequent nonblocking cleanup.
            pass
        for stream in (process.stdin, process.stdout):
            stream.close()

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        if self.active is not None:
            self.active.close()
        self._stop()


def snapshot_worker(environment: Mapping[str, str]) -> int:
    """Newline JSON protocol; only metadata survives between ledger reads."""
    cache = SnapshotCache()
    store: LedgerStore | None = None
    while line := sys.stdin.readline(4097):
        try:
            if len(line) > 4096 or not line.endswith("\n"):
                raise ValueError("Invalid snapshot request framing.")
            request = json.loads(line)
            if not isinstance(request, dict) or request.get("operation") != "snapshot":
                raise ValueError("Snapshot worker accepts only read-only snapshots.")
            with redirect_stdout(sys.stderr):
                if store is None:
                    store = LedgerStore(environment=environment)
                result = local_snapshot(store, request.get("repo_id"), environment,
                                        cache=cache, force=bool(request.get("force")))
        except (LedgerError, OSError, ValueError) as error:
            result = {"error": {"code": getattr(error, "code", "ui_job_failed"), "message": str(error)}}
        except Exception as error:
            result = {"error": {"code": "ui_job_failed",
                                "message": f"{type(error).__name__}: {error}"}}
        print(json.dumps(result, ensure_ascii=False), flush=True)
    return 0


def clip(text: str, width: int) -> str:
    value = "".join(character if character.isprintable() else " " for character in str(text))
    return value[:character_index_at_display_column(value, max(0, width))]


def edit_buffer(text: str, cursor: int, key: Any, *, multiline: bool) -> tuple[str, int, bool]:
    """Edit Unicode characters, not UTF-8 bytes; keep terminal width in rendering."""
    cursor = min(max(0, cursor), len(text))
    if key == curses.KEY_LEFT:
        return text, max(0, cursor - 1), True
    if key == curses.KEY_RIGHT:
        return text, min(len(text), cursor + 1), True
    start, end = text.rfind("\n", 0, cursor) + 1, text.find("\n", cursor)
    end = len(text) if end < 0 else end
    if key == curses.KEY_HOME:
        return text, start, True
    if key == curses.KEY_END:
        return text, end, True
    if key in (curses.KEY_UP, curses.KEY_DOWN) and multiline:
        column = display_width(text[start:cursor])
        if key == curses.KEY_UP and start:
            previous = text.rfind("\n", 0, start - 1) + 1
            return text, previous + character_index_at_display_column(text[previous:start - 1], column), True
        if key == curses.KEY_DOWN and end < len(text):
            following = text.find("\n", end + 1)
            following = len(text) if following < 0 else following
            return text, end + 1 + character_index_at_display_column(text[end + 1:following], column), True
        return text, cursor, True
    if key in (curses.KEY_BACKSPACE, "\x7f", "\b"):
        return (text[:cursor - 1] + text[cursor:], cursor - 1, True) if cursor else (text, cursor, True)
    if key == curses.KEY_DC:
        return text[:cursor] + text[cursor + 1:], cursor, True
    if key in ("\n", "\r"):
        return (text[:cursor] + "\n" + text[cursor:], cursor + 1, True) if multiline else (text, cursor, True)
    if isinstance(key, str) and len(key) == 1 and key.isprintable():
        return text[:cursor] + key + text[cursor:], cursor + len(key), True
    return text, cursor, False


def describe_message(message: Mapping[str, Any]) -> str:
    state = "UNCERTAIN claim" if message["state"] == "delivering" else message["state"]
    suffix = f" HOLD:{message['hold_reason']}" if message.get("hold_reason") else ""
    if message.get("withdraw_requested"):
        suffix += " withdrawal requested"
    if message.get("confirmation_source") == "operator":
        suffix += " USER-CONFIRMED (operator evidence; NOT native ACK)"
    elif message.get("confirmation_source") == "native":
        suffix += " NATIVE runtime-journal confirmation"
    return f"{message['kind']} {state}{suffix}"


def wrap_summary(value: str, width: int) -> list[str]:
    """Wrap Hangul/Latin words, Han/Kana/emoji cells and parenthesized ages."""
    width = max(1, width)
    result: list[str] = []
    for paragraph in (value.strip() or "—").split("\n"):
        line = ""
        for word in re.findall(r"\(\d+[smhd] ago\)|\S+", paragraph):
            units: list[str] = []
            run = ""
            for character in word:
                if units and not run and (unicodedata.combining(character) or
                                          character in "。、，．）)]｝}】」』：:；;！？!?…"):
                    units[-1] += character
                elif display_width(character) == 2 and not unicodedata.name(character, "").startswith("HANGUL "):
                    if run:
                        units.append(run)
                        run = ""
                    units.append(character)
                elif character == "/":
                    units.append(run + character)
                    run = ""
                else:
                    run += character
            if run:
                units.append(run)
            for index, unit in enumerate(units):
                gap = " " if index == 0 and line else ""
                if line and display_width(line + gap + unit) > width:
                    result.append(line)
                    line = ""
                    gap = ""
                if display_width(unit) > width:
                    parts = wrap_display_text(unit, width)
                    result.extend(parts[:-1])
                    line = parts[-1]
                else:
                    line += gap + unit
        result.append(line)
    return result


def status_time(value: float | None, now: float) -> str:
    if value is None:
        return "—"
    age = max(0, int(now - value))
    amount, unit = ((age, "s") if age < 60 else (age // 60, "m") if age < 3600 else
                    (age // 3600, "h") if age < 86400 else (age // 86400, "d"))
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(value)) + f" ({amount}{unit} ago)"


def yes_no(value: Any) -> str:
    return "unknown" if value is None else "yes" if value else "no"


class MailboxApp:
    """Responsive regions, ephemeral drafts, and bounded asynchronous mutations."""
    REGIONS = ("agents", "threads", "detail")
    SETTINGS = ("mode", "main", "members", *DEFAULT_THRESHOLDS)

    def __init__(self, screen: Any, environment: Mapping[str, str], *, job_factory=Job,
                 clock=time.monotonic, wall_clock=time.time, quick_send: bool = False):
        self.screen, self.environment = screen, dict(environment)
        self.repo_id = self.environment.get(REPO_ENV) or None
        self.store = LedgerStore(environment=environment)
        self.job_factory, self.clock, self.wall_clock = job_factory, clock, wall_clock
        self.snapshot_worker = SnapshotWorker(environment) if job_factory is Job else None
        self.snapshot_force = False
        self.jobs: dict[str, Any] = {}
        self.local: dict[str, Any] = {"agents": [], "threads": [], "submissions": [], "peers": [], "outbox": []}
        self.remote: dict[str, Any] = {}
        self.remote_errors: dict[str, str] = {}
        self.remote_due: dict[str, float] = {}
        self.next_bell, self.bell_stamp = 0.0, None
        self.dirty = True
        self.next_age_refresh = float("inf")
        self.detail_age_deadlines: dict[str, float] | None = None
        self.focus, self.mode, self.layout = "agents", "nav", "single"
        self.preview = "agents"
        self.selections = {"agents": 0, "threads": 0}
        self.scrolls = {"agents": 0, "threads": 0, "detail": 0}
        self.detail: dict[str, Any] | None = None
        self.detail_identity: Any = None
        self.detail_technical = False
        self.detail_available, self.detail_total = 0, 0
        self.detail_anchor_pending = False
        self.detail_latest: tuple[Any, int] | None = None
        self.viewer: tuple[str, Any] | None = None
        self.diagnostics = False
        self.status, self.input_error = "", ""
        self.status_level, self.last_error = "info", ""
        self.drafts: dict[tuple[str, str, str | None], dict[str, Any]] = {}
        self.draft_key: tuple[str, str, str | None] | None = None
        self.body, self.body_cursor, self.draft_version = "", 0, 0
        self.delivery, self.reply_to = "mailbox", None
        self.mail_kind = "action"
        self.recipient: dict[str, Any] | None = None
        self.submitted: dict[str, Any] | None = None
        self.result_tags: dict[tuple[str, str], tuple[str, float]] = {}
        self.confirm: dict[str, Any] | None = None
        self.edit: dict[str, Any] | None = None
        self.settings_selection = 0
        self.settings_pending: dict[str, Any] = {}
        self.settings_picker: dict[str, Any] | None = None
        self.settings_numeric: dict[str, Any] | None = None
        self.settings_results: dict[str, str] = {}
        self.settings_context = settings_scope(self.store, self.repo_id, self.environment)
        self.settings_saving: dict[str, Any] | None = None
        self.help_return = "nav"
        self.overlay_scroll = 0
        self.cursor_position: tuple[int, int] | None = None
        self.hit_map: list[tuple[tuple[int, int, int, int], str, Any]] = []
        self.input_scroll: int | None = None
        self.theme = Theme({"NO_COLOR": ""})
        self.quick_send, self.running = quick_send, True

    def set_status(self, message: str, level: str = "info") -> None:
        self.status, self.status_level = message, level
        if level == "error":
            self.last_error = message

    def input_active(self) -> bool:
        return self.draft_key is not None and (
            self.mode == "input" or self.mode == "help" and self.help_return == "input" or
            self.mode == "confirm" and self.confirm is not None and
            self.confirm["return_mode"] == "input")

    def start_job(self, key: str, operation: str, **values: Any) -> bool:
        if not self.running:
            return False
        if key in self.jobs:
            if key == "mutation":
                self.set_status("An operation is pending; this input has not been submitted.", "error")
            return False
        request = {"operation": operation, "repo_id": self.repo_id, **values}
        if operation in {"snapshot", "setting"}:
            request.setdefault("scope", self.current_settings_scope())
        if operation == "snapshot":
            if self.snapshot_force:
                request["force"] = True
            self.jobs[key] = (self.snapshot_worker.request(request) if self.snapshot_worker is not None
                              else self.job_factory(request, self.environment))
            self.snapshot_force = False
        else:
            self.jobs[key] = self.job_factory(request, self.environment)
        self.dirty = True
        return True

    @staticmethod
    def identity(item: Mapping[str, Any] | None) -> Any:
        if not item:
            return None
        if item.get("type"):
            field = {"thread": "thread_id", "direct": "submission_id", "outbox": "item_id"}[item["type"]]
            return item["type"], item[field]
        return item["host"], item["terminal_id"]

    def agents(self) -> list[dict[str, Any]]:
        values = list(self.local.get("agents", []))
        peers = {peer["canonical_name"]: peer["uuid"] for peer in self.local.get("peers", [])}
        for host, snapshot in self.remote.items():
            if not any(item["host"] == host and item["uuid"] == peers.get(host)
                       for item in snapshot.get("hosts", [])):
                continue
            for agent in snapshot.get("agents", []):
                stale = host in self.remote_errors or agent.get("stale", False)
                values.append({**agent, "stale": stale, "unread": "?" if stale else agent.get("unread", "?")})
        local_host = self.local.get("host")
        scope = self.local.get("scope", self.settings_context)
        workspace = scope.get("workspace_id")
        return sorted(values, key=lambda agent: (
            self.repo_id is not None and agent["repo_id"] != self.repo_id,
            agent["host"] != local_host, agent["host"],
            (Path(agent["repo_id"]).name, agent["repo_id"])
            if self.repo_id is not None and agent["repo_id"] != self.repo_id else ("", ""),
            not (agent["host"] == local_host and workspace and self.role_workspace(agent) == workspace),
            not bool(self.role_workspace(agent)), self.role_workspace(agent) or "",
            agent.get("workspace_id") != self.role_workspace(agent),
            agent.get("workspace_id") or "",
            agent.get("alias") or "", agent["terminal_id"]))

    def agents_workspace(self) -> str | None:
        # settings_scope() resolves this alongside the local snapshot. Keep its
        # order stable until tick() can restore selection by identity on refresh;
        # reading the live ledger here could reorder the OLD snapshot first.
        # current_settings_scope() would also discard pending Settings edits.
        scope = self.local.get("scope", self.settings_context)
        return scope.get("requested_workspace_id", scope.get("workspace_id"))

    @staticmethod
    def role_workspace(agent: Mapping[str, Any]) -> str | None:
        return agent.get("role_workspace_id") or agent.get("workspace_id") or None

    def workspace_label(self, host: str, workspace: str | None,
                        agents: Sequence[Mapping[str, Any]]) -> str:
        if not workspace:
            return "No workspace"
        snapshot = self.local if host == self.local.get("host") else self.remote.get(host, {})
        for item in snapshot.get("workspaces", []):
            if (item.get("host", host) == host and item.get("workspace_id") == workspace
                    and item.get("label")):
                return str(item["label"])
        # Pane labels name panes, not workspaces. Only explicit workspace labels
        # are suitable here; older peer boards may carry workspace IDs only.
        for item in [*snapshot.get("panes", []), *agents]:
            if item.get("workspace_id") == workspace and item.get("workspace_label"):
                return str(item["workspace_label"])
        return workspace

    def receiver_notice(self) -> str:
        """Only current local, participating OMP/Claude sessions need a receiver."""
        missing: dict[str, int] = {}
        modes = self.local.get("repo_modes", {})
        for agent in self.local.get("agents", []):
            repo = agent.get("repo_id")
            mode = modes.get(repo, (self.local.get("settings") or {}).get("mode", "off"))
            if (agent.get("host") == self.local.get("host") and
                    (self.repo_id is None or repo == self.repo_id) and
                    agent.get("active", True) and not agent.get("stale") and
                    mode in {"auto", "manual"} and agent.get("client") in {"omp", "claude"} and
                    agent.get("extension_present") in (False, 0)):
                client = agent["client"]
                missing[client] = missing.get(client, 0) + 1
        if not missing:
            return ""
        clients = ", ".join(sorted(missing))
        return f"⚠ {sum(missing.values())} sessions without HAM receiver ({clients}) — runtime status"

    def host_resources(self, host: str) -> Mapping[str, Any] | None:
        if host == self.local.get("host"):
            return self.local.get("resources")
        peer = next((item for item in self.local.get("peers", [])
                     if item["canonical_name"] == host), None)
        if peer:
            return next((item.get("resources") for item in self.remote.get(host, {}).get("hosts", [])
                         if item["host"] == host and item["uuid"] == peer["uuid"]), None)
        return None

    @staticmethod
    def resource_counts(agent: Mapping[str, Any]) -> dict[str, str]:
        devices, servers = agent.get("devices"), agent.get("servers")
        counts = {name: "?" if devices is None else str(sum(row.get("kind") == kind for row in devices))
                  for name, kind in (("phone", "phone"), ("emu", "emu"))}
        counts["server"] = "?" if servers is None else str(len(servers))
        return {name: "·" if count == "0" else count for name, count in counts.items()}

    def host_resource_cue(self, host: str) -> str:
        resources = self.host_resources(host) or {}
        findings = [label + " " + ("?" if resources.get(key) is None else str(len(resources[key])))
                    for label, key in (("dead", "dead_serve"), ("unowned", "servers_unowned"),
                                       ("stale-dev", "stale_devices"))
                    if resources.get(key) is None or resources[key]]
        if resources.get("error"):
            findings.append("!")
        return " · ".join(findings)

    @staticmethod
    def thread_handled(item: Mapping[str, Any]) -> bool:
        """Only explicit native handling completes every retained message.

        Delivering is the unresolved-claim truth used by resolve/_claim_rows;
        claim IDs and operator/native evidence survive successful handling.
        Withdrawn is terminal cancellation, not handled. Direct/outbox transport
        records cannot establish human handling either.
        """
        messages = item.get("messages", [])
        return (item.get("type") == "thread" and bool(messages) and
                all(message.get("state") == "handled" for message in messages))

    def items(self, region: str | None = None) -> list[Any]:
        region = region or (self.preview if self.focus == "detail" else self.focus)
        if region == "agents":
            return self.agents()
        values = ([{"type": "thread", **item} for item in self.local.get("threads", [])] +
                  [{"type": "direct", **item} for item in self.local.get("submissions", [])] +
                  [{"type": "outbox", **item} for item in self.local.get("outbox", [])
                   if item["state"] in {"pending", "failed"}])
        # Stable partition: retain the ledger's updated/recent order in each group.
        active, handled = [], []
        for item in values:
            (handled if self.thread_handled(item) else active).append(item)
        return active + handled

    def selected(self, region: str | None = None) -> Any:
        region = region or (self.preview if self.focus == "detail" else self.focus)
        items = self.items(region)
        index = min(max(0, self.selections[region]), max(0, len(items) - 1))
        self.selections[region] = index
        return items[index] if items else None

    def refresh_detail(self, identities: Mapping[str, Any] | None = None) -> None:
        if self.viewer is not None:
            region, identity = self.viewer
            if not any(self.identity(item) == identity for item in self.items(region)):
                self.close_viewer()
                self.set_status("The viewed item is no longer available.")
        if identities:
            for region, identity in identities.items():
                self.selections[region] = next((index for index, item in enumerate(self.items(region))
                                               if self.identity(item) == identity), self.selections[region])
                self.selected(region)
        if self.viewer is not None:
            region, identity = self.viewer
            self.selections[region] = next(index for index, item in enumerate(self.items(region))
                                           if self.identity(item) == identity)
        if not self.diagnostics:
            identity = self.identity(self.selected(self.preview))
            if identity != self.detail_identity:
                self.detail_identity = identity
                self.detail_technical = False
                self.scrolls["detail"] = 0
                self.detail_total = 0
                self.detail_anchor_pending = True
                self.detail_latest = None
            self.detail = self.selected(self.preview)

    def tick(self) -> None:
        if not self.running:
            return
        now = self.clock()
        for key, job in list(self.jobs.items()):
            if self.jobs.get(key) is not job:
                continue
            done, value = job.poll()
            if not done:
                continue
            job.close()
            del self.jobs[key]
            self.dirty = True
            identities = {region: self.identity(self.selected(region)) for region in ("agents", "threads")}
            error = value.get("error") if isinstance(value, dict) else None
            if key.startswith("remote:"):
                host = key.split(":", 1)[1]
                if error:
                    self.remote_errors[host] = str(error.get("message", error))
                else:
                    self.remote[host] = value
                    state = next((item for item in value.get("hosts", []) if item["host"] == host), {})
                    if state.get("error"):
                        self.remote_errors[host] = str(state["error"].get("message", state["error"]))
                    else:
                        self.remote_errors.pop(host, None)
            elif key == "snapshot":
                if error:
                    self.set_status(f"Local ledger: {error.get('code')}: {error.get('message')}", "error")
                else:
                    self.local = value
                    if not self.matches_settings_scope(self.local.get("settings")):
                        self.local["settings"] = None
                    peers = {peer["canonical_name"] for peer in value.get("peers", [])}
                    for cache in (self.remote, self.remote_errors, self.remote_due):
                        for host in list(cache):
                            if host not in peers:
                                del cache[host]
                    for pending in list(self.jobs):
                        if pending.startswith("remote:") and pending.split(":", 1)[1] not in peers:
                            self.jobs.pop(pending).close()
            else:
                saving = self.settings_saving if job.operation == "setting" else None
                if saving:
                    if saving["scope"] == self.current_settings_scope():
                        self.settings_results[saving["field"]] = (
                            f"✕ {error.get('code')}: {error.get('message')}" if error else
                            "Saved." if self.matches_settings_scope(value) else "Settings scope changed; reloading…")
                    self.settings_saving = None
                if job.operation in {"send", "direct"}:
                    self.finish_send(value, error)
                elif error:
                    self.set_status(f"{error.get('code')}: {error.get('message')}", "error")
                else:
                    self.set_status("Settings saved." if job.operation == "setting" else "Operation completed.")
                    if job.operation == "setting":
                        snapshot = self.jobs.pop("snapshot", None)
                        if snapshot is not None:
                            snapshot.close()
                        if self.matches_settings_scope(value):
                            self.local["settings"] = value
                if self.running:
                    self.start_job("snapshot", "snapshot")
            self.refresh_detail(identities)
            if not self.running:
                return
        for identity, (_tag, until) in list(self.result_tags.items()):
            if now >= until:
                del self.result_tags[identity]
                self.dirty = True
        if self.snapshot_force and "snapshot" not in self.jobs:
            self.start_job("snapshot", "snapshot")
        if now >= self.next_bell:
            self.next_bell = now + LOCAL_INTERVAL
            try:
                stamp = (self.store.bell_directory / "_board").stat().st_mtime_ns
            except FileNotFoundError:
                stamp = 0
            if stamp != self.bell_stamp and "snapshot" not in self.jobs:
                self.bell_stamp = stamp
                self.start_job("snapshot", "snapshot")
        for peer in self.local.get("peers", []):
            host = peer["canonical_name"]
            if now >= self.remote_due.get(host, 0):
                self.remote_due[host] = now + REMOTE_INTERVAL
                self.start_job(f"remote:{host}", "remote", host=host, previous=self.remote.get(host))

    def save_draft(self) -> None:
        if self.draft_key is not None:
            self.drafts[self.draft_key] = {"text": self.body, "cursor": self.body_cursor,
                                          "version": self.draft_version, "delivery": self.delivery}

    def open_input(self, recipient: Mapping[str, Any], reply_to: str | None = None) -> None:
        if recipient.get("stale"):
            self.set_status("Remote snapshot is stale; refresh before selecting a recipient.", "error")
            return
        self.recipient = dict(recipient)
        self.reply_to = reply_to
        self.draft_key = (recipient["host"], recipient["terminal_id"], reply_to)
        draft = self.drafts.get(self.draft_key, {})
        self.body, self.body_cursor = draft.get("text", ""), draft.get("cursor", 0)
        self.draft_version, self.delivery = draft.get("version", 0), draft.get("delivery", "mailbox")
        self.mail_kind = "action"
        self.mode, self.input_error = "input", ""
        self.input_scroll = None
        self.save_draft()

    def choose_agent(self) -> None:
        agent = self.selected("agents")
        if agent:
            self.open_input(agent)
        else:
            self.set_status("No agent selected. Refresh to discover recipients.", "error")

    def reply(self) -> None:
        item = self.selected("threads")
        if not item or item.get("type") != "thread" or not item.get("messages"):
            self.set_status("Select a mailbox thread to reply.", "error")
            return
        latest = max(item["messages"], key=lambda message: (message.get("created_at", 0), message["message_id"]))
        agents = self.agents()
        # Agent replies identify their sender by immutable agent ID; user-origin
        # messages identify the other participant by the concrete recipient slot.
        participant = next((agent for agent in agents
                            if latest.get("sender") not in (None, "user") and
                            agent.get("agent_id") == latest["sender"]), None)
        if participant is not None:
            identity = self.identity(participant)
        else:
            user_messages = [message for message in item["messages"] if message.get("sender") in (None, "user")]
            other = max(user_messages, key=lambda message: (message.get("created_at", 0), message["message_id"])) if user_messages else None
            identity = ((other.get("recipient_host"), other.get("recipient_terminal_id")) if other else None)
        recipient = next((agent for agent in agents if self.identity(agent) == identity and not agent.get("stale")), None)
        if recipient is None:
            self.set_status("Thread participant is unavailable or stale; refresh before replying.", "error")
            return
        self.open_input(recipient, latest["message_id"])

    def submit(self) -> None:
        if not self.body.strip():
            self.input_error = "Enter a non-empty message body."
            return
        if not self.recipient:
            self.input_error = "No recipient selected."
            return
        recipient = next((agent for agent in self.agents()
                          if self.identity(agent) == self.identity(self.recipient)), None)
        if recipient is None or recipient.get("stale"):
            self.input_error = "Recipient is unavailable or stale; refresh before sending."
            return
        request = {"repo_id": self.repo_id or recipient["repo_id"],
                   "recipient": dict(self.recipient), "body": self.body, "kind": self.mail_kind,
                   "subject": None, "reply_to": self.reply_to}
        if self.delivery == "direct":
            self.open_confirm("DIRECT · may interrupt; acceptance ≠ native delivery",
                              [f"To {self.address(recipient)}", self.body],
                              "direct", request, return_mode="input")
        else:
            self.send_request("send", request)

    def send_request(self, operation: str, request: Mapping[str, Any]) -> None:
        self.save_draft()
        if self.start_job("mutation", operation, **request):
            self.submitted = {"key": self.draft_key, "version": self.draft_version,
                              "identity": self.identity(request["recipient"]), "operation": operation,
                              "recipient": self.address(request["recipient"]), "kind": self.mail_kind}
            self.input_error = ""

    def finish_send(self, value: Mapping[str, Any], error: Any) -> None:
        submitted, self.submitted = self.submitted, None
        if submitted is None:
            return
        failed = bool(error) or value.get("acceptance") == "failed"
        if failed:
            message = str(error.get("message", error) if error else
                          value.get("evidence") or value.get("error") or "Delivery failed.")
            tag = "failed: " + message
            self.set_status(message, "error")
            if self.draft_key == submitted["key"] and self.input_active():
                self.input_error, self.mode, self.confirm = message, "input", None
        else:
            draft = self.drafts.get(submitted["key"])
            cleared = draft is not None and draft["version"] == submitted["version"]
            if cleared:
                del self.drafts[submitted["key"]]
                if self.draft_key == submitted["key"]:
                    self.body, self.body_cursor = "", 0
                    if self.input_active():
                        self.mode, self.confirm = "nav", None
                    self.draft_key = None
            tag = ("direct accepted" if value.get("acceptance") == "accepted" else "direct uncertain"
                   if submitted["operation"] == "direct" else value.get("state") or "queued")
            self.set_status("Direct acceptance ≠ native delivery." if submitted["operation"] == "direct"
                            else "Message queued in the mailbox.")
            if self.quick_send and cleared and not any(draft["text"].strip() for draft in self.drafts.values()):
                if submitted["operation"] == "send":
                    self.notify_queued(submitted)
                self.running = False
        self.result_tags[submitted["identity"]] = (tag, self.clock() + 10)

    def notify_queued(self, submitted: Mapping[str, Any]) -> None:
        """Let the existing notification helper finish outside the closing UI."""
        body = f"Queued · {submitted.get('recipient', 'recipient')} · {submitted.get('kind', 'action').upper()}"
        try:
            subprocess.Popen(
                [sys.executable, "-c",
                 "import sys; from ham.ui.messenger import show_notification; show_notification(sys.argv[1])",
                 body],
                env=self.environment, cwd=str(PLUGIN_ROOT), stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
            )
        except OSError:
            pass

    @staticmethod
    def address(agent: Mapping[str, Any] | None) -> str:
        return f"{agent['host']}/{agent.get('alias') or agent['terminal_id']}" if agent else "—"

    def open_confirm(self, title: str, lines: list[str], operation: str, values: Mapping[str, Any],
                     *, return_mode: str = "nav") -> None:
        self.confirm = {"title": title, "lines": lines, "operation": operation,
                        "values": dict(values), "choice": 0, "return_mode": return_mode}
        self.overlay_scroll = 0
        self.mode = "confirm"

    def confirm_key(self, key: Any) -> None:
        confirm = self.confirm
        assert confirm is not None
        if key == key_input.ESCAPE:
            self.mode, self.confirm = confirm["return_mode"], None
        elif key in ("\t", key_input.SHIFT_TAB, curses.KEY_LEFT, curses.KEY_RIGHT):
            confirm["choice"] = 1 - confirm["choice"]
        elif key in (curses.KEY_UP, curses.KEY_DOWN, curses.KEY_PPAGE, curses.KEY_NPAGE):
            self.overlay_key(key)
        elif key == "\r":
            self.mode, self.confirm = confirm["return_mode"], None
            if not confirm["choice"]:
                return
            operation = confirm["operation"]
            if operation == "quit":
                self.running = False
            elif operation in {"send", "direct"}:
                self.send_request(operation, confirm["values"])
            elif operation == "setting":
                self.save_setting(**confirm["values"])
            else:
                self.start_job("mutation", operation, **confirm["values"])

    def resolve(self, disposition: str) -> None:
        item = self.selected("threads")
        message = next((message for message in (item or {}).get("messages", [])
                        if message["state"] == "delivering"), None)
        if message is None:
            self.set_status("Select a thread containing an uncertain native claim.", "error")
            return
        self.edit = {"title": "Operator evidence (required; NOT native ACK)", "text": "", "cursor": 0,
                     "action": "resolve", "return_mode": "nav",
                     "claim": {"claim_id": message["claim_id"], "agent_id": message["claimed_agent_id"],
                               "generation": message["claimed_generation"], "disposition": disposition},
                     "target": self.message_target(message)}
        self.overlay_scroll = 0
        self.mode = "evidence"

    @staticmethod
    def message_target(message: Mapping[str, Any]) -> list[str]:
        return [f"Message: {message.get('message_id', '—')}",
                f"Claim: {message.get('claim_id') or '—'}",
                f"Old agent: {message.get('claimed_agent_id') or '—'}",
                f"Generation: {message.get('claimed_generation', '—')}",
                f"Current state: {message.get('state', '—')}"]

    def thread_action(self, key: str) -> None:
        item = self.selected("threads")
        if key in ("a", "n"):
            self.resolve("applied" if key == "a" else "not_applied")
        elif key == "w":
            message = next((message for message in (item or {}).get("messages", [])
                            if message.get("origin_host") == self.store.host and
                            message["state"] in {"pending", "queued"}), None)
            if message:
                self.open_confirm("Withdraw message?", self.message_target(message),
                                  "withdraw", {"message_id": message["message_id"]})
            else:
                self.set_status("No locally originated pending or queued message is selected.", "error")
        elif key == "t":
            if item and item.get("type") == "outbox":
                self.open_confirm("Retry outbox item?",
                                  [f"Item: {item['item_id']}", *self.message_target(item)],
                                  "retry", {"item_id": item["item_id"]})
            else:
                self.set_status("Select a pending or failed outbox item to retry.", "error")

    def current_settings_scope(self) -> dict[str, Any]:
        scope = settings_scope(self.store, self.repo_id, self.environment, self.local.get("panes", ()))
        if scope != self.settings_context:
            self.settings_context = scope
            self.settings_pending.clear()
            self.settings_picker = None
            self.settings_numeric = None
            self.settings_results.clear()
        return scope

    def matches_settings_scope(self, settings: Mapping[str, Any] | None) -> bool:
        scope = self.current_settings_scope()
        if not scope["workspace_id"]:
            return (bool(settings) and settings.get("repo_id") == scope["repo_id"]
                    and settings.get("workspace_id") is None
                    and settings.get("host") in {None, scope["host"]})
        return bool(settings) and all(settings.get(key) == value for key, value in scope.items())

    def settings_agent_in_scope(self, agent: Mapping[str, Any], *, live: bool = True) -> bool:
        scope = self.current_settings_scope()
        if (not scope["workspace_id"] or
                any(agent.get(key) != scope[key] for key in ("repo_id", "host")) or
                not agent.get("workspace_id") or
                (live and (agent.get("stale") or not agent.get("active", True)))):
            return False
        with self.store.read() as connection:
            return RepoSettings.scope_workspace(
                connection, scope["repo_id"], scope["host"], agent["workspace_id"]) == scope["workspace_id"]

    def configured_members_in_scope(self) -> list[dict[str, Any]]:
        scope = self.current_settings_scope()
        known = {self.identity(agent): agent for agent in self.agents()}
        members = []
        for target in self.setting_value("members") or []:
            agent = known.get((target["host"], target["terminal_id"]))
            if (target["host"] == scope["host"] and
                    (agent is None or self.settings_agent_in_scope(agent, live=False))):
                members.append(target)
        return members

    def setting_label(self, field: str) -> str:
        scope = self.current_settings_scope()
        if field in {"main", "members"}:
            return (f"{field.title()} (workspace {scope['workspace_id'] or 'unavailable'}"
                    f" @ {scope['host']}) · role group")
        return f"{field} (repo-wide)" if field == "mode" else field

    def setting_value(self, field: str) -> Any:
        self.current_settings_scope()
        settings = self.local.get("settings") or {}
        if not self.matches_settings_scope(settings):
            settings = {}
        fields = settings.get("settings", {})
        if field == "mode":
            return settings.get("mode", "off")
        if field in DEFAULT_THRESHOLDS:
            return fields.get("thresholds", {}).get("value", {}).get(field, DEFAULT_THRESHOLDS[field])
        return fields.get(field, {}).get("value")

    def settings_ready(self, field: str) -> bool:
        if not self.repo_id:
            message = "Select an agent to choose a repository first."
        elif field in {"main", "members"} and not self.current_settings_scope()["workspace_id"]:
            message = "The opener workspace is unavailable. Reopen Mailbox from a live workspace pane."
        elif not self.matches_settings_scope(self.local.get("settings")):
            message = "Loading settings…"
            self.start_job("snapshot", "snapshot")
        else:
            return True
        self.settings_results[field] = message
        self.set_status(message, "error" if not self.repo_id else "info")
        return False

    def save_setting(self, field: str, value: Any, scope: Mapping[str, Any] | None = None) -> bool:
        if scope is not None and dict(scope) != self.current_settings_scope():
            self.set_status("Settings scope changed; reopen the picker or confirmation.", "error")
            return False
        if not self.settings_ready(field):
            return False
        if "mutation" in self.jobs:
            message = "An operation is pending; this setting has not been submitted."
            self.settings_results[field] = message
            self.set_status(message, "error")
            return False
        save_field, save_value = field, value
        if field in DEFAULT_THRESHOLDS:
            save_field = "thresholds"
            save_value = {name: self.setting_value(name) for name in DEFAULT_THRESHOLDS}
            save_value[field] = value
        if not self.start_job("mutation", "setting", field=save_field, value=save_value):
            return False
        self.settings_saving = {"field": field, "scope": dict(self.current_settings_scope())}
        self.settings_results[field] = "Saving settings…"
        self.settings_pending.pop(field, None)
        return True

    def open_settings_picker(self, field: str) -> None:
        current = self.configured_members_in_scope() if field == "members" else self.setting_value(field)
        if field == "mode":
            entries = [
                ("off · No main or worker roles.", "off"),
                ("manual · Explicit main and checked members only.", "manual"),
                ("auto · Infer the group's sole agent or unique main-checkout agent; an empty linked anchor has no auto main.", "auto"),
            ]
        else:
            scope = self.current_settings_scope()
            with self.store.read() as connection:
                active = {(agent["host"], agent["terminal_id"]) for agent in RepoSettings.scope_agents(
                    connection, scope["repo_id"], scope["host"], scope["workspace_id"])} if scope["workspace_id"] else set()
            agents = [agent for agent in self.agents()
                      if self.identity(agent) in active and self.settings_agent_in_scope(agent)]
            entries = [("(none)", None)] if field == "main" else []
            for agent in agents:
                if not agent.get("stale"):
                    entries.append((f"{self.address(agent)} · {agent.get('turn_state', 'unknown')}",
                                    {name: agent[name] for name in ("host", "terminal_id")}))
            if field == "members":
                live = [value for _label, value in entries]
                known = self.agents()
                for target in current or []:
                    agent = next((agent for agent in known
                                  if self.identity(agent) == (target["host"], target["terminal_id"])), None)
                    if target not in live:
                        entries.append((f"{self.address(agent or target)} · stale", target))
        selection = next((index for index, (_label, value) in enumerate(entries)
                          if value == current), 0)
        self.settings_picker = {"field": field, "entries": entries, "selection": selection,
                                "scope": dict(self.current_settings_scope()),
                                "checked": [value for _label, value in entries if value in (current or [])]
                                if field == "members" else []}
        self.overlay_scroll = 0

    def settings_picker_key(self, key: Any) -> None:
        self.current_settings_scope()
        if self.settings_picker is None:
            return
        picker = self.settings_picker
        assert picker is not None
        entries = picker["entries"]
        if key == key_input.ESCAPE:
            self.settings_picker = None
            self.overlay_scroll = 0
        elif key in (curses.KEY_UP, "k", curses.KEY_DOWN, "j",
                     curses.KEY_PPAGE, curses.KEY_NPAGE, curses.KEY_HOME, curses.KEY_END):
            selection = picker["selection"]
            if key == curses.KEY_HOME:
                selection = 0
            elif key == curses.KEY_END:
                selection = len(entries) - 1
            else:
                amount = max(1, self.screen.getmaxyx()[0] - 8) if key in (
                    curses.KEY_PPAGE, curses.KEY_NPAGE) else 1
                selection += (-amount if key in (curses.KEY_UP, "k", curses.KEY_PPAGE) else amount)
            picker["selection"] = min(max(0, len(entries) - 1), max(0, selection))
            self.overlay_scroll = 0
        elif key == " " and picker["field"] == "members" and entries:
            target = entries[picker["selection"]][1]
            checked = picker["checked"]
            checked.remove(target) if target in checked else checked.append(target)
        elif key == "\r":
            value = picker["checked"] if picker["field"] == "members" else entries[picker["selection"]][1]
            if self.save_setting(picker["field"], value, picker["scope"]):
                self.settings_picker = None
                self.overlay_scroll = 0

    def step_setting(self, direction: int) -> None:
        field = self.SETTINGS[self.settings_selection]
        if not self.settings_ready(field):
            return
        value = self.settings_pending.get(field, self.setting_value(field))
        if field == "mode":
            modes = ("off", "manual", "auto")
            value = modes[(modes.index(value) + direction) % len(modes)]
        elif field in DEFAULT_THRESHOLDS:
            # ledger_settings.validate_setting accepts 0..10000 for every threshold.
            value = min(10000, max(0, value + direction * (5 if field == "coalesce_seconds" else 1)))
        else:
            return
        if field == "mode" and value == self.setting_value(field):
            self.settings_pending.pop(field, None)
        else:
            self.settings_pending[field] = value
        if not self.settings_saving or self.settings_saving["field"] != field:
            self.settings_results.pop(field, None)

    def settings_key(self, key: Any) -> None:
        self.current_settings_scope()
        if self.settings_picker is not None:
            self.settings_picker_key(key)
            return
        numeric = self.settings_numeric
        if numeric is not None:
            field = numeric["field"]
            if key == key_input.ESCAPE:
                self.settings_numeric = None
            elif key == "\r":
                text = numeric["text"]
                if not text or len(text) > 5 or not 0 <= int(text) <= 10000:
                    self.settings_results[field] = "Enter a number from 0 to 10000."
                elif self.save_setting(field, int(text), numeric["scope"]):
                    self.settings_numeric = None
            elif isinstance(key, str) and len(key) == 1 and "0" <= key <= "9":
                numeric["text"], numeric["cursor"], _ = edit_buffer(
                    numeric["text"], numeric["cursor"], key, multiline=False)
            elif key in (curses.KEY_LEFT, curses.KEY_RIGHT, curses.KEY_HOME, curses.KEY_END,
                         curses.KEY_BACKSPACE, curses.KEY_DC, "\x7f", "\b"):
                numeric["text"], numeric["cursor"], _ = edit_buffer(
                    numeric["text"], numeric["cursor"], key, multiline=False)
            return
        if key == key_input.ESCAPE:
            self.settings_pending.clear()
            self.mode = "nav"
            return
        if key in (curses.KEY_PPAGE, curses.KEY_NPAGE, curses.KEY_HOME, curses.KEY_END):
            self.overlay_key(key)
            return
        if key in (curses.KEY_UP, "k", curses.KEY_DOWN, "j"):
            self.settings_selection = min(len(self.SETTINGS) - 1, max(0, self.settings_selection +
                                          (-1 if key in (curses.KEY_UP, "k") else 1)))
            self.overlay_scroll = 0
            return
        field = self.SETTINGS[self.settings_selection]
        if key in (curses.KEY_LEFT, "h", "-", curses.KEY_RIGHT, "l", "+"):
            self.step_setting(-1 if key in (curses.KEY_LEFT, "h", "-") else 1)
            return
        if key not in ("m", " ", "\r") or not self.settings_ready(field):
            return
        if key in ("m", " "):
            agent = self.selected("agents")
            if not agent or not self.settings_agent_in_scope(agent):
                self.set_status("Choose a live agent in the opener's workspace role group on this host first.", "error")
                return
            target = {name: agent[name] for name in ("host", "terminal_id")}
            field, value = "main", target
            if key == " ":
                field, value = "members", self.configured_members_in_scope()
                value.remove(target) if target in value else value.append(target)
            if "mutation" in self.jobs:
                self.settings_results[field] = "An operation is pending; this setting has not been submitted."
                self.set_status(self.settings_results[field], "error")
                return
            self.open_confirm(f"Change {self.setting_label(field)}?", [f"Target: {self.address(agent)}",
                              json.dumps(value, ensure_ascii=False)], "setting",
                              {"field": field, "value": value, "scope": dict(self.current_settings_scope())},
                              return_mode="settings")
        elif field in self.settings_pending:
            self.save_setting(field, self.settings_pending[field])
        elif field in {"mode", "main", "members"}:
            self.open_settings_picker(field)
        elif not self.settings_pending:
            text = str(self.setting_value(field))
            self.settings_numeric = {"field": field, "text": text, "cursor": len(text),
                                     "scope": dict(self.current_settings_scope())}

    def settings_mouse(self, event: key_input.MouseEvent, hits: list[Any]) -> None:
        if event.action in {"wheel_up", "wheel_down"}:
            if any(target == "overlay" for _, target, _ in hits):
                direction = -1 if event.action == "wheel_up" else 1
                if self.settings_picker is not None:
                    for _ in range(3):
                        self.settings_picker_key(curses.KEY_UP if direction < 0 else curses.KEY_DOWN)
                else:
                    self.overlay_scroll = max(0, self.overlay_scroll + direction * 3)
            return
        if self.settings_numeric is not None:
            return
        for _, target, value in hits:
            if target == "picker-entry" and self.settings_picker is not None:
                self.settings_picker["selection"] = value
                self.settings_picker_key(" " if self.settings_picker["field"] == "members" else "\r")
                return
            if target == "setting-step" and self.settings_picker is None:
                self.settings_selection, direction = value
                self.step_setting(direction)
                return
            if target == "setting" and self.settings_picker is None:
                activate = self.settings_selection == value
                self.settings_selection = value
                if activate:
                    self.settings_key("\r")
                return

    def edit_key(self, key: Any) -> None:
        edit = self.edit
        assert edit is not None
        if key == key_input.ESCAPE:
            self.mode, self.edit = edit["return_mode"], None
            return
        if key in (curses.KEY_UP, curses.KEY_DOWN, curses.KEY_PPAGE, curses.KEY_NPAGE):
            self.overlay_key(key)
            return
        if key == "\r":
            try:
                if not edit["text"].strip():
                    raise ValueError("Non-empty operator evidence is required.")
                claim = {**edit["claim"], "evidence": edit["text"].strip()}
                self.open_confirm("USER CONFIRM " + claim["disposition"],
                                  [*edit["target"], "Evidence: " + claim["evidence"],
                                   "Operator confirmation, NOT native ACK."],
                                  "resolve", {"claim": claim})
                self.edit = None
            except (ValueError, TypeError) as error:
                self.set_status(str(error), "error")
            return
        edit["text"], edit["cursor"], _handled = edit_buffer(edit["text"], edit["cursor"], key, multiline=False)

    def refresh(self) -> None:
        self.snapshot_force = True
        self.start_job("snapshot", "snapshot")
        self.remote_due.clear()

    def request_quit(self) -> None:
        count = sum(bool(draft["text"]) for draft in self.drafts.values())
        if count:
            noun = "draft" if count == 1 else "drafts"
            self.open_confirm(f"Discard {count} unsent {noun}?", [], "quit", {})
        else:
            self.running = False

    def input_key(self, key: Any) -> None:
        if key == key_input.ESCAPE:
            self.save_draft()
            self.mode = "nav"
            self.draft_key = None
        elif key == key_input.F1:
            self.help_return, self.mode = "input", "help"
            self.overlay_scroll = 0
        elif key == "\x12":
            self.refresh()
        elif key == "\x04":
            self.delivery = "direct" if self.delivery == "mailbox" else "mailbox"
            self.draft_version += 1
            self.save_draft()
        elif key == "\x07":
            self.mail_kind = "fyi" if self.mail_kind == "action" else "action"
            self.draft_version += 1
            self.save_draft()
        elif key == "\r":
            self.submit()
        elif key != "\n":
            text, cursor, handled = edit_buffer(self.body, self.body_cursor,
                                                "\r" if key == key_input.NEWLINE else key, multiline=True)
            if handled:
                self.input_scroll = None
                if text != self.body:
                    self.draft_version += 1
                    self.input_error = ""
                self.body, self.body_cursor = text, cursor
                self.save_draft()

    def open_viewer(self) -> None:
        if self.mode != "nav" or self.focus not in {"agents", "threads"}:
            return
        item = self.selected(self.focus)
        if item is None:
            return
        self.preview, self.diagnostics = self.focus, False
        self.refresh_detail()
        self.viewer = self.focus, self.identity(item)
        self.mode = "viewer"
        self.detail_anchor_pending = True

    def close_viewer(self) -> None:
        self.viewer = None
        self.mode = "nav"

    def scroll_detail(self, key: Any) -> None:
        direction = -1 if key in (curses.KEY_UP, "k", curses.KEY_PPAGE) else 1
        amount = max(1, self.detail_available - 1) if key in (
            curses.KEY_PPAGE, curses.KEY_NPAGE) else 1
        self.scrolls["detail"] = self.clamp_detail(
            0 if key == curses.KEY_HOME else self.detail_total if key == curses.KEY_END
            else self.scrolls["detail"] + direction * amount)

    def toggle_detail(self) -> None:
        if self.technical_supported():
            self.detail_technical = not self.detail_technical
            self.scrolls["detail"] = 0
            self.detail_anchor_pending = True

    def viewer_key(self, key: Any) -> None:
        if key in (key_input.ESCAPE, "q", " "):
            self.close_viewer()
        elif key == "t":
            self.toggle_detail()
        elif key in (curses.KEY_UP, curses.KEY_DOWN, "j", "k", curses.KEY_PPAGE,
                     curses.KEY_NPAGE, curses.KEY_HOME, curses.KEY_END):
            self.scroll_detail(key)

    def key(self, key: Any) -> None:
        if key is None:
            return
        self.dirty = True
        if key == curses.KEY_RESIZE:
            return
        if isinstance(key, key_input.MouseEvent):
            self.mouse_key(key)
            return
        self.set_status("")
        if self.mode == "viewer":
            self.viewer_key(key)
            return
        if self.focus == "agents" and self.mode == "nav":
            self.result_tags.pop(self.identity(self.selected("agents")), None)
        if self.mode == "input":
            self.input_key(key)
            return
        if self.mode == "confirm":
            self.confirm_key(key)
            return
        if self.mode == "evidence":
            self.edit_key(key)
            return
        if self.mode == "settings":
            self.settings_key(key)
            return
        if self.mode == "help":
            if key in (key_input.ESCAPE, key_input.F1, "?", "q"):
                self.mode = self.help_return
            else:
                self.overlay_key(key)
            return
        if key == key_input.ESCAPE:
            if self.diagnostics:
                self.diagnostics = False
                self.refresh_detail()
            elif self.quick_send:
                self.request_quit()
        elif key == "q":
            self.request_quit()
        elif key in ("\t", key_input.SHIFT_TAB, curses.KEY_LEFT, curses.KEY_RIGHT, "h", "l"):
            direction = -1 if key in (key_input.SHIFT_TAB, curses.KEY_LEFT, "h") else 1
            self.focus = self.REGIONS[(self.REGIONS.index(self.focus) + direction) % len(self.REGIONS)]
            if self.focus != "detail":
                self.preview, self.diagnostics = self.focus, False
            self.refresh_detail()
        elif key in (curses.KEY_UP, curses.KEY_DOWN, "j", "k", curses.KEY_PPAGE,
                     curses.KEY_NPAGE, curses.KEY_HOME, curses.KEY_END):
            direction = -1 if key in (curses.KEY_UP, "k", curses.KEY_PPAGE) else 1
            amount = 1
            if self.focus == "detail":
                self.scroll_detail(key)
            else:
                if key in (curses.KEY_PPAGE, curses.KEY_NPAGE):
                    visible = {value for _, target, value in self.hit_map if target == "list-row"
                               and (isinstance(value, tuple) if self.focus == "threads"
                                    else isinstance(value, int))}
                    amount = max(1, len(visible) - 1)
                items = self.items(self.focus)
                self.selections[self.focus] = (0 if key == curses.KEY_HOME else len(items) - 1
                                               if key == curses.KEY_END else
                                               self.selections[self.focus] + direction * amount)
                self.selected()
                self.preview, self.diagnostics = self.focus, False
                self.refresh_detail()
        elif key == " ":
            self.open_viewer()
        elif key == "\r":
            if self.focus == "agents":
                self.choose_agent()
            elif self.focus == "threads":
                self.reply()
        elif key == "s":
            agent = self.selected("agents")
            if not self.repo_id and agent:
                self.repo_id = agent["repo_id"]
                self.refresh()
            self.mode = "settings"
            self.overlay_scroll = 0
        elif key in ("?", key_input.F1):
            self.help_return, self.mode = "nav", "help"
            self.overlay_scroll = 0
        elif key == "e":
            self.diagnostics, self.focus = True, "detail"
            self.scrolls["detail"] = 0
        elif key in ("r", "\x12"):
            self.refresh()
        elif key == "t" and self.focus == "detail" and self.technical_supported():
            self.toggle_detail()
        elif self.focus == "threads" and key in ("w", "t", "a", "n"):
            self.thread_action(key)

    def add_hit(self, rect: tuple[int, int, int, int], target: str, value: Any = None) -> None:
        if rect[2] > 0 and rect[3] > 0:
            self.hit_map.append((rect, target, value))

    def mouse_key(self, event: key_input.MouseEvent) -> None:
        root = self.screen
        if self.quick_send:
            rect = content_rect(*root.getmaxyx(), 120)
            self.screen = rect.window(root)
            event = key_input.MouseEvent(event.row - rect.y, event.column - rect.x, event.action)
        try:
            self.mouse_content(event)
        finally:
            self.screen = root

    def mouse_content(self, event: key_input.MouseEvent) -> None:
        height, width = self.screen.getmaxyx()
        if self.mode == "viewer":
            if self.environment.get("HAM_MAILBOX_NO_MOUSE"):
                return
            y, x, rows, columns = self.viewer_rect()
            inside = y <= event.row < y + rows and x <= event.column < x + columns
            if event.action in {"wheel_up", "wheel_down"}:
                self.scrolls["detail"] = self.clamp_detail(
                    self.scrolls["detail"] + (-3 if event.action == "wheel_up" else 3))
            elif event.action == "press" and not inside:
                self.close_viewer()
            return
        if (self.environment.get("HAM_MAILBOX_NO_MOUSE") or width < 32 or height < 10
                or not 0 < event.row < height - 1 or not 0 <= event.column < width):
            return
        if self.mode == "help" and event.action == "press":
            self.mode = self.help_return
            return
        hits = [(rect, target, value) for rect, target, value in reversed(self.hit_map)
                if rect[0] <= event.row < rect[0] + rect[2]
                and rect[1] <= event.column < rect[1] + rect[3]]
        if self.mode == "settings":
            self.settings_mouse(event, hits)
            return
        wheel = event.action in {"wheel_up", "wheel_down"}
        amount = -3 if event.action == "wheel_up" else 3
        if wheel:
            if self.mode in {"confirm", "evidence", "help"}:
                if any(target == "overlay" for _, target, _ in hits):
                    self.overlay_scroll = max(0, self.overlay_scroll + amount)
                return
            input_hit = next((value for _, target, value in hits if target == "input"), None)
            if input_hit is not None:
                first, maximum = input_hit
                self.input_scroll = min(maximum, max(0, first + amount))
                return
            region = next((value for _, target, value in hits if target == "region"), None)
            if region == "detail":
                self.scrolls["detail"] = self.clamp_detail(self.scrolls["detail"] + amount)
            elif region is not None:
                self.selections[region] += amount
                self.selected(region)
                if self.preview == region:
                    self.diagnostics = False
                    self.refresh_detail()
            return
        if event.action != "press":
            return
        if self.mode == "input":
            for rect, target, value in hits:
                if target == "input-line":
                    text, offset = value
                    self.body_cursor = offset + character_index_at_display_column(
                        text, event.column - rect[1])
                    self.save_draft()
                    break
            return
        if self.mode in {"confirm", "evidence"}:
            for rect, target, value in hits:
                if target == "button":
                    if self.mode == "confirm":
                        self.confirm["choice"] = value
                        self.confirm_key("\r")
                    else:
                        self.edit_key("\r" if value else key_input.ESCAPE)
                    break
                if target == "edit-text" and self.mode == "evidence":
                    text, offset = value
                    self.edit["cursor"] = offset + character_index_at_display_column(
                        text, event.column - rect[1])
                    break
            return
        tab = next((value for _, target, value in hits if target == "list-tab"), None)
        if tab is not None:
            self.set_status("")
            self.focus = self.preview = tab
            self.diagnostics = False
            self.refresh_detail()
            return
        region = next((value for _, target, value in hits if target == "region"), None)
        if region is None:
            return
        self.set_status("")
        self.focus = region
        if region != "detail":
            self.preview, self.diagnostics = region, False
            row = next((value for _, target, value in hits if target == "list-row"), None)
            if row is not None and region == "threads":
                row = next((index for index, item in enumerate(self.items(region))
                            if self.identity(item) == row), None)
                if row is None:  # The clicked snapshot's item has since disappeared.
                    return
            activate = row is not None and row == self.selections[region]
            if row is not None:
                self.selections[region] = row
            self.refresh_detail()
            if region == "agents":
                self.result_tags.pop(self.identity(self.selected("agents")), None)
            if activate:
                self.choose_agent() if region == "agents" else self.reply()

    def initialize_colors(self) -> None:
        self.theme = Theme(self.environment)

    def style(self, role: str = "text", attribute: int = 0) -> int:
        return self.theme.style(role) | attribute

    def selection_style(self) -> int:
        return self.theme.style("selection")

    def state_style(self, state: str, *, stale: bool = False, selected: bool = False) -> int:
        return self.theme.turn_style(state, stale=stale, selected=selected)

    def put(self, row: int, column: int, text: str, attribute: int = 0) -> None:
        height, width = self.screen.getmaxyx()
        if not (0 <= row < height and 0 <= column < width):
            return
        available = width - column - (1 if row == height - 1 else 0)
        if available <= 0:
            return
        value = clip(str(text), available)
        if value:
            try:
                self.screen.addstr(row, column, value, attribute)
            except curses.error:
                pass

    def box(self, rect: tuple[int, int, int, int], title: str, focused: bool = False,
            warning: bool = False) -> tuple[int, int, int, int]:
        y, x, height, width = rect
        attr = self.style("warning" if warning else "accent" if focused else "text",
                          curses.A_BOLD if focused else curses.A_DIM)
        if height < 2 or width < 2:
            return y, x, 0, 0
        self.put(y, x, "┌" + "─" * (width - 2) + "┐", attr)
        self.put(y, x + 2, truncate_display_text(" " + title + " ", max(0, width - 4)), attr)
        for row in range(y + 1, y + height - 1):
            self.put(row, x, "│", attr)
            self.put(row, x + width - 1, "│", attr)
        self.put(y + height - 1, x, "└" + "─" * (width - 2) + "┘", attr)
        return y + 1, x + 1, height - 2, width - 2

    def mode_word(self) -> str:
        if self.mode == "nav":
            return "NAV · " + self.focus.title()
        if self.mode == "input":
            return "INPUT → " + self.address(self.recipient)
        if self.mode == "settings" and self.settings_picker is not None:
            return "CHECK" if self.settings_picker["field"] == "members" else "PICK"
        return self.mode.upper()

    def footer_hints(self) -> list[Hint]:
        if self.mode == "viewer":
            return [Hint("↑↓", "Scroll"), Hint("t", "Technical"), Hint("Esc", "Close", 0)]
        if self.mode == "input":
            return [Hint("Enter", "Send", 0), Hint("Shift+Enter", "Newline", 1),
                    Hint("Ctrl+G", "Action/FYI", 3), Hint("Ctrl+D", "Direct"),
                    Hint("Ctrl+R", "Refresh"), Hint("F1", "Help", 4), Hint("Esc", "Keep draft", 2)]
        if self.mode == "confirm":
            return [Hint("Tab/←→", "Select"), Hint("↑↓", "Scroll"), Hint("Enter", "Choose"), Hint("Esc", "Cancel", 0)]
        if self.mode == "evidence":
            return [Hint("←→", "Edit"), Hint("Enter", "Review"), Hint("Esc", "Cancel", 0)]
        if self.mode == "settings":
            if self.settings_picker is not None:
                hints = [Hint("↑↓", "Select")]
                if self.settings_picker["field"] == "members":
                    hints.append(Hint("Space", "Toggle"))
                return hints + [Hint("Enter", "Save"), Hint("Esc", "Cancel", 0)]
            if self.settings_numeric is not None:
                return [Hint("", "Digits only"), Hint("←→", "Cursor"), Hint("Enter", "Save"), Hint("Esc", "Cancel", 0)]
            return [Hint("↑↓", "Select"), Hint("←→", "Adjust"), Hint("Enter", "Pick/save"),
                    Hint("m", "Main"), Hint("Space", "Member"), Hint("Esc", "Discard/close", 0)]
        if self.mode == "help":
            return [Hint("↑↓", "Scroll"), Hint("PgUp/PgDn", "Page"), Hint("F1", "Close", 1), Hint("Esc", "Close", 0)]
        context = ([Hint("Space", "View"), Hint("Enter", "Send")] if self.focus == "agents" else
                   [Hint("Space", "View"), Hint("Enter", "Reply"), Hint("w", "Withdraw"),
                    Hint("t", "Retry"), Hint("a/n", "Recover")] if self.focus == "threads" else
                   [Hint("↑↓", "Scroll"), Hint("PgUp/PgDn", "Page")] +
                   ([Hint("t", "Technical")] if self.technical_supported() else []))
        return [Hint("Tab", "Focus")] + context + [
            Hint("s", "Settings"), Hint("r", "Refresh"), Hint("e", "Errors"),
            Hint("F1", "Help", 1), Hint("q", "Close", 0)]

    def side_by_side_width(self) -> int:
        # Each half needs its own two border cells. Use the same full-column
        # model as the table, including the minimum readable summary.
        columns = self.agent_columns(None, self.agents())
        return 2 * (sum(size for _, size, _ in columns) + 2 * (len(columns) - 1) + 2)

    def region_rects(self, top: int, height: int, width: int) -> dict[str, tuple[int, int, int, int]]:
        if width < 80 or height < 18:
            self.layout = "single"
            return {self.focus: (top, 0, height, width)}
        # Reserve six Detail content rows plus its notice and borders, while
        # keeping at least eight rows in the list after INPUT is reserved.
        detail_height = min(height - 8, max(9, round(height * .4)))
        if self.quick_send:
            detail_height = min(detail_height, 9)
        top_height = height - detail_height
        detail = (top + top_height, 0, detail_height, width)
        if width < self.side_by_side_width():
            self.layout = "tabbed"
            active = self.focus if self.focus != "detail" else self.preview
            return {active: (top, 0, top_height, width), "detail": detail}
        self.layout = "stacked"
        left = width // 2
        return {"agents": (top, 0, top_height, left),
                "threads": (top, left, top_height, width - left),
                "detail": detail}

    def render_list_tabs(self, rect: tuple[int, int, int, int], detail: bool = False) -> None:
        y, x, _, width = rect
        active = self.focus if self.focus != "detail" else self.preview
        column = x + 2
        for index, region in enumerate(("agents", "threads")):
            if index:
                self.put(y, column, " │ ", curses.A_DIM)
                column += 3
            label = ("› " if self.quick_send and self.mode == "nav" and region == self.focus else "") + region.title()
            attr = self.style("accent", curses.A_BOLD) if region == active and not (
                self.quick_send and self.mode == "input") else curses.A_DIM
            self.put(y, column, label, attr)
            self.add_hit((y, column, 1, len(label)), "list-tab", region)
            column += len(label)
        if detail:
            self.put(y, column, " · Detail", self.style("accent", curses.A_BOLD) if not (
                self.quick_send and self.mode == "input") else curses.A_DIM)

    def format_time(self, value: float | None, now: float) -> str:
        """Repaint relative ages only when their displayed unit changes."""
        formatted = status_time(value, now)
        if value is not None:
            age = max(0, int(now - value))
            unit = 1 if age < 60 else 60 if age < 3600 else 3600 if age < 86400 else 86400
            deadline = math.ceil(value + (age // unit + 1) * unit)
            self.next_age_refresh = min(self.next_age_refresh, deadline)
            if self.detail_age_deadlines is not None:
                suffix = formatted[formatted.rfind("("):]
                self.detail_age_deadlines[suffix] = min(
                    self.detail_age_deadlines.get(suffix, float("inf")), deadline)
        return formatted

    def render_if_needed(self) -> None:
        if self.dirty or self.wall_clock() >= self.next_age_refresh:
            self.render()

    def render(self) -> None:
        root = self.screen
        root.erase()
        if self.quick_send:
            rect = content_rect(*root.getmaxyx(), 120)
            self.screen = rect.window(root)
        try:
            self.render_content()
        finally:
            self.screen = root

    def render_content(self) -> None:
        self.dirty = False
        self.next_age_refresh = float("inf")
        self.cursor_position = None
        self.hit_map.clear()
        if self.mode == "viewer":
            self.refresh_detail()
        height, width = self.screen.getmaxyx()
        self.layout = "tiny" if width < 32 or height < 10 else "single"
        repo = self.repo_id or "all repositories"
        short_repo = Path(repo).name
        if self.quick_send:
            draw_title(self.screen, self.theme, "Quick send",
                       f"{short_repo} · to {self.address(self.recipient)}", width,
                       self.mode.upper())
        else:
            word = truncate_display_text(self.mode_word(), width)
            right = max(0, width - display_width(word))
            self.put(0, 0, "HAM", curses.A_BOLD)
            self.put(0, 5, truncate_display_text(short_repo, max(0, right - 6)), curses.A_DIM)
            self.put(0, right, word, self.style("accent", curses.A_BOLD))
        hints = self.footer_hints()
        max_footer_rows = 2 if self.quick_send else 1
        footer_rows = len(footer_lines(hints, max(0, width - 1), max_footer_rows))
        draw_footer(self.screen, height - footer_rows, 0, max(0, width - 1), hints, self.theme,
                    max_rows=max_footer_rows)
        bottom = height - footer_rows
        content_top = 2 if self.quick_send else 1
        input_visible = self.layout != "tiny" and self.input_active()
        notice = self.receiver_notice()
        status = self.status if not (input_visible and self.status == self.input_error) else ""
        if notice or status:
            bottom -= 1
            error = bool(status) and self.status_level == "error"
            text = ("✕ " if error else "") + status
            if notice and not status:
                text = notice
            elif notice and display_width(text + " · " + notice) <= max(0, width - 1):
                text += " · " + notice
            self.put(bottom, 0, text,
                     self.style("error" if error else "text", curses.A_BOLD if error or notice else 0))
        if self.layout == "tiny":
            self.put(min(2, height - 2), 0, "Resize terminal", curses.A_BOLD)
        else:
            self.refresh_detail()
            input_rect = None
            if input_visible:
                body_height = max(0, bottom - content_top)
                lines = wrap_message_lines(self.body.split("\n"), width=max(1, width - 4),
                                           cursor_row=0, cursor_column=0)[0]
                rows = min(max(3, body_height // 3), max(3, len(lines)))
                warning = bool(self.recipient and not self.recipient.get("extension_present"))
                input_height = rows + 2 + int(warning) + int(bool(self.input_error))
                if self.quick_send and self.mode == "input":
                    input_height = max(10, input_height)
                input_height = min(input_height, max(3 + int(bool(self.input_error)), body_height - 3))
                bottom -= input_height
                input_rect = (bottom, 0, input_height, width)
            for region, rect in self.region_rects(content_top, max(0, bottom - content_top), width).items():
                self.add_hit(rect, "region", region)
                tabs = self.layout == "single" or (self.layout == "tabbed" and region != "detail")
                title = "" if tabs else region.title()
                active = self.focus == region and not (self.quick_send and self.mode == "input")
                if self.quick_send and self.mode == "nav" and active and title:
                    title = "› " + title
                inner = self.box(rect, title, active)
                if tabs:
                    self.render_list_tabs(rect, detail=region == "detail")
                if region == "agents":
                    self.render_agents(inner)
                elif region == "threads":
                    self.render_threads(inner)
                elif self.mode != "viewer":
                    self.render_detail(inner)
            if input_rect:
                self.render_input(input_rect)
            if self.mode in {"confirm", "settings", "help", "evidence"}:
                self.render_overlay((content_top, 0, max(0, bottom - content_top), width))
        if self.mode == "viewer":
            self.render_viewer()
        if self.cursor_position is not None:
            row, column = self.cursor_position
            if 0 <= row < height - 1 and 0 <= column < width:
                try:
                    self.screen.move(row, column)
                    curses.curs_set(1)
                except (curses.error, AttributeError):
                    pass
        else:
            try:
                curses.curs_set(0)
            except curses.error:
                pass
        self.screen.refresh()

    @staticmethod
    def agent_columns(width: int | None, agents: list[Mapping[str, Any]]) -> list[tuple[str, int, bool]]:
        alias_width = min(18, max(8, max((display_width(agent.get("alias") or agent["terminal_id"])
                                       for agent in agents), default=8)))
        role_width = min(8, max(4, max((display_width(agent.get("role") or "—")
                                      for agent in agents), default=4)))
        columns = [("turn", 11, False), ("alias", alias_width, False), ("mail", 4, True),
                   ("rx", 2, True), ("role", role_width, False),
                   *((name, 3, True) for name in ("phone", "emu", "server")),
                   ("summary", 16, False)]
        full_width = sum(size for _, size, _ in columns) + 2 * (len(columns) - 1)
        if width is None:
            return columns
        # Full-width tabs should not lose resource columns to a long alias or
        # role. Fit their existing minimums before dropping any column.
        if width >= full_width - (alias_width - 8) - (role_width - 4):
            shortage = max(0, full_width - width)
            alias_trim = min(shortage, alias_width - 8)
            columns[1] = ("alias", alias_width - alias_trim, False)
            columns[4] = ("role", role_width - (shortage - alias_trim), False)
            columns[-1] = ("summary", 16 + max(0, width - full_width), False)
            return columns
        # Keep RX at the smallest usable width by collapsing TURN first.
        turn_width = 11 if width >= 11 + 8 + 4 + 2 + 6 else 3
        alias_width = min(alias_width, max(8, width - turn_width - 4 - 4))
        columns = [("turn", turn_width, False), ("alias", alias_width, False), ("mail", 4, True)]
        def table_width() -> int:
            return sum(size for _, size, _ in columns) + 2 * (len(columns) - 1)
        if table_width() + 4 - (alias_width - 8) <= width:
            alias_width -= max(0, table_width() + 4 - width)
            columns[1] = ("alias", alias_width, False)
            columns.append(("rx", 2, True))
        summary = width - table_width() >= 18
        if summary:
            columns.append(("summary", width - table_width() - 2, False))
        for additions in ([("role", role_width, False)],
                          [(name, 3, True) for name in ("phone", "emu", "server")]):
            cost = sum(size + 2 for _, size, _ in additions)
            flexible = columns[-1][1] - 16 if summary else columns[1][1] - 8 + width - table_width()
            if cost <= flexible:
                if summary:
                    name, size, centered = columns.pop()
                    columns.extend(additions)
                    columns.append((name, size - cost, centered))
                else:
                    columns.extend(additions)
                    columns[1] = ("alias", alias_width - max(0, table_width() - width), False)
                    alias_width = columns[1][1]
        if not summary:
            columns[1] = ("alias", columns[1][1] + width - table_width(), False)
        return columns

    @staticmethod
    def agent_cell(value: str, width: int, centered: bool) -> str:
        value = truncate_display_text(value, width)
        spare = max(0, width - display_width(value))
        left = spare // 2 if centered else 0
        return " " * left + value + " " * (spare - left)

    def column_text(self, values: Mapping[str, str], columns: list[tuple[str, int, bool]]) -> str:
        return "  ".join(self.agent_cell(values.get(name, ""), size, centered)
                          for name, size, centered in columns)

    def render_table_header(self, y: int, x: int, text: str, width: int) -> None:
        text = truncate_display_text(text, width)
        self.put(y, x, pad_display_text(text, width), curses.A_BOLD)
        column = 0
        for part in text.split(" "):
            if part.isalnum():
                self.put(y, x + column, part, curses.A_BOLD | curses.A_UNDERLINE)
            column += display_width(part) + 1

    def list_window(self, region: str, lines: list[tuple[str, int, int, bool]], height: int
                    ) -> list[tuple[str, int, int, bool]]:
        chosen = next((index for index, line in enumerate(lines)
                       if line[1] == self.selections[region]), None)
        scroll = self.scrolls[region]
        if chosen is not None:
            selected_rows = sum(line[1] == self.selections[region] for line in lines[chosen:])
            visible_end = chosen + min(selected_rows, max(1, height))
            if chosen < scroll:
                scroll = chosen
                if region == "threads":
                    # Keep a returning group's rule visible when it fits with
                    # the selected entry; presentation rows are not selections.
                    while (scroll > 0 and lines[scroll - 1][1] < 0 and
                           visible_end - (scroll - 1) <= height):
                        scroll -= 1
            elif visible_end > scroll + height:
                scroll = max(0, visible_end - height)
            if region == "agents":
                section = next((index for index in range(chosen, -1, -1)
                                if lines[index][1] == -2), chosen)
                first_agent = section
                while first_agent < chosen and lines[first_agent][1] < 0:
                    first_agent += 1
                if chosen - section < height:
                    # Restore a section heading when its selected row fits below it.
                    scroll = min(scroll, section)
                elif scroll > section and height > 1:
                    # Long sections retain context without hiding the selected row.
                    context = [line for line in lines[section:first_agent]
                               if line[1] in {-2, -3}][:height - 1]
                    anchor = next((index for index in range(chosen, section, -1)
                                   if lines[index][1] == -4), None)
                    workspace = next((index for index in range(chosen, anchor or section, -1)
                                      if lines[index][1] == -5), None)
                    body_start = first_agent
                    for heading in (anchor, workspace):
                        if heading is not None and len(context) < height - 1:
                            context.append(lines[heading])
                            body_start = heading + 1
                    body_height = height - len(context)
                    scroll = max(body_start, min(scroll, chosen), chosen - body_height + 1)
                    self.scrolls[region] = scroll
                    return context + lines[scroll:scroll + body_height]
        scroll = min(max(0, scroll), max(0, len(lines) - height))
        self.scrolls[region] = scroll
        return lines[scroll:scroll + max(0, height)]

    def render_agents(self, rect: tuple[int, int, int, int]) -> None:
        y, x, height, width = rect
        if height <= 0 or width <= 0:
            return
        agents = self.agents()
        columns = self.agent_columns(width, agents)
        header_style = self.style("accent", curses.A_BOLD)
        headers = {"alias": "ALIAS", "turn": "    TURN" if columns[0][1] == 11 else "",
                   "mail": "MAIL", "rx": "RX", "role": "ROLE", "phone": "PHN",
                   "emu": "EMU", "server": "SRV", "summary": "PHASE · SUMMARY"}
        lines: list[tuple[str, int, int, bool]] = []
        hosts = sorted(set([agent["host"] for agent in agents] +
                           ([self.local["host"]] if self.local.get("host") else []) +
                           [host for host, snapshot in self.remote.items()
                            if any(item["host"] == host and item["uuid"] == peer["uuid"]
                                   for peer in self.local.get("peers", []) if peer["canonical_name"] == host
                                   for item in snapshot.get("hosts", []))] +
                           list(self.remote_errors)),
                       key=lambda host: (host != self.local.get("host"), host))
        current_workspace = self.agents_workspace()
        sections = [(host, False) for host in hosts]
        if self.repo_id is not None:
            sections.extend((host, True) for host in hosts
                            if any(agent["host"] == host and agent["repo_id"] != self.repo_id
                                   for agent in agents))
        other_heading = False
        for host, other_repos in sections:
            if other_repos and not other_heading:
                lines.append(("", -1, 0, False))
                lines.append(("Other repositories", -1, curses.A_BOLD, False))
                other_heading = True
            group = [(index, agent) for index, agent in enumerate(agents)
                     if agent["host"] == host and
                     (self.repo_id is not None and agent["repo_id"] != self.repo_id) == other_repos]
            stale = host in self.remote_errors or any(agent.get("stale") for _, agent in group)
            count = f"{len(group)} {'agent' if len(group) == 1 else 'agents'}"
            host_width = max(0, width - display_width("▾  · " + count))
            label = "▾ " + truncate_display_text(host, host_width) + " · " + count
            findings = self.host_resource_cue(host)
            if stale:
                findings = "stale" + (" · " + findings if findings else "")
            if findings and display_width(label) + 3 < width:
                label += " · " + truncate_display_text(findings, width - display_width(label) - 3)
            lines.append((label, -2, curses.A_BOLD, False))
            if group:
                lines.append((self.column_text(headers, columns), -3, header_style, False))
            if host in self.remote_errors:
                lines.append((self.remote_errors[host], -1, self.style("warning"), False))
            previous_group: Any = object()
            previous_workspace: Any = object()
            for index, agent in group:
                group_key = agent["repo_id"] if other_repos else (agent["repo_id"], self.role_workspace(agent))
                workspace = agent.get("workspace_id") or None
                anchor = self.role_workspace(agent)
                if group_key != previous_group:
                    members = [item for _, item in group
                               if (item["repo_id"] if other_repos else
                                   (item["repo_id"], self.role_workspace(item))) == group_key]
                    current = bool(not other_repos and anchor and host == self.local.get("host")
                                   and anchor == current_workspace)
                    suffix = f" · {len(members)}" + (" · current" if current else "")
                    name = (Path(group_key).name if other_repos else
                            self.workspace_label(host, anchor, members))
                    label = "  " + truncate_display_text(name, max(0, width - 2 - display_width(suffix))) + suffix
                    lines.append((label, -4, self.style("accent", curses.A_BOLD) if current else curses.A_DIM, False))
                    previous_group = group_key
                    previous_workspace = object()
                if not other_repos and workspace != anchor and workspace != previous_workspace:
                    members = [item for _, item in group if item["repo_id"] == agent["repo_id"]
                               and item.get("workspace_id") == workspace and self.role_workspace(item) == anchor]
                    current = bool(host == self.local.get("host") and workspace == current_workspace)
                    suffix = f" (worktree) · {len(members)}" + (" · current" if current else "")
                    name = self.workspace_label(host, workspace, members)
                    label = "    " + truncate_display_text(name, max(0, width - 4 - display_width(suffix))) + suffix
                    lines.append((label, -5, self.style("accent", curses.A_BOLD) if current else curses.A_DIM, False))
                previous_workspace = workspace
                chosen = index == self.selections["agents"]
                marker = " "
                turn = agent.get("turn_state") or "unknown"
                glyph = self.theme.turn_label(turn).split()[0]
                receiver = agent.get("extension_present")
                rx = "?" if agent.get("stale") or receiver is None else "✓" if receiver else "⚠"
                values = {"alias": agent.get("alias") or agent["terminal_id"],
                          "turn": marker + " " + glyph + " " + pad_display_text(turn, 7),
                          "mail": "?" if agent.get("stale") else str(agent.get("unread", "?")),
                          "rx": rx, "role": agent.get("role") or "—", **self.resource_counts(agent),
                          "summary": " · ".join(str(agent[field]) for field in ("phase", "summary")
                                                if agent.get(field))}
                if values["mail"] == "0":
                    values["mail"] = "·"
                if columns[0][1] == 3:
                    values["turn"] = marker + " " + glyph
                tag = self.result_tags.get(self.identity(agent))
                if tag:
                    name = "summary" if columns[-1][0] == "summary" else "alias"
                    size = next(size for column, size, _ in columns if column == name)
                    suffix = "[" + tag[0] + "]"
                    prefix = truncate_display_text(values[name], max(0, size - display_width(suffix) - 1))
                    values[name] = (prefix + " " if prefix else "") + suffix
                text = self.column_text(values, columns)
                attr = self.selection_style() if chosen else 0
                lines.append((text, index, attr, True))
        if not agents:
            lines.append((" No agents. r refresh", -1, curses.A_DIM, False))
        for row, (text, _index, attr, _primary) in enumerate(self.list_window("agents", lines, height), y):
            if _index >= 0 and _index == self.selections["agents"]:
                self.theme.draw_selected_row(self.screen, row, text[1:], width, x, marker="›")
            else:
                self.put(row, x, pad_display_text(truncate_display_text(text, width), width), attr)
            if _index >= 0:
                agent = agents[_index]
                turn = agent.get("turn_state") or "unknown"
                glyph = self.theme.turn_label(turn).split()[0]
                chosen = _index == self.selections["agents"]
                offset = 0
                counts = {"mail": "?" if agent.get("stale") else str(agent.get("unread", "?")),
                          **self.resource_counts(agent)}
                if counts["mail"] == "0":
                    counts["mail"] = "·"
                for name, size, centered in columns:
                    if name == "turn":
                        value = glyph if size == 3 else glyph + " " + pad_display_text(turn, 7)
                        turn_attr = (attr | curses.A_BOLD if chosen else
                                     self.state_style(turn, stale=bool(agent.get("stale"))))
                        self.put(row, x + 2, value, turn_attr)
                    elif not chosen and name in counts:
                        value = counts[name]
                        if value == "·":
                            self.put(row, x + offset, self.agent_cell(value, size, centered),
                                     attr | curses.A_DIM)
                        elif name == "mail" and value.isdigit() and int(value):
                            self.put(row, x + offset, self.agent_cell(value, size, centered),
                                     attr | curses.A_BOLD)
                    offset += size + 2
            if _index >= 0:
                self.add_hit((row, x, 1, width), "list-row", _index)

    def thread_state(self, state: str, *, direct: bool = False) -> tuple[str, str]:
        """Use one-cell ASCII cues; direct acceptance is never a native receipt."""
        glyph, palette = {
            "pending": (".", "activity"), "queued": (".", "activity"),
            "submitting": (">", "activity"),
            "delivering": ("?", "warning"), "unknown": ("?", "warning"),
            "accepted": ("+", "success"), "delivered": ("+", "success"),
            "handled": ("=", "accent"),
            "failed": ("!", "error"), "undeliverable": ("!", "error"),
            "withdrawn": ("-", "muted"),
        }.get(state, ("?", "muted"))
        label = "UNCERTAIN delivering" if state == "delivering" else state
        return f"{glyph} {'DIRECT ' if direct else ''}{label}", palette

    def thread_kind(self, kind: str) -> tuple[str, str]:
        return {"fyi": ("i fyi", "accent"), "action": ("! action", "activity")}.get(
            kind, (f"? {kind}", "muted"))

    def outbox_operation(self, kind: str) -> tuple[str, str]:
        """Outbox kinds describe transport operations, not FYI/action mail kinds."""
        glyph = {"message": ">", "receipt": "<", "setting": "#", "withdraw": "-"}.get(kind, "?")
        return f"{glyph} {kind}", "accent" if kind in {"message", "receipt", "setting", "withdraw"} else "muted"

    def message_sender(self, message: Mapping[str, Any]) -> str:
        sender = str(message.get("sender_label") or "—")
        repo = message.get("sender_repo_id") or message.get("repo_id")
        if self.repo_id is not None and repo and repo != self.repo_id:
            sender += f" (repo {truncate_display_text(Path(repo).name, 24)})"
        return sender

    def render_threads(self, rect: tuple[int, int, int, int]) -> None:
        y, x, height, width = rect
        if height <= 0 or width <= 0:
            return
        self.render_table_header(y, x, "  STATE / TITLE · PARTICIPANTS", width)
        lines: list[tuple[str, int, int, bool]] = []
        accents: dict[int, tuple[str, str, str, str]] = {}
        items = self.items("threads")
        active_count = sum(not self.thread_handled(item) for item in items)
        for index, item in enumerate(items):
            handled = self.thread_handled(item)
            if index == 0 or index == active_count:
                if index:
                    lines.append(("", -1, 0, False))
                label = (f"─ Handled ({len(items) - active_count}) " if handled else
                         f"─ Active ({active_count}) ")
                label = truncate_display_text(label, width)
                lines.append((label + "─" * max(0, width - display_width(label)),
                              -1, curses.A_BOLD | (curses.A_DIM if handled else 0), False))
            chosen = index == self.selections["threads"]
            marker = " "
            if item["type"] == "thread":
                messages = item.get("messages", [])
                latest = messages[-1] if messages else {}
                uncertain = any(message.get("state") == "delivering" for message in messages)
                unfinished = next((message for message in reversed(messages)
                                   if message.get("state") != "handled"), latest)
                state, palette = self.thread_state("delivering" if uncertain else unfinished.get("state", "unknown"))
                kind, kind_palette = self.thread_kind(latest.get("kind", "unknown"))
                body = (latest.get("body") or "").splitlines()
                title = item.get("subject") or (body[0] if body else "(empty message)")
                participants = f"{self.message_sender(latest)} → {latest.get('recipient_address', '—')}"
                metadata = f"{kind} · {participants} · {state}"
            elif item["type"] == "direct":
                state, palette = self.thread_state(item["acceptance"], direct=True)
                kind, kind_palette = "> direct", "accent"
                title = item.get("subject") or (item.get("body") or "Terminal input").splitlines()[0]
                metadata = f"{kind} · user → {self.address(item.get('target'))} · acceptance ≠ native delivery"
            else:
                status, palette = self.thread_state(item["state"])
                state = "OUTBOX " + status
                kind, kind_palette = self.outbox_operation(item["kind"])
                title = item.get("last_error") or item["item_id"]
                metadata = f"{kind} · → {item['target_host']} · {item['state']}"
            accents[index] = state, palette, kind, kind_palette
            attr = self.selection_style() if chosen else curses.A_DIM if handled else 0
            lines.append((f"{marker} {state} · {title}", index, attr, True))
            lines.append(("  " + metadata, index, attr if chosen else attr | curses.A_DIM, False))
        if not lines:
            lines = [(" No threads. Enter on an agent to send.", -1, curses.A_DIM, False)]
        for row, (text, _index, attr, _primary) in enumerate(self.list_window("threads", lines, height - 1), y + 1):
            if _index >= 0 and _index == self.selections["threads"]:
                self.theme.draw_selected_row(self.screen, row, text[2:], width, x,
                                             marker="› " if _primary else "  ")
            else:
                self.put(row, x, pad_display_text(truncate_display_text(text, width), width), attr)
            if _index >= 0:
                state, palette, kind, kind_palette = accents[_index]
                label, tone = (state, palette) if _primary else (kind, kind_palette)
                chosen = _index == self.selections["threads"]
                label_style = (self.selection_style() | curses.A_BOLD if chosen
                               else self.style(tone, curses.A_BOLD))
                if self.thread_handled(items[_index]):
                    label_style = ((self.selection_style() | curses.A_BOLD) if chosen else
                                   self.style(tone) | curses.A_DIM)
                self.put(row, x + 2, clip(label, width - 2), label_style)
            if _index >= 0:
                self.add_hit((row, x, 1, width), "list-row", self.identity(items[_index]))

    def technical_supported(self) -> bool:
        return not self.diagnostics and (self.detail or {}).get("type") in {"thread", "direct", "outbox"}

    def clamp_detail(self, offset: int) -> int:
        return min(max(0, offset), max(0, self.detail_total - self.detail_available))

    def thread_detail(self, width: int = 100, *,
                      message_starts: list[int] | None = None) -> list[tuple[str, int]]:
        item = self.detail or {}
        result: list[tuple[str, int]] = []

        def header(record: Mapping[str, Any], index: int, total: int,
                   sender: str, recipient: str) -> str:
            metadata = f"Message {index}/{total} · {self.format_time(record.get('created_at'), self.wall_clock())}"
            prefix = metadata + " · "
            # On narrow panels reserve a separate participant row rather than
            # allowing the timestamp to consume both ends of the direction.
            separate = width < display_width(prefix) + 19
            room = max(2, width - (0 if separate else display_width(prefix)) - 3)
            sender = " ".join(sender.splitlines())
            recipient = " ".join(recipient.splitlines())
            sender_room = min(display_width(sender), max(1, room // 2))
            recipient_room = max(1, room - sender_room)
            direction = (truncate_display_text(sender, sender_room) + " → " +
                         truncate_display_text(recipient, recipient_room))
            return (truncate_display_text(metadata, width) + "\n" + direction if separate else prefix + direction)

        if item.get("type") == "thread":
            messages = item["messages"]
            for index, message in enumerate(messages, 1):
                state, palette = self.thread_state(message["state"])
                kind, _ = self.thread_kind(message["kind"])
                summary = f"{kind} · {state}"
                if message.get("confirmation_source") == "operator":
                    summary += " · operator NOT native ACK"
                elif message.get("confirmation_source") == "native":
                    summary += " · native confirmation"
                if message.get("withdraw_requested"):
                    summary += " · withdrawal requested"
                if message.get("hold_reason"):
                    summary += " · HOLD"
                if index > 1:
                    result.append(("", 0))
                if message_starts is not None:
                    message_starts.append(len(result))
                result.extend([
                    (header(message, index, len(messages), self.message_sender(message),
                            str(message.get("recipient_address") or "—")), curses.A_BOLD),
                    (summary,
                     self.style("warning" if message.get("confirmation_source") == "operator" else palette)
                     | curses.A_BOLD),
                    (message["body"], 0)])
                if self.detail_technical:
                    result.extend((line, curses.A_DIM) for line in [
                        *self.message_target(message), describe_message(message),
                        f"via: {message.get('via') or '—'}",
                        f"confirmation source: {message.get('confirmation_source') or '—'}",
                        f"evidence: {message.get('evidence') or '—'}"])
        elif item.get("type") == "direct":
            state, palette = self.thread_state(item["acceptance"], direct=True)
            result = [
                (header(item, 1, 1, str(item.get("sender") or "—"), self.address(item.get("target"))),
                 curses.A_BOLD),
                (state + " · acceptance ≠ native delivery/handled",
                 self.style(palette) | curses.A_BOLD),
                (item.get("body") or "", 0)]
        elif item.get("type") == "outbox":
            state, palette = self.thread_state(item["state"])
            kind, _ = self.outbox_operation(item["kind"])
            result = [
                (f"OUTBOX {state} · Transport operation: {kind}",
                 self.style(palette) | curses.A_BOLD),
                (f"→ {item.get('target_host') or '—'} · transport, NOT native ACK",
                 curses.A_BOLD)]
            # Outbox stores a JSON string: only accept's nested immutable message is mail.
            payload = item.get("payload")
            if isinstance(payload, str):
                try:
                    payload = json.loads(payload)
                except (ValueError, TypeError):
                    payload = None
            message = payload.get("message") if isinstance(payload, dict) else None
            if item["kind"] == "message" and isinstance(message, dict) and isinstance(message.get("body"), str):
                result.append((message["body"], 0))
        if item.get("type") in {"thread", "direct", "outbox"} and self.detail_technical:
            result.extend([("Technical · full recorded source", curses.A_DIM),
                           (json.dumps(item, ensure_ascii=False, indent=2), curses.A_DIM)])
        return result

    def agent_resource_lines(self, item: Mapping[str, Any], now: float) -> list[str]:
        devices, servers = item.get("devices"), item.get("servers")
        lines = ["Devices" + (": ?" if devices is None else ": none" if not devices else "")]
        for device in devices or []:
            icon = {"phone": "📱", "emu": "🤖"}.get(device.get("kind"), "•")
            lines.extend([
                f"  {device.get('kind', '?')}: {icon} {device.get('label') or device.get('device_id') or '?'}",
                f"  ID: {device.get('device_id') or '?'}",
                f"  Claimed: {self.format_time(device.get('claimed_at'), now)}",
                f"  Attached: {yes_no(device.get('attached'))}",
            ])
        lines.append("Servers" + (": ?" if servers is None else ": none" if not servers else ""))
        for server in servers or []:
            lines.extend([
                f"  Port: {server.get('port', '?')} · {server.get('process') or '?'} · PID {server.get('pid', '?')}",
                f"  Bind: {server.get('bind') or '?'}",
                f"  Cwd: {server.get('cwd') or '?'}",
                f"  Inherited: {yes_no(server.get('inherited'))}",
                f"  Started: {self.format_time(server.get('started_at'), now)}",
            ])
            urls = server.get("urls")
            lines.extend([f"  URL: {url}" for url in urls] if urls else
                         ["  URL: ?" if urls is None else "  URL: none"])
        return lines

    def agent_mail_lines(self, item: Mapping[str, Any]) -> list[str]:
        address = self.address(item)
        threads = [thread for thread in self.items("threads") if thread.get("type") == "thread"
                   and any(message.get("recipient_address") == address or
                           (message.get("recipient_host"), message.get("recipient_terminal_id")) ==
                           (item["host"], item["terminal_id"]) or
                           (item.get("agent_id") and message.get("sender") == item["agent_id"]) or
                           (message.get("sender") != "user" and
                            f"{message.get('origin_host')}/{message.get('sender_label')}" == address)
                           for message in thread.get("messages", []))]
        handled = sum(self.thread_handled(thread) for thread in threads)
        summary = (f"Threads: {len(threads) - handled} active · {handled} handled with this agent"
                   if threads else "No threads with this agent yet")
        return ["Mail", summary,
                "Read: Tab → Threads, select a thread, Space opens it"]

    def agent_detail(self, width: int | None = None) -> list[tuple[str, int]]:
        """Build both previews with section-local, display-cell-aligned fields."""
        item = self.detail or {}
        now = self.wall_clock()
        result: list[tuple[str, int]] = []

        def section(title: str, fields: list[str], *, dim: bool = False) -> None:
            if result:
                result.append(("", 0))
            result.extend((part, curses.A_DIM if dim else curses.A_BOLD)
                          for part in (wrap_summary(title, width) if width else [title]))
            labels = [line.strip().partition(": ")[0] for line in fields
                      if ": " in line and not line.startswith("Read: ")]
            label_width = max((display_width(label) for label in labels), default=0)
            for line in fields:
                attribute = curses.A_DIM if dim or line.startswith("Read: ") else 0
                label, separator, value = line.strip().partition(": ")
                if not separator or line.startswith("Read: "):
                    parts = wrap_summary(line, width) if width else [line]
                    result.extend((part, attribute if separator else attribute | curses.A_BOLD)
                                  for part in parts)
                    continue
                prefix = label + ":" + " " * (label_width - display_width(label) + 1)
                if title == "Work" and label == "Summary":
                    attribute |= curses.A_BOLD
                # Keep labels usable in tiny viewers rather than spending the
                # whole row on an indent that leaves no value cells.
                if width is not None and display_width(prefix) >= width:
                    result.extend((part, attribute) for part in wrap_summary(prefix.strip(), width))
                    prefix = ""
                parts = wrap_summary(value, max(1, width - display_width(prefix))) if width else [value]
                result.append((prefix + parts[0], attribute))
                result.extend((" " * display_width(prefix) + part, attribute) for part in parts[1:])

        section("Work", [
            f"Turn state: {item.get('turn_state') or 'unknown'}",
            f"Phase: {item.get('phase') or '—'}",
            f"Summary: {item.get('summary') or '—'}"])
        section("Identity & role", [
            f"Alias: {item.get('alias') or '—'}", f"Address: {self.address(item)}",
            f"Host: {item['host']}{'  STALE/offline' if item.get('stale') else ''}",
            f"Role: {item.get('role') or '—'}", f"Client: {item.get('client') or '—'}",
            *([f"Workspace: {item['workspace_id']}"] if item.get("workspace_id") else []),
            *([f"Pane: {item['pane_id']}"] if item.get("pane_id") else [])])
        section("Devices & servers", self.agent_resource_lines(item, now))
        section("Mail", [f"Unread: {item.get('unread', '?')}", *self.agent_mail_lines(item)[1:]])
        # Agents have always shown receiver evidence directly; only message
        # technical data is folded by t. Preserve that distinction.
        section("Receiver/technical", [
            f"Receiver: {'ready' if item.get('extension_present') else 'extension absent'}",
            f"Extension present: {yes_no(item.get('extension_present'))}",
            *([f"Remedy: runtime status on {item['host']}; ask the user before runtime install "
               f"{item['client']}, then restart only the selected sessions."]
              if item.get("client") in {"omp", "claude"} and item.get("extension_present") in (False, 0) else []),
            f"Repo ID: {item.get('repo_id') or '—'}",
            f"Main checkout: {yes_no(item.get('is_main_checkout'))}",
            f"Terminal ID: {item['terminal_id']}", f"Generation: {item.get('generation', '—')}",
            f"Session reference: {item.get('session_ref') or '—'}",
            f"Last updated: {self.format_time(item.get('status_updated_at'), now)}",
            f"Last seen: {self.format_time(item.get('last_seen'), now)}",
            f"First seen: {self.format_time(item.get('first_seen'), now)}",
            f"User border: {yes_no(item.get('user_border'))}"], dim=True)
        return result

    def detail_lines(self) -> list[str]:
        if self.diagnostics:
            return ["Diagnostics", json.dumps({"connection_errors": self.remote_errors,
                    "pending_operations": list(self.jobs), "status": self.status, "last_error": self.last_error,
                    "repo_id": self.repo_id}, ensure_ascii=False, indent=2)]
        item = self.detail or {}
        if item.get("type") in {"thread", "direct", "outbox"}:
            return [text for text, _attribute in self.thread_detail()]
        if item.get("host") and item.get("terminal_id") and not item.get("type"):
            return [text for text, _attribute in self.agent_detail()]
        return [json.dumps(item, ensure_ascii=False, indent=2)] if item else ["Select an agent or thread to preview."]

    def viewer_rect(self) -> tuple[int, int, int, int]:
        height, width = self.screen.getmaxyx()
        rows = max(0, min(40, round(height * .9), height - 2))
        # Keep the full viewer hint readable at the 40-column breakpoint.
        columns = max(0, min(100, max(37, round(width * .9)), width - 2))
        return (height - rows) // 2, (width - columns) // 2, rows, columns

    def render_viewer(self) -> None:
        y, x, height, width = self.viewer_rect()
        for row in range(y, y + height):
            self.put(row, x, " " * width)
        self.render_detail((y + 1, x + 1, max(0, height - 3), max(0, width - 2)))
        item = self.detail or {}
        if item.get("type") == "thread":
            body = (item.get("messages") or [{}])[-1].get("body") or ""
            title = item.get("subject") or (body.splitlines() or ["Thread"])[0]
        elif item.get("type"):
            title = item["type"].upper() + (" · " + item["subject"] if item.get("subject") else "")
        else:
            title = item.get("alias") or item.get("terminal_id") or "Agent"
        self.box((y, x, height, width), title, True)
        if height > 3:
            self.put(y + height - 2, x + 1,
                     truncate_display_text("↑↓ scroll · t technical · Esc close", max(0, width - 2)),
                     curses.A_DIM)
        self.cursor_position = None

    def render_detail(self, rect: tuple[int, int, int, int]) -> None:
        y, x, height, width = rect
        x, width = x + 1, width - 1
        at_latest = (self.detail_latest is not None and
                     self.scrolls["detail"] == self.detail_latest[1])
        self.detail_available = max(0, height - 1)
        if height <= 0 or width <= 0:
            self.detail_total = 0
            self.scrolls["detail"] = 0
            return
        agent = self.detail and not self.detail.get("type") and self.detail.get("terminal_id")
        message_starts: list[int] = []
        # Building offscreen entries must not schedule age-only repaints.
        previous_deadline = self.next_age_refresh
        previous_candidates = self.detail_age_deadlines
        age_deadlines: dict[str, float] = {}
        self.detail_age_deadlines = age_deadlines
        try:
            entries = (self.thread_detail(width, message_starts=message_starts)
                       if not self.diagnostics and (self.detail or {}).get("type") else
                       self.agent_detail(width) if agent and not self.diagnostics else
                       [(line, 0) for line in self.detail_lines()])
        finally:
            self.next_age_refresh = previous_deadline
            self.detail_age_deadlines = previous_candidates
        lines = []
        latest_row = 0
        for index, (line, attribute) in enumerate(entries):
            if message_starts and index == message_starts[-1]:
                latest_row = len(lines)
            lines.extend((part, attribute) for logical in line.split("\n")
                         for part in ([logical] if agent and not self.diagnostics else
                                      wrap_display_text(logical, max(1, width))))
        self.detail_total = len(lines)
        latest = None
        if message_starts:
            latest = (self.detail["messages"][-1]["message_id"], self.clamp_detail(latest_row))
        # Keep the latest header anchored through arrivals and reflow. Readers
        # elsewhere retain their numeric position; diagnostics leave anchors pending.
        follow_latest = latest is not None and at_latest
        if not self.diagnostics and (self.detail_anchor_pending or follow_latest):
            self.scrolls["detail"] = latest[1] if latest else 0
        self.scrolls["detail"] = self.clamp_detail(self.scrolls["detail"])
        if not self.diagnostics:
            self.detail_anchor_pending = False
            self.detail_latest = latest
        first = self.scrolls["detail"]
        last = min(len(lines), first + self.detail_available)
        position = f"{first + 1}-{last}/{len(lines)}" if last > first else f"0-0/{len(lines)}"
        more = (" ↑more" if first else "") + (" ↓more" if last < len(lines) else "")
        toggle = f" · t technical:{'on' if self.detail_technical else 'off'}" if self.technical_supported() else ""
        notices = [
            f"Read-only · viewing does not ACK · {position}{more}{toggle}",
            f"Read-only/no ACK · {position}{more.replace('more', '')}" +
            (f" · t tech:{'on' if self.detail_technical else 'off'}" if toggle else ""),
            f"no ACK {position}{more.replace('more', '').replace(' ', '')}" +
            (f" t:{'on' if self.detail_technical else 'off'}" if toggle else "")]
        notice = next((text for text in notices if display_width(text) <= width), notices[-1])
        self.put(y, x, truncate_display_text(notice, width), curses.A_DIM)
        for row, (text, attr) in enumerate(lines[first:last], y + 1):
            visible = truncate_display_text(text, width)
            self.put(row, x, visible, attr)
            for suffix, deadline in age_deadlines.items():
                if suffix in visible:
                    self.next_age_refresh = min(self.next_age_refresh, deadline)
            if agent and not self.diagnostics and text.startswith("Turn state: "):
                self.put(row, x + len("Turn state: "), clip(text[len("Turn state: "):], width - len("Turn state: ")),
                         self.state_style(self.detail.get("turn_state") or "unknown",
                                          stale=bool(self.detail.get("stale"))))

    def render_input(self, rect: tuple[int, int, int, int]) -> None:
        direct = self.delivery == "direct"
        title = ("Reply to " if self.reply_to else "To ") + self.address(self.recipient)
        if direct:
            title = "DIRECT · " + title + " · may interrupt; acceptance ≠ native delivery"
        else:
            title = f"[{self.mail_kind.upper()} Ctrl+G] " + title
        y, x, height, width = self.box(rect, title, True, direct)
        x, width = x + 1, width - 1
        if height <= 0 or width <= 0:
            return
        if self.recipient and not self.recipient.get("extension_present") and height - int(bool(self.input_error)) > 1:
            self.put(y, x, truncate_display_text(
                "⚠ native receiver absent — message will wait in the mailbox", width), self.style("warning"))
            y, height = y + 1, height - 1
        if self.input_error and height > 1:
            self.put(y + height - 1, x, truncate_display_text(self.input_error, width), self.style("error"))
            height -= 1
        before = self.body[:self.body_cursor]
        logical = self.body.split("\n")
        logical_offsets = [0]
        for part in logical[:-1]:
            logical_offsets.append(logical_offsets[-1] + len(part) + 1)
        lines, row, column = wrap_message_lines(logical, width=max(1, width - 1),
                                               cursor_row=before.count("\n"),
                                               cursor_column=len(before.rsplit("\n", 1)[-1]))
        maximum = max(0, len(lines) - max(1, height))
        first = (max(0, row - max(1, height) + 1) if self.input_scroll is None else
                 min(maximum, self.input_scroll))
        self.add_hit(rect, "input", (first, maximum))
        visible = lines[first:first + max(0, height)]
        for offset, line in enumerate(visible):
            self.put(y + offset, x, truncate_display_text(line.text, width))
            offset_in_body = logical_offsets[line.logical_row] + line.start
            self.add_hit((y + offset, x, 1, width), "input-line", (line.text, offset_in_body))
        for offset in range(len(visible), max(0, height)):
            self.add_hit((y + offset, x, 1, width), "input-line", ("", len(self.body)))
        if height > 0 and self.mode == "input" and first <= row < first + height:
            self.cursor_position = y + row - first, x + min(width - 1, column)

    def settings_lines(self) -> list[str]:
        self.current_settings_scope()
        settings = self.local.get("settings") or {}
        if not self.matches_settings_scope(settings):
            settings = {}
        fields = settings.get("settings", {})
        picker = self.settings_picker
        if picker is not None:
            field = picker["field"]
            result = [self.setting_label(field) + " · " + ("Space/click toggles; Enter saves" if field == "members" else
                                      "Enter/click saves") +
                      (" · " + self.settings_results[field] if field in self.settings_results else "")]
            for index, (label, value) in enumerate(picker["entries"]):
                marker = ("☑" if value in picker["checked"] else "☐") if field == "members" else (
                    "●" if value == self.setting_value(field) else "○")
                result.append(("▌ " if index == picker["selection"] else "  ") + marker + " " + label)
            return result
        scope = self.current_settings_scope()
        result = [f"Target: repository {self.repo_id or '(select an agent first)'} · "
                  f"workspace {scope['workspace_id'] or 'unavailable'} @ {scope['host']} · role group · "
                  f"opener workspace: {scope['requested_workspace_id'] or 'unavailable'} · "
                  "Main/member changes save at the anchor."]
        for index, field in enumerate(self.SETTINGS):
            value = self.settings_pending.get(field, self.setting_value(field))
            if field in DEFAULT_THRESHOLDS:
                shown = f"◀ {value} ▶"
            elif field == "main":
                shown = self.address(value) if value else "(none)"
            elif field == "members":
                shown = ", ".join(self.address(target) for target in value or []) or "(none)"
            else:
                shown = str(value)
            numeric = self.settings_numeric
            if numeric is not None and numeric["field"] == field:
                shown = "> " + numeric["text"] + "  (digits only)"
            feedback = self.settings_results.get(field, "")
            if (self.settings_saving and self.settings_saving["field"] == field
                    and self.settings_saving["scope"] == scope and not feedback.startswith("Saving settings")):
                feedback = "Saving settings… · " + feedback
            result.append(("▌ " if index == self.settings_selection else "  ") + f"{self.setting_label(field)}: {shown}" +
                          (" · unsaved" if field in self.settings_pending else "") +
                          (" · " + feedback if feedback else ""))
        result.extend([f"home: {settings.get('home_host') or 'unset'}",
                       f"computed main: {json.dumps(settings.get('computed_main'), ensure_ascii=False)}",
                       f"propagation pending: {settings.get('propagation_pending', 0)}"])
        for field, data in fields.items():
            result.append(f"{field} v{data.get('version', '—')} @ {data.get('origin_host', '—')}")
        result.extend("⚠ CONFLICT " + json.dumps(conflict, ensure_ascii=False)
                      for conflict in settings.get("conflicts", []))
        return result

    def render_overlay(self, body: tuple[int, int, int, int]) -> None:
        body_y, body_x, height, width = body
        if self.mode == "help":
            context = ["INPUT: Enter send · Ctrl+G action/fyi · Shift+Enter / Alt+Enter newline · Esc keep draft · Ctrl+D direct · Ctrl+R refresh"
                       if self.help_return == "input" else " / ".join(self.footer_hints_for_nav())]
            lines = context + ["", "NAV: Tab / Shift+Tab, ←→, h/l change region",
                    "↑↓ / j/k select or scroll · PgUp/PgDn · Home/End",
                    "Agents Enter: quick send · Threads Enter: reply · Detail: read-only",
                    "Agents / Threads Space view: read-only modal; Enter inside does nothing.",
                    "Viewer: ↑↓ / PgUp/PgDn / Home/End / wheel scroll · t technical · Esc/q/Space close.",
                    "Viewer: outside click closes; inside click is inert. Live arrivals follow only at latest.",
                    "Detail: t toggles technical source; threads reopen latest, single items reopen top.",
                    "Detail PgUp/PgDn: viewport page with one row overlap; range + ↑/↓ show more.",
                    "s settings · ? / F1 help · e diagnostics · r refresh · q close",
                    "Threads: w withdraw · t retry · a/n uncertain claim recovery (evidence + confirm)",
                    "Thread states: . queued/pending · > submitting · + accepted/delivered · = handled",
                    "! failed/undeliverable · - withdrawn · ? UNCERTAIN delivering / unknown",
                    "Kinds: i fyi (cyan) · ! action (yellow) · > direct (user input, NOT native mail)",
                    "OUTBOX transport ops (cyan): > message · < receipt · # setting · - withdraw",
                    "States: yellow waiting/uncertain · green accepted/delivered · cyan handled · red failed",
                    "INPUT: printable characters are text (including q, ?, digits, :, /)",
                    "←→ Home/End Backspace/Delete edit · ↑↓ move between lines",
                    "Ctrl+J is ignored; use Shift+Enter or Alt+Enter for a newline.",
                    "Quick send and replies start as action; Ctrl+G toggles action/fyi for this compose.",
                    "Action: recipient must act/answer; wakes participating idle OMP promptly under safeguards.",
                    "FYI: batched idle OMP wake after ~5-10 min locally queued; mid-turn FYI arrives after that turn.",
                    "Existing wake safeguards apply to both kinds; FYI needs no reply unless it changes your work.",
                    "Mailbox sends immediately; direct input always asks for confirmation.",
                    "Inspection never acknowledges messages. Direct acceptance ≠ native delivery.",
                    "Receiver warning: runtime status on the recipient host; ask before runtime install omp/claude.",
                    "If already installed, restart only the selected sessions to register their receiver.",
                    "Mouse: click focus/select; click selected row to send/reply. Wheel scrolls.",
                    "Shift+drag selects terminal text. HAM_MAILBOX_NO_MOUSE=1 disables mouse.",
                    "Esc closes dialogs. Top-level Esc closes the quick-send overlay only."]
        elif self.mode == "settings":
            lines = self.settings_lines()
        elif self.mode == "confirm":
            assert self.confirm is not None
            lines = [self.confirm["title"], *self.confirm["lines"]]
        else:
            assert self.edit is not None
            lines = [self.edit["title"], *self.edit.get("target", [])]
        compact = self.mode in {"confirm", "evidence"}
        if compact:
            content_width = max((display_width(line) for line in lines), default=0)
            if self.mode == "evidence":
                content_width = max(content_width, display_width(self.edit["text"]) + 2)
            overlay_width = min(width - 4, max(content_width + 4, 40))
            columns = max(1, overlay_width - 4)
        else:
            overlay_width = min(width, 100)
            columns = max(1, overlay_width - 3)
        wrapped = []
        setting_rows = {}
        for index, line in enumerate(lines):
            parts = wrap_display_text(line, columns)
            count = len(self.settings_picker["entries"]) if self.settings_picker else len(self.SETTINGS)
            if self.mode == "settings" and 1 <= index <= count:
                for row in range(len(wrapped), len(wrapped) + len(parts)):
                    setting_rows[row] = index - 1
            wrapped.extend(parts)
        reserved = 3 if self.mode == "evidence" else 2 if compact else 0
        overlay_height = min(height, len(wrapped) + reserved + 2) if compact else min(height, 28)
        top = body_y + (height - overlay_height) // 2
        left = body_x + (width - overlay_width) // 2
        self.add_hit((top, left, overlay_height, overlay_width), "overlay")
        for row in range(top, top + overlay_height):
            self.put(row, left, " " * overlay_width)
        title = self.mode_word() if self.mode == "settings" else self.mode.upper()
        y, x, rows, columns = self.box((top, left, overlay_height, overlay_width), title, True)
        self.cursor_position = None
        if compact:
            x, columns = x + 1, columns - 2
        else:
            x, columns = x + 1, columns - 1
        if rows <= 0 or columns <= 0:
            return
        available = max(0, rows - reserved)
        start = self.overlay_scroll
        if self.mode == "settings" and not start:
            chosen = next((index for index, text in enumerate(wrapped) if text.startswith("▌")), 0)
            start = max(0, chosen + 1 - available)
        start = min(start, max(0, len(wrapped) - available))
        self.overlay_scroll = start
        for offset, text in enumerate(wrapped[start:start + max(0, rows - reserved)]):
            self.put(y + offset, x, truncate_display_text(text, columns),
                     curses.A_BOLD if text.startswith("▌") else 0)
            if start + offset in setting_rows:
                index = setting_rows[start + offset]
                target = "picker-entry" if self.settings_picker is not None else "setting"
                self.add_hit((y + offset, x, 1, columns), target, index)
                if target == "setting":
                    for arrow, direction in (("◀", -1), ("▶", 1)):
                        if arrow in text and self.SETTINGS[index] in DEFAULT_THRESHOLDS:
                            column = x + display_width(text[:text.index(arrow)])
                            self.add_hit((y + offset, column, 1, 1), "setting-step", (index, direction))
                    numeric = self.settings_numeric
                    if numeric and numeric["field"] == self.SETTINGS[index] and "> " in text:
                        column = display_width(text[:text.index("> ") + 2]) + numeric["cursor"]
                        self.cursor_position = y + offset, x + min(columns - 1, column)
        if self.mode in {"confirm", "evidence"}:
            choice = self.confirm["choice"] if self.mode == "confirm" else 0
            cancel, accept = ("[ Cancel ]", "Confirm") if not choice else ("Cancel", "[ Confirm ]")
            button_row = y + rows - (2 if self.mode == "evidence" else 1)
            if button_row >= y:
                self.put(button_row, x, truncate_display_text(cancel + "   " + accept, columns),
                         self.style("accent", curses.A_BOLD))
                self.add_hit((button_row, x, 1, min(columns, display_width(cancel))), "button", 0)
                button_x = x + display_width(cancel) + 3
                self.add_hit((button_row, button_x, 1,
                              min(display_width(accept), max(0, x + columns - button_x))), "button", 1)
        if self.mode == "evidence":
            assert self.edit is not None
            text, cursor = self.edit["text"], self.edit["cursor"]
            start = 0
            while display_width(text[start:cursor]) > max(0, columns - 3):
                start += 1
            self.put(y + rows - 1, x, truncate_display_text("> " + text[start:], columns))
            self.add_hit((y + rows - 1, x + 2, 1, columns - 2), "edit-text", (text[start:], start))
            self.cursor_position = y + rows - 1, x + min(columns - 1, 2 + display_width(text[start:cursor]))

    def overlay_key(self, key: Any) -> None:
        if key == curses.KEY_HOME:
            self.overlay_scroll = 0
        elif key == curses.KEY_END:
            self.overlay_scroll = 10**9
        elif key in (curses.KEY_UP, curses.KEY_DOWN, "j", "k", curses.KEY_PPAGE, curses.KEY_NPAGE):
            direction = -1 if key in (curses.KEY_UP, "k", curses.KEY_PPAGE) else 1
            amount = max(1, self.screen.getmaxyx()[0] - 8) if key in (curses.KEY_PPAGE, curses.KEY_NPAGE) else 1
            self.overlay_scroll = max(0, self.overlay_scroll + direction * amount)

    def footer_hints_for_nav(self) -> list[str]:
        if self.focus == "agents":
            return ["Agents: Space view; Enter opens quick send"]
        if self.focus == "threads":
            return ["Threads: Space view; Enter reply", "w withdraw", "t retry", "a/n recover uncertain claim"]
        return ["Detail: ↑↓ / PgUp/PgDn scroll; read-only, never ACK"] + (
            ["t technical details (opens latest message)"] if self.technical_supported() else [])

    def run(self) -> int:
        curses.nonl()
        if hasattr(curses, "set_escdelay"):
            curses.set_escdelay(25)
        self.screen.keypad(True)
        self.screen.timeout(100)
        self.initialize_colors()

        def read_key() -> Any:
            try:
                return self.screen.get_wch()
            except curses.error:
                return None

        decoder = key_input.KeyDecoder(read_key, curses.getmouse)
        try:
            if not self.environment.get("HAM_MAILBOX_NO_MOUSE"):
                try:
                    curses.mousemask(curses.ALL_MOUSE_EVENTS)
                    curses.mouseinterval(0)
                except curses.error:
                    pass
            with key_input.kitty_keyboard(enabled=not self.environment.get("HAM_MAILBOX_NO_KITTY")):
                while self.running:
                    self.tick()
                    if not self.running:
                        break
                    self.render_if_needed()
                    key = decoder.next()
                    if key is not None:
                        self.key(key)
        finally:
            try:
                curses.mousemask(0)
            except curses.error:
                pass
            for job in self.jobs.values():
                job.close()
            self.jobs.clear()
            if self.snapshot_worker is not None:
                self.snapshot_worker.close()
        return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="HAM mailbox: agents, threads and quick send.")
    parser.add_argument("command", choices=("split", "tab", "overlay", "pane", "overlay-pane", "job", "snapshot-worker"))
    arguments = parser.parse_args(argv)
    values = dict(os.environ)
    try:
        if arguments.command == "snapshot-worker":
            return snapshot_worker(values)
        if arguments.command in {"split", "tab"}:
            print(json.dumps(launch(arguments.command, values), ensure_ascii=False))
            return 0
        if arguments.command == "overlay":
            opener_id = focused_pane_id(values)
            if not opener_id:
                raise LedgerError("opener_unavailable", "Open HAM from a live workspace pane (an agent is not required).")
            opener = pane_record(opener_id, values)
            store = LedgerStore(environment=values)
            cwd = str(opener.get("cwd") or values.get("PWD") or os.getcwd())
            repo_id = repository_identity(cwd, store.host)["repo_id"]
            return 0 if agent_messenger.launch_plugin_overlay(
                "mailbox-popup", environment=values,
                extra_arguments=["--env", f"{REPO_ENV}={repo_id}",
                                 "--env", f"{OPENER_PANE}={opener_id}",
                                 "--env", f"{OPENER_TERMINAL}={opener['terminal_id']}"]) else 1
        if arguments.command == "job":
            result = execute_job(json.load(sys.stdin), values)
            print(json.dumps(result, ensure_ascii=False))
            return 0
        if not sys.stdin.isatty() or not sys.stdout.isatty():
            raise LedgerError("interactive_required", "Mailbox pane requires a terminal.")
        try:
            locale.setlocale(locale.LC_ALL, "")
        except locale.Error:
            pass
        with terminal_flow_control_disabled(sys.stdin):
            result = curses.wrapper(lambda screen: MailboxApp(
                screen, values, quick_send=arguments.command == "overlay-pane").run())
        if arguments.command != "overlay-pane":
            close_owned(values)
        return result
    except (LedgerError, OSError, ValueError) as error:
        print(json.dumps({"error": {"code": getattr(error, "code", "mailbox_ui_failed"), "message": str(error)}}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
