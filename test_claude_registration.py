"""Cold Claude root registration and foreground identity over private real ledgers."""
import ham_test_support  # Protect the live ledger before product imports.
from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock
import uuid

import ledger_cli
from ledger_mail import Mailbox
from ledger_runtime import NativeReceiver
from ledger_status import AgentRegistry, _claude_ancestor_pid, claude_foreground_pid
from ledger_store import SCHEMA_VERSION, LedgerError, LedgerStore
from test_ledger_core import snapshot


ROOT = Path(__file__).resolve().parent
ROOT_PID = 4242


class PrivateClaudeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        helper = Path.home() / '.config/herdr/scripts/agent_work.py'
        if helper.is_file():
            cls.work = Path(subprocess.check_output([
                str(helper), 'path', '--checkout', str(ROOT),
                '--name', 'claude-registration-tests',
            ], text=True).strip())
        else:
            # Like the other private-ledger fixtures, also work on machines
            # without Herdr installed (including an empty sandbox HOME).
            cls.work = Path(os.environ.get('AGENT_WORK_ROOT', Path.home() / '.cache/agent-work')) / 'claude-registration-tests'
            cls.work.mkdir(parents=True, exist_ok=True)

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=self.work)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        environment = {name: str(self.root / name.lower()) for name in (
            'HOME', 'XDG_CONFIG_HOME', 'XDG_STATE_HOME', 'XDG_DATA_HOME',
            'XDG_RUNTIME_DIR', 'HERDR_AGENT_LEDGER_DIR', 'HERDR_AGENT_REQUEST_STATE_DIR',
            'TMPDIR',
        )}
        for value in environment.values():
            Path(value).mkdir()
        environment.update(
            PATH=os.environ.get('PATH', os.defpath),
            HERDR_SOCKET_PATH=str(self.root / 'private.sock'),
            HERDR_AGENT_LEDGER_AUTOSTART='0', HERDR_ENV='1',
            HERDR_PANE_ID='stale-workspace:p99',
        )
        self.environment = environment
        patcher = mock.patch.dict(os.environ, environment, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)


