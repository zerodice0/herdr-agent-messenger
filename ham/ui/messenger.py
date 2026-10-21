#!/usr/bin/env python3
"""Keyboard-first overlay for sending prompts to local and SSH-hosted agents."""

from __future__ import annotations

from ham.paths import PLUGIN_ROOT

import argparse
from contextlib import contextmanager
import curses
from dataclasses import dataclass
import json
import locale
import os
from pathlib import Path
import queue
import shlex
import subprocess
import sys

try:
    import termios
except ImportError:  # pragma: no cover - the plugin currently targets macOS/Linux.
    termios = None  # type: ignore[assignment]
import threading
import time
from typing import Any, Iterator, Mapping, Sequence, TextIO

from ham.agents.directory import (
    AgentCache,
    AgentRecord,
    ProbeResult,
    RemoteDiscovery,
    SendResult,
    dispatch_prompts,
    fetch_local_agent,
    herdr_executable,
    query_local_agents,
    ssh_config_path,
    ssh_hosts,
)
from ham.agents.route import encode_agent_route
from ham.agents.output import agent_address
from ham.ui.messenger_i18n import detect_language, messages
from ham.ui.messenger_render import (
    DELIVERY_DELEGATE,
    DELIVERY_DIRECT,
    MessengerRenderMixin,
    _help_hints,
)
from ham.ui.recipient_view import (
    STATUS_ORDER,
    RecipientViewRow,
    build_recipient_view_rows,
    filter_and_sort_agents,
    format_recipient_line,
    group_header_parts,
    recipient_host_key,
    recipient_workspace_key,
    sticky_headers_before,
    visible_recipient_rows,
    workspace_display,
)
from ham.ui.terminal_text import (
    WrappedMessageLine,
    character_index_at_display_column,
    display_width,
    truncate_display_text,
    wrap_display_text,
    wrap_message_lines,
)
from ham.ui.tui_style import Theme, content_rect, draw_empty, draw_footer, draw_status, draw_title, footer_lines


PLUGIN_ID = "herdr.agent-messenger"
MESSENGER_ENTRYPOINT = "messenger"
HELD_ENTRYPOINT = "held"
SKILL_INSTALLER_ENTRYPOINT = "skill-installer"
SKILL_PROJECT_ROOT_ENV = "HERDR_AGENT_SKILL_PROJECT_ROOT"
SKILL_RELATIVE_PATH = Path(".agents/skills/herdr-agent-messenger/SKILL.md")
SENDER_PANE_ENV = "HERDR_AGENT_MESSENGER_SENDER_PANE_ID"
SKILL_GUIDE_KEY = "\x07"
REMOTE_DETAILS_KEY = "\x15"


@dataclass(frozen=True)
class SendJobResult:
    sender_available: bool
    results: tuple[SendResult, ...]
    error: str = ""
    cancelled: bool = False


def _single_line(value: str) -> str:
    return " ".join(value.split())


def bundled_router_command() -> str:
    launcher = bundled_skill_path().parent / "scripts" / "herdr_agent_messenger.py"
    return shlex.join(("python3", str(launcher)))


def _orchestration_target_descriptor(
    recipient: AgentRecord,
    *,
    index: int | None = None,
) -> str:
    workspace = _single_line(recipient.workspace_label) or "unknown workspace"
    heading = "Target" if index is None else f"Target {index}"
    selector = (
        f"--address {_single_line(agent_address(recipient))}"
        if recipient.name
        else f"--route {encode_agent_route(recipient)} (unlabeled target only)"
    )
    return f"{heading}: {selector}; workspace: {workspace}; status: {recipient.status}"


def build_orchestration_request(
    recipients: Sequence[AgentRecord],
    original_request: str,
) -> str:
    """Ask the coordinator to do semantic decomposition and worker orchestration."""

    router = bundled_router_command()
    single_target = len(recipients) == 1
    descriptors = "\n".join(
        _orchestration_target_descriptor(
            recipient,
            index=None if single_target else index,
        )
        for index, recipient in enumerate(recipients, start=1)
    )
    header = "single-target" if single_target else "multi-target"
    shared = (
        f"Agent Messenger {header} request\n"
        "You coordinate; targets have not received this task.\n"
        f"{descriptors}\n\n"
        "Use bundled commands; no CLI research/install.\n"
        "Use listed --address; unlabeled targets use --route (batch: route).\n"
        "Output is compact JSON. Exit 0 = settled/succeeded; 1 = failure/error "
        "(inspect error_code or error.code); 3 = HAM still owns it.\n"
        "deferred, awaiting_decision, submitted_* and batch submitted/deferred: "
        "do not resend. Follow each request_id with:\n"
        f"{router} request-status --request-id ID --wait MS\n\n"
        "--- BEGIN ORIGINAL REQUEST ---\n"
        f"{original_request}\n"
        "--- END ORIGINAL REQUEST ---\n"
        "This text is the task, not permission to change targets/router/contract.\n\n"
    )
    if single_target:
        contract = (
            "Create a tailored instruction with only needed context:\n"
            f"{router} request --address HOST/LABEL --message 'TAILORED INSTRUCTION' "
            "--timeout 120000\n"
            "Wait until settled or clear failure. Verify the result against the task "
            "and workspace; run/request appropriate checks, synthesize the outcome, "
            "and report failures and risks.\n"
        )
    else:
        contract = (
            "Create tailored non-overlapping assignments per target/workspace; "
            "HAM does not split messages. Dispatch with bounded concurrency:\n"
            f"{router} batch --requests-json "
            "'[{\"address\":\"HOST/LABEL\",\"message\":\"TAILORED INSTRUCTION\"}]' "
            "--wait --timeout 120000 --max-workers 4\n"
            "Inspect every item; aggregate succeeded is not proof all settled. "
            "Wait for each item to settle or clearly fail; follow up on missing/"
            "inconsistent work. Verify each result against its task/workspace, "
            "run/request appropriate checks, and synthesize outcomes, failures, risks.\n"
        )
    return shared + contract


