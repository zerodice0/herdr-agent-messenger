"""Small, cell-aware drawing vocabulary shared by HAM's curses surfaces."""
from __future__ import annotations

import curses
from dataclasses import dataclass
import os
from typing import Mapping, Sequence

from ham.ui.terminal_text import display_width, truncate_display_text


@dataclass(frozen=True)
class Rect:
    y: int
    x: int
    height: int
    width: int

    def window(self, screen):
        return _ContentWindow(screen, self)


class _ContentWindow:
    """A coordinate view, also usable with the recording screens in unit tests."""
    def __init__(self, screen, rect: Rect):
        self.screen, self.rect = screen, rect

    def getmaxyx(self):
        return self.rect.height, self.rect.width

    def addstr(self, y, x, text, attr=0):
        if 0 <= y < self.rect.height and 0 <= x < self.rect.width:
            self.screen.addstr(y + self.rect.y, x + self.rect.x,
                               truncate_display_text(text, self.rect.width - x), attr)

    def addnstr(self, y, x, text, count, attr=0):
        self.addstr(y, x, text[:count], attr)

    def move(self, y, x):
        self.screen.move(y + self.rect.y, x + self.rect.x)

    def erase(self):
        for y in range(self.rect.height):
            try:
                self.addstr(y, 0, " " * self.rect.width)
            except curses.error:
                pass

    def __getattr__(self, name):
        return getattr(self.screen, name)


def content_rect(height: int, width: int, cap: int, *, top: int | None = None) -> Rect:
    gutter = 1 if width < 80 else 2
    columns = max(0, min(cap, width - 2 * gutter))
    y = min(max(0, height - 1), (2 if height >= 30 else 1 if height >= 12 else 0)
            if top is None else max(0, top))
    return Rect(y, max(0, (width - columns) // 2), max(0, height - y - (1 if height > 2 else 0)), columns)


class Theme:
    _PAIRS = {"accent": 1, "success": 2, "activity": 3, "warning": 3, "error": 4}
    _TURN_ROLES = {"idle": "text", "working": "activity", "done": "success", "blocked": "error"}
    _TURN_LABELS = {"idle": "○ idle", "working": "● working", "done": "✓ done", "blocked": "! blocked"}

    def __init__(self, environment: Mapping[str, str] | None = None):
        self.colors_enabled = False
        if "NO_COLOR" in (os.environ if environment is None else environment):
            return
        try:
            if not curses.has_colors():
                return
            curses.start_color()
            try:
                curses.use_default_colors()
                background = -1
            except curses.error:
                background = curses.COLOR_BLACK
            for pair, foreground in enumerate((curses.COLOR_CYAN, curses.COLOR_GREEN,
                                                curses.COLOR_YELLOW, curses.COLOR_RED), 1):
                curses.init_pair(pair, foreground, background)
            self.colors_enabled = True
        except curses.error:
            pass

    def style(self, role: str = "text", *, bold: bool = False, selected: bool = False) -> int:
        attr = curses.A_BOLD if bold else 0
        if selected or role == "selection":
            return attr | curses.A_REVERSE
        if role == "muted":
            attr |= curses.A_DIM
        pair = self._PAIRS.get(role)
        return attr | (curses.color_pair(pair) if pair and self.colors_enabled else 0)

    def turn_style(self, state: str, *, selected: bool = False, stale: bool = False) -> int:
        role = "muted" if stale else self._TURN_ROLES.get(state, "muted")
        return self.style(role, bold=state in ("working", "done", "blocked") and not stale,
                          selected=selected)

    @classmethod
    def turn_label(cls, state: str) -> str:
        return cls._TURN_LABELS.get(state, "? unknown")

    def status_style(self, kind: str = "info") -> int:
        return self.style({"info": "text", "progress": "activity"}.get(kind, kind),
                          bold=kind in ("warning", "error"))


@dataclass(frozen=True)
class Hint:
    key: str
    action: str
    priority: int = 100

    @property
    def text(self) -> str:
        return f"[{self.key}] {self.action}" if self.key else self.action


def footer_lines(hints: Sequence[Hint], width: int, max_rows: int = 2) -> list[list[Hint]]:
    """Preserve declaration order; shed lower-priority hints only when needed."""
    admitted = [index for index, hint in enumerate(hints)
                if display_width(hint.text) <= width]
    while admitted:
        rows: list[list[Hint]] = []
        used = 0
        for index in admitted:
            hint = hints[index]
            cells = display_width(hint.text)
            if rows and used + 2 + cells <= width:
                rows[-1].append(hint)
                used += 2 + cells
            else:
                rows.append([hint])
                used = cells
        if len(rows) <= max_rows:
            return rows
        admitted.remove(max(admitted, key=lambda index: (hints[index].priority, index)))
    return []


def _put(screen, y: int, x: int, text: str, attr: int = 0):
    height, width = screen.getmaxyx()
    if 0 <= y < height and 0 <= x < width:
        try:
            screen.addstr(y, x, truncate_display_text(text, max(0, width - x - int(y == height - 1))), attr)
        except curses.error:
            pass


def draw_footer(screen, y: int, x: int, width: int, hints: Sequence[Hint], theme: Theme,
                max_rows: int = 2) -> int:
    rows = footer_lines(hints, width, max_rows)
    for index, row in enumerate(rows):
        column = x
        for hint in row:
            if hint.key:
                key = f"[{hint.key}]"
                _put(screen, y + index, column, key, theme.style("accent", bold=True))
                column += display_width(key)
                _put(screen, y + index, column, " " + hint.action)
                column += 1 + display_width(hint.action) + 2
            else:
                _put(screen, y + index, column, hint.action)
                column += display_width(hint.action) + 2
    return len(rows)


def draw_status(screen, y: int, x: int, width: int, text: str, theme: Theme, kind: str = "info"):
    _put(screen, y, x, truncate_display_text(text, width), theme.status_style(kind))


def draw_empty(screen, y: int, x: int, width: int, text: str, theme: Theme,
               hints: Sequence[Hint] | None = None) -> int:
    draw_status(screen, y, x, width, text, theme)
    return 1 + (draw_footer(screen, y + 2, x, width, hints, theme) + 1 if hints else 0)
