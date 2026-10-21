#!/usr/bin/env python3

from __future__ import annotations
import tests.ham_test_support as ham_test_support  # Protect the live ledger before product imports.

from contextlib import contextmanager
import curses
import tempfile
from typing import Iterator
import unittest
from unittest import mock

from ham.agents import directory as agent_directory
from ham.ui import messenger as agent_messenger
from ham.ui.tui_style import content_rect
from ham.ui.messenger_render import MESSAGE_MIN_VISIBLE_ROWS

from tests.curses_snapshot_harness import (
    FakeCursesWindow,
    ScreenSnapshot,
    SnapshotTestCase,
    deterministic_color_pair,
)


def record(
    name: str,
    pane_id: str,
    session_id: str,
    *,
    host: str = "local",
    status: str = "idle",
    workspace: str = "messenger",
    local: bool = True,
    worktree: bool = False,
) -> agent_directory.AgentRecord:
    return agent_directory.AgentRecord(
        host=host,
        name=name,
        pane_id=pane_id,
        workspace_id=f"workspace-{session_id}",
        workspace_label=workspace,
        status=status,
        session_id=session_id,
        cwd=f"/work/{workspace}",
        local=local,
        revision=1,
        agent_kind="codex",
        terminal_id=f"terminal-{session_id}",
        workspace_is_worktree=worktree,
    )


SENDER = record("coordinator-owl", "w0:p0", "sender")
SELECTED = record(
    "amber-crane",
    "w1:p1",
    "selected-working",
    status="working",
    workspace="決済-결제-Workspace",
    worktree=True,
)
IDLE = record(
    "blue-raven",
    "w1:p2",
    "idle",
    workspace="문서-日本語",
)
DONE = record(
    "green-turtle",
    "w1:p3",
    "done",
    status="done",
    workspace="release",
)
BLOCKED = record(
    "red-fox",
    "w1:p4",
    "blocked",
    status="blocked",
    workspace="payments",
)
REMOTE_DONE = record(
    "purple-koala",
    "w2:p1",
    "remote-done",
    host="macbook-pro",
    status="done",
    workspace="aibridge",
    local=False,
)
REMOTE_IDLE = record(
    "silver-wolf",
    "w3:p1",
    "remote-idle",
    host="studio-日本",
    status="idle",
    workspace="国際化-작업공간",
    local=False,
    worktree=True,
)
RECIPIENTS = (SELECTED, IDLE, DONE, BLOCKED, REMOTE_DONE, REMOTE_IDLE)

LONG_MESSAGE = [
    "First, inspect the renderer boundary and keep this line intact.",
    "둘째, 한글 작업공간과 선택 상태를 확인합니다.",
    "三番目に、日本語の表示幅と折り返しを確認します。",
    "Fourth, the message editor must retain at least four visible rows.",
    "다섯째 줄은 스크롤바 위치도 함께 검증합니다.",
]


@contextmanager
def rendered_screen(
    language: str,
    width: int,
    height: int,
    *,
    section: str = "recipients",
) -> Iterator[tuple[agent_messenger.MessengerApp, ScreenSnapshot]]:
    window = FakeCursesWindow(height, width)
    locales = {"en": "en_US.UTF-8", "ko": "ko_KR.UTF-8", "ja": "ja_JP.UTF-8"}
    with tempfile.TemporaryDirectory() as state_directory:
        environment = {
            "LANG": locales[language],
            "HERDR_PLUGIN_STATE_DIR": state_directory,
        }
        with (
            mock.patch.object(
                agent_messenger,
                "query_local_agents",
                return_value=[SENDER, *[item for item in RECIPIENTS if item.local]],
            ),
            mock.patch.object(
                agent_messenger,
                "ssh_hosts",
                return_value=["macbook-pro", "studio-日本", "offline-서울"],
            ),
            mock.patch.object(
                agent_messenger.curses,
                "color_pair",
                side_effect=deterministic_color_pair,
            ),
            mock.patch.object(agent_messenger.curses, "curs_set"),
        ):
            app = agent_messenger.MessengerApp(window, SENDER, environment)
            app.agents = list(RECIPIENTS)
            app.discovery_choice = True
            app.mode_choice = True
            app.remote_enabled = True
            app.host_status = {
                "macbook-pro": "",
                "studio-日本": "",
                "offline-서울": app.text["unavailable"],
            }
            app.host_errors = {"offline-서울": "connection timed out"}
            app.selected = {SELECTED.identity}
            app.section = section
            app.message_lines = list(LONG_MESSAGE)
            app.message_row = len(LONG_MESSAGE) - 1
            app.message_column = len(LONG_MESSAGE[-1])
            app.render()
            yield app, window.snapshot()


