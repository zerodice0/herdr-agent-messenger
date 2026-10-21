#!/usr/bin/env python3

import tests.ham_test_support as ham_test_support  # Protect the live ledger before product imports.
from pathlib import Path
import shlex
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest import mock

from ham.agents import directory as agent_directory
from ham.ui import messenger as agent_messenger
from ham import cli as agent_skill_cli
from ham.ui import messenger_i18n as messenger_i18n


def agent(
    *,
    name: str = "blue-raven",
    pane_id: str = "w1:p1",
    session_id: str = "session-1",
    workspace_id: str | None = None,
    workspace_label: str = "project",
    workspace_is_worktree: bool = False,
) -> agent_directory.AgentRecord:
    return agent_directory.AgentRecord(
        host="local",
        name=name,
        pane_id=pane_id,
        workspace_id=workspace_id or pane_id.split(":", 1)[0],
        workspace_label=workspace_label,
        status="idle",
        session_id=session_id,
        cwd="/work/project",
        local=True,
        revision=1,
        agent_kind="codex",
        terminal_id=f"terminal-{pane_id}",
        workspace_is_worktree=workspace_is_worktree,
    )


class ImmediateThread:
    def __init__(self, *, target, args=(), **_kwargs):
        self.target = target
        self.args = args

    def start(self):
        self.target(*self.args)