class ClaudeRegistrationTest(PrivateClaudeTest):
    def setUp(self):
        super().setUp()
        foreground = mock.patch('ledger_status.claude_foreground_pid', return_value=ROOT_PID)
        self.foreground = foreground.start()
        self.addCleanup(foreground.stop)
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        self.session = str(uuid.uuid4())
        self.transcript = self.root / 'claude/projects/private-project' / (self.session + '.jsonl')
        self.transcript.parent.mkdir(parents=True)
        self.transcript.touch()
        self.store = LedgerStore(self.root / 'ledger', canonical_name='alpha')
        self.registry = AgentRegistry(self.store)
        self.mail = Mailbox(self.store)
        self.receiver = NativeReceiver(self.store)
        latest = mock.patch('ledger_status.live_snapshot', side_effect=lambda environment=None: deepcopy(self.latest))
        latest.start()
        self.addCleanup(latest.stop)
        self.reconcile()
        self.sender = self.current('term-a')
        self.cold = self.current()

    def reconcile(self, session=None, *, moved=False):
        live = snapshot(self.repo, recipient_session=session, moved=moved)
        live['agents'][1]['agent'] = 'claude'
        if session is None:
            del live['agents'][1]['agent_session']
        self.latest = deepcopy(live)
        self.registry.reconcile(live, refresh_display=False, environment=self.environment)

    def current(self, terminal='term-b'):
        with self.store.read() as connection:
            return dict(connection.execute(
                'SELECT * FROM agents WHERE terminal_id=? AND active=1', (terminal,),
            ).fetchone())

    def send(self):
        return self.mail.send(self.sender, self.current(), kind='fyi', body='Review cold registration. 한글')

    def hook(self, agent=None, **options):
        return self.receiver.claude_hook(self.cold if agent is None else agent, **{
            'hook_event_name': 'SessionStart', 'session_id': self.session,
            'transcript_path': str(self.transcript), 'environment': self.environment, **options,
        })

    def delivery(self, sent):
        with self.store.read() as connection:
            return dict(connection.execute(
                'SELECT * FROM deliveries WHERE message_id=?', (sent['message_id'],),
            ).fetchone())

    def durable_state(self):
        with self.store.read() as connection:
            database = tuple(connection.iterdump())
        bells = {path.name: (path.stat().st_mtime_ns, path.read_bytes())
                 for path in self.store.bell_directory.iterdir()}
        return database, bells

    def write_request_evidence(self, result):
        digest = 'b' * 64
        records = [
            {'type': 'api-request-blob', 'hash': digest, 'message': {
                'role': 'system', 'content': [{'type': 'text', 'text': result['additional_context']}],
            }},
            {'type': 'api-request', 'id': 'real-request-boundary', 'tail': [digest]},
        ]
        with self.transcript.open('a') as stream:
            for record in records:
                stream.write(json.dumps({'sessionId': self.session, **record}) + '\n')

    def test_cold_session_start_registers_real_uuid_and_claims_exact_registered_identity(self):
        self.assertEqual(self.cold['session_ref'], 'unreported')
        self.assertEqual(self.cold['extension_present'], 0)
        self.assertIsNone(self.cold['native_session_pid'])
        sent = self.send()
        result = self.hook()
        registered = self.current()
        self.assertEqual(registered['agent_id'], self.cold['agent_id'])
        self.assertEqual(registered['generation'], self.cold['generation'])
        self.assertEqual(registered['session_ref'], self.session)
        self.assertEqual(registered['extension_present'], 1)
        self.assertEqual(registered['native_session_pid'], ROOT_PID)
        for key in ('agent_id', 'generation', 'session_ref', 'native_session_pid'):
            self.assertEqual(result['agent'][key], registered[key])
        claim = result['claim']
        self.assertIsNotNone(claim)
        self.assertEqual((claim['agent_id'], claim['generation']),
                         (registered['agent_id'], registered['generation']))
        delivery = self.delivery(sent)
        self.assertEqual((delivery['state'], delivery['claimed_agent_id'], delivery['claimed_generation']),
                         ('delivering', registered['agent_id'], registered['generation']))
        with self.store.read() as connection:
            native = dict(connection.execute('SELECT * FROM native_claims').fetchone())
            self.assertEqual(connection.execute('SELECT count(*) FROM wake_log').fetchone()[0], 0)
        self.assertEqual((native['claim_id'], native['session_ref'], native['transcript_path']),
                         (claim['claim_id'], self.session, str(self.transcript)))
        self.write_request_evidence(result)
        self.assertEqual(self.receiver.observe_claude()['observed'], 1)
        delivered = self.delivery(sent)
        self.assertEqual((delivered['state'], delivered['delivered_agent_id'], delivered['confirmation_source']),
                         ('delivered', registered['agent_id'], 'native'))

    def test_same_process_unreported_snapshot_preserves_registered_row_and_pending_marker(self):
        sent = self.send()
        result = self.hook()
        registered = self.current()
        self.reconcile(moved=True)
        interim = self.current()
        for key in ('agent_id', 'generation', 'session_ref', 'extension_present', 'native_session_pid'):
            self.assertEqual(interim[key], registered[key])
        self.assertEqual(interim['pane_id'], 'w2:p9')
        with self.store.read() as connection:
            self.assertEqual(connection.execute(
                "SELECT count(*) FROM agents WHERE terminal_id='term-b'",
            ).fetchone()[0], 1)
        self.assertIsNone(self.hook(interim, hook_event_name='UserPromptSubmit')['claim'])
        self.assertEqual(self.delivery(sent)['claim_id'], result['claim']['claim_id'])
        self.write_request_evidence(result)
        self.assertEqual(self.receiver.observe_claude()['observed'], 1)

    def test_matching_actual_herdr_uuid_clears_pending_marker_without_generation_change(self):
        self.hook()
        registered = self.current()
        self.reconcile(self.session)
        reported = self.current()
        for key in ('agent_id', 'generation', 'session_ref', 'extension_present'):
            self.assertEqual(reported[key], registered[key])
        self.assertIsNone(reported['native_session_pid'])
        self.assertIsNone(self.current('term-a')['native_session_pid'])

    def test_latest_different_herdr_uuid_rejects_stale_cold_hook_without_side_effects(self):
        sent = self.send()
        latest = str(uuid.uuid4())
        self.reconcile(latest)
        current = self.current()
        self.assertNotEqual(current['agent_id'], self.cold['agent_id'])
        self.assertEqual(current['generation'], self.cold['generation'] + 1)
        before = self.durable_state()
        with self.assertRaises(LedgerError) as caught:
            self.hook()
        self.assertEqual(caught.exception.code, 'stale_session')
        self.assertEqual(self.durable_state(), before)
        self.assertEqual(current['session_ref'], latest)
        self.assertEqual(current['extension_present'], 0)
        self.assertIsNone(current['native_session_pid'])
        self.assertEqual(self.delivery(sent)['state'], 'queued')

    def test_fresh_different_uuid_rejects_bootstrap_even_when_database_is_still_unreported(self):
        sent = self.send()
        self.latest['agents'][1]['agent_session'] = {'value': str(uuid.uuid4())}
        before = self.durable_state()
        with self.assertRaises(LedgerError) as caught:
            self.hook()
        self.assertEqual(caught.exception.code, 'stale_session')
        self.assertEqual(self.durable_state(), before)
        self.assertEqual(self.current()['session_ref'], 'unreported')
        self.assertEqual(self.delivery(sent)['state'], 'queued')

    def test_fresh_matching_uuid_allows_cold_registration_without_reconcile(self):
        sent = self.send()
        self.latest['agents'][1]['agent_session'] = {'value': self.session}
        result = self.hook()
        registered = self.current()
        self.assertEqual((registered['agent_id'], registered['generation']),
                         (self.cold['agent_id'], self.cold['generation']))
        self.assertEqual((registered['session_ref'], registered['extension_present'],
                          registered['native_session_pid']), (self.session, 1, ROOT_PID))
        self.assertEqual(self.delivery(sent)['claim_id'], result['claim']['claim_id'])
        self.assertEqual(self.delivery(sent)['claimed_agent_id'], registered['agent_id'])
        self.assertEqual(self.delivery(sent)['claimed_generation'], registered['generation'])

    def test_fresh_missing_or_replaced_current_root_rejects_cold_registration_without_side_effects(self):
        sent = self.send()
        original = deepcopy(self.latest)
        variants = []
        absent = deepcopy(original)
        absent['agents'] = absent['agents'][:1]
        variants.append(absent)
        for field, value in (('agent', 'omp'), ('pane_id', 'other:p9'), ('terminal_id', 'reused-terminal')):
            replaced = deepcopy(original)
            replaced['agents'][1][field] = value
            variants.append(replaced)
        for latest in variants:
            with self.subTest(latest=latest):
                self.latest = latest
                before = self.durable_state()
                with self.assertRaises(LedgerError) as caught:
                    self.hook()
                self.assertEqual(caught.exception.code, 'native_root_unverified')
                self.assertEqual(self.durable_state(), before)
                self.assertEqual(self.delivery(sent)['state'], 'queued')

    def cli_context(self):
        def shell_pid(pane, environment=None):
            return 202 if pane in ('w1:p2', 'w2:p9') else 101

        with mock.patch('ledger_status.pane_shell_pid', side_effect=shell_pid), \
                mock.patch('ledger_status.process_ancestors', return_value={202, ROOT_PID}), \
                mock.patch('ledger_cli.claude_foreground_pid', return_value=self.foreground.return_value):
            return ledger_cli._live_context(self.registry, self.latest, self.environment)

    def test_cli_live_context_accepts_verified_pending_root_using_physical_terminal_ancestry(self):
        self.hook()
        sent = self.send()
        registered = self.current()
        before = self.durable_state()
        context = self.cli_context()
        for key in ('agent_id', 'generation', 'session_ref', 'extension_present', 'native_session_pid'):
            self.assertEqual(context[key], registered[key])
        viewed = self.mail.read(context, message_id=sent['message_id'])['messages'][0]
        self.assertEqual(viewed['state'], 'queued')
        self.assertEqual(self.durable_state(), before)

    def test_cli_live_context_rejects_pending_root_with_different_or_missing_foreground(self):
        self.hook()
        sent = self.send()
        for pid in (ROOT_PID + 1, None):
            with self.subTest(pid=pid):
                self.foreground.return_value = pid
                before = self.durable_state()
                with self.assertRaises(LedgerError) as caught:
                    self.cli_context()
                self.assertEqual(caught.exception.code, 'stale_session')
                self.assertEqual(self.durable_state(), before)
                self.assertEqual(self.delivery(sent)['state'], 'queued')

    def test_cli_live_context_rejects_unreported_snapshot_without_pending_marker(self):
        self.hook()
        self.reconcile(self.session)
        self.assertIsNone(self.current()['native_session_pid'])
        del self.latest['agents'][1]['agent_session']
        sent = self.send()
        before = self.durable_state()
        with self.assertRaises(LedgerError) as caught:
            self.cli_context()
        self.assertEqual(caught.exception.code, 'stale_session')
        self.assertEqual(self.durable_state(), before)
        self.assertEqual(self.delivery(sent)['state'], 'queued')

    def test_cli_live_context_rejects_different_reported_uuid_or_client_despite_pending_marker(self):
        self.hook()
        sent = self.send()
        original = deepcopy(self.latest)
        for override in ({'agent_session': {'value': str(uuid.uuid4())}}, {'agent': 'omp'}):
            with self.subTest(override=override):
                self.latest = deepcopy(original)
                self.latest['agents'][1].update(override)
                before = self.durable_state()
                with self.assertRaises(LedgerError) as caught:
                    self.cli_context()
                self.assertEqual(caught.exception.code, 'stale_session')
                self.assertEqual(self.durable_state(), before)
                self.assertEqual(self.delivery(sent)['state'], 'queued')


    def assert_pending_root_replaced(self, next_pid):
        sent = self.send()
        result = self.hook()
        pending = self.current()
        self.foreground.return_value = next_pid
        before = self.durable_state()
        with self.assertRaises(LedgerError) as caught:
            self.hook(pending)
        self.assertEqual(caught.exception.code, 'native_root_unverified' if next_pid is None else 'stale_session')
        self.assertEqual(self.durable_state(), before)
        self.reconcile()
        replacement = self.current()
        self.assertNotEqual(replacement['agent_id'], pending['agent_id'])
        self.assertEqual(replacement['generation'], pending['generation'] + 1)
        self.assertEqual(replacement['session_ref'], 'unreported')
        self.assertEqual(replacement['extension_present'], 0)
        self.assertIsNone(replacement['native_session_pid'])
        delivery = self.delivery(sent)
        self.assertEqual((delivery['state'], delivery['hold_reason']), ('delivering', 'acceptance_unknown'))
        self.assertEqual((delivery['claim_id'], delivery['claimed_agent_id'], delivery['claimed_generation']),
                         (result['claim']['claim_id'], pending['agent_id'], pending['generation']))
        self.write_request_evidence(result)
        self.assertEqual(self.receiver.observe_claude()['observed'], 0)
        self.assertEqual(self.delivery(sent), delivery)
        with self.store.read() as connection:
            historical = dict(connection.execute(
                'SELECT * FROM agents WHERE agent_id=?', (pending['agent_id'],),
            ).fetchone())
        self.assertEqual(historical['active'], 0)
        self.assertEqual(historical['session_ref'], self.session)

    def test_pending_different_foreground_process_fails_closed_and_holds_old_claim(self):
        self.assert_pending_root_replaced(ROOT_PID + 1)

    def test_pending_unverifiable_foreground_fails_closed_and_holds_old_claim(self):
        self.assert_pending_root_replaced(None)

    def test_stale_expected_generation_cannot_bootstrap_unreported_registration(self):
        self.reconcile(str(uuid.uuid4()))
        self.reconcile()
        replacement = self.current()
        self.assertGreater(replacement['generation'], self.cold['generation'])
        self.send()
        before = self.durable_state()
        with self.assertRaises(LedgerError) as caught:
            self.registry.agent_session(
                terminal_id=self.cold['terminal_id'], session_ref=self.session, client='claude',
                repo_id=replacement['repo_id'], expected_agent_id=replacement['agent_id'],
                expected_generation=self.cold['generation'], environment=self.environment,
            )
        self.assertEqual(caught.exception.code, 'stale_session')
        self.assertEqual(self.durable_state(), before)
        self.assertEqual(self.current()['session_ref'], 'unreported')

    def test_unverified_foreground_cannot_bootstrap_cold_registration(self):
        self.send()
        self.foreground.return_value = None
        before = self.durable_state()
        with self.assertRaises(LedgerError) as caught:
            self.hook()
        self.assertEqual(caught.exception.code, 'native_root_unverified')
        self.assertEqual(self.durable_state(), before)

    def test_nested_and_invalid_root_hooks_have_no_registration_or_delivery_side_effects(self):
        sent = self.send()
        alias = self.transcript.parent / 'alias' / self.transcript.name
        alias.parent.mkdir()
        alias.symlink_to(self.transcript)
        invalid = [
            {'subagent_id': 'agent-nested'},
            {'hook_event_name': 'Stop'},
            {'session_id': 'not-a-uuid'},
            {'session_id': 'AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA'},
            {'transcript_path': 'relative/' + self.transcript.name},
            {'transcript_path': str(self.transcript.with_name('other.jsonl'))},
            {'transcript_path': str(self.transcript.parent / 'subagents' / self.transcript.name)},
            {'transcript_path': str(self.root / 'other' / self.transcript.name)},
            {'transcript_path': str(alias)},
            {'transcript_path': str(self.transcript.parent / 'alias' / '..' / self.transcript.name)},
        ]
        for options in invalid:
            with self.subTest(options=options):
                before = self.durable_state()
                result = self.hook(**options)
                self.assertIsNone(result['claim'])
                self.assertIsNone(result['additional_context'])
                self.assertEqual(self.durable_state(), before)
        self.assertEqual(self.delivery(sent)['state'], 'queued')
        self.assertEqual(self.current()['session_ref'], 'unreported')
        self.assertEqual(self.current()['extension_present'], 0)

    def test_schema4_upgrade_preserves_historical_agents_and_adds_only_null_pending_marker(self):
        with self.store.transaction() as connection:
            connection.execute(
                "UPDATE agents SET session_ref=?,extension_present=1 WHERE terminal_id='term-b'",
                (self.session,),
            )
            connection.execute('ALTER TABLE agents DROP COLUMN native_session_pid')
            connection.execute("UPDATE ledger_meta SET value='4' WHERE key='schema_version'")
            historical = [dict(row) for row in connection.execute('SELECT * FROM agents ORDER BY agent_id')]
        reopened = LedgerStore(self.store.directory, canonical_name='alpha')
        with reopened.read() as connection:
            migrated = [dict(row) for row in connection.execute('SELECT * FROM agents ORDER BY agent_id')]
            version = connection.execute("SELECT value FROM ledger_meta WHERE key='schema_version'").fetchone()[0]
            self.assertEqual(connection.execute('SELECT count(*) FROM native_claims').fetchone()[0], 0)
        self.assertEqual(version, str(SCHEMA_VERSION))
        self.assertEqual(migrated, [{**row, 'native_session_pid': None} for row in historical])
        recipient = next(row for row in migrated if row['terminal_id'] == 'term-b')
        self.assertEqual((recipient['agent_id'], recipient['generation'], recipient['session_ref'],
                          recipient['extension_present']),
                         (self.cold['agent_id'], self.cold['generation'], self.session, 1))


