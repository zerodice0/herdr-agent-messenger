#!/usr/bin/env python3

from __future__ import annotations
import ham_test_support  # Protect the live ledger before product imports.

from contextlib import contextmanager
import curses
import tempfile
from typing import Iterator
import unittest
from unittest import mock

import agent_directory
import agent_messenger

from curses_snapshot_harness import (
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
            app.colors_enabled = True
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
        host_heading = snapshot.line(2)
        self.assertIn("…", host_heading)
        self.assertTrue(host_heading.endswith("(1)"), snapshot.formatted())
        self.assert_capture_in_bounds(snapshot)

    def test_semantic_layout_snapshots_cover_supported_terminal_sizes(self):
        for language, width, height in self.CASES:
            with self.subTest(language=language, size=f"{width}x{height}"):
                with rendered_screen(language, width, height) as (app, snapshot):
                    self.assertEqual((snapshot.width, snapshot.height), (width, height))
                    self.assertEqual(snapshot.refresh_count, 1)
                    self.assert_capture_in_bounds(snapshot)
                    self.assert_screen_lines(
                        snapshot,
                        {
                            0: app.text["send_prompt"],
                            1: f'{app.text["coordinator"]}: {SENDER.qualified_name}',
                        },
                    )

                    recipient_row = self.assert_text_visible(
                        snapshot,
                        app.text["recipients"],
                        unclipped=True,
                    ).row
                    message_row = self.assert_text_visible(
                        snapshot,
                        app.text["message"],
                        unclipped=True,
                    ).row
                    self.assertGreater(recipient_row, 2)
                    self.assertGreater(message_row, recipient_row)

                    message_draws = [
                        call
                        for call in snapshot.draw_calls
                        if call.column == 0
                        and call.visible_text in {"│", "↑", "↓"}
                        and message_row < call.row < height - 2
                    ]
                    self.assertGreaterEqual(
                        len(message_draws),
                        agent_messenger.MESSAGE_MIN_VISIBLE_ROWS,
                        snapshot.formatted(),
                    )
                    self.assertTrue(
                        all(call.row < height for call in message_draws),
                        snapshot.formatted(),
                    )

                    footer_rows = sorted(
                        {
                            call.row
                            for call in snapshot.draw_calls
                            if call.attribute & curses.A_DIM
                            and call.row >= height - 5
                        }
                    )
                    self.assertTrue(footer_rows, snapshot.formatted())
                    self.assertEqual(footer_rows[-1], height - 1, snapshot.formatted())
                    self.assert_text_role(
                        snapshot,
                        "[Esc]",
                        pair=agent_messenger.PAIR_ACCENT,
                        attributes=curses.A_BOLD,
                    )
                    for keycap in ("[Space]", "[Ctrl+R]", "[Ctrl+G]"):
                        self.assert_text_role(
                            snapshot,
                            keycap,
                            pair=agent_messenger.PAIR_ACCENT,
                            attributes=curses.A_BOLD,
                        )

    def test_small_snapshot_wraps_footer_and_keeps_four_message_rows(self):
        with rendered_screen("en", 60, 20) as (app, snapshot):
            help_lines = app._help_lines(app.text["help_recipients"])
            self.assertGreaterEqual(len(help_lines), 2)
            footer_rows = sorted(
                {
                    call.row
                    for call in snapshot.draw_calls
                    if call.visible_text.startswith("[") and call.row >= 15
                }
            )
            self.assertEqual(
                footer_rows,
                list(range(20 - len(help_lines), 20)),
                snapshot.formatted(),
            )

            message_heading = self.assert_text_visible(snapshot, app.text["message"]).row
            markers = [
                call
                for call in snapshot.draw_calls
                if call.column == 0
                and call.visible_text in {"│", "↑", "↓"}
                and call.row > message_heading
            ]
            self.assertEqual(
                len(markers),
                agent_messenger.MESSAGE_MIN_VISIBLE_ROWS,
                snapshot.formatted(),
            )
            self.assertTrue(
                any(
                    call.column == 58 and call.visible_text == "█"
                    for call in snapshot.draw_calls
                ),
                snapshot.formatted(),
            )
            self.assert_text_visible(snapshot, "다섯째 줄은", unclipped=True)

    def test_large_snapshot_preserves_groups_unicode_states_and_color_roles(self):
        with rendered_screen("ja", 160, 50) as (app, snapshot):
            for group in ("local", "macbook-pro", "studio-日本"):
                self.assert_text_role(
                    snapshot,
                    f"▾ {group}",
                    pair=agent_messenger.PAIR_ACCENT,
                    attributes=curses.A_BOLD,
                )

            workspace_row = self.assert_text_visible(
                snapshot,
                "WT:決済-결제-Workspace",
                unclipped=True,
            ).row
            self.assertNotIn("(1)", snapshot.line(workspace_row))
            self.assert_text_role(
                snapshot,
                "(1)",
                pair=agent_messenger.PAIR_ACCENT,
                attributes=curses.A_DIM,
            )
            agent_row = self.assert_text_visible(
                snapshot,
                "amber-crane",
                unclipped=True,
            ).row
            self.assertEqual(agent_row, workspace_row + 1)
            self.assertIn("w1:p1", snapshot.line(agent_row))
            self.assertEqual(snapshot.line(agent_row).count("[x]"), 1)
            self.assert_text_role(
                snapshot,
                "amber-crane",
                attributes=curses.A_BOLD,
            )
            focused_row = self.assert_text_visible(
                snapshot,
                "red-fox",
                unclipped=True,
            ).row
            self.assertEqual(snapshot.line(focused_row).count("›"), 1)

            self.assert_text_role(
                snapshot,
                "[x]",
                pair=agent_messenger.PAIR_ACCENT,
                attributes=curses.A_BOLD,
            )
            self.assert_text_role(
                snapshot,
                f'● {app.text["status_working"]}',
                pair=agent_messenger.PAIR_WARNING,
                attributes=curses.A_BOLD,
            )
            self.assert_text_role(
                snapshot,
                f'✓ {app.text["status_done"]}',
                pair=agent_messenger.PAIR_SUCCESS,
            )
            idle = self.assert_text_role(
                snapshot,
                f'○ {app.text["status_idle"]}',
                attributes=curses.A_DIM,
            )
            self.assertEqual(idle.attribute, curses.A_DIM)
            self.assert_text_role(
                snapshot,
                f'! {app.text["status_blocked"]}',
                pair=agent_messenger.PAIR_ERROR,
                attributes=curses.A_BOLD,
            )
            for line in LONG_MESSAGE:
                self.assert_text_visible(snapshot, line, unclipped=True)
            self.assert_text_role(
                snapshot,
                app.text["remote_warning_summary"].format(
                    unavailable=1,
                    available=2,
                    details=app.text["remote_details_hint"],
                ),
                pair=agent_messenger.PAIR_WARNING,
                attributes=curses.A_BOLD,
            )

            app.remote_details_visible = True
            app.render()
            details = app.screen.snapshot()
            self.assert_capture_in_bounds(details)
            self.assert_screen_lines(
                details,
                {
                    0: app.text["remote_details_title"],
                    1: app.text["remote_warning_summary"].format(
                        unavailable=1,
                        available=2,
                        details=app.text["remote_details_hint"],
                    ),
                },
            )
            self.assert_text_role(
                details,
                "! offline-서울 — connection timed out",
                pair=agent_messenger.PAIR_WARNING,
                attributes=curses.A_BOLD,
            )


if __name__ == "__main__":
    unittest.main()
