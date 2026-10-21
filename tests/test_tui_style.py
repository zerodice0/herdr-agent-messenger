"""Behavioral contracts shared by HAM's terminal overlays."""
import tests.ham_test_support as ham_test_support  # Isolate before product imports.

from contextlib import ExitStack
import curses
import tempfile
import unittest
from unittest import mock

from ham.ui import messenger, skill_installer
from ham.ui.terminal_text import display_width
from ham.ui.tui_style import Hint, Theme, content_rect, draw_footer, footer_lines
from tests.curses_snapshot_harness import FakeCursesWindow
from tests.test_agent_messenger import agent


class TuiStyleTest(unittest.TestCase):
    def test_content_rect_centers_each_surface_at_overlay_sizes(self):
        cases = (
            (27, 62, {120: 60, 104: 60, 96: 60, 80: 60}),
            (37, 92, {120: 88, 104: 88, 96: 88, 80: 80}),
            (52, 172, {120: 120, 104: 104, 96: 96, 80: 80}),
        )
        for height, width, caps in cases:
            for cap, columns in caps.items():
                with self.subTest(height=height, width=width, cap=cap):
                    rect = content_rect(height, width, cap)
                    self.assertEqual(rect.width, columns)
                    self.assertEqual(rect.x, (width - columns) // 2)
                    self.assertEqual(rect.y, 1 if height < 30 else 2)
                    self.assertEqual(rect.height, height - rect.y - 1)
                    self.assertGreaterEqual(rect.x, 1 if width < 80 else 2)
                    self.assertLessEqual(rect.x + rect.width, width)

    def test_full_width_footer_keeps_declaration_order_regardless_of_priority(self):
        hints = [
            Hint("Ctrl+R", "Refresh", 100),
            Hint("Ctrl+D", "Direct", 100),
            Hint("Shift+Enter", "Newline", 2),
            Hint("F1", "Help", 1),
            Hint("Esc", "Close", 0),
        ]
        width = sum(display_width(hint.text) for hint in hints) + 2 * (len(hints) - 1)
        self.assertEqual(footer_lines(hints, width), [hints])

    def test_narrow_footer_sheds_secondary_hints_without_reordering_survivors(self):
        secondary = [Hint("Ctrl+R", "Refresh", 100), Hint("Ctrl+D", "Direct", 100)]
        essential = [Hint("Esc", "Close", 0), Hint("F1", "Help", 1),
                     Hint("Shift+Enter", "Newline", 2)]
        hints = [*secondary, *reversed(essential)]
        for width in (32, 40, 60):
            with self.subTest(width=width):
                rows = footer_lines(hints, width)
                self.assertLessEqual(len(rows), 2)
                flattened = [hint for row in rows for hint in row]
                self.assertEqual(flattened, [hint for hint in hints if hint in flattened])
                for hint in essential:
                    self.assertIn(hint, flattened)
                if width in (32, 40):
                    self.assertLess(len(flattened), len(hints))
                self.assertTrue(all(
                    sum(display_width(hint.text) for hint in row) + 2 * (len(row) - 1) <= width
                    for row in rows
                ))
                screen = FakeCursesWindow(6, width + 2)
                draw_footer(screen, 1, 1, width, hints, Theme({"NO_COLOR": ""}))
                snapshot = screen.snapshot()
                self.assertTrue(all(not call.clipped for call in snapshot.draw_calls))
                for hint in essential:
                    self.assertTrue(snapshot.rows_containing(hint.text), snapshot.formatted())

    def test_content_window_offsets_and_clips_complete_unicode_cells(self):
        screen = FakeCursesWindow(52, 172)
        rect = content_rect(52, 172, 80)
        view = rect.window(screen)
        view.addstr(1, 2, "한글")
        view.addnstr(2, rect.width - 3, "가나다", 3)
        calls = screen.snapshot().draw_calls
        self.assertEqual((calls[0].row, calls[0].column), (rect.y + 1, rect.x + 2))
        self.assertEqual(calls[0].visible_text, "한글")
        self.assertTrue(calls[1].visible_text.startswith("가"))
        self.assertLessEqual(calls[1].end_column, rect.x + rect.width)


class OverlayRendererTest(unittest.TestCase):
    def color_spies(self, stack):
        stack.enter_context(mock.patch.object(curses, "has_colors", return_value=True))
        spies = [stack.enter_context(mock.patch.object(curses, name))
                 for name in ("start_color", "init_pair", "color_pair")]
        stack.enter_context(mock.patch.object(curses, "curs_set"))
        return spies

    def messenger_app(self, stack, screen, environment):
        sender = agent()
        recipient = agent(name="recipient", pane_id="w1:p2", session_id="recipient")
        stack.enter_context(mock.patch.object(messenger, "query_local_agents", return_value=[sender, recipient]))
        stack.enter_context(mock.patch.object(messenger, "ssh_hosts", return_value=["peer"]))
        return messenger.MessengerApp(screen, sender, environment)

    def test_no_color_skips_pair_initialization_and_drawing_on_short_overlays(self):
        for value in ("", "1"):
            for surface in ("messenger", "held", "installer"):
                with self.subTest(value=value, surface=surface), ExitStack() as stack:
                    directory = stack.enter_context(tempfile.TemporaryDirectory())
                    # Explicit overlay environment wins over the parent process environment.
                    stack.enter_context(mock.patch.dict("os.environ", {}, clear=True))
                    environment = {"NO_COLOR": value, "HOME": directory,
                                   "HERDR_PLUGIN_STATE_DIR": directory, "LANG": "en_US.UTF-8"}
                    spies = self.color_spies(stack)
                    screen = FakeCursesWindow(52, 172)
                    if surface == "messenger":
                        app = self.messenger_app(stack, screen, environment)
                        for discovery, mode in ((False, False), (True, False), (True, True)):
                            app.discovery_choice, app.mode_choice = discovery, mode
                            app.render()
                    elif surface == "held":
                        delivery = mock.Mock()
                        delivery.held_messages.return_value = []
                        app = messenger.HeldMessagesApp(screen, environment, delivery=delivery)
                        app.render()
                        app.items = [mock.Mock(sender="sender", recipient="recipient", state="awaiting_decision",
                                               preview="Preview", since="now", deadline="later")]
                        app.render()
                    else:
                        screen.keypad = mock.Mock()
                        screen.get_wch = mock.Mock(return_value="\x1b")
                        stack.enter_context(mock.patch.object(curses, "wrapper", side_effect=lambda callback: callback(screen)))
                        stack.enter_context(mock.patch.object(skill_installer, "installation_status",
                                                              return_value=skill_installer.STATUS_MISSING))
                        skill_installer.overlay(environment)
                    for spy in spies:
                        spy.assert_not_called()
                    snapshot = screen.snapshot()
                    self.assertTrue(any(call.visible_text.strip() for call in snapshot.draw_calls))
                    self.assertFalse(any(call.attribute & curses.A_COLOR for call in snapshot.draw_calls))

    def test_installer_status_stays_adjacent_to_name_at_every_overlay_width(self):
        for height, width in ((27, 62), (37, 92), (52, 172)):
            with self.subTest(width=width), ExitStack() as stack:
                directory = stack.enter_context(tempfile.TemporaryDirectory())
                environment = {"HOME": directory, "NO_COLOR": "", "LANG": "en_US.UTF-8"}
                screen = FakeCursesWindow(height, width)
                screen.keypad = mock.Mock()
                screen.get_wch = mock.Mock(return_value="\x1b")
                stack.enter_context(mock.patch.object(curses, "wrapper", side_effect=lambda callback: callback(screen)))
                stack.enter_context(mock.patch.object(curses, "curs_set"))
                stack.enter_context(mock.patch.object(skill_installer, "installation_status",
                                                      return_value=skill_installer.STATUS_MISSING))
                skill_installer.overlay(environment)
                snapshot = screen.snapshot()
                text = skill_installer.COPY["en"]
                labels = [text[client] for client in ("codex", "claude", "omp")]
                longest = max(map(display_width, labels))
                status = text[skill_installer.STATUS_MISSING]
                for label in labels:
                    rows = [line for line in snapshot.lines() if label in line and status in line]
                    self.assertTrue(rows, snapshot.formatted())
                    for line in rows:
                        start = line.index(label)
                        status_column = display_width(line[:line.index(status)])
                        # Two spaces separate the padded name and the one-cell status glyph.
                        self.assertEqual(status_column - display_width(line[:start]), longest + 4)
                selected = [call for call in snapshot.draw_calls if call.attribute & curses.A_REVERSE]
                self.assertEqual(len(selected), 1)
                self.assertIn(status, selected[0].visible_text)
                self.assertTrue(selected[0].attribute & curses.A_BOLD)
                self.assertFalse(selected[0].attribute & curses.A_DIM)


if __name__ == "__main__":
    unittest.main()
