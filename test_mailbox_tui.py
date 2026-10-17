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
from mailbox_tui import (Job, MailboxApp, OPENER_PANE, OPENER_TERMINAL, REPO_ENV, clip,
                         close_owned, describe_message, dispatch_direct, edit_buffer,
                         execute_job, launch, local_snapshot)


class Screen:
    def __init__(self, height=24, width=100):
        self.height, self.width = height, width
        self.rows = {}

    def getmaxyx(self):
        return self.height, self.width

    def erase(self):
        self.rows = {}

    def addstr(self, row, column, text, attribute=0):
        self.rows[row] = text

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

    def app(self):
        app = MailboxApp(Screen(), self.environment, job_factory=PendingJob, clock=lambda: self.now)
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
        app.local['peers'] = [{'canonical_name': 'offline'}]
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


if __name__ == '__main__':
    unittest.main()