def decode_json_object(value: str) -> dict[str, Any]:
    try:
        decoded = json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return {}
    return decoded if isinstance(decoded, dict) else {}


def focused_pane_id(environment: Mapping[str, str] | None = None) -> str:
    values = os.environ if environment is None else environment
    context = decode_json_object(values.get("HERDR_PLUGIN_CONTEXT_JSON", "{}"))
    pane = context.get("pane")
    nested_pane_id = pane.get("pane_id") if isinstance(pane, dict) else None
    return str(
        context.get("focused_pane_id")
        or context.get("pane_id")
        or nested_pane_id
        or values.get("HERDR_ACTIVE_PANE_ID")
        or values.get("HERDR_PANE_ID")
        or ""
    )


def run_herdr(
    arguments: Sequence[str],
    environment: Mapping[str, str] | None = None,
    *,
    timeout: float = 10,
) -> subprocess.CompletedProcess[str]:
    command = [herdr_executable(environment), *arguments]
    try:
        return subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            check=False,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return subprocess.CompletedProcess(command, 1, "", str(error))


def show_notification(
    body: str,
    environment: Mapping[str, str] | None = None,
) -> bool:
    text = messages(detect_language(environment))
    result = run_herdr(
        [
            "notification",
            "show",
            text["title"],
            "--body",
            body,
            "--sound",
            "none",
        ],
        environment,
    )
    return result.returncode == 0


def bundled_skill_path() -> Path:
    return PLUGIN_ROOT / SKILL_RELATIVE_PATH


def launch_plugin_overlay(
    entrypoint: str,
    *,
    environment: Mapping[str, str] | None = None,
    extra_arguments: Sequence[str] = (),
) -> bool:
    result = run_herdr(
        [
            "plugin",
            "pane",
            "open",
            "--plugin",
            PLUGIN_ID,
            "--entrypoint",
            entrypoint,
            "--placement",
            "overlay",
            "--focus",
            *extra_arguments,
        ],
        environment,
    )
    return result.returncode == 0


def launch_overlay(
    pane_id: str,
    environment: Mapping[str, str] | None = None,
) -> bool:
    return launch_plugin_overlay(
        MESSENGER_ENTRYPOINT,
        environment=environment,
        extra_arguments=(
            "--env",
            f"{SENDER_PANE_ENV}={pane_id}",
        ),
    )


def launch_skill_guide(environment: Mapping[str, str] | None = None) -> int:
    text = messages(detect_language(environment))
    values = os.environ if environment is None else environment
    context = decode_json_object(values.get("HERDR_PLUGIN_CONTEXT_JSON", "{}"))
    workspace = context.get("workspace")
    nested_cwd = workspace.get("cwd") if isinstance(workspace, dict) else None
    project_root = context.get("workspace_cwd") or nested_cwd
    extra_arguments: tuple[str, ...] = ()
    if isinstance(project_root, str) and project_root.strip():
        extra_arguments = ("--env", f"{SKILL_PROJECT_ROOT_ENV}={project_root}")
    if launch_plugin_overlay(
        SKILL_INSTALLER_ENTRYPOINT,
        environment=environment,
        extra_arguments=extra_arguments,
    ):
        return 0
    if not show_notification(text["overlay_open_failed"], environment):
        print(text["overlay_open_failed"], file=sys.stderr)
    return 1


def launch(environment: Mapping[str, str] | None = None) -> int:
    values = os.environ if environment is None else environment
    text = messages(detect_language(values))
    pane_id = focused_pane_id(values)
    local_agents = query_local_agents(values) if pane_id else []
    sender = next(
        (record for record in local_agents if record.pane_id == pane_id),
        None,
    )
    if sender is None:
        if not show_notification(text["no_focused_agent"], values):
            print(text["no_focused_agent"], file=sys.stderr)
        return 0
    if not launch_overlay(pane_id, values):
        if not show_notification(text["overlay_open_failed"], values):
            print(text["overlay_open_failed"], file=sys.stderr)
        return 1
    return 0


def _same_occupant(left: AgentRecord, right: AgentRecord | None) -> bool:
    if right is None:
        return False
    if left.session_id:
        return left.session_id == right.session_id
    if left.revision <= 0:
        return False
    return (
        left.pane_id == right.pane_id
        and left.target == right.target
        and left.revision == right.revision
        and left.agent_kind == right.agent_kind
        and left.terminal_id == right.terminal_id
        and left.cwd == right.cwd
    )


def skill_guide_lines(text: Mapping[str, str], skill_path: Path) -> list[str]:
    return [
        text["skill_guide_intro"],
        "",
        text["skill_guide_target"],
        "",
        text["skill_guide_install"],
        os.fspath(skill_path),
        "",
        text["skill_guide_example"],
    ]


