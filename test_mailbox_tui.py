"""Mailbox UI behavior against real isolated services and interactive key handling."""
import curses
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from agent_directory import AgentRecord, SendResult
from ledger_mail import Mailbox
from ledger_settings import RepoSettings
from ledger_status import AgentRegistry
from ledger_store import LedgerStore
from terminal_text import display_width
from mailbox_tui import (Job, MailboxApp, OPENER_PANE, OPENER_TERMINAL, REPO_ENV, clip,
                         close_owned, describe_message, dispatch_direct, edit_buffer,
                         execute_job, launch, local_snapshot, wrap_summary)


class Screen:
    def __init__(self, height=24, width=100):
        self.height, self.width = height, width
        self.rows = {}
        self.attributes = {}

    def getmaxyx(self):
        return self.height, self.width

    def erase(self):
        self.rows = {}
        self.attributes = {}

    def addstr(self, row, column, text, attribute=0):
        self.rows[row] = text
        self.attributes[row] = attribute

    def refresh(self):
        pass


class PendingJob:
    def __init__(self, request, environment):
        self.request = request
        self.operation = request['operation']
        self.result = None
        self.closed = False

    def poll(self):
        return (self.result is not None, self.result)

    def close(self):
        self.closed = True


class MailboxUiTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.environment = {**os.environ, 'HERDR_AGENT_LEDGER_DIR': str(self.root / 'ledger'),
                            'HERDR_AGENT_LEDGER_AUTOSTART': '0',
                            'HERDR_PLUGIN_STATE_DIR': str(self.root / 'plugin-state')}
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

    def app(self, **options):
        app = MailboxApp(Screen(), self.environment, job_factory=PendingJob,
                         clock=lambda: self.now, **options)
        app.local = local_snapshot(self.store, self.repo_id)
        return app

    def test_user_compose_with_no_opener_agent(self):
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

    def test_korean_composition_requires_review_before_send(self):
        self.now = 0
        app = self.app()
        app.selection = next(index for index, agent in enumerate(app.agents()) if agent['terminal_id'] == 'term-b')
        app.key('c')
        app.key('\t')
        for character in '한글 q123':
            app.key(character)
        self.assertEqual(app.body, '한글 q123')
        app.key('\x0b')
        app.key('\x13')
        self.assertIsNotNone(app.confirm)
        self.assertNotIn('mutation', app.jobs)
        app.key('\x13')
        request = app.jobs['mutation'].request
        self.assertEqual((request['operation'], request['kind'], request['body']), ('send', 'action', '한글 q123'))

    def test_remote_schedule_independent_of_local_bell_and_input(self):
        self.now = 0
        app = self.app()
        app.local['peers'] = [{'canonical_name': 'offline', 'uuid': 'offline-uuid'}]
        app.tick()
        remote = app.jobs['remote:offline']
        app.key('4')
        self.assertEqual(app.tab, 3)
        self.now = 31
        app.tick()
        self.assertIs(app.jobs['remote:offline'], remote)  # no unbounded duplicate workers
        remote.result = {'error': {'message': 'SSH unavailable'}}
        app.tick()
        self.assertIn('offline', app.remote_errors)
        app.key('q')
        self.assertFalse(app.running)
        self.now = 62
        app.tick()
        self.assertIn('remote:offline', app.jobs)

    def test_uncertain_old_generation_requires_evidence(self):
        sent = self.mail.send_user(self.repo_id, self.recipient, body='Old claim')
        claim = self.mail.claim(terminal_id='term-b', agent_id=self.recipient['agent_id'],
                                generation=self.recipient['generation'])
        self.snapshot['agents'][1]['agent_session']['value'] = 'replacement'
        self.registry.reconcile(self.snapshot, refresh_display=False)
        self.now = 0
        app = self.app()
        app.tab = 1
        app.key('a')
        self.assertIn('NOT runtime-journal', app.edit['warning'])
        app.key('\n')
        self.assertIsNotNone(app.edit)
        self.assertNotIn('mutation', app.jobs)
        for character in 'Transcript line 42 checked manually':
            app.key(character)
        app.key('\n')
        request = app.jobs['mutation'].request
        self.assertEqual(request['claim']['generation'], self.recipient['generation'])
        result = execute_job(request, self.environment)
        self.assertEqual(result['receipts'][0]['state'], 'delivered')
        self.assertEqual(result['receipts'][0]['confirmation_source'], 'operator')
        message = self.mail.show_thread(sent['thread_id'])['messages'][0]
        self.assertEqual(message['delivered_agent_id'], claim['agent_id'])
        self.assertIn('USER-CONFIRMED', describe_message(message))

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

    def test_settings_edit_mutates_real_internal_service(self):
        self.now = 0
        app = self.app()
        app.tab = 3
        app.key('\n')
        app.edit['text'] = 'manual'
        app.key('\n')
        request = app.jobs['mutation'].request
        with patch('ledger_display.LedgerDisplay.refresh'), patch('ledger_worker.ensure_worker'):
            execute_job(request, self.environment)
        self.assertEqual(RepoSettings(self.store).show(self.repo_id)['mode'], 'manual')

    def test_setting_result_precedes_next_toggle_and_discards_stale_snapshot(self):
        self.now = 0
        app = self.app()
        app.tab = 3
        app.recipient = self.recipient
        target = {key: self.recipient[key] for key in ("host", "terminal_id")}
        app.key(' ')
        mutation = app.jobs['mutation']
        self.assertIn('Saving settings', app.status)
        app.key(' ')
        self.assertIs(app.jobs['mutation'], mutation)
        self.assertIn('has not been submitted', app.status)
        self.assertNotIn('members', RepoSettings(self.store).show(self.repo_id)['settings'])
        app.start_job('snapshot', 'snapshot')
        stale = app.jobs['snapshot']
        stale.result = json.loads(json.dumps(app.local))
        with patch('ledger_display.LedgerDisplay.refresh',
                   return_value={'updated': [], 'failed': [], 'skipped': []}):
            mutation.result = RepoSettings(self.store).mutate(self.repo_id, 'members', [target])
        self.assertEqual(RepoSettings(self.store).show(self.repo_id)['settings']['members']['value'], [target])
        app.tick()
        self.assertTrue(stale.closed)
        self.assertIsNot(app.jobs['snapshot'], stale)
        self.assertEqual(app.local['settings']['settings']['members']['value'], [target])
        app.key(' ')
        self.assertEqual(app.jobs['mutation'].request['value'], [])
        with patch('ledger_display.LedgerDisplay.refresh',
                   return_value={'updated': [], 'failed': [], 'skipped': []}):
            app.jobs['mutation'].result = RepoSettings(self.store).mutate(
                self.repo_id, 'members', app.jobs['mutation'].request['value'])
        app.tick()
        self.assertEqual(RepoSettings(self.store).show(self.repo_id)['settings']['members']['value'], [])
        self.assertEqual(app.local['settings']['settings']['members']['value'], [])

    def test_conflict_wrapping_reserves_list_prefix_without_losing_text(self):
        self.now = 0
        app = self.app()
        app.tab = 3
        app.screen = Screen(height=80, width=60)
        conflict = {'winner_host': 'z-winner-' * 8, 'loser_host': 'a-loser-' * 8,
                    'winner_value': '"auto"', 'loser_value': '"manual"'}
        app.local['settings']['conflicts'] = [conflict]
        app.render_list(80, 60)
        rows = list(app.screen.rows.values())
        first = next(index for index, text in enumerate(rows) if '! CONFLICT' in text)
        self.assertEqual(''.join(text[2:] for text in rows[first:]),
                         '! CONFLICT ' + json.dumps(conflict, ensure_ascii=False))

    def test_compact_resize_and_unicode_cell_clipping(self):
        self.now = 0
        app = self.app()
        for height, width in [(5, 12), (10, 26), (24, 100)]:
            app.screen = Screen(height, width)
            for tab in range(4):
                app.tab = tab
                app.render()
                self.assertTrue(all(0 <= row < height for row in app.screen.rows))
        self.assertEqual(clip('한글abc', 3), '한')

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
        job.input, job.output, job.errors = (tempfile.TemporaryFile() for _ in range(3))
        try:
            process.wait(timeout=5)
            self.assertTrue(started.exists())
        finally:
            job.close()
        time.sleep(.5)
        self.assertFalse(survived.exists())

    def test_send_result_preserves_newer_composer_edits(self):
        self.now = 0
        app = self.app()
        app.key('c')
        app.key('\t')
        app.key('A')
        app.key('\x13')
        app.key('\x13')
        app.key('B')
        app.jobs['mutation'].result = {'state': 'queued'}
        app.tick()
        self.assertEqual(app.body, 'AB')

    def test_busy_editor_keeps_unsubmitted_evidence(self):
        self.now = 0
        app = self.app()
        app.start_job('mutation', 'setting', field='mode', value='manual')
        app.open_edit('Evidence', 'Checked transcript', 'resolve', claim={'claim_id': 'unused'})
        app.key('\n')
        self.assertEqual(app.edit['text'], 'Checked transcript')
        self.assertEqual(app.jobs['mutation'].operation, 'setting')

    def test_thread_detail_rebinds_to_refreshed_delivery(self):
        sent = self.mail.send_user(self.repo_id, self.recipient, body='Live detail')
        self.now = 0
        app = self.app()
        app.tab = 1
        app.key('\n')
        self.mail.withdraw(sent['message_id'])
        app.start_job('snapshot', 'snapshot')
        app.jobs['snapshot'].result = local_snapshot(self.store, self.repo_id)
        app.tick()
        self.assertEqual(app.detail['messages'][0]['state'], 'withdrawn')

    def test_settings_conflicts_can_scroll_beyond_selected_rows(self):
        self.now = 0
        app = self.app()
        app.tab = 3
        app.screen = Screen(10, 40)
        app.local['settings']['conflicts'] = [{'winner_value': 'x' * 400}]
        for _ in range(4):
            app.key(curses.KEY_NPAGE)
            app.render()
        self.assertGreater(app.scroll, len(app.items()))
        self.assertTrue(any('x' in row for row in app.screen.rows.values()))

    def test_status_summary_uses_indented_lines_at_narrow_and_wide_widths(self):
        self.now = 0
        summary = '한국어 상태 🐦 日本語の進捗 review complete'
        for width in (40, 200):
            with self.subTest(width=width):
                app = self.app()
                agent = {**app.local['agents'][0], 'summary': summary, 'user_border': True,
                         'role': 'worker', 'turn_state': 'working', 'phase': 'review'}
                app.local['agents'] = [agent]
                app.screen = Screen(80, width)
                app.render()
                rows = [text for row, text in sorted(app.screen.rows.items())
                        if 3 <= row < 78 and app.screen.attributes[row] == curses.A_REVERSE]
                self.assertTrue(rows[0].startswith('› '))
                self.assertTrue(all(line.startswith('    ') for line in rows[1:]))
                self.assertNotIn('Summary:', ''.join(rows))
                self.assertEqual(''.join(summary.split()), ''.join(''.join(rows[1:]).split()))
                if width == 40:
                    for column in ('worker', 'working/review', ' 0'):
                        self.assertIn(column, rows[0])
                else:
                    for column in ('worker', 'working/review', 'unread:', 'extension absent',
                                   'user border preserved'):
                        self.assertIn(column, rows[0])
                self.assertTrue(all(display_width(text) <= width - 1 for text in rows))

    def test_status_wrapped_selection_and_scroll_stay_item_based(self):
        self.now = 0
        app = self.app()
        first, second = app.agents()
        app.local['agents'] = [{**first, 'summary': '긴 작업 설명입니다 ' * 20},
                               {**second, 'summary': '次の作業を確認します 🐦'}]
        app.screen = Screen(10, 40)
        app.render()
        app.key(curses.KEY_NPAGE)
        app.render()
        self.assertGreater(app.scroll, 0)
        self.assertEqual(app.selection, 0)
        app.key(curses.KEY_DOWN)
        app.render()
        self.assertEqual(app.selection, 1)
        self.assertEqual(app.selected()['terminal_id'], second['terminal_id'])
        selected_rows = [text[2:] for row, text in app.screen.rows.items()
                         if row >= 3 and app.screen.attributes[row] == curses.A_REVERSE]
        self.assertTrue(selected_rows)
        self.assertIn(second['alias'], ''.join(selected_rows))
        self.assertIn('次の作業を確認します 🐦', ''.join(selected_rows))
        app.key('\n')
        self.assertEqual(app.detail['terminal_id'], second['terminal_id'])
        app.key('\x1b')
        app.key(curses.KEY_UP)
        app.render()
        self.assertEqual(app.selection, 0)
        self.assertEqual(sum(attribute == curses.A_REVERSE for row, attribute in app.screen.attributes.items()
                             if 3 <= row < 8), 4)
        app.screen.height = 14
        app.render()
        self.assertEqual(app.scroll, 0)
        self.assertIn('── ui-test', app.screen.rows[3])

    def test_remote_stale_status_keeps_summary_and_missing_summary_placeholder(self):
        self.now = 0
        app = self.app()
        remote = {**app.local['agents'][0], 'host': 'remote', 'alias': 'remote-worker',
                  'terminal_id': 'remote-terminal', 'summary': '遠隔の長い作業状況 🐦 한국어 상태 ' * 4}
        remote.pop('status_updated_at', None)
        app.local['agents'] = []
        app.local['peers'] = [{'canonical_name': 'remote', 'uuid': 'remote-uuid'}]
        app.remote = {'remote': {'agents': [remote],
                                 'hosts': [{'host': 'remote', 'uuid': 'remote-uuid'}]}}
        app.remote_errors = {'remote': 'SSH unavailable'}
        app.screen = Screen(80, 40)
        app.render()
        rendered = ''.join(text[2:] for row, text in sorted(app.screen.rows.items())
                           if row >= 3 and app.screen.attributes[row] == curses.A_REVERSE)
        self.assertIn('遠隔の長い作業状況', rendered)
        self.assertTrue(rendered.endswith('…'))
        app.remote['remote']['agents'][0]['summary'] = ''
        app.render()
        rendered = ''.join(text[2:] for row, text in sorted(app.screen.rows.items())
                           if row >= 3 and app.screen.attributes[row] == curses.A_REVERSE)
        self.assertTrue(rendered.rstrip().endswith('—'))
        app.key('\n')
        self.assertIn('Last updated: —', app.detail_lines())
        self.assertIn('User border: unknown', app.detail_lines())
        self.assertIn('Host: remote  STALE/offline', app.detail_lines())
        self.assertIn('Unread: ?', app.detail_lines())

    def test_status_detail_has_labeled_fields_and_full_wrapped_summary(self):
        self.now = 0
        summary = '전체 요약을 빠짐없이 확인합니다 🐦\n日本語の詳細も表示します ' * 3
        for width in (40, 200):
            with self.subTest(width=width):
                app = self.app(wall_clock=lambda: 115.5)
                agent = {**app.local['agents'][0], 'summary': summary, 'host': 'remote',
                         'alias': 'remote-worker', 'role': 'worker', 'turn_state': 'working',
                         'phase': 'review', 'extension_present': 1, 'unread': 3,
                         'stale': True, 'user_border': True, 'is_main_checkout': 0,
                         'status_updated_at': 103.5, 'last_seen': 102.5, 'first_seen': 100.5}
                app.local['agents'] = [agent]
                app.screen = Screen(100, width)
                app.key('\n')
                lines = app.detail_lines()
                for expected in (
                    'Address: remote/remote-worker', 'Alias: remote-worker',
                    'Host: remote  STALE/offline', 'Role: worker', 'Client: codex',
                    'Turn state: working', 'Phase: review', 'Summary: ' + summary,
                    'Unread: 3', 'Receiver: ready', 'Extension present: yes',
                    'Repo ID: ' + agent['repo_id'], 'Main checkout: no',
                    'Workspace / pane: ' + agent['workspace_id'] + ' / ' + agent['pane_id'],
                    'Terminal ID: ' + agent['terminal_id'],
                    'Generation: ' + str(agent['generation']),
                    'Session reference: ' + agent['session_ref'],
                    'Last updated: ' + time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(103.5)) + ' (12s ago)',
                    'Last seen: ' + time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(102.5)) + ' (13s ago)',
                    'First seen: ' + time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(100.5)) + ' (15s ago)',
                    'User border: yes',
                ):
                    self.assertIn(expected, lines)
                app.render()
                rows = [text for row, text in sorted(app.screen.rows.items()) if 3 <= row < 98]
                first = next(index for index, text in enumerate(rows) if text.startswith('Summary: '))
                last = next(index for index, text in enumerate(rows) if text.startswith('Unread: '))
                summary_rows = rows[first:last]
                self.assertTrue(all(text.startswith(' ' * 9) for text in summary_rows[1:]))
                displayed = summary_rows[0][9:] + ''.join(text[9:] for text in summary_rows[1:])
                self.assertEqual(''.join(summary.split()), ''.join(displayed.split()))
                self.assertTrue(all(display_width(text) <= width - 1
                                    for row, text in app.screen.rows.items() if 3 <= row < 98))

    def test_status_metadata_is_single_line_and_spaced_words_are_not_split(self):
        self.now = 0
        app = self.app()
        app.local['agents'] = [{**app.local['agents'][0], 'role': 'worker',
                                'summary': 'Review transport fixtures and preserve every status field.'}]
        app.screen = Screen(24, 40)
        app.render()
        rows = [text for row, text in sorted(app.screen.rows.items())
                if row >= 3 and app.screen.attributes[row] == curses.A_REVERSE]
        self.assertEqual(sum(text.startswith('› ') for text in rows), 1)
        self.assertIn('worker', rows[0])
        self.assertEqual(rows[1:], ['    Review transport fixtures and',
                                   '    preserve every status field.'])
        self.assertEqual(wrap_summary('word ' + 'x' * 45 + ' end', 20),
                         ['word', 'x' * 20, 'x' * 20, 'xxxxx end'])
        cjk = '日本語の連続した進捗情報を表示します🐦' * 3
        parts = wrap_summary(cjk, 20)
        self.assertEqual(''.join(parts), cjk)
        self.assertTrue(all(display_width(part) <= 20 for part in parts))

    def test_narrow_host_grouping_preserves_role_phase_and_unread(self):
        self.now = 0
        app = self.app()
        app.local['agents'] = [
            {**agent, 'host': 'status-summary-smoke',
             'alias': alias, 'role': role, 'turn_state': 'idle', 'phase': phase, 'unread': unread}
            for agent, alias, role, phase, unread in
            zip(app.local['agents'], ('alpha-worker', 'beta-reviewer'),
                ('worker', 'main'), ('implement', 'review'), (12, 3))]
        app.screen = Screen(30, 40)
        app.render()
        body = list(app.screen.rows.values())
        self.assertEqual(sum('status-summary-smoke' in row for row in body), 1)
        selected = next(row for row in body if row.startswith('› '))
        self.assertEqual(selected, '› alpha-worker worker idle/implement 12')
        other = next(row for row in body if 'beta-reviewer' in row)
        self.assertEqual(other, '  beta-reviewer main idle/review 3')
        self.assertTrue(all(display_width(row) <= 39 for row in body))

    def test_cjk_fills_lines_without_splitting_latin_words_or_starting_punctuation(self):
        value = '日本語の進捗: 遠隔状態と受信の表示を確認します 🐦 Review transport fixtures.'
        parts = wrap_summary(value, 35)
        self.assertTrue(parts[0].startswith('日本語の進捗: 遠隔状態'))
        self.assertGreaterEqual(display_width(parts[0]), 33)
        self.assertEqual(''.join(value.split()), ''.join(''.join(parts).split()))
        for word in ('Review', 'transport', 'fixtures.'):
            self.assertTrue(any(word in part for part in parts))
        parts = wrap_summary('確認します。日本語（続き）、終了。', 10)
        self.assertTrue(all(not part.startswith(tuple('。、）')) for part in parts))
        self.assertEqual(''.join(parts), '確認します。日本語（続き）、終了。')

    def test_phone_width_keeps_fitting_hangul_words_intact(self):
        self.now = 0
        summary = ('한국어 작업 요약: 상태 보드와 긴 메시지 줄바꿈을 확인하고 다음 작업의 '
                   '선택 위치까지 검토합니다 🐦 끝까지 보존')
        for width in (40, 35, 30):
            with self.subTest(width=width):
                lines = wrap_summary(summary, width)
                self.assertEqual(summary.split(), ' '.join(lines).split())
                self.assertTrue(all(display_width(line) <= width for line in lines))
                jamo = '각각 ㄱㅏㄱㅏ ꥠힰꥠힰ'
                self.assertEqual(jamo.split(), ' '.join(wrap_summary(jamo, width)).split())
        long_word = '가' * 25
        lines = wrap_summary(long_word, 35)
        self.assertEqual(''.join(lines), long_word)
        self.assertEqual(len(lines), 2)
        self.assertTrue(all(display_width(line) <= 35 for line in lines))
        app = self.app()
        app.local['agents'] = [{**app.local['agents'][0], 'summary': summary}]
        app.screen = Screen(100, 40)
        app.render()
        preview = [text[4:] for row, text in sorted(app.screen.rows.items())
                   if row >= 3 and app.screen.attributes[row] == curses.A_REVERSE
                   and text.startswith('    ')]
        words = ' '.join(preview).rstrip('…').split()
        self.assertEqual(words, summary.split()[:len(words)])
        app.key('\n')
        app.render()
        rows = [text for row, text in sorted(app.screen.rows.items()) if 3 <= row < 98]
        start = next(i for i, text in enumerate(rows) if text.startswith('Summary: '))
        end = next(i for i, text in enumerate(rows) if text.startswith('Unread: '))
        self.assertEqual(summary.split(), ' '.join(text[9:] for text in rows[start:end]).split())

    def test_all_agent_detail_fields_have_hanging_indent_and_atomic_age(self):
        self.now = 0
        now = 1700000000
        app = self.app(wall_clock=lambda: now)
        app.detail = {**app.local['agents'][0], 'host': 'status-summary-smoke',
                      'alias': 'alpha-worker', 'summary': 'Review full detail.',
                      'status_updated_at': now - 1, 'last_seen': now - 3}
        app.screen = Screen(100, 40)
        app.render()
        rows = [text for row, text in sorted(app.screen.rows.items()) if 3 <= row < 98]
        field = None
        for text in rows:
            if not text.startswith(' '):
                field = text.partition(': ')[0] + ': '
            else:
                self.assertIsNotNone(field)
                self.assertTrue(text.startswith(' ' * len(field)), text)
        address_end = next(i for i, text in enumerate(rows) if text.startswith('Alias: '))
        address = rows[:address_end]
        self.assertEqual(address, ['Address: status-summary-smoke/', '         alpha-worker'])
        for label, age in (('Last updated: ', '(1s ago)'), ('Last seen: ', '(3s ago)')):
            start = next(i for i, text in enumerate(rows) if text.startswith(label))
            end = start + 1
            while end < len(rows) and rows[end].startswith(' '):
                end += 1
            self.assertTrue(any(age in text for text in rows[start:end]))
            self.assertTrue(all(text.startswith(' ' * len(label)) for text in rows[start + 1:end]))
        self.assertTrue(all(display_width(text) <= 39 for text in rows))

    def test_disconnected_empty_host_keeps_error_in_group_header(self):
        self.now = 0
        app = self.app()
        app.local['agents'] = []
        app.remote_errors = {'remote': 'SSH unavailable'}
        app.screen = Screen(30, 40)
        app.render()
        body = ' '.join(text.strip() for row, text in sorted(app.screen.rows.items())
                        if 3 <= row < 28)
        for field in ('remote', 'STALE/offline', 'SSH unavailable'):
            self.assertIn(field, body)
        self.assertFalse(any(text.startswith('› ') for text in app.screen.rows.values()))

    def test_status_summary_caps_three_lines_but_detail_keeps_full_text(self):
        self.now = 0
        app = self.app()
        summary = 'Review transport fixtures and preserve every status field. ' * 8 + 'UNSEEN-END'
        app.local['agents'] = [{**app.local['agents'][0], 'summary': summary}]
        app.screen = Screen(24, 40)
        app.render()
        rows = [text for row, text in sorted(app.screen.rows.items())
                if row >= 3 and app.screen.attributes[row] == curses.A_REVERSE]
        self.assertEqual(len(rows), 4)
        self.assertEqual(sum(text.startswith('› ') for text in rows), 1)
        self.assertTrue(all(text.startswith('    ') for text in rows[1:]))
        self.assertTrue(rows[-1].endswith('…'))
        self.assertNotIn('UNSEEN-END', ''.join(rows))
        app.key('\n')
        self.assertIn('Summary: ' + summary, app.detail_lines())

    def test_summary_outer_whitespace_does_not_create_false_truncation(self):
        self.now = 0
        app = self.app()
        app.local['agents'] = [{**app.local['agents'][0], 'summary': 'Line1\nLine2\nLine3\n'}]
        app.screen = Screen(24, 200)
        app.render()
        rows = [text for row, text in sorted(app.screen.rows.items())
                if row >= 3 and app.screen.attributes[row] == curses.A_REVERSE]
        self.assertEqual(rows[1:], ['    Line1', '    Line2', '    Line3'])
        app.local['agents'][0]['summary'] = ' \n '
        app.render()
        rows = [text for row, text in sorted(app.screen.rows.items())
                if row >= 3 and app.screen.attributes[row] == curses.A_REVERSE]
        self.assertEqual(rows[1:], ['    —'])
        app.key('\n')
        app.render()
        self.assertIn('Summary: —', app.screen.rows.values())

    def test_detail_times_use_injected_wall_clock_and_boolean_labels(self):
        self.now = 0
        now = 1700000000.25
        app = self.app(wall_clock=lambda: now)
        agent = {**app.local['agents'][0], 'status_updated_at': now - 12,
                 'last_seen': now - 120, 'first_seen': now - 90000,
                 'is_main_checkout': 1, 'extension_present': 0, 'user_border': False}
        app.detail = agent
        app.screen = Screen(40, 100)
        app.render()
        body = '\n'.join(app.screen.rows.values())
        self.assertIn(time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(now - 12)) + ' (12s ago)', body)
        self.assertIn('(2m ago)', body)
        self.assertIn('(1d ago)', body)
        self.assertIn('Main checkout: yes', body)
        self.assertIn('Extension present: no', body)
        self.assertIn('User border: no', body)
        self.assertNotIn(str(now - 12), body)

    def test_non_agent_details_keep_existing_thread_direct_and_outbox_layouts(self):
        self.now = 0
        app = self.app()
        for item in ({'type': 'direct', 'host': 'remote', 'terminal_id': 'term-direct',
                      'acceptance': 'unknown'},
                     {'type': 'outbox', 'host': 'remote', 'terminal_id': 'term-outbox',
                      'state': 'failed'},
                     {'status': 'SSH unavailable', 'connection_errors': {'remote': 'offline'}}):
            app.detail = item
            self.assertEqual(app.detail_lines(), [json.dumps(item, ensure_ascii=False, indent=2)])
        app.detail = {'type': 'thread', 'thread_id': 'thread-id', 'subject': 'Inspect', 'messages': []}
        self.assertEqual(app.detail_lines()[0], 'Thread thread-id  Inspect')

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

    def test_async_remote_arrival_and_peer_removal_preserve_selected_slot(self):
        self.now = 0
        app = self.app()
        app.next_bell = 10
        app.selection = 1
        selected = app.selected()
        app.local['peers'] = [{'canonical_name': 'aaa-peer', 'uuid': 'peer-uuid'}]
        app.remote_due['aaa-peer'] = 30
        remote = {**selected, 'host': 'aaa-peer', 'terminal_id': 'remote-terminal',
                  'agent_id': 'remote-agent', 'summary': 'Remote task'}
        app.start_job('remote:aaa-peer', 'remote', host='aaa-peer')
        app.jobs['remote:aaa-peer'].result = {
            'agents': [remote], 'hosts': [{'host': 'aaa-peer', 'uuid': 'peer-uuid', 'error': None}]}
        app.tick()
        self.assertEqual(app.selection, 2)
        self.assertEqual(app.selected()['terminal_id'], selected['terminal_id'])
        self.assertEqual(app.selected()['host'], selected['host'])
        app.remote_errors['aaa-peer'] = 'SSH unavailable'
        app.start_job('remote:aaa-peer', 'remote', host='aaa-peer')
        pending = app.jobs['remote:aaa-peer']
        app.start_job('snapshot', 'snapshot')
        app.jobs['snapshot'].result = {**app.local, 'peers': []}
        app.tick()
        self.assertTrue(pending.closed)
        self.assertNotIn('remote:aaa-peer', app.jobs)
        self.assertEqual(app.remote, {})
        self.assertEqual(app.remote_errors, {})
        self.assertEqual(app.remote_due, {})
        self.assertEqual(app.selection, 1)
        self.assertEqual(app.selected()['terminal_id'], selected['terminal_id'])


if __name__ == '__main__':
    unittest.main()
