#!/usr/bin/env python3
"""Plugin-owned, code-only mailbox board and explicit operator controls.

All durable mailbox mutations belong to the ledger services. UI state contains
only live plugin-pane identities; snapshots and subprocess results are ephemeral.
"""
from __future__ import annotations

import argparse
import curses
import fcntl
import json
import locale
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
from typing import Any, Mapping, Sequence

from agent_directory import (dispatch_user_prompts, herdr_executable, query_local_agents,
                             query_remote_agents, ssh_config_path, user_prompt_text)
from agent_messenger import focused_pane_id, terminal_flow_control_disabled
from ledger_mail import Mailbox
from ledger_remote import RemoteLedger
from ledger_settings import DEFAULT_THRESHOLDS, RepoSettings
from ledger_status import AgentRegistry, repository_identity
from ledger_store import LedgerError, LedgerStore, ledger_directory
from terminal_text import (character_index_at_display_column, display_width,
                           wrap_display_text, wrap_message_lines)

PLUGIN = "herdr.agent-messenger"
OPENER_PANE = "HERDR_HAM_OPENER_PANE_ID"
OPENER_TERMINAL = "HERDR_HAM_OPENER_TERMINAL_ID"
REPO_ENV = "HERDR_HAM_REPO_ID"
LOCAL_INTERVAL = 1.0
REMOTE_INTERVAL = 30.0
JOB_TIMEOUT = 25.0


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
        if isinstance(saved, dict):
            try:
                live = pane_record(saved["pane_id"], values)
                if live["terminal_id"] == saved["terminal_id"]:
                    herdr(["plugin", "pane", "focus", saved["pane_id"]], values)
                    return {"focused": True, **saved}
            except (LedgerError, KeyError):
                pass
        store = LedgerStore(environment=values)
        cwd = str(opener.get("cwd") or values.get("PWD") or os.getcwd())
        repo_id = repository_identity(cwd, store.host)["repo_id"]
        arguments = ["plugin", "pane", "open", "--plugin", PLUGIN, "--entrypoint", "mailbox",
                     "--placement", placement,
                     "--cwd", str(Path(__file__).resolve().parent), "--focus"]
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
        return {"focused": False, **saved}


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


def local_snapshot(store: LedgerStore, repo_id: str | None,
                   environment: Mapping[str, str] | None = None) -> dict[str, Any]:
    settings = RepoSettings(store)
    agents = AgentRegistry(store).list(repo_id)
    with store.read() as connection:
        unread = {(row["recipient_host"], row["recipient_terminal_id"]): row["n"] for row in connection.execute(
            "SELECT recipient_host,recipient_terminal_id,count(*) n FROM deliveries d JOIN messages m USING(message_id) "
            "WHERE d.state IN ('queued','delivering')" + (" AND m.repo_id=?" if repo_id else "") +
            " GROUP BY recipient_host,recipient_terminal_id", (repo_id,) if repo_id else ())}
    for agent in agents:
        agent["role"] = settings.role(agent)
        agent["unread"] = unread.get((agent["host"], agent["terminal_id"]), 0)
    if environment is not None:
        try:
            result = herdr(["api", "snapshot"], environment)
            panes = result.get("snapshot", result).get("panes", [])
            labels = {pane["terminal_id"]: pane.get("label") or "" for pane in panes}
            for agent in agents:
                current = labels.get(agent["terminal_id"], "")
                agent["user_border"] = bool(current and current != agent.get("last_ham_label"))
        except LedgerError:
            # The ledger remains inspectable while Herdr is temporarily absent.
            pass
    mail = Mailbox(store)
    return {"host": store.host, "agents": agents, "threads": mail.list_threads(repo_id)["threads"],
            "submissions": mail.list_direct(repo_id)["submissions"],
            "settings": settings.show(repo_id) if repo_id else None,
            "peers": RemoteLedger(store).peers(), "outbox": RemoteLedger(store).outbox()}


