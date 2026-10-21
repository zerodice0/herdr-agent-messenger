"""Curses rendering behavior shared by the messenger application controller."""

from __future__ import annotations

import curses

from ham.agents.directory import AgentRecord
from ham.ui.recipient_view import RecipientViewRow
from ham.ui.terminal_text import (
    ShortcutHelpSpan,
    display_width,
    scrollbar_thumb,
    shortcut_help_spans,
    truncate_display_text,
    wrap_display_text,
    wrap_message_lines,
)


MESSAGE_MIN_VISIBLE_ROWS = 4
STATUS_GLYPHS = {
    "blocked": "!",
    "working": "●",
    "done": "✓",
    "idle": "○",
    "unknown": "?",
}
PAIR_ACCENT = 1
PAIR_SUCCESS = 2
PAIR_WARNING = 3
PAIR_ERROR = 4
DELIVERY_DELEGATE = "delegate"
DELIVERY_DIRECT = "direct"


class MessengerRenderMixin:
    """Render a controller that provides messenger state and view helpers."""

    def _message_editor_width(self) -> int:
        _height, width = self.screen.getmaxyx()
        return max(1, width - 5)

    def _initialize_colors(self) -> None:
        if not curses.has_colors():
            return
        try:
            curses.start_color()
            try:
                curses.use_default_colors()
                background = -1
            except curses.error:
                background = curses.COLOR_BLACK
            curses.init_pair(PAIR_ACCENT, curses.COLOR_CYAN, background)
            curses.init_pair(PAIR_SUCCESS, curses.COLOR_GREEN, background)
            curses.init_pair(PAIR_WARNING, curses.COLOR_YELLOW, background)
            curses.init_pair(PAIR_ERROR, curses.COLOR_RED, background)
        except curses.error:
            return
        self.colors_enabled = True

    def _style(self, pair: int = 0, attribute: int = 0) -> int:
        if pair and self.colors_enabled:
            attribute |= curses.color_pair(pair)
        return attribute

    def _status_attribute(self, agent: AgentRecord) -> int:
        if agent.stale:
            return curses.A_DIM
        if agent.status == "blocked":
            return self._style(PAIR_ERROR, curses.A_BOLD)
        if agent.status == "working":
            return self._style(PAIR_WARNING, curses.A_BOLD)
        if agent.status == "done":
            return self._style(PAIR_SUCCESS)
        if agent.status in ("idle", "unknown"):
            return curses.A_DIM
        return 0

    @staticmethod
    def _recipient_panel_height(record_count: int, content_height: int) -> int:
        message_panel_height = 1 + MESSAGE_MIN_VISIBLE_ROWS
        maximum = max(3, content_height - message_panel_height)
        desired = 2 + max(1, record_count)
        return max(3, min(desired, maximum))

    def _render_remote_details(self) -> None:
        self.screen.erase()
        height, width = self.screen.getmaxyx()
        failures = self._unavailable_hosts()
        self._safe_add(
            0,
            0,
            self.text["remote_details_title"],
            self._style(PAIR_WARNING, curses.A_BOLD),
        )
        self._safe_add(1, 0, self._remote_summary(), self._style(PAIR_WARNING))
        visible_rows = max(1, height - 4)
        maximum_offset = max(0, len(failures) - visible_rows)
        self.remote_details_offset = min(self.remote_details_offset, maximum_offset)
        visible = failures[
            self.remote_details_offset : self.remote_details_offset + visible_rows
        ]
        for index, (host, error) in enumerate(visible, start=3):
            line = f"! {host} — {error}"
            self._safe_add(
                index,
                0,
                truncate_display_text(line, max(1, width - 1)),
                self._style(PAIR_WARNING, curses.A_BOLD),
            )
        self._render_help_footer(self.text["remote_details_help"])
        self._set_cursor_visibility(False)
        self.screen.refresh()

    def _safe_add(self, row: int, column: int, value: str, attribute: int = 0) -> None:
        height, width = self.screen.getmaxyx()
        if row < 0 or row >= height or column < 0 or column >= width - 1:
            return
        available = width - column - 1
        try:
            self.screen.addnstr(
                row, column, truncate_display_text(value, available), available, attribute,
            )
        except curses.error:
            pass

    def _help_lines(self, value: str) -> list[tuple[ShortcutHelpSpan, ...]]:
        _height, width = self.screen.getmaxyx()
        return shortcut_help_spans(value, max(1, width - 1))

    def _render_help_footer(self, value: str) -> int:
        height, _width = self.screen.getmaxyx()
        lines = self._help_lines(value)
        start = max(0, height - len(lines))
        for offset, spans in enumerate(lines):
            column = 0
            for span in spans:
                attribute = (
                    self._style(PAIR_ACCENT, curses.A_BOLD)
                    if span.keycap
                    else curses.A_DIM
                )
                self._safe_add(start + offset, column, span.text, attribute)
                column += display_width(span.text)
        return len(lines)

    def _render_choice(self, row: int) -> int:
        self._safe_add(
            row,
            0,
            self.text["discover_question"],
            self._style(PAIR_ACCENT, curses.A_BOLD),
        )
        options = (
            self.text["discover_remote_option"],
            self.text["local_only_option"],
        )
        for index, label in enumerate(options):
            marker = "›" if index == self.discovery_option else " "
            attribute = self._style(
                PAIR_ACCENT if index == self.discovery_option else 0,
                curses.A_REVERSE | curses.A_BOLD
                if index == self.discovery_option
                else 0,
            )
            self._safe_add(row + 2 + index, 2, f"{marker} {label}", attribute)
        return row + 5

    def _render_mode_choice(self, row: int) -> int:
        self._safe_add(
            row,
            0,
            self.text["mode_question"],
            self._style(PAIR_ACCENT, curses.A_BOLD),
        )
        options = (
            (
                self.text["delegate_option"],
                self.text["delegate_privacy"],
            ),
            (
                self.text["direct_option"],
                self.text["direct_privacy"],
            ),
        )
        _height, width = self.screen.getmaxyx()
        option_row = row + 2
        for index, (label, description) in enumerate(options):
            marker = "›" if index == self.mode_option else " "
            attribute = self._style(
                PAIR_ACCENT if index == self.mode_option else 0,
                curses.A_REVERSE | curses.A_BOLD
                if index == self.mode_option
                else curses.A_BOLD,
            )
            label_lines = wrap_display_text(label, width - 6)
            for line_index, line in enumerate(label_lines):
                prefix = f"{marker} " if line_index == 0 else "  "
                self._safe_add(
                    option_row + line_index,
                    2,
                    f"{prefix}{line}",
                    attribute,
                )
            option_row += len(label_lines)
            description_lines = wrap_display_text(description, width - 7)
            for line in description_lines:
                self._safe_add(option_row, 6, line, curses.A_DIM)
                option_row += 1
            option_row += 1
        return option_row

    def _render_mode_summary(self, row: int) -> int:
        if self.delivery_mode == DELIVERY_DIRECT:
            label = self.text["direct_option"]
            description = self.text["direct_privacy"]
            pair = PAIR_WARNING
        else:
            label = self.text["delegate_option"]
            description = self.text["delegate_privacy"]
            pair = PAIR_SUCCESS
        _height, width = self.screen.getmaxyx()
        heading = f'{self.text["delivery_mode"]}: {label}'
        for line in wrap_display_text(heading, width - 1):
            self._safe_add(row, 0, line, self._style(pair, curses.A_BOLD))
            row += 1
        for line in wrap_display_text(description, width - 3):
            self._safe_add(row, 2, line, curses.A_DIM)
            row += 1
        return row

    def _render_recipients(self, row: int, available_rows: int) -> int:
        _height, width = self.screen.getmaxyx()
        records = self.filtered_agents()
        selected_count = len(self.selected)
        list_rows = max(1, available_rows - 2)
        visible, has_more = self._visible_recipient_rows(records, list_rows)
        visible_agent_indexes = [
            view_row.agent_index
            for view_row in visible
            if view_row.kind == "agent"
        ]
        if visible_agent_indexes:
            first = min(visible_agent_indexes) + 1
            last = max(visible_agent_indexes) + 1
            before = "↑" if first > 1 else ""
            after = "↓" if has_more else ""
            position = f"{before}{first}-{last}/{len(records)}{after}"
        else:
            position = "0/0"
        heading = (
            f'{self.text["recipients"]}: {selected_count} '
            f'{self.text["selected"]} · {position}'
        )
        attribute = self._style(
            PAIR_ACCENT,
            curses.A_REVERSE if self.section == "recipients" else curses.A_BOLD,
        )
        self._safe_add(row, 0, heading, attribute)
        row += 1
        self._safe_add(
            row,
            0,
            f'{self.text["search"]}: {self.search}',
            self._style(PAIR_ACCENT),
        )
        row += 1

        for visible_index, view_row in enumerate(visible):
            line_row = row + visible_index
            if view_row.kind != "agent":
                group_label, group_count = self._group_header_parts(
                    view_row,
                    max(1, width - 1),
                )
                header_attribute = self._style(PAIR_ACCENT, curses.A_BOLD)
                self._safe_add(line_row, 0, group_label, header_attribute)
                if group_count:
                    self._safe_add(
                        line_row,
                        display_width(group_label),
                        group_count,
                        self._style(PAIR_ACCENT, curses.A_DIM),
                    )
                continue
            agent = view_row.agent
            absolute_index = view_row.agent_index
            marker = "x" if agent.identity in self.selected else " "
            disabled = agent.stale
            state_label = (
                self.text["stale"]
                if disabled
                else self.text.get(f"status_{agent.status}", agent.status)
            )
            state_glyph = "~" if disabled else STATUS_GLYPHS.get(agent.status, "?")
            state = f"{state_glyph} {state_label}"
            focused = absolute_index == self.cursor and self.section == "recipients"
            line = self._recipient_line(
                agent,
                marker,
                state,
                focused=focused,
                indent=view_row.indent,
            )
            if disabled:
                line_attribute = curses.A_DIM
            elif agent.identity in self.selected:
                line_attribute = curses.A_BOLD
            else:
                line_attribute = 0
            self._safe_add(line_row, 0, line, line_attribute)
            if focused:
                self._safe_add(
                    line_row,
                    view_row.indent,
                    "›",
                    self._style(PAIR_ACCENT, curses.A_BOLD),
                )
            if agent.identity in self.selected:
                self._safe_add(
                    line_row,
                    view_row.indent + 2,
                    "[x]",
                    self._style(PAIR_ACCENT, curses.A_BOLD),
                )
            status_column = display_width(line) - display_width(state)
            self._safe_add(
                line_row,
                status_column,
                state,
                self._status_attribute(agent),
            )
        return row + list_rows

    def _render_message(self, row: int, available_rows: int) -> int:
        attribute = self._style(
            PAIR_ACCENT,
            curses.A_REVERSE if self.section == "message" else curses.A_BOLD,
        )
        self._safe_add(row, 0, self.text["message"], attribute)
        row += 1
        visible_rows = max(2, available_rows - 1)
        _height, width = self.screen.getmaxyx()
        wrapped_lines, cursor_row, cursor_column = wrap_message_lines(
            self.message_lines,
            width=self._message_editor_width(),
            cursor_row=self.message_row,
            cursor_column=self.message_column,
        )
        start = max(0, cursor_row - visible_rows + 1)
        visible_lines = wrapped_lines[start : start + visible_rows]
        thumb = scrollbar_thumb(len(wrapped_lines), visible_rows, start)
        scrollbar_column = width - 2
        for offset in range(visible_rows):
            marker = "│"
            if offset == 0 and start > 0:
                marker = "↑"
            elif (
                offset == visible_rows - 1
                and start + visible_rows < len(wrapped_lines)
            ):
                marker = "↓"
            self._safe_add(
                row + offset,
                0,
                marker,
                self._style(PAIR_ACCENT, curses.A_DIM),
            )
            line = visible_lines[offset].text if offset < len(visible_lines) else ""
            if not line and offset == 0 and self.message_lines == [""]:
                self._safe_add(
                    row + offset,
                    2,
                    self.text["message_placeholder"],
                    curses.A_DIM,
                )
            else:
                self._safe_add(row + offset, 2, line)
            if thumb is not None and scrollbar_column > 2:
                thumb_start, thumb_size = thumb
                scrollbar_glyph = (
                    "█"
                    if thumb_start <= offset < thumb_start + thumb_size
                    else "│"
                )
                self._safe_add(
                    row + offset,
                    scrollbar_column,
                    scrollbar_glyph,
                    self._style(PAIR_ACCENT, curses.A_DIM),
                )
        if self.section == "message":
            self.message_cursor = (row + cursor_row - start, 2 + cursor_column)
        return row + visible_rows

    def render(self) -> None:
        if self.remote_details_visible:
            self.message_cursor = None
            self._render_remote_details()
            return
        self.screen.erase()
        self.message_cursor = None
        height, _width = self.screen.getmaxyx()
        self._safe_add(
            0,
            0,
            self.text["send_prompt"],
            self._style(PAIR_ACCENT, curses.A_BOLD),
        )
        self._safe_add(
            1,
            0,
            f'{self.text["coordinator"]}: {self.sender.qualified_name}',
            self._style(PAIR_SUCCESS),
        )
        row = 3

        if not self.discovery_choice:
            self._render_choice(row)
            self._render_help_footer(self.text["help_discovery"])
            self._set_cursor_visibility(False)
            self.screen.refresh()
            return

        if not self.mode_choice:
            self._render_mode_choice(row)
            self._render_help_footer(self.text["help_mode"])
            self._set_cursor_visibility(False)
            self.screen.refresh()
            return

        if self.sending:
            help_text = self.text["help_sending"]
        elif self.section == "message":
            help_text = self.text["help_message"]
        else:
            help_text = self.text["help_recipients"]
        help_lines = self._help_lines(help_text)
        reserved_footer = 2 + len(help_lines)
        row = self._render_mode_summary(row)
        content_height = max(6, height - row - reserved_footer)
        filtered_records = self.filtered_agents()
        recipient_height = self._recipient_panel_height(
            len(self._recipient_view_rows(filtered_records)),
            content_height,
        )
        row = self._render_recipients(row, recipient_height)
        self._render_message(row, max(3, content_height - recipient_height))

        if self.remote_enabled:
            _available, _refreshing, unavailable = self._remote_counts()
            self._safe_add(
                height - len(help_lines) - 2,
                0,
                self._remote_summary(),
                self._style(
                    PAIR_WARNING if unavailable else PAIR_ACCENT,
                    curses.A_BOLD if unavailable else curses.A_DIM,
                ),
            )
        self._safe_add(
            height - len(help_lines) - 1,
            0,
            self.status,
            self._style(PAIR_WARNING, curses.A_BOLD),
        )
        self._render_help_footer(help_text)
        if self.sending:
            self.message_cursor = None
        self._set_cursor_visibility(self.message_cursor is not None)
        if self.message_cursor is not None:
            try:
                self.screen.move(*self.message_cursor)
            except curses.error:
                pass
        self.screen.refresh()

    def _set_cursor_visibility(self, visible: bool) -> None:
        try:
            curses.curs_set(2 if visible else 0)
        except curses.error:
            pass