class AgentMessengerTest(unittest.TestCase):
    def test_extracted_symbols_remain_available_from_agent_messenger(self):
        self.assertEqual(
            agent_messenger.STATUS_ORDER,
            {"blocked": 0, "working": 1, "done": 2, "idle": 3, "unknown": 4},
        )
        for name in (
            "RecipientViewRow",
            "ShortcutHelpSpan",
            "WrappedMessageLine",
            "display_width",
            "scrollbar_thumb",
            "wrap_message_lines",
        ):
            self.assertTrue(hasattr(agent_messenger, name), name)

    def test_agents_sort_by_workspace_before_status_and_label(self):
        sender = agent()
        zeta = agent(
            name="alpha-agent",
            pane_id="w1:p2",
            session_id="session-2",
            workspace_label="한글 작업공간",
        )
        alpha = agent(
            name="zeta-agent",
            pane_id="w2:p1",
            session_id="session-3",
            workspace_label="alpha-worktree",
            workspace_is_worktree=True,
        )
        with tempfile.TemporaryDirectory() as state_directory:
            environment = {
                "LANG": "ko_KR.UTF-8",
                "HERDR_PLUGIN_STATE_DIR": state_directory,
            }
            with (
                mock.patch.object(
                    agent_messenger,
                    "query_local_agents",
                    return_value=[sender, zeta, alpha],
                ),
                mock.patch.object(agent_messenger, "ssh_hosts", return_value=[]),
            ):
                app = agent_messenger.MessengerApp(mock.Mock(), sender, environment)

        self.assertEqual(
            [record.name for record in app.filtered_agents()],
            ["zeta-agent", "alpha-agent"],
        )
        self.assertEqual(agent_messenger.workspace_display(alpha), "WT:alpha-worktree")
        self.assertEqual(agent_messenger.workspace_display(zeta), "한글 작업공간")

    def test_narrow_recipient_hierarchy_preserves_unicode_workspace_and_target(self):
        record = agent(
            name="white-bison",
            workspace_label="결제 기능 작업트리",
            workspace_is_worktree=True,
        )
        sender = agent(name="blue-raven", pane_id="w1:p9", session_id="sender")
        screen = mock.Mock()
        screen.getmaxyx.return_value = (25, 58)
        with tempfile.TemporaryDirectory() as state_directory:
            environment = {
                "LANG": "en_US.UTF-8",
                "HERDR_PLUGIN_STATE_DIR": state_directory,
            }
            with (
                mock.patch.object(
                    agent_messenger,
                    "query_local_agents",
                    return_value=[sender, record],
                ),
                mock.patch.object(agent_messenger, "ssh_hosts", return_value=[]),
            ):
                app = agent_messenger.MessengerApp(screen, sender, environment)

        rows = app._recipient_view_rows([record])
        self.assertEqual(
            [row.kind for row in rows],
            ["workspace_header", "agent"],
        )
        header = app._group_header(rows[0])
        line = app._recipient_line(record, " ", "idle", indent=rows[1].indent)
        self.assertLessEqual(agent_messenger.display_width(line), 58)
        self.assertIn("WT:결제 기능", header)
        self.assertIn("white-bison", line)
        self.assertIn("w1:p1", line)

    def test_recipient_cursor_selection_and_status_use_independent_styles(self):
        sender = agent()
        recipient = agent(
            name="white-bison",
            pane_id="w1:p2",
            session_id="session-2",
        )
        recipient = agent_directory.replace(recipient, status="working")
        screen = mock.Mock()
        screen.getmaxyx.return_value = (18, 88)
        with tempfile.TemporaryDirectory() as state_directory:
            environment = {
                "LANG": "en_US.UTF-8",
                "HERDR_PLUGIN_STATE_DIR": state_directory,
            }
            with (
                mock.patch.object(
                    agent_messenger,
                    "query_local_agents",
                    return_value=[sender, recipient],
                ),
                mock.patch.object(agent_messenger, "ssh_hosts", return_value=[]),
            ):
                app = agent_messenger.MessengerApp(screen, sender, environment)

        app.selected = {recipient.identity}
        rendered: list[tuple[int, int, str, int]] = []

        def record(row, column, value, attribute=0):
            rendered.append((row, column, value, attribute))

        style = lambda pair=0, attribute=0: pair * 100_000 + attribute
        with (
            mock.patch.object(app, "_safe_add", side_effect=record),
            mock.patch.object(app, "_style", side_effect=style),
        ):
            app._render_recipients(0, 3)

        identity = next(
            item for item in rendered if "white-bison" in item[2]
        )
        cursor = next(item for item in rendered if item[2] == "›")
        checkbox = next(item for item in rendered if item[2] == "[x]")
        status = next(item for item in rendered if item[2] == "● working")
        self.assertEqual(identity[3], agent_messenger.curses.A_BOLD)
        self.assertEqual(
            cursor[3],
            style(agent_messenger.PAIR_ACCENT, agent_messenger.curses.A_BOLD),
        )
        self.assertEqual(checkbox[3], cursor[3])
        self.assertEqual(cursor[1], 2)
        self.assertEqual(checkbox[1], 4)
        self.assertEqual(
            status[3],
            style(agent_messenger.PAIR_WARNING, agent_messenger.curses.A_BOLD),
        )

    def test_recipient_panel_grows_until_message_minimum(self):
        panel_height = agent_messenger.MessengerApp._recipient_panel_height
        self.assertEqual(panel_height(1, 12), 3)
        self.assertEqual(panel_height(4, 12), 6)
        self.assertEqual(panel_height(20, 12), 7)

    def test_long_message_keeps_four_rows_and_shows_scrollbar(self):
        sender = agent()
        screen = mock.Mock()
        screen.getmaxyx.return_value = (18, 40)
        with tempfile.TemporaryDirectory() as state_directory:
            environment = {
                "LANG": "en_US.UTF-8",
                "HERDR_PLUGIN_STATE_DIR": state_directory,
            }
            with (
                mock.patch.object(
                    agent_messenger,
                    "query_local_agents",
                    return_value=[sender],
                ),
                mock.patch.object(agent_messenger, "ssh_hosts", return_value=[]),
            ):
                app = agent_messenger.MessengerApp(screen, sender, environment)

        app.section = "message"
        app.message_lines = [f"line {index}" for index in range(8)]
        app.message_row = 7
        app.message_column = len(app.message_lines[-1])
        rendered: list[tuple[int, int, str]] = []

        def record(row, column, value, _attribute=0):
            rendered.append((row, column, value))

        with (
            mock.patch.object(app, "_safe_add", side_effect=record),
            mock.patch.object(app, "_style", return_value=0),
        ):
            app._render_message(0, 5)

        message_markers = [
            value
            for row, column, value in rendered
            if 1 <= row <= 4 and column == 0
        ]
        scrollbar = [
            value
            for row, column, value in rendered
            if 1 <= row <= 4 and column == 38
        ]
        self.assertEqual(len(message_markers), 4)
        self.assertEqual(scrollbar, ["│", "│", "█", "█"])

    def test_scrollbar_thumb_tracks_start_middle_and_end(self):
        self.assertIsNone(agent_messenger.scrollbar_thumb(4, 4, 0))
        self.assertEqual(agent_messenger.scrollbar_thumb(8, 4, 0), (0, 2))
        self.assertEqual(agent_messenger.scrollbar_thumb(8, 4, 2), (1, 2))
        self.assertEqual(agent_messenger.scrollbar_thumb(8, 4, 4), (2, 2))

    def test_remote_recipients_are_grouped_by_host_and_workspace(self):
        sender = agent()
        first = agent_directory.replace(
            agent(name="purple-koala", pane_id="w3:pQ", session_id="remote-1"),
            host="macbook-pro",
            local=False,
            workspace_label="aibridge",
        )
        second = agent_directory.replace(
            agent(name="brown-fox", pane_id="w2:p2Q", session_id="remote-2"),
            host="macbook-pro",
            local=False,
            workspace_label="edgedx_mobile",
        )
        screen = mock.Mock()
        screen.getmaxyx.return_value = (18, 88)
        with tempfile.TemporaryDirectory() as state_directory:
            environment = {
                "LANG": "en_US.UTF-8",
                "HERDR_PLUGIN_STATE_DIR": state_directory,
            }
            with (
                mock.patch.object(
                    agent_messenger,
                    "query_local_agents",
                    return_value=[sender],
                ),
                mock.patch.object(agent_messenger, "ssh_hosts", return_value=[]),
            ):
                app = agent_messenger.MessengerApp(screen, sender, environment)

        app.agents = [first, second]
        single_rows = app._recipient_view_rows([first])
        self.assertEqual(app._group_header(single_rows[0]), "▾ macbook-pro (1)")
        self.assertEqual(app._group_header(single_rows[1]), "  ▾ aibridge")

        rows = app._recipient_view_rows(app.filtered_agents())
        self.assertEqual(
            [row.kind for row in rows],
            [
                "host_header",
                "workspace_header",
                "agent",
                "workspace_header",
                "agent",
            ],
        )
        self.assertEqual(rows[0].group_count, 2)
        self.assertEqual(app._group_header(rows[0]), "▾ macbook-pro (2)")
        self.assertEqual(
            app._group_header(rows[1]),
            "  ▾ aibridge",
        )
        self.assertEqual(
            app._group_header(rows[3]),
            "  ▾ edgedx_mobile",
        )

        line = app._recipient_line(first, " ", "○ idle", indent=rows[2].indent)
        self.assertIn("purple-koala", line)
        self.assertNotIn("aibridge", line)
        self.assertIn("w3:pQ", line)
        self.assertNotIn("macbook-pro", line)

    def test_duplicate_workspace_labels_are_disambiguated_by_workspace_id(self):
        sender = agent()
        first = agent(
            name="purple-koala",
            pane_id="w1:p2",
            session_id="workspace-1",
            workspace_id="w1",
            workspace_label="recipelabo_flutter",
        )
        second = agent(
            name="brown-fox",
            pane_id="w2:p1",
            session_id="workspace-2",
            workspace_id="w2",
            workspace_label="recipelabo_flutter",
        )
        with tempfile.TemporaryDirectory() as state_directory:
            environment = {
                "LANG": "en_US.UTF-8",
                "HERDR_PLUGIN_STATE_DIR": state_directory,
            }
            with (
                mock.patch.object(
                    agent_messenger,
                    "query_local_agents",
                    return_value=[sender],
                ),
                mock.patch.object(agent_messenger, "ssh_hosts", return_value=[]),
            ):
                app = agent_messenger.MessengerApp(mock.Mock(), sender, environment)

        rows = app._recipient_view_rows([first, second])
        headers = [
            app._group_header(row)
            for row in rows
            if row.kind == "workspace_header"
        ]
        self.assertEqual(
            headers,
            [
                "▾ recipelabo_flutter [w1]",
                "▾ recipelabo_flutter [w2]",
            ],
        )

    def test_unknown_workspace_header_uses_current_locale(self):
        sender = agent()
        recipient = agent_directory.replace(
            agent(name="white-bison", pane_id="w1:p2", session_id="session-2"),
            workspace_id="",
            workspace_label="",
            cwd="",
        )
        expected = {
            "en_US.UTF-8": "unknown workspace",
            "ko_KR.UTF-8": "알 수 없는 작업공간",
            "ja_JP.UTF-8": "不明なワークスペース",
        }
        for locale_name, label in expected.items():
            with self.subTest(locale=locale_name):
                with tempfile.TemporaryDirectory() as state_directory:
                    environment = {
                        "LANG": locale_name,
                        "HERDR_PLUGIN_STATE_DIR": state_directory,
                    }
                    with (
                        mock.patch.object(
                            agent_messenger,
                            "query_local_agents",
                            return_value=[sender],
                        ),
                        mock.patch.object(
                            agent_messenger,
                            "ssh_hosts",
                            return_value=[],
                        ),
                    ):
                        app = agent_messenger.MessengerApp(
                            mock.Mock(), sender, environment
                        )

                rows = app._recipient_view_rows([recipient])
                self.assertEqual(app._group_header(rows[0]), f"▾ {label}")

    def test_long_workspace_group_keeps_nested_sticky_headers_while_scrolling(self):
        sender = agent()
        recipients = [
            agent_directory.replace(
                agent(
                    name=f"agent-{index}",
                    pane_id=f"w1:p{index}",
                    session_id=f"remote-{index}",
                ),
                host="macbook-pro",
                local=False,
            )
            for index in range(5)
        ]
        screen = mock.Mock()
        screen.getmaxyx.return_value = (18, 88)
        with tempfile.TemporaryDirectory() as state_directory:
            environment = {"HERDR_PLUGIN_STATE_DIR": state_directory}
            with (
                mock.patch.object(
                    agent_messenger,
                    "query_local_agents",
                    return_value=[sender],
                ),
                mock.patch.object(agent_messenger, "ssh_hosts", return_value=[]),
            ):
                app = agent_messenger.MessengerApp(screen, sender, environment)

        app.agents = recipients
        app.cursor = 3
        visible, has_more = app._visible_recipient_rows(app.filtered_agents(), 4)
        self.assertEqual(visible[0].kind, "host_header")
        self.assertEqual(visible[0].group_count, 5)
        self.assertEqual(visible[1].kind, "workspace_header")
        self.assertEqual(visible[1].group_count, 5)
        self.assertEqual(
            [row.agent_index for row in visible if row.kind == "agent"],
            [2, 3],
        )
        self.assertTrue(has_more)

        visible, has_more = app._visible_recipient_rows(
            app.filtered_agents(),
            2,
        )
        self.assertEqual(
            [row.kind for row in visible],
            ["workspace_header", "agent"],
        )
        self.assertEqual(visible[-1].agent_index, 3)
        self.assertTrue(has_more)

    def test_search_matches_label_pane_and_full_session_id(self):
        sender = agent()
        recipient = agent(
            name="purple-koala",
            pane_id="w3:pQ",
            session_id="019ff833-session-value",
        )
        with tempfile.TemporaryDirectory() as state_directory:
            environment = {"HERDR_PLUGIN_STATE_DIR": state_directory}
            with (
                mock.patch.object(
                    agent_messenger,
                    "query_local_agents",
                    return_value=[sender, recipient],
                ),
                mock.patch.object(agent_messenger, "ssh_hosts", return_value=[]),
            ):
                app = agent_messenger.MessengerApp(mock.Mock(), sender, environment)

        for query in ("purple-koala", "w3:pQ", "019ff833"):
            app.search = query
            self.assertEqual(app.filtered_agents(), [recipient])

    def test_estimated_recipient_counts_include_remote_cache_groups(self):
        sender = agent()
        local = agent(name="white-bison", pane_id="w1:p2", session_id="local-2")
        cache = mock.Mock()
        remote = {
            "macbook-pro": [
                agent_directory.replace(
                    agent(
                        name="purple-koala",
                        pane_id="w2:p1",
                        session_id="remote-1",
                        workspace_id="w2",
                    ),
                    host="macbook-pro",
                    local=False,
                ),
                agent_directory.replace(
                    agent(
                        name="brown-fox",
                        pane_id="w2:p2",
                        session_id="remote-2",
                        workspace_id="w2",
                    ),
                    host="macbook-pro",
                    local=False,
                ),
            ],
            "winmini": [
                agent_directory.replace(
                    agent(
                        name="orange-lemur",
                        pane_id="w3:p1",
                        session_id="remote-3",
                        workspace_id="w3",
                    ),
                    host="winmini",
                    local=False,
                )
            ],
        }
        cache.agents.side_effect = remote.get
        with (
            mock.patch.object(
                agent_messenger.AgentCache,
                "from_environment",
                return_value=cache,
            ),
            mock.patch.object(
                agent_messenger,
                "ssh_hosts",
                return_value=["macbook-pro", "winmini"],
            ),
        ):
            counts = agent_messenger.estimated_recipient_counts(
                [sender, local],
                sender,
                {},
            )
        self.assertEqual(counts, (4, 6))

    def test_focused_pane_id_prefers_plugin_context(self):
        environment = {
            "HERDR_PLUGIN_CONTEXT_JSON": '{"focused_pane_id":"w2:p3"}',
            "HERDR_ACTIVE_PANE_ID": "w1:p1",
        }
        self.assertEqual(agent_messenger.focused_pane_id(environment), "w2:p3")

    def test_launch_notifies_when_focused_pane_has_no_agent(self):
        environment = {"HERDR_PANE_ID": "w1:p1", "LANG": "en_US.UTF-8"}
        with (
            mock.patch.object(agent_messenger, "query_local_agents", return_value=[]),
            mock.patch.object(agent_messenger, "show_notification", return_value=True) as notify,
            mock.patch.object(agent_messenger, "launch_popup") as popup,
        ):
            self.assertEqual(agent_messenger.launch(environment), 0)
        self.assertIn("No agent is running", notify.call_args.args[0])
        popup.assert_not_called()

    def test_launch_opens_popup_for_focused_agent(self):
        environment = {"HERDR_PANE_ID": "w1:p1", "LANG": "en_US.UTF-8"}
        sender = agent()
        recipient = agent(name="white-bison", pane_id="w1:p2", session_id="session-2")
        with (
            mock.patch.object(
                agent_messenger,
                "query_local_agents",
                return_value=[sender, recipient],
            ),
            mock.patch.object(agent_messenger, "launch_popup", return_value=True) as popup,
            mock.patch.object(
                agent_messenger,
                "estimated_recipient_counts",
                return_value=(1, 0),
            ),
        ):
            self.assertEqual(agent_messenger.launch(environment), 0)
        popup.assert_called_once_with(
            "w1:p1",
            environment,
            recipient_count=1,
            group_count=0,
        )

    def test_launch_popup_height_scales_with_known_recipients(self):
        completed = mock.Mock(returncode=0)
        with mock.patch.object(agent_messenger, "run_herdr", return_value=completed) as run:
            self.assertTrue(
                agent_messenger.launch_popup("w1:p1", {}, recipient_count=4)
            )
        arguments = run.call_args.args[0]
        self.assertEqual(arguments[arguments.index("--width") + 1], "120")
        self.assertEqual(arguments[arguments.index("--height") + 1], "17")
        self.assertEqual(agent_messenger.desired_popup_height(0), 15)
        self.assertEqual(agent_messenger.desired_popup_height(50), 32)

    def test_popup_expands_for_large_viewport(self):
        layout = mock.Mock(
            returncode=0,
            stdout=(
                '{"result":{"layout":{"area":{"width":293,"height":84}}}}'
            ),
        )
        opened = mock.Mock(returncode=0, stdout="")
        with mock.patch.object(
            agent_messenger,
            "run_herdr",
            side_effect=[layout, opened],
        ) as run:
            self.assertTrue(
                agent_messenger.launch_popup("w1:p1", {}, recipient_count=25)
            )
        arguments = run.call_args_list[-1].args[0]
        self.assertEqual(arguments[arguments.index("--width") + 1], "120")
        self.assertEqual(arguments[arguments.index("--height") + 1], "32")

    def test_popup_dimensions_fit_small_viewport(self):
        layout = mock.Mock(
            returncode=0,
            stdout=(
                '{"result":{"layout":{"area":{"width":80,"height":29}}}}'
            ),
        )
        opened = mock.Mock(returncode=0, stdout="")
        with mock.patch.object(
            agent_messenger,
            "run_herdr",
            side_effect=[layout, opened],
        ) as run:
            self.assertTrue(
                agent_messenger.launch_popup("w1:p1", {}, recipient_count=50)
            )
        arguments = run.call_args_list[-1].args[0]
        self.assertEqual(arguments[arguments.index("--width") + 1], "72")
        self.assertEqual(arguments[arguments.index("--height") + 1], "23")

    def test_legacy_skill_guide_action_opens_interactive_skill_popup(self):
        completed = mock.Mock(returncode=0)
        with mock.patch.object(agent_messenger, "run_herdr", return_value=completed) as run:
            self.assertEqual(agent_messenger.launch_skill_guide({}), 0)
        arguments = run.call_args.args[0]
        self.assertEqual(
            arguments[arguments.index("--entrypoint") + 1],
            agent_messenger.SKILL_INSTALLER_ENTRYPOINT,
        )
        self.assertEqual(arguments[arguments.index("--width") + 1], "60")
        self.assertEqual(
            arguments[arguments.index("--height") + 1],
            str(agent_messenger.SKILL_INSTALLER_HEIGHT),
        )
        self.assertTrue(agent_messenger.bundled_skill_path().is_file())

    def test_language_detection_supports_three_locales(self):
        self.assertEqual(messenger_i18n.detect_language({"LANG": "en_US.UTF-8"}), "en")
        self.assertEqual(messenger_i18n.detect_language({"LANG": "ja_JP.UTF-8"}), "ja")
        self.assertEqual(messenger_i18n.detect_language({"LANG": "ko_KR.UTF-8"}), "ko")

    def test_unknown_language_falls_back_to_english(self):
        with mock.patch.object(messenger_i18n, "_macos_language", return_value=None):
            self.assertEqual(messenger_i18n.detect_language({"LANG": "fr_FR.UTF-8"}), "en")

    def test_delivery_mode_copy_and_key_help_exist_in_every_language(self):
        required = {
            "coordinator",
            "mode_question",
            "delivery_mode",
            "delegate_option",
            "delegate_privacy",
            "direct_option",
            "direct_privacy",
            "help_mode",
            "remote_warning_summary",
            "remote_details_help",
        }
        english_keys = set(messenger_i18n.messages("en"))
        for language in messenger_i18n.SUPPORTED_LANGUAGES:
            text = messenger_i18n.messages(language)
            self.assertEqual(set(text), english_keys, language)
            self.assertTrue(required.issubset(text), language)
            self.assertIn("Ctrl+O", text["help_recipients"])
            self.assertIn("Ctrl+O", text["help_message"])
            self.assertIn("Ctrl+G", text["help_recipients"])
            self.assertIn("host/label", text["skill_guide_target"])

    def test_locale_precedence_stops_at_first_configured_value(self):
        self.assertEqual(
            messenger_i18n.detect_language(
                {"LC_ALL": "fr_FR.UTF-8", "LANG": "ko_KR.UTF-8"}
            ),
            "en",
        )
        self.assertEqual(
            messenger_i18n.detect_language(
                {"LC_ALL": "", "LC_MESSAGES": "ja_JP.UTF-8", "LANG": "ko_KR.UTF-8"}
            ),
            "ja",
        )

    def test_display_width_accounts_for_wide_characters(self):
        self.assertEqual(agent_messenger.display_width("Agent 한글"), 10)

    def test_shortcut_help_wraps_without_losing_any_items(self):
        for language in messenger_i18n.SUPPORTED_LANGUAGES:
            text = messenger_i18n.messages(language)
            for key in (
                "help_discovery",
                "help_mode",
                "help_recipients",
                "help_message",
                "help_sending",
            ):
                lines = agent_messenger.wrap_help_text(text[key], 71)
                self.assertTrue(
                    all(agent_messenger.display_width(line) <= 71 for line in lines),
                    (language, key),
                )
                self.assertTrue(lines, (language, key))
            self.assertEqual(
                len(agent_messenger.wrap_help_text(text["help_recipients"], 71)),
                2,
                language,
            )

    def test_recipient_footer_displays_refresh_skill_and_close_shortcuts(self):
        sender = agent()
        screen = mock.Mock()
        screen.getmaxyx.return_value = (18, 72)
        with tempfile.TemporaryDirectory() as state_directory:
            environment = {
                "LANG": "ko_KR.UTF-8",
                "HERDR_PLUGIN_STATE_DIR": state_directory,
            }
            with (
                mock.patch.object(
                    agent_messenger,
                    "query_local_agents",
                    return_value=[sender],
                ),
                mock.patch.object(agent_messenger, "ssh_hosts", return_value=[]),
            ):
                app = agent_messenger.MessengerApp(screen, sender, environment)

        rendered: list[tuple[int, str, int]] = []
        with mock.patch.object(
            app,
            "_safe_add",
            side_effect=lambda row, _column, value, attribute=0: rendered.append(
                (row, value, attribute)
            ),
        ):
            line_count = app._render_help_footer(app.text["help_recipients"])

        self.assertEqual(line_count, 2)
        self.assertEqual(sorted({row for row, _value, _attribute in rendered}), [16, 17])
        combined = "".join(value for _row, value, _attribute in rendered)
        self.assertIn("[Ctrl+R] 새로고침", combined)
        self.assertIn("[Ctrl+G] 스킬", combined)
        self.assertIn("[Esc] 닫기", combined)
        keycaps = [
            attribute
            for _row, value, attribute in rendered
            if value in {"[↑↓]", "[Space]", "[Ctrl+R]", "[Ctrl+G]", "[Esc]"}
        ]
        self.assertTrue(keycaps)
        self.assertTrue(all(attribute & agent_messenger.curses.A_BOLD for attribute in keycaps))
        self.assertTrue(
            all(not attribute & agent_messenger.curses.A_REVERSE for attribute in keycaps)
        )

    def test_ctrl_g_exits_messenger_to_open_interactive_skill_popup(self):
        sender = agent()
        with tempfile.TemporaryDirectory() as state_directory:
            environment = {
                "LANG": "en_US.UTF-8",
                "HERDR_PLUGIN_STATE_DIR": state_directory,
            }
            with (
                mock.patch.object(
                    agent_messenger,
                    "query_local_agents",
                    return_value=[sender],
                ),
                mock.patch.object(agent_messenger, "ssh_hosts", return_value=[]),
            ):
                app = agent_messenger.MessengerApp(mock.Mock(), sender, environment)

        app.mode_choice = True
        app.section = "message"
        app.message_lines = ["keep this"]
        app.handle_key(agent_messenger.SKILL_GUIDE_KEY)
        self.assertTrue(app.open_skill_installer)
        self.assertFalse(app.running)
        self.assertEqual(app.section, "message")
        self.assertEqual(app.message_lines, ["keep this"])

    def test_question_mark_is_inserted_while_editing_message(self):
        sender = agent()
        with tempfile.TemporaryDirectory() as state_directory:
            environment = {
                "LANG": "en_US.UTF-8",
                "HERDR_PLUGIN_STATE_DIR": state_directory,
            }
            with (
                mock.patch.object(
                    agent_messenger,
                    "query_local_agents",
                    return_value=[sender],
                ),
                mock.patch.object(agent_messenger, "ssh_hosts", return_value=[]),
            ):
                app = agent_messenger.MessengerApp(mock.Mock(), sender, environment)

        app.mode_choice = True
        app.section = "message"
        app.message_lines = ["Can you review"]
        app.message_column = len(app.message_lines[0])
        app.handle_key("?")

        self.assertFalse(app.open_skill_installer)
        self.assertEqual(app.message_lines, ["Can you review?"])

    def test_skill_guide_wraps_long_paths_inside_popup(self):
        screen = mock.Mock()
        screen.getmaxyx.return_value = (14, 42)
        with mock.patch.object(agent_messenger.curses, "curs_set"):
            agent_messenger.render_skill_guide_screen(
                screen,
                messenger_i18n.messages("en"),
                Path(
                    "/a/very/long/plugin/path/skills/herdr-agent-messenger/SKILL.md"
                ),
            )
        for call in screen.addnstr.call_args_list:
            row, column, _value, maximum = call.args[:4]
            self.assertLess(row, 14)
            self.assertLess(column, 42)
            self.assertLessEqual(maximum, 41 - column)

    def test_message_soft_wrap_preserves_text_and_maps_cursor(self):
        message = "현재 프로젝트 내용을 분석하고 리뷰한 내용을 종합해서 보고해줘."
        wrapped, cursor_row, cursor_column = agent_messenger.wrap_message_lines(
            [message],
            width=16,
            cursor_row=0,
            cursor_column=len(message),
        )

        self.assertEqual("".join(line.text for line in wrapped), message)
        self.assertTrue(
            all(agent_messenger.display_width(line.text) <= 16 for line in wrapped)
        )
        self.assertEqual(cursor_row, len(wrapped) - 1)
        self.assertEqual(
            cursor_column,
            agent_messenger.display_width(wrapped[-1].text),
        )

    def test_cursor_at_soft_wrap_boundary_moves_to_next_visual_line(self):
        wrapped, cursor_row, cursor_column = agent_messenger.wrap_message_lines(
            ["abcdef"],
            width=3,
            cursor_row=0,
            cursor_column=3,
        )

        self.assertEqual([line.text for line in wrapped], ["abc", "def"])
        self.assertEqual((cursor_row, cursor_column), (1, 0))

    def test_display_column_maps_to_cjk_character_boundary(self):
        self.assertEqual(
            agent_messenger.character_index_at_display_column("가나다", 3),
            1,
        )

    def test_wide_status_text_is_clipped_in_terminal_cells_before_drawing(self):
        app = agent_messenger.MessengerApp.__new__(agent_messenger.MessengerApp)
        app.screen = mock.Mock()
        app.screen.getmaxyx.return_value = (4, 10)
        app._safe_add(1, 0, "가나다라마")
        rendered = app.screen.addnstr.call_args.args[2]
        self.assertEqual(rendered, "가나다라…")
        self.assertLessEqual(agent_messenger.display_width(rendered), 9)

    def test_vertical_cursor_movement_follows_the_rendered_wrap_width(self):
        sender = agent()
        screen = mock.Mock()
        screen.getmaxyx.return_value = (32, 45)
        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch.object(agent_messenger, "query_local_agents", return_value=[sender]),
            mock.patch.object(agent_messenger, "ssh_hosts", return_value=[]),
        ):
            app = agent_messenger.MessengerApp(
                screen, sender, {"HERDR_PLUGIN_STATE_DIR": directory, "LANG": "en_US.UTF-8"},
            )
        app.section = "message"
        for text in ("x" * 80, "가a" * 30):
            with self.subTest(text=text):
                app.message_lines = [text]
                app.message_row = 0
                app.message_column = 2
                app._render_message(0, 8)
                before = app.message_cursor
                app._handle_message_key(agent_messenger.curses.KEY_DOWN)
                app._render_message(0, 8)
                self.assertEqual(app.message_cursor, (before[0] + 1, before[1]))
                app._handle_message_key(agent_messenger.curses.KEY_UP)
                app._render_message(0, 8)
                self.assertEqual(app.message_cursor, before)

    def test_discovery_choice_supports_arrows_and_enter(self):
        sender = agent()
        with tempfile.TemporaryDirectory() as state_directory:
            environment = {
                "LANG": "en_US.UTF-8",
                "HERDR_PLUGIN_STATE_DIR": state_directory,
            }
            with (
                mock.patch.object(
                    agent_messenger,
                    "query_local_agents",
                    return_value=[sender],
                ),
                mock.patch.object(
                    agent_messenger,
                    "ssh_hosts",
                    return_value=["macbook"],
                ),
            ):
                app = agent_messenger.MessengerApp(mock.Mock(), sender, environment)

        self.assertFalse(app.discovery_choice)
        app.handle_key(agent_messenger.curses.KEY_DOWN)
        self.assertEqual(app.discovery_option, 1)
        with mock.patch.object(app, "_start_remote_discovery") as discover:
            app.handle_key("\n")
        self.assertTrue(app.discovery_choice)
        self.assertFalse(app.mode_choice)
        self.assertFalse(app.remote_enabled)
        discover.assert_not_called()

    def test_delivery_mode_defaults_to_delegate_and_supports_keyboard_flow(self):
        sender = agent()
        with tempfile.TemporaryDirectory() as state_directory:
            environment = {
                "LANG": "en_US.UTF-8",
                "HERDR_PLUGIN_STATE_DIR": state_directory,
            }
            with (
                mock.patch.object(
                    agent_messenger,
                    "query_local_agents",
                    return_value=[sender],
                ),
                mock.patch.object(agent_messenger, "ssh_hosts", return_value=[]),
            ):
                app = agent_messenger.MessengerApp(mock.Mock(), sender, environment)

        self.assertTrue(app.discovery_choice)
        self.assertFalse(app.mode_choice)
        self.assertEqual(app.mode_option, 0)
        self.assertEqual(app.delivery_mode, agent_messenger.DELIVERY_DELEGATE)

        app.handle_key(agent_messenger.curses.KEY_DOWN)
        self.assertEqual(app.mode_option, 1)
        app.handle_key("\n")
        self.assertTrue(app.mode_choice)
        self.assertEqual(app.delivery_mode, agent_messenger.DELIVERY_DIRECT)

        app.handle_key("\x0f")
        self.assertFalse(app.mode_choice)
        self.assertTrue(app.mode_return_to_editor)
        app.handle_key("\x1b")
        self.assertTrue(app.mode_choice)
        self.assertEqual(app.delivery_mode, agent_messenger.DELIVERY_DIRECT)

        app.handle_key("\x0f")
        app.handle_key("c")
        self.assertTrue(app.mode_choice)
        self.assertEqual(app.delivery_mode, agent_messenger.DELIVERY_DELEGATE)

        app.handle_key("\x0f")
        app.handle_key("d")
        self.assertTrue(app.mode_choice)
        self.assertEqual(app.delivery_mode, agent_messenger.DELIVERY_DIRECT)

    def test_escape_from_initial_mode_choice_returns_to_ssh_discovery(self):
        sender = agent()
        with tempfile.TemporaryDirectory() as state_directory:
            environment = {
                "LANG": "en_US.UTF-8",
                "HERDR_PLUGIN_STATE_DIR": state_directory,
            }
            with (
                mock.patch.object(
                    agent_messenger,
                    "query_local_agents",
                    return_value=[sender],
                ),
                mock.patch.object(
                    agent_messenger,
                    "ssh_hosts",
                    return_value=["macbook"],
                ),
            ):
                app = agent_messenger.MessengerApp(mock.Mock(), sender, environment)

        app.handle_key("l")
        self.assertTrue(app.discovery_choice)
        self.assertFalse(app.mode_choice)
        app.handle_key("\x1b")
        self.assertFalse(app.discovery_choice)
        self.assertTrue(app.running)

    def test_local_only_cancels_discovery_and_removes_remote_agents(self):
        sender = agent()
        local_recipient = agent(
            name="red-fox",
            pane_id="w1:p2",
            session_id="session-2",
        )
        remote_recipient = agent_directory.replace(
            local_recipient,
            host="macbook",
            name="white-owl",
            pane_id="w2:p1",
            session_id="remote-session",
            local=False,
        )
        with tempfile.TemporaryDirectory() as state_directory:
            environment = {
                "LANG": "en_US.UTF-8",
                "HERDR_PLUGIN_STATE_DIR": state_directory,
            }
            with (
                mock.patch.object(
                    agent_messenger,
                    "query_local_agents",
                    return_value=[sender, local_recipient],
                ),
                mock.patch.object(
                    agent_messenger,
                    "ssh_hosts",
                    return_value=["macbook"],
                ),
            ):
                app = agent_messenger.MessengerApp(mock.Mock(), sender, environment)

        discovery = mock.Mock()
        app.discovery_choice = True
        app.mode_choice = False
        app.discovery = discovery
        app.remote_enabled = True
        app.pending_hosts = {"macbook"}
        app.host_status = {"macbook": app.text["refreshing"]}
        app.host_errors = {"macbook": "timeout"}
        app.agents.append(remote_recipient)
        app.selected = {local_recipient.identity, remote_recipient.identity}

        app.handle_key("\x1b")
        app.handle_key("l")

        discovery.cancel.assert_called_once_with()
        self.assertIsNone(app.discovery)
        self.assertFalse(app.remote_enabled)
        self.assertFalse(app.pending_hosts)
        self.assertFalse(app.host_status)
        self.assertFalse(app.host_errors)
        self.assertEqual(app.agents, [local_recipient])
        self.assertEqual(app.selected, {local_recipient.identity})

    def test_remote_failures_use_compact_warning_and_scrollable_details(self):
        sender = agent()
        screen = mock.Mock()
        screen.getmaxyx.return_value = (7, 52)
        with tempfile.TemporaryDirectory() as state_directory:
            environment = {
                "LANG": "en_US.UTF-8",
                "HERDR_PLUGIN_STATE_DIR": state_directory,
            }
            with (
                mock.patch.object(
                    agent_messenger,
                    "query_local_agents",
                    return_value=[sender],
                ),
                mock.patch.object(agent_messenger, "ssh_hosts", return_value=[]),
            ):
                app = agent_messenger.MessengerApp(screen, sender, environment)

        app.remote_enabled = True
        app.host_status = {
            "available-host": "",
            **{
                f"failed-host-{index}": app.text["unavailable"]
                for index in range(5)
            },
        }
        app.host_errors = {
            f"failed-host-{index}": f"connection error {index}"
            for index in range(5)
        }

        summary = app._remote_summary()
        self.assertTrue(summary.startswith("⚠ 5 remote unavailable"))
        self.assertIn("Ctrl+U Details", summary)
        self.assertIn("1 available", summary)
        self.assertNotIn("failed-host-0", summary)

        app.handle_key(agent_messenger.REMOTE_DETAILS_KEY)
        self.assertTrue(app.remote_details_visible)
        app.handle_key(agent_messenger.curses.KEY_DOWN)
        self.assertEqual(app.remote_details_offset, 1)

        rendered: list[str] = []
        with (
            mock.patch.object(
                app,
                "_safe_add",
                side_effect=lambda _row, _column, value, _attribute=0: rendered.append(
                    value
                ),
            ),
            mock.patch.object(app, "_set_cursor_visibility"),
        ):
            app._render_remote_details()
        self.assertTrue(any("failed-host-1" in value for value in rendered))
        self.assertTrue(any("connection error 1" in value for value in rendered))

        app.handle_key("\x1b")
        self.assertFalse(app.remote_details_visible)

    def test_mode_privacy_copy_wraps_on_narrow_screens_in_every_language(self):
        sender = agent()
        for language in messenger_i18n.SUPPORTED_LANGUAGES:
            environment = {"LANG": f"{language}_TEST.UTF-8"}
            screen = mock.Mock()
            screen.getmaxyx.return_value = (22, 44)
            with tempfile.TemporaryDirectory() as state_directory:
                environment["HERDR_PLUGIN_STATE_DIR"] = state_directory
                with (
                    mock.patch.object(
                        agent_messenger,
                        "query_local_agents",
                        return_value=[sender],
                    ),
                    mock.patch.object(agent_messenger, "ssh_hosts", return_value=[]),
                ):
                    app = agent_messenger.MessengerApp(screen, sender, environment)

            rendered: list[tuple[int, int, str]] = []

            def record(row, column, value, _attribute=0):
                rendered.append((row, column, value))

            with mock.patch.object(app, "_safe_add", side_effect=record):
                app._render_mode_choice(3)

            descriptions = "".join(
                value for _row, column, value in rendered if column == 6
            )
            self.assertEqual(
                descriptions,
                app.text["delegate_privacy"] + app.text["direct_privacy"],
                language,
            )
            self.assertTrue(
                all(
                    agent_messenger.display_width(value) <= 37
                    for _row, column, value in rendered
                    if column == 6
                ),
                language,
            )
            for mode, privacy_key in (
                (agent_messenger.DELIVERY_DELEGATE, "delegate_privacy"),
                (agent_messenger.DELIVERY_DIRECT, "direct_privacy"),
            ):
                rendered.clear()
                app.delivery_mode = mode
                with mock.patch.object(app, "_safe_add", side_effect=record):
                    app._render_mode_summary(3)
                summary = "".join(
                    value for _row, column, value in rendered if column == 2
                )
                self.assertEqual(summary, app.text[privacy_key], language)

    def test_color_pairs_use_terminal_palette(self):
        sender = agent()
        with tempfile.TemporaryDirectory() as state_directory:
            environment = {
                "LANG": "en_US.UTF-8",
                "HERDR_PLUGIN_STATE_DIR": state_directory,
            }
            with (
                mock.patch.object(
                    agent_messenger,
                    "query_local_agents",
                    return_value=[sender],
                ),
                mock.patch.object(agent_messenger, "ssh_hosts", return_value=[]),
            ):
                app = agent_messenger.MessengerApp(mock.Mock(), sender, environment)

        with (
            mock.patch.object(agent_messenger.curses, "has_colors", return_value=True),
            mock.patch.object(agent_messenger.curses, "start_color"),
            mock.patch.object(agent_messenger.curses, "use_default_colors"),
            mock.patch.object(agent_messenger.curses, "init_pair") as init_pair,
        ):
            app._initialize_colors()

        self.assertTrue(app.colors_enabled)
        self.assertEqual(init_pair.call_count, 4)
        self.assertTrue(all(call.args[2] == -1 for call in init_pair.call_args_list))

    def test_message_cursor_is_placed_after_footer_rendering(self):
        sender = agent()
        screen = mock.Mock()
        screen.getmaxyx.return_value = (34, 110)
        with tempfile.TemporaryDirectory() as state_directory:
            environment = {
                "LANG": "en_US.UTF-8",
                "HERDR_PLUGIN_STATE_DIR": state_directory,
            }
            with (
                mock.patch.object(
                    agent_messenger,
                    "query_local_agents",
                    return_value=[sender],
                ),
                mock.patch.object(agent_messenger, "ssh_hosts", return_value=[]),
            ):
                app = agent_messenger.MessengerApp(screen, sender, environment)

        app.mode_choice = True
        app.section = "message"
        app.message_lines = ["hello"]
        app.message_column = 5
        with mock.patch.object(app, "_set_cursor_visibility"):
            app.render()

        self.assertEqual(screen.move.call_args.args, app.message_cursor)
        self.assertEqual(app.message_cursor[1], 7)

    def test_long_message_cursor_remains_inside_editor(self):
        sender = agent()
        screen = mock.Mock()
        screen.getmaxyx.return_value = (22, 44)
        with tempfile.TemporaryDirectory() as state_directory:
            environment = {
                "LANG": "ko_KR.UTF-8",
                "HERDR_PLUGIN_STATE_DIR": state_directory,
            }
            with (
                mock.patch.object(
                    agent_messenger,
                    "query_local_agents",
                    return_value=[sender],
                ),
                mock.patch.object(agent_messenger, "ssh_hosts", return_value=[]),
            ):
                app = agent_messenger.MessengerApp(screen, sender, environment)

        app.mode_choice = True
        app.section = "message"
        app.message_lines = ["긴 메시지를 입력해도 화면에서 잘리지 않아야 합니다. " * 4]
        app.message_column = len(app.message_lines[0])
        with mock.patch.object(app, "_set_cursor_visibility"):
            app.render()

        cursor_row, cursor_column = screen.move.call_args.args
        self.assertLess(cursor_row, 20)
        self.assertLess(cursor_column, 43)
        self.assertEqual(screen.move.call_args.args, app.message_cursor)

    def test_arrow_keys_move_between_soft_wrapped_message_rows(self):
        sender = agent()
        screen = mock.Mock()
        screen.getmaxyx.return_value = (22, 12)
        with tempfile.TemporaryDirectory() as state_directory:
            environment = {
                "LANG": "en_US.UTF-8",
                "HERDR_PLUGIN_STATE_DIR": state_directory,
            }
            with (
                mock.patch.object(
                    agent_messenger,
                    "query_local_agents",
                    return_value=[sender],
                ),
                mock.patch.object(agent_messenger, "ssh_hosts", return_value=[]),
            ):
                app = agent_messenger.MessengerApp(screen, sender, environment)

        app.mode_choice = True
        app.section = "message"
        app.message_lines = ["abcdefghijklmnopqrst"]
        app.message_column = 20
        app.handle_key(agent_messenger.curses.KEY_UP)
        self.assertEqual((app.message_row, app.message_column), (0, 13))
        app.handle_key(agent_messenger.curses.KEY_UP)
        self.assertEqual((app.message_row, app.message_column), (0, 6))

    def test_ctrl_s_invokes_send_from_message_editor(self):
        sender = agent()
        with tempfile.TemporaryDirectory() as state_directory:
            environment = {
                "LANG": "en_US.UTF-8",
                "HERDR_PLUGIN_STATE_DIR": state_directory,
            }
            with (
                mock.patch.object(
                    agent_messenger,
                    "query_local_agents",
                    return_value=[sender],
                ),
                mock.patch.object(agent_messenger, "ssh_hosts", return_value=[]),
            ):
                app = agent_messenger.MessengerApp(mock.Mock(), sender, environment)

        app.mode_choice = True
        app.section = "message"
        with mock.patch.object(app, "_send") as send:
            app.handle_key("\x13")
        send.assert_called_once_with()

    def test_local_refresh_removes_disappeared_agent_selection(self):
        sender = agent()
        recipient = agent(name="red-fox", pane_id="w1:p2", session_id="session-2")
        with tempfile.TemporaryDirectory() as state_directory:
            environment = {
                "LANG": "en_US.UTF-8",
                "HERDR_PLUGIN_STATE_DIR": state_directory,
            }
            with (
                mock.patch.object(
                    agent_messenger,
                    "query_local_agents",
                    return_value=[sender, recipient],
                ),
                mock.patch.object(agent_messenger, "ssh_hosts", return_value=[]),
            ):
                app = agent_messenger.MessengerApp(mock.Mock(), sender, environment)

        app.mode_choice = True
        app.selected.add(recipient.identity)
        with mock.patch.object(
            agent_messenger,
            "query_local_agents",
            return_value=[sender],
        ):
            app.handle_key("\x12")
        self.assertNotIn(recipient.identity, app.selected)

    def test_remote_scope_does_not_replace_local_agents_with_same_host(self):
        sender = agent()
        local_recipient = agent(
            name="red-fox",
            pane_id="w1:p2",
            session_id="session-2",
        )
        remote_recipient = agent_directory.replace(
            local_recipient,
            local=False,
            name="white-owl",
            pane_id="w2:p1",
            session_id="remote-session",
        )
        with tempfile.TemporaryDirectory() as state_directory:
            environment = {
                "LANG": "en_US.UTF-8",
                "HERDR_PLUGIN_STATE_DIR": state_directory,
            }
            with (
                mock.patch.object(
                    agent_messenger,
                    "query_local_agents",
                    return_value=[sender, local_recipient],
                ),
                mock.patch.object(agent_messenger, "ssh_hosts", return_value=[]),
            ):
                app = agent_messenger.MessengerApp(mock.Mock(), sender, environment)

        app._replace_agent_scope(
            local=False,
            host=local_recipient.host,
            records=[remote_recipient],
        )
        self.assertIn(local_recipient, app.agents)
        self.assertIn(remote_recipient, app.agents)

    @unittest.skipIf(agent_messenger.termios is None, "termios unavailable")
    def test_terminal_flow_control_is_disabled_and_restored(self):
        stream = mock.Mock()
        stream.fileno.return_value = 7
        original = [
            agent_messenger.termios.IXON | agent_messenger.termios.IXOFF | 1,
            0,
            0,
            0,
            0,
            0,
            [],
        ]
        with (
            mock.patch.object(
                agent_messenger.termios,
                "tcgetattr",
                return_value=original,
            ),
            mock.patch.object(agent_messenger.termios, "tcsetattr") as set_attributes,
        ):
            with agent_messenger.terminal_flow_control_disabled(stream):
                pass

        disabled = set_attributes.call_args_list[0].args[2]
        self.assertFalse(disabled[0] & agent_messenger.termios.IXON)
        self.assertFalse(disabled[0] & agent_messenger.termios.IXOFF)
        self.assertEqual(set_attributes.call_args_list[1].args[2], original)

    def test_delegate_sends_one_orchestration_request_to_coordinator_only(self):
        sender = agent()
        first = agent(name="red-fox", pane_id="w1:p2", session_id="session-2")
        second = agent(name="white-owl", pane_id="w1:p3", session_id="session-3")
        with tempfile.TemporaryDirectory() as state_directory:
            environment = {
                "LANG": "en_US.UTF-8",
                "HERDR_PLUGIN_STATE_DIR": state_directory,
            }
            with (
                mock.patch.object(
                    agent_messenger,
                    "query_local_agents",
                    return_value=[sender, first, second],
                ),
                mock.patch.object(agent_messenger, "ssh_hosts", return_value=[]),
            ):
                app = agent_messenger.MessengerApp(mock.Mock(), sender, environment)

        app.selected = {first.identity, second.identity}
        original_request = "\n  Please review this change.  \n"
        app.message_lines = original_request.split("\n")
        results = [agent_directory.SendResult(sender, True)]
        with (
            mock.patch.object(agent_messenger, "fetch_local_agent", return_value=sender),
            mock.patch.object(
                agent_messenger,
                "dispatch_prompts",
                return_value=results,
            ) as dispatch,
            mock.patch.object(agent_messenger, "show_notification"),
            mock.patch.object(agent_messenger.threading, "Thread", ImmediateThread),
        ):
            app._send()
            app._poll_send()

        self.assertFalse(app.running)
        self.assertEqual(dispatch.call_count, 1)
        self.assertEqual(dispatch.call_args.args[1], (sender,))
        self.assertNotIn(first, dispatch.call_args.args[1])
        self.assertNotIn(second, dispatch.call_args.args[1])
        orchestration_request = dispatch.call_args.args[2]
        self.assertIn(first.qualified_name, orchestration_request)
        self.assertIn(second.qualified_name, orchestration_request)
        self.assertIn(
            "--- BEGIN ORIGINAL REQUEST ---\n"
            f"{original_request}\n"
            "--- END ORIGINAL REQUEST ---",
            orchestration_request,
        )
        self.assertIn(
            f"{agent_messenger.bundled_router_command()} batch --requests-json",
            orchestration_request,
        )
        self.assertIn(f"--address {first.qualified_name}", orchestration_request)
        self.assertIn(f"--address {second.qualified_name}", orchestration_request)
        self.assertNotIn(agent_skill_cli.encode_agent_route(first), orchestration_request)
        self.assertNotIn(agent_skill_cli.encode_agent_route(second), orchestration_request)

    def test_single_recipient_uses_address_and_preserves_original_task(self):
        recipient = agent_directory.replace(
            agent(name="red-fox", pane_id="w1:p2", session_id="session-2"),
            host="winmini",
        )
        original = "\n  Review the failing test.  \n"
        request = agent_messenger.build_orchestration_request([recipient], original)

        self.assertIn("--address local/red-fox", request)
        self.assertNotIn("--address winmini/red-fox", request)
        self.assertIn(
            f"{agent_messenger.bundled_router_command()} request --address",
            request,
        )
        self.assertNotIn(" batch --requests-json", request)
        self.assertNotIn(agent_skill_cli.encode_agent_route(recipient), request)
        self.assertIn(
            f"--- BEGIN ORIGINAL REQUEST ---\n{original}\n--- END ORIGINAL REQUEST ---",
            request,
        )

    def test_multiple_recipients_keep_host_workspace_and_status(self):
        first = agent(name="red-fox", pane_id="w1:p2", session_id="session-2")
        second = agent_directory.replace(
            agent(name="white-owl", pane_id="w2:p3", session_id="session-3"),
            host="macbook-pro", local=False, route_target="w2:p3", status="working",
        )
        request = agent_messenger.build_orchestration_request(
            [first, second], "Review both implementations."
        )
        self.assertIn(
            f"{agent_messenger.bundled_router_command()} batch --requests-json",
            request,
        )
        self.assertIn('"address":"HOST/LABEL"', request)
        for recipient in (first, second):
            self.assertIn(
                f"--address {recipient.qualified_name}; "
                f"workspace: {recipient.workspace_label}; status: {recipient.status}",
                request,
            )
            self.assertNotIn(agent_skill_cli.encode_agent_route(recipient), request)

    def test_orchestration_preserves_unicode_workspace_on_one_line(self):
        workspace = ("장기 결제 마이그레이션 🚀 " * 20) + "\n\t최종 검증 작업공간"
        recipient = agent(name="white-bison", workspace_label=workspace)
        request = agent_messenger.build_orchestration_request(
            [recipient], "Inspect the migration."
        )
        self.assertIn(f"workspace: {' '.join(workspace.split())}; status: idle", request)
        self.assertNotIn(workspace, request)

    def test_orchestration_commands_run_without_persistent_shell_state(self):
        first = agent(name="red-fox", pane_id="w1:p2", session_id="session-2")
        second = agent(name="white-owl", pane_id="w1:p3", session_id="session-3")
        root = Path("/work/Herdr Router's tools")
        launcher = root / "scripts" / "herdr_agent_messenger.py"
        for recipients in ([first], [first, second]):
            with (
                self.subTest(target_count=len(recipients)),
                mock.patch.object(agent_messenger, "bundled_skill_path", return_value=root / "SKILL.md"),
            ):
                request = agent_messenger.build_orchestration_request(recipients, "Review")
                self.assertEqual(
                    agent_messenger.bundled_router_command(),
                    f"python3 {shlex.quote(str(launcher))}",
                )
                self.assertNotIn("cd ", request)
                commands = [
                    shlex.split(line) for line in request.splitlines()
                    if line.startswith("python3 ")
                ]
                self.assertEqual(len(commands), 2)
                for command in commands:
                    self.assertEqual(command[:2], ["python3", str(launcher)])
                self.assertEqual(
                    commands[0][2:], ["request-status", "--request-id", "ID", "--wait", "MS"]
                )
                self.assertEqual(commands[1][2], "request" if len(recipients) == 1 else "batch")
                self.assertNotIn("ham()", request)

    def test_orchestration_exposes_compact_lifecycle_and_exit_contract(self):
        first = agent(name="red-fox", pane_id="w1:p2", session_id="session-2")
        second = agent(name="white-owl", pane_id="w1:p3", session_id="session-3")
        for recipients in ([first], [first, second]):
            with self.subTest(target_count=len(recipients)):
                request = agent_messenger.build_orchestration_request(recipients, "Review")
                self.assertIn("compact JSON", request)
                for state in ("deferred", "awaiting_decision", "submitted_*"):
                    self.assertIn(state, request)
                self.assertIn("do not resend", request)
                self.assertIn("0 = settled/succeeded", request)
                self.assertIn("1 = failure/error", request)
                self.assertIn("3 = HAM still owns it", request)

    def test_unlabeled_target_keeps_route_without_fabricating_label_address(self):
        unnamed = agent_directory.replace(
            agent(name="", pane_id="w8:p4", session_id="remote-session"),
            host="macbook-pro", local=False, route_target="w8:p4",
        )
        request = agent_messenger.build_orchestration_request(
            [unnamed], "Ask this worker for a status report."
        )
        self.assertIn(f"--route {agent_skill_cli.encode_agent_route(unnamed)}", request)
        self.assertNotIn("--address macbook-pro/w8:p4", request)

    def test_direct_mode_dispatches_to_multiple_selected_recipients(self):
        sender = agent()
        first = agent(name="red-fox", pane_id="w1:p2", session_id="session-2")
        second = agent(name="white-owl", pane_id="w1:p3", session_id="session-3")
        with tempfile.TemporaryDirectory() as state_directory:
            environment = {
                "LANG": "en_US.UTF-8",
                "HERDR_PLUGIN_STATE_DIR": state_directory,
            }
            with (
                mock.patch.object(
                    agent_messenger,
                    "query_local_agents",
                    return_value=[sender, first, second],
                ),
                mock.patch.object(agent_messenger, "ssh_hosts", return_value=[]),
            ):
                app = agent_messenger.MessengerApp(mock.Mock(), sender, environment)

        app.selected = {first.identity, second.identity}
        app.delivery_mode = agent_messenger.DELIVERY_DIRECT
        app.message_lines = ["  Please review this change.  "]
        results = [
            agent_directory.SendResult(first, True),
            agent_directory.SendResult(second, True),
        ]
        with (
            mock.patch.object(agent_messenger, "fetch_local_agent", return_value=sender),
            mock.patch.object(
                agent_messenger,
                "dispatch_prompts",
                return_value=results,
            ) as dispatch,
            mock.patch.object(agent_messenger, "show_notification"),
            mock.patch.object(agent_messenger.threading, "Thread", ImmediateThread),
        ):
            app._send()
            app._poll_send()

        self.assertFalse(app.running)
        self.assertCountEqual(dispatch.call_args.args[1], [first, second])
        self.assertEqual(dispatch.call_args.args[2], "Please review this change.")

    def test_send_runs_in_background_and_escape_cancels_pending_work(self):
        sender = agent()
        recipient = agent(name="red-fox", pane_id="w1:p2", session_id="session-2")
        started = threading.Event()
        stopped = threading.Event()

        def cancellable_dispatch(*_args, cancel_event, **_kwargs):
            started.set()
            cancel_event.wait(1)
            stopped.set()
            return []

        with tempfile.TemporaryDirectory() as state_directory:
            environment = {
                "LANG": "en_US.UTF-8",
                "HERDR_PLUGIN_STATE_DIR": state_directory,
            }
            with (
                mock.patch.object(
                    agent_messenger,
                    "query_local_agents",
                    return_value=[sender, recipient],
                ),
                mock.patch.object(agent_messenger, "ssh_hosts", return_value=[]),
            ):
                app = agent_messenger.MessengerApp(mock.Mock(), sender, environment)

        app.mode_choice = True
        app.delivery_mode = agent_messenger.DELIVERY_DIRECT
        app.selected = {recipient.identity}
        app.message_lines = ["Review"]
        with (
            mock.patch.object(agent_messenger, "fetch_local_agent", return_value=sender),
            mock.patch.object(
                agent_messenger,
                "dispatch_prompts",
                side_effect=cancellable_dispatch,
            ),
        ):
            before = time.monotonic()
            app._send()
            self.assertLess(time.monotonic() - before, 0.1)
            self.assertTrue(started.wait(1))
            app.handle_key("\x1b")
            self.assertTrue(stopped.wait(1))
            deadline = time.monotonic() + 1
            while app.send_results.empty() and time.monotonic() < deadline:
                time.sleep(0.001)
            app._poll_send()

        self.assertFalse(app.sending)
        self.assertEqual(app.status, app.text["send_cancelled"])

    def test_cancelled_or_uncertain_delivery_keeps_only_unsent_recipients_selected(self):
        sender = agent()
        delivered = agent(name="red-fox", pane_id="w:p2", session_id="delivered")
        pending = agent(name="blue-owl", pane_id="w:p3", session_id="pending")
        for uncertain, cancelled in ((False, True), (True, False), (True, True)):
            with (
                self.subTest(uncertain=uncertain, cancelled=cancelled),
                tempfile.TemporaryDirectory() as directory,
                mock.patch.object(agent_messenger, "query_local_agents", return_value=[sender, delivered, pending]),
                mock.patch.object(agent_messenger, "ssh_hosts", return_value=[]),
            ):
                app = agent_messenger.MessengerApp(
                    mock.Mock(), sender, {"HERDR_PLUGIN_STATE_DIR": directory, "LANG": "en_US.UTF-8"},
                )
                app.delivery_mode = agent_messenger.DELIVERY_DIRECT
                app.selected = {delivered.identity, pending.identity}
                app.sending = True
                results = [agent_directory.SendResult(delivered, not uncertain, submission_unknown=uncertain)]
                if not cancelled:
                    results.append(agent_directory.SendResult(pending, False, "rejected"))
                app.send_results.put(agent_messenger.SendJobResult(True, tuple(results), cancelled=cancelled))
                app._poll_send()
                self.assertEqual(app.selected, {pending.identity})
                self.assertFalse(app.sending)
                if uncertain:
                    self.assertEqual(app.status, app.text["delivery_unknown"].format(count=1))
                else:
                    self.assertIn(app.text["sent"].format(count=1), app.status)

    def test_uncertain_coordinator_delivery_requires_reselection(self):
        sender = agent()
        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch.object(agent_messenger, "query_local_agents", return_value=[sender]),
            mock.patch.object(agent_messenger, "ssh_hosts", return_value=[]),
        ):
            app = agent_messenger.MessengerApp(
                mock.Mock(), sender, {"HERDR_PLUGIN_STATE_DIR": directory, "LANG": "en_US.UTF-8"},
            )
        app.selected = {"worker"}
        app.send_results.put(agent_messenger.SendJobResult(True, (
            agent_directory.SendResult(sender, False, submission_unknown=True),
        )))
        app._poll_send()
        self.assertEqual(app.selected, set())
        self.assertEqual(app.status, app.text["delivery_unknown"].format(count=1))

    def test_cancelled_sender_revalidation_reports_send_cancelled(self):
        sender = agent()
        recipient = agent(name="red-fox", pane_id="w1:p2", session_id="session-2")
        with tempfile.TemporaryDirectory() as state_directory:
            environment = {
                "LANG": "en_US.UTF-8",
                "HERDR_PLUGIN_STATE_DIR": state_directory,
            }
            with (
                mock.patch.object(
                    agent_messenger,
                    "query_local_agents",
                    return_value=[sender, recipient],
                ),
                mock.patch.object(agent_messenger, "ssh_hosts", return_value=[]),
            ):
                app = agent_messenger.MessengerApp(mock.Mock(), sender, environment)

        def cancelled_fetch(*_args, cancel_event, **_kwargs):
            cancel_event.set()
            return None

        with mock.patch.object(
            agent_messenger,
            "fetch_local_agent",
            side_effect=cancelled_fetch,
        ):
            app.sending = True
            app._dispatch_send_job((recipient,), "Review")
            app._poll_send()

        self.assertFalse(app.sending)
        self.assertEqual(app.status, app.text["send_cancelled"])

    def test_partial_send_failure_keeps_only_failed_recipients_selected(self):
        sender = agent()
        succeeded = agent(name="red-fox", pane_id="w1:p2", session_id="session-2")
        failed = agent(name="white-owl", pane_id="w1:p3", session_id="session-3")
        with tempfile.TemporaryDirectory() as state_directory:
            environment = {
                "LANG": "en_US.UTF-8",
                "HERDR_PLUGIN_STATE_DIR": state_directory,
            }
            with (
                mock.patch.object(
                    agent_messenger,
                    "query_local_agents",
                    return_value=[sender, succeeded, failed],
                ),
                mock.patch.object(agent_messenger, "ssh_hosts", return_value=[]),
            ):
                app = agent_messenger.MessengerApp(mock.Mock(), sender, environment)

        app.sending = True
        app.delivery_mode = agent_messenger.DELIVERY_DIRECT
        app.selected = {succeeded.identity, failed.identity}
        app.send_results.put(
            agent_messenger.SendJobResult(
                True,
                (
                    agent_directory.SendResult(succeeded, True),
                    agent_directory.SendResult(failed, False, "unavailable"),
                ),
            )
        )
        app._poll_send()

        self.assertEqual(app.selected, {failed.identity})
        self.assertIn("failed for 1", app.status)


