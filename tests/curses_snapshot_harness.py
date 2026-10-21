"""Deterministic, terminal-free screen capture helpers for curses tests.

The fake window models terminal cells (including wide Unicode characters) and
keeps the individual draw calls.  Tests can therefore assert semantic regions
and color roles without depending on a brittle full-screen golden string.
"""

from __future__ import annotations

from dataclasses import dataclass
import difflib
import unittest
from typing import Iterable, Mapping

import curses

from ham.ui.messenger import display_width


COLOR_PAIR_SHIFT = 8


def deterministic_color_pair(pair: int) -> int:
    """Encode a curses color pair without requiring ``initscr()``."""

    return pair << COLOR_PAIR_SHIFT


def color_pair_from_attribute(attribute: int) -> int:
    return (attribute >> COLOR_PAIR_SHIFT) & 0xFF


def clip_display_text(value: str, width: int) -> str:
    """Clip to complete terminal cells, never splitting a wide character."""

    if width <= 0:
        return ""
    output = ""
    used = 0
    for character in value:
        character_width = display_width(character)
        if used + character_width > width:
            break
        output += character
        used += character_width
    return output


@dataclass(frozen=True)
class DrawCall:
    row: int
    column: int
    requested_text: str
    visible_text: str
    attribute: int
    character_limit: int

    @property
    def end_column(self) -> int:
        return self.column + display_width(self.visible_text)

    @property
    def clipped(self) -> bool:
        return self.visible_text != self.requested_text


@dataclass
class _Cell:
    text: str = " "
    attribute: int = 0
    continuation: bool = False


class FakeCursesWindow:
    """A small subset of ``curses.window`` used by ``MessengerApp.render``."""

    def __init__(self, height: int, width: int) -> None:
        if height <= 0 or width <= 1:
            raise ValueError("a fake curses window needs positive usable dimensions")
        self.height = height
        self.width = width
        self.draw_calls: list[DrawCall] = []
        self.move_calls: list[tuple[int, int]] = []
        self.refresh_count = 0
        self.erase_count = 0
        self._cells: list[list[_Cell]] = []
        self._reset_cells()

    def _reset_cells(self) -> None:
        self._cells = [
            [_Cell() for _column in range(self.width)]
            for _row in range(self.height)
        ]

    def getmaxyx(self) -> tuple[int, int]:
        return self.height, self.width

    def erase(self) -> None:
        self.erase_count += 1
        self.draw_calls.clear()
        self.move_calls.clear()
        self._reset_cells()

    def refresh(self) -> None:
        self.refresh_count += 1

    def move(self, row: int, column: int) -> None:
        if not (0 <= row < self.height and 0 <= column < self.width):
            raise curses.error("cursor outside fake window")
        self.move_calls.append((row, column))

    def addnstr(
        self,
        row: int,
        column: int,
        value: str,
        character_limit: int,
        attribute: int = 0,
    ) -> None:
        if not (0 <= row < self.height and 0 <= column < self.width):
            raise curses.error("write outside fake window")

        character_limited = value[: max(0, character_limit)]
        # MessengerApp intentionally leaves the final terminal column unused so
        # a real curses window does not raise while touching its lower-right cell.
        available_cells = max(0, self.width - column - 1)
        visible = clip_display_text(character_limited, available_cells)
        self.draw_calls.append(
            DrawCall(
                row,
                column,
                value,
                visible,
                attribute,
                character_limit,
            )
        )
        self._write_cells(row, column, visible, attribute)

    def _write_cells(
        self,
        row: int,
        column: int,
        value: str,
        attribute: int,
    ) -> None:
        current = column
        for character in value:
            character_width = display_width(character)
            if character_width == 0:
                previous = max(column, current - 1)
                self._cells[row][previous].text += character
                continue
            if current + character_width > self.width - 1:
                break
            self._cells[row][current] = _Cell(character, attribute, False)
            for offset in range(1, character_width):
                self._cells[row][current + offset] = _Cell("", attribute, True)
            current += character_width

    def snapshot(self) -> "ScreenSnapshot":
        return ScreenSnapshot(
            self.height,
            self.width,
            tuple(
                tuple(_Cell(cell.text, cell.attribute, cell.continuation) for cell in row)
                for row in self._cells
            ),
            tuple(self.draw_calls),
            tuple(self.move_calls),
            self.refresh_count,
        )