def dispatch_direct(store: LedgerStore, recipient: Mapping[str, Any], body: str,
                    environment: Mapping[str, str], *, subject: str = "") -> dict[str, Any]:
    """Persist before transport; reuse the existing verified prompt transport.

    No AgentRecord is invented for the user. Terminal transport acceptance is
    deliberately independent of native mailbox delivery/handling.
    """
    prompt = user_prompt_text(body)
    mailbox = Mailbox(store, environment)
    record = mailbox.begin_direct(recipient["repo_id"], recipient, body=prompt, subject=subject)
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
        from ledger_worker import ensure_worker
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
        return RemoteLedger(store, environment).query_status(request["host"], repo_id)
    if operation == "send":
        return mail.send_user(repo_id, request["recipient"], body=request["body"], kind=request["kind"],
                              subject=request.get("subject", ""), reply_to=request.get("reply_to"))
    if operation == "direct":
        return dispatch_direct(store, request["recipient"], request["body"], environment,
                               subject=request.get("subject", ""))
    if operation == "setting":
        result = RepoSettings(store).mutate(repo_id, request["field"], request["value"])
        from ledger_display import LedgerDisplay
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
        self.process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "job"],
                                        env=dict(environment), stdin=self.input, stdout=self.output,
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
    if key in ("\n", "\r", curses.KEY_ENTER):
        return (text[:cursor] + "\n" + text[cursor:], cursor + 1, True) if multiline else (text, cursor, True)
    if isinstance(key, str) and key.isprintable():
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