class HeldMessagesTest(unittest.TestCase):
    @staticmethod
    def held(request_id="a" * 32, recipient="local/red-fox", state="awaiting_decision"):
        return SimpleNamespace(
            request_id=request_id, sender="local/blue-raven", recipient=recipient,
            state=state, since="2026-10-16T10:00:00Z", deadline="2026-10-16T10:03:00Z",
            preview=f"Review for {recipient}",
        )

    def test_selection_push_cancel_and_refresh_use_selected_request(self):
        first, second = self.held(), self.held("b" * 32, "remote/white-owl", "deferred")
        delivery = mock.Mock()
        delivery.held_messages.side_effect = [[first, second], [first], []]
        delivery.decide.side_effect = [
            {"state": "submitted_unknown", "phase": "waiting_for_requested_turn"},
            {"state": "submission_failed", "phase": "delivery_cancelled"},
        ]
        app = agent_messenger.HeldMessagesApp(mock.Mock(), {}, delivery=delivery)
        app.handle_key(agent_messenger.curses.KEY_UP)
        self.assertEqual(app.cursor, 0)
        app.handle_key(agent_messenger.curses.KEY_DOWN)
        app.handle_key(agent_messenger.curses.KEY_DOWN)
        self.assertEqual(app.cursor, 1)
        app.handle_key("p")
        self.assertEqual(app.items, [first])
        self.assertEqual(app.cursor, 0)
        app.handle_key("c")
        self.assertEqual(app.items, [])
        app.handle_key("p")
        self.assertEqual(
            delivery.decide.call_args_list,
            [mock.call(second.request_id, "push", {}), mock.call(first.request_id, "cancel", {})],
        )
        self.assertEqual(app.error, "")

    def test_refresh_preserves_selected_request_when_other_rows_disappear(self):
        first, second = self.held(), self.held("b" * 32)
        delivery = mock.Mock()
        delivery.held_messages.side_effect = [[first, second], [second]]
        app = agent_messenger.HeldMessagesApp(mock.Mock(), {}, delivery=delivery)
        app.handle_key(agent_messenger.curses.KEY_DOWN)
        app.refresh()
        self.assertEqual(app.items[app.cursor].request_id, second.request_id)

    def test_decision_errors_remain_inline_and_list_can_be_retried(self):
        from tests.curses_snapshot_harness import FakeCursesWindow

        delivery = mock.Mock()
        delivery.held_messages.return_value = [self.held()]
        delivery.decide.side_effect = RuntimeError("request storage unavailable")
        window = FakeCursesWindow(18, 100)
        app = agent_messenger.HeldMessagesApp(window, {"LANG": "en_US.UTF-8"}, delivery=delivery)
        app.handle_key("p")
        with mock.patch.object(agent_messenger.curses, "curs_set"):
            app.render()
        self.assertTrue(window.snapshot().rows_containing("request storage unavailable"))
        self.assertEqual(len(app.items), 1)
        delivery.decide.side_effect = None
        delivery.decide.return_value = {
            "state": "submission_failed", "phase": "delivery_cancelled",
        }
        delivery.held_messages.return_value = []
        app.handle_key("c")
        self.assertEqual(app.error, "")
        self.assertEqual(app.items, [])

    def test_unapplied_decision_notice_is_visible_in_every_language(self):
        from tests.curses_snapshot_harness import FakeCursesWindow

        cases = (
            ("p", "push", "deferred", "deferred_recipient_focused"),
            ("p", "push", "awaiting_decision", "awaiting_decision"),
            ("p", "push", "submission_failed", "recipient_changed"),
            ("c", "cancel", "submitted_working", "waiting_for_requested_turn"),
            ("c", "cancel", "submitted_settled", "settled"),
            ("c", "cancel", "submission_failed", "recipient_changed"),
        )
        for language in messenger_i18n.SUPPORTED_LANGUAGES:
            for key, decision, state, phase in cases:
                with self.subTest(language=language, decision=decision, state=state):
                    delivery = mock.Mock()
                    delivery.held_messages.side_effect = [[self.held()], []]
                    delivery.decide.return_value = {"state": state, "phase": phase}
                    window = FakeCursesWindow(18, 110)
                    app = agent_messenger.HeldMessagesApp(
                        window, {"LANG": f"{language}_TEST.UTF-8"}, delivery=delivery
                    )
                    app.handle_key(key)
                    with mock.patch.object(agent_messenger.curses, "curs_set"):
                        app.render()
                    notice = app.text[f"held_{decision}_not_applied"].format(state=state)
                    self.assertTrue(window.snapshot().rows_containing(notice))
                    self.assertTrue(app.running)
                    self.assertEqual(app.items, [])

    def test_decision_exception_is_localized_in_every_language(self):
        from tests.curses_snapshot_harness import FakeCursesWindow

        for language in messenger_i18n.SUPPORTED_LANGUAGES:
            for key in ("p", "c"):
                with self.subTest(language=language, key=key):
                    delivery = mock.Mock()
                    delivery.held_messages.return_value = [self.held()]
                    delivery.decide.side_effect = RuntimeError("storage unavailable")
                    window = FakeCursesWindow(18, 110)
                    app = agent_messenger.HeldMessagesApp(
                        window, {"LANG": f"{language}_TEST.UTF-8"}, delivery=delivery
                    )
                    app.handle_key(key)
                    with mock.patch.object(agent_messenger.curses, "curs_set"):
                        app.render()
                    notice = app.text["held_error"].format(error="storage unavailable")
                    self.assertTrue(window.snapshot().rows_containing(notice))
                    self.assertTrue(app.running)
                    self.assertEqual(len(app.items), 1)

    def test_load_error_is_visible_without_preventing_close(self):
        from tests.curses_snapshot_harness import FakeCursesWindow

        delivery = mock.Mock()
        delivery.held_messages.side_effect = OSError("queue unreadable")
        window = FakeCursesWindow(14, 90)
        app = agent_messenger.HeldMessagesApp(window, {"LANG": "en_US.UTF-8"}, delivery=delivery)
        with mock.patch.object(agent_messenger.curses, "curs_set"):
            app.render()
        self.assertTrue(window.snapshot().rows_containing("queue unreadable"))
        app.handle_key("\x1b")
        self.assertFalse(app.running)

    def test_warning_remains_visible_in_all_languages_with_empty_and_populated_lists(self):
        from tests.curses_snapshot_harness import FakeCursesWindow

        for language in messenger_i18n.SUPPORTED_LANGUAGES:
            for items in ([], [self.held()]):
                with self.subTest(language=language, populated=bool(items)):
                    delivery = mock.Mock()
                    delivery.held_messages.return_value = items
                    window = FakeCursesWindow(18, 52)
                    app = agent_messenger.HeldMessagesApp(
                        window, {"LANG": f"{language}_TEST.UTF-8"}, delivery=delivery
                    )
                    with mock.patch.object(agent_messenger.curses, "curs_set"):
                        app.render()
                    snapshot = window.snapshot()
                    warning_lines = agent_messenger.wrap_display_text(
                        app.text["held_warning"], 51
                    )
                    for line in warning_lines:
                        self.assertTrue(
                            snapshot.rows_containing(line.rstrip()), snapshot.formatted()
                        )
                    if not items:
                        self.assertTrue(snapshot.rows_containing(app.text["held_empty"]))
                    else:
                        self.assertTrue(snapshot.rows_containing(items[0].preview))
                    self.assertFalse(any(call.clipped for call in snapshot.draw_calls))

    def test_scrolling_keeps_selected_recipient_visible(self):
        from tests.curses_snapshot_harness import FakeCursesWindow

        items = [self.held(f"{index:032x}", f"local/worker-{index}") for index in range(20)]
        delivery = mock.Mock()
        delivery.held_messages.return_value = items
        window = FakeCursesWindow(12, 110)
        app = agent_messenger.HeldMessagesApp(window, {"LANG": "en_US.UTF-8"}, delivery=delivery)
        for _ in range(25):
            app.handle_key(agent_messenger.curses.KEY_DOWN)
        with mock.patch.object(agent_messenger.curses, "curs_set"):
            app.render()
        self.assertEqual(app.cursor, 19)
        selected_calls = [
            call for call in window.snapshot().draw_calls
            if call.attribute & agent_messenger.curses.A_REVERSE
        ]
        self.assertEqual(len(selected_calls), 1)
        self.assertIn("local/worker-19", selected_calls[0].visible_text)

    def test_idle_refresh_observes_worker_changes_and_q_closes(self):
        first = self.held()
        delivery = mock.Mock()
        delivery.held_messages.side_effect = [[first], []]
        screen = mock.Mock()
        screen.getmaxyx.return_value = (18, 100)
        screen.get_wch.side_effect = [agent_messenger.curses.error(), "q"]
        app = agent_messenger.HeldMessagesApp(screen, {"LANG": "en_US.UTF-8"}, delivery=delivery)
        with mock.patch.object(agent_messenger.curses, "curs_set"):
            self.assertEqual(app.run(), 0)
        self.assertEqual(app.items, [])
        self.assertFalse(app.running)
        delivery.decide.assert_not_called()

    def test_popup_does_not_require_a_focused_agent(self):
        delivery = mock.Mock()
        delivery.held_messages.return_value = []
        screen = mock.Mock()
        screen.getmaxyx.return_value = (18, 100)
        screen.get_wch.return_value = "q"
        with (
            mock.patch("ham.agents.delivery", delivery),
            mock.patch.object(agent_messenger.sys.stdin, "isatty", return_value=True),
            mock.patch.object(agent_messenger.sys.stdout, "isatty", return_value=True),
            mock.patch.object(agent_messenger.curses, "wrapper", side_effect=lambda run: run(screen)),
            mock.patch.object(agent_messenger.curses, "curs_set"),
            mock.patch.object(agent_messenger, "fetch_local_agent") as fetch,
        ):
            self.assertEqual(agent_messenger.popup_held({"LANG": "en_US.UTF-8"}), 0)
        fetch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