@dataclass(frozen=True)
class ScreenSnapshot:
    height: int
    width: int
    cells: tuple[tuple[_Cell, ...], ...]
    draw_calls: tuple[DrawCall, ...]
    move_calls: tuple[tuple[int, int], ...]
    refresh_count: int

    def line(self, row: int) -> str:
        return "".join(
            "" if cell.continuation else cell.text
            for cell in self.cells[row]
        ).rstrip()

    def lines(self) -> list[str]:
        return [self.line(row) for row in range(self.height)]

    def rows_containing(self, value: str) -> list[int]:
        return [row for row, line in enumerate(self.lines()) if value in line]

    def calls_containing(self, value: str) -> list[DrawCall]:
        return [call for call in self.draw_calls if value in call.visible_text]

    def calls_at_row(self, row: int) -> list[DrawCall]:
        return [call for call in self.draw_calls if call.row == row]

    def formatted(self, rows: Iterable[int] | None = None) -> str:
        selected_rows = range(self.height) if rows is None else rows
        number_width = len(str(max(0, self.height - 1)))
        return "\n".join(
            f"{row:0{number_width}d}|{self.line(row)}"
            for row in selected_rows
            if 0 <= row < self.height
        )


class SnapshotTestCase(unittest.TestCase):
    """Meaning-oriented assertions with readable screen output on failure."""

    def assert_screen_lines(
        self,
        snapshot: ScreenSnapshot,
        expected: Mapping[int, str],
    ) -> None:
        expected_lines = [f"{row:02d}|{value}" for row, value in expected.items()]
        actual_lines = [f"{row:02d}|{snapshot.line(row)}" for row in expected]
        if actual_lines == expected_lines:
            return
        diff = "\n".join(
            difflib.unified_diff(
                expected_lines,
                actual_lines,
                fromfile="expected regions",
                tofile="captured regions",
                lineterm="",
            )
        )
        self.fail(f"screen region mismatch:\n{diff}\n\nFull capture:\n{snapshot.formatted()}")

    def assert_text_visible(
        self,
        snapshot: ScreenSnapshot,
        value: str,
        *,
        unclipped: bool = False,
    ) -> DrawCall:
        calls = snapshot.calls_containing(value)
        if not calls:
            self.fail(f"{value!r} was not visible:\n{snapshot.formatted()}")
        call = calls[0]
        if unclipped and call.clipped:
            self.fail(
                f"{value!r} came from a clipped draw call {call!r}:\n"
                f"{snapshot.formatted()}"
            )
        return call

    def assert_text_role(
        self,
        snapshot: ScreenSnapshot,
        value: str,
        *,
        pair: int = 0,
        attributes: int = 0,
    ) -> DrawCall:
        matching = [
            call
            for call in snapshot.calls_containing(value)
            if color_pair_from_attribute(call.attribute) == pair
            and call.attribute & attributes == attributes
        ]
        if not matching:
            visible = snapshot.calls_containing(value)
            self.fail(
                f"{value!r} did not have pair={pair}, attributes={attributes}; "
                f"matching text calls={visible!r}\n\n{snapshot.formatted()}"
            )
        return matching[0]

    def assert_capture_in_bounds(self, snapshot: ScreenSnapshot) -> None:
        invalid = [
            call
            for call in snapshot.draw_calls
            if call.row < 0
            or call.row >= snapshot.height
            or call.column < 0
            or call.end_column > snapshot.width - 1
        ]
        invalid_cursors = [
            cursor
            for cursor in snapshot.move_calls
            if not (
                0 <= cursor[0] < snapshot.height
                and 0 <= cursor[1] < snapshot.width
            )
        ]
        if invalid or invalid_cursors:
            self.fail(
                f"out-of-bounds draw calls={invalid!r}, cursors={invalid_cursors!r}\n"
                f"{snapshot.formatted()}"
            )