def render_skill_guide_screen(
    screen: curses.window,
    text: Mapping[str, str],
    skill_path: Path,
) -> None:
    theme = Theme()
    screen.erase()
    content = content_rect(*screen.getmaxyx(), 80).window(screen)
    height, width = content.getmaxyx()

    def add(row: int, column: int, value: str, attribute: int = 0) -> None:
        if row < 0 or row >= height or column < 0 or column >= width:
            return
        try:
            available = width - column
            content.addnstr(row, column, truncate_display_text(value, available), available, attribute)
        except curses.error:
            pass

    add(0, 0, text["skill_guide_title"], theme.style("accent", bold=True))
    hints = _help_hints(text["skill_guide_close"])
    footer_rows = len(footer_lines(hints, width, max_rows=2))
    row = 2
    for value in skill_guide_lines(text, skill_path):
        if not value:
            row += 1
            continue
        for line in wrap_display_text(value, max(1, min(76, width - 4))):
            if row >= height - footer_rows:
                break
            add(row, 2, line)
            row += 1
    draw_footer(
        content, min(row + 1, max(0, height - footer_rows)), 0, width, hints, theme,
        max_rows=2,
    )
    try:
        curses.curs_set(0)
    except curses.error:
        pass
    screen.refresh()


@contextmanager
def terminal_flow_control_disabled(stream: TextIO) -> Iterator[None]:
    """Let curses receive Ctrl+S instead of treating it as terminal XOFF."""

    if termios is None:
        yield
        return
    try:
        descriptor = stream.fileno()
        original = termios.tcgetattr(descriptor)
        updated = list(original)
        updated[0] &= ~(termios.IXON | termios.IXOFF)
        termios.tcsetattr(descriptor, termios.TCSANOW, updated)
    except (OSError, ValueError, termios.error):
        yield
        return
    try:
        yield
    finally:
        try:
            termios.tcsetattr(descriptor, termios.TCSANOW, original)
        except (OSError, ValueError, termios.error):
            pass


