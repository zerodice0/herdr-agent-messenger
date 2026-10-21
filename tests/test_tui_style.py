"""Behavioral contracts shared by HAM's terminal overlays."""
import tests.ham_test_support as ham_test_support  # Isolate before product imports.

from contextlib import ExitStack
import curses
import tempfile
import unittest
from unittest import mock

from ham.ui import messenger, skill_installer
from ham.ui.terminal_text import display_width
from ham.ui.tui_style import Hint, Theme, content_rect, draw_footer, draw_title, footer_lines
from tests.curses_snapshot_harness import (
    FakeCursesWindow, color_pair_from_attribute, deterministic_color_pair,
)
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


    def test_selection_keeps_marker_weight_and_band_or_reverse_at_every_color_depth(self):
        for colors, environment in ((256, {}), (8, {}), (256, {"NO_COLOR": ""})):
            with (
                self.subTest(colors=colors, environment=environment),
                mock.patch.object(curses, "has_colors", return_value=True),
                mock.patch.object(curses, "COLORS", colors, create=True),
                mock.patch.object(curses, "start_color"),
                mock.patch.object(curses, "use_default_colors"),
                mock.patch.object(curses, "init_pair") as initialize_pair,
                mock.patch.object(curses, "color_pair", side_effect=deterministic_color_pair),
            ):
                screen = FakeCursesWindow(3, 32)
                theme = Theme(environment)
                theme.draw_selected_row(screen, 1, "한글 target", 28, x=1)
                snapshot = screen.snapshot()
                self.assertTrue(snapshot.line(1).lstrip().startswith("› 한글 target"))
                cells = snapshot.cells[1][1:29]
                self.assertTrue(all(cell.attribute & curses.A_BOLD for cell in cells))
                self.assertEqual(len({cell.attribute for cell in cells}), 1)
                attribute = cells[0].attribute
                if colors >= 256 and "NO_COLOR" not in environment:
                    pair = color_pair_from_attribute(attribute)
                    self.assertTrue(pair)
                    foreground, background = next(
                        call.args[1:] for call in initialize_pair.call_args_list
                        if call.args[0] == pair
                    )
                    self.assertEqual(foreground, -1)
                    self.assertNotEqual(background, -1)
                else:
                    self.assertTrue(attribute & curses.A_REVERSE)
                    self.assertFalse(attribute & curses.A_COLOR)
                self.assertFalse(any(call.clipped for call in snapshot.draw_calls))

    def test_title_truncates_korean_context_without_clipping_or_hiding_indicator_at_62_columns(self):
        for surface in ("Quick send", "Send Prompt", "Held", "Skill installer"):
            with self.subTest(surface=surface):
                screen = FakeCursesWindow(27, 62)
                rect = content_rect(27, 62, 120)
                context = "한글 작업공간 " * 12
                draw_title(rect.window(screen), Theme({"NO_COLOR": ""}), surface, context,
                           rect.width, indicator="1-4/20")
                snapshot = screen.snapshot()
                title = snapshot.line(rect.y).lstrip()
                prefix = "HAM · " + surface
                self.assertTrue(title.startswith(prefix + " · 한글"))
                self.assertIn("…", title)
                self.assertTrue(title.endswith("1-4/20"))
                self.assertLessEqual(display_width(title), rect.width)
                self.assertEqual(snapshot.line(rect.y + 1), "")
                self.assertFalse(any(call.clipped for call in snapshot.draw_calls))
                self.assertTrue(all(call.end_column <= rect.x + rect.width
                                    for call in snapshot.draw_calls))

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

    def test_messenger_and_held_have_one_title_without_legacy_headers(self):
        for surface, states in (("Send Prompt", ("discovery", "mode", "ready", "remote")),
                                ("Held", ("empty", "list"))):
            for state in states:
                with self.subTest(surface=surface, state=state), ExitStack() as stack:
                    directory = stack.enter_context(tempfile.TemporaryDirectory())
                    environment = {"NO_COLOR": "", "LANG": "en_US.UTF-8",
                                   "HERDR_PLUGIN_STATE_DIR": directory}
                    stack.enter_context(mock.patch.object(curses, "curs_set"))
                    screen = FakeCursesWindow(27, 62)
                    if surface == "Send Prompt":
                        app = self.messenger_app(stack, screen, environment)
                        app.discovery_choice = state != "discovery"
                        app.mode_choice = state == "ready"
                        app.remote_details_visible = state == "remote"
                        cap = 104
                    else:
                        delivery = mock.Mock()
                        delivery.held_messages.return_value = []
                        app = messenger.HeldMessagesApp(screen, environment, delivery=delivery)
                        if state == "list":
                            app.items = [mock.Mock(sender="sender", recipient="recipient",
                                                   state="awaiting_decision", preview="Preview",
                                                   since="now", deadline="later")]
                        cap = 96
                    app.text = {**app.text, "title": "LEGACY HEADER",
                                "held_title": "LEGACY HEADER", "held_context": "LEGACY HEADER",
                                "remote_details_title": "LEGACY HEADER"}
                    app.render()
                    snapshot = screen.snapshot()
                    rect = content_rect(27, 62, cap)
                    self.assertEqual(snapshot.rows_containing("HAM · "), [rect.y])
                    self.assertTrue(snapshot.line(rect.y).lstrip().startswith("HAM · " + surface))
                    self.assertEqual(snapshot.line(rect.y + 1), "")
                    self.assertNotIn("LEGACY HEADER", "\n".join(snapshot.lines()))

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
                status = text[skill_installer.STATUS_MISSING]
                for label in labels:
                    rows = [line for line in snapshot.lines() if label in line and status in line]
                    self.assertTrue(rows, snapshot.formatted())
                selected = [call for call in snapshot.draw_calls if call.attribute & curses.A_REVERSE]
                self.assertEqual(len(selected), 1)
                self.assertIn(status, selected[0].visible_text)
                self.assertTrue(selected[0].attribute & curses.A_BOLD)
                self.assertFalse(selected[0].attribute & curses.A_DIM)


if __name__ == "__main__":
    unittest.main()
