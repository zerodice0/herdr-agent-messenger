"""Consumer-visible quick-send behavior and preserved isolated mailbox services."""
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
from key_input import ESCAPE, F1, NEWLINE, SHIFT_TAB
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
                    app.key('s')
                    app.key('\r')
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

    def test_two_layout_counts_rows_inside_list_borders(self):
        app = self.app()
        app.layout = 'two'
        self.assertEqual(set(app.region_rects(1, 14, 100)), {'agents', 'detail'})
        self.assertEqual(set(app.region_rects(1, 16, 100)), {'agents', 'threads', 'detail'})

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
                        app.edit = {'title': 'Edit mode', 'text': 'manual', 'cursor': 6,
                                    'action': 'setting', 'field': 'mode', 'return_mode': 'settings'}
                        app.mode = 'evidence'
                    snapshot = self.capture(app, height, width)
                    dialog_height, dialog_width = 5, 40
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
                 (24, 80, 'two'), (24, 139, 'two'), (24, 140, 'three'),
                 (9, 160, 'tiny'), (10, 160, 'single'),
                 (17, 160, 'single'), (18, 160, 'three'),
                 (17, 100, 'single'), (18, 100, 'two')]
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
            for height, width in ((9, 31), (10, 32), (17, 79), (18, 80), (24, 139), (24, 140)):
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
        self.assertFalse(any(call.attribute & curses.A_REVERSE for call in snapshot.draw_calls))

    def test_agent_metadata_uses_only_present_parts_and_variable_row_heights(self):
        app = self.app()
        base = app.local['agents'][0]
        metadata = ({}, {'role': 'builder'}, {'phase': 'review', 'summary': 'checking'},
                    {}, {'summary': 'ready'})
        app.local['agents'] = [
            {**base, 'terminal_id': f'term-{index}', 'alias': alias,
             'role': '', 'phase': '', 'summary': '', **fields}
            for index, (alias, fields) in enumerate(zip(
                ('alpha', 'bravo', 'charlie', 'delta', 'echo'), metadata))]
        app.screen = Screen(20, 79)
        app.render_agents((0, 0, 20, 79))
        snapshot = app.screen.snapshot()
        self.assertEqual(snapshot.rows_containing('bravo')[0],
                         snapshot.rows_containing('alpha')[0] + 1)
        self.assertEqual(snapshot.rows_containing('charlie')[0],
                         snapshot.rows_containing('bravo')[0] + 2)
        self.assertEqual(snapshot.rows_containing('echo')[0],
                         snapshot.rows_containing('delta')[0] + 1)
        self.assertEqual(snapshot.line(snapshot.rows_containing('bravo')[0] + 1).strip(), 'builder')
        self.assertEqual(snapshot.line(snapshot.rows_containing('charlie')[0] + 1).strip(),
                         'review · checking')
        self.assertNotIn('— ·', '\n'.join(snapshot.lines()))
        app.selections['agents'] = 3
        app.screen = Screen(6, 79)
        app.render_agents((0, 0, 6, 79))
        snapshot = app.screen.snapshot()
        self.assertEqual(app.scrolls['agents'], 2)
        self.assertEqual(snapshot.rows_containing('delta'), [5])
        self.assertIn('▌', snapshot.line(5))
        self.assertNotIn('echo', '\n'.join(snapshot.lines()))
        app.key(curses.KEY_DOWN)
        app.screen.erase()
        app.render_agents((0, 0, 6, 79))
        snapshot = app.screen.snapshot()
        self.assertEqual(app.selected('agents')['alias'], 'echo')
        self.assertEqual(app.scrolls['agents'], 4)
        self.assertEqual(snapshot.rows_containing('echo'), [4])
        self.assertIn('▌', snapshot.line(4))
        self.assertEqual(snapshot.line(5).strip(), 'ready')
        self.assertEqual(app.screen.out_of_bounds, [])

    def test_no_color_never_initializes_or_uses_pairs_but_renders_layout(self):
        self.environment['NO_COLOR'] = '1'
        app = self.app()
        with patch('mailbox_tui.curses.has_colors', return_value=True), \
             patch('mailbox_tui.curses.start_color') as start, \
             patch('mailbox_tui.curses.init_pair') as initialize, \
             patch('mailbox_tui.curses.color_pair') as pair:
            app.initialize_colors()
            snapshot = self.capture(app, width=140)
        start.assert_not_called()
        initialize.assert_not_called()
        pair.assert_not_called()
        self.assertEqual(app.layout, 'three')
        self.assertIn('Agents', '\n'.join(snapshot.lines()))
        self.assertIn('Threads', '\n'.join(snapshot.lines()))
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

    def test_settings_edit_mutates_real_internal_service_after_confirm(self):
        app = self.app()
        app.key('s')
        app.key('\r')
        self.assertEqual(app.mode, 'evidence')
        app.edit['text'], app.edit['cursor'] = 'manual', 6
        app.key('\r')
        self.accept(app)
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


if __name__ == '__main__':
    unittest.main()
