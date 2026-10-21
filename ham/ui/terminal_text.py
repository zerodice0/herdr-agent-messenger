"""Pure terminal-cell text measurement and wrapping helpers."""

from __future__ import annotations

from dataclasses import dataclass
from itertools import combinations
import re
from typing import Sequence
import unicodedata


@dataclass(frozen=True)
class WrappedMessageLine:
    text: str
    logical_row: int
    start: int
    end: int


@dataclass(frozen=True)
class ShortcutHelpSpan:
    text: str
    keycap: bool = False


def display_width(value: str) -> int:
    return sum(
        0
        if unicodedata.combining(character)
        else 2
        if unicodedata.east_asian_width(character) in ("F", "W")
        else 1
        for character in value
    )


def wrap_message_lines(
    lines: Sequence[str],
    *,
    width: int,
    cursor_row: int,
    cursor_column: int,
) -> tuple[list[WrappedMessageLine], int, int]:
    """Soft-wrap logical message lines and map the logical cursor to the result."""

    width = max(1, width)
    wrapped: list[WrappedMessageLine] = []
    visual_cursor_row = 0
    visual_cursor_column = 0

    for logical_row, line in enumerate(lines):
        segments: list[WrappedMessageLine] = []
        start = 0
        current_width = 0
        for index, character in enumerate(line):
            character_width = display_width(character)
            if index > start and current_width + character_width > width:
                segments.append(
                    WrappedMessageLine(line[start:index], logical_row, start, index)
                )
                start = index
                current_width = 0
            current_width += character_width
        segments.append(
            WrappedMessageLine(line[start:], logical_row, start, len(line))
        )

        for segment_index, segment in enumerate(segments):
            visual_index = len(wrapped)
            wrapped.append(segment)
            cursor_in_segment = (
                logical_row == cursor_row
                and (
                    segment.start <= cursor_column < segment.end
                    or (
                        cursor_column == segment.end
                        and (
                            segment.end == len(line)
                            or segment_index == len(segments) - 1
                        )
                    )
                )
            )
            if cursor_in_segment:
                visual_cursor_row = visual_index
                visual_cursor_column = display_width(
                    line[segment.start:cursor_column]
                )

    return wrapped, visual_cursor_row, visual_cursor_column


def scrollbar_thumb(
    total_rows: int,
    visible_rows: int,
    start: int,
) -> tuple[int, int] | None:
    """Return the scrollbar thumb offset and size for a vertical viewport."""

    if visible_rows <= 0 or total_rows <= visible_rows:
        return None
    thumb_size = max(
        1,
        min(
            visible_rows,
            (visible_rows * visible_rows + total_rows - 1) // total_rows,
        ),
    )
    scroll_range = total_rows - visible_rows
    thumb_range = visible_rows - thumb_size
    clamped_start = min(max(0, start), scroll_range)
    thumb_start = (
        clamped_start * thumb_range + scroll_range // 2
    ) // scroll_range
    return thumb_start, thumb_size


def character_index_at_display_column(value: str, column: int) -> int:
    """Map a terminal-cell column to the nearest character boundary."""

    current_width = 0
    for index, character in enumerate(value):
        character_width = display_width(character)
        if current_width + character_width > column:
            return index
        current_width += character_width
    return len(value)


def wrap_display_text(value: str, width: int) -> list[str]:
    wrapped, _cursor_row, _cursor_column = wrap_message_lines(
        [value],
        width=max(1, width),
        cursor_row=0,
        cursor_column=len(value),
    )
    return [line.text for line in wrapped]


def wrap_help_text(value: str, width: int) -> list[str]:
    """Return the plain-text form of keycap-styled shortcut help lines."""

    return [
        "".join(span.text for span in line)
        for line in shortcut_help_spans(value, width)
    ]


def shortcut_help_spans(
    value: str,
    width: int,
) -> list[tuple[ShortcutHelpSpan, ...]]:
    """Lay out shortcut items while keeping each key and action together."""

    width = max(1, width)
    items: list[tuple[ShortcutHelpSpan, ...]] = []
    for raw_item in re.split(r"\s{2,}", value.strip()):
        if not raw_item:
            continue
        parts = raw_item.split(maxsplit=1)
        key = parts[0]
        is_key = bool(
            key in {"↑↓", "Enter", "Space", "Tab", "Esc", "?"}
            or re.fullmatch(r"Ctrl\+[A-Za-z]", key)
            or re.fullmatch(r"[A-Z]", key)
        )
        if len(parts) == 1 or not is_key:
            items.append((ShortcutHelpSpan(parts[0]),))
            if len(parts) > 1:
                items[-1] = (ShortcutHelpSpan(raw_item),)
            continue
        label = parts[1]
        items.append(
            (
                ShortcutHelpSpan(f"[{key}]", keycap=True),
                ShortcutHelpSpan(f" {label}"),
            )
        )

    if not items:
        return [(ShortcutHelpSpan(""),)]

    item_widths = [sum(display_width(span.text) for span in item) for item in items]

    def segment_width(start: int, end: int) -> int:
        return sum(item_widths[start:end]) + max(0, end - start - 1)

    minimum_lines = 1
    current_width = 0
    for item_width in item_widths:
        if current_width and current_width + 1 + item_width > width:
            minimum_lines += 1
            current_width = item_width
        else:
            current_width += (1 if current_width else 0) + item_width

    best_boundaries: tuple[int, ...] | None = None
    best_score: tuple[int, int] | None = None
    for cuts in combinations(range(1, len(items)), minimum_lines - 1):
        boundaries = (0, *cuts, len(items))
        widths = [
            segment_width(boundaries[index], boundaries[index + 1])
            for index in range(minimum_lines)
        ]
        if any(line_width > width for line_width in widths):
            continue
        score = (max(widths), max(widths) - min(widths))
        if best_score is None or score < best_score:
            best_score = score
            best_boundaries = boundaries

    if best_boundaries is None:
        best_boundaries = (0, len(items))

    lines: list[tuple[ShortcutHelpSpan, ...]] = []
    for boundary_index in range(len(best_boundaries) - 1):
        start, end = best_boundaries[boundary_index : boundary_index + 2]
        spans: list[ShortcutHelpSpan] = []
        for item_index in range(start, end):
            if spans:
                spans.append(ShortcutHelpSpan(" "))
            spans.extend(items[item_index])
        lines.append(tuple(spans))
    return lines


def truncate_display_text(value: str, width: int) -> str:
    if width <= 0:
        return ""
    if display_width(value) <= width:
        return value
    if width == 1:
        return "…"
    output = ""
    for character in value:
        if display_width(output + character) > width - 1:
            break
        output += character
    return output + "…"


def pad_display_text(value: str, width: int) -> str:
    truncated = truncate_display_text(value, width)
    return truncated + " " * max(0, width - display_width(truncated))
