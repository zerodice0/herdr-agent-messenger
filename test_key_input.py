#!/usr/bin/env python3

from __future__ import annotations

from collections import deque
import curses
import io
import unittest
from unittest import mock

from key_input import ESCAPE, F1, KITTY_POP, KITTY_PUSH, NEWLINE, SHIFT_TAB, KeyDecoder, kitty_keyboard


class FakeRead:
    def __init__(self, units: str | list[str | int | None]) -> None:
        self.units = deque(units)

    def __call__(self) -> str | int | None:
        return self.units.popleft() if self.units else None


class FakeTTY(io.StringIO):
    def __init__(self, *, tty: bool = True) -> None:
        super().__init__()
        self.tty = tty
        self.flushes = 0

    def isatty(self) -> bool:
        return self.tty

    def flush(self) -> None:
        self.flushes += 1
        super().flush()


class KeyDecoderTests(unittest.TestCase):
    def test_shift_and_alt_enter_insert_newline(self) -> None:
        sequences = (
            "\x1b[13;2u", "\x1b[13;3u", "\x1b[13;4u",
            "\x1b[27;2;13~", "\x1b[27;3;13~", "\x1b[13;2~",
            "\x1b\r", "\x1b\n",
        )
        for sequence in sequences:
            with self.subTest(sequence=repr(sequence)):
                self.assertEqual(KeyDecoder(FakeRead(sequence)).next(), NEWLINE)

    def test_unmodified_enter_remains_carriage_return(self) -> None:
        for units in ("\r", "\x1b[13u", "\x1b[13;1u", [curses.KEY_ENTER]):
            with self.subTest(units=repr(units)):
                self.assertEqual(KeyDecoder(FakeRead(units)).next(), "\r")

    def test_escape_kitty_and_timeout(self) -> None:
        for units in ("\x1b[27u", "\x1b[27;1u", ["\x1b", None]):
            with self.subTest(units=repr(units)):
                self.assertEqual(KeyDecoder(FakeRead(units)).next(), ESCAPE)

    def test_control_letters_kitty_and_modify_other_keys(self) -> None:
        for letter in "drcDRCj":
            for sequence in (f"\x1b[{ord(letter)};5u", f"\x1b[27;5;{ord(letter)}~"):
                with self.subTest(sequence=repr(sequence)):
                    self.assertEqual(
                        KeyDecoder(FakeRead(sequence)).next(),
                        chr(ord(letter.lower()) & 0x1f),
                    )

    def test_shift_tab(self) -> None:
        for units in ("\x1b[Z", "\x1b[9;2u", [curses.KEY_BTAB]):
            with self.subTest(units=repr(units)):
                self.assertEqual(KeyDecoder(FakeRead(units)).next(), SHIFT_TAB)

    def test_plain_text_digits_and_line_feed_pass_through(self) -> None:
        decoder = KeyDecoder(FakeRead("한글0123456789\n"))
        for character in "한글0123456789\n":
            self.assertEqual(decoder.next(), character)
        self.assertIsNone(decoder.next())

    def test_kitty_plain_printable_tab_and_backspace(self) -> None:
        for sequence, expected in (
            ("\x1b[54620u", "한"),
            ("\x1b[53;1u", "5"),
            ("\x1b[9u", "\t"),
            ("\x1b[127u", curses.KEY_BACKSPACE),
        ):
            with self.subTest(sequence=repr(sequence)):
                self.assertEqual(KeyDecoder(FakeRead(sequence)).next(), expected)

    def test_unknown_complete_sequences_are_skipped(self) -> None:
        for sequence in ("\x1b[999~", "\x1b[?1u", "\x1b[1 q", "\x1bOQ"):
            with self.subTest(sequence=repr(sequence)):
                decoder = KeyDecoder(FakeRead(sequence + "한"))
                self.assertEqual(decoder.next(), "한")
                self.assertIsNone(decoder.next())
        self.assertIsNone(KeyDecoder(FakeRead("\x1b[999~")).next())

    def test_truncated_sequence_discards_partial_before_timeout(self) -> None:
        for partial in ("\x1b[", "\x1b[13;", "\x1bO", "\x1b[1 "):
            with self.subTest(partial=repr(partial)):
                decoder = KeyDecoder(FakeRead(list(partial) + [None, "x"]))
                self.assertEqual(decoder.next(), ESCAPE)
                self.assertEqual(decoder.next(), "x")
                self.assertIsNone(decoder.next())

    def test_units_after_escape_are_not_lost(self) -> None:
        for following in ("x", "한", curses.KEY_UP):
            with self.subTest(following=following):
                decoder = KeyDecoder(FakeRead(["\x1b", following, "y"]))
                self.assertEqual(decoder.next(), ESCAPE)
                self.assertEqual(decoder.next(), following)
                self.assertEqual(decoder.next(), "y")

    def test_interrupted_sequence_preserves_nonsequence_unit(self) -> None:
        decoder = KeyDecoder(FakeRead(["\x1b", "[", "1", curses.KEY_UP, "x"]))
        self.assertEqual(decoder.next(), ESCAPE)
        self.assertEqual(decoder.next(), curses.KEY_UP)
        self.assertEqual(decoder.next(), "x")

    def test_lock_bits_and_alternate_key_subfields(self) -> None:
        for sequence in (
            "\x1b[13;66u", "\x1b[13;130u", "\x1b[13;194u",
            "\x1b[13:13;2u", "\x1b[13:13:13;2:1u",
            "\x1b[27;66;13~",
        ):
            with self.subTest(sequence=repr(sequence)):
                self.assertEqual(KeyDecoder(FakeRead(sequence)).next(), NEWLINE)
        self.assertEqual(KeyDecoder(FakeRead("\x1b[100;69u")).next(), "\x04")
        self.assertEqual(KeyDecoder(FakeRead("\x1b[97;65u")).next(), "a")

    def test_legacy_navigation_and_function_keys(self) -> None:
        for suffix, expected in (
            ("A", curses.KEY_UP), ("B", curses.KEY_DOWN),
            ("C", curses.KEY_RIGHT), ("D", curses.KEY_LEFT),
            ("H", curses.KEY_HOME), ("F", curses.KEY_END), ("P", F1),
        ):
            for prefix in ("\x1b[", "\x1bO"):
                with self.subTest(sequence=repr(prefix + suffix)):
                    self.assertEqual(KeyDecoder(FakeRead(prefix + suffix)).next(), expected)
        for suffix, expected in (
            ("11~", F1), ("5~", curses.KEY_PPAGE), ("6~", curses.KEY_NPAGE),
            ("3~", curses.KEY_DC), ("2~", curses.KEY_IC),
        ):
            with self.subTest(suffix=suffix):
                self.assertEqual(KeyDecoder(FakeRead("\x1b[" + suffix)).next(), expected)
        self.assertEqual(KeyDecoder(FakeRead([curses.KEY_F0 + 1])).next(), F1)
        self.assertEqual(KeyDecoder(FakeRead([curses.KEY_RESIZE])).next(), curses.KEY_RESIZE)

    def test_invalid_unicode_codepoints_are_ignored(self) -> None:
        decoder = KeyDecoder(FakeRead("\x1b[1114112u\x1b[55296ux"))
        self.assertEqual(decoder.next(), "x")

    def test_sequence_collection_is_bounded(self) -> None:
        decoder = KeyDecoder(FakeRead("\x1b[" + "1" * 32))
        self.assertEqual(decoder.next(), ESCAPE)
        self.assertIsNone(decoder.next())


