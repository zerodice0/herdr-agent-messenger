#!/usr/bin/env python3

from __future__ import annotations

from collections import deque
import contextlib
import curses
import sys
from typing import Callable, Iterator, TextIO


KITTY_PUSH = "\x1b[>1u"
KITTY_POP = "\x1b[<u"
NEWLINE = "newline"
ESCAPE = "escape"
SHIFT_TAB = "shift-tab"
F1 = "f1"


def _write_keyboard_mode(stream: TextIO, sequence: str) -> None:
    try:
        stream.write(sequence)
        stream.flush()
    except (OSError, ValueError):
        pass


@contextlib.contextmanager
def kitty_keyboard(stream: TextIO | None = None, *, enabled: bool = True) -> Iterator[None]:
    """Enable kitty key disambiguation on a tty and restore it on every exit."""
    if stream is None:
        stream = sys.__stdout__
    try:
        active = enabled and stream is not None and stream.isatty()
    except (OSError, ValueError):
        active = False
    if not active:
        yield
        return
    try:
        _write_keyboard_mode(stream, KITTY_PUSH)
        yield
    finally:
        _write_keyboard_mode(stream, KITTY_POP)


class KeyDecoder:
    def __init__(self, read: Callable[[], str | int | None]) -> None:
        self._read = read
        self._pending: deque[str | int] = deque()

    def _unit(self) -> str | int | None:
        return self._pending.popleft() if self._pending else self._read()

    def next(self) -> str | int | None:
        """Return one logical key, ignoring unknown complete escape sequences."""
        while True:
            unit = self._unit()
            if isinstance(unit, int):
                return {
                    curses.KEY_BTAB: SHIFT_TAB,
                    curses.KEY_F0 + 1: F1,
                    curses.KEY_ENTER: "\r",
                }.get(unit, unit)
            if unit != "\x1b":
                return unit
            following = self._unit()
            if following is None:
                return ESCAPE
            if following in ("\r", "\n"):
                return NEWLINE
            if following not in ("[", "O"):
                self._pending.appendleft(following)
                return ESCAPE
            sequence = self._sequence()
            if sequence is None:
                return ESCAPE
            decoded = self._decode(following, sequence)
            if decoded is not None:
                return decoded

    def _sequence(self) -> str | None:
        parts: list[str] = []
        intermediates = False
        for _ in range(32):
            unit = self._unit()
            if unit is None:
                return None
            if not isinstance(unit, str) or len(unit) != 1:
                self._pending.appendleft(unit)
                return None
            code = ord(unit)
            if 0x40 <= code <= 0x7e:
                parts.append(unit)
                return "".join(parts)
            if 0x20 <= code <= 0x2f:
                intermediates = True
            elif not (0x30 <= code <= 0x3f and not intermediates):
                self._pending.appendleft(unit)
                return None
            parts.append(unit)
        return None

    @staticmethod
    def _decode(prefix: str, sequence: str) -> str | int | None:
        arrows = {
            "A": curses.KEY_UP,
            "B": curses.KEY_DOWN,
            "C": curses.KEY_RIGHT,
            "D": curses.KEY_LEFT,
            "H": curses.KEY_HOME,
            "F": curses.KEY_END,
            "P": F1,
        }
        if sequence in arrows:
            return arrows[sequence]
        if prefix != "[":
            return None
        if sequence == "Z":
            return SHIFT_TAB
        final = sequence[-1]
        try:
            params = [int(part.split(":", 1)[0]) for part in sequence[:-1].split(";")]
        except ValueError:
            return None
        modifier = params[1] if len(params) > 1 else 1
        modifier = ((modifier - 1) & ~(64 | 128)) + 1
        if final == "u" and len(params) in (1, 2):
            codepoint = params[0]
        elif final == "~" and len(params) == 3 and params[0] == 27:
            codepoint = params[2]
        elif final == "~" and len(params) == 2 and params[0] == 13:
            return NEWLINE if modifier == 2 else None
        elif final == "~" and len(params) == 1:
            return {
                2: curses.KEY_IC,
                3: curses.KEY_DC,
                5: curses.KEY_PPAGE,
                6: curses.KEY_NPAGE,
                11: F1,
            }.get(params[0])
        else:
            return None
        if codepoint == 13 and modifier in ((2, 3, 4) if final == "u" else (2, 3)):
            return NEWLINE
        if final == "u":
            if codepoint == 9 and modifier == 2:
                return SHIFT_TAB
            if modifier == 1:
                special = {9: "\t", 13: "\r", 27: ESCAPE, 127: curses.KEY_BACKSPACE}
                if codepoint in special:
                    return special[codepoint]
        if not 0 <= codepoint <= 0x10ffff:
            return None
        character = chr(codepoint)
        if modifier == 5 and character.isascii() and character.isalpha():
            return chr(ord(character.lower()) & 0x1f)
        if final == "u" and modifier == 1 and character.isprintable():
            return character
        return None
