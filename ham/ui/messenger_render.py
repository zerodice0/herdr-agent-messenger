"""Curses rendering behavior shared by the messenger application controller."""

from __future__ import annotations

import curses

from ham.agents.directory import AgentRecord
from ham.ui.recipient_view import RecipientViewRow
from ham.ui.terminal_text import (
    display_width,
    scrollbar_thumb,
    truncate_display_text,
    wrap_display_text,
    wrap_message_lines,
)
from ham.ui.tui_style import Hint, content_rect, draw_footer, draw_status, draw_title, footer_lines


MESSAGE_MIN_VISIBLE_ROWS = 4
DELIVERY_DELEGATE = "delegate"
DELIVERY_DIRECT = "direct"


def _help_hints(value: str) -> list[Hint]:
    """Keep the existing localized key/action copy in the shared footer grammar."""
    priorities = {"Esc": 0, "Ctrl+S": 1, "Enter": 2, "Ctrl+G": 3, "Tab": 4}
    hints = []
    for item in value.split("  "):
        key, separator, action = item.strip().partition(" ")
        if not separator:
            continue
        if action.startswith("/ "):
            alternate, _, action = action[2:].partition(" ")
            key = f"{key}/{alternate}"
        if key.endswith("..."):
            continue
        hints.append(Hint(key, action, priorities.get(key, 100)))
    return hints