class KittyKeyboardTests(unittest.TestCase):
    def test_push_and_pop_around_exception(self) -> None:
        stream = FakeTTY()
        with self.assertRaisesRegex(RuntimeError, "body failed"):
            with kitty_keyboard(stream):
                self.assertEqual(stream.getvalue(), KITTY_PUSH)
                self.assertEqual(stream.flushes, 1)
                raise RuntimeError("body failed")
        self.assertEqual(stream.getvalue(), KITTY_PUSH + KITTY_POP)
        self.assertEqual(stream.flushes, 2)

    def test_pop_on_keyboard_interrupt(self) -> None:
        stream = FakeTTY()
        with self.assertRaises(KeyboardInterrupt):
            with kitty_keyboard(stream):
                raise KeyboardInterrupt
        self.assertEqual(stream.getvalue(), KITTY_PUSH + KITTY_POP)
        self.assertEqual(stream.flushes, 2)

    def test_default_stream_and_normal_exit(self) -> None:
        stream = FakeTTY()
        with mock.patch("key_input.sys.__stdout__", stream):
            with kitty_keyboard():
                self.assertEqual(stream.getvalue(), KITTY_PUSH)
        self.assertEqual(stream.getvalue(), KITTY_PUSH + KITTY_POP)
        self.assertEqual(stream.flushes, 2)

    def test_non_tty_and_disabled_streams_are_untouched(self) -> None:
        for tty, enabled in ((False, True), (True, False)):
            with self.subTest(tty=tty, enabled=enabled):
                stream = FakeTTY(tty=tty)
                with kitty_keyboard(stream, enabled=enabled):
                    pass
                self.assertEqual(stream.getvalue(), "")
                self.assertEqual(stream.flushes, 0)

    def test_write_and_flush_errors_are_ignored(self) -> None:
        for method in ("write", "flush"):
            for error in (OSError, ValueError):
                with self.subTest(method=method, error=error):
                    stream = FakeTTY()
                    with mock.patch.object(stream, method, side_effect=error("closed")) as failing:
                        with kitty_keyboard(stream):
                            pass
                    self.assertEqual(failing.call_count, 2)

    def test_body_write_error_is_not_swallowed(self) -> None:
        stream = FakeTTY()
        with self.assertRaisesRegex(OSError, "body failed"):
            with kitty_keyboard(stream):
                raise OSError("body failed")
        self.assertEqual(stream.getvalue(), KITTY_PUSH + KITTY_POP)


if __name__ == "__main__":
    unittest.main()