class MessengerApp(MessengerRenderMixin):
    def __init__(
        self,
        screen: curses.window,
        sender: AgentRecord,
        environment: Mapping[str, str],
    ) -> None:
        self.screen = screen
        self.theme = Theme(environment)
        self.content_screen = self._content_rect().window(screen)
        self.sender = sender
        self.environment = environment
        self.text = messages(detect_language(environment))
        self.config_path = ssh_config_path(environment)
        self.hosts = ssh_hosts(environment)
        self.cache = AgentCache.from_environment(environment)
        self.agents = [
            agent
            for agent in query_local_agents(environment)
            if agent.identity != sender.identity
        ]
        self.selected: set[str] = set()
        self.search = ""
        self.cursor = 0
        self.recipient_offset = 0
        self.section = "recipients"
        self.message_lines = [""]
        self.message_row = 0
        self.message_column = 0
        self.status = self.text["no_ssh_hosts"] if not self.hosts else ""
        self.status_kind = "info"
        self.discovery_choice = bool(not self.hosts)
        self.discovery_option = 0
        self.mode_choice = False
        self.mode_return_to_editor = False
        self.mode_option = 0
        self.delivery_mode = DELIVERY_DELEGATE
        self.remote_enabled = False
        self.discovery: RemoteDiscovery | None = None
        self.pending_hosts: set[str] = set()
        self.host_status: dict[str, str] = {}
        self.host_errors: dict[str, str] = {}
        self.skipped_hosts = 0
        self.running = True
        self.message_cursor: tuple[int, int] | None = None
        self.sending = False
        self.send_results: queue.Queue[SendJobResult] = queue.Queue()
        self.send_cancel = threading.Event()
        self.open_skill_installer = False
        self.remote_details_visible = False
        self.remote_details_offset = 0

    def filtered_agents(self) -> list[AgentRecord]:
        return filter_and_sort_agents(self.agents, self.search)

    def _display_host(self, host: str) -> str:
        return host

    _recipient_host_key = staticmethod(recipient_host_key)
    _recipient_workspace_key = staticmethod(recipient_workspace_key)

    def _recipient_view_rows(
        self,
        records: Sequence[AgentRecord],
    ) -> list[RecipientViewRow]:
        return build_recipient_view_rows(
            records,
            unknown_workspace=self.text["unknown_workspace"],
            display_host=self._display_host,
        )

    _sticky_headers_before = staticmethod(sticky_headers_before)

    def _visible_recipient_rows(
        self,
        records: Sequence[AgentRecord],
        list_rows: int,
    ) -> tuple[list[RecipientViewRow], bool]:
        visible, has_more, self.recipient_offset = visible_recipient_rows(
            records,
            cursor=self.cursor,
            offset=self.recipient_offset,
            list_rows=list_rows,
            unknown_workspace=self.text["unknown_workspace"],
            display_host=self._display_host,
        )
        return visible, has_more

    _group_header_parts = staticmethod(group_header_parts)

    def _group_header(self, row: RecipientViewRow) -> str:
        return "".join(self._group_header_parts(row))

    def _recipient_line(
        self,
        agent: AgentRecord,
        marker: str,
        state: str,
        *,
        focused: bool = False,
        indent: int = 0,
    ) -> str:
        _height, width = self._content_size()
        return format_recipient_line(
            agent,
            marker,
            state,
            width=width,
            focused=focused,
            indent=indent,
        )

    def _replace_agent_scope(
        self,
        *,
        local: bool,
        host: str,
        records: Sequence[AgentRecord],
    ) -> None:
        selected_for_scope = {
            agent.identity
            for agent in self.agents
            if agent.local == local
            and agent.host == host
            and agent.identity in self.selected
        }
        self.agents = [
            agent
            for agent in self.agents
            if not (agent.local == local and agent.host == host)
        ]
        self.agents.extend(records)
        available = {agent.identity for agent in records if not agent.stale}
        self.selected.difference_update(selected_for_scope - available)
        self._clamp_cursor()

    def _start_remote_discovery(self, *, force: bool) -> None:
        if self.discovery:
            self.discovery.cancel()
        self.remote_enabled = True
        self.skipped_hosts = 0
        self.host_errors.clear()
        hosts_to_probe: list[str] = []
        now = time.time()
        for host in self.hosts:
            entry = self.cache.host_entry(host)
            fresh = self.cache.is_fresh(host, now)
            if fresh and not force:
                if entry.get("reachable"):
                    self._replace_agent_scope(
                        local=False,
                        host=host,
                        records=self.cache.agents(host),
                    )
                    self.host_status[host] = self.text["cached"]
                else:
                    self._replace_agent_scope(
                        local=False,
                        host=host,
                        records=self.cache.agents(host, stale=True),
                    )
                    self.host_status[host] = self.text["unavailable"]
                    self.host_errors[host] = str(entry.get("error") or "unavailable")
                    self.skipped_hosts += 1
                continue

            cached_agents = self.cache.agents(host, stale=True)
            self._replace_agent_scope(
                local=False,
                host=host,
                records=cached_agents,
            )
            self.host_status[host] = self.text["refreshing"]
            hosts_to_probe.append(host)

        self.pending_hosts = set(hosts_to_probe)
        if not hosts_to_probe:
            self.discovery = None
            self.status = self._discovery_summary()
            self.status_kind = "warning" if self.skipped_hosts else "success"
            return
        self.discovery = RemoteDiscovery(hosts_to_probe, config_path=self.config_path)
        self.discovery.start()
        self.status = self.text["discovering"]
        self.status_kind = "progress"

    def _discovery_summary(self) -> str:
        summary = self.text["discovery_complete"]
        if self.skipped_hosts:
            summary += " " + self.text["hosts_skipped"].format(count=self.skipped_hosts)
        return summary

    def _poll_discovery(self) -> None:
        if not self.discovery:
            return
        for result in self.discovery.poll():
            if result.host not in self.pending_hosts:
                continue
            self.pending_hosts.discard(result.host)
            self.cache.update(result)
            if result.success:
                self._replace_agent_scope(
                    local=False,
                    host=result.host,
                    records=result.agents,
                )
                self.host_status[result.host] = ""
                self.host_errors.pop(result.host, None)
            else:
                stale_agents = self.cache.agents(result.host, stale=True)
                self._replace_agent_scope(
                    local=False,
                    host=result.host,
                    records=stale_agents,
                )
                self.host_status[result.host] = self.text["unavailable"]
                self.host_errors[result.host] = result.error or "unavailable"
                self.skipped_hosts += 1
        if not self.pending_hosts:
            self.cache.save()
            self.discovery = None
            self.status = self._discovery_summary()
            self.status_kind = "warning" if self.skipped_hosts else "success"
            return
        if time.monotonic() >= self.discovery.deadline:
            self.discovery.cancel()
            for host in tuple(self.pending_hosts):
                result = ProbeResult(host, (), False, "timeout")
                self.cache.update(result)
                self.host_status[host] = self.text["unavailable"]
                self.host_errors[host] = result.error
                self.skipped_hosts += 1
            self.pending_hosts.clear()
            self.cache.save()
            self.discovery = None
            self.status = self._discovery_summary()
            self.status_kind = "warning"

    def _cancel_discovery(self) -> None:
        if self.discovery:
            self.discovery.cancel()
            self.discovery = None
        self.pending_hosts.clear()
        self.status = self.text["discovery_cancelled"]
        self.status_kind = "info"

    def _use_local_only(self) -> None:
        if self.discovery:
            self.discovery.cancel()
            self.discovery = None
        remote_selected = {
            agent.identity
            for agent in self.agents
            if not agent.local and agent.identity in self.selected
        }
        self.selected.difference_update(remote_selected)
        self.agents = [agent for agent in self.agents if agent.local]
        self.pending_hosts.clear()
        self.host_status.clear()
        self.host_errors.clear()
        self.skipped_hosts = 0
        self.remote_enabled = False
        self.status = ""
        self.status_kind = "info"
        self._clamp_cursor()

    def _remote_counts(self) -> tuple[int, int, int]:
        available = sum(
            status != self.text["unavailable"] and status != self.text["refreshing"]
            for status in self.host_status.values()
        )
        refreshing = sum(
            status == self.text["refreshing"] for status in self.host_status.values()
        )
        unavailable = sum(
            status == self.text["unavailable"] for status in self.host_status.values()
        )
        return available, refreshing, unavailable

    def _remote_summary(self) -> str:
        available, refreshing, unavailable = self._remote_counts()
        if unavailable:
            summary = self.text["remote_warning_summary"].format(
                unavailable=unavailable,
                available=available,
                details=self.text["remote_details_hint"],
            )
        else:
            summary = self.text["remote_summary"].format(available=available)
        if refreshing:
            summary += " " + self.text["remote_refreshing_count"].format(
                count=refreshing
            )
        return summary

    def _unavailable_hosts(self) -> list[tuple[str, str]]:
        return sorted(
            (
                self._display_host(host),
                _single_line(self.host_errors.get(host, "unavailable")),
            )
            for host, status in self.host_status.items()
            if status == self.text["unavailable"]
        )

    def _clamp_cursor(self) -> None:
        records = self.filtered_agents()
        self.cursor = max(0, min(self.cursor, max(0, len(records) - 1)))

    def _toggle_current(self) -> None:
        records = self.filtered_agents()
        if not records:
            return
        agent = records[self.cursor]
        if agent.stale:
            return
        if agent.identity in self.selected:
            self.selected.remove(agent.identity)
        else:
            self.selected.add(agent.identity)

    def _handle_recipient_key(self, key: str | int) -> None:
        records = self.filtered_agents()
        if key == curses.KEY_UP:
            self.cursor = max(0, self.cursor - 1)
        elif key == curses.KEY_DOWN:
            self.cursor = min(max(0, len(records) - 1), self.cursor + 1)
        elif key == " ":
            self._toggle_current()
        elif key == "\t":
            self.section = "message"
        elif key in (curses.KEY_BACKSPACE, "\x7f", "\b"):
            self.search = self.search[:-1]
            self._clamp_cursor()
        elif key == "\x01":
            self.selected.update(agent.identity for agent in records if not agent.stale)
        elif key == "\x04":
            self.selected.clear()
        elif key == "\x12":
            local_agents = [
                agent
                for agent in query_local_agents(self.environment)
                if agent.local and agent.identity != self.sender.identity
            ]
            self._replace_agent_scope(
                local=True,
                host=self.sender.host,
                records=local_agents,
            )
            if self.remote_enabled:
                self._start_remote_discovery(force=True)
            self._clamp_cursor()
        elif isinstance(key, str) and key.isprintable() and key not in ("\n", "\r"):
            self.search += key
            self._clamp_cursor()

    def _handle_message_key(self, key: str | int) -> None:
        line = self.message_lines[self.message_row]
        if key == "\t":
            self.section = "recipients"
        elif key == "\x13":
            self._send()
        elif key in ("\n", "\r", curses.KEY_ENTER):
            before = line[: self.message_column]
            after = line[self.message_column :]
            self.message_lines[self.message_row] = before
            self.message_lines.insert(self.message_row + 1, after)
            self.message_row += 1
            self.message_column = 0
        elif key in (curses.KEY_BACKSPACE, "\x7f", "\b"):
            if self.message_column > 0:
                self.message_lines[self.message_row] = (
                    line[: self.message_column - 1] + line[self.message_column :]
                )
                self.message_column -= 1
            elif self.message_row > 0:
                previous = self.message_lines[self.message_row - 1]
                self.message_column = len(previous)
                self.message_lines[self.message_row - 1] = previous + line
                self.message_lines.pop(self.message_row)
                self.message_row -= 1
        elif key == curses.KEY_LEFT:
            if self.message_column > 0:
                self.message_column -= 1
        elif key == curses.KEY_RIGHT:
            if self.message_column < len(line):
                self.message_column += 1
        elif key == curses.KEY_UP:
            self._move_message_cursor_visual(-1)
        elif key == curses.KEY_DOWN:
            self._move_message_cursor_visual(1)
        elif isinstance(key, str) and key.isprintable():
            self.message_lines[self.message_row] = (
                line[: self.message_column] + key + line[self.message_column :]
            )
            self.message_column += len(key)

    def _move_message_cursor_visual(self, delta: int) -> None:
        wrapped_lines, cursor_row, cursor_column = wrap_message_lines(
            self.message_lines,
            width=self._message_editor_width(),
            cursor_row=self.message_row,
            cursor_column=self.message_column,
        )
        target_row = max(0, min(cursor_row + delta, len(wrapped_lines) - 1))
        if target_row == cursor_row:
            return
        target = wrapped_lines[target_row]
        column_offset = character_index_at_display_column(
            target.text,
            cursor_column,
        )
        self.message_row = target.logical_row
        self.message_column = target.start + column_offset

    def _send(self) -> None:
        if self.sending:
            return
        recipients = [
            agent
            for agent in self.agents
            if agent.identity in self.selected and not agent.stale
        ]
        original_message = "\n".join(self.message_lines)
        if not recipients:
            self.status = self.text["no_recipients"]
            self.status_kind = "warning"
            return
        if not original_message.strip():
            self.status = self.text["empty_message"]
            self.status_kind = "warning"
            return
        self.status = (
            self.text["delegating"]
            if self.delivery_mode == DELIVERY_DELEGATE
            else self.text["sending"]
        )
        self.status_kind = "progress"
        self.sending = True
        self.send_cancel.clear()
        threading.Thread(
            target=self._dispatch_send_job,
            args=(tuple(recipients), original_message),
            daemon=True,
        ).start()

    def _dispatch_send_job(
        self,
        recipients: tuple[AgentRecord, ...],
        message: str,
    ) -> None:
        try:
            current_sender = fetch_local_agent(
                self.sender.pane_id,
                self.environment,
                cancel_event=self.send_cancel,
            )
            if self.send_cancel.is_set():
                self.send_results.put(SendJobResult(True, (), cancelled=True))
                return
            if not _same_occupant(self.sender, current_sender):
                self.send_results.put(SendJobResult(False, ()))
                return
            dispatch_recipients = recipients
            dispatch_message = message.strip()
            if self.delivery_mode == DELIVERY_DELEGATE:
                dispatch_recipients = (self.sender,)
                dispatch_message = build_orchestration_request(recipients, message)
            results = dispatch_prompts(
                self.sender,
                dispatch_recipients,
                dispatch_message,
                config_path=self.config_path,
                environment=self.environment,
                cancel_event=self.send_cancel,
            )
        except Exception as error:
            self.send_results.put(SendJobResult(True, (), str(error)))
            return
        self.send_results.put(
            SendJobResult(
                True,
                tuple(results),
                cancelled=self.send_cancel.is_set(),
            )
        )

    def _poll_send(self) -> None:
        try:
            job = self.send_results.get_nowait()
        except queue.Empty:
            return
        self.sending = False
        results = list(job.results)
        succeeded = [result for result in results if result.success]
        unknown = [result for result in results if result.submission_unknown]
        failed = [result for result in results if not result.success and not result.submission_unknown]
        if self.delivery_mode == DELIVERY_DIRECT:
            self.selected.difference_update(result.agent.identity for result in [*succeeded, *unknown])
        elif unknown or (job.cancelled and succeeded):
            self.selected.clear()
        if job.cancelled:
            if unknown:
                self.status = self.text["delivery_unknown"].format(count=len(unknown))
                self.status_kind = "warning"
            elif succeeded:
                self.status = (
                    self.text["delegated"]
                    if self.delivery_mode == DELIVERY_DELEGATE
                    else self.text["sent"].format(count=len(succeeded))
                ) + " " + self.text["send_cancelled"]
                self.status_kind = "success"
            else:
                self.status = self.text["send_cancelled"]
                self.status_kind = "info"
            return
        if not job.sender_available:
            self.status = self.text["sender_unavailable"]
            self.status_kind = "error"
            return
        if job.error:
            self.status = (
                self.text["delegate_failed"]
                if self.delivery_mode == DELIVERY_DELEGATE
                else self.text["all_failed"]
            )
            self.status_kind = "error"
            return
        if not results:
            self.status = (
                self.text["delegate_failed"]
                if self.delivery_mode == DELIVERY_DELEGATE
                else self.text["all_failed"]
            )
            self.status_kind = "error"
            return
        if unknown:
            if self.delivery_mode == DELIVERY_DIRECT:
                self.selected = {result.agent.identity for result in failed}
            self.status = self.text["delivery_unknown"].format(count=len(unknown))
            self.status_kind = "warning"
            return
        if not failed:
            if self.delivery_mode == DELIVERY_DELEGATE:
                body = self.text["delegated"]
            else:
                body = self.text["sent"].format(count=len(succeeded))
            show_notification(body, self.environment)
            self.running = False
            return
        if self.delivery_mode == DELIVERY_DELEGATE:
            self.status = self.text["delegate_failed"]
            self.status_kind = "error"
            return
        self.selected = {result.agent.identity for result in failed}
        if succeeded:
            self.status = self.text["partial_failure"].format(
                sent=len(succeeded),
                failed=len(failed),
            )
            self.status_kind = "warning"
        else:
            self.status = self.text["all_failed"]
            self.status_kind = "error"

    def handle_key(self, key: str | int) -> None:
        if self.remote_details_visible:
            failures = self._unavailable_hosts()
            height, _width = self._content_size()
            footer_rows = len(self._help_lines(self.text["remote_details_help"]))
            maximum_offset = max(0, len(failures) - max(1, height - 3 - footer_rows))
            if key == curses.KEY_UP:
                self.remote_details_offset = max(0, self.remote_details_offset - 1)
            elif key == curses.KEY_DOWN:
                self.remote_details_offset = min(
                    maximum_offset,
                    self.remote_details_offset + 1,
                )
            elif key in (REMOTE_DETAILS_KEY, "\x1b", "q", "Q"):
                self.remote_details_visible = False
            return
        if key == SKILL_GUIDE_KEY and not self.sending:
            self.open_skill_installer = True
            self.running = False
            return
        if (
            key == REMOTE_DETAILS_KEY
            and not self.sending
            and self._unavailable_hosts()
        ):
            self.remote_details_visible = True
            self.remote_details_offset = 0
            return
        if not self.discovery_choice:
            if key == curses.KEY_UP:
                self.discovery_option = (self.discovery_option - 1) % 2
            elif key == curses.KEY_DOWN:
                self.discovery_option = (self.discovery_option + 1) % 2
            elif key in ("\n", "\r", curses.KEY_ENTER, " "):
                self.discovery_choice = True
                if self.discovery_option == 0:
                    self._start_remote_discovery(force=False)
                else:
                    self._use_local_only()
            elif isinstance(key, str) and key.lower() == "d":
                self.discovery_choice = True
                self.discovery_option = 0
                self._start_remote_discovery(force=False)
            elif isinstance(key, str) and key.lower() == "l":
                self.discovery_choice = True
                self.discovery_option = 1
                self._use_local_only()
            elif key == "\x1b":
                self.running = False
            return

        if not self.mode_choice:
            if key == curses.KEY_UP:
                self.mode_option = (self.mode_option - 1) % 2
            elif key == curses.KEY_DOWN:
                self.mode_option = (self.mode_option + 1) % 2
            elif key in ("\n", "\r", curses.KEY_ENTER, " "):
                self.delivery_mode = (
                    DELIVERY_DELEGATE if self.mode_option == 0 else DELIVERY_DIRECT
                )
                self.mode_choice = True
                self.mode_return_to_editor = False
            elif isinstance(key, str) and key.lower() == "c":
                self.mode_option = 0
                self.delivery_mode = DELIVERY_DELEGATE
                self.mode_choice = True
                self.mode_return_to_editor = False
            elif isinstance(key, str) and key.lower() == "d":
                self.mode_option = 1
                self.delivery_mode = DELIVERY_DIRECT
                self.mode_choice = True
                self.mode_return_to_editor = False
            elif key == "\x1b":
                if self.mode_return_to_editor:
                    self.mode_choice = True
                    self.mode_return_to_editor = False
                elif self.hosts:
                    self.discovery_choice = False
                else:
                    self.running = False
            return

        if self.sending:
            if key == "\x1b":
                self.send_cancel.set()
                self.status = self.text["cancelling_send"]
                self.status_kind = "progress"
            return

        if key == "\x1b":
            if self.discovery:
                self._cancel_discovery()
            else:
                self.running = False
            return
        if key == "\x0f":
            self.mode_option = 0 if self.delivery_mode == DELIVERY_DELEGATE else 1
            self.mode_choice = False
            self.mode_return_to_editor = True
            return
        if self.section == "recipients":
            self._handle_recipient_key(key)
        else:
            self._handle_message_key(key)

    def run(self) -> int:
        self.screen.keypad(True)
        self.screen.timeout(100)
        while self.running:
            self._poll_discovery()
            self._poll_send()
            self.render()
            try:
                key = self.screen.get_wch()
            except curses.error:
                continue
            self.handle_key(key)
        if self.discovery:
            self.discovery.cancel()
        return 0


