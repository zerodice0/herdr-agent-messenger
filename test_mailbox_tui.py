"""Consumer-visible quick-send behavior and preserved isolated mailbox services."""
import ham_test_support  # Protect the live ledger before product imports.
import curses
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from agent_directory import AgentRecord, SendResult
from curses_snapshot_harness import DrawCall, FakeCursesWindow
from key_input import ESCAPE, F1, NEWLINE, SHIFT_TAB, MouseEvent
from ledger_mail import Mailbox
from ledger_settings import RepoSettings
from ledger_status import AgentRegistry
from ledger_store import LedgerError, LedgerStore
from terminal_text import display_width
from mailbox_tui import (Job, MailboxApp, OPENER_PANE, OPENER_TERMINAL, REPO_ENV, clip,
                         close_owned, describe_message, dispatch_direct, edit_buffer,
                         execute_job, launch, local_snapshot, maintain, wrap_summary)


SCRATCH = Path('/home/zerodice0/.cache/agent-work/herdr-agent-labels-mailbox-quicksend-2708b9bca7/mailbox-quicksend')


class Screen(FakeCursesWindow):
    """Cell-grid harness with addstr's full-width semantics and strict bounds.

    The shared harness's addnstr reserves the last column on every row for the
    older Messenger renderer; mailbox boxes legitimately occupy that column.
    Record attempted bounds before curses errors could be swallowed by the app.
    """
    def __init__(self, height=24, width=100):
        super().__init__(height, width)
        self.out_of_bounds = []

    def addstr(self, row, column, text, attribute=0):
        if not (0 <= row < self.height and 0 <= column < self.width
                and column + display_width(text) <= self.width):
            self.out_of_bounds.append((row, column, text))
            raise curses.error('write outside fake window')
        self.draw_calls.append(DrawCall(row, column, text, text, attribute, len(text)))
        current = column
        for character in text:
            width = display_width(character)
            if not width:
                self._cells[row][max(column, current - 1)].text += character
                continue
            cell_type = type(self._cells[row][current])
            self._cells[row][current] = cell_type(character, attribute, False)
            for offset in range(1, width):
                self._cells[row][current + offset] = cell_type('', attribute, True)
            current += width


class PendingJob:
    def __init__(self, request, environment):
        self.request = request
        self.operation = request['operation']
        self.result = None
        self.closed = False

    def poll(self):
        return self.result is not None, self.result

    def close(self):
        self.closed = True


class MailboxUiTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=SCRATCH)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.environment = {**os.environ, 'HERDR_AGENT_LEDGER_DIR': str(self.root / 'ledger'),
                            'HERDR_AGENT_LEDGER_AUTOSTART': '0',
                            'HERDR_PLUGIN_STATE_DIR': str(self.root / 'plugin-state')}
        # A test must never query or mutate the live Herdr instance.
        isolated = patch('mailbox_tui.herdr', side_effect=LedgerError('isolated', 'No live Herdr in tests'))
        isolated.start()
        self.addCleanup(isolated.stop)
        resources = patch('resource_scan.scan_host_resources',
                          return_value={'listeners': [], 'serve': [], 'error': None})
        resources.start()
        self.addCleanup(resources.stop)
        self.cursors = patch('mailbox_tui.curses.curs_set')
        self.cursors.start()
        self.addCleanup(self.cursors.stop)
        self.store = LedgerStore(environment=self.environment, canonical_name='ui-test')
        self.registry = AgentRegistry(self.store)
        self.snapshot = {'panes': [{'terminal_id': 'term-a', 'pane_id': 'w1:p1', 'workspace_id': 'w1'},
                                  {'terminal_id': 'term-b', 'pane_id': 'w1:p2', 'workspace_id': 'w1'}],
                         'agents': []}
        for index, pane in enumerate(self.snapshot['panes']):
            self.snapshot['agents'].append({**pane, 'agent': 'codex', 'agent_status': 'idle',
                                           'name': ('orange-rabbit', 'blue-owl')[index],
                                           'agent_session': {'value': f'session-{index}'}, 'cwd': str(self.root)})
        self.registry.reconcile(self.snapshot, refresh_display=False)
        self.recipient = next(agent for agent in self.registry.list() if agent['terminal_id'] == 'term-b')
        self.repo_id = self.recipient['repo_id']
        self.environment[REPO_ENV] = self.repo_id
        self.mail = Mailbox(self.store, self.environment)
        self.now = 0

    def app(self, **options):
        app = MailboxApp(Screen(), self.environment, job_factory=PendingJob,
                         clock=lambda: self.now, **options)
        app.local = local_snapshot(self.store, self.repo_id)
        return app

    def select_agent(self, app, terminal_id='term-b'):
        app.selections['agents'] = next(index for index, agent in enumerate(app.agents())
                                        if agent['terminal_id'] == terminal_id)
        return app.selected('agents')

    def type_text(self, app, text):
        for character in text:
            app.key(NEWLINE if character == '\n' else character)

    def accept(self, app):
        self.assertEqual(app.mode, 'confirm')
        self.assertEqual(app.confirm['choice'], 0)
        self.assertNotIn('mutation', app.jobs)
        app.key('\t')
        app.key('\r')

    def capture(self, app, height=24, width=100):
        app.screen = Screen(height, width)
        app.render()
        self.assertEqual(app.screen.out_of_bounds, [])
        self.assertTrue(all(call.column + display_width(call.requested_text) <= width
                            for call in app.screen.draw_calls))
        return app.screen.snapshot()

    def resource_app(self, **options):
        app = self.app(wall_clock=lambda: 1_800_000_000, **options)
        app.environment['NO_COLOR'] = '1'
        agent = app.local['agents'][0]
        app.local['agents'] = [{
            **agent, 'alias': 'reviewer', 'turn_state': 'working', 'unread': 0,
            'extension_present': True, 'role': 'builder', 'phase': 'review', 'summary': 'ready',
            'devices': [
                {'kind': 'phone', 'device_id': 'phone-serial', 'label': 'Pixel',
                 'claimed_at': 1_799_999_880, 'stale': False, 'attached': True},
                {'kind': 'emu', 'device_id': 'emulator-5554', 'label': '5554',
                 'claimed_at': 1_799_999_940, 'stale': False, 'attached': None}],
            'servers': [
                {'port': 5180, 'bind': '127.0.0.1', 'pid': 321, 'process': 'node',
                 'cwd': '/work/game', 'started_at': 1_799_996_400, 'inherited': True,
                 'urls': ['https://review.example/game/', 'https://review.example/other/']},
                {'port': 8787, 'bind': '::1', 'pid': 654, 'process': 'bun',
                 'cwd': '/work/api', 'started_at': 1_799_999_970, 'inherited': False, 'urls': []}],
        }]
        app.local['resources'] = {
            'dead_serve': [{'url': 'https://dead.example/'}],
            'servers_unowned': [{'url': 'https://unowned.example/'}],
            'stale_devices': [{'device_id': 'retired-phone'}], 'error': None,
        }
        return app

    def test_resource_presence_captures_at_40_and_160_columns(self):
        for width in (40, 160):
            with self.subTest(width=width):
                app = self.resource_app()
                before = json.dumps(app.local, sort_keys=True)
                bell = self.store.bell_directory.joinpath('_board').stat().st_mtime_ns
                snapshot = self.capture(app, height=35, width=width)
                text = '\n'.join(snapshot.lines())
                self.assertIn('reviewer', text)
                if width == 40:
                    self.assertNotIn('RESOURCES', text)
                    self.assertNotIn('P1 E1 S2', text)
                    self.assertIn('D:1 U:1 X:1', text)
                else:
                    self.assertIn('RESOURCES', text)
                    self.assertIn('P1 E1 S2', text)
                    self.assertIn('dead:1 unowned:1 stale-dev:1', text)
                app.focus = 'detail'
                pages = []
                while True:
                    pages.extend(self.capture(app, height=35, width=width).lines())
                    previous = app.scrolls['detail']
                    app.key(curses.KEY_NPAGE)
                    if app.scrolls['detail'] == previous:
                        break
                detail = '\n'.join(pages)
                for evidence in ('Devices', 'phone: Pixel', 'emu: 5554', 'Servers',
                                 'Port: 5180', 'node', 'Cwd: /work/game', 'Inherited: yes',
                                 '(1h ago)', 'URL: https://review.example/game/',
                                 'Port: 8787', 'URL: none', 'Read-only'):
                    self.assertIn(evidence, detail)
                self.assertEqual(json.dumps(app.local, sort_keys=True), before)
                self.assertEqual(app.jobs, {})
                self.assertEqual(self.store.bell_directory.joinpath('_board').stat().st_mtime_ns, bell)

    def test_agent_resource_details_include_device_and_server_evidence(self):
        app = self.resource_app()
        app.detail = app.agents()[0]
        lines = app.detail_lines()
        text = '\n'.join(lines)
        for evidence in ('Devices', 'phone: Pixel', 'ID: phone-serial', 'emu: 5554',
                         'ID: emulator-5554', 'Attached: yes', 'Attached: unknown',
                         'Servers', 'Port: 5180 · node · PID 321', 'Bind: 127.0.0.1',
                         'Cwd: /work/game', 'Inherited: yes', '(1h ago)',
                         'URL: https://review.example/game/', 'URL: https://review.example/other/',
                         'Port: 8787 · bun · PID 654', 'Inherited: no', '(30s ago)', 'URL: none'):
            self.assertIn(evidence, text)
        self.assertIn('(2m ago)', text)
        self.assertLess(lines.index('Devices'), lines.index('Servers'))

    def test_resource_fields_are_independently_unknown_or_known_empty(self):
        for devices in (None, [], [{'kind': 'phone', 'device_id': 'serial'}]):
            for servers in (None, [], [{'port': 5180, 'process': 'node'}]):
                for resources in (None, {}, {'dead_serve': [], 'error': None}):
                    with self.subTest(devices=devices, servers=servers, resources=resources):
                        app = self.resource_app()
                        agent = app.local['agents'][0]
                        for key, value in (('devices', devices), ('servers', servers)):
                            agent.pop(key)
                            if value is not None:
                                agent[key] = value
                        app.local.pop('resources')
                        if resources is not None:
                            app.local['resources'] = resources
                        app.detail = agent
                        cue = app.resource_cue(agent)
                        self.assertIn('P?' if devices is None else 'P0' if not devices else 'P1', cue)
                        self.assertIn('E?' if devices is None else 'E0', cue)
                        self.assertIn('S?' if servers is None else 'S0' if not servers else 'S1', cue)
                        self.assertIn('unowned:? stale-dev:?', app.host_resource_cue(self.store.host))
                        self.assertIn('dead:0' if resources and 'dead_serve' in resources else 'dead:?',
                                      app.host_resource_cue(self.store.host))
                        lines = app.detail_lines()
                        self.assertIn('Devices: ?' if devices is None else 'Devices: none' if not devices
                                      else 'Devices', lines)
                        self.assertIn('Servers: ?' if servers is None else 'Servers: none' if not servers
                                      else 'Servers', lines)

    def test_resource_cues_use_unicode_with_color_and_ascii_without_color(self):
        app = self.resource_app()
        agent = app.agents()[0]
        for colors, no_color, expected in ((True, False, '📱1 🤖1 🌐2'),
                                           (False, False, 'P1 E1 S2'),
                                           (True, True, 'P1 E1 S2')):
            with self.subTest(colors=colors, no_color=no_color):
                app.colors_enabled = colors
                app.environment.pop('NO_COLOR', None)
                if no_color:
                    app.environment['NO_COLOR'] = '1'
                self.assertEqual(app.resource_cue(agent), expected)
                app.screen = Screen(6, 160)
                with patch('mailbox_tui.curses.color_pair', side_effect=lambda pair: pair << 8):
                    app.render_agents((0, 0, 6, 160))
                self.assertIn(expected, '\n'.join(app.screen.snapshot().lines()))
                self.assertEqual(app.screen.out_of_bounds, [])

    def test_old_peer_resource_fields_render_unknown_and_wrong_uuid_is_ignored(self):
        app = self.resource_app()
        app.local['peers'] = [{'canonical_name': 'old-peer', 'uuid': 'peer-uuid'}]
        old_agent = {key: value for key, value in app.local['agents'][0].items()
                     if key not in {'devices', 'servers'}}
        old_agent.update(host='old-peer', alias='legacy')
        app.remote['old-peer'] = {'agents': [old_agent],
                                  'hosts': [{'host': 'old-peer', 'uuid': 'peer-uuid'}]}
        app.screen = Screen(10, 160)
        app.render_agents((0, 0, 10, 160))
        text = '\n'.join(app.screen.snapshot().lines())
        self.assertIn('P? E? S?', text)
        self.assertIn('old-peer · 1 agents · dead:? unowned:? stale-dev:?', text)
        app.detail = next(agent for agent in app.agents() if agent['host'] == 'old-peer')
        self.assertIn('Devices: ?', app.detail_lines())
        self.assertIn('Servers: ?', app.detail_lines())
        app.remote['old-peer']['hosts'][0].update(
            uuid='wrong-uuid', resources={'dead_serve': [], 'servers_unowned': [], 'stale_devices': []})
        app.screen.erase()
        app.render_agents((0, 0, 10, 160))
        self.assertNotIn('old-peer', '\n'.join(app.screen.snapshot().lines()))
        self.assertIsNone(app.host_resources('old-peer'))

    def test_host_resource_counts_show_without_agents_and_report_scan_error(self):
        app = self.resource_app()
        app.local['agents'] = []
        app.local['resources']['error'] = 'listener discovery unavailable'
        app.screen = Screen(6, 160)
        app.render_agents((0, 0, 6, 160))
        text = '\n'.join(app.screen.snapshot().lines())
        self.assertIn('0 agents · dead:1 unowned:1 stale-dev:1 !', text)
        self.assertIn(' No agents. r refresh', text)

    def test_local_snapshot_preserves_host_resources_and_missing_fields(self):
        for resources in (None, {'dead_serve': [], 'servers_unowned': [],
                                  'stale_devices': [], 'error': None}):
            with self.subTest(resources=resources):
                exported = {
                    'host': {'canonical_name': self.store.host, 'uuid': self.store.host_uuid},
                    'agents': [{**self.recipient, 'unread': 0}],
                }
                if resources is not None:
                    exported['resources'] = resources
                with patch('mailbox_tui.RemoteLedger.status_export', return_value=exported):
                    snapshot = local_snapshot(self.store, self.repo_id)
                if resources is None:
                    self.assertNotIn('resources', snapshot)
                else:
                    self.assertEqual(snapshot['resources'], resources)

    def mouse_at_text(self, app, needle, action='press', offset=0):
        call = next(call for call in reversed(app.screen.draw_calls) if needle in call.requested_text)
        column = call.column + display_width(call.requested_text[:call.requested_text.index(needle)]) + offset
        app.key(MouseEvent(call.row, column, action))
        return call.row, column

    def test_mouse_region_focus_and_agent_alias_and_summary_cells(self):
        for width in (120, 160):
            for summary in (False, True):
                with self.subTest(width=width, summary=summary):
                    app = self.app()
                    app.local['agents'] = [
                        {**agent, 'alias': f'agent-{index}', 'summary': f'summary-{index}',
                         'role': '', 'phase': ''}
                        for index, agent in enumerate(app.agents())]
                    self.capture(app, 32, width)
                    self.mouse_at_text(app, ' Threads ')
                    self.assertEqual(app.focus, 'threads')
                    self.assertEqual(app.mode, 'nav')
                    self.capture(app, 32, width)
                    point = self.mouse_at_text(app, 'summary-1' if summary else 'agent-1')
                    self.assertEqual(app.focus, 'agents')
                    self.assertEqual(app.selections['agents'], 1)
                    self.assertEqual(app.mode, 'nav')
                    expected = app.agents()[1]
                    self.capture(app, 32, width)
                    app.key(MouseEvent(*point, 'press'))
                    self.assertEqual(app.mode, 'input')
                    self.assertEqual(app.identity(app.recipient), app.identity(expected))

    def test_mouse_headers_and_empty_region_space_only_focus(self):
        app = self.app()
        self.capture(app, 32, 160)
        app.focus = 'threads'
        self.mouse_at_text(app, 'ALIAS')
        self.assertEqual((app.focus, app.mode, app.selections['agents']), ('agents', 'nav', 0))
        app.focus = 'threads'
        self.mouse_at_text(app, ' · 2 agents')
        self.assertEqual((app.focus, app.mode), ('agents', 'nav'))
        app.focus = 'agents'
        rect = next(rect for rect, kind, value in app.hit_map
                    if kind == 'region' and value == 'threads')
        y, x, height, width = rect
        app.key(MouseEvent(y + height - 2, x + width - 2, 'press'))
        self.assertEqual((app.focus, app.mode), ('threads', 'nav'))
        app.key(MouseEvent(0, 2, 'press'))
        app.key(MouseEvent(31, 2, 'press'))
        self.assertEqual((app.focus, app.mode), ('threads', 'nav'))

    def test_mouse_thread_rows_select_then_open_reply_without_ack(self):
        for width in (100, 160):
            for secondary in (False, True):
                with self.subTest(width=width, secondary=secondary):
                    app = self.app()
                    sent = [self.mail.send_user(self.repo_id, agent, body='thread-' + str(index))
                            for index, agent in enumerate(app.agents())]
                    app.local = local_snapshot(self.store, self.repo_id)
                    app.selections['threads'] = 0
                    snapshot = self.capture(app, 36, width)
                    rows = [call for call in snapshot.draw_calls
                            if 'queued · thread-' in call.requested_text]
                    self.assertGreaterEqual(len(rows), 2)
                    row = rows[1].row + int(secondary)
                    column = rows[1].column
                    app.key(MouseEvent(row, column, 'press'))
                    self.assertEqual((app.focus, app.selections['threads'], app.mode), ('threads', 1, 'nav'))
                    target = app.selected('threads')
                    self.capture(app, 36, width)
                    app.key(MouseEvent(row, column, 'press'))
                    self.assertEqual(app.mode, 'input')
                    self.assertEqual(app.reply_to, target['messages'][-1]['message_id'])
                    for result in sent:
                        message = self.mail.show_thread(result['thread_id'])['messages'][0]
                        self.assertEqual(message['state'], 'queued')
                        self.assertIsNone(message['delivered_at'])

    def test_mouse_wheels_scroll_target_region_without_changing_focus(self):
        for width in (100, 160):
            with self.subTest(width=width):
                app = self.app()
                base = app.agents()[0]
                app.local['agents'] = [
                    {**base, 'alias': f'agent-{index:02}', 'terminal_id': f'term-{index}',
                     'summary': 'long detail ' * 100} for index in range(10)]
                app.local['submissions'] = [
                    {'submission_id': str(index), 'acceptance': 'accepted', 'body': 'direct',
                     'target': base} for index in range(10)]
                self.capture(app, 24, width)
                self.mouse_at_text(app, ' Detail ', 'wheel_down')
                self.assertEqual(app.scrolls['detail'], 3)
                self.assertEqual(app.focus, 'agents')
                self.capture(app, 24, width)
                self.mouse_at_text(app, ' Detail ', 'wheel_up')
                self.assertEqual(app.scrolls['detail'], 0)
                self.mouse_at_text(app, ' Threads ')
                self.capture(app, 24, width)
                self.mouse_at_text(app, ' Agents ', 'wheel_down')
                self.assertEqual((app.selections['agents'], app.focus), (3, 'threads'))
                self.capture(app, 24, width)
                self.mouse_at_text(app, ' Agents ', 'wheel_up')
                self.assertEqual((app.selections['agents'], app.focus), (0, 'threads'))
                self.mouse_at_text(app, ' Detail ')
                self.capture(app, 24, width)
                self.mouse_at_text(app, ' Threads ', 'wheel_down')
                self.assertEqual((app.selections['threads'], app.focus), (3, 'detail'))
                for _ in range(6):
                    self.mouse_at_text(app, ' Agents ', 'wheel_down')
                self.assertEqual((app.selections['agents'], app.focus), (9, 'detail'))
                for _ in range(6):
                    self.mouse_at_text(app, ' Agents ', 'wheel_up')
                self.assertEqual(app.selections['agents'], 0)

    def test_mouse_input_cursor_uses_display_cells_and_ignores_outside_clicks(self):
        app = self.app()
        app.key('\r')
        self.type_text(app, '가나다 abc\n둘째 줄')
        self.capture(app, 32, 100)
        self.mouse_at_text(app, '가나다 abc', offset=3)
        self.assertEqual(app.body_cursor, 1)
        self.assertEqual(app.drafts[app.draft_key]['cursor'], 1)
        self.mouse_at_text(app, '둘째 줄', offset=5)
        self.assertEqual(app.body_cursor, len('가나다 abc\n둘째 '))
        before = app.body, app.body_cursor, app.focus, app.recipient
        self.mouse_at_text(app, ' Agents ')
        self.assertEqual((app.body, app.body_cursor, app.focus, app.recipient), before)
        self.assertEqual(app.mode, 'input')

    def test_mouse_input_wheel_scrolls_text_and_click_tracks_wrapped_offset(self):
        app = self.app()
        app.key('\r')
        text = '\n'.join(f'행{index:02} 가나다' for index in range(20))
        self.type_text(app, text)
        self.capture(app, 32, 100)
        self.mouse_at_text(app, 'To ', 'wheel_up')
        snapshot = self.capture(app, 32, 100)
        visible = [call for call in snapshot.draw_calls if call.requested_text.startswith('행')]
        line = visible[0]
        logical_row = int(line.requested_text[1:3])
        self.assertLess(logical_row, 17)
        app.key(MouseEvent(line.row, line.column + 3, 'press'))
        self.assertEqual(app.body_cursor, len('\n'.join(text.split('\n')[:logical_row])) +
                         int(logical_row > 0) + 2)
        self.assertEqual(app.mode, 'input')
        app.body = '가' * 60 + '\nlast'
        app.body_cursor = len(app.body)
        self.capture(app, 32, 40)
        call = next(call for call in app.screen.draw_calls if call.requested_text.startswith('가'))
        app.key(MouseEvent(call.row + 1, call.column + 4, 'press'))
        self.assertEqual(app.body_cursor, 20)

    def test_mouse_confirm_buttons_execute_only_chosen_action(self):
        for choice in ('Confirm', 'Cancel'):
            with self.subTest(choice=choice):
                app = self.app()
                app.open_confirm('Withdraw message?', ['Message: exact-message'], 'withdraw',
                                 {'message_id': 'exact-message'})
                self.capture(app, 24, 100)
                app.key(MouseEvent(2, 2, 'press'))
                self.assertEqual(app.mode, 'confirm')
                self.mouse_at_text(app, choice)
                self.assertEqual(app.mode, 'nav')
                if choice == 'Confirm':
                    self.assertEqual(app.jobs['mutation'].request['message_id'], 'exact-message')
                    self.assertEqual(app.jobs['mutation'].operation, 'withdraw')
                else:
                    self.assertNotIn('mutation', app.jobs)

    def test_mouse_evidence_buttons_review_cancel_and_cursor(self):
        for choice in ('Confirm', 'Cancel'):
            with self.subTest(choice=choice):
                app = self.app()
                app.edit = {'title': 'Evidence', 'text': 'observed', 'cursor': 8, 'action': 'resolve',
                            'return_mode': 'nav', 'claim': {'disposition': 'applied'}, 'target': []}
                app.mode = 'evidence'
                self.capture(app, 24, 100)
                app.key(MouseEvent(2, 2, 'press'))
                self.assertEqual(app.mode, 'evidence')
                self.mouse_at_text(app, 'observed', offset=2)
                self.assertEqual(app.edit['cursor'], 2)
                self.mouse_at_text(app, choice)
                self.assertEqual(app.mode, 'confirm' if choice == 'Confirm' else 'nav')
                self.assertNotIn('mutation', app.jobs)
                if choice == 'Confirm':
                    self.assertEqual(app.confirm['values']['claim']['evidence'], 'observed')
        app = self.app()
        app.edit = {'title': 'Evidence', 'text': '', 'cursor': 0, 'action': 'resolve', 'return_mode': 'nav'}
        app.mode = 'evidence'
        self.capture(app)
        self.mouse_at_text(app, 'Confirm')
        self.assertEqual(app.mode, 'evidence')
        self.assertIn('Non-empty operator evidence', app.status)

    def test_mouse_settings_rows_select_then_edit_and_wheel_overlay(self):
        app = self.app()
        app.key('s')
        self.capture(app, 24, 100)
        point = self.mouse_at_text(app, 'members:')
        self.assertEqual(app.settings_selection, app.SETTINGS.index('members'))
        self.assertEqual(app.mode, 'settings')
        self.capture(app, 24, 100)
        app.key(MouseEvent(*point, 'press'))
        self.assertEqual(app.mode_word(), 'CHECK')
        self.assertEqual(app.settings_picker['field'], 'members')
        app.key(ESCAPE)
        app.local['settings']['conflicts'] = [{'detail': 'conflict'} for _ in range(40)]
        self.capture(app, 24, 100)
        before = app.focus, app.settings_selection
        self.mouse_at_text(app, ' SETTINGS ', 'wheel_down')
        self.capture(app, 24, 100)
        self.assertEqual(app.overlay_scroll, 3)
        self.assertEqual((app.focus, app.settings_selection), before)
        self.mouse_at_text(app, ' SETTINGS ', 'wheel_up')
        self.capture(app, 24, 100)
        self.assertEqual(app.overlay_scroll, 0)

    def test_mouse_help_click_closes_to_original_mode_and_tiny_ignores(self):
        for mode in ('nav', 'input'):
            app = self.app()
            if mode == 'input':
                app.key('\r')
            app.key(F1)
            self.capture(app, 24, 100)
            app.key(MouseEvent(2, 2, 'press'))
            self.assertEqual(app.mode, mode)
        app = self.app()
        app.key(F1)
        self.capture(app, 9, 31)
        before = app.focus, app.mode, dict(app.selections), dict(app.scrolls)
        for action in ('press', 'wheel_up', 'wheel_down'):
            app.key(MouseEvent(2, 2, action))
            self.assertEqual((app.focus, app.mode, app.selections, app.scrolls), before)
        self.assertEqual(app.hit_map, [])

    def test_mouse_hit_map_rebuilt_on_resize(self):
        app = self.app()
        self.capture(app, 24, 160)
        old_threads = next(rect for rect, kind, value in app.hit_map
                           if kind == 'region' and value == 'threads')
        snapshot = self.capture(app, 24, 60)
        rect = next(rect for rect, kind, value in app.hit_map if kind == 'region')
        self.assertEqual(rect, (1, 0, 22, 60))
        self.assertNotIn(old_threads, [rect for rect, _, _ in app.hit_map])
        header = next(call for call in snapshot.draw_calls if 'ALIAS' in call.requested_text)
        app.key(MouseEvent(header.row, header.column, 'press'))
        self.assertEqual(app.focus, 'agents')
        self.assertEqual(app.mode, 'nav')
        self.assertTrue(all(rect[1] + rect[3] <= 60 for rect, _, _ in app.hit_map))

    def test_run_enables_mouse_unless_disabled_and_always_releases_mask(self):
        for disabled in (False, True):
            for fail in (False, True):
                with self.subTest(disabled=disabled, fail=fail):
                    app = self.app()
                    app.environment.pop('HAM_MAILBOX_NO_MOUSE', None)
                    if disabled:
                        app.environment['HAM_MAILBOX_NO_MOUSE'] = '1'
                    app.start_job('pending', 'snapshot')
                    job = app.jobs['pending']
                    def tick():
                        if fail:
                            raise RuntimeError('loop failed')
                        app.running = False
                    with patch.object(app, 'tick', side_effect=tick), \
                         patch.object(app, 'initialize_colors'), \
                         patch.object(app.screen, 'keypad', create=True), \
                         patch.object(app.screen, 'timeout', create=True), \
                         patch('mailbox_tui.curses.nonl'), \
                         patch('mailbox_tui.curses.set_escdelay'), \
                         patch('mailbox_tui.curses.mousemask') as mask, \
                         patch('mailbox_tui.curses.mouseinterval') as interval:
                        if fail:
                            with self.assertRaisesRegex(RuntimeError, 'loop failed'):
                                app.run()
                        else:
                            self.assertEqual(app.run(), 0)
                    self.assertEqual([call.args for call in mask.call_args_list],
                                     [(0,)] if disabled else [(curses.ALL_MOUSE_EVENTS,), (0,)])
                    self.assertEqual(interval.call_count, 0 if disabled else 1)
                    if not disabled:
                        interval.assert_called_once_with(0)
                    self.assertTrue(job.closed)
                    self.assertEqual(app.jobs, {})

    def test_execute_send_is_user_origin_without_opener_agent(self):
        result = execute_job({'operation': 'send', 'repo_id': self.repo_id,
                              'recipient': self.recipient, 'body': '사용자가 남긴 메시지',
                              'kind': 'action', 'subject': 'Inspect'}, self.environment)
        message = self.mail.show_thread(result['thread_id'])['messages'][0]
        self.assertEqual((message['sender'], message['sender_label']), ('user', 'user'))
        self.assertEqual(message['body'], '사용자가 남긴 메시지')
        self.assertEqual(message['state'], 'queued')
        self.assertIsNone(message['delivered_at'])


    def test_board_unread_and_read_only_thread_inspection(self):
        result = self.mail.send_user(self.repo_id, self.recipient, body='Read only')
        before = self.store.bell_directory.joinpath('_board').stat().st_mtime_ns
        data = local_snapshot(self.store, self.repo_id)
        recipient = next(agent for agent in data['agents'] if agent['terminal_id'] == 'term-b')
        self.assertEqual(recipient['unread'], 1)
        self.assertEqual(data['threads'][0]['messages'][0]['state'], 'queued')
        self.assertEqual(self.store.bell_directory.joinpath('_board').stat().st_mtime_ns, before)
        self.assertIsNone(self.mail.show_thread(result['thread_id'])['messages'][0]['delivered_at'])


    def test_not_applied_requeues_only_exact_claim(self):
        sent = self.mail.send_user(self.repo_id, self.recipient, body='Not applied')
        claim = self.mail.claim(terminal_id='term-b', agent_id=self.recipient['agent_id'], generation=1)
        result = execute_job({'operation': 'resolve', 'claim': {
            'claim_id': claim['claim_id'], 'agent_id': claim['agent_id'], 'generation': 1,
            'disposition': 'not_applied', 'evidence': 'Cancelled before context acceptance'}}, self.environment)
        self.assertEqual(result['receipts'][0]['state'], 'queued')
        self.assertEqual(result['receipts'][0]['confirmation_source'], 'operator')
        self.assertIsNone(self.mail.show_thread(sent['thread_id'])['messages'][0]['delivered_at'])


    def test_receipt_confirmation_source_does_not_infer_native_delivery(self):
        base = {'kind': 'action', 'state': 'queued', 'delivered_at': None}
        manual = describe_message({**base, 'confirmation_source': 'operator', 'evidence': 'Verified absent'})
        self.assertIn('queued', manual)
        self.assertIn('NOT native ACK', manual)
        self.assertNotIn('delivered', manual)
        native = describe_message({**base, 'state': 'delivered', 'confirmation_source': 'native'})
        self.assertIn('NATIVE runtime-journal', native)
        unconfirmed = describe_message({**base, 'evidence': 'operator:untrusted body prefix'})
        self.assertNotIn('USER-CONFIRMED', unconfirmed)


    def target(self):
        return AgentRecord(host='local', name='blue-owl', pane_id='w1:p2', workspace_id='w1',
                           workspace_label='Test', status='idle', session_id=self.recipient['session_ref'],
                           cwd=str(self.root), local=True, terminal_id='term-b', agent_kind='codex')


    def test_direct_persists_exact_text_before_shared_transport(self):
        target = self.target()
        def dispatch(recipients, body, **options):
            records = self.mail.list_direct(self.repo_id)['submissions']
            self.assertEqual(records[0]['acceptance'], 'submitting')
            self.assertEqual(records[0]['body'], 'Message from user (HAM):\n\n직접 입력')
            self.assertEqual(body, '직접 입력')
            return [SendResult(target, True)]
        with patch('mailbox_tui.query_local_agents', return_value=[target]), \
             patch('mailbox_tui.dispatch_user_prompts', side_effect=dispatch):
            result = dispatch_direct(self.store, self.recipient, '직접 입력', self.environment)
        self.assertEqual((result['acceptance'], result['via']), ('accepted', 'direct'))
        self.assertFalse(result.get('delivered_at'))
        self.assertEqual(self.mail.list_threads(self.repo_id)['threads'], [])


    def test_direct_unknown_never_becomes_native_delivery(self):
        target = self.target()
        with patch('mailbox_tui.query_local_agents', return_value=[target]), \
             patch('mailbox_tui.dispatch_user_prompts', return_value=[SendResult(target, False, 'timeout', True)]):
            result = dispatch_direct(self.store, self.recipient, 'Uncertain', self.environment)
        self.assertEqual(result['acceptance'], 'unknown')
        self.assertFalse(result.get('delivered_at'))
        self.assertEqual(self.mail.list_threads(self.repo_id)['threads'], [])


    def test_explicit_direct_input_does_not_require_native_session_extension(self):
        from dataclasses import replace
        self.snapshot['agents'][1].pop('agent_session')
        self.registry.reconcile(self.snapshot)
        recipient = next(agent for agent in self.registry.list() if agent['terminal_id'] == 'term-b')
        self.assertEqual(recipient['session_ref'], 'unreported')
        target = replace(self.target(), session_id='')
        with patch('mailbox_tui.query_local_agents', return_value=[target]), \
             patch('mailbox_tui.dispatch_user_prompts', return_value=[SendResult(target, True)]) as transport:
            result = dispatch_direct(self.store, recipient, 'User selected this terminal', self.environment)
        self.assertEqual(result['acceptance'], 'accepted')
        self.assertEqual(transport.call_count, 1)


    def test_remote_direct_preserves_peer_endpoint_and_actual_recipient(self):
        from dataclasses import replace
        from agent_directory import ProbeResult
        from ledger_remote import RemoteLedger
        config = self.root / 'ssh-config'
        config.write_text('Host peer-alias\n  HostName example.invalid\n')
        allowlist = self.root / 'ssh-hosts'
        allowlist.write_text('peer-alias\n')
        environment = {**self.environment, 'HERDR_AGENT_MESSENGER_SSH_CONFIG': str(config),
                       'HERDR_AGENT_MESSENGER_SSH_HOSTS_FILE': str(allowlist)}
        RemoteLedger(self.store, environment).add_peer(
            canonical_name='remote', uuid='00000000-0000-4000-8000-000000000001',
            ssh_alias='peer-alias', remote_ham_root='/test/ham', remote_socket='/test/peer.sock')
        recipient = {**self.recipient, 'host': 'remote'}
        target = replace(self.target(), host='peer-alias', local=False)
        with patch('mailbox_tui.query_remote_agents', return_value=ProbeResult('peer-alias', (target,), True)) as query, \
             patch('mailbox_tui.dispatch_user_prompts', return_value=[SendResult(target, True)]) as transport:
            result = dispatch_direct(self.store, recipient, 'Remote user text', environment)
        self.assertEqual(result['acceptance'], 'accepted')
        self.assertEqual(query.call_args.kwargs['remote_sockets'], {'peer-alias': '/test/peer.sock'})
        self.assertEqual(transport.call_args.kwargs['remote_sockets'], {'peer-alias': '/test/peer.sock'})
        self.assertIs(transport.call_args.args[0][0], target)
        self.assertEqual(result['target']['host'], 'remote')


    def test_unicode_editor_moves_and_deletes_by_character(self):
        text, cursor, _ = edit_buffer("한글\nabc", 2, curses.KEY_LEFT, multiline=True)
        self.assertEqual(cursor, 1)
        text, cursor, _ = edit_buffer(text, cursor, "국", multiline=True)
        self.assertEqual((text, cursor), ("한국글\nabc", 2))
        text, cursor, _ = edit_buffer(text, cursor, curses.KEY_DC, multiline=True)
        self.assertEqual(text, "한국\nabc")
        text, cursor, _ = edit_buffer(text, cursor, curses.KEY_DOWN, multiline=True)
        self.assertEqual(cursor, len(text))
        text, cursor, _ = edit_buffer(text, cursor, curses.KEY_HOME, multiline=True)
        self.assertEqual(cursor, 3)


    def test_worker_start_failure_does_not_turn_committed_send_into_retry(self):
        from ledger_store import LedgerError
        environment = {**self.environment, 'HERDR_AGENT_LEDGER_AUTOSTART': '1'}
        with patch('ledger_worker.ensure_worker', side_effect=LedgerError('busy', 'worker unavailable')):
            result = execute_job({'operation': 'send', 'repo_id': self.repo_id,
                                  'recipient': self.recipient, 'body': 'Committed once',
                                  'kind': 'fyi'}, environment)
        self.assertEqual(result['state'], 'queued')
        self.assertTrue(result['warnings'][0]['committed'])
        self.assertEqual(len(self.mail.list_threads(self.repo_id)['threads']), 1)


    def test_launch_dedupe_uses_live_terminal_not_ham_label(self):
        opener = {'pane_id': 'w1:p1', 'terminal_id': 'open-terminal', 'workspace_id': 'w1', 'cwd': str(self.root), 'label': 'HAM'}
        owned = {'pane_id': 'w1:p3', 'terminal_id': 'ui-terminal', 'workspace_id': 'w1'}
        values = {**self.environment, 'HERDR_PANE_ID': 'w1:p1'}
        calls = []
        def transport(arguments, environment):
            calls.append(arguments)
            if arguments[:2] == ['pane', 'get']:
                return {'pane': opener if arguments[2] == opener['pane_id'] else owned}
            if arguments[:3] == ['plugin', 'pane', 'open']:
                return {'plugin_pane': {'pane': owned}}
            if arguments == ['api', 'snapshot']:
                return {'snapshot': {'panes': [opener, owned]}}
            return {}
        with patch('mailbox_tui.herdr', side_effect=transport):
            opened = launch('split', values)
            focused = launch('tab', values)
        self.assertFalse(opened['focused'])
        self.assertTrue(focused['focused'])
        opening = next(call for call in calls if call[:3] == ['plugin', 'pane', 'open'])
        self.assertIn('right', opening)
        self.assertNotIn('--workspace', opening)
        self.assertIn(f'{OPENER_PANE}=w1:p1', opening)
        self.assertIn(f'{OPENER_TERMINAL}=open-terminal', opening)
        self.assertEqual(sum(call[:3] == ['plugin', 'pane', 'open'] for call in calls), 1)


    def test_q_close_resolves_moved_owned_terminal_only(self):
        calls = []
        def transport(arguments, environment):
            calls.append(arguments)
            return {'snapshot': {'panes': [{'pane_id': 'w2:p9', 'terminal_id': 'ui-terminal'}]}}
        with patch('mailbox_tui.herdr', side_effect=transport):
            result = close_owned({**self.environment, 'HERDR_HAM_PLUGIN_OWNED': '1',
                                  'HERDR_PANE_ID': 'w1:p3', 'HERDR_TERMINAL_ID': 'ui-terminal'})
        self.assertTrue(result)
        self.assertEqual(calls[-1], ['plugin', 'pane', 'close', 'w2:p9'])
        self.assertFalse(close_owned(self.environment))


    def test_moved_mailbox_dedupes_in_current_workspace(self):
        from mailbox_tui import ui_state_directory
        path = ui_state_directory(self.environment) / 'panes.json'
        path.write_text(json.dumps({'w1': {'pane_id': 'w1:p3', 'terminal_id': 'ui-terminal'}}))
        opener = {'pane_id': 'w2:p1', 'terminal_id': 'open-terminal', 'workspace_id': 'w2'}
        moved = {'pane_id': 'w2:p9', 'terminal_id': 'ui-terminal', 'workspace_id': 'w2'}
        calls = []
        def transport(arguments, environment):
            calls.append(arguments)
            if arguments == ['api', 'snapshot']:
                return {'snapshot': {'panes': [opener, moved]}}
            if arguments[:2] == ['pane', 'get']:
                return {'pane': opener if arguments[2] == opener['pane_id'] else moved}
            return {}
        with patch('mailbox_tui.herdr', side_effect=transport):
            result = launch('split', {**self.environment, 'HERDR_PANE_ID': 'w2:p1'})
        self.assertTrue(result['focused'])
        self.assertEqual(result['pane_id'], 'w2:p9')
        self.assertEqual(json.loads(path.read_text())['w2']['terminal_id'], 'ui-terminal')
        self.assertFalse(any(call[:3] == ['plugin', 'pane', 'open'] for call in calls))


    def test_bell_changed_during_snapshot_is_not_consumed(self):
        self.now = 0
        app = self.app()
        app.tick()
        original_stamp = app.bell_stamp
        running = app.jobs['snapshot']
        self.mail.send_user(self.repo_id, self.recipient, body='Changed after snapshot began')
        self.now = 2
        app.tick()
        self.assertEqual(app.bell_stamp, original_stamp)
        running.result = app.local
        self.now = 3
        app.tick()
        self.assertIsNot(app.jobs['snapshot'], running)


    def test_finished_worker_close_terminates_owned_proxy_descendant(self):
        started = self.root / 'proxy-started'
        survived = self.root / 'proxy-survived'
        child = ("import pathlib,time; "
                 f"pathlib.Path({str(started)!r}).write_text('started'); "
                 f"time.sleep(.4); pathlib.Path({str(survived)!r}).write_text('survived')")
        leader = ("import os,pathlib,sys,time; "
                  f"os.posix_spawn(sys.executable,[sys.executable,'-c',{child!r}],os.environ); "
                  f"\nwhile not pathlib.Path({str(started)!r}).exists(): time.sleep(.01)")
        process = subprocess.Popen([sys.executable, '-c', leader], start_new_session=True)
        job = Job.__new__(Job)
        job.process = process
        job.input, job.output, job.errors = (tempfile.TemporaryFile(dir=self.root) for _ in range(3))
        try:
            process.wait(timeout=5)
            self.assertTrue(started.exists())
        finally:
            job.close()
        time.sleep(.5)
        self.assertFalse(survived.exists())


    def test_remote_job_uses_shared_per_host_board_with_verified_previous(self):
        previous = {'agents': [], 'hosts': [{'host': 'remote', 'uuid': 'remote-uuid'}]}
        board = {'agents': [], 'hosts': [{'host': 'remote', 'uuid': 'remote-uuid',
                                         'error': {'code': 'offline', 'message': 'SSH unavailable'}}]}
        with patch('mailbox_tui.status_board', return_value=board) as view:
            result = execute_job({'operation': 'remote', 'repo_id': self.repo_id,
                                  'host': 'remote', 'previous': previous}, self.environment)
        self.assertIs(result, board)
        self.assertEqual(view.call_args.kwargs,
                         {'repo_id': self.repo_id, 'host': 'remote',
                          'environment': self.environment, 'previous': previous})


    def test_cached_remote_rows_require_current_verified_host_uuid(self):
        self.now = 0
        app = self.app()
        remote = {**app.local['agents'][0], 'host': 'remote', 'terminal_id': 'remote-terminal'}
        app.local['agents'] = []
        app.local['peers'] = [{'canonical_name': 'remote', 'uuid': 'original-uuid'}]
        app.remote = {'remote': {'agents': [remote],
                                 'hosts': [{'host': 'remote', 'uuid': 'original-uuid'}]}}
        app.remote_errors['remote'] = 'SSH unavailable'
        self.assertTrue(app.agents()[0]['stale'])
        self.assertEqual(app.agents()[0]['unread'], '?')
        app.repo_id = 'another-repository'
        self.assertEqual(app.agents(), [])
        app.repo_id = self.repo_id
        app.local['peers'][0]['uuid'] = 'replacement-uuid'
        self.assertEqual(app.agents(), [])


    def test_enter_opens_selected_agent_and_printable_keys_are_text(self):
        app = self.app()
        target = self.select_agent(app)
        app.key('\r')
        self.assertEqual(app.mode, 'input')
        self.assertEqual((app.recipient['host'], app.recipient['terminal_id']),
                         (target['host'], target['terminal_id']))
        self.type_text(app, '한글 123q?:/')
        self.assertEqual(app.body, '한글 123q?:/')
        self.assertEqual(app.mode, 'input')
        self.assertTrue(app.running)

    def test_newline_then_enter_sends_immediate_user_fyi_without_subject(self):
        app = self.app()
        self.select_agent(app)
        app.key('\r')
        self.type_text(app, '첫 줄\nsecond line')
        self.assertEqual(app.body, '첫 줄\nsecond line')
        self.assertNotIn('mutation', app.jobs)
        app.key('\r')
        self.assertIsNone(app.confirm)
        request = app.jobs['mutation'].request
        self.assertEqual(request['operation'], 'send')
        self.assertEqual(request['kind'], 'fyi')
        self.assertFalse(request.get('subject'))
        self.assertEqual(request['recipient']['terminal_id'], 'term-b')
        result = execute_job(request, self.environment)
        message = self.mail.show_thread(result['thread_id'])['messages'][0]
        self.assertEqual(message['sender'], 'user')
        self.assertEqual(message['kind'], 'fyi')
        self.assertEqual(message['body'], '첫 줄\nsecond line')
        self.assertFalse(message.get('subject'))
        self.assertEqual(message['recipient_terminal_id'], 'term-b')

    def test_blank_body_is_field_local_error_without_job(self):
        app = self.app()
        app.key('\r')
        self.type_text(app, '  \n ')
        app.key('\r')
        self.assertTrue(app.input_error)
        self.assertEqual(app.mode, 'input')
        self.assertNotIn('mutation', app.jobs)
        snapshot = self.capture(app)
        self.assertIn(app.input_error, '\n'.join(snapshot.lines()))

    def test_escape_restores_independent_recipient_drafts_and_cursors(self):
        app = self.app()
        self.select_agent(app, 'term-b')
        app.key('\r')
        self.type_text(app, '한글 first')
        app.key(curses.KEY_LEFT)
        cursor = app.body_cursor
        app.key(ESCAPE)
        self.assertEqual(app.mode, 'nav')
        self.select_agent(app, 'term-a')
        app.key('\r')
        self.assertEqual(app.body, '')
        self.type_text(app, 'second')
        app.key(ESCAPE)
        self.select_agent(app, 'term-b')
        app.key('\r')
        self.assertEqual((app.body, app.body_cursor), ('한글 first', cursor))
        app.key(ESCAPE)
        self.select_agent(app, 'term-a')
        app.key('\r')
        self.assertEqual((app.body, app.body_cursor), ('second', 6))

    def test_stale_recipient_cannot_open_input(self):
        app = self.app()
        app.local['agents'] = [{**app.agents()[0], 'stale': True}]
        app.key('\r')
        self.assertEqual(app.mode, 'nav')
        self.assertIsNone(app.recipient)
        self.assertIn('stale', app.status.lower())
        self.assertNotIn('mutation', app.jobs)

    def test_recipient_becoming_stale_blocks_submit(self):
        app = self.app()
        app.key('\r')
        self.type_text(app, 'kept')
        app.local['agents'] = [{**agent, 'stale': True} for agent in app.local['agents']]
        app.key('\r')
        self.assertIn('stale', app.input_error.lower())
        self.assertEqual(app.body, 'kept')
        self.assertNotIn('mutation', app.jobs)

    def test_threads_enter_replies_to_latest_message_and_persists_reply(self):
        sent = self.mail.send_user(self.repo_id, self.recipient, body='First')
        latest = self.mail.send_user(self.repo_id, self.recipient, body='Latest', reply_to=sent['message_id'])
        app = self.app()
        app.key('\t')
        app.key('\r')
        self.assertEqual(app.mode, 'input')
        self.assertEqual(app.reply_to, latest['message_id'])
        self.assertEqual(app.recipient['terminal_id'], 'term-b')
        self.type_text(app, 'Reply')
        app.key('\r')
        result = execute_job(app.jobs['mutation'].request, self.environment)
        messages = self.mail.show_thread(result['thread_id'])['messages']
        reply = next(message for message in messages if message['message_id'] == result['message_id'])
        self.assertEqual(reply['reply_to'], latest['message_id'])

    def test_unresolvable_thread_participant_does_not_open_input(self):
        self.mail.send_user(self.repo_id, self.recipient, body='Unavailable')
        app = self.app()
        app.local['agents'] = []
        app.key('\t')
        app.key('\r')
        self.assertEqual(app.mode, 'nav')
        self.assertIn('unavailable', app.status.lower())

    def test_reply_draft_is_distinct_from_new_message_draft(self):
        sent = self.mail.send_user(self.repo_id, self.recipient, body='Original')
        app = self.app()
        self.select_agent(app)
        app.key('\r')
        self.type_text(app, 'New message draft')
        app.key(ESCAPE)
        app.key('\t')
        app.key('\r')
        self.assertEqual(app.reply_to, sent['message_id'])
        self.assertEqual(app.body, '')
        self.type_text(app, 'Reply draft')
        app.key(ESCAPE)
        app.key(SHIFT_TAB)
        app.key('\r')
        self.assertEqual(app.body, 'New message draft')

    def test_direct_requires_confirm_with_default_cancel(self):
        app = self.app()
        app.key('\r')
        self.type_text(app, 'May interrupt')
        app.key('\x04')
        self.assertEqual(app.delivery, 'direct')
        snapshot = self.capture(app, width=139)
        self.assertIn('DIRECT', '\n'.join(snapshot.lines()))
        app.key('\r')
        self.assertEqual(app.mode, 'confirm')
        self.assertEqual(app.confirm['choice'], 0)
        self.assertNotIn('mutation', app.jobs)
        app.key('\r')
        self.assertEqual(app.mode, 'input')
        self.assertEqual(app.body, 'May interrupt')
        self.assertNotIn('mutation', app.jobs)
        app.key('\r')
        self.accept(app)
        self.assertEqual(app.jobs['mutation'].operation, 'direct')

    def test_withdraw_requires_confirmation_and_exact_message_target(self):
        sent = self.mail.send_user(self.repo_id, self.recipient, body='Withdraw me')
        app = self.app()
        app.key('\t')
        app.key('w')
        self.assertEqual(app.mode, 'confirm')
        self.assertIn(sent['message_id'], '\n'.join(app.confirm['lines']))
        self.assertEqual(app.confirm['choice'], 0)
        app.key('\r')
        self.assertNotIn('mutation', app.jobs)
        self.assertEqual(self.mail.show_thread(sent['thread_id'])['messages'][0]['state'], 'queued')
        app.key('w')
        self.accept(app)
        execute_job(app.jobs['mutation'].request, self.environment)
        self.assertEqual(self.mail.show_thread(sent['thread_id'])['messages'][0]['state'], 'withdrawn')

    def test_retry_requires_confirmation_and_exact_outbox_target(self):
        app = self.app()
        app.local['outbox'] = [{'item_id': 'outbox-exact', 'state': 'failed',
                                'target_host': 'peer', 'kind': 'mail', 'last_error': 'offline'}]
        app.key('\t')
        app.key('t')
        self.assertEqual(app.mode, 'confirm')
        self.assertIn('outbox-exact', '\n'.join(app.confirm['lines']))
        self.assertEqual(app.confirm['choice'], 0)
        app.key('\r')
        self.assertNotIn('mutation', app.jobs)
        app.key('t')
        self.accept(app)
        request = app.jobs['mutation'].request
        self.assertEqual((request['operation'], request['item_id']), ('retry', 'outbox-exact'))
        with patch('mailbox_tui.RemoteLedger.retry', return_value={'state': 'pending'}) as retry:
            execute_job(request, self.environment)
        retry.assert_called_once_with('outbox-exact')

    def test_uncertain_old_generation_requires_evidence_then_confirm_for_both_dispositions(self):
        for key, disposition, state in [('a', 'applied', 'delivered'), ('n', 'not_applied', 'queued')]:
            with self.subTest(key=key):
                sent = self.mail.send_user(self.repo_id, self.recipient, body='Old claim ' + key)
                claim = self.mail.claim(terminal_id='term-b', agent_id=self.recipient['agent_id'],
                                        generation=self.recipient['generation'])
                self.snapshot['agents'][1]['agent_session']['value'] = 'replacement-' + key
                self.registry.reconcile(self.snapshot, refresh_display=False)
                app = self.app()
                app.key('\t')
                app.selections['threads'] = next(index for index, item in enumerate(app.items('threads'))
                                                 if item['thread_id'] == sent['thread_id'])
                snapshot = self.capture(app, width=160)
                self.assertIn('UNCERTAIN', '\n'.join(snapshot.lines()))
                app.key(key)
                self.assertEqual(app.mode, 'evidence')
                app.key('\r')
                self.assertEqual(app.mode, 'evidence')
                self.assertIsNotNone(app.edit)
                self.assertNotIn('mutation', app.jobs)
                self.type_text(app, 'Transcript line 42 checked manually')
                app.key('\r')
                self.assertEqual(app.mode, 'confirm')
                self.assertEqual(app.confirm['choice'], 0)
                target = '\n'.join(app.confirm['lines'])
                for value in (sent['message_id'], claim['claim_id'], claim['agent_id'],
                              str(claim['generation']), 'delivering', 'NOT native ACK'):
                    self.assertIn(value, target)
                self.accept(app)
                request = app.jobs['mutation'].request
                self.assertEqual(request['claim']['generation'], claim['generation'])
                self.assertEqual(request['claim']['disposition'], disposition)
                result = execute_job(request, self.environment)
                receipt = next(receipt for receipt in result['receipts'] if receipt['message_id'] == sent['message_id'])
                self.assertEqual(receipt['state'], state)
                self.assertEqual(receipt['confirmation_source'], 'operator')
                message = self.mail.show_thread(sent['thread_id'])['messages'][0]
                self.assertIn('USER-CONFIRMED', describe_message(message))
                # The next subcase uses the newly reconciled live generation.
                self.recipient = next(agent for agent in self.registry.list() if agent['terminal_id'] == 'term-b')

    def test_popup_success_closes_and_failed_send_keeps_text(self):
        for result, successful in [({'state': 'queued'}, True),
                                   ({'acceptance': 'failed', 'evidence': 'Transport rejected'}, False),
                                   ({'error': {'code': 'offline', 'message': 'Peer unavailable'}}, False)]:
            with self.subTest(result=result):
                app = self.app(popup=True)
                app.key('\r')
                self.type_text(app, 'keep on failure')
                app.key('\r')
                app.jobs['mutation'].result = result
                app.tick()
                self.assertEqual(app.running, not successful)
                self.assertEqual(app.mode, 'nav' if successful else 'input')
                self.assertEqual(app.body, '' if successful else 'keep on failure')
                self.assertEqual(bool(app.input_error), not successful)
                if successful:
                    self.assertFalse(app.drafts)

    def test_popup_success_preserves_other_nonblank_drafts(self):
        for other_text in ('other draft', ' \n '):
            with self.subTest(other_text=other_text):
                app = self.app(popup=True)
                self.select_agent(app, 'term-a')
                app.key('\r')
                self.type_text(app, other_text)
                app.key(ESCAPE)
                self.select_agent(app, 'term-b')
                app.key('\r')
                self.type_text(app, 'submitted')
                app.key('\r')
                app.jobs['mutation'].result = {'state': 'queued'}
                app.tick()
                self.assertEqual(app.running, bool(other_text.strip()))
                self.assertEqual(app.mode, 'nav')
                self.assertEqual(app.result_tags[app.identity(app.recipient)][0], 'queued')

    def test_success_keeps_newer_draft_in_input_and_popup_open(self):
        for popup in (False, True):
            with self.subTest(popup=popup):
                app = self.app(popup=popup)
                app.key('\r')
                self.type_text(app, 'submitted')
                app.key('\r')
                self.type_text(app, ' plus unsent')
                app.jobs['mutation'].result = {'state': 'queued'}
                app.tick()
                self.assertTrue(app.running)
                self.assertEqual(app.mode, 'input')
                self.assertEqual(app.body, 'submitted plus unsent')
                self.assertEqual(app.drafts[app.draft_key]['text'], app.body)
                self.assertIn('queued', app.status)

    def test_failed_send_does_not_reopen_dismissed_input_or_other_overlay(self):
        for context in ('nav', 'settings', 'help', 'confirm', 'evidence'):
            with self.subTest(context=context):
                app = self.app()
                app.key('\r')
                self.type_text(app, 'pending draft')
                saved_key = app.draft_key
                app.key('\r')
                app.key(ESCAPE)
                self.assertIsNone(app.draft_key)
                if context == 'settings':
                    app.key('s')
                elif context == 'help':
                    app.key('?')
                elif context == 'confirm':
                    app.key('q')
                elif context == 'evidence':
                    sent = self.mail.send_user(self.repo_id, self.recipient, body='Claim to inspect')
                    self.mail.claim(terminal_id='term-b', agent_id=self.recipient['agent_id'],
                                    generation=self.recipient['generation'])
                    app.local = local_snapshot(self.store, self.repo_id)
                    app.key('\t')
                    app.selections['threads'] = next(
                        index for index, item in enumerate(app.items('threads'))
                        if item['thread_id'] == sent['thread_id'])
                    app.key('a')
                    self.assertEqual(app.mode, 'evidence')
                app.jobs['mutation'].result = {'error': {'message': 'Send failed'}}
                app.tick()
                self.assertEqual(app.mode, context)
                self.assertEqual(app.drafts[saved_key]['text'], 'pending draft')
                self.assertEqual(app.status, 'Send failed')
                self.assertEqual(app.status_level, 'error')

    def test_failed_send_returns_active_input_overlays_to_input(self):
        for context in ('input', 'help', 'confirm'):
            with self.subTest(context=context):
                app = self.app()
                app.key('\r')
                self.type_text(app, 'pending draft')
                app.key('\r')
                if context == 'help':
                    app.key(F1)
                elif context == 'confirm':
                    app.key('\x04')
                    app.key('\r')
                app.jobs['mutation'].result = {'error': {'message': 'Send failed'}}
                app.tick()
                self.assertEqual(app.mode, 'input')
                self.assertEqual(app.body, 'pending draft')
                self.assertEqual(app.input_error, 'Send failed')

    def test_popup_launch_inherits_opener_repository_scope(self):
        from mailbox_tui import main
        opener = {'pane_id': 'w1:p1', 'terminal_id': 'opener', 'cwd': str(self.root)}
        values = {**self.environment, 'HERDR_PANE_ID': 'w1:p1'}
        with patch.dict(os.environ, values, clear=True), \
             patch('mailbox_tui.focused_pane_id', return_value='w1:p1'), \
             patch('mailbox_tui.pane_record', return_value=opener), \
             patch('mailbox_tui.repository_identity', return_value={'repo_id': 'opener-repo'}) as identity, \
             patch('mailbox_tui.agent_messenger.launch_plugin_popup', return_value=True) as popup:
            self.assertEqual(main(['popup']), 0)
        self.assertEqual(identity.call_args.args, (str(self.root), self.store.host))
        self.assertEqual(popup.call_args.kwargs['extra_arguments'],
                         ['--env', f'{REPO_ENV}=opener-repo'])

    def test_open_input_refreshes_snapshot_when_repository_changes(self):
        app = self.app()
        app.start_job('snapshot', 'snapshot')
        old_snapshot = app.jobs['snapshot']
        app.open_input({**self.recipient, 'repo_id': 'different-repo'})
        self.assertTrue(old_snapshot.closed)
        self.assertIsNot(app.jobs['snapshot'], old_snapshot)
        self.assertEqual(app.jobs['snapshot'].request['repo_id'], 'different-repo')

    def test_settings_edits_wait_for_matching_repository_snapshot(self):
        for settings in (None, {'repo_id': 'old-repo'}):
            for key in ('m', ' ', '\r'):
                with self.subTest(settings=settings, key=key):
                    app = self.app()
                    app.local['settings'] = settings
                    app.key('s')
                    app.key(key)
                    self.assertEqual(app.mode, 'settings')
                    self.assertIsNone(app.confirm)
                    self.assertIsNone(app.edit)
                    self.assertEqual(app.status, 'Loading settings…')
                    snapshot = app.jobs['snapshot']
                    app.key(key)
                    self.assertIs(app.jobs['snapshot'], snapshot)

    def test_settings_mode_picker_saves_auto_once_with_keys_or_mouse(self):
        for mouse in (False, True):
            with self.subTest(mouse=mouse):
                app = self.app()
                app.local['settings']['mode'] = 'off'
                app.key('s')
                if mouse:
                    self.capture(app)
                    self.mouse_at_text(app, 'mode:')
                else:
                    app.key('\r')
                self.assertEqual(app.mode_word(), 'PICK')
                self.assertIsNone(app.edit)
                if mouse:
                    self.capture(app)
                    self.mouse_at_text(app, 'auto ·')
                else:
                    app.key(curses.KEY_DOWN)
                    app.key(curses.KEY_DOWN)
                    app.key('\r')
                job = app.jobs['mutation']
                self.assertEqual(job.request, {'operation': 'setting', 'repo_id': self.repo_id,
                                               'field': 'mode', 'value': 'auto'})
                self.assertIsNone(app.confirm)
                app.key('\r')
                app.key(curses.KEY_END)
                app.key('\r')
                self.assertIs(app.jobs['mutation'], job)
                self.assertIn('pending', app.settings_results['mode'])

    def test_settings_main_picker_live_repo_targets_and_none(self):
        for choose_none in (False, True):
            with self.subTest(choose_none=choose_none):
                app = self.app()
                target = {name: app.agents()[0][name] for name in ('host', 'terminal_id')}
                app.local['agents'] += [
                    {**app.agents()[0], 'terminal_id': 'stale', 'stale': True},
                    {**app.agents()[0], 'terminal_id': 'elsewhere', 'repo_id': 'elsewhere'}]
                if choose_none:
                    app.local['settings']['settings']['main'] = {'value': target}
                app.key('s')
                app.key(curses.KEY_DOWN)
                app.key('\r')
                entries = app.settings_picker['entries']
                self.assertEqual(len(entries), 3)
                self.assertEqual(entries[0], ('(none)', None))
                app.key(curses.KEY_HOME)
                if not choose_none:
                    app.key(curses.KEY_DOWN)
                app.key('\r')
                self.assertEqual(app.jobs['mutation'].request['field'], 'main')
                self.assertEqual(app.jobs['mutation'].request['value'], None if choose_none else target)

    def test_settings_members_checklist_retains_stale_until_unticked(self):
        stale = {'host': 'missing-host', 'terminal_id': 'missing-terminal'}
        for untick in (False, True):
            with self.subTest(untick=untick):
                app = self.app()
                app.local['settings']['settings']['members'] = {'value': [stale]}
                app.key('s')
                app.key(curses.KEY_DOWN)
                app.key(curses.KEY_DOWN)
                app.key('\r')
                self.assertEqual(app.mode_word(), 'CHECK')
                self.assertIn('stale', app.settings_picker['entries'][-1][0])
                selected = app.settings_picker['entries'][0][1]
                app.key(' ')
                if untick:
                    app.key(curses.KEY_END)
                    app.key(' ')
                app.key('\r')
                self.assertEqual(app.jobs['mutation'].request['field'], 'members')
                self.assertCountEqual(app.jobs['mutation'].request['value'],
                                      [selected] if untick else [stale, selected])

    def test_settings_threshold_steps_clamp_and_escape_discards(self):
        for field, step in (('idle_minutes', 1), ('coalesce_seconds', 5),
                            ('wakes_per_hour', 1), ('max_hop', 1)):
            for initial, direction in ((12, 1), (9999, 1), (10000, 1), (1, -1), (0, -1)):
                with self.subTest(field=field, initial=initial, direction=direction):
                    app = self.app()
                    thresholds = {name: app.setting_value(name) for name in app.SETTINGS[3:]}
                    thresholds[field] = initial
                    app.local['settings']['settings']['thresholds'] = {'value': thresholds}
                    app.key('s')
                    app.settings_selection = app.SETTINGS.index(field)
                    for _ in range(2):
                        app.key(curses.KEY_RIGHT if direction > 0 else curses.KEY_LEFT)
                    expected = min(10000, max(0, initial + 2 * direction * step))
                    self.assertEqual(app.settings_pending[field], expected)
                    self.assertIn('unsaved', '\n'.join(app.settings_lines()))
                    app.key('\r')
                    self.assertEqual(app.jobs['mutation'].request['value'],
                                     {**thresholds, field: expected})
        app = self.app()
        app.key('s')
        app.key(curses.KEY_RIGHT)
        app.settings_selection = app.SETTINGS.index('idle_minutes')
        app.key(curses.KEY_RIGHT)
        app.key(ESCAPE)
        self.assertEqual(app.mode, 'nav')
        self.assertEqual(app.settings_pending, {})
        self.assertNotIn('mutation', app.jobs)
        app.key('s')
        self.assertNotIn('unsaved', '\n'.join(app.settings_lines()))

    def test_settings_picker_checklist_and_numeric_escape_cancel_without_save(self):
        for field in ('mode', 'main', 'members', 'idle_minutes'):
            with self.subTest(field=field):
                app = self.app()
                original = app.setting_value(field)
                app.key('s')
                app.settings_selection = app.SETTINGS.index(field)
                app.key('\r')
                if field == 'members':
                    app.key(' ')
                elif field == 'idle_minutes':
                    app.key(curses.KEY_BACKSPACE)
                    app.key('7')
                else:
                    app.key(curses.KEY_DOWN)
                app.key(ESCAPE)
                self.assertEqual(app.mode, 'settings')
                self.assertIsNone(app.settings_picker)
                self.assertIsNone(app.settings_numeric)
                self.assertNotIn('mutation', app.jobs)
                self.assertEqual(app.setting_value(field), original)

    def test_settings_mode_row_pending_cycles_then_saves(self):
        app = self.app()
        app.local['settings']['mode'] = 'off'
        app.key('s')
        app.key('l')
        app.key('l')
        self.assertEqual(app.settings_pending, {'mode': 'auto'})
        app.key('\r')
        self.assertEqual(app.jobs['mutation'].request['value'], 'auto')
        self.assertIsNone(app.settings_picker)

    def test_settings_numeric_escape_hatch_rejects_text_and_json(self):
        app = self.app()
        app.key('s')
        app.settings_selection = app.SETTINGS.index('idle_minutes')
        app.key('\r')
        self.assertEqual(app.mode, 'settings')
        self.assertIsNone(app.edit)
        original = app.settings_numeric['text']
        self.type_text(app, '{"mode":"auto"}')
        self.assertEqual(app.settings_numeric['text'], original)
        app.key(curses.KEY_BACKSPACE)
        self.type_text(app, '42')
        app.key('\r')
        self.assertEqual(app.jobs['mutation'].request['value']['idle_minutes'], 42)
        app = self.app()
        app.key('s')
        app.key('\r')
        before = app.settings_picker.copy()
        self.type_text(app, '{"mode":"auto"}')
        self.assertEqual(app.settings_picker, before)
        self.assertNotIn('mutation', app.jobs)
        app.key(ESCAPE)
        app.settings_selection = app.SETTINGS.index('idle_minutes')
        app.key('+')
        app.settings_selection = app.SETTINGS.index('max_hop')
        app.key('\r')
        self.assertIsNone(app.settings_numeric)

    def test_settings_mouse_threshold_arrows_and_checklist_entries(self):
        app = self.app()
        app.key('s')
        self.capture(app)
        line = next(call for call in app.screen.draw_calls if 'idle_minutes:' in call.requested_text)
        right = line.column + display_width(line.requested_text[:line.requested_text.index('▶')])
        initial = app.setting_value('idle_minutes')
        app.key(MouseEvent(line.row, right, 'press'))
        self.capture(app)
        app.key(MouseEvent(line.row, right, 'press'))
        self.assertEqual(app.settings_pending['idle_minutes'], initial + 2)
        app.key('\r')
        self.assertEqual(app.jobs['mutation'].request['value']['idle_minutes'], initial + 2)
        app = self.app()
        app.key('s')
        app.settings_selection = app.SETTINGS.index('members')
        app.key('\r')
        self.capture(app)
        selected = app.settings_picker['entries'][0][1]
        self.mouse_at_text(app, app.settings_picker['entries'][0][0])
        self.assertIn(selected, app.settings_picker['checked'])
        app.key('\r')
        self.assertEqual(app.jobs['mutation'].request['value'], [selected])

    def test_settings_field_local_save_result_and_error(self):
        for error in (False, True):
            with self.subTest(error=error):
                app = self.app()
                app.key('s')
                app.key(curses.KEY_RIGHT)
                app.key('\r')
                self.assertIn('Saving settings', '\n'.join(app.settings_lines()))
                result = RepoSettings(self.store).show(self.repo_id)
                if error:
                    result = {'error': {'code': 'denied', 'message': 'Not saved'}}
                app.jobs['mutation'].result = result
                app.tick()
                self.assertIn('Not saved' if error else 'Saved.', '\n'.join(app.settings_lines()))

    def test_settings_shortcuts_still_require_confirmation(self):
        for key, field in (('m', 'main'), (' ', 'members')):
            with self.subTest(key=key):
                app = self.app()
                app.key('s')
                app.key(key)
                self.assertEqual(app.mode, 'confirm')
                self.assertEqual(app.confirm['values']['field'], field)
                self.accept(app)
                self.assertEqual(app.jobs['mutation'].operation, 'setting')

    def test_short_failed_input_retains_text_cursor_without_duplicate_error(self):
        app = self.app()
        app.open_input({**self.recipient, 'extension_present': False})
        self.type_text(app, 'visible draft')
        app.key('\r')
        app.jobs['mutation'].result = {'error': {'message': 'Send failed'}}
        app.tick()
        for height in (10, 11, 12, 18):
            with self.subTest(height=height):
                snapshot = self.capture(app, height=height, width=79)
                text = '\n'.join(snapshot.lines())
                self.assertIn('visible draft', text)
                self.assertIsNotNone(app.cursor_position)
                errors = [call for call in snapshot.draw_calls if call.requested_text == 'Send failed'
                          or call.requested_text == '✕ Send failed']
                self.assertEqual(len(errors), 1)

    def test_short_input_drops_receiver_warning_before_text(self):
        app = self.app()
        app.open_input({**self.recipient, 'extension_present': False})
        self.type_text(app, 'visible draft')
        app.input_error = 'Send failed'
        app.screen = Screen(10, 79)
        app.render_input((4, 0, 4, 79))
        text = '\n'.join(app.screen.snapshot().lines())
        self.assertIn('visible draft', text)
        self.assertIn('Send failed', text)
        self.assertNotIn('native receiver absent', text)
        self.assertIsNotNone(app.cursor_position)

    def test_input_height_and_render_use_same_wrap_width(self):
        from mailbox_tui import wrap_message_lines
        app = self.app()
        app.open_input({**self.recipient, 'extension_present': True})
        self.type_text(app, 'x' * 116)
        with patch('mailbox_tui.wrap_message_lines', wraps=wrap_message_lines) as wrap:
            self.capture(app, height=30, width=32)
        widths = [call.kwargs['width'] for call in wrap.call_args_list]
        self.assertEqual(len(widths), 2)
        self.assertEqual(widths[0], widths[1])
        self.assertEqual(widths[0], 28)

    def test_input_content_and_cursor_keep_one_column_inside_box_padding(self):
        app = self.app()
        app.open_input({**self.recipient, 'extension_present': True})
        self.type_text(app, '가나다')
        app.screen = Screen(16, 40)
        app.render_input((3, 2, 8, 34))
        snapshot = app.screen.snapshot()
        row = snapshot.rows_containing('가나다')[0]
        self.assertEqual(snapshot.cells[row][2].text, '│')
        self.assertEqual(snapshot.cells[row][3].text, ' ')
        self.assertEqual(snapshot.cells[row][4].text, '가')
        self.assertEqual(app.cursor_position, (row, 10))
        self.assertEqual(app.screen.out_of_bounds, [])

    def test_noncompact_settings_and_help_content_keep_inner_left_padding(self):
        for mode, text in (('settings', 'Target:'), ('help', 'NAV: Tab')):
            with self.subTest(mode=mode):
                app = self.app()
                app.key('s' if mode == 'settings' else '?')
                snapshot = self.capture(app, height=24, width=80)
                call = next(call for call in snapshot.draw_calls if text in call.requested_text)
                self.assertEqual(call.column, 2)
                self.assertEqual(snapshot.cells[call.row][0].text, '│')
                self.assertEqual(snapshot.cells[call.row][1].text, ' ')

    def test_popup_success_does_not_spawn_refresh_jobs_after_exit(self):
        app = self.app(popup=True)
        app.local['peers'] = [{'canonical_name': 'peer', 'uuid': 'peer-uuid'}]
        app.key('\r')
        self.type_text(app, 'submitted')
        app.key('\r')
        app.jobs['mutation'].result = {'state': 'queued'}
        app.tick()
        self.assertFalse(app.running)
        self.assertEqual(app.jobs, {})
        app.tick()
        self.assertEqual(app.jobs, {})

    def test_stacked_region_rects_fall_back_for_short_or_narrow_body(self):
        app = self.app()
        app.layout = 'stacked'
        for focus in ('agents', 'threads', 'detail'):
            app.focus = focus
            for height, width in ((0, 120), (14, 100), (17, 200), (18, 79)):
                with self.subTest(focus=focus, height=height, width=width):
                    self.assertEqual(app.region_rects(3, height, width),
                                     {focus: (3, 0, height, width)})
        app.focus = 'agents'
        self.assertEqual(app.region_rects(3, 18, 81), {
            'agents': (3, 0, 10, 40),
            'threads': (3, 40, 10, 41),
            'detail': (13, 0, 8, 81)})
        self.assertEqual(app.region_rects(1, 80, 200)['detail'], (69, 0, 12, 200))

    def test_stacked_top_bottom_geometry_at_large_terminal_sizes(self):
        for height, width, top_height in ((35, 110, 25), (40, 120, 28), (50, 200, 36)):
            with self.subTest(height=height, width=width):
                app = self.app()
                self.capture(app, height, width)
                self.assertEqual(app.layout, 'stacked')
                regions = {value: rect for rect, kind, value in app.hit_map if kind == 'region'}
                self.assertEqual(regions, {
                    'agents': (1, 0, top_height, width // 2),
                    'threads': (1, width // 2, top_height, width - width // 2),
                    'detail': (1 + top_height, 0, height - 2 - top_height, width)})
                for _ in range(2):
                    for focus in ('threads', 'detail', 'agents'):
                        app.key('\t')
                        self.assertEqual(app.focus, focus)
                        self.capture(app, height, width)
                        self.assertEqual({value: rect for rect, kind, value in app.hit_map
                                          if kind == 'region'}, regions)

    def test_layout_uses_body_height_after_header_footer_and_status(self):
        for height, status, layout in ((19, '', 'single'), (20, '', 'stacked'),
                                       (20, 'Notice', 'single'), (21, 'Notice', 'stacked')):
            with self.subTest(height=height, status=status):
                app = self.app()
                app.status = status
                self.capture(app, height, 120)
                self.assertEqual(app.layout, layout)
                regions = [rect for rect, kind, _ in app.hit_map if kind == 'region']
                bottom = height - 1 - bool(status)
                self.assertEqual(max(y + rows for y, _, rows, _ in regions), bottom)

    def test_input_stays_full_width_below_detail_and_can_force_single(self):
        for popup in (False, True):
            with self.subTest(popup=popup):
                app = self.app(popup=popup)
                app.open_input({**self.recipient, 'extension_present': True})
                self.type_text(app, 'draft')
                with patch.object(app, 'render_input', wraps=app.render_input) as render_input:
                    self.capture(app, 40, 120)
                input_rect = render_input.call_args.args[0]
                self.assertEqual(app.layout, 'stacked')
                detail = next(rect for rect, kind, value in app.hit_map
                              if kind == 'region' and value == 'detail')
                self.assertEqual((input_rect[1], input_rect[3]), (0, 120))
                self.assertEqual(input_rect[0], detail[0] + detail[2])
                self.assertEqual(input_rect[0] + input_rect[2], 39)
                before = app.body, app.body_cursor, app.focus, dict(app.drafts)
                self.capture(app, 24, 120)
                self.assertEqual(app.layout, 'single')
                self.assertEqual(len([1 for _, kind, _ in app.hit_map if kind == 'region']), 1)
                self.assertEqual((app.body, app.body_cursor, app.focus, app.drafts), before)
                self.assertTrue(app.running)

    def test_diagnostics_preserves_last_error_after_key_dismisses_status(self):
        app = self.app()
        app.start_job('snapshot', 'snapshot')
        app.jobs['snapshot'].result = {'error': {'code': 'offline', 'message': 'Ledger unavailable'}}
        app.tick()
        error = app.status
        app.key('e')
        self.assertEqual(app.status, '')
        self.assertEqual(app.last_error, error)
        self.assertIn(error, '\n'.join(app.detail_lines()))

    def test_error_status_uses_error_pair_and_bold_glyph_without_color(self):
        for no_color in (False, True):
            with self.subTest(no_color=no_color):
                app = self.app()
                if no_color:
                    app.environment['NO_COLOR'] = '1'
                app.colors_enabled = not no_color
                app.local['agents'] = [{**app.local['agents'][0], 'stale': True}]
                app.key('\r')
                with patch('mailbox_tui.curses.color_pair', return_value=1 << 20) as pair:
                    snapshot = self.capture(app, width=100)
                status = next(call for call in snapshot.draw_calls if 'refresh before selecting' in call.requested_text)
                self.assertTrue(status.requested_text.startswith('✕ '))
                self.assertTrue(status.attribute & curses.A_BOLD)
                if no_color:
                    pair.assert_not_called()
                else:
                    self.assertIn(unittest.mock.call(4), pair.call_args_list)

    def test_nonpopup_success_stays_open_and_marks_recipient_row(self):
        app = self.app()
        target = self.select_agent(app)
        app.key('\r')
        self.type_text(app, 'Queued')
        app.key('\r')
        app.jobs['mutation'].result = {'state': 'queued'}
        app.tick()
        self.assertTrue(app.running)
        self.assertEqual(app.mode, 'nav')
        self.assertEqual(app.body, '')
        self.assertEqual(app.result_tags[(target['host'], target['terminal_id'])][0], 'queued')
        snapshot = self.capture(app, width=79)
        self.assertIn('[queued]', '\n'.join(snapshot.lines()))
        self.now = 11
        app.tick()
        self.assertFalse(app.result_tags)

    def test_narrow_failed_send_suffix_survives_colored_state_repaint(self):
        for color in (False, True):
            for error in ('Transport rejected', 'Transport rejected by peer'):
                with self.subTest(color=color, error=error):
                    app = self.app()
                    app.environment.pop('NO_COLOR', None)
                    app.local['agents'][0]['turn_state'] = 'working'
                    app.key('\r')
                    self.type_text(app, 'retain this draft')
                    app.key('\r')
                    app.jobs['mutation'].result = {'acceptance': 'failed', 'evidence': error}
                    app.tick()
                    app.key(ESCAPE)
                    app.colors_enabled = color
                    with patch('mailbox_tui.curses.color_pair', side_effect=lambda pair: pair << 8):
                        snapshot = self.capture(app, height=35, width=80)
                    label = '[failed: ' + error + ']'
                    rows = snapshot.rows_containing(label)
                    self.assertEqual(len(rows), 1)
                    row = rows[0]
                    self.assertEqual(app.layout, 'stacked')
                    self.assertIn(label + '│', snapshot.line(row))
                    label_column = display_width(snapshot.line(row).split(label)[0])
                    for cell in snapshot.cells[row][label_column:label_column + len(label)]:
                        if color:
                            self.assertEqual(cell.attribute & curses.A_COLOR, 9 << 8)
                            self.assertFalse(cell.attribute & curses.A_REVERSE)
                        else:
                            self.assertTrue(cell.attribute & curses.A_REVERSE)
                    if color and error == 'Transport rejected':
                        self.assertEqual(snapshot.cells[row][3].attribute & curses.A_COLOR, 8 << 8)
                    self.assertIn(((row, 1, 1, 38), 'list-row', 0), app.hit_map)
                    app.key(MouseEvent(row, 38, 'press'))
                    self.assertEqual(app.mode, 'input')
                    self.assertEqual(app.body, 'retain this draft')

    def test_top_level_escape_only_quits_popup(self):
        for popup in (False, True):
            with self.subTest(popup=popup):
                app = self.app(popup=popup)
                app.key(ESCAPE)
                self.assertEqual(app.running, not popup)

    def test_quit_with_nonempty_draft_confirms_and_cancel_is_default(self):
        app = self.app()
        app.key('\r')
        self.type_text(app, 'unsent')
        app.key(ESCAPE)
        app.key('q')
        self.assertEqual(app.mode, 'confirm')
        self.assertEqual(app.confirm['title'], 'Discard 1 unsent draft?')
        self.assertEqual(app.confirm['choice'], 0)
        app.key('\r')
        self.assertTrue(app.running)
        app.key('q')
        self.accept(app)
        self.assertFalse(app.running)

    def test_discard_dialog_pluralizes_the_rendered_draft_count(self):
        for count in (1, 2, 5):
            with self.subTest(count=count):
                app = self.app()
                app.drafts = {('host', str(index), None): {'text': 'unsent'}
                              for index in range(count)}
                app.request_quit()
                noun = 'draft' if count == 1 else 'drafts'
                title = f'Discard {count} unsent {noun}?'
                self.assertEqual(app.confirm['title'], title)
                snapshot = self.capture(app, height=20, width=60)
                self.assertIn(title, '\n'.join(snapshot.lines()))

    def test_compact_dialogs_are_centered_and_preserve_regions_outside_them(self):
        for height, width in ((32, 107), (20, 60)):
            for mode in ('confirm', 'evidence'):
                with self.subTest(height=height, width=width, mode=mode):
                    app = self.app()
                    background = self.capture(app, height, width)
                    if mode == 'confirm':
                        app.open_confirm('Discard 1 unsent draft?', [], 'quit', {})
                    else:
                        app.edit = {'title': 'Evidence', 'text': 'observed', 'cursor': 8,
                                    'action': 'resolve', 'return_mode': 'nav'}
                        app.mode = 'evidence'
                    snapshot = self.capture(app, height, width)
                    dialog_height, dialog_width = (6 if mode == 'evidence' else 5), 40
                    top = 1 + (height - 2 - dialog_height) // 2
                    left = (width - dialog_width) // 2
                    right, bottom = left + dialog_width - 1, top + dialog_height - 1
                    self.assertGreater(top, 1)
                    self.assertGreater(left, 1)
                    self.assertLess(bottom, height - 2)
                    self.assertEqual(snapshot.cells[top][left].text, '┌')
                    self.assertEqual(snapshot.cells[top][right].text, '┐')
                    self.assertEqual(snapshot.cells[bottom][left].text, '└')
                    self.assertEqual(snapshot.cells[bottom][right].text, '┘')
                    for row in range(top + 1, bottom):
                        self.assertEqual(snapshot.cells[row][left].text, '│')
                        self.assertEqual(snapshot.cells[row][right].text, '│')
                        self.assertEqual(snapshot.cells[row][left + 1].text, ' ')
                        self.assertEqual(snapshot.cells[row][right - 1].text, ' ')
                    self.assertTrue(all(cell.text == ' '
                                        for cell in snapshot.cells[top + 2][left + 1:right]))
                    for row in range(1, height - 1):
                        for column in range(width):
                            if not (top <= row <= bottom and left <= column <= right):
                                self.assertEqual(snapshot.cells[row][column],
                                                 background.cells[row][column], (row, column))
                    self.assertEqual(app.overlay_scroll, 0)
                    app.key(curses.KEY_DOWN)
                    self.capture(app, height, width)
                    self.assertEqual(app.overlay_scroll, 0)

    def test_confirm_targets_wrap_then_scroll_only_after_filling_the_body(self):
        for height, width in ((32, 107), (20, 60)):
            with self.subTest(height=height, width=width):
                app = self.app()
                message_id = 'm' * 120
                claim_id = 'c' * 95
                app.open_confirm('Withdraw message?', ['Message: ' + message_id, 'Claim: ' + claim_id],
                                 'withdraw', {'message_id': message_id})
                snapshot = self.capture(app, height, width)
                border = next(call for call in snapshot.draw_calls
                              if call.requested_text.startswith('┌') and call.column == 2)
                self.assertEqual(display_width(border.requested_text), width - 4)
                self.assertGreater(border.row, 1)
                content = [call.requested_text for call in snapshot.draw_calls
                           if call.column == 4 and call.row > border.row
                           and not call.requested_text.startswith('[ Cancel ]')]
                self.assertIn('Message: ' + message_id, ''.join(content))
                self.assertIn('Claim: ' + claim_id, ''.join(content))
                self.assertEqual(app.overlay_scroll, 0)
                app.key(curses.KEY_DOWN)
                self.capture(app, height, width)
                self.assertEqual(app.overlay_scroll, 0)
                app.open_confirm('Long target details', [f'Claim {index:02}: {claim_id}' for index in range(40)],
                                 'withdraw', {'message_id': message_id})
                snapshot = self.capture(app, height, width)
                self.assertEqual(snapshot.cells[1][2].text, '┌')
                self.assertEqual(snapshot.cells[height - 2][2].text, '└')
                self.assertNotIn('Claim 39:', '\n'.join(snapshot.lines()))
                app.key(curses.KEY_DOWN)
                self.capture(app, height, width)
                self.assertEqual(app.overlay_scroll, 1)
                for _ in range(200):
                    app.key(curses.KEY_DOWN)
                snapshot = self.capture(app, height, width)
                self.assertIn('Claim 39:', '\n'.join(snapshot.lines()))
                self.assertIn('[ Cancel ]', '\n'.join(snapshot.lines()))
                app.key(curses.KEY_UP)
                previous = app.overlay_scroll
                self.capture(app, height, width)
                self.assertEqual(app.overlay_scroll, previous)

    def test_evidence_dialog_scrolls_target_details_without_hiding_the_editor(self):
        app = self.app()
        app.edit = {'title': 'Operator evidence', 'text': 'verified journal', 'cursor': 16,
                    'target': [f'Claim {index:02}: ' + 'c' * 95 for index in range(40)],
                    'action': 'resolve', 'return_mode': 'nav'}
        app.mode = 'evidence'
        snapshot = self.capture(app, height=20, width=60)
        self.assertEqual(snapshot.cells[1][2].text, '┌')
        self.assertEqual(snapshot.cells[18][2].text, '└')
        self.assertIn('> verified journal', snapshot.line(17))
        self.assertEqual(app.cursor_position, (17, 22))
        app.key(curses.KEY_DOWN)
        self.capture(app, height=20, width=60)
        self.assertEqual(app.overlay_scroll, 1)
        self.assertEqual(app.edit['text'], 'verified journal')
        for _ in range(200):
            app.key(curses.KEY_DOWN)
        snapshot = self.capture(app, height=20, width=60)
        self.assertIn('Claim 39:', '\n'.join(snapshot.lines()))
        self.assertIn('> verified journal', snapshot.line(17))
        self.assertEqual(app.cursor_position, (17, 22))

    def test_ctrl_j_does_nothing_in_input(self):
        app = self.app()
        app.key('\r')
        self.type_text(app, 'body')
        before = app.body, app.body_cursor, app.draft_version
        app.key('\n')
        self.assertEqual((app.body, app.body_cursor, app.draft_version), before)
        self.assertEqual(app.mode, 'input')
        self.assertNotIn('mutation', app.jobs)

    def test_help_and_refresh_preserve_input(self):
        app = self.app()
        app.key('\r')
        self.type_text(app, '한글 draft')
        before = app.body, app.body_cursor
        app.key(F1)
        self.assertEqual(app.mode, 'help')
        app.key(ESCAPE)
        self.assertEqual(app.mode, 'input')
        self.assertEqual((app.body, app.body_cursor), before)
        app.key('\x12')
        self.assertEqual(app.mode, 'input')
        self.assertIn('snapshot', app.jobs)
        self.assertEqual((app.body, app.body_cursor), before)

    def test_navigation_uses_regions_and_digits_do_not_navigate(self):
        app = self.app()
        for digit in '1234567890':
            app.key(digit)
            self.assertEqual((app.focus, app.mode), ('agents', 'nav'))
        for key, focus in [('\t', 'threads'), ('l', 'detail'), (SHIFT_TAB, 'threads'),
                           (curses.KEY_LEFT, 'agents'), ('h', 'detail')]:
            app.key(key)
            self.assertEqual(app.focus, focus)
        app.key('?')
        self.assertEqual(app.mode, 'help')
        self.assertFalse(app.diagnostics)
        app.key(ESCAPE)
        app.key('e')
        self.assertTrue(app.diagnostics)
        self.assertEqual(app.focus, 'detail')
        app.key(ESCAPE)
        self.assertFalse(app.diagnostics)
        self.assertTrue(app.running)

    def test_layout_breakpoints_preserve_input_focus_selection_and_draft(self):
        cases = [(24, 31, 'tiny'), (24, 32, 'single'), (24, 79, 'single'),
                 (24, 80, 'single'), (24, 139, 'single'), (24, 140, 'single'),
                 (32, 79, 'single'), (32, 80, 'stacked'), (32, 140, 'stacked'),
                 (40, 120, 'stacked'), (50, 200, 'stacked'),
                 (9, 160, 'tiny'), (10, 160, 'single'),
                 (17, 160, 'single'), (18, 160, 'single'),
                 (17, 100, 'single'), (18, 100, 'single')]
        app = self.app()
        self.select_agent(app)
        app.key('\t')
        app.open_input(self.recipient)
        self.type_text(app, '한글 draft\nsecond')
        app.key(curses.KEY_LEFT)
        expected = app.mode, app.focus, dict(app.selections), app.body, app.body_cursor, dict(app.drafts)
        for height, width, layout in cases:
            with self.subTest(height=height, width=width):
                app.key(curses.KEY_RESIZE)
                snapshot = self.capture(app, height, width)
                self.assertEqual(app.layout, layout)
                self.assertEqual((app.mode, app.focus, app.selections, app.body, app.body_cursor, app.drafts), expected)
                if layout == 'tiny':
                    self.assertIn('Resize terminal', '\n'.join(snapshot.lines()))

    def test_layouts_keep_all_overlay_modes_in_bounds_without_state_loss(self):
        for mode in ('nav', 'settings', 'help', 'evidence', 'confirm'):
            for height, width in ((9, 31), (10, 32), (17, 79), (18, 80),
                                  (24, 139), (24, 140), (40, 120), (50, 200)):
                with self.subTest(mode=mode, height=height, width=width):
                    app = self.app()
                    app.key('\r')
                    self.type_text(app, 'saved')
                    app.key(ESCAPE)
                    if mode == 'settings':
                        app.key('s')
                    elif mode == 'help':
                        app.key('?')
                    elif mode == 'evidence':
                        app.key('s')
                        app.key('\r')
                    elif mode == 'confirm':
                        app.key('q')
                    before = app.mode, app.focus, dict(app.drafts)
                    self.capture(app, height, width)
                    self.assertEqual((app.mode, app.focus, app.drafts), before)

    def test_agents_header_rows_share_display_cell_columns_with_hangul_alias(self):
        app = self.app()
        app.local['agents'] = [
            {**agent, 'alias': alias, 'turn_state': 'working', 'unread': 12, 'extension_present': True}
            for agent, alias in zip(app.local['agents'], ('한글별칭', 'latin-alias'))]
        app.selections['agents'] = next(index for index, agent in enumerate(app.agents())
                                        if agent['alias'] == '한글별칭')
        snapshot = self.capture(app, width=79)
        lines = snapshot.lines()
        header = next(line for line in lines if 'ALIAS' in line and 'MAIL' in line)
        rows = [next(line for line in lines if alias in line and '● working' in line)
                for alias in ('한글별칭', 'latin-alias')]
        def cell_column(line, needle):
            return display_width(line[:line.index(needle)])
        for row in rows:
            self.assertEqual(cell_column(row, '● working'), cell_column(header, 'TURN'))
            self.assertEqual(cell_column(row, '12') + 2, cell_column(header, 'MAIL') + 4)
            self.assertEqual(cell_column(row, '✓'), cell_column(header, 'RX'))
        self.assertEqual(cell_column(rows[0], '한글별칭'), cell_column(header, 'ALIAS'))
        chosen_row = snapshot.rows_containing('한글별칭')[0]
        self.assertTrue(all(cell.attribute & curses.A_REVERSE
                            for cell in snapshot.cells[chosen_row][1:78]))

    def test_agent_metadata_stays_in_single_rows_and_scrolls_by_rows(self):
        app = self.app()
        base = app.local['agents'][0]
        metadata = ({}, {'role': 'builder'}, {'phase': 'review', 'summary': 'checking'},
                    {}, {'summary': 'ready'})
        aliases = ('alpha', 'bravo', 'charlie', 'delta', 'echo')
        app.local['agents'] = [
            {**base, 'terminal_id': f'term-{index}', 'alias': alias,
             'role': '', 'phase': '', 'summary': '', **fields}
            for index, (alias, fields) in enumerate(zip(aliases, metadata))]
        app.screen = Screen(20, 79)
        app.render_agents((0, 0, 20, 79))
        snapshot = app.screen.snapshot()
        rows = [snapshot.rows_containing(alias)[0] for alias in aliases]
        self.assertEqual(rows, list(range(rows[0], rows[0] + len(aliases))))
        self.assertIn('builder', snapshot.line(rows[1]))
        self.assertIn('review · checking', snapshot.line(rows[2]))
        self.assertIn('ready', snapshot.line(rows[4]))
        self.assertNotIn('— ·', '\n'.join(snapshot.lines()))
        app.selections['agents'] = 3
        app.screen = Screen(6, 79)
        app.render_agents((0, 0, 6, 79))
        snapshot = app.screen.snapshot()
        self.assertEqual(snapshot.rows_containing('delta'), [5])
        self.assertNotIn('echo', '\n'.join(snapshot.lines()))
        app.key(curses.KEY_DOWN)
        app.screen.erase()
        app.render_agents((0, 0, 6, 79))
        snapshot = app.screen.snapshot()
        self.assertEqual(app.selected('agents')['alias'], 'echo')
        self.assertEqual(snapshot.rows_containing('echo'), [5])
        self.assertIn('ready', snapshot.line(5))
        self.assertEqual(app.screen.out_of_bounds, [])

    def test_agent_column_order_width_threshold_and_cell_alignment(self):
        for width in (54, 55, 79):
            with self.subTest(width=width):
                app = self.app()
                app.local['agents'] = [{**app.local['agents'][0], 'alias': '한글별칭',
                                        'turn_state': 'working', 'unread': 12,
                                        'extension_present': True, 'role': 'builder',
                                        'phase': 'review', 'summary': 'checking'}]
                app.screen = Screen(6, width)
                app.render_agents((0, 0, 6, width))
                snapshot = app.screen.snapshot()
                header = snapshot.line(0)
                row = snapshot.line(snapshot.rows_containing('한글별칭')[0])
                def column(line, text):
                    return display_width(line[:line.index(text)])
                self.assertEqual(column(header, 'TURN'), 2)
                self.assertEqual(column(header, 'ALIAS'), 13)
                self.assertEqual(column(row, '● working'), 2)
                self.assertEqual(column(row, '한글별칭'), 13)
                self.assertEqual(column(row, '12') + 2, column(header, 'MAIL') + 4)
                self.assertEqual(column(row, '✓'), column(header, 'RX'))
                if width >= 55:
                    self.assertEqual(column(header, 'MAIL'), 28)
                    self.assertEqual(column(header, 'RX'), 33)
                    self.assertEqual(column(header, 'ROLE'), 36)
                    self.assertEqual(column(header, 'PHASE'), 44)
                    self.assertEqual(column(row, 'builder'), 36)
                    self.assertEqual(column(row, 'review'), 44)
                else:
                    self.assertEqual(column(header, 'MAIL'), width - 7)
                    self.assertEqual(column(header, 'RX'), width - 2)
                    self.assertNotIn('ROLE', header)
                    self.assertNotIn('builder', row)
                    self.assertNotIn('review', row)
                if width == 79:
                    self.assertIn('PHASE · SUMMARY', header)
                    self.assertIn('review · checking', row)

    def test_table_headers_host_rules_and_unfocused_selection_have_distinct_styles(self):
        with patch.multiple('mailbox_tui.curses', A_BOLD=1, A_UNDERLINE=2,
                            A_REVERSE=4, A_DIM=8):
            app = self.app()
            app.focus = 'detail'
            app.screen = Screen(16, 100)
            app.render_agents((0, 0, 8, 100))
            app.local['submissions'] = [
                {'submission_id': 'first', 'acceptance': 'accepted', 'body': 'first message',
                 'target': self.recipient},
                {'submission_id': 'second', 'acceptance': 'accepted', 'body': 'second message',
                 'target': self.recipient}]
            app.render_threads((8, 0, 8, 100))
            snapshot = app.screen.snapshot()
            for row in (0, 8):
                for cell in snapshot.cells[row]:
                    self.assertEqual(cell.attribute, 3 if cell.text.isalpha() else 1)
            self.assertIn('STATE / TITLE', snapshot.line(8))
            self.assertIn('PHASE · SUMMARY', snapshot.line(0))
            for row, separator in ((0, '·'), (8, '/')):
                column = snapshot.line(row).index(separator)
                self.assertEqual(snapshot.cells[row][column].attribute, 1)
            host_row = next(row for row, line in enumerate(snapshot.lines()) if ' · 2 agents' in line)
            host = snapshot.line(host_row)
            self.assertTrue(host.startswith('─ ui-test · 2 agents · dead:'))
            self.assertTrue(host.endswith('─'))
            self.assertTrue(all(cell.attribute == 1 for cell in snapshot.cells[host_row]))
            agent_row = snapshot.rows_containing(app.agents()[0]['alias'])[0]
            thread_row = snapshot.rows_containing('DIRECT accepted')[0]
            for row in (agent_row, thread_row, thread_row + 1):
                self.assertTrue(all(cell.attribute & 4 for cell in snapshot.cells[row]))
                self.assertFalse(any(cell.attribute & 2 for cell in snapshot.cells[row]))
            self.assertIn('user →', snapshot.line(thread_row + 1))
            second_thread = snapshot.rows_containing('DIRECT accepted')[1]
            self.assertEqual(second_thread, thread_row + 2)
            self.assertFalse(any(cell.attribute & 4 for cell in snapshot.cells[second_thread]))

    def test_turn_state_colors_only_state_cells_and_detail_word(self):
        states = (('working', False, 3), ('idle', False, 2), ('done', False, 1),
                  ('blocked', False, 4), ('unknown', False, 0), ('working', True, 0))
        for state, stale, pair in states:
            for selected in (False, True):
                with self.subTest(state=state, stale=stale, selected=selected), \
                     patch.multiple('mailbox_tui.curses', A_BOLD=1, A_UNDERLINE=2,
                                    A_REVERSE=4, A_DIM=8), \
                     patch('mailbox_tui.curses.color_pair', side_effect=lambda value: value << 8):
                    app = self.app()
                    app.environment.pop('NO_COLOR', None)
                    app.colors_enabled = True
                    base = app.local['agents'][0]
                    agent = {**base, 'alias': 'target-agent', 'turn_state': state, 'stale': stale}
                    app.local['agents'] = [
                        agent, {**base, 'alias': 'other-agent', 'terminal_id': 'other',
                                'turn_state': 'unknown', 'stale': False}]
                    app.selections['agents'] = next(
                        index for index, item in enumerate(app.agents())
                        if item['alias'] == ('target-agent' if selected else 'other-agent'))
                    app.focus = 'detail'
                    app.screen = Screen(8, 100)
                    app.render_agents((0, 0, 8, 100))
                    snapshot = app.screen.snapshot()
                    row = snapshot.rows_containing('target-agent')[0]
                    base_pair = 5 if selected else 0
                    state_pair = 5 + pair if selected else pair
                    for column, cell in enumerate(snapshot.cells[row]):
                        self.assertFalse(cell.attribute & 4)
                        if not 2 <= column < 12:
                            self.assertEqual(cell.attribute & 0xff00, base_pair << 8)
                    state_cell = snapshot.cells[row][2]
                    self.assertEqual(state_cell.attribute & 0xff00, state_pair << 8)
                    if pair:
                        self.assertTrue(state_cell.attribute & 1)
                    else:
                        self.assertTrue(state_cell.attribute & 8)
                    other_row = snapshot.rows_containing('other-agent')[0]
                    other_pair = 0 if selected else 5
                    self.assertTrue(all(cell.attribute & 0xff00 == other_pair << 8
                                        for cell in snapshot.cells[other_row]))
                    self.assertFalse(any(cell.attribute & 4 for cell in snapshot.cells[other_row]))
                    colored = [call for call in snapshot.draw_calls
                               if call.row == row and call.attribute & 0xff00
                               and call.attribute & 0xff00 != 5 << 8]
                    if pair:
                        self.assertTrue(colored)
                        self.assertTrue(all(call.column >= 2
                                            and call.column + display_width(call.requested_text) <= 12
                                            for call in colored))
                    else:
                        self.assertEqual(colored, [])
                    app.detail = agent
                    app.screen = Screen(30, 100)
                    app.render_detail((0, 0, 30, 100))
                    snapshot = app.screen.snapshot()
                    row = snapshot.rows_containing('Turn state:')[0]
                    self.assertTrue(snapshot.line(row).startswith(' Turn state: '))
                    self.assertEqual(snapshot.cells[row][0].attribute, 0)
                    start = 1 + len('Turn state: ')
                    for column, cell in enumerate(snapshot.cells[row]):
                        expected = pair << 8 if start <= column < start + len(state) else 0
                        self.assertEqual(cell.attribute & 0xff00, expected)
                    self.assertFalse(any(cell.attribute & 4 for cell in snapshot.cells[row]))
                    if pair:
                        self.assertTrue(all(cell.attribute & 1
                                            for cell in snapshot.cells[row][start:start + len(state)]))
                    else:
                        self.assertTrue(all(cell.attribute & 8
                                            for cell in snapshot.cells[row][start:start + len(state)]))

    def test_colored_thread_selection_uses_white_background_without_reverse(self):
        with patch.multiple('mailbox_tui.curses', A_BOLD=1, A_UNDERLINE=2,
                            A_REVERSE=4, A_DIM=8), \
             patch('mailbox_tui.curses.color_pair', side_effect=lambda value: value << 8):
            app = self.app()
            app.environment.pop('NO_COLOR', None)
            app.colors_enabled = True
            app.focus = 'agents'
            app.local['submissions'] = [
                {'submission_id': 'first', 'acceptance': 'accepted', 'body': 'first message',
                 'target': self.recipient},
                {'submission_id': 'second', 'acceptance': 'accepted', 'body': 'second message',
                 'target': self.recipient}]
            app.screen = Screen(8, 100)
            app.render_threads((0, 0, 8, 100))
            snapshot = app.screen.snapshot()
            row = snapshot.rows_containing('first message')[0]
            for selected_row in (row, row + 1):
                self.assertTrue(all(cell.attribute & 0xff00 == 5 << 8
                                    for cell in snapshot.cells[selected_row]))
                self.assertFalse(any(cell.attribute & 4 for cell in snapshot.cells[selected_row]))
            row = snapshot.rows_containing('second message')[0]
            self.assertEqual(snapshot.cells[row][2].attribute & 0xff00, 2 << 8)
            title_column = snapshot.line(row).index('second message')
            self.assertFalse(any(cell.attribute & 0xff00 for cell in snapshot.cells[row][title_column:]))

    def test_selected_thread_state_kind_and_operation_labels_use_contrasting_bold_foreground(self):
        cases = [
            ({'thread_id': state, 'subject': 'Legible title',
              'messages': [{'state': state, 'kind': kind, 'body': 'Body',
                            'sender_label': 'user', 'recipient_address': 'host/worker'}]},
             f'{glyph} {state}', kind_label)
            for state, glyph, kind, kind_label in (
                ('queued', '.', 'action', '! action'),
                ('delivered', '+', 'fyi', 'i fyi'),
                ('handled', '=', 'action', '! action'),
                ('failed', '!', 'fyi', 'i fyi'))]
        cases.extend([
            ({'submission_id': 'direct', 'acceptance': 'accepted', 'body': 'Legible title',
              'target': self.recipient}, '+ DIRECT accepted', '> direct'),
            ({'item_id': 'outbox', 'state': 'pending', 'kind': 'setting', 'target_host': 'remote'},
             'OUTBOX . pending', '# setting'),
            ({'item_id': 'outbox', 'state': 'failed', 'kind': 'receipt', 'target_host': 'remote',
              'last_error': 'Legible title'}, 'OUTBOX ! failed', '< receipt'),
        ])
        for item, state_label, kind_label in cases:
            with self.subTest(state=state_label, kind=kind_label), \
                 patch('mailbox_tui.curses.color_pair', side_effect=lambda value: value << 8):
                app = self.app()
                app.colors_enabled = True
                collection = ('threads' if 'thread_id' in item else
                              'submissions' if 'submission_id' in item else 'outbox')
                app.local[collection] = [item]
                app.focus = 'threads'
                app.screen = Screen(8, 100)
                app.render_threads((0, 0, 8, 100))
                snapshot = app.screen.snapshot()
                for row, label in ((2, state_label), (3, kind_label)):
                    self.assertIn(label, snapshot.line(row))
                    start = snapshot.line(row).index(label)
                    label_cells = snapshot.cells[row][start:start + len(label)]
                    self.assertTrue(all(cell.attribute & curses.A_BOLD for cell in label_cells))
                    self.assertTrue(all(not cell.attribute & curses.A_DIM for cell in label_cells))
                    self.assertTrue(all(cell.attribute & curses.A_COLOR == 5 << 8
                                        for cell in snapshot.cells[row]))
                    self.assertFalse(any(cell.attribute & curses.A_REVERSE for cell in snapshot.cells[row]))
                identity = app.identity(app.items('threads')[0])
                self.assertEqual(app.hit_map, [
                    ((2, 0, 1, 100), 'list-row', identity),
                    ((3, 0, 1, 100), 'list-row', identity)])
                self.assertEqual(app.screen.out_of_bounds, [])

    def test_thread_status_and_kind_accents_preserve_plain_title_and_participants(self):
        cases = (
            ('pending', '.', 3), ('queued', '.', 3), ('delivering', '?', 3),
            ('delivered', '+', 2), ('handled', '=', 1), ('failed', '!', 4),
            ('undeliverable', '!', 4), ('withdrawn', '-', 0),
        )
        for state, glyph, pair in cases:
            for kind, kind_label, kind_pair in (('fyi', 'i fyi', 1), ('action', '! action', 3)):
                with self.subTest(state=state, kind=kind), \
                     patch('mailbox_tui.curses.color_pair', side_effect=lambda value: value << 8):
                    app = self.app()
                    app.colors_enabled = True
                    app.local['threads'] = [{
                        'thread_id': 'styled-thread', 'subject': 'Visible title',
                        'messages': [{'state': state, 'kind': kind, 'body': 'body',
                                      'sender_label': 'sender', 'recipient_address': 'host/recipient'}]}]
                    app.selections['threads'] = 1  # Leave the representative row unselected.
                    app.screen = Screen(8, 90)
                    app.render_threads((0, 0, 8, 90))
                    snapshot = app.screen.snapshot()
                    label = '? UNCERTAIN delivering' if state == 'delivering' else f'{glyph} {state}'
                    self.assertIn(label + ' · Visible title', snapshot.line(2))
                    self.assertIn(kind_label + ' · sender → host/recipient', snapshot.line(3))
                    self.assertEqual(display_width(glyph), 1)
                    self.assertEqual(snapshot.cells[2][2].attribute & curses.A_COLOR, pair << 8)
                    self.assertEqual(snapshot.cells[3][2].attribute & curses.A_COLOR, kind_pair << 8)
                    for row, text in ((2, 'Visible title'), (3, 'sender → host/recipient')):
                        column = snapshot.line(row).index(text)
                        self.assertTrue(all(not cell.attribute & curses.A_COLOR
                                            for cell in snapshot.cells[row][column:column + len(text)]))
                    self.assertEqual(app.screen.out_of_bounds, [])

    def thread_fixture(self, identity, *states):
        return {'thread_id': identity, 'subject': identity, 'messages': [
            {'message_id': f'{identity}-{index}', 'state': state, 'kind': 'fyi',
             'body': f'{identity} body {index}', 'sender_label': 'user',
             'recipient_address': self.recipient['host'] + '/blue-owl',
             'recipient_host': self.recipient['host'],
             'recipient_terminal_id': self.recipient['terminal_id']}
            for index, state in enumerate(states)]}

    def test_thread_groups_stably_partition_all_handled_below_active_and_transport(self):
        app = self.app()
        app.local['threads'] = [
            self.thread_fixture('handled-recent', 'handled', 'handled'),
            self.thread_fixture('active-recent', 'queued'),
            self.thread_fixture('handled-older', 'handled'),
            self.thread_fixture('active-older', 'delivered')]
        app.local['submissions'] = [
            {'submission_id': 'direct', 'acceptance': 'accepted', 'body': 'Direct input',
             'target': self.recipient}]
        app.local['outbox'] = [
            {'item_id': 'transport', 'state': 'pending', 'kind': 'receipt', 'target_host': 'remote'}]
        self.assertEqual([app.identity(item) for item in app.items('threads')], [
            ('thread', 'active-recent'), ('thread', 'active-older'), ('direct', 'direct'),
            ('outbox', 'transport'), ('thread', 'handled-recent'), ('thread', 'handled-older')])
        app.screen = Screen(22, 100)
        app.render_threads((0, 0, 22, 100))
        snapshot = app.screen.snapshot()
        self.assertIn('─ Active (4) ', snapshot.line(1))
        handled_header = snapshot.rows_containing('─ Handled (2) ')[0]
        self.assertEqual(snapshot.line(handled_header - 1).strip(), '')
        self.assertLess(snapshot.rows_containing('active-older')[0], handled_header)
        self.assertGreater(snapshot.rows_containing('handled-recent')[0], handled_header)
        for row in (handled_header - 1, handled_header):
            self.assertFalse(any(rect[0] == row for rect, target, _ in app.hit_map
                                 if target == 'list-row'))

    def test_latest_handled_cannot_hide_any_earlier_unfinished_state(self):
        for state in ('pending', 'queued', 'delivering', 'delivered',
                      'undeliverable', 'withdrawn'):
            with self.subTest(state=state):
                app = self.app()
                app.local['threads'] = [
                    self.thread_fixture('complete', 'handled'),
                    self.thread_fixture('unfinished', state, 'handled')]
                first, last = app.items('threads')
                self.assertEqual(first['thread_id'], 'unfinished')
                self.assertFalse(app.thread_handled(first))
                self.assertTrue(app.thread_handled(last))
                app.screen = Screen(12, 100)
                app.render_threads((0, 0, 12, 100))
                row = app.screen.snapshot().rows_containing(' · unfinished')[0]
                expected, _ = app.thread_state(state)
                self.assertIn(expected, app.screen.snapshot().line(row))
        app.local['threads'] = [self.thread_fixture('empty')]
        self.assertFalse(app.thread_handled(app.items('threads')[0]))
        app.local['threads'] = [{'thread_id': 'missing-state', 'messages': [{}]}]
        self.assertFalse(app.thread_handled(app.items('threads')[0]))

    def test_claim_audit_evidence_does_not_invent_or_prevent_explicit_handling(self):
        sent = self.mail.send_user(self.repo_id, self.recipient, body='Audited message')
        claim = self.mail.claim(terminal_id='term-b', agent_id=self.recipient['agent_id'],
                                generation=self.recipient['generation'])
        target = {key: claim[key] for key in ('claim_id', 'agent_id', 'generation')}
        self.mail.release(**target, disposition='unknown', evidence='Delivery outcome unknown')
        newer = self.mail.send_user(self.repo_id, self.recipient, body='Later handled reply',
                                    reply_to=sent['message_id'])
        self.mail.handle(self.recipient, [newer['message_id']])
        app = self.app()
        unresolved = app.items('threads')[0]
        self.assertEqual(unresolved['messages'][0]['state'], 'delivering')
        self.assertTrue(unresolved['messages'][0]['evidence'])
        self.assertFalse(app.thread_handled(unresolved))
        self.assertEqual(unresolved['messages'][-1]['state'], 'handled')
        app.screen = Screen(12, 100)
        app.render_threads((0, 0, 12, 100))
        self.assertIn('? UNCERTAIN delivering', app.screen.snapshot().line(2))
        self.mail.resolve_claim(**target, disposition='applied', evidence='Inspected old context')
        app.local = local_snapshot(self.store, self.repo_id)
        delivered = app.items('threads')[0]
        self.assertEqual(delivered['messages'][0]['confirmation_source'], 'operator')
        self.assertEqual(delivered['messages'][0]['state'], 'delivered')
        self.assertFalse(app.thread_handled(delivered))
        self.mail.handle(self.recipient, [sent['message_id']])
        app.local = local_snapshot(self.store, self.repo_id)
        handled = app.items('threads')[0]
        self.assertEqual(handled['messages'][0]['claim_id'], claim['claim_id'])
        self.assertEqual(handled['messages'][0]['confirmation_source'], 'operator')
        self.assertTrue(handled['messages'][0]['evidence'])
        self.assertTrue(app.thread_handled(handled))
        self.capture(app, 35, 110)
        self.assertIn('Handled (1)', '\n'.join(app.screen.snapshot().lines()))

    def test_thread_empty_groups_have_no_padding_and_completed_selection_is_not_dim(self):
        for states, heading, absent in (([], None, None),
                                       (['queued'], 'Active (1)', 'Handled ('),
                                       (['handled'], 'Handled (1)', 'Active (')):
            for selected in (False, True):
                for colors in (False, True):
                    with self.subTest(states=states, selected=selected, colors=colors), \
                         patch('mailbox_tui.curses.color_pair', side_effect=lambda value: value << 8):
                        app = self.app()
                        app.colors_enabled = colors
                        app.local['threads'] = [self.thread_fixture('entry', *states)] if states else []
                        app.selections['threads'] = 0 if selected else 9
                        app.screen = Screen(8, 100)
                        app.render_threads((0, 0, 8, 100))
                        snapshot = app.screen.snapshot()
                        text = '\n'.join(snapshot.lines())
                        if heading is None:
                            self.assertIn('No threads.', text)
                            self.assertNotIn('Active (', text)
                            self.assertNotIn('Handled (', text)
                            continue
                        self.assertIn(heading, snapshot.line(1))
                        self.assertNotIn(absent, text)
                        self.assertIn(' · entry', snapshot.line(2))
                        if states == ['handled']:
                            for row in (2, 3):
                                self.assertTrue(all(bool(cell.attribute & curses.A_DIM) == (not selected)
                                                    for cell in snapshot.cells[row]))
                                if selected:
                                    self.assertTrue(snapshot.cells[row][2].attribute & curses.A_BOLD)
                                    if colors:
                                        self.assertTrue(all(cell.attribute & curses.A_COLOR == 5 << 8
                                                            for cell in snapshot.cells[row]))

    def test_new_reply_returns_selected_handled_thread_to_active_and_actions_keep_identity(self):
        handled = self.mail.send_user(self.repo_id, self.recipient, body='Handled original')
        self.mail.handle(self.recipient, [handled['message_id']])
        other = self.mail.send_user(self.repo_id, self.recipient, body='Other active')
        app = self.app()
        app.focus = app.preview = 'threads'
        app.selections['threads'] = 1
        self.capture(app, 35, 110)
        self.assertEqual(app.identity(app.selected()), ('thread', handled['thread_id']))
        reply = self.mail.send_user(self.repo_id, self.recipient, body='New reply',
                                    reply_to=handled['message_id'])
        app.start_job('snapshot', 'snapshot')
        app.jobs['snapshot'].result = local_snapshot(self.store, self.repo_id)
        app.tick()
        self.assertEqual(app.selections['threads'], 0)
        self.assertEqual(app.identity(app.selected()), ('thread', handled['thread_id']))
        self.assertEqual(app.detail['thread_id'], handled['thread_id'])
        self.assertEqual(app.items('threads')[1]['thread_id'], other['thread_id'])
        self.assertFalse(app.thread_handled(app.selected()))
        app.key('\r')
        self.assertEqual(app.reply_to, reply['message_id'])
        app.key(ESCAPE)
        app.key('w')
        self.assertEqual(app.confirm['values']['message_id'], reply['message_id'])

    def test_group_headers_and_blank_rows_are_not_actions_and_navigation_reaches_lower_group(self):
        app = self.app()
        app.local['threads'] = ([self.thread_fixture(f'active-{index}', 'queued') for index in range(2)] +
                                [self.thread_fixture(f'handled-{index}', 'handled') for index in range(12)])
        app.focus = app.preview = 'threads'
        snapshot = self.capture(app, 35, 110)
        header_row = snapshot.rows_containing('─ Handled (12) ')[0]
        thread_rect = next(rect for rect, target, value in app.hit_map
                           if target == 'region' and value == 'threads')
        for row in (header_row - 1, header_row, snapshot.rows_containing('─ Active (2) ')[0]):
            app.key(MouseEvent(row, thread_rect[1] + 3, 'press'))
            self.assertEqual((app.selections['threads'], app.mode), (0, 'nav'))
        app.key(curses.KEY_DOWN)
        self.assertEqual(app.selected()['thread_id'], 'active-1')
        app.key(curses.KEY_DOWN)
        self.assertEqual(app.selected()['thread_id'], 'handled-0')
        app.key(curses.KEY_UP)
        self.assertEqual(app.selected()['thread_id'], 'active-1')
        self.capture(app, 35, 110)
        app.key(MouseEvent(header_row, thread_rect[1] + 3, 'wheel_down'))
        self.assertEqual(app.selected()['thread_id'], 'handled-2')
        self.capture(app, 35, 110)
        app.key(MouseEvent(header_row, thread_rect[1] + 3, 'wheel_up'))
        self.assertEqual(app.selected()['thread_id'], 'active-1')
        self.capture(app, 35, 110)
        visible = {value for _, target, value in app.hit_map
                   if target == 'list-row' and isinstance(value, tuple)}
        app.key(curses.KEY_NPAGE)
        self.assertEqual(app.selections['threads'], len(visible))
        self.capture(app, 35, 110)
        app.key(curses.KEY_END)
        snapshot = self.capture(app, 35, 110)
        self.assertEqual(app.selected()['thread_id'], 'handled-11')
        self.assertGreater(app.scrolls['threads'], 0)
        self.assertIn(' · handled-11', '\n'.join(snapshot.lines()))
        self.assertEqual(sum(target == 'region' and value == 'threads'
                             for _, target, value in app.hit_map), 1)
        app.key(curses.KEY_PPAGE)
        self.assertLess(app.selections['threads'], 13)
        app.key(curses.KEY_HOME)
        snapshot = self.capture(app, 35, 110)
        self.assertEqual(app.selected()['thread_id'], 'active-0')
        self.assertIn('Active (2)', '\n'.join(snapshot.lines()))

    def test_clicks_use_rendered_thread_identity_after_snapshot_regroups_or_removes_item(self):
        app = self.app()
        app.local['threads'] = [self.thread_fixture('active', 'queued'),
                                self.thread_fixture('completed', 'handled')]
        app.focus = app.preview = 'threads'
        self.capture(app, 35, 110)
        rect = next(rect for rect, target, value in app.hit_map
                    if target == 'list-row' and value == ('thread', 'completed'))
        app.start_job('snapshot', 'snapshot')
        app.jobs['snapshot'].result = {
            **app.local, 'threads': [self.thread_fixture('completed', 'handled', 'queued'),
                                     self.thread_fixture('active', 'queued')]}
        app.tick()
        self.assertEqual(app.selected()['thread_id'], 'active')
        app.key(MouseEvent(rect[0], rect[1], 'press'))
        self.assertEqual((app.selected()['thread_id'], app.mode), ('completed', 'nav'))
        app.key(MouseEvent(rect[0], rect[1], 'press'))
        self.assertEqual((app.mode, app.reply_to), ('input', 'completed-1'))
        app.key(ESCAPE)
        app.start_job('snapshot', 'snapshot')
        app.jobs['snapshot'].result = {**app.local, 'threads': [self.thread_fixture('active', 'queued')]}
        app.tick()
        app.key(MouseEvent(rect[0], rect[1], 'press'))
        self.assertEqual((app.selected()['thread_id'], app.mode), ('active', 'nav'))

    def test_compact_detail_scrolls_full_history_and_long_input_keeps_cursor_in_bounds(self):
        for height, width in ((50, 200), (35, 110)):
            with self.subTest(height=height, width=width):
                sent = self.mail.send_user(self.repo_id, self.recipient,
                                           body='\n'.join(f'History line {index}' for index in range(50)) +
                                                '\nHISTORY-END')
                app = self.app()
                app.focus = app.preview = 'threads'
                app.selections['threads'] = next(index for index, item in enumerate(app.items('threads'))
                                                  if item['thread_id'] == sent['thread_id'])
                app.key('\t')
                self.capture(app, height, width)
                app.key(curses.KEY_END)
                snapshot = self.capture(app, height, width)
                self.assertIn('HISTORY-END', '\n'.join(snapshot.lines()))
                self.assertGreater(app.scrolls['detail'], 0)
                app.open_input({**self.recipient, 'extension_present': True})
                self.type_text(app, '\n'.join(f'Input line {index}' for index in range(40)) + '\nINPUT-END')
                snapshot = self.capture(app, height, width)
                self.assertIn('INPUT-END', '\n'.join(snapshot.lines()))
                row, column = app.cursor_position
                self.assertTrue(0 < row < height - 1)
                self.assertTrue(0 < column < width - 1)
                self.assertEqual(app.body_cursor, len(app.body))
                input_hit = next(rect for rect, target, _ in app.hit_map if target == 'input')
                self.assertTrue(input_hit[0] < row < input_hit[0] + input_hit[2] - 1)
                detail = next(rect for rect, target, value in app.hit_map
                              if target == 'region' and value == 'detail')
                self.assertTrue(8 <= detail[2] <= 12)

    def test_compact_initial_native_body_paragraphs_and_followup_remain_readable(self):
        now = 1700000000
        first_body = 'FIRST-BODY\n\nSECOND-PARAGRAPH\n' + '\n'.join(f'Original line {i}' for i in range(20))
        for height, width in ((35, 110), (50, 200)):
            with self.subTest(height=height, width=width):
                app = self.app(wall_clock=lambda: now)
                thread = self.thread_fixture('native-original', 'delivering', 'delivered')
                thread['subject'] = 'Long subject ' * 100
                thread['messages'][0].update(
                    body=first_body, created_at=now - 60, sender_label='sender-' * 100,
                    recipient_address='host/recipient-' * 100, hold_reason='Long routing reason ' * 100)
                thread['messages'][1].update(
                    body='FOLLOWUP-BODY\n' + '\n'.join(f'Followup line {i}' for i in range(8)),
                    sender_label='actual-second-sender',
                    recipient_address='actual-second-recipient', created_at=now)
                app.local['threads'] = [thread]
                app.focus, app.preview = 'detail', 'threads'
                snapshot = self.capture(app, height, width)
                rect = next(rect for rect, target, region in app.hit_map
                            if target == 'region' and region == 'detail')
                y, _, outer_height, _ = rect
                visible = snapshot.lines()[y + 2:y + outer_height - 1]
                text = '\n'.join(visible)
                self.assertIn('Message 2/2', visible[0])
                self.assertIn('FOLLOWUP-BODY', visible[2])
                self.assertIn('↑more', snapshot.line(y + 1))
                app.key(curses.KEY_HOME)
                snapshot = self.capture(app, height, width)
                visible = snapshot.lines()[y + 2:y + outer_height - 1]
                text = '\n'.join(visible)
                self.assertIn('Message 1/2', visible[0])
                self.assertIn(time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(now - 60)), visible[0])
                self.assertIn('→', visible[0])
                self.assertIn('UNCERTAIN delivering', visible[1])
                self.assertIn('fyi', visible[1])
                self.assertIn('FIRST-BODY', visible[2])
                self.assertEqual(visible[3].strip(' │'), '')
                self.assertIn('SECOND-PARAGRAPH', visible[4])
                self.assertNotIn('native-original-0', text)
                self.assertTrue(snapshot.cells[y + 2][2].attribute & curses.A_BOLD)
                self.assertFalse(snapshot.cells[y + 4][2].attribute & (curses.A_BOLD | curses.A_DIM))
                seen = []
                while True:
                    snapshot = self.capture(app, height, width)
                    seen.extend(snapshot.lines()[y + 2:y + outer_height - 1])
                    before = app.scrolls['detail']
                    app.key(curses.KEY_NPAGE)
                    if app.scrolls['detail'] == before:
                        break
                all_text = '\n'.join(seen)
                for original in first_body.splitlines():
                    self.assertIn(original, all_text)
                for value in ('Message 2/2', 'actual-second-sender', 'actual-second-recipient', 'FOLLOWUP-BODY'):
                    self.assertIn(value, all_text)
                self.assertLess(all_text.index('FIRST-BODY'), all_text.index('FOLLOWUP-BODY'))
                self.assertEqual(app.jobs, {})

    def test_new_thread_selection_opens_latest_and_toggle_reanchors_without_losing_history(self):
        app = self.app()
        thread = self.thread_fixture('latest-three', 'queued', 'delivered', 'queued')
        for index, message in enumerate(thread['messages'], 1):
            message['body'] = '\n'.join(f'MAIL-{index} line {line}' for line in range(6))
        app.local['threads'] = [thread, self.thread_fixture('single', 'queued')]
        app.focus, app.preview = 'detail', 'threads'

        def visible():
            snapshot = self.capture(app, 35, 110)
            y, _, height, _ = next(rect for rect, target, value in app.hit_map
                                   if target == 'region' and value == 'detail')
            self.assertEqual(app.detail_available, 5)
            return snapshot, y, snapshot.lines()[y + 2:y + height - 1]

        snapshot, y, lines = visible()
        self.assertIn('Message 3/3', lines[0])
        self.assertIn('MAIL-3 line 0', lines[2])
        self.assertIn('↑more', snapshot.line(y + 1))
        app.key(curses.KEY_HOME)
        self.assertIn('Message 1/3', visible()[2][0])
        for _ in range(2):
            app.key('t')
            self.assertIn('Message 3/3', visible()[2][0])
        app.key(SHIFT_TAB)
        app.key('j')
        app.key('\t')
        self.assertIn('Message 1/1', visible()[2][0])
        self.assertEqual(app.scrolls['detail'], 0)
        app.key(SHIFT_TAB)
        app.key('k')
        app.key('\t')
        self.assertIn('Message 3/3', visible()[2][0])
        self.assertFalse(app.detail_technical)
        self.assertEqual(app.jobs, {})

    def test_thread_refresh_preserves_position_and_only_follows_arrivals_at_latest(self):
        for scrolled in (False, True):
            with self.subTest(scrolled=scrolled):
                app = self.app()
                thread = self.thread_fixture('refresh-latest', 'queued', 'queued', 'queued')
                for index, message in enumerate(thread['messages'], 1):
                    message['body'] = '\n'.join(f'MAIL-{index} line {line}' for line in range(8))
                app.local['threads'] = [thread]
                app.focus, app.preview = 'detail', 'threads'
                self.capture(app, 35, 110)
                if scrolled:
                    app.key(curses.KEY_PPAGE)
                offset = app.scrolls['detail']
                app.start_job('snapshot', 'snapshot')
                app.jobs['snapshot'].result = json.loads(json.dumps(app.local))
                app.tick()
                self.capture(app, 35, 110)
                self.assertEqual(app.scrolls['detail'], offset)
                updated = json.loads(json.dumps(app.local))
                updated['threads'][0]['messages'].append(
                    {**thread['messages'][-1], 'message_id': 'new-arrival',
                     'body': 'NEW-ARRIVAL\nNew line 1\nNew line 2\nNew line 3'})
                app.start_job('snapshot', 'snapshot')
                app.jobs['snapshot'].result = updated
                app.tick()
                snapshot = self.capture(app, 35, 110)
                if scrolled:
                    self.assertEqual(app.scrolls['detail'], offset)
                else:
                    y = next(rect[0] for rect, target, value in app.hit_map
                             if target == 'region' and value == 'detail')
                    self.assertIn('Message 4/4', snapshot.line(y + 2))
                    self.assertIn('NEW-ARRIVAL', snapshot.line(y + 4))
                self.assertEqual(app.jobs, {})

    def test_short_latest_message_uses_clamped_last_page(self):
        app = self.app()
        app.local['threads'] = [self.thread_fixture('short-latest', 'queued', 'queued', 'queued')]
        app.focus, app.preview = 'detail', 'threads'
        snapshot = self.capture(app, 35, 110)
        y, _, height, _ = next(rect for rect, target, value in app.hit_map
                               if target == 'region' and value == 'detail')
        text = '\n'.join(snapshot.lines()[y + 2:y + height - 1])
        self.assertEqual(app.scrolls['detail'], app.detail_total - app.detail_available)
        self.assertIn('Message 3/3', text)
        self.assertIn('short-latest body 2', text)
        app.key(curses.KEY_HOME)
        self.assertIn('Message 1/3', self.capture(app, 35, 110).line(y + 2))

    def test_agent_detail_mail_counts_loaded_participant_threads_and_explains_reading(self):
        app = self.app()
        agent = self.select_agent(app)
        active = self.thread_fixture('active-mail', 'queued', 'handled')
        sent = self.thread_fixture('sent-mail', 'delivered')
        sent['messages'][0].update(sender=agent['agent_id'], sender_label=agent['alias'],
                                   origin_host=agent['host'], recipient_address='elsewhere/other',
                                   recipient_host='elsewhere', recipient_terminal_id='other')
        handled = self.thread_fixture('handled-mail', 'handled', 'handled')
        unrelated = self.thread_fixture('unrelated', 'queued')
        unrelated['messages'][0].update(recipient_address='elsewhere/other',
                                        recipient_host='elsewhere', recipient_terminal_id='other')
        app.local['threads'] = [active, sent, handled, unrelated]
        app.local['submissions'] = [{'submission_id': 'not-native', 'acceptance': 'accepted',
                                     'target': agent, 'body': 'DIRECT is not a thread'}]
        app.local['outbox'] = [{'item_id': 'not-thread', 'state': 'pending',
                               'kind': 'message', 'target_host': agent['host']}]
        app.refresh_detail()
        lines = app.detail_lines()
        self.assertIn('Threads: 2 active · 1 handled with this agent', lines)
        self.assertEqual(lines[0], 'Address: ' + app.address(agent))
        self.assertTrue(lines[1].startswith('Alias: '))
        mail = lines.index('Mail')
        self.assertEqual(mail, next(index for index, line in enumerate(lines)
                                   if line.startswith('Unread: ')) + 1)
        self.assertLess(mail, next(index for index, line in enumerate(lines) if line.startswith('Devices')))
        self.assertLess(mail, next(index for index, line in enumerate(lines) if line.startswith('Servers')))
        self.capture(app, 35, 110)
        app.focus = 'detail'
        for _ in range(2):
            app.key(curses.KEY_NPAGE)
            snapshot = self.capture(app, 35, 110)
        hint = next(call for call in app.screen.draw_calls if call.visible_text.startswith('Read: Tab → Threads,'))
        self.assertTrue(hint.attribute & curses.A_DIM)
        self.assertIn('Threads: 2 active', '\n'.join(snapshot.lines()))
        self.assertEqual(app.jobs, {})

    def test_agent_detail_mail_zero_case_still_explains_where_to_read(self):
        app = self.app()
        app.refresh_detail()
        lines = app.detail_lines()
        self.assertIn('No threads with this agent yet', lines)
        self.assertIn('Read: Tab → Threads, select a thread; its messages appear here', lines)

    def test_compact_direct_body_preserves_real_and_literal_newlines_and_actual_sender(self):
        body = 'DIRECT-FIRST\n\nLiteral \\n stays literal\nDIRECT-LAST'
        for height, width in ((35, 110), (50, 200)):
            for acceptance in ('accepted', 'unknown', 'failed'):
                with self.subTest(height=height, acceptance=acceptance):
                    app = self.app()
                    app.local['submissions'] = [{
                        'submission_id': 'direct-original', 'acceptance': acceptance,
                        'body': body, 'sender': 'actual-origin', 'target': self.recipient,
                        'subject': 'Long hidden subject ' * 100, 'evidence': 'Direct transport evidence'}]
                    app.focus, app.preview = 'detail', 'threads'
                    snapshot = self.capture(app, height, width)
                    y, _, outer_height, _ = next(rect for rect, target, value in app.hit_map
                                               if target == 'region' and value == 'detail')
                    lines = snapshot.lines()[y + 2:y + outer_height - 1]
                    self.assertIn('actual-origin', lines[0])
                    self.assertIn('→', lines[0])
                    self.assertIn('DIRECT ' + acceptance, lines[1])
                    self.assertIn('acceptance ≠ native delivery/handled', lines[1])
                    self.assertIn('DIRECT-FIRST', lines[2])
                    self.assertEqual(lines[3].strip(' │'), '')
                    self.assertIn('Literal \\n stays literal', lines[4])
                    self.assertNotIn('Direct transport evidence', '\n'.join(lines))
                    app.key(curses.KEY_END)
                    snapshot = self.capture(app, height, width)
                    self.assertIn('DIRECT-LAST', '\n'.join(snapshot.lines()[y + 2:y + outer_height - 1]))
                    app.key('t')
                    self.assertEqual(app.scrolls['detail'], 0)
                    self.capture(app, height, width)
                    for value in ('direct-original', 'Direct transport evidence', 'Long hidden subject'):
                        self.assertIn(value, '\n'.join(app.detail_lines()))
                    app.key('t')
                    self.assertNotIn('Direct transport evidence', '\n'.join(app.detail_lines()))
                    self.assertEqual(app.jobs, {})

    def test_detail_pages_use_actual_compact_viewport_and_show_range_and_more(self):
        for height, width, available in ((35, 110, 5), (50, 200, 9)):
            with self.subTest(height=height):
                app = self.app()
                thread = self.thread_fixture('paging', 'queued')
                thread['messages'][0]['body'] = '\n'.join(f'Page body {i}' for i in range(30))
                app.local['threads'] = [thread]
                app.focus, app.preview = 'detail', 'threads'
                snapshot = self.capture(app, height, width)
                y = next(rect[0] for rect, target, region in app.hit_map
                         if target == 'region' and region == 'detail')
                self.assertIn(f'1-{available}/32', snapshot.line(y + 1))
                self.assertNotIn('↑', snapshot.line(y + 1))
                self.assertIn('↓', snapshot.line(y + 1))
                self.assertIn('t technical', snapshot.line(y + 1))
                app.key(curses.KEY_NPAGE)
                snapshot = self.capture(app, height, width)
                self.assertEqual(app.scrolls['detail'], available - 1)
                self.assertIn(f'{available}-{2 * available - 1}/32', snapshot.line(y + 1))
                self.assertIn('↑', snapshot.line(y + 1))
                self.assertIn('↓', snapshot.line(y + 1))
                app.key(curses.KEY_PPAGE)
                self.assertEqual(app.scrolls['detail'], 0)
                app.key(curses.KEY_END)
                snapshot = self.capture(app, height, width)
                self.assertIn(f'{33 - available}-32/32', snapshot.line(y + 1))
                self.assertIn('↑', snapshot.line(y + 1))
                self.assertNotIn('↓', snapshot.line(y + 1))
                app.key('j')
                self.assertEqual(app.scrolls['detail'], 32 - available)
                app.key('k')
                self.assertEqual(app.scrolls['detail'], 31 - available)
                app.key(curses.KEY_HOME)
                app.key('k')
                self.assertEqual(app.scrolls['detail'], 0)

    def test_empty_detail_range_and_narrow_notice_keep_ack_position_and_toggle(self):
        app = self.app()
        app.detail = {'type': 'thread', 'thread_id': 'empty', 'messages': []}
        app.screen = Screen(8, 110)
        app.render_detail((0, 0, 6, 108))
        notice = app.screen.snapshot().line(0)
        self.assertIn('0-0/0', notice)
        self.assertIn('does not ACK', notice)
        self.assertIn('t technical', notice)
        self.assertNotIn('↑', notice)
        self.assertNotIn('↓', notice)
        app.focus = 'detail'
        app.key('t')
        self.assertIn('empty', '\n'.join(app.detail_lines()))
        app.key('t')
        app.detail = {'type': 'thread', **self.thread_fixture('narrow', 'queued')}
        app.detail['messages'][0]['body'] = '\n'.join('content' for _ in range(20))
        app.screen = Screen(8, 40)
        app.render_detail((0, 0, 6, 39))
        notice = app.screen.snapshot().line(0)
        for value in ('ACK', '1-5/22', '↓', 't:off'):
            self.assertIn(value, notice)
        self.assertEqual(app.screen.out_of_bounds, [])
        app.screen = Screen(8, 110)
        app.render_detail((0, 0, 1, 108))
        self.assertIn('0-0/22', app.screen.snapshot().line(0))
        self.assertEqual(app.detail_available, 0)

    def test_narrow_detail_preserves_direction_and_full_truth_warnings_without_expanding(self):
        for width in (32, 40, 48, 56, 60):
            for kind in ('operator', 'delivering', 'direct', 'outbox'):
                with self.subTest(width=width, kind=kind):
                    app = self.app(wall_clock=lambda: 1700000000)
                    app.focus, app.preview = 'detail', 'threads'
                    if kind in {'operator', 'delivering'}:
                        thread = self.thread_fixture('narrow-warning', 'delivered' if kind == 'operator'
                                                     else 'delivering')
                        thread['messages'][0].update(
                            created_at=1700000000, sender_label='origin-agent',
                            recipient_address='peer/receiver', body='Visible ordinary body',
                            confirmation_source='operator' if kind == 'operator' else None)
                        app.local['threads'] = [thread]
                    elif kind == 'direct':
                        app.local['submissions'] = [{
                            'submission_id': 'direct-warning', 'acceptance': 'accepted',
                            'sender': 'origin-agent', 'created_at': 1700000000,
                            'target': {'host': 'peer', 'alias': 'receiver', 'terminal_id': 'receiver'},
                            'body': 'Visible ordinary body'}]
                    else:
                        app.local['outbox'] = [{
                            'item_id': 'outbox-warning', 'state': 'failed', 'kind': 'receipt',
                            'target_host': 'a-long-canonical-target-host-' * 4, 'payload': '{}'}]
                    snapshot = self.capture(app, 35, width)
                    y, _, height, _ = next(rect for rect, target, value in app.hit_map
                                          if target == 'region' and value == 'detail')
                    content = snapshot.lines()[y + 2:y + height - 1]
                    # Ignore visual wrap boundaries, not warning content. The body
                    # contains no warning text, so it cannot mask a missing state cue.
                    text = ''.join(''.join(line.strip(' │') for line in content).split())
                    if kind == 'operator':
                        self.assertIn('delivered', text)
                        self.assertIn('operatorNOTnativeACK', text)
                    elif kind == 'delivering':
                        self.assertIn('UNCERTAINdelivering', text)
                    elif kind == 'direct':
                        self.assertIn('DIRECTaccepted', text)
                        self.assertIn('acceptance≠nativedelivery/handled', text)
                    else:
                        self.assertIn('OUTBOX', text)
                        self.assertIn('failed', text)
                        self.assertIn('receipt', text)
                        self.assertIn('NOTnativeACK', text)
                    if kind != 'outbox':
                        direction = next(line for line in content if '→' in line)
                        self.assertIn('origin-agent', direction)
                        self.assertIn('peer/receiver', direction)
                        self.assertIn('Message 1/1', content[0])
                        self.assertIn('Visibleordinarybody', text)
                    self.assertFalse(app.detail_technical)
                    self.assertEqual(app.scrolls['detail'], 0)
                    self.assertEqual(app.jobs, {})

    def test_technical_toggle_snapshot_selection_resize_and_clamp_policy(self):
        app = self.app()
        thread = self.thread_fixture('canonical', 'delivering')
        message = thread['messages'][0]
        message.update(body='TECH-FIRST\n' + '\n'.join('wide words ' * 15 for _ in range(20)),
                       sender_label='CANONICAL-SENDER-' * 30, recipient_address='CANONICAL-RECIPIENT-' * 30,
                       claim_id='canonical-claim', claimed_agent_id='old-agent-identity',
                       claimed_generation=17, via='relay-canonical', confirmation_source='operator',
                       evidence='EXACT EVIDENCE\nsecond evidence line', opaque={'original': 'UNMODIFIED-SOURCE'})
        app.local['threads'] = [thread, self.thread_fixture('other', 'queued')]
        app.focus, app.preview = 'detail', 'threads'
        self.capture(app, 35, 110)
        self.assertFalse(app.detail_technical)
        self.assertNotIn('canonical-claim', '\n'.join(app.detail_lines()))
        app.key(curses.KEY_END)
        self.assertGreater(app.scrolls['detail'], 0)
        app.key('t')
        self.assertTrue(app.detail_technical)
        self.assertEqual(app.scrolls['detail'], 0)
        self.capture(app, 35, 110)
        technical = '\n'.join(app.detail_lines())
        for value in ('canonical-claim', 'old-agent-identity', '17', 'relay-canonical',
                      'operator', 'EXACT EVIDENCE', 'second evidence line', 'UNMODIFIED-SOURCE',
                      message['sender_label'], message['recipient_address']):
            self.assertIn(value, technical)
        app.key(curses.KEY_NPAGE)
        offset = app.scrolls['detail']
        app.start_job('snapshot', 'snapshot')
        app.jobs['snapshot'].result = {**app.local, 'threads': [
            {**thread, 'messages': [{**message, 'hold_reason': 'Updated routing hold'}]},
            self.thread_fixture('other', 'queued')]}
        app.tick()
        self.capture(app, 35, 110)
        self.assertTrue(app.detail_technical)
        self.assertEqual(app.scrolls['detail'], offset)
        app.key(curses.KEY_END)
        self.capture(app, 50, 200)
        self.assertEqual(app.scrolls['detail'], app.detail_total - app.detail_available)
        # A shorter same-identity snapshot clamps rather than retaining an invalid offset.
        app.start_job('snapshot', 'snapshot')
        app.jobs['snapshot'].result = {**app.local, 'threads': [
            self.thread_fixture('canonical', 'queued'), self.thread_fixture('other', 'queued')]}
        app.tick()
        self.capture(app, 35, 110)
        self.assertLessEqual(app.scrolls['detail'], app.detail_total - app.detail_available)
        app.key('t')
        snapshot = self.capture(app, 35, 110)
        self.assertFalse(app.detail_technical)
        self.assertEqual(app.scrolls['detail'], 0)
        app.key('t')
        app.key(SHIFT_TAB)
        app.key('j')
        app.key('\t')
        self.capture(app, 35, 110)
        self.assertEqual(app.detail['thread_id'], 'other')
        self.assertFalse(app.detail_technical)
        self.assertEqual(app.scrolls['detail'], 0)

    def test_outbox_only_real_nested_message_payload_exposes_body_and_keeps_raw_evidence(self):
        payload = {'message': {
            'message_id': '00000000-0000-4000-8000-000000000001',
            'thread_id': '00000000-0000-4000-8000-000000000001',
            'repo_id': self.repo_id, 'reply_to': None, 'origin_host': self.store.host,
            'sender': 'user', 'sender_label': 'user', 'kind': 'action', 'hop': 0,
            'body': 'TRANSPORT-BODY\n\nTRANSPORT-LAST', 'subject': '', 'created_at': 1700000000,
            'recipient_host': 'remote', 'recipient_terminal_id': 'terminal',
            'recipient_address': 'remote/actual-recipient'},
                   'resolved': {'recipient_host': 'remote', 'recipient_terminal_id': 'terminal',
                                'recipient_address': 'remote/actual-recipient'}}
        for operation in ('message', 'receipt', 'setting', 'withdraw'):
            # A real accept payload is mail only for message operations; even a
            # misleading nested body in another operation must not become a reply.
            with self.subTest(operation=operation):
                app = self.app()
                app.local['outbox'] = [{
                    'item_id': 'transport-original', 'state': 'failed', 'kind': operation,
                    'target_host': 'remote', 'last_error': 'EXACT TRANSPORT ERROR',
                    'payload': json.dumps(payload)}]
                app.focus, app.preview = 'detail', 'threads'
                snapshot = self.capture(app, 35, 110)
                y, _, height, _ = next(rect for rect, target, value in app.hit_map
                                       if target == 'region' and value == 'detail')
                visible = '\n'.join(snapshot.lines()[y + 2:y + height - 1])
                self.assertIn('OUTBOX', visible)
                self.assertIn('failed', visible)
                self.assertIn('Transport operation:', visible)
                self.assertIn(operation, visible)
                self.assertIn('NOT native ACK', visible)
                self.assertNotIn('! action', visible)
                if operation == 'message':
                    self.assertIn('TRANSPORT-BODY', visible)
                    self.assertIn('TRANSPORT-LAST', visible)
                else:
                    self.assertNotIn('TRANSPORT-BODY', visible)
                app.key('t')
                technical = '\n'.join(app.detail_lines())
                for value in ('EXACT TRANSPORT ERROR', 'transport-original', 'TRANSPORT-BODY'):
                    self.assertIn(value, technical)
                self.assertEqual(app.jobs, {})
        for invalid in ('not JSON', '[]', json.dumps({'body': 'MISLEADING-BODY'}),
                        json.dumps({'message': {'body': ['MISLEADING-BODY']}})):
            app.detail = {'type': 'outbox', 'item_id': 'bad-payload', 'state': 'pending',
                          'kind': 'message', 'target_host': 'remote', 'payload': invalid}
            app.detail_technical = False
            self.assertNotIn('MISLEADING-BODY', '\n'.join(app.detail_lines()))

    def test_technical_key_scope_keeps_input_retry_confirmation_and_diagnostics_unchanged(self):
        app = self.app()
        self.capture(app, 35, 110)
        app.key('t')
        self.assertFalse(app.detail_technical)
        app.key('\r')
        app.key('t')
        self.assertEqual(app.body, 't')
        app.key(ESCAPE)
        app.local['outbox'] = [{'item_id': 'retry-canonical', 'kind': 'receipt', 'state': 'failed',
                               'target_host': 'remote', 'claim_id': 'retry-claim',
                               'claimed_agent_id': 'retry-old-agent', 'claimed_generation': 9}]
        app.focus = app.preview = 'threads'
        app.refresh_detail()
        app.key('t')
        self.assertEqual(app.mode, 'confirm')
        self.assertEqual(app.confirm['choice'], 0)
        target = '\n'.join(app.confirm['lines'])
        for value in ('retry-canonical', 'retry-claim', 'retry-old-agent', '9', 'failed'):
            self.assertIn(value, target)
        app.key('t')
        self.assertFalse(app.detail_technical)
        self.assertNotIn('mutation', app.jobs)
        app.key(ESCAPE)
        app.key('e')
        app.key('t')
        self.assertFalse(app.detail_technical)
        app.key(ESCAPE)
        app.key('?')
        app.key('t')
        self.assertEqual(app.mode, 'help')
        self.assertFalse(app.detail_technical)
        app.key(ESCAPE)
        app.key('s')
        app.key('t')
        self.assertEqual(app.mode, 'settings')
        self.assertFalse(app.detail_technical)
        app.key(ESCAPE)
        thread = self.thread_fixture('evidence-entry', 'delivering')
        thread['messages'][0].update(claim_id='claim', claimed_agent_id='old', claimed_generation=1)
        app.local['threads'], app.local['outbox'] = [thread], []
        app.focus = app.preview = 'threads'
        app.refresh_detail()
        app.key('a')
        app.key('t')
        self.assertEqual(app.mode, 'evidence')
        self.assertEqual(app.edit['text'], 't')
        self.assertFalse(app.detail_technical)
        self.assertNotIn('mutation', app.jobs)
        app.key(ESCAPE)
        app.focus, app.preview = 'detail', 'agents'
        app.refresh_detail()
        app.key('t')
        self.assertFalse(app.detail_technical)

    def test_technical_evidence_is_dim_but_normal_body_and_headers_survive_no_color(self):
        app = self.app()
        app.environment['NO_COLOR'] = ''
        app.initialize_colors()
        app.local['threads'] = [self.thread_fixture('dim-evidence', 'delivered')]
        app.local['threads'][0]['messages'][0].update(
            confirmation_source='operator', evidence='EXACT-TECHNICAL-EVIDENCE',
            body='Normal body includes UNCERTAIN and NOT native ACK')
        app.focus, app.preview = 'detail', 'threads'
        snapshot = self.capture(app, 35, 110)
        y = next(rect[0] for rect, target, value in app.hit_map
                 if target == 'region' and value == 'detail')
        self.assertIn('operator NOT native ACK', snapshot.line(y + 3))
        self.assertTrue(snapshot.cells[y + 2][2].attribute & curses.A_BOLD)
        self.assertFalse(snapshot.cells[y + 4][2].attribute & (curses.A_DIM | curses.A_BOLD))
        app.key('t')
        # Scroll within the same compact panel until the canonical evidence is visible.
        found = False
        while True:
            snapshot = self.capture(app, 35, 110)
            rows = snapshot.rows_containing('evidence: EXACT-TECHNICAL-EVIDENCE')
            if rows:
                self.assertTrue(snapshot.cells[rows[0]][2].attribute & curses.A_DIM)
                found = True
                break
            before = app.scrolls['detail']
            app.key(curses.KEY_NPAGE)
            if before == app.scrolls['detail']:
                break
        self.assertTrue(found)
        self.assertFalse(any(call.attribute & curses.A_COLOR for call in snapshot.draw_calls))

    def test_thread_uncertainty_is_not_hidden_by_a_later_delivered_message(self):
        app = self.app()
        app.local['threads'] = [{
            'thread_id': 'held', 'messages': [
                {'state': 'delivering', 'kind': 'action'},
                {'state': 'delivered', 'kind': 'fyi', 'body': 'Newer message'}]}]
        app.screen = Screen(8, 90)
        app.render_threads((0, 0, 8, 90))
        self.assertIn('? UNCERTAIN delivering · Newer message', app.screen.snapshot().line(2))
        self.assertIn('i fyi', app.screen.snapshot().line(3))
        self.assertEqual(app.jobs, {})

    def test_direct_acceptance_and_outbox_are_distinct_from_native_receipts(self):
        for acceptance, glyph, pair in (('submitting', '>', 3), ('accepted', '+', 2),
                                        ('unknown', '?', 3), ('failed', '!', 4)):
            with self.subTest(acceptance=acceptance), \
                 patch('mailbox_tui.curses.color_pair', side_effect=lambda value: value << 8):
                app = self.app()
                app.colors_enabled = True
                app.local['submissions'] = [{
                    'submission_id': 'direct-evidence', 'acceptance': acceptance,
                    'body': 'Explicit user input', 'target': self.recipient}]
                app.local['outbox'] = [
                    {'item_id': 'pending', 'state': 'pending', 'kind': 'message', 'target_host': 'remote'},
                    {'item_id': 'failed', 'state': 'failed', 'kind': 'receipt', 'target_host': 'remote',
                     'last_error': 'Transport rejected'}]
                app.selections['threads'] = 9
                app.screen = Screen(10, 100)
                app.render_threads((0, 0, 10, 100))
                snapshot = app.screen.snapshot()
                self.assertIn(f'{glyph} DIRECT {acceptance}', snapshot.line(2))
                self.assertEqual(snapshot.cells[2][2].attribute & curses.A_COLOR, pair << 8)
                self.assertIn('> direct · user →', snapshot.line(3))
                self.assertIn('acceptance ≠ native delivery', snapshot.line(3))
                self.assertIn('OUTBOX . pending', snapshot.line(4))
                self.assertIn('OUTBOX ! failed · Transport rejected', snapshot.line(6))
                app.detail = app.items('threads')[0]
                app.screen = Screen(20, 100)
                app.render_detail((0, 0, 20, 100))
                detail = app.screen.snapshot()
                state_row = detail.rows_containing(f'DIRECT {acceptance}')[0]
                self.assertIn('acceptance ≠ native delivery/handled', '\n'.join(detail.lines()))
                self.assertIn('Explicit user input', '\n'.join(detail.lines()))
                self.assertEqual(detail.cells[state_row][1].attribute & curses.A_COLOR, pair << 8)
                self.assertEqual(app.jobs, {})

    def test_outbox_transport_operations_cover_pending_and_failed_without_inventing_mail_kind(self):
        for operation, glyph in (('message', '>'), ('receipt', '<'), ('setting', '#'), ('withdraw', '-')):
            for state, state_glyph, state_pair in (('pending', '.', 3), ('failed', '!', 4)):
                with self.subTest(operation=operation, state=state), \
                     patch('mailbox_tui.curses.color_pair', side_effect=lambda value: value << 8):
                    app = self.app()
                    app.colors_enabled = True
                    app.local['outbox'] = [{
                        'item_id': 'transport-operation', 'state': state, 'kind': operation,
                        'target_host': 'remote', 'last_error': 'Transport rejected' if state == 'failed' else None,
                        # A message transport payload may carry an action, but its operation
                        # label must still say message rather than guessing its mail kind.
                        'payload': {'kind': 'action'} if operation == 'message' else {}}]
                    app.selections['threads'] = 1
                    app.screen = Screen(8, 100)
                    app.render_threads((0, 0, 8, 100))
                    snapshot = app.screen.snapshot()
                    self.assertIn(f'OUTBOX {state_glyph} {state}', snapshot.line(2))
                    self.assertIn(f'{glyph} {operation} · → remote · {state}', snapshot.line(3))
                    self.assertNotIn('! action', snapshot.line(3))
                    self.assertNotIn('i fyi', snapshot.line(3))
                    self.assertNotIn('? ' + operation, snapshot.line(3))
                    self.assertEqual(display_width(glyph), 1)
                    self.assertEqual(snapshot.cells[2][2].attribute & curses.A_COLOR, state_pair << 8)
                    self.assertEqual(snapshot.cells[3][2].attribute & curses.A_COLOR, 1 << 8)
                    if state == 'failed':
                        self.assertIn('Transport rejected', snapshot.line(2))
                    app.detail = app.items('threads')[0]
                    app.screen = Screen(20, 100)
                    app.render_detail((0, 0, 20, 100))
                    detail = app.screen.snapshot()
                    state_row = detail.rows_containing(f'OUTBOX {state_glyph} {state}')[0]
                    self.assertIn(f'Transport operation: {glyph} {operation}', '\n'.join(detail.lines()))
                    self.assertIn('NOT native ACK', '\n'.join(detail.lines()))
                    self.assertEqual(detail.cells[state_row][1].attribute & curses.A_COLOR, state_pair << 8)
                    self.assertEqual(app.jobs, {})

    def test_selected_narrow_thread_rows_keep_suffix_geometry_and_no_color_cues(self):
        for no_color in (False, True):
            with self.subTest(no_color=no_color), \
                 patch('mailbox_tui.curses.color_pair', side_effect=lambda value: value << 8) as pair:
                app = self.app()
                app.environment.pop('NO_COLOR', None)
                if no_color:
                    app.environment['NO_COLOR'] = '1'
                    app.initialize_colors()
                else:
                    app.colors_enabled = True
                app.focus = 'threads'
                app.local['threads'] = [{
                    'thread_id': 'narrow', 'subject': '한글 title [failed: rejected]',
                    'messages': [{'state': 'failed', 'kind': 'action', 'body': 'body',
                                  'sender_label': 'user', 'recipient_address': 'host/recipient'}]}]
                app.screen = Screen(8, 46)
                app.render_threads((1, 1, 6, 44))
                snapshot = app.screen.snapshot()
                row = snapshot.rows_containing('[failed: rejected]')[0]
                self.assertIn('! failed · 한글 title [failed: rejected]', snapshot.line(row))
                self.assertIn('! action · user → host/recipient', snapshot.line(row + 1))
                self.assertEqual(app.hit_map, [
                    ((row, 1, 1, 44), 'list-row', ('thread', 'narrow')),
                    ((row + 1, 1, 1, 44), 'list-row', ('thread', 'narrow'))])
                for selected_row in (row, row + 1):
                    for cell in snapshot.cells[selected_row][1:45]:
                        if no_color:
                            self.assertTrue(cell.attribute & curses.A_REVERSE)
                            self.assertFalse(cell.attribute & curses.A_COLOR)
                        else:
                            self.assertEqual(cell.attribute & curses.A_COLOR, 5 << 8)
                            self.assertFalse(cell.attribute & curses.A_REVERSE)
                if no_color:
                    pair.assert_not_called()
                self.assertEqual(app.screen.out_of_bounds, [])

    def test_thread_detail_state_kind_and_operator_evidence_keep_their_meaning(self):
        app = self.app()
        message = {'state': 'delivered', 'kind': 'action', 'body': 'Body says UNCERTAIN and NOT native ACK',
                   'message_id': 'evidence', 'sender_label': 'user', 'recipient_address': 'host/recipient',
                   'confirmation_source': 'operator', 'evidence': 'Checked old claim'}
        app.detail = {'type': 'thread', 'thread_id': 'evidence', 'messages': [message]}
        app.colors_enabled = True
        with patch('mailbox_tui.curses.color_pair', side_effect=lambda value: value << 8):
            app.screen = Screen(30, 110)
            app.render_detail((0, 0, 30, 110))
        snapshot = app.screen.snapshot()
        status_row = snapshot.rows_containing('delivered')[0]
        self.assertIn('action', snapshot.line(status_row))
        self.assertTrue(snapshot.cells[status_row][1].attribute & curses.A_BOLD)
        self.assertIn('operator', snapshot.line(status_row))
        self.assertIn('NOT native ACK', snapshot.line(status_row))
        body_row = snapshot.rows_containing('Body says UNCERTAIN')[0]
        self.assertFalse(snapshot.cells[body_row][1].attribute & (curses.A_BOLD | curses.A_DIM))
        self.assertEqual(app.jobs, {})

    def test_detail_and_empty_list_content_have_one_column_left_padding(self):
        app = self.app()
        app.screen = Screen(12, 50)
        app.detail = None
        app.render_detail((1, 3, 5, 40))
        snapshot = app.screen.snapshot()
        for text in ('Read-only', 'Select an agent'):
            row = snapshot.rows_containing(text)[0]
            self.assertEqual(snapshot.cells[row][3].text, ' ')
            self.assertTrue(snapshot.line(row).startswith('    ' + text))
        app.screen = Screen(12, 50)
        app.local['threads'] = []
        app.local['submissions'] = []
        app.local['outbox'] = []
        app.render_threads((1, 3, 5, 40))
        snapshot = app.screen.snapshot()
        row = snapshot.rows_containing('No threads.')[0]
        self.assertTrue(snapshot.line(row).startswith('    No threads.'))
        self.assertEqual(snapshot.cells[row][3].text, ' ')
        self.assertEqual(app.screen.out_of_bounds, [])
        app.screen = Screen(12, 50)
        app.local['agents'] = []
        app.remote = {}
        app.remote_errors = {}
        app.render_agents((1, 3, 5, 40))
        snapshot = app.screen.snapshot()
        row = snapshot.rows_containing('No agents.')[0]
        self.assertTrue(snapshot.line(row).startswith('    No agents.'))
        self.assertEqual(snapshot.cells[row][3].text, ' ')
        self.assertEqual(app.screen.out_of_bounds, [])

    def test_color_initialization_assigns_turn_state_palette(self):
        app = self.app()
        app.environment.pop('NO_COLOR', None)
        with patch('mailbox_tui.curses.has_colors', return_value=True), \
             patch('mailbox_tui.curses.start_color') as start, \
             patch('mailbox_tui.curses.use_default_colors'), \
             patch('mailbox_tui.curses.init_pair') as initialize:
            app.initialize_colors()
        start.assert_called_once_with()
        self.assertEqual(initialize.call_args_list, [
            unittest.mock.call(1, curses.COLOR_CYAN, -1),
            unittest.mock.call(2, curses.COLOR_GREEN, -1),
            unittest.mock.call(3, curses.COLOR_YELLOW, -1),
            unittest.mock.call(4, curses.COLOR_RED, -1),
            unittest.mock.call(5, curses.COLOR_BLACK, curses.COLOR_WHITE),
            unittest.mock.call(6, curses.COLOR_CYAN, curses.COLOR_WHITE),
            unittest.mock.call(7, curses.COLOR_GREEN, curses.COLOR_WHITE),
            unittest.mock.call(8, curses.COLOR_YELLOW, curses.COLOR_WHITE),
            unittest.mock.call(9, curses.COLOR_RED, curses.COLOR_WHITE)])
        self.assertTrue(app.colors_enabled)

    def test_no_color_never_initializes_or_uses_pairs_including_empty_value(self):
        for value in ('1', ''):
            with self.subTest(value=value):
                app = self.app()
                app.environment['NO_COLOR'] = value
                for agent in app.local['agents']:
                    agent['turn_state'] = 'working'
                with patch('mailbox_tui.curses.has_colors', return_value=True) as has_colors, \
                     patch('mailbox_tui.curses.start_color') as start, \
                     patch('mailbox_tui.curses.init_pair') as initialize, \
                     patch('mailbox_tui.curses.color_pair') as pair:
                    app.initialize_colors()
                    snapshot = self.capture(app, height=40, width=140)
                has_colors.assert_not_called()
                start.assert_not_called()
                initialize.assert_not_called()
                pair.assert_not_called()
                self.assertFalse(app.colors_enabled)
                self.assertEqual(app.layout, 'stacked')
                text = '\n'.join(snapshot.lines())
                for label in ('Agents', 'Threads', 'Detail', 'Turn state: working'):
                    self.assertIn(label, text)
                self.assertFalse(any(call.attribute & curses.A_COLOR for call in snapshot.draw_calls))

    def test_settings_main_and_members_use_agents_selection_through_confirmation(self):
        for key, field in [('m', 'main'), (' ', 'members')]:
            with self.subTest(key=key):
                app = self.app()
                selected = self.select_agent(app, 'term-a')
                app.recipient = self.recipient  # Deliberately different: never the settings target.
                app.key('s')
                snapshot = self.capture(app)
                self.assertIn('Target: ' + app.address(selected), '\n'.join(snapshot.lines()))
                app.key(key)
                self.assertEqual(app.mode, 'confirm')
                self.assertEqual(app.confirm['choice'], 0)
                self.assertIn('Target: ' + app.address(selected), app.confirm['lines'])
                app.key('\r')
                self.assertNotIn('mutation', app.jobs)
                app.key(key)
                self.accept(app)
                request = app.jobs['mutation'].request
                target = {name: selected[name] for name in ('host', 'terminal_id')}
                self.assertEqual(request['field'], field)
                self.assertEqual(request['value'], target if field == 'main' else [target])
                with patch('ledger_display.LedgerDisplay.refresh'):
                    execute_job(request, self.environment)
                self.assertEqual(RepoSettings(self.store).show(self.repo_id)['settings'][field]['value'], request['value'])

    def test_settings_picker_mutates_real_internal_service_without_confirm(self):
        app = self.app()
        app.key('s')
        app.key('\r')
        self.assertEqual(app.mode_word(), 'PICK')
        app.key(curses.KEY_HOME)
        app.key(curses.KEY_DOWN)
        app.key('\r')
        self.assertIsNone(app.confirm)
        with patch('ledger_display.LedgerDisplay.refresh'), patch('ledger_worker.ensure_worker'):
            execute_job(app.jobs['mutation'].request, self.environment)
        self.assertEqual(RepoSettings(self.store).show(self.repo_id)['mode'], 'manual')

    def test_setting_result_precedes_next_toggle_and_discards_stale_snapshot(self):
        app = self.app()
        selected = self.select_agent(app)
        target = {key: selected[key] for key in ('host', 'terminal_id')}
        app.key('s')
        app.key(' ')
        self.accept(app)
        mutation = app.jobs['mutation']
        app.start_job('snapshot', 'snapshot')
        stale = app.jobs['snapshot']
        stale.result = json.loads(json.dumps(app.local))
        with patch('ledger_display.LedgerDisplay.refresh'):
            mutation.result = RepoSettings(self.store).mutate(self.repo_id, 'members', [target])
        app.tick()
        self.assertTrue(stale.closed)
        self.assertIsNot(app.jobs['snapshot'], stale)
        self.assertEqual(app.local['settings']['settings']['members']['value'], [target])
        app.key(' ')
        self.accept(app)
        self.assertEqual(app.jobs['mutation'].request['value'], [])
        with patch('ledger_display.LedgerDisplay.refresh'):
            app.jobs['mutation'].result = execute_job(app.jobs['mutation'].request, self.environment)
        app.tick()
        self.assertEqual(app.local['settings']['settings']['members']['value'], [])
        self.assertEqual(RepoSettings(self.store).show(self.repo_id)['settings']['members']['value'], [])

    def test_single_mutation_keeps_newer_unsubmitted_input(self):
        app = self.app()
        app.key('\r')
        self.type_text(app, 'A')
        app.key('\r')
        mutation = app.jobs['mutation']
        self.type_text(app, 'B')
        app.key('\r')
        self.assertIs(app.jobs['mutation'], mutation)
        self.assertEqual(mutation.request['body'], 'A')
        self.assertEqual(app.body, 'AB')
        self.assertIn('not been submitted', app.status)
        mutation.result = {'state': 'queued'}
        app.tick()
        self.assertEqual(app.body, 'AB')
        app.open_input(app.recipient)
        self.assertEqual(app.body, 'AB')

    def test_finished_send_does_not_clear_different_recipient_draft(self):
        app = self.app()
        self.select_agent(app, 'term-b')
        app.key('\r')
        self.type_text(app, 'sent draft')
        app.key('\r')
        app.key(ESCAPE)
        self.select_agent(app, 'term-a')
        app.key('\r')
        self.type_text(app, 'different draft')
        app.jobs['mutation'].result = {'state': 'queued'}
        app.tick()
        self.assertEqual(app.mode, 'input')
        self.assertEqual(app.body, 'different draft')
        self.assertEqual(len(app.drafts), 1)

    def test_local_bell_and_remote_poll_cadences_are_independent_of_input(self):
        app = self.app()
        app.local['peers'] = [{'canonical_name': 'offline', 'uuid': 'offline-uuid'}]
        app.key('\r')
        self.type_text(app, 'draft')
        app.tick()
        remote = app.jobs['remote:offline']
        snapshot = app.jobs['snapshot']
        snapshot.result = app.local
        remote.result = {'error': {'message': 'SSH unavailable'}}
        self.now = .5
        app.tick()
        self.assertNotIn('snapshot', app.jobs)
        self.assertNotIn('remote:offline', app.jobs)
        self.mail.send_user(self.repo_id, self.recipient, body='bell changed')
        self.now = .99
        app.tick()
        self.assertNotIn('snapshot', app.jobs)
        self.now = 1
        app.tick()
        self.assertIn('snapshot', app.jobs)
        self.now = 29.99
        app.tick()
        self.assertNotIn('remote:offline', app.jobs)
        self.now = 30
        app.tick()
        retry = app.jobs['remote:offline']
        self.assertIsNot(retry, remote)
        self.now = 61
        app.tick()
        self.assertIs(app.jobs['remote:offline'], retry)
        self.assertEqual((app.mode, app.body), ('input', 'draft'))

    def test_snapshot_refresh_preserves_selected_agent_and_thread_identity(self):
        first = self.mail.send_user(self.repo_id, self.recipient, body='First thread')
        app = self.app()
        selected = self.select_agent(app)
        second = self.mail.send_user(self.repo_id, self.recipient, body='Second thread')
        refreshed = local_snapshot(self.store, self.repo_id)
        refreshed['agents'] = list(reversed(refreshed['agents']))
        app.start_job('snapshot', 'snapshot')
        app.jobs['snapshot'].result = refreshed
        app.tick()
        self.assertEqual(app.selected('agents')['terminal_id'], selected['terminal_id'])
        self.assertEqual(app.selected('threads')['thread_id'], first['thread_id'])
        self.assertNotEqual(first['thread_id'], second['thread_id'])

    def test_async_remote_arrival_and_peer_removal_preserve_selected_identity(self):
        app = self.app()
        app.next_bell = 10
        self.select_agent(app, 'term-a')
        selected = app.selected('agents')
        app.local['peers'] = [{'canonical_name': 'aaa-peer', 'uuid': 'peer-uuid'}]
        app.remote_due['aaa-peer'] = 30
        remote = {**selected, 'host': 'aaa-peer', 'terminal_id': 'remote-terminal',
                  'agent_id': 'remote-agent', 'summary': 'Remote task'}
        app.start_job('remote:aaa-peer', 'remote', host='aaa-peer')
        app.jobs['remote:aaa-peer'].result = {
            'agents': [remote], 'hosts': [{'host': 'aaa-peer', 'uuid': 'peer-uuid', 'error': None}]}
        app.tick()
        self.assertEqual(app.selections['agents'], 2)
        self.assertEqual(app.selected('agents')['terminal_id'], selected['terminal_id'])
        app.remote_errors['aaa-peer'] = 'SSH unavailable'
        app.start_job('remote:aaa-peer', 'remote', host='aaa-peer')
        pending = app.jobs['remote:aaa-peer']
        app.start_job('snapshot', 'snapshot')
        app.jobs['snapshot'].result = {**app.local, 'peers': []}
        app.tick()
        self.assertTrue(pending.closed)
        self.assertEqual((app.remote, app.remote_errors, app.remote_due), ({}, {}, {}))
        self.assertNotIn('remote:aaa-peer', app.jobs)
        self.assertEqual(app.selected('agents')['terminal_id'], selected['terminal_id'])

    def test_detail_refreshes_with_snapshot_and_inspection_never_acks(self):
        sent = self.mail.send_user(self.repo_id, self.recipient, body='Read-only detail')
        app = self.app()
        app.key('\t')
        app.key('\t')
        self.assertEqual(app.focus, 'detail')
        with patch.object(Mailbox, 'claim', side_effect=AssertionError('inspection claimed mail')), \
             patch.object(Mailbox, 'read', side_effect=AssertionError('inspection read inbox')), \
             patch.object(Mailbox, 'handle', side_effect=AssertionError('inspection handled mail')), \
             patch.object(Mailbox, 'started', side_effect=AssertionError('inspection ACKed mail')), \
             patch.object(Mailbox, 'resolve_claim', side_effect=AssertionError('inspection resolved mail')):
            snapshot = self.capture(app, width=160)
            app.key(curses.KEY_NPAGE)
            app.render()
            app.key('t')
            self.capture(app, height=35, width=110)
            app.key(curses.KEY_END)
            app.key(curses.KEY_HOME)
            app.key('t')
            self.capture(app, height=35, width=110)
            self.assertNotIn('mutation', app.jobs)
        self.assertIn('Read-only · viewing does not ACK', '\n'.join(snapshot.lines()))
        self.assertEqual(self.mail.show_thread(sent['thread_id'])['messages'][0]['state'], 'queued')
        self.mail.withdraw(sent['message_id'])
        app.start_job('snapshot', 'snapshot')
        app.jobs['snapshot'].result = local_snapshot(self.store, self.repo_id)
        app.tick()
        self.assertEqual(app.detail['messages'][0]['state'], 'withdrawn')

    def test_stale_peer_keeps_summary_unknown_unread_and_visible_host_error(self):
        app = self.app()
        remote = {**app.local['agents'][0], 'host': 'remote', 'alias': 'remote-worker',
                  'terminal_id': 'remote-terminal', 'summary': '遠隔 요약', 'unread': 7}
        app.local['agents'] = []
        app.local['peers'] = [{'canonical_name': 'remote', 'uuid': 'remote-uuid'}]
        app.remote = {'remote': {'agents': [remote], 'hosts': [{'host': 'remote', 'uuid': 'remote-uuid'}]}}
        app.remote_errors['remote'] = 'SSH unavailable'
        snapshot = self.capture(app, width=79)
        body = '\n'.join(snapshot.lines())
        for value in ('remote', 'stale', 'SSH unavailable', '遠隔 요약'):
            self.assertIn(value, body)
        self.assertEqual(app.selected('agents')['unread'], '?')
        app.key('h')
        self.capture(app, height=50, width=79)
        self.assertIn('Unread: ?', app.detail_lines())
        self.assertIn('Host: remote  STALE/offline', app.detail_lines())

    def test_agent_detail_preserves_full_summary_timestamps_and_boolean_labels(self):
        now = 1700000000.25
        app = self.app(wall_clock=lambda: now)
        summary = '전체 요약 🐦\n日本語の詳細 ' * 4 + 'SUMMARY-END'
        agent = {**app.local['agents'][0], 'summary': summary, 'status_updated_at': now - 12,
                 'last_seen': now - 120, 'first_seen': now - 90000,
                 'is_main_checkout': 1, 'extension_present': 0, 'user_border': False}
        app.local['agents'] = [agent]
        app.key('h')
        self.capture(app, height=100, width=79)
        lines = app.detail_lines()
        self.assertIn('Summary: ' + summary, lines)
        self.assertIn('Last updated: ' + time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(now - 12)) + ' (12s ago)', lines)
        self.assertTrue(any('(2m ago)' in line for line in lines))
        self.assertTrue(any('(1d ago)' in line for line in lines))
        for expected in ('Main checkout: yes', 'Extension present: no', 'User border: no'):
            self.assertIn(expected, lines)

    def test_execute_snapshot_passes_isolated_environment_and_labels(self):
        with patch('mailbox_tui.herdr', return_value={'snapshot': {'panes': [
                {'terminal_id': 'term-b', 'label': 'user custom border'}]}}):
            result = execute_job({'operation': 'snapshot', 'repo_id': self.repo_id}, self.environment)
        self.assertIn('settings', result)
        agent = next(agent for agent in result['agents'] if agent['terminal_id'] == 'term-b')
        self.assertTrue(agent['user_border'])

    def test_maintain_disabled_and_failure_preserve_committed_result(self):
        original = {'state': 'queued', 'warnings': [{'code': 'existing'}]}
        with patch('ledger_worker.ensure_worker') as ensure:
            self.assertIs(maintain(self.store, original, self.environment), original)
        ensure.assert_not_called()
        with patch('ledger_worker.ensure_worker', side_effect=OSError('worker unavailable')):
            result = maintain(self.store, original, {**self.environment, 'HERDR_AGENT_LEDGER_AUTOSTART': '1'})
        self.assertEqual(result['state'], 'queued')
        self.assertEqual(result['warnings'][0], {'code': 'existing'})
        self.assertTrue(result['warnings'][1]['committed'])

    def test_real_job_owns_process_group_returns_result_and_closes_streams(self):
        job = Job({'operation': 'send', 'repo_id': self.repo_id, 'recipient': self.recipient,
                   'body': 'Real isolated worker', 'kind': 'fyi'}, self.environment)
        self.addCleanup(job.close)
        self.assertEqual(os.getpgid(job.process.pid), job.process.pid)
        job.process.wait(timeout=10)
        done, result = job.poll()
        self.assertTrue(done)
        self.assertEqual(result['state'], 'queued')
        self.assertEqual(self.mail.show_thread(result['thread_id'])['messages'][0]['sender'], 'user')
        job.close()
        job.close()
        self.assertTrue(all(stream.closed for stream in (job.input, job.output, job.errors)))

    def test_job_timeout_kills_group_and_reports_uncertain_durable_state(self):
        process = Mock(pid=123456)
        process.poll.return_value = None
        with patch('mailbox_tui.subprocess.Popen', return_value=process), \
             patch('mailbox_tui.time.monotonic', return_value=0):
            job = Job({'operation': 'snapshot'}, self.environment, timeout=25)
        with patch('mailbox_tui.time.monotonic', return_value=24.99):
            self.assertEqual(job.poll(), (False, None))
        with patch('mailbox_tui.time.monotonic', return_value=25), \
             patch('mailbox_tui.os.killpg') as kill:
            done, result = job.poll()
        self.assertTrue(done)
        self.assertEqual(result['error']['code'], 'ui_timeout')
        self.assertIn('durable state', result['error']['message'])
        self.assertEqual([call.args for call in kill.call_args_list],
                         [(123456, signal.SIGTERM), (123456, signal.SIGKILL)])
        self.assertTrue(all(stream.closed for stream in (job.input, job.output, job.errors)))

    def test_job_invalid_worker_output_is_actionable_error(self):
        process = Mock(pid=123456)
        process.poll.return_value = 1
        with patch('mailbox_tui.subprocess.Popen', return_value=process):
            job = Job({'operation': 'snapshot'}, self.environment)
        job.output.write('not json')
        job.errors.write('worker traceback')
        done, result = job.poll()
        self.assertTrue(done)
        self.assertEqual(result['error']['code'], 'ui_job_failed')
        self.assertEqual(result['error']['message'], 'worker traceback')
        with patch('mailbox_tui.os.killpg'):
            job.close()

    def test_unicode_clipping_and_summary_wrapping_preserve_cells_and_words(self):
        self.assertEqual(clip('한글abc', 3), '한')
        value = '日本語の進捗: 遠隔状態と受信の表示を確認します 🐦 Review transport fixtures.'
        parts = wrap_summary(value, 35)
        self.assertEqual(''.join(value.split()), ''.join(''.join(parts).split()))
        self.assertTrue(all(display_width(part) <= 35 for part in parts))
        for word in ('Review', 'transport', 'fixtures.'):
            self.assertTrue(any(word in part for part in parts))


def resource_render_text(scratch, widths=(40, 160)):
    """Return isolated Agents and paged Detail text; do not run a test case."""
    directory = Path(scratch)
    directory.mkdir(parents=True, exist_ok=True)
    result = {}
    with patch.object(sys.modules[__name__], 'SCRATCH', directory):
        case = MailboxUiTest('test_resource_presence_captures_at_40_and_160_columns')
        try:
            case.setUp()
            for width in widths:
                app = case.resource_app()
                agents = '\n'.join(case.capture(app, height=35, width=width).lines())
                app.focus = 'detail'
                pages = []
                while True:
                    pages.append('\n'.join(case.capture(app, height=35, width=width).lines()))
                    previous = app.scrolls['detail']
                    app.key(curses.KEY_NPAGE)
                    if app.scrolls['detail'] == previous:
                        break
                result[width] = {'agents': agents, 'detail_pages': pages}
        finally:
            case.doCleanups()
    return result


if __name__ == '__main__':
    unittest.main()