class MailboxApp:
    """Four keyboard-first screens; every potentially blocking action is a Job."""
    def __init__(self, screen: Any, environment: Mapping[str, str], *, job_factory=Job, clock=time.monotonic):
        self.screen = screen
        self.environment = dict(environment)
        self.repo_id = self.environment.get(REPO_ENV) or None
        self.store = LedgerStore(environment=environment)
        self.job_factory, self.clock = job_factory, clock
        self.jobs: dict[str, Any] = {}
        self.local: dict[str, Any] = {"agents": [], "threads": [], "submissions": [], "peers": [], "outbox": []}
        self.remote: dict[str, Any] = {}
        self.remote_errors: dict[str, str] = {}
        self.remote_due: dict[str, float] = {}
        self.next_bell = 0.0
        self.bell_stamp: int | None = None
        self.tab, self.selection, self.scroll = 0, 0, 0
        self.status = "Loading local ledger…"
        self.body, self.subject = "", ""
        self.body_cursor = self.subject_cursor = 0
        self.cursor_position: tuple[int, int] | None = None
        self.kind, self.delivery = "fyi", "mailbox"
        self.compose_field = 0
        self.recipient: dict[str, Any] | None = None
        self.reply_to: str | None = None
        self.edit: dict[str, Any] | None = None
        self.detail: dict[str, Any] | None = None
        self.confirm: str | None = None
        self.running = True
        self.draft_version = 0
        self.submitted_draft_version: int | None = None
        self.follow_selection = True

    def start_job(self, key: str, operation: str, **values: Any) -> bool:
        if key in self.jobs:
            if key == "mutation":
                self.status = "An operation is pending; this input has not been submitted."
            return False
        self.jobs[key] = self.job_factory({"operation": operation, "repo_id": self.repo_id, **values}, self.environment)
        if key == "mutation" and operation == "setting":
            self.status = "Saving settings… navigation and q remain available."
        return True

    def tick(self) -> None:
        now = self.clock()
        for key, job in list(self.jobs.items()):
            if self.jobs.get(key) is not job:
                continue
            done, value = job.poll()
            if not done:
                continue
            job.close()
            del self.jobs[key]
            error = value.get("error") if isinstance(value, dict) else None
            if key.startswith("remote:"):
                host = key.split(":", 1)[1]
                if error:
                    self.remote_errors[host] = str(error.get("message", error))
                else:
                    self.remote[host] = value
                    self.remote_errors.pop(host, None)
                    self.refresh_detail()
            elif key == "snapshot":
                if error:
                    self.status = f"Local ledger: {error.get('code')}: {error.get('message')}"
                else:
                    self.local = value
                    self.refresh_detail()
                    mutation = self.jobs.get("mutation")
                    if mutation is None or mutation.operation != "setting":
                        self.status = "Local ledger updated. Read-only inspection does not acknowledge messages."
            else:
                self.status = (f"{error.get('code')}: {error.get('message')}" if error else
                               json.dumps(value, ensure_ascii=False))
                if not error and job.operation == "setting":
                    # A snapshot started before this commit cannot replace the
                    # authoritative result used by the next settings key.
                    snapshot = self.jobs.pop("snapshot", None)
                    if snapshot is not None:
                        snapshot.close()
                    self.local["settings"] = value
                if (not error and job.operation in {"send", "direct"}
                        and self.submitted_draft_version == self.draft_version
                        and value.get("acceptance") != "failed"):
                    self.body, self.body_cursor = "", 0
                    self.draft_version += 1
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
        # This schedule is deliberately independent from local bell activity.
        for peer in self.local.get("peers", []):
            host = peer["canonical_name"]
            if now >= self.remote_due.get(host, 0):
                self.remote_due[host] = now + REMOTE_INTERVAL
                self.start_job(f"remote:{host}", "remote", host=host)

    def refresh_detail(self) -> None:
        if not self.detail:
            return
        item = self.detail
        kind = item.get("type")
        if kind in {"thread", "direct", "outbox"}:
            collection, identity = {"thread": ("threads", "thread_id"),
                                    "direct": ("submissions", "submission_id"),
                                    "outbox": ("outbox", "item_id")}[kind]
            replacement = next((entry for entry in self.local.get(collection, [])
                                if entry[identity] == item[identity]), None)
            self.detail = {**replacement, "type": kind} if replacement else None
        elif "agent_id" in item:
            self.detail = next((agent for agent in self.agents() if agent["agent_id"] == item["agent_id"]), None)

    def agents(self) -> list[dict[str, Any]]:
        values = list(self.local["agents"])
        for host, snapshot in self.remote.items():
            for agent in snapshot.get("agents", []):
                values.append({**agent, "stale": host in self.remote_errors,
                               "unread": "?" if host in self.remote_errors else agent.get("unread", "?")})
        return sorted(values, key=lambda agent: (agent["host"], agent.get("alias") or "", agent["terminal_id"]))

    def items(self) -> list[Any]:
        if self.tab == 0:
            return self.agents()
        if self.tab == 1:
            return ([{"type": "thread", **thread} for thread in self.local["threads"]] +
                    [{"type": "direct", **record} for record in self.local["submissions"]] +
                    [{"type": "outbox", **record} for record in self.local.get("outbox", []) if record["state"] in {"pending", "failed"}])
        if self.tab == 3:
            return ["mode", "main", "members", *DEFAULT_THRESHOLDS]
        return []

    def selected(self) -> Any:
        items = self.items()
        self.selection = min(max(0, self.selection), max(0, len(items) - 1))
        return items[self.selection] if items else None

    def put(self, row: int, text: str, attribute: int = 0) -> None:
        height, width = self.screen.getmaxyx()
        if 0 <= row < height and width > 1:
            try:
                self.screen.addstr(row, 0, clip(text, width - 1), attribute)
            except curses.error:
                pass

    def render(self) -> None:
        self.screen.erase()
        self.cursor_position = None
        height, width = self.screen.getmaxyx()
        self.put(0, "HAM  1 Status  2 Threads  3 Compose  4 Settings", curses.A_BOLD)
        self.put(1, f"[{('STATUS', 'THREADS', 'COMPOSE', 'SETTINGS')[self.tab]}] repo {self.repo_id or '(all; select an agent)'}", curses.A_REVERSE)
        if height < 7 or width < 20:
            self.put(2, "Resize terminal; q closes.")
        elif self.edit:
            self.render_edit(height, width)
        elif self.detail:
            self.render_detail(height, width)
        elif self.tab == 2:
            self.render_compose(height, width)
        else:
            self.render_list(height, width)
        footer = "Esc back  q close  1–4 screen  ↑↓ select  Enter inspect  c compose  r refresh  ? errors"
        if self.tab == 1:
            footer = "Enter inspect  a applied / n not-applied (claim)  w withdraw  t retry outbox"
        elif self.tab == 2:
            footer = "Tab field  ^K kind  ^D mailbox/direct  ^S review/send  Esc back"
        elif self.tab == 3:
            footer = "Enter edit  m use selected board agent as main  Space toggle manual member"
        if self.edit:
            footer = "Enter confirm  Esc cancel  ←→ move  Home/End  Backspace/Delete edit"
        self.put(height - 2, self.status)
        self.put(height - 1, footer, curses.A_DIM)
        if self.cursor_position is not None:
            try:
                self.screen.move(*self.cursor_position)
                curses.curs_set(1)
            except (curses.error, AttributeError):
                pass
        else:
            try:
                curses.curs_set(0)
            except curses.error:
                pass
        self.screen.refresh()

    def render_list(self, height: int, width: int) -> None:
        items = self.items()
        self.selected()
        lines: list[tuple[str, int]] = []
        if self.tab == 0:
            lines.append(("HOST / ALIAS   ROLE   TURN / PHASE   UNREAD   RECEIVER", -1))
            for index, agent in enumerate(items):
                absent = "extension absent" if not agent.get("extension_present") else "receiver ready"
                stale = " STALE/offline" if agent.get("stale") else ""
                lines.append((f"{agent['host']}/{agent.get('alias') or agent['terminal_id']}  {agent.get('role') or '—'}  "
                              f"{agent.get('turn_state', 'unknown')}/{agent.get('phase') or '—'}  "
                              f"unread:{agent.get('unread', '?')}  {absent}{stale}"
                              f"{'  user border preserved' if agent.get('user_border') else ''}", index))
            for host, error in self.remote_errors.items():
                lines.append((f"! {host}: disconnected — {error}", -1))
        elif self.tab == 1:
            for index, item in enumerate(items):
                if item["type"] == "thread":
                    states = "; ".join(describe_message(message) for message in item["messages"])
                    line = f"{item['subject'] or '(no subject)'}  {states}  [{item['thread_id'][:8]}]"
                elif item["type"] == "direct":
                    target = item["target"]
                    address = target.get("recipient_address") or f"{target['host']}/{target.get('alias') or target['terminal_id']}"
                    line = f"USER DIRECT {item['acceptance']}  {address}  [{item['submission_id'][:8]}]"
                else:
                    line = f"OUTBOX {item['state']} {item['kind']} → {item['target_host']}  {item.get('last_error') or ''}"
                lines.append((line, index))
        else:
            settings = self.local.get("settings") or {}
            fields = settings.get("settings", {})
            for index, field in enumerate(items):
                value = settings.get("mode", "off") if field == "mode" else fields.get(field, {}).get("value")
                if field in DEFAULT_THRESHOLDS:
                    value = fields.get("thresholds", {}).get("value", DEFAULT_THRESHOLDS).get(field)
                lines.append((f"{field}: {json.dumps(value, ensure_ascii=False)}", index))
            lines.append((f"home: {settings.get('home_host') or 'unset'}; propagation pending: {settings.get('propagation_pending', 0)}", -1))
            for field, data in fields.items():
                lines.append((f"{field} v{data['version']} @ {data['origin_host']}", -1))
            for conflict in settings.get("conflicts", []):
                for line in wrap_display_text("! CONFLICT " + json.dumps(conflict, ensure_ascii=False), width - 3):
                    lines.append((line, -1))
        if not lines:
            lines = [("No entries. No message has been acknowledged by opening this view.", -1)]
        available = height - 5
        chosen_line = next((index for index, (_line, item) in enumerate(lines) if item == self.selection), 0)
        if self.follow_selection:
            self.scroll = max(0, min(self.scroll, chosen_line))
            if chosen_line >= self.scroll + available:
                self.scroll = chosen_line - available + 1
        self.scroll = min(max(0, self.scroll), max(0, len(lines) - available))
        for row, (line, item) in enumerate(lines[self.scroll:self.scroll + available], 3):
            self.put(row, ("› " if item == self.selection else "  ") + line,
                     curses.A_REVERSE if item == self.selection else 0)

    def render_compose(self, height: int, width: int) -> None:
        target = self.recipient
        self.put(3, f"USER → {target['host']}/{target.get('alias') or target['terminal_id']}" if target else "Select a status-board agent, then c to compose.", curses.A_BOLD)
        self.put(4, f"{self.delivery.upper()} · {self.kind} · sender=user; direct never means native delivered")
        subject_start = 0
        while display_width(self.subject[subject_start:self.subject_cursor]) > max(1, width - 11):
            subject_start += 1
        self.put(5, "Subject: " + self.subject[subject_start:], curses.A_REVERSE if self.compose_field == 0 else 0)
        self.put(6, "Body (Enter=new line):", curses.A_REVERSE if self.compose_field == 1 else 0)
        before = self.body[:self.body_cursor]
        logical_row, logical_column = before.count("\n"), len(before.rsplit("\n", 1)[-1])
        lines, cursor_row, cursor_column = wrap_message_lines(
            self.body.split("\n"), width=max(1, width - 2),
            cursor_row=logical_row, cursor_column=logical_column)
        available = max(1, height - 10)
        first = max(0, cursor_row - available + 1)
        for row, line in enumerate(lines[first:first + available], 7):
            self.put(row, " " + line.text)
        if self.compose_field:
            self.cursor_position = (min(height - 4, 7 + cursor_row - first), min(width - 2, 1 + cursor_column))
        else:
            self.cursor_position = (5, min(width - 2, 9 + display_width(self.subject[subject_start:self.subject_cursor])))
        if self.confirm:
            self.put(height - 3, self.confirm, curses.A_BOLD)

    def detail_lines(self) -> list[str]:
        item = self.detail or {}
        if item.get("type") == "thread":
            result = [f"Thread {item['thread_id']}  {item['subject']}"]
            for message in item["messages"]:
                result.extend(["", f"{message['sender_label']} → {message['recipient_address']}", describe_message(message),
                               f"id:{message['message_id']}  via:{message.get('via') or '—'}",
                               f"claim:{message.get('claim_id') or '—'} agent:{message.get('claimed_agent_id') or '—'} "
                               f"generation:{message.get('claimed_generation')}",
                               f"confirmation source:{message.get('confirmation_source') or '—'}",
                               f"evidence:{message.get('evidence') or '—'}", message["body"]])
            result.append("a/n resolves the first uncertain claim in this thread; inspect its ID/evidence first.")
            return result
        return [json.dumps(item, ensure_ascii=False, indent=2)]

    def render_detail(self, height: int, width: int) -> None:
        lines = [part for line in self.detail_lines() for logical in line.split("\n")
                 for part in wrap_display_text(logical, width - 1)]
        self.scroll = min(max(0, self.scroll), max(0, len(lines) - (height - 5)))
        for row, line in enumerate(lines[self.scroll:self.scroll + height - 5], 3):
            self.put(row, line)

    def render_edit(self, height: int, width: int) -> None:
        assert self.edit is not None
        self.put(3, self.edit["title"], curses.A_BOLD)
        for row, line in enumerate(wrap_display_text(self.edit.get("warning", ""), width - 1), 4):
            if row >= height - 4:
                break
            self.put(row, line)
        visible = self.edit["text"]
        cursor = self.edit["cursor"]
        prefix = visible[:cursor]
        while display_width(prefix) > width - 4:
            visible, prefix = visible[1:], prefix[1:]
        self.put(height - 3, "> " + visible, curses.A_REVERSE)

    def choose_agent(self) -> None:
        agent = self.selected()
        if self.tab == 0 and agent:
            if agent.get("stale"):
                self.status = "Remote snapshot is stale; refresh before selecting a recipient."
                return
            self.recipient = dict(agent)
            self.repo_id = agent["repo_id"]
            self.reply_to = None
            self.draft_version += 1
            self.tab, self.selection, self.scroll = 2, 0, 0
            self.start_job("snapshot", "snapshot")

    def open_edit(self, title: str, value: str, action: str, *, warning: str = "", **extra: Any) -> None:
        self.edit = {"title": title, "text": value, "cursor": len(value), "action": action,
                     "warning": warning, **extra}

    def edit_key(self, key: Any) -> None:
        edit = self.edit
        assert edit is not None
        if key == "\x1b":
            self.edit = None
            return
        if key in ("\n", "\r", curses.KEY_ENTER):
            if "mutation" in self.jobs:
                self.status = "An operation is pending; keep this evidence/edit until it finishes."
                return
            try:
                if edit["action"] == "resolve":
                    if not edit["text"].strip():
                        raise ValueError("Evidence is required; this is a manual confirmation, not runtime-journal proof.")
                    claim = {**edit["claim"], "evidence": edit["text"].strip()}
                    self.start_job("mutation", "resolve", claim=claim)
                else:
                    field = edit["field"]
                    value = json.loads(edit["text"]) if field in {"main", "members"} else edit["text"].strip()
                    if field in DEFAULT_THRESHOLDS:
                        value = int(value)
                        thresholds = dict((self.local.get("settings") or {}).get("settings", {}).get("thresholds", {}).get("value", DEFAULT_THRESHOLDS))
                        thresholds[field] = value
                        field, value = "thresholds", thresholds
                    self.start_job("mutation", "setting", field=field, value=value)
                self.edit = None
            except (ValueError, TypeError) as error:
                self.status = str(error)
            return
        text, cursor = edit["text"], edit["cursor"]
        if key == curses.KEY_LEFT:
            cursor = max(0, cursor - 1)
        elif key == curses.KEY_RIGHT:
            cursor = min(len(text), cursor + 1)
        elif key == curses.KEY_HOME:
            cursor = 0
        elif key == curses.KEY_END:
            cursor = len(text)
        elif key in (curses.KEY_BACKSPACE, "\x7f", "\b") and cursor:
            text, cursor = text[:cursor - 1] + text[cursor:], cursor - 1
        elif key == curses.KEY_DC:
            text = text[:cursor] + text[cursor + 1:]
        elif isinstance(key, str) and key.isprintable():
            text, cursor = text[:cursor] + key + text[cursor:], cursor + len(key)
        edit["text"], edit["cursor"] = text, cursor

    def resolve(self, disposition: str) -> None:
        item = self.detail or self.selected()
        if not item or item.get("type") != "thread":
            self.status = "Select a thread containing an uncertain native claim."
            return
        message = next((message for message in item["messages"] if message["state"] == "delivering"), None)
        if message is None:
            self.status = "This thread has no uncertain native claim."
            return
        self.open_edit(f"USER CONFIRM {disposition}: {message['claim_id']}", "", "resolve",
                       warning="Manual operator evidence, NOT runtime-journal confirmation or handled/provider understanding. "
                               f"Claimed agent {message['claimed_agent_id']}, generation {message['claimed_generation']}. "
                               "Applied records that OLD claim; not-applied releases it without automatic replay. "
                               "Enter confirms only with evidence; Esc cancels.",
                       claim={"claim_id": message["claim_id"], "agent_id": message["claimed_agent_id"],
                              "generation": message["claimed_generation"], "disposition": disposition})

    def settings_key(self, key: Any) -> None:
        settings = self.local.get("settings") or {}
        if not self.repo_id:
            self.status = "Select an agent on Status to choose a repository first."
            return
        field = self.selected()
        if key in ("\n", "\r", curses.KEY_ENTER) and field:
            fields = settings.get("settings", {})
            value = settings.get("mode", "off") if field == "mode" else fields.get(field, {}).get("value")
            warning = "mode: off | manual | auto. Changes are local settings with durable peer propagation."
            if field in DEFAULT_THRESHOLDS:
                value = fields.get("thresholds", {}).get("value", DEFAULT_THRESHOLDS)[field]
                warning = "Integer 0–10000; thresholds are changed as one versioned field."
            elif field in {"main", "members"}:
                value = json.dumps(value if value is not None else ([] if field == "members" else None), ensure_ascii=False)
                warning = 'JSON target {"host":"canonical","terminal_id":"term_…"}; main null restores automatic/no designation. members is a target array.'
            self.open_edit(f"Edit {field}", str(value), "setting", field=field, warning=warning)
        elif key in ("m", " "):
            if self.recipient is None or self.recipient["repo_id"] != self.repo_id:
                self.status = "Choose a board agent with c first, then return to Settings."
                return
            target = {key: self.recipient[key] for key in ("host", "terminal_id")}
            if key == "m":
                self.start_job("mutation", "setting", field="main", value=target)
            else:
                members = list(settings.get("settings", {}).get("members", {}).get("value", []))
                members.remove(target) if target in members else members.append(target)
                self.start_job("mutation", "setting", field="members", value=members)

    def key(self, key: Any) -> None:
        if self.edit:
            self.edit_key(key)
            return
        if key == curses.KEY_RESIZE:
            return
        if key == "\x1b":
            if self.confirm:
                self.confirm = None
            elif self.detail:
                self.detail, self.scroll = None, 0
            else:
                self.tab = 0
            return
        # Text entry owns ordinary printable keys (including q and digits).
        if self.tab == 2 and not self.detail:
            if key == "\x13":
                if "mutation" in self.jobs:
                    self.status = "An operation is already pending; inspect its result before sending again."
                elif not self.recipient or not self.body.strip():
                    self.status = "Choose a recipient and enter a message body."
                elif not self.confirm:
                    self.confirm = f"Ctrl+S again: USER {'DIRECT INPUT (may interrupt)' if self.delivery == 'direct' else self.kind + ' mailbox'} → {self.recipient['host']}/{self.recipient.get('alias')}"
                else:
                    self.submitted_draft_version = self.draft_version
                    self.start_job("mutation", "direct" if self.delivery == "direct" else "send",
                                   recipient=self.recipient, body=self.body, kind=self.kind,
                                   subject=self.subject, reply_to=self.reply_to)
                    self.confirm = None
                return
            if key == "\t":
                self.compose_field = 1 - self.compose_field
                return
            if key == "\x0b":
                self.kind = "action" if self.kind == "fyi" else "fyi"
                self.draft_version += 1
                self.confirm = None
                return
            if key == "\x04":
                self.delivery = "direct" if self.delivery == "mailbox" else "mailbox"
                self.draft_version += 1
                self.confirm = None
                return
            field = "body" if self.compose_field else "subject"
            text, cursor, handled = edit_buffer(getattr(self, field), getattr(self, field + "_cursor"), key,
                                                multiline=bool(self.compose_field))
            if handled:
                setattr(self, field, text)
                self.draft_version += 1
                setattr(self, field + "_cursor", cursor)
                self.confirm = None
                return
        if key == "q":
            self.running = False
        elif key in ("1", "2", "3", "4"):
            self.tab, self.selection, self.scroll, self.detail = int(key) - 1, 0, 0, None
            self.follow_selection = True
        elif key in (curses.KEY_UP, curses.KEY_DOWN):
            amount = -1 if key == curses.KEY_UP else 1
            if self.detail:
                self.scroll = max(0, self.scroll + amount)
            else:
                self.follow_selection = True
                self.selection += amount
                self.selected()
        elif key in (curses.KEY_NPAGE, curses.KEY_PPAGE):
            self.follow_selection = False
            self.scroll = max(0, self.scroll + (1 if key == curses.KEY_NPAGE else -1) * max(1, self.screen.getmaxyx()[0] - 5))
        elif key == "c" and self.tab == 0:
            self.choose_agent()
        elif key == "r":
            self.start_job("snapshot", "snapshot")
            self.remote_due.clear()
        elif key == "?":
            self.detail = {"status": self.status, "connection_errors": self.remote_errors,
                           "pending_operations": list(self.jobs), "repo_id": self.repo_id}
            self.scroll = 0
        elif self.tab == 3:
            self.settings_key(key)
        elif self.tab == 1:
            if key in ("a", "n"):
                self.resolve("applied" if key == "a" else "not_applied")
            elif key in ("\n", "\r", curses.KEY_ENTER):
                self.detail, self.scroll = self.selected(), 0
            elif key == "w":
                item = self.detail or self.selected()
                if item and item.get("type") == "thread":
                    message = next((message for message in item["messages"] if message["origin_host"] == self.store.host and message["state"] in {"pending", "queued"}), None)
                    if message:
                        self.start_job("mutation", "withdraw", message_id=message["message_id"])
            elif key == "t":
                item = self.detail or self.selected()
                if item and item.get("type") == "outbox":
                    self.start_job("mutation", "retry", item_id=item["item_id"])
        elif self.tab == 0 and key in ("\n", "\r", curses.KEY_ENTER):
            self.detail, self.scroll = self.selected(), 0

    def run(self) -> int:
        self.screen.keypad(True)
        self.screen.timeout(100)
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        try:
            while self.running:
                self.tick()
                self.render()
                try:
                    self.key(self.screen.get_wch())
                except curses.error:
                    pass
        finally:
            for job in self.jobs.values():
                job.close()
            self.jobs.clear()
        return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="HAM mailbox: code-only status, threads, user compose and settings.")
    parser.add_argument("command", choices=("split", "tab", "pane", "job"))
    arguments = parser.parse_args(argv)
    values = dict(os.environ)
    try:
        if arguments.command in {"split", "tab"}:
            print(json.dumps(launch(arguments.command, values), ensure_ascii=False))
            return 0
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
            result = curses.wrapper(lambda screen: MailboxApp(screen, values).run())
        close_owned(values)
        return result
    except (LedgerError, OSError, ValueError) as error:
        print(json.dumps({"error": {"code": getattr(error, "code", "mailbox_ui_failed"), "message": str(error)}}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