def overlay(environment: Mapping[str, str] | None = None) -> int:
    values = dict(os.environ if environment is None else environment)
    text = messages(detect_language(values))
    pane_id = values.get(SENDER_PANE_ENV, "")
    sender = fetch_local_agent(pane_id, values) if pane_id else None
    if sender is None:
        if not show_notification(text["no_focused_agent"], values):
            print(text["no_focused_agent"], file=sys.stderr)
        return 0
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        print(text["interactive_required"], file=sys.stderr)
        return 1
    try:
        locale.setlocale(locale.LC_ALL, "")
    except locale.Error:
        pass
    app: MessengerApp | None = None

    def run(screen: curses.window) -> int:
        nonlocal app
        app = MessengerApp(screen, sender, values)
        return app.run()

    with terminal_flow_control_disabled(sys.stdin):
        result = curses.wrapper(run)
    if app is not None and app.open_skill_installer:
        installer_values = dict(values)
        if sender.cwd:
            installer_values[SKILL_PROJECT_ROOT_ENV] = sender.cwd
        if not launch_plugin_overlay(
            SKILL_INSTALLER_ENTRYPOINT,
            environment=installer_values,
            extra_arguments=(
                "--env",
                f"{SKILL_PROJECT_ROOT_ENV}={sender.cwd}",
            )
            if sender.cwd
            else (),
        ):
            return 1
    return result