class ClaudeForegroundPidTest(PrivateClaudeTest):
    def setUp(self):
        super().setUp()
        self.agent = {'pane_id': 'w2:p9', 'terminal_id': 'term-b'}
        self.pane = {'ok': True, 'result': {'pane': dict(self.agent)}}
        self.info = {'ok': True, 'result': {'process_info': {
            'pane_id': 'w2:p9', 'foreground_process_group_id': ROOT_PID,
            'foreground_processes': [{
                'pid': ROOT_PID, 'name': 'claude', 'argv': ['/opt/claude/bin/claude'],
                'cwd': str(self.root),
            }],
        }}}
        self.ancestors = {ROOT_PID, ROOT_PID + 100}

    def foreground_pid(self, *, pane=None, info=None, require_ancestor=True, failure=None,
                       process_table=None):
        pane = self.pane if pane is None else pane
        info = self.info if info is None else info

        def run(command, **options):
            if failure is not None:
                raise failure
            payload = info if 'process-info' in command else pane
            if isinstance(payload, subprocess.CompletedProcess):
                return payload
            return subprocess.CompletedProcess(command, 0, 'Herdr notice\n' + json.dumps(payload), '')

        if process_table is None:
            ancestry = mock.patch('ledger_status._claude_ancestor_pid',
                                  return_value=ROOT_PID if ROOT_PID in self.ancestors else None)
        else:
            ancestry = mock.patch('ledger_status.subprocess.run', return_value=subprocess.CompletedProcess(
                ['ps', '-axo', 'pid=,ppid=,comm='], 0, process_table, ''))

        with mock.patch('ledger_status.herdr_executable', return_value='private-herdr'), \
                mock.patch('ledger_status.run_bounded_command', side_effect=run), \
                ancestry, mock.patch('ledger_status.os.getpid', return_value=9000):
            return claude_foreground_pid(self.agent, self.environment, require_ancestor=require_ancestor)

    def test_foreground_ancestor_accepts_observed_process_info_despite_stale_pane_env_hint(self):
        self.assertNotEqual(self.environment['HERDR_PANE_ID'], self.agent['pane_id'])
        self.assertEqual(self.foreground_pid(), ROOT_PID)

    def test_background_pid_outside_ancestors_rejects_even_with_claimed_environment_pid(self):
        self.ancestors = {ROOT_PID + 100}
        self.environment.update(CLAUDE_PID=str(ROOT_PID), HERDR_AGENT_PID=str(ROOT_PID))
        self.assertIsNone(self.foreground_pid())
        # Reconciliation deliberately checks the observed root without hook ancestry.
        self.assertEqual(self.foreground_pid(require_ancestor=False), ROOT_PID)

    def test_foreground_root_accepts_real_nearest_claude_ancestry_through_hook_python(self):
        processes = ('9000 8000 python\n8000 4242 python\n'
                     '4242 3000 claude\n3000 1 driver\n')
        self.assertEqual(self.foreground_pid(process_table=processes), ROOT_PID)

    def test_background_claude_rejects_foreground_claude_grandparent_even_in_ancestry(self):
        processes = ('9000 7000 python\n7000 4242 claude\n'
                     '4242 3000 claude\n3000 1 driver\n')
        # Both Claude PIDs are ancestors, but the hook belongs to the nearest one.
        self.ancestors.add(7000)
        self.assertIsNone(self.foreground_pid(process_table=processes))
        self.assertEqual(self.foreground_pid(process_table=processes, require_ancestor=False), ROOT_PID)

    def test_multiple_foreground_claude_processes_including_nested_agent_reject(self):
        for nested_argv in (['claude'], ['/opt/claude/bin/claude', '--agent', 'reviewer']):
            with self.subTest(argv=nested_argv):
                info = deepcopy(self.info)
                info['result']['process_info']['foreground_processes'].append({
                    'pid': ROOT_PID + 1, 'name': 'claude', 'argv': nested_argv, 'cwd': str(self.root),
                })
                self.ancestors.add(ROOT_PID + 1)
                self.assertIsNone(self.foreground_pid(info=info))
                self.assertIsNone(self.foreground_pid(info=info, require_ancestor=False))

    def test_single_custom_agent_root_remains_supported(self):
        self.info['result']['process_info']['foreground_processes'][0]['argv'] += ['--agent', 'reviewer']
        self.assertEqual(self.foreground_pid(), ROOT_PID)

    def test_claude_executable_argv_identifies_root_when_process_name_is_generic(self):
        self.info['result']['process_info']['foreground_processes'][0]['name'] = 'node'
        self.assertEqual(self.foreground_pid(), ROOT_PID)

    def test_wrong_pane_or_terminal_identity_rejects(self):
        for key, value in (('pane_id', 'w9:p99'), ('terminal_id', 'reused-terminal')):
            with self.subTest(key=key):
                pane = deepcopy(self.pane)
                pane['result']['pane'][key] = value
                self.assertIsNone(self.foreground_pid(pane=pane))
        info = deepcopy(self.info)
        info['result']['process_info']['pane_id'] = 'w9:p99'
        self.assertIsNone(self.foreground_pid(info=info))

    def test_missing_or_malformed_foreground_process_info_rejects(self):
        invalid = [None, [], {}, {'pane_id': 'w2:p9'},
                   {'pane_id': 'w2:p9', 'foreground_process_group_id': ROOT_PID,
                    'foreground_processes': None}]
        valid = self.info['result']['process_info']
        for field, value in (
            ('foreground_process_group_id', None), ('foreground_process_group_id', 0),
            ('foreground_process_group_id', True), ('foreground_process_group_id', '4242'),
            ('foreground_process_group_id', -1),
            ('foreground_processes', []), ('foreground_processes', [None]),
            ('foreground_processes', [{'pid': ROOT_PID, 'name': 'bash', 'argv': ['bash']}]),
        ):
            invalid.append({**valid, field: value})
        for pid in (None, '4242', True, 0, 1, -10):
            process = {**valid['foreground_processes'][0], 'pid': pid}
            invalid.append({**valid, 'foreground_processes': [process]})
        for data in invalid:
            with self.subTest(process_info=data):
                self.assertIsNone(self.foreground_pid(info={'ok': True, 'result': {'process_info': data}}))

    def test_error_and_unreadable_herdr_responses_reject(self):
        bad = [
            {'ok': False, 'result': self.info['result']},
            {'ok': True, 'error': {'message': 'unavailable'}, 'result': self.info['result']},
            {'ok': True, 'result': {}},
            subprocess.CompletedProcess([], 1, json.dumps(self.info), 'failed'),
            subprocess.CompletedProcess([], 0, 'not JSON', ''),
        ]
        for response in bad:
            with self.subTest(response=response):
                self.assertIsNone(self.foreground_pid(info=response))
                self.assertIsNone(self.foreground_pid(pane=response))
        for error in (OSError('private endpoint unavailable'), subprocess.TimeoutExpired('private-herdr', 5)):
            with self.subTest(error=error):
                self.assertIsNone(self.foreground_pid(failure=error))


