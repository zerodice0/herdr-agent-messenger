"""Pure recipient filtering, hierarchy, and viewport calculations."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from ham.agents.directory import AgentRecord
from ham.ui.terminal_text import display_width, pad_display_text, truncate_display_text


STATUS_ORDER = {"blocked": 0, "working": 1, "done": 2, "idle": 3, "unknown": 4}


@dataclass(frozen=True)
class RecipientViewRow:
    kind: str
    agent_index: int
    agent: AgentRecord
    group_count: int = 0
    group_label: str = ""
    indent: int = 0


def workspace_display(agent: AgentRecord) -> str:
    prefix = "WT:" if agent.workspace_is_worktree else ""
    return f"{prefix}{agent.workspace_label or agent.workspace_id}"


def filter_and_sort_agents(
    records: Sequence[AgentRecord],
    search: str,
) -> list[AgentRecord]:
    needle = search.casefold().strip()
    filtered = list(records)
    if needle:
        filtered = [
            agent
            for agent in filtered
            if needle
            in " ".join(
                (
                    agent.host,
                    agent.name,
                    agent.target,
                    agent.pane_id,
                    agent.session_id,
                    agent.workspace_label,
                    agent.workspace_id,
                    agent.status,
                )
            ).casefold()
        ]
    return sorted(
        filtered,
        key=lambda agent: (
            not agent.local,
            agent.host.casefold(),
            agent.workspace_label.casefold(),
            agent.workspace_id.casefold(),
            STATUS_ORDER.get(agent.status, 9),
            agent.target.casefold(),
        ),
    )


def recipient_host_key(agent: AgentRecord) -> tuple[bool, str]:
    return agent.local, agent.host


def recipient_workspace_key(agent: AgentRecord) -> tuple[bool, str, str]:
    workspace_key = (
        agent.workspace_id
        or agent.workspace_label
        or agent.cwd
        or agent.pane_id
    )
    return agent.local, agent.host, workspace_key


def build_recipient_view_rows(
    records: Sequence[AgentRecord],
    *,
    unknown_workspace: str,
    display_host: Callable[[str], str] = str,
) -> list[RecipientViewRow]:
    if not records:
        return []

    host_counts: dict[tuple[bool, str], int] = {}
    workspace_counts: dict[tuple[bool, str, str], int] = {}
    workspace_labels: dict[tuple[bool, str, str], str] = {}
    label_keys: dict[
        tuple[tuple[bool, str], str],
        set[tuple[bool, str, str]],
    ] = {}
    for record in records:
        host_key = recipient_host_key(record)
        workspace_key = recipient_workspace_key(record)
        workspace_label = workspace_display(record) or unknown_workspace
        host_counts[host_key] = host_counts.get(host_key, 0) + 1
        workspace_counts[workspace_key] = workspace_counts.get(workspace_key, 0) + 1
        workspace_labels.setdefault(workspace_key, workspace_label)
        label_keys.setdefault(
            (host_key, workspace_label.casefold()),
            set(),
        ).add(workspace_key)

    show_host_headers = len(host_counts) > 1 or any(
        not record.local for record in records
    )
    rows: list[RecipientViewRow] = []
    previous_host_key: tuple[bool, str] | None = None
    previous_workspace_key: tuple[bool, str, str] | None = None
    for agent_index, record in enumerate(records):
        host_key = recipient_host_key(record)
        workspace_key = recipient_workspace_key(record)
        if show_host_headers and host_key != previous_host_key:
            rows.append(
                RecipientViewRow(
                    kind="host_header",
                    agent_index=agent_index,
                    agent=record,
                    group_count=host_counts[host_key],
                    group_label=display_host(record.host),
                )
            )
        if workspace_key != previous_workspace_key:
            workspace_label = workspace_labels[workspace_key]
            duplicate_label = len(
                label_keys[(host_key, workspace_label.casefold())]
            ) > 1
            if duplicate_label:
                workspace_id = record.workspace_id or workspace_key[-1]
                workspace_label = f"{workspace_label} [{workspace_id}]"
            rows.append(
                RecipientViewRow(
                    kind="workspace_header",
                    agent_index=agent_index,
                    agent=record,
                    group_count=workspace_counts[workspace_key],
                    group_label=workspace_label,
                    indent=2 if show_host_headers else 0,
                )
            )
        rows.append(
            RecipientViewRow(
                kind="agent",
                agent_index=agent_index,
                agent=record,
                indent=4 if show_host_headers else 2,
            )
        )
        previous_host_key = host_key
        previous_workspace_key = workspace_key
    return rows


def sticky_headers_before(
    rows: Sequence[RecipientViewRow],
    offset: int,
    max_headers: int,
) -> list[RecipientViewRow]:
    if not rows or offset <= 0 or max_headers <= 0:
        return []
    target = rows[offset].agent
    host_key = recipient_host_key(target)
    workspace_key = recipient_workspace_key(target)
    host_header: RecipientViewRow | None = None
    workspace_header: RecipientViewRow | None = None
    has_host_headers = any(candidate.kind == "host_header" for candidate in rows)
    for index in range(offset - 1, -1, -1):
        row = rows[index]
        if (
            workspace_header is None
            and row.kind == "workspace_header"
            and recipient_workspace_key(row.agent) == workspace_key
        ):
            workspace_header = row
        if (
            host_header is None
            and row.kind == "host_header"
            and recipient_host_key(row.agent) == host_key
        ):
            host_header = row
        if workspace_header is not None and (
            host_header is not None or not has_host_headers
        ):
            break
    headers = [
        header
        for header in (host_header, workspace_header)
        if header is not None
    ]
    return headers[-max_headers:]


def visible_recipient_rows(
    records: Sequence[AgentRecord],
    *,
    cursor: int,
    offset: int,
    list_rows: int,
    unknown_workspace: str,
    display_host: Callable[[str], str] = str,
) -> tuple[list[RecipientViewRow], bool, int]:
    """Return visible rows, overflow state, and the adjusted viewport offset."""

    rows = build_recipient_view_rows(
        records,
        unknown_workspace=unknown_workspace,
        display_host=display_host,
    )
    if not rows:
        return [], False, 0
    cursor_row = next(
        (
            index
            for index, row in enumerate(rows)
            if row.kind == "agent" and row.agent_index == cursor
        ),
        0,
    )
    offset = min(offset, max(0, len(rows) - 1))
    if cursor_row < offset:
        offset = cursor_row
    while True:
        sticky = sticky_headers_before(rows, offset, max(0, list_rows - 1))
        capacity = max(1, list_rows - len(sticky))
        if cursor_row < offset + capacity:
            break
        offset = cursor_row - capacity + 1
    sticky = sticky_headers_before(rows, offset, max(0, list_rows - 1))
    capacity = max(1, list_rows - len(sticky))
    end = offset + capacity
    visible = [*sticky, *rows[offset:end]]
    return visible, end < len(rows), offset


def group_header_parts(
    row: RecipientViewRow,
    max_width: int | None = None,
) -> tuple[str, str]:
    label = f'{" " * row.indent}▾ {row.group_label}'
    show_count = row.kind == "host_header" or row.group_count > 1
    count = f" ({row.group_count})" if show_count else ""
    if count and max_width is not None:
        label = truncate_display_text(
            label,
            max(0, max_width - display_width(count)),
        )
    return label, count


def format_recipient_line(
    agent: AgentRecord,
    marker: str,
    state: str,
    *,
    width: int,
    focused: bool = False,
    indent: int = 0,
) -> str:
    usable_width = max(1, width - indent)
    label = agent.name or agent.agent_kind or agent.pane_id
    pane = agent.pane_id
    cursor_marker = "›" if focused else " "
    prefix = f'{" " * indent}{cursor_marker} [{marker}] '
    state_width = display_width(state)
    if usable_width >= 84:
        pane_width = min(12, max(7, display_width(pane)))
        label_width = max(14, usable_width - pane_width - state_width - 9)
        return (
            prefix
            + f"{pad_display_text(label, label_width)} "
            + f"{pad_display_text(pane, pane_width)} "
            + state
        )
    available = max(4, usable_width - state_width - 8)
    identity = f"{label} · {pane}"
    return f"{prefix}{truncate_display_text(identity, available)} {state}"