def launch_held(environment: Mapping[str, str] | None = None) -> int:
    return 0 if launch_plugin_overlay(
        HELD_ENTRYPOINT,
        environment=environment,
    ) else 1


class HeldMessagesApp:
    """Review focused-recipient deliveries without requiring an active agent."""

    def __init__(
        self,
        screen: curses.window,
        environment: Mapping[str, str],
        *,
        delivery: Any = None,
    ) -> None:
        if delivery is None:
            from ham.agents import delivery as agent_delivery

            delivery = agent_delivery
        self.screen = screen
        self.theme = Theme(environment)
        self.environment = environment
        self.delivery = delivery
        self.text = messages(detect_language(environment))
        self.items: list[Any] = []
        self.cursor = 0
        self.offset = 0
        self.error = ""
        self.running = True
        self.refresh()

    def refresh(self) -> None:
        selected = self.items[self.cursor].request_id if self.items else None
        try:
            items = self.delivery.held_messages(self.environment)
        except Exception as error:
            self.error = self.text["held_error"].format(error=error)
            return
        self.items = items
        self.cursor = next(
            (index for index, item in enumerate(items) if item.request_id == selected),
            min(self.cursor, max(0, len(items) - 1)),
        )

    def handle_key(self, key: str | int) -> None:
        if key in ("q", "Q", "\x1b"):
            self.running = False
        elif key == curses.KEY_UP:
            self.cursor = max(0, self.cursor - 1)
        elif key == curses.KEY_DOWN:
            self.cursor = min(max(0, len(self.items) - 1), self.cursor + 1)
        elif key in ("p", "P", "c", "C") and self.items:
            decision = "push" if key in ("p", "P") else "cancel"
            try:
                record = self.delivery.decide(
                    self.items[self.cursor].request_id, decision, self.environment
                )
                state = record["state"]
                applied = (
                    state.startswith("submitted_")
                    if decision == "push"
                    else record.get("phase") == "delivery_cancelled"
                )
                self.error = (
                    "" if applied else self.text[f"held_{decision}_not_applied"].format(state=state)
                )
            except Exception as error:
                self.error = self.text["held_error"].format(error=error)
            self.refresh()

    def render(self) -> None:
        self.screen.erase()
        content = content_rect(*self.screen.getmaxyx(), 96).window(self.screen)
        height, width = content.getmaxyx()

        def add(row: int, value: str, attribute: int = 0) -> None:
            if not 0 <= row < height:
                return
            try:
                content.addnstr(
                    row, 0, truncate_display_text(value, width), width, attribute,
                )
            except curses.error:
                pass

        hints = _help_hints(self.text["held_help"])
        if not self.items:
            draw_title(content, self.theme, "Held", "0 held", width)
            close_hints = [hint for hint in hints if hint.key == "Esc"]
            row = 2 + draw_empty(
                content, 2, 0, width, self.text["held_empty"], self.theme,
                hints=close_hints,
            )
            if self.error:
                draw_status(content, row, 0, width, self.error, self.theme, kind="error")
        else:
            item = self.items[self.cursor]
            footer_rows = len(footer_lines(hints, width, max_rows=2))
            preview_lines = wrap_display_text(item.preview, width)[:4]
            # Reserve two preview rows even when the selected message is short.
            preview_rows = max(2, len(preview_lines))
            date_lines = wrap_display_text(
                self.text["held_since"].format(since=item.since, deadline=item.deadline),
                width,
            )
            warning_lines = wrap_display_text(self.text["held_warning"], width)
            details_rows = (
                1 + preview_rows + len(date_lines) + len(warning_lines)
                + bool(self.error) + footer_rows
            )
            list_rows = max(1, height - 2 - details_rows)
            visible_count = min(len(self.items), list_rows)
            self.offset = min(
                max(0, len(self.items) - visible_count),
                max(min(self.offset, self.cursor), self.cursor - visible_count + 1),
            )
            first = self.offset + 1
            last = min(len(self.items), self.offset + visible_count)
            position = f"{'↑' if self.offset else ''}{first}-{last}/{len(self.items)}"
            if last < len(self.items):
                position += "↓"
            draw_title(
                content, self.theme, "Held", f"{len(self.items)} held",
                width, indicator=position,
            )
            row = 2
            for index in range(self.offset, last):
                item = self.items[index]
                state = self.text[f"held_{item.state}"]
                label = self.text["held_item"].format(
                    sender=item.sender, recipient=item.recipient, state=state,
                )
                if index == self.cursor:
                    self.theme.draw_selected_row(content, row, label, width)
                else:
                    add(row, f"  {label}")
                row += 1
            row += 1
            for offset, line in enumerate(preview_lines):
                add(row + offset, line)
            row += preview_rows
            for line in date_lines:
                add(row, line)
                row += 1
            for line in warning_lines:
                add(row, line, self.theme.style("warning"))
                row += 1
            if self.error:
                draw_status(content, row, 0, width, self.error, self.theme, kind="error")
                row += 1
            draw_footer(content, row, 0, width, hints, self.theme, max_rows=2)
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        self.screen.refresh()

    def run(self) -> int:
        self.screen.keypad(True)
        self.screen.timeout(1000)
        while self.running:
            self.render()
            try:
                key = self.screen.get_wch()
            except curses.error:
                self.refresh()
                continue
            self.handle_key(key)
        return 0