class ClaudeAncestorPidTest(PrivateClaudeTest):
    def ancestor_pid(self, processes='', *, returncode=0, failure=None):
        result = subprocess.CompletedProcess(
            ['ps', '-axo', 'pid=,ppid=,comm='], returncode, processes, '')
        with mock.patch('ledger_status.os.getpid', return_value=9000), \
                mock.patch('ledger_status.subprocess.run', return_value=result, side_effect=failure):
            return _claude_ancestor_pid()

    def test_native_ancestor_walk_crosses_hook_python_and_returns_foreground_claude(self):
        processes = ('9000 8000 python\n8000 4242 python\n'
                     '4242 3000 claude\n3000 1 driver\n')
        self.assertEqual(self.ancestor_pid(processes), ROOT_PID)

    def test_native_ancestor_walk_returns_nearest_background_claude_not_foreground_parent(self):
        processes = ('9000 7000 python\n7000 4242 claude\n'
                     '4242 3000 claude\n3000 1 driver\n')
        self.assertEqual(self.ancestor_pid(processes), 7000)

    def test_native_ancestor_comm_path_with_spaces_preserves_claude_basename(self):
        processes = ('9000 8000 /usr/bin/python\n8000 4242 /opt/hook python\n'
                     '4242 3000 /opt/Claude Code/claude\n3000 1 driver\n')
        self.assertEqual(self.ancestor_pid(processes), ROOT_PID)

    def test_native_ancestor_unknown_missing_cycle_or_malformed_process_table_rejects(self):
        invalid = [
            '',
            '9000 8000 python\n8000 3000 python\n3000 1 driver\n',
            '9000 8000 python\n4242 3000 claude\n',
            '9000 8000 python\n8000 9000 python\n',
            'not-a-pid 1 claude\n9000 4242 python\n4242 1 claude\n',
            '9000 not-a-parent python\n4242 1 claude\n',
            '9000 4242\n4242 1 claude\n',
        ]
        for processes in invalid:
            with self.subTest(processes=processes):
                self.assertIsNone(self.ancestor_pid(processes))

    def test_native_ancestor_ps_failure_or_timeout_rejects(self):
        processes = '9000 4242 python\n4242 1 claude\n'
        self.assertIsNone(self.ancestor_pid(processes, returncode=1))
        for failure in (OSError('ps unavailable'), subprocess.TimeoutExpired('ps', 5)):
            with self.subTest(failure=failure):
                self.assertIsNone(self.ancestor_pid(processes, failure=failure))


if __name__ == '__main__':
    unittest.main()