class MessengerCursesSnapshotTest(SnapshotTestCase):
    CASES = (
        ("en", 60, 20),
        ("ko", 100, 30),
        ("ja", 160, 50),
    )

    def test_fake_window_tracks_wide_cells_and_clipping(self):
        window = FakeCursesWindow(2, 8)
        window.addnstr(0, 0, "ab한글z", 20)
        window.addnstr(1, 0, "한글테스트", 20)
        snapshot = window.snapshot()

        self.assertEqual(snapshot.line(0), "ab한글z")
        self.assertFalse(snapshot.draw_calls[0].clipped)
        self.assertEqual(snapshot.line(1), "한글테")
        self.assertTrue(snapshot.draw_calls[1].clipped)
        self.assert_capture_in_bounds(snapshot)

    def test_long_group_header_reserves_space_for_count(self):
        target = record(
            "blue-moose",
            "w1:p1",
            "long-host",
            host="very-long-remote-host-name-that-overflows",
            local=False,
        )
        window = FakeCursesWindow(8, 32)
        with tempfile.TemporaryDirectory() as state_directory:
            environment = {
                "LANG": "en_US.UTF-8",
                "HERDR_PLUGIN_STATE_DIR": state_directory,
            }
            with (
                mock.patch.object(
                    agent_messenger,
                    "query_local_agents",
                    return_value=[SENDER],
                ),
                mock.patch.object(agent_messenger, "ssh_hosts", return_value=[]),
            ):
                app = agent_messenger.MessengerApp(window, SENDER, environment)

        app.agents = [target]
        app._render_recipients(0, 6)
        snapshot = window.snapshot()
        host_heading = snapshot.line(content_rect(8, 32, 104).y + 2)
        self.assertIn("…", host_heading)
        self.assertTrue(host_heading.endswith("(1)"), snapshot.formatted())
        self.assert_capture_in_bounds(snapshot)

    def test_supported_terminal_sizes_keep_editor_and_footer_in_content_rect(self):
        for language, width, height in self.CASES:
            with self.subTest(language=language, size=f"{width}x{height}"):
                with rendered_screen(language, width, height) as (app, snapshot):
                    rect = content_rect(height, width, 104)
                    self.assertEqual(snapshot.refresh_count, 1)
                    self.assert_capture_in_bounds(snapshot)
                    visible = [call for call in snapshot.draw_calls if call.visible_text.strip()]
                    self.assertTrue(visible)
                    self.assertTrue(all(
                        rect.y <= call.row < rect.y + rect.height
                        and rect.x <= call.column
                        and call.end_column <= rect.x + rect.width
                        for call in visible
                    ), snapshot.formatted())
                    recipient_row = self.assert_text_visible(snapshot, app.text["recipients"]).row
                    message_row = self.assert_text_visible(snapshot, app.text["message"]).row
                    self.assertGreater(message_row, recipient_row)
                    footer = self.assert_text_visible(snapshot, "[Esc]", unclipped=True)
                    editor_rows = {
                        call.row for call in snapshot.draw_calls
                        if call.column == rect.x + 2 and message_row < call.row < footer.row
                    }
                    self.assertGreaterEqual(len(editor_rows), MESSAGE_MIN_VISIBLE_ROWS)
                    self.assertEqual(app.message_lines, LONG_MESSAGE)

    def test_large_snapshot_preserves_unicode_groups_selection_and_remote_errors(self):
        with rendered_screen("ja", 172, 52) as (app, snapshot):
            for group in ("local", "macbook-pro", "studio-日本"):
                self.assert_text_visible(snapshot, group, unclipped=True)
            workspace_row = self.assert_text_visible(
                snapshot, "WT:決済-결제-Workspace", unclipped=True,
            ).row
            agent_row = self.assert_text_visible(snapshot, SELECTED.name, unclipped=True).row
            self.assertEqual(agent_row, workspace_row + 1)
            self.assertIn(SELECTED.pane_id, snapshot.line(agent_row))
            self.assertEqual(snapshot.line(agent_row).count("[x]"), 1)
            focused = [call for call in snapshot.draw_calls
                       if call.visible_text.lstrip().startswith("›")]
            self.assertTrue(focused, snapshot.formatted())
            self.assertTrue(all(call.attribute & curses.A_BOLD for call in focused))
            self.assertTrue(all(call.attribute & (curses.A_REVERSE | curses.A_COLOR)
                                for call in focused))
            for state, glyph in (("working", "●"), ("done", "✓"), ("idle", "○"), ("blocked", "!")):
                self.assert_text_visible(snapshot, f'{glyph} {app.text["status_" + state]}')
            for line in LONG_MESSAGE:
                self.assert_text_visible(snapshot, line, unclipped=True)
            self.assert_text_visible(snapshot, app._remote_summary())
            before = list(app.message_lines), set(app.selected)
            app.remote_details_visible = True
            app.render()
            details = app.screen.snapshot()
            self.assert_capture_in_bounds(details)
            self.assert_text_visible(details, "offline-서울")
            self.assert_text_visible(details, "connection timed out")
            self.assertEqual((app.message_lines, app.selected), before)


if __name__ == "__main__":
    unittest.main()