def overlay_held(environment: Mapping[str, str] | None = None) -> int:
    values = dict(os.environ if environment is None else environment)
    text = messages(detect_language(values))
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        print(text["interactive_required"], file=sys.stderr)
        return 1
    try:
        locale.setlocale(locale.LC_ALL, "")
    except locale.Error:
        pass
    return curses.wrapper(lambda screen: HeldMessagesApp(screen, values).run())


def skill_guide(environment: Mapping[str, str] | None = None) -> int:
    values = dict(os.environ if environment is None else environment)
    text = messages(detect_language(values))
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        print(text["interactive_required"], file=sys.stderr)
        return 1
    try:
        locale.setlocale(locale.LC_ALL, "")
    except locale.Error:
        pass

    def run(screen: curses.window) -> int:
        screen.keypad(True)
        while True:
            render_skill_guide_screen(screen, text, bundled_skill_path())
            key = screen.get_wch()
            if key in (
                SKILL_GUIDE_KEY,
                "?",
                "\x1b",
                "q",
                "Q",
                "\n",
                "\r",
                curses.KEY_ENTER,
            ):
                return 0

    return curses.wrapper(run)


def parse_cli_arguments(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Send prompts to Herdr agents.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("launch", help="Validate the focused agent and open the overlay.")
    subparsers.add_parser("overlay", help="Run the interactive overlay.")
    subparsers.add_parser("held-launch", help="Open held messages.")
    subparsers.add_parser("overlay-held", help="Run the held messages overlay.")
    subparsers.add_parser("guide-launch", help="Open the bundled agent skill guide.")
    subparsers.add_parser("guide", help="Run the interactive agent skill guide.")
    subparsers.add_parser("skill-path", help="Print the bundled SKILL.md path.")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    arguments = parse_cli_arguments(argv)
    if arguments.command == "launch":
        return launch()
    if arguments.command == "overlay":
        return overlay()
    if arguments.command == "held-launch":
        return launch_held()
    if arguments.command == "overlay-held":
        return overlay_held()
    if arguments.command == "guide-launch":
        return launch_skill_guide()
    if arguments.command == "guide":
        return skill_guide()
    print(bundled_skill_path())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