class MessengerRenderMixin:
    """Render a controller that provides messenger state and view helpers."""

    def _content_rect(self):
        return content_rect(*self.screen.getmaxyx(), 104)

    def _content_size(self) -> tuple[int, int]:
        rect = self._content_rect()
        return rect.height, rect.width

    def _message_editor_width(self) -> int:
        _height, width = self._content_size()
        return max(1, min(88, width - 5))

    def _status_attribute(self, agent: AgentRecord, *, selected: bool = False) -> int:
        return self.theme.turn_style(agent.status, selected=selected, stale=agent.stale)

    @staticmethod
    def _recipient_panel_height(record_count: int, content_height: int) -> int:
        message_panel_height = 1 + MESSAGE_MIN_VISIBLE_ROWS
        maximum = max(3, content_height - message_panel_height)
        desired = 2 + max(1, record_count)
        return max(3, min(desired, maximum))

    def _render_remote_details(self) -> None:
        self.screen.erase()
        self.content_screen = self._content_rect().window(self.screen)
        height, width = self._content_size()
        failures = self._unavailable_hosts()
        draw_title(
            self.content_screen, self.theme, "Send Prompt",
            f"coordinator {self.sender.qualified_name} · {len(self.selected)} selected",
            width,
        )
        self._safe_add(2, 0, self._remote_summary(), self.theme.style("warning"))
        footer_rows = len(self._help_lines(self.text["remote_details_help"]))
        visible_rows = max(1, height - 3 - footer_rows)
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
                self.theme.style("warning", bold=True),
            )
        self._render_help_footer(self.text["remote_details_help"])
        self._set_cursor_visibility(False)
        self.screen.refresh()

    def _safe_add(self, row: int, column: int, value: str, attribute: int = 0) -> None:
        height, width = self.content_screen.getmaxyx()
        if row < 0 or row >= height or column < 0 or column >= width:
            return
        available = width - column
        try:
            self.content_screen.addnstr(
                row, column, truncate_display_text(value, available), available, attribute,
            )
        except curses.error:
            pass

    def _help_lines(self, value: str) -> list[list[Hint]]:
        _height, width = self._content_size()
        return footer_lines(_help_hints(value), width, max_rows=2)

    def _render_help_footer(self, value: str, *, row: int | None = None) -> int:
        height, width = self._content_size()
        hints = _help_hints(value)
        rows = len(footer_lines(hints, width, max_rows=2))
        start = height - rows if row is None else min(row, max(0, height - rows))
        return draw_footer(
            self.content_screen, start, 0, width, hints, self.theme, max_rows=2,
        )

    def _render_choice(self, row: int) -> int:
        self._safe_add(
            row,
            0,
            self.text["discover_question"],
            self.theme.style("accent", bold=True),
        )
        options = (
            self.text["discover_remote_option"],
            self.text["local_only_option"],
        )
        _height, width = self._content_size()
        for index, label in enumerate(options):
            if index == self.discovery_option:
                self.theme.draw_selected_row(
                    self.content_screen, row + 2 + index, label, width - 2, x=2,
                )
            else:
                self._safe_add(row + 2 + index, 2, f"  {label}")
        return row + 5

    def _render_mode_choice(self, row: int) -> int:
        if self.mode_return_to_editor:
            self._safe_add(
                row, 0, self.text["mode_change_context"].format(count=len(self.selected)),
            )
            row += 2
        self._safe_add(
            row,
            0,
            self.text["mode_question"],
            self.theme.style("accent", bold=True),
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
        _height, width = self._content_size()
        option_row = row + 2
        for index, (label, description) in enumerate(options):
            label_lines = wrap_display_text(label, width - 6)
            for line_index, line in enumerate(label_lines):
                if index == self.mode_option:
                    self.theme.draw_selected_row(
                        self.content_screen, option_row + line_index, line,
                        width - 2, x=2, marker="› " if line_index == 0 else "  ",
                    )
                else:
                    self._safe_add(
                        option_row + line_index, 2, f"  {line}",
                        self.theme.style(bold=True),
                    )
            option_row += len(label_lines)
            description_lines = wrap_display_text(description, width - 7)
            for line in description_lines:
                self._safe_add(option_row, 6, line)
                option_row += 1
            option_row += 1
        return option_row

    def _render_mode_summary(self, row: int) -> int:
        if self.delivery_mode == DELIVERY_DIRECT:
            label = self.text["direct_option"]
            description = self.text["direct_privacy"]
        else:
            label = self.text["delegate_option"]
            description = self.text["delegate_privacy"]
        _height, width = self._content_size()
        heading = f'{self.text["delivery_mode"]}: {label}'
        for line in wrap_display_text(heading, width - 1):
            self._safe_add(row, 0, line, self.theme.style(bold=True))
            row += 1
        for line in wrap_display_text(description, width - 3):
            self._safe_add(row, 2, line)
            row += 1
        return row

    def _render_recipients(self, row: int, available_rows: int) -> int:
        _height, width = self._content_size()
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
        attribute = self.theme.style("accent", bold=True)
        self._safe_add(row, 0, heading, attribute)
        row += 1
        self._safe_add(
            row,
            0,
            f'{self.text["search"]}: {self.search}',
            self.theme.style("accent"),
        )
        row += 1

        for visible_index, view_row in enumerate(visible):
            line_row = row + visible_index
            if view_row.kind != "agent":
                group_label, group_count = self._group_header_parts(
                    view_row,
                    max(1, width - 1),
                )
                header_attribute = self.theme.style("accent", bold=True)
                self._safe_add(line_row, 0, group_label, header_attribute)
                if group_count:
                    self._safe_add(
                        line_row,
                        display_width(group_label),
                        group_count,
                        self.theme.style("muted"),
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
            state = f"~ {state_label}" if disabled else (
                self.theme.turn_label(agent.status).split(" ", 1)[0] + f" {state_label}"
            )
            focused = absolute_index == self.cursor and self.section == "recipients"
            line = self._recipient_line(
                agent,
                marker,
                state,
                focused=focused,
                indent=view_row.indent,
            )
            if disabled:
                line_attribute = self.theme.style("muted")
            elif agent.identity in self.selected:
                line_attribute = self.theme.style(bold=True)
            else:
                line_attribute = 0
            if focused:
                self.theme.draw_selected_row(
                    self.content_screen, line_row, line, width, marker="",
                )
            else:
                self._safe_add(line_row, 0, line, line_attribute)
            if agent.identity in self.selected:
                self._safe_add(
                    line_row,
                    view_row.indent + 2,
                    "[x]",
                    self.theme.style("accent", bold=True, selected=focused),
                )
            status_column = display_width(line) - display_width(state)
            self._safe_add(
                line_row,
                status_column,
                state,
                self._status_attribute(agent, selected=focused),
            )
        return row + list_rows

    def _render_message(self, row: int, available_rows: int) -> int:
        attribute = self.theme.style(
            "accent", bold=True, selected=self.section == "message",
        )
        self._safe_add(row, 0, self.text["message"], attribute)
        row += 1
        visible_rows = max(2, available_rows - 1)
        _height, width = self._content_size()
        wrapped_lines, cursor_row, cursor_column = wrap_message_lines(
            self.message_lines,
            width=self._message_editor_width(),
            cursor_row=self.message_row,
            cursor_column=self.message_column,
        )
        start = max(0, cursor_row - visible_rows + 1)
        visible_lines = wrapped_lines[start : start + visible_rows]
        thumb = scrollbar_thumb(len(wrapped_lines), visible_rows, start)
        scrollbar_column = min(width - 2, self._message_editor_width() + 3)
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
                self.theme.style("muted"),
            )
            line = visible_lines[offset].text if offset < len(visible_lines) else ""
            if not line and offset == 0 and self.message_lines == [""]:
                self._safe_add(
                    row + offset,
                    2,
                    self.text["message_placeholder"],
                    self.theme.style("muted"),
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
                    self.theme.style("muted"),
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
        self.content_screen = self._content_rect().window(self.screen)
        height, width = self._content_size()
        draw_title(
            self.content_screen, self.theme, "Send Prompt",
            f"coordinator {self.sender.qualified_name} · {len(self.selected)} selected",
            width,
        )
        row = 2

        if not self.discovery_choice:
            row = self._render_choice(row)
            self._render_help_footer(self.text["help_discovery"], row=row)
            self._set_cursor_visibility(False)
            self.screen.refresh()
            return

        if not self.mode_choice:
            row = self._render_mode_choice(row)
            self._render_help_footer(self.text["help_mode"], row=row)
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
            draw_status(
                self.content_screen, height - len(help_lines) - 2, 0, width,
                self._remote_summary(), self.theme,
                kind="warning" if unavailable else "info",
            )
        draw_status(
            self.content_screen, height - len(help_lines) - 1, 0, width,
            self.status, self.theme, kind=self.status_kind,
        )
        self._render_help_footer(help_text)
        if self.sending:
            self.message_cursor = None
        self._set_cursor_visibility(self.message_cursor is not None)
        if self.message_cursor is not None:
            try:
                self.content_screen.move(*self.message_cursor)
            except curses.error:
                pass
        self.screen.refresh()

    def _set_cursor_visibility(self, visible: bool) -> None:
        try:
            curses.curs_set(2 if visible else 0)
        except curses.error:
            pass
