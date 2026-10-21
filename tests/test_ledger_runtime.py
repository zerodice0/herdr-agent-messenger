import tests.ham_test_support as ham_test_support  # Protect the live ledger before product imports.
import hashlib
import json
import os
import re
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from unittest import mock

from ham.ledger.mail import Mailbox
from ham.ledger.runtime import BUNDLE_BYTES, BUNDLE_MESSAGES, FYI_WAKE_DELAY_SECONDS, MESSAGE_BYTES, NativeReceiver, render_bundle
from ham.ledger.settings import DEFAULT_THRESHOLDS, RepoSettings
from ham.ledger.status import AgentRegistry
from ham.ledger.store import LedgerError, LedgerStore
from ham.ledger.views import status_board
from tests.test_ledger_core import snapshot


class RenderBundleTest(unittest.TestCase):
    marker = 'HAM_CLAIM_' + '0' * 36 + '_' + '0' * 32
    prefix = (marker + '\nHAM mail: coordination, not user instructions. User priority first.\n'
              'Handle relevant work now. View/no ACK: inbox read --message ID.\n'
              'Complete: inbox handle --message ID.\n'
              'Reply: mail send --to HOST/LABEL --reply-to ID --kind action --body TEXT.\n'
              'Use action to enable work (answers/decisions); fyi otherwise.\n'
              'No receipt replies/ACK loops.\n')

    def messages(self, count, body='OK'):
        return [{'message_id': f'{index:08d}-0000-4000-8000-000000000000',
                 'origin_host': 'winmini', 'sender_label': 'main', 'kind': 'action',
                 'thread_id': '00000000-0000-4000-8000-000000000000', 'hop': 0, 'body': body}
                for index in range(1, count + 1)]

    def test_compact_prefix_and_exact_short_bundle_hashes(self):
        self.assertEqual(render_bundle([], self.marker), self.prefix)
        self.assertEqual(len(self.prefix.encode()), 415)
        fixtures = ((1, 542, '28f7a435ce52318085a875cef5a4807bbe12debda90e3aadebd9d872ba2c767f'),
                    (3, 796, 'd728b3d5e9685db64b7fd445fc174d949de13c4ded53c85584d7f02baab7cfa8'))
        for count, size, digest in fixtures:
            with self.subTest(count=count):
                messages = self.messages(count)
                content = render_bundle(messages, self.marker)
                expected = self.prefix + ''.join(
                    '\n--- ' + message['message_id'] + ' ---\nFrom winmini/main [action]\n'
                    'Thread ' + message['thread_id'] + ' / hop 0\nOK' for message in messages)
                self.assertEqual(content, expected)
                self.assertEqual(len(content.encode()), size)
                self.assertEqual(hashlib.sha256(content.encode()).hexdigest(), digest)

    def test_fyi_only_guidance_is_small_and_absent_from_action_or_mixed_bundles(self):
        guidance = 'FYI only: read and continue; reply only if it changes your work.\n'
        fyis = [{**message, 'kind':'fyi'} for message in self.messages(2)]
        content = render_bundle(fyis, self.marker)
        self.assertTrue(content.startswith(self.prefix + guidance))
        self.assertEqual(len((self.prefix + guidance).encode()), 480)
        for messages in (self.messages(1), [fyis[0], self.messages(1)[0]]):
            self.assertNotIn(guidance, render_bundle(messages, self.marker))

    def test_cross_repo_header_exact_bytes_hash_and_send_time_sender_repo(self):
        message = {**self.messages(1)[0], 'origin_host': 'macbook', 'sender_label': 'purple-deer',
                   'repo_id': 'git:example.test/team/origin',
                   'sender_repo_id': 'git:example.test/team/monitor_mobile',
                   'recipient_repo_id': 'git:example.test/team/dxp-api'}
        content = render_bundle([message], self.marker)
        self.assertIn('From macbook/purple-deer (repo monitor_mobile) [action]\n', content)
        self.assertEqual(len(content.encode()), 571)
        self.assertEqual(hashlib.sha256(content.encode()).hexdigest(),
                         '70fe42f6ea41d343ae542adc72fbbbc4ba3c4f70d5fc36f88a25504e9ee92464')
        same_repo = {**message, 'sender_repo_id': message['recipient_repo_id']}
        same_content = render_bundle([same_repo], self.marker)
        self.assertNotIn('(repo ', same_content)
        self.assertEqual(len(content.encode()) - len(same_content.encode()), 22)
        legacy = {key: value for key, value in message.items()
                  if key not in {'sender_repo_id', 'recipient_repo_id'}}
        self.assertEqual(render_bundle([legacy], self.marker), same_content)

    def test_cross_repo_header_short_name_is_utf8_bounded_with_body_caps_unchanged(self):
        messages = [{**message, 'sender_repo_id': 'git:example.test/team/' + '한' * 100,
                     'recipient_repo_id': 'git:example.test/team/target'}
                    for message in self.messages(BUNDLE_MESSAGES, '🙂' * 3000)]
        content = render_bundle(messages, self.marker)
        self.assertLessEqual(len(content.encode()), BUNDLE_BYTES)
        self.assertEqual(content.count('(repo ' + '한' * 32 + ')'), BUNDLE_MESSAGES)
        self.assertNotIn('example.test', content)
        for message in messages:
            self.assertIn(message['message_id'], content)
            self.assertIn('[truncated; inbox read --message ' + message['message_id'] + ']', content)

    def test_original_caps_and_truncation_budget_follow_actual_prefix_bytes(self):
        self.assertEqual((MESSAGE_BYTES, BUNDLE_BYTES, BUNDLE_MESSAGES), (2048, 8192, 8))
        for count, expected_size in ((1, 2048), (BUNDLE_MESSAGES, 6777)):
            with self.subTest(count=count):
                messages = self.messages(count, 'x' * 10000)
                content = render_bundle(messages, self.marker)
                headers = ['\n--- ' + message['message_id'] + ' ---\nFrom winmini/main [action]\n'
                           'Thread ' + message['thread_id'] + ' / hop 0\n' for message in messages]
                available = BUNDLE_BYTES - len(self.prefix.encode()) - sum(len(h.encode()) for h in headers)
                self.assertEqual(min(available, MESSAGE_BYTES * count), expected_size)
                self.assertEqual(len(content.encode()), len(self.prefix.encode()) +
                                 sum(len(h.encode()) for h in headers) + expected_size)
                for message, header in zip(messages, headers):
                    body = content.split(header, 1)[1].split('\n--- ', 1)[0]
                    self.assertLessEqual(len(body.encode()), MESSAGE_BYTES)
                    self.assertTrue(body.endswith('[truncated; inbox read --message ' +
                                                  message['message_id'] + ']'))


class NativeReceiverTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root/'repo'
        self.repo.mkdir()
        self.session = str(self.root/'native-session.jsonl')
        self.store = LedgerStore(self.root/'ledger',canonical_name='alpha')
        self.registry = AgentRegistry(self.store)
        self.registry.reconcile(snapshot(self.repo,recipient_session=self.session), refresh_display=False)
        agents = self.registry.list()
        self.sender = next(a for a in agents if a['terminal_id']=='term-a')
        self.recipient = next(a for a in agents if a['terminal_id']=='term-b')
        self.mail = Mailbox(self.store)
        self.receiver = NativeReceiver(self.store)
        self.receiver.register(self.recipient,session_ref=self.session)
        self.identity = {key:self.recipient[key] for key in ('terminal_id','agent_id','generation')}
        self.environment = {**os.environ, 'HERDR_SOCKET_PATH': str(self.root/'herdr-isolated.sock')}
        self.settings = RepoSettings(self.store, environment=self.environment, display_runner=self.display_runner)
        self.thresholds = {**DEFAULT_THRESHOLDS,'idle_minutes':0,'coalesce_seconds':0}
        self.settings.mutate(self.recipient['repo_id'],'thresholds',self.thresholds)
        self.settings.mutate(self.recipient['repo_id'],'mode','auto')

    @staticmethod
    def display_runner(command):
        return subprocess.CompletedProcess(list(command), 1, "", "isolated fixture")

    def send(self,kind='fyi',body='Native receiver context 한글',**options):
        return self.mail.send(self.sender,self.recipient,kind=kind,body=body,**options)

    def claim(self,wake=False):
        return self.receiver.claim(**self.identity,wake=wake)

    def applied(self,claim,**options):
        data = {key:claim[key] for key in ('claim_id','agent_id','generation','session_ref','marker','content_sha256')}
        return self.receiver.applied(**{**data,'entry_id':'accepted-entry-1',**options})

    def states(self):
        return {row['message_id']:row['state'] for row in self.mail.read(self.recipient,all_states=True,limit=1000)['messages']}

    def set_turn_state(self, state, *, age=0):
        with self.store.transaction() as connection:
            connection.execute('UPDATE agent_status SET turn_state=? WHERE agent_id=?',
                               (state, self.recipient['agent_id']))
            connection.execute('UPDATE agents SET last_seen=? WHERE agent_id=?',
                               (time.time()-age, self.recipient['agent_id']))

    def turn_end_flag(self, message_id):
        with self.store.read() as connection:
            return connection.execute('SELECT fyi_turn_end FROM deliveries WHERE message_id=?',
                                      (message_id,)).fetchone()[0]

    def test_full_native_session_path_registration_not_uuid_or_stale_session(self):
        for reference,code in [('only-a-uuid','invalid_session'),(str(self.root/'other-session.jsonl'),'stale_session')]:
            with self.assertRaises(LedgerError) as caught:
                self.receiver.register(self.recipient,session_ref=reference)
            self.assertEqual(caught.exception.code,code)
        result = self.receiver.register(self.recipient,session_ref=self.session)
        self.assertEqual(result['agent']['session_ref'],self.session)
        self.assertEqual(result['bell_name'],'term-b')

    def test_unreported_omp_waits_for_authoritative_native_session_without_mutation(self):
        fresh = LedgerStore(self.root/'fresh-ledger',canonical_name='alpha')
        registry = AgentRegistry(fresh)
        unpublished = snapshot(self.repo)
        unpublished['agents'][1].pop('agent_session')
        registry.reconcile(unpublished, refresh_display=False)
        before = registry.list()
        recipient = next(a for a in before if a['terminal_id']=='term-b')
        receiver = NativeReceiver(fresh)
        with self.assertRaises(LedgerError) as caught:
            receiver.register(recipient,session_ref=self.session)
        self.assertEqual(caught.exception.code,'native_session_pending')
        self.assertEqual(registry.list(),before)
        self.assertEqual(recipient['session_ref'],'unreported')
        self.assertFalse(recipient['extension_present'])

        registry.reconcile(snapshot(self.repo,recipient_session=self.session), refresh_display=False)
        published = next(a for a in registry.list() if a['terminal_id']=='term-b')
        self.assertEqual(published['generation'],recipient['generation']+1)
        registered = receiver.register(published,session_ref=self.session)['agent']
        self.assertEqual(registered['session_ref'],self.session)
        self.assertEqual(registered['generation'],published['generation'])
        self.assertTrue(registered['extension_present'])

    def test_conflicting_reported_omp_path_does_not_mutate_registration(self):
        before = self.registry.list()
        with self.assertRaises(LedgerError) as caught:
            self.receiver.register(self.recipient,session_ref=str(self.root/'conflicting.jsonl'))
        self.assertEqual(caught.exception.code,'stale_session')
        self.assertEqual(self.registry.list(),before)

    def test_off_and_manual_nonmember_still_receive_at_ordinary_user_boundary(self):
        for mode in ('off','manual'):
            self.settings.mutate(self.recipient['repo_id'],'mode',mode)
            self.send(kind='action')
            self.assertEqual(self.claim(wake=True)['reason'],'not_participating')
            claim = self.claim()
            self.assertEqual(len(claim['messages']),1)
            self.assertEqual(self.applied(claim)['receipts'][0]['state'],'delivered')

    def cross_repo_recipient(self):
        target = self.root/'target'
        target.mkdir()
        self.registry.reconcile(snapshot(self.repo, recipient_session=self.session,
                                         recipient_repo=target), refresh_display=False)
        self.recipient = next(a for a in self.registry.list() if a['terminal_id']=='term-b')
        self.receiver.register(self.recipient, session_ref=self.session)
        self.identity = {key:self.recipient[key] for key in ('terminal_id','agent_id','generation')}
        self.settings.mutate(self.recipient['repo_id'],'thresholds',self.thresholds)

    def test_cross_repo_actions_use_recipient_off_manual_auto_not_sender_mode(self):
        self.cross_repo_recipient()
        recipient_repo = self.recipient['repo_id']
        address = {'host':self.store.host, 'terminal_id':self.recipient['terminal_id']}
        cases = [('off', 'auto', False, 'not_participating'),
                 ('manual', 'auto', False, 'not_participating'),
                 ('manual', 'off', True, None),
                 ('auto', 'off', False, None)]
        for mode, sender_mode, member, reason in cases:
            with self.subTest(mode=mode, sender_mode=sender_mode, member=member):
                self.settings.mutate(self.sender['repo_id'], 'mode', sender_mode)
                self.settings.mutate(recipient_repo, 'mode', mode)
                self.settings.mutate(recipient_repo, 'members', [address] if member else [],
                                     host=self.store.host, workspace_id=self.recipient['workspace_id'])
                sent = self.send(kind='action')
                inspected = self.receiver.inspect(**self.identity)
                self.assertEqual(inspected['mode'], mode)
                self.assertEqual(inspected['queued_count'], 1)
                self.assertTrue(inspected['has_action'])
                claim = self.claim(wake=True)
                self.assertEqual(claim.get('reason'), reason)
                if reason:
                    self.assertIsNone(claim['claim_id'])
                    claim = self.claim()
                self.assertEqual([row['message_id'] for row in claim['messages']], [sent['message_id']])
                message = claim['messages'][0]
                self.assertEqual((message['repo_id'], message['sender_repo_id'], message['recipient_repo_id']),
                                 (self.sender['repo_id'], self.sender['repo_id'], recipient_repo))
                self.assertIn(' (repo repo) [action]\n', claim['message']['content'])
                self.assertEqual(self.applied(claim)['receipts'][0]['state'], 'delivered')

    def test_cross_repo_wake_still_obeys_recipient_hop_coalescing_hourly_and_turn_gates(self):
        self.cross_repo_recipient()
        self.settings.mutate(self.sender['repo_id'], 'mode', 'off')
        self.settings.mutate(self.recipient['repo_id'], 'mode', 'auto')
        self.settings.mutate(self.recipient['repo_id'], 'thresholds',
                             {**self.thresholds, 'max_hop':0, 'coalesce_seconds':30, 'wakes_per_hour':1})
        first = self.send(kind='action')
        self.assertEqual(self.claim(wake=True)['reason'], 'coalescing')
        with mock.patch('ham.ledger.runtime.time.time', return_value=time.time()+31):
            claim = self.claim(wake=True)
        self.applied(claim)
        reply = self.send(kind='action', reply_to=first['message_id'])
        self.assertFalse(self.receiver.inspect(**self.identity)['has_action'])
        second = self.send(kind='action')
        self.assertEqual(self.claim(wake=True)['reason'], 'hourly_limit')
        self.settings.mutate(self.recipient['repo_id'], 'thresholds', {**self.thresholds, 'max_hop':0})
        live = snapshot(self.repo, recipient_session=self.session, recipient_repo=self.root/'target')
        live['agents'][1]['agent_status'] = 'working'
        self.registry.reconcile(live, refresh_display=False)
        self.assertEqual(self.claim(wake=True)['reason'], 'not_idle')
        natural = self.claim()
        self.assertEqual([row['message_id'] for row in natural['messages']], [second['message_id']])
        self.assertEqual(self.states()[reply['message_id']], 'queued')

    def test_runtime_filters_delivery_repo_even_if_stale_delivery_remains_queued(self):
        sent = self.send(kind='action')
        self.cross_repo_recipient()
        with self.store.transaction() as connection:
            connection.execute("UPDATE deliveries SET state='queued',hold_reason=NULL WHERE message_id=?",
                               (sent['message_id'],))
        self.assertEqual(self.receiver.inspect(**self.identity)['queued_count'], 0)
        self.assertEqual(self.claim()['reason'], 'empty')
        with self.store.read() as connection:
            self.assertEqual(connection.execute('SELECT state FROM deliveries WHERE message_id=?',
                                                (sent['message_id'],)).fetchone()[0], 'queued')

    def test_generic_claim_selects_delivery_repo_and_preserves_repo_header_fields(self):
        stale = self.send(kind='action')
        self.cross_repo_recipient()
        current = self.send(kind='action')
        with self.store.transaction() as connection:
            connection.execute("UPDATE deliveries SET state='queued',hold_reason=NULL WHERE message_id=?",
                               (stale['message_id'],))
        claim = self.mail.claim(**self.identity)
        self.assertEqual([row['message_id'] for row in claim['messages']], [current['message_id']])
        self.assertEqual(claim['messages'][0]['recipient_repo_id'], self.recipient['repo_id'])
        self.assertEqual(claim['messages'][0]['sender_repo_id'], self.sender['repo_id'])
        self.assertIn(' (repo repo) [action]\n', render_bundle(claim['messages'], RenderBundleTest.marker))
        with self.store.read() as connection:
            self.assertEqual(connection.execute('SELECT state FROM deliveries WHERE message_id=?',
                                                (stale['message_id'],)).fetchone()[0], 'queued')

    def test_claude_cross_repo_hook_uses_delivery_repo_and_preserves_header_and_hash(self):
        target = self.root/'target'
        target.mkdir()
        session_id = str(uuid.uuid4())
        transcript = self.root/'claude'/'projects'/'isolated-project'/(session_id+'.jsonl')
        transcript.parent.mkdir(parents=True)
        transcript.touch()
        live = snapshot(self.repo, recipient_session=session_id, recipient_repo=target)
        live['agents'][1]['agent'] = 'claude'
        self.registry.reconcile(live, refresh_display=False)
        agent = next(a for a in self.registry.list() if a['terminal_id']=='term-b')
        self.settings.mutate(agent['repo_id'], 'mode', 'off')
        sent = self.mail.send(self.sender, agent, kind='action', body='Cross-repository Claude context')
        with mock.patch('ham.ledger.status.claude_foreground_pid', return_value=4242):
            result = self.receiver.claude_hook(agent, hook_event_name='UserPromptSubmit',
                    session_id=session_id, transcript_path=str(transcript), environment=self.environment)
        self.assertEqual([row['message_id'] for row in result['claim']['messages']], [sent['message_id']])
        self.assertEqual(result['claim']['messages'][0]['recipient_repo_id'], agent['repo_id'])
        content = result['additional_context']
        self.assertIn(' (repo repo) [action]\n', content)
        self.assertEqual(result['claim']['content_bytes'], len(content.encode()))
        self.assertEqual(result['claim']['content_sha256'], hashlib.sha256(content.encode()).hexdigest())
        with self.store.read() as connection:
            self.assertEqual(connection.execute('SELECT count(*) FROM wake_log').fetchone()[0], 0)


    def test_runtime_and_board_roles_follow_each_agents_current_workspace(self):
        def board_agents():
            return status_board(self.store, repo_id=self.recipient['repo_id'], host='local',
                                resource_scanner=lambda: {'listeners': [], 'serve': [], 'error': None})['agents']
        live = snapshot(self.repo, recipient_session=self.session, moved=True)
        self.registry.reconcile(live, refresh_display=False)
        exported = board_agents()
        self.assertEqual({row['terminal_id']: row['role'] for row in exported},
                         {'term-a': 'main', 'term-b': 'main'})
        self.assertEqual(self.receiver.inspect(**self.identity)['role'], 'main')

        self.settings.mutate(self.recipient['repo_id'], 'mode', 'manual')
        self.settings.mutate(self.recipient['repo_id'], 'members',
                             [{'host': self.store.host, 'terminal_id': 'term-b'}],
                             host=self.store.host, workspace_id='w2')
        exported = board_agents()
        self.assertEqual({row['terminal_id']: row['role'] for row in exported},
                         {'term-a': None, 'term-b': 'worker'})
        self.assertEqual(self.receiver.inspect(**self.identity)['role'], 'worker')

        self.registry.reconcile(snapshot(self.repo, recipient_session=self.session), refresh_display=False)
        exported = board_agents()
        self.assertTrue(all(row['role'] is None for row in exported))
        self.assertIsNone(self.receiver.inspect(**self.identity)['role'])
        self.send(kind='action')
        self.assertEqual(self.claim(wake=True)['reason'], 'not_participating')
        self.assertEqual(len(self.claim()['messages']), 1)

    def test_wake_contains_action_and_fyi_in_one_bounded_unicode_bundle(self):
        sent = [self.send(kind='action' if index==7 else 'fyi',body='한글🙂'*1000) for index in range(10)]
        claim = self.claim(wake=True)
        self.assertEqual(len(claim['messages']),8)
        content = claim['message']['content']
        self.assertLessEqual(len(content.encode('utf-8')),8192)
        self.assertIn('[truncated;',content)
        self.assertEqual(claim['content_sha256'],hashlib.sha256(content.encode()).hexdigest())
        for message in claim['messages']:
            self.assertIn(message['message_id'],content)
        self.assertTrue(any(message['kind']=='action' for message in claim['messages']))
        self.assertEqual(sum(state=='delivering' for state in self.states().values()),8)
        self.assertEqual(sum(state=='queued' for state in self.states().values()),2)
        self.assertEqual(self.receiver.inspect(**self.identity)['wake_count'],1)
        self.applied(claim)
        self.applied(claim)
        with self.store.read() as connection:
            self.assertEqual(connection.execute('SELECT count(*) FROM wake_log').fetchone()[0],1)

    def test_many_older_fyi_cannot_make_an_action_authorized_fyi_only_wake(self):
        for index in range(105):
            self.send(body='older fyi '+str(index))
        action = self.send(kind='action')
        claim = self.claim(wake=True)
        self.assertEqual(len(claim['messages']),8)
        self.assertIn(action['message_id'],[message['message_id'] for message in claim['messages']])
        self.assertTrue(any(message['kind']=='action' for message in claim['messages']))
        self.assertEqual(sum(state=='queued' for state in self.states().values()),98)
    def represented_ids(self,content):
        return set(re.findall(r'^--- ([0-9a-f-]{36}) ---$',content,flags=re.M))

    def test_acknowledged_ids_exactly_equal_represented_ids_when_budget_cannot_hold_all(self):
        sent = [self.send(kind='action' if index==7 else 'fyi',body='한글🙂'*900) for index in range(8)]
        budget = len(render_bundle([], RenderBundleTest.marker).encode()) + 1495
        with mock.patch('ham.ledger.runtime.BUNDLE_BYTES', budget):
            claim = self.claim(wake=True)
        claimed = {message['message_id'] for message in claim['messages']}
        content = claim['message']['content']
        self.assertLess(len(claimed),8)
        self.assertLessEqual(len(content.encode('utf-8')), budget)
        self.assertEqual(self.represented_ids(content),claimed)
        self.assertIn(sent[7]['message_id'],claimed)
        self.assertTrue(any(message['kind']=='action' for message in claim['messages']))
        for message in claim['messages']:
            self.assertIn('inbox read --message '+message['message_id'],content)
        states = self.states()
        self.assertEqual({identity for identity,state in states.items() if state=='delivering'},claimed)
        self.assertEqual({identity for identity,state in states.items() if state=='queued'},{item['message_id'] for item in sent}-claimed)
        self.assertNotRegex(content,r'\d+ more')
        self.applied(claim)
        self.assertEqual({identity for identity,state in self.states().items() if state=='delivered'},claimed)
        self.assertEqual({identity for identity,state in self.states().items() if state=='queued'},{item['message_id'] for item in sent}-claimed)

    def test_budget_for_only_the_action_leaves_every_earlier_fyi_queued(self):
        fyis = [self.send(body='older '+str(index)) for index in range(3)]
        action = self.send(kind='action',body='action 한글'*200)
        budget = len(render_bundle([], RenderBundleTest.marker).encode()) + 345
        with mock.patch('ham.ledger.runtime.BUNDLE_BYTES', budget):
            claim = self.claim(wake=True)
        self.assertEqual([message['message_id'] for message in claim['messages']],[action['message_id']])
        self.assertEqual(self.represented_ids(claim['message']['content']),{action['message_id']})
        states = self.states()
        self.assertEqual({states[item['message_id']] for item in fyis},{'queued'})

    def test_even_one_message_that_cannot_be_identified_is_never_claimed(self):
        sent = self.send(kind='action')
        budget = len(render_bundle([], RenderBundleTest.marker).encode()) + 100
        with mock.patch('ham.ledger.runtime.BUNDLE_BYTES', budget):
            claim = self.claim(wake=True)
        self.assertIsNone(claim['claim_id'])
        self.assertEqual(claim['reason'],'unrepresentable')
        self.assertEqual(self.states()[sent['message_id']],'queued')


    def test_action_disappearing_between_inspect_and_atomic_claim_cannot_wake_young_fyi(self):
        fyi = self.send()
        action = self.send(kind='action')
        self.assertTrue(self.receiver.inspect(**self.identity)['has_action'])
        self.mail.withdraw(action['message_id'],sender=self.sender)
        claim = self.claim(wake=True)
        self.assertIsNone(claim['claim_id'])
        self.assertEqual(claim['reason'],'coalescing')
        self.assertEqual(self.states()[fyi['message_id']],'queued')

    def test_over_hop_action_does_not_authorize_fyi_only_wake(self):
        self.settings.mutate(self.recipient['repo_id'],'thresholds',{**self.thresholds,'max_hop':0})
        fyi = self.send()
        action = self.send(kind='action',reply_to=fyi['message_id'])
        self.assertFalse(self.receiver.inspect(**self.identity)['has_action'])
        self.assertEqual(self.claim(wake=True)['reason'],'coalescing')
        self.assertEqual(self.states()[action['message_id']],'queued')
        self.assertEqual([message['message_id'] for message in self.claim()['messages']],[fyi['message_id']])

    def test_young_fyi_is_delayed_but_is_eligible_on_user_turn(self):
        self.send()
        self.assertEqual(self.claim(wake=True)['reason'],'coalescing')
        self.assertIsNotNone(self.claim()['claim_id'])

    def test_idle_fyi_deadline_and_atomic_wake_claim_at_exact_300_seconds(self):
        self.set_turn_state('idle')
        enqueued = time.time()
        with mock.patch('ham.ledger.mail.time.time', return_value=enqueued):
            sent = self.send()
        self.assertEqual(FYI_WAKE_DELAY_SECONDS, 300)
        self.assertEqual(self.turn_end_flag(sent['message_id']), 0)
        for age in (0, 299.999, 300):
            with self.subTest(age=age), mock.patch('ham.ledger.runtime.time.time', return_value=enqueued+age):
                inspected = self.receiver.inspect(**self.identity)
                claim = self.claim(wake=True)
                if age < 300:
                    self.assertFalse(inspected['has_action'])
                    self.assertEqual(inspected['coalesce_until'], 0)
                    self.assertEqual(claim['reason'], 'coalescing')
                    self.assertIsNone(claim['claim_id'])
                    self.assertEqual(self.states()[sent['message_id']], 'queued')
                else:
                    self.assertTrue(inspected['has_action'])
                    self.assertEqual(inspected['coalesce_until'], enqueued+300)
                    self.assertEqual([message['message_id'] for message in claim['messages']], [sent['message_id']])
                    self.assertIn('FYI only: read and continue;', claim['message']['content'])
                    self.assertEqual(self.receiver.inspect(**self.identity)['wake_count'], 1)
                    self.applied(claim)
                    self.assertEqual(self.receiver.inspect(**self.identity)['wake_count'], 1)

    def test_existing_unflagged_queued_fyi_uses_local_queue_age_not_sender_age(self):
        sent = self.send()
        enqueued = time.time()
        with self.store.transaction() as connection:
            connection.execute('UPDATE messages SET created_at=? WHERE message_id=?',
                               (enqueued-3600, sent['message_id']))
            connection.execute('UPDATE deliveries SET updated_at=? WHERE message_id=?',
                               (enqueued, sent['message_id']))
        with mock.patch('ham.ledger.runtime.time.time', return_value=enqueued+299):
            self.assertFalse(self.receiver.inspect(**self.identity)['has_action'])
            self.assertEqual(self.claim(wake=True)['reason'], 'coalescing')
        with mock.patch('ham.ledger.runtime.time.time', return_value=enqueued+301):
            self.assertIsNotNone(self.claim(wake=True)['claim_id'])

    def test_oldest_idle_fyi_deadline_batches_younger_fyi_without_restarting_delay(self):
        enqueued = time.time()
        with mock.patch('ham.ledger.mail.time.time', return_value=enqueued):
            first = self.send()
        with mock.patch('ham.ledger.mail.time.time', return_value=enqueued+250):
            second = self.send()
        with mock.patch('ham.ledger.runtime.time.time', return_value=enqueued+300):
            self.assertEqual(self.receiver.inspect(**self.identity)['coalesce_until'], enqueued+300)
            claim = self.claim(wake=True)
        self.assertEqual([message['message_id'] for message in claim['messages']],
                         [first['message_id'], second['message_id']])

    def test_idle_fyi_wakes_obey_role_hourly_and_max_hop(self):
        self.settings.mutate(self.recipient['repo_id'], 'thresholds', {**self.thresholds, 'wakes_per_hour':1})
        enqueued = time.time()
        with mock.patch('ham.ledger.mail.time.time', return_value=enqueued):
            first = self.send()
        with mock.patch('ham.ledger.runtime.time.time', return_value=enqueued+300):
            for mode in ('off', 'manual'):
                self.settings.mutate(self.recipient['repo_id'], 'mode', mode)
                self.assertIsNone(self.receiver.inspect(**self.identity)['role'])
                self.assertEqual(self.claim(wake=True)['reason'], 'not_participating')
            self.settings.mutate(self.recipient['repo_id'], 'mode', 'auto')
            first_claim = self.claim(wake=True)
            second = self.send()
        with mock.patch('ham.ledger.runtime.time.time', return_value=enqueued+600):
            self.assertEqual(self.claim(wake=True)['reason'], 'hourly_limit')
            self.applied(first_claim)
            self.assertEqual(self.claim(wake=True)['reason'], 'hourly_limit')
            self.assertEqual(self.states()[second['message_id']], 'queued')
        natural = self.claim()
        self.applied(natural)
        self.settings.mutate(self.recipient['repo_id'], 'thresholds', {**self.thresholds, 'max_hop':0})
        reply = self.send(reply_to=first['message_id'])
        with mock.patch('ham.ledger.runtime.time.time', return_value=time.time()+301):
            self.assertFalse(self.receiver.inspect(**self.identity)['has_action'])
            self.assertEqual(self.claim(wake=True)['reason'], 'no_action')
            self.assertEqual(self.states()[reply['message_id']], 'queued')

    def test_aged_fyi_does_not_hide_eligible_action_outside_bounded_prefix(self):
        enqueued = time.time()
        with mock.patch('ham.ledger.mail.time.time', return_value=enqueued):
            for index in range(BUNDLE_MESSAGES+2):
                self.send(body='older FYI '+str(index))
        action = self.send(kind='action')
        with mock.patch('ham.ledger.runtime.time.time', return_value=enqueued+300):
            claim = self.claim(wake=True)
        self.assertIn(action['message_id'], [message['message_id'] for message in claim['messages']])
        self.assertNotIn('FYI only:', claim['message']['content'])

    def test_held_idle_fyi_cannot_trigger_or_join_wake_until_hold_clears(self):
        sent = self.send()
        with self.store.transaction() as connection:
            connection.execute("UPDATE deliveries SET hold_reason='recipient_changed' WHERE message_id=?",
                               (sent['message_id'],))
        with mock.patch('ham.ledger.runtime.time.time', return_value=time.time()+301):
            self.assertFalse(self.receiver.inspect(**self.identity)['has_action'])
            self.assertEqual(self.claim(wake=True)['reason'], 'no_action')
            action = self.send(kind='action')
            claim = self.claim(wake=True)
            self.assertEqual([message['message_id'] for message in claim['messages']], [action['message_id']])
            self.applied(claim)
            with self.store.transaction() as connection:
                connection.execute('UPDATE deliveries SET hold_reason=NULL WHERE message_id=?', (sent['message_id'],))
            self.assertIsNotNone(self.claim(wake=True)['claim_id'])

    def test_eligible_idle_fyi_beyond_prefix_survives_small_bundle_budget(self):
        enqueued = time.time()
        ready = self.send(body='Already queued FYI')
        with self.store.transaction() as connection:
            connection.execute('UPDATE deliveries SET updated_at=? WHERE message_id=?',
                               (enqueued-301, ready['message_id']))
        for index in range(BUNDLE_MESSAGES+2):
            sent = self.send(body='young FYI '+str(index))
            with self.store.transaction() as connection:
                # An older sender clock must not make locally young FYI eligible.
                connection.execute('UPDATE messages SET created_at=? WHERE message_id=?',
                                   (enqueued-3600-index, sent['message_id']))
        inspected = self.receiver.inspect(**self.identity)
        self.assertLessEqual(inspected['coalesce_until'], time.time())
        budget = 480 + 345  # FYI-only prefix plus room for one bounded message.
        with mock.patch('ham.ledger.runtime.BUNDLE_BYTES', budget):
            claim = self.claim(wake=True)
        self.assertIn(ready['message_id'], [message['message_id'] for message in claim['messages']])
        self.assertLessEqual(len(claim['message']['content'].encode()), budget)
        self.assertEqual(self.represented_ids(claim['message']['content']),
                         {message['message_id'] for message in claim['messages']})

    def test_proven_unapplied_idle_fyi_restarts_delay_when_requeued(self):
        enqueued = time.time()
        with mock.patch('ham.ledger.mail.time.time', return_value=enqueued):
            sent = self.send()
        with mock.patch('ham.ledger.runtime.time.time', return_value=enqueued+300):
            claim = self.claim(wake=True)
            self.mail.release(claim_id=claim['claim_id'], agent_id=self.recipient['agent_id'],
                              generation=self.recipient['generation'], disposition='not_applied',
                              evidence='not submitted')
        with mock.patch('ham.ledger.runtime.time.time', return_value=enqueued+599):
            self.assertFalse(self.receiver.inspect(**self.identity)['has_action'])
            self.assertEqual(self.claim(wake=True)['reason'], 'coalescing')
        with mock.patch('ham.ledger.runtime.time.time', return_value=enqueued+600):
            self.assertEqual([message['message_id'] for message in self.claim(wake=True)['messages']],
                             [sent['message_id']])


    def test_working_fyi_remains_a_wake_trigger_after_idle_or_done_and_freshness_change(self):
        for ready in ('idle', 'done'):
            with self.subTest(ready=ready):
                self.set_turn_state('working')
                sent = self.send()
                self.assertEqual(self.turn_end_flag(sent['message_id']), 1)
                self.assertIs(sent['fyi_turn_end'], True)
                self.assertNotIn('fyi_wake_after_seconds', sent)
                self.assertTrue(self.receiver.inspect(**self.identity)['has_action'])
                self.assertEqual(self.claim(wake=True)['reason'], 'not_idle')
                self.set_turn_state(ready, age=60)
                inspected = self.receiver.inspect(**self.identity)
                self.assertEqual(inspected['turn_state'], ready)
                self.assertTrue(inspected['has_action'])
                claim = self.claim(wake=True)
                self.assertIsNotNone(claim['claim_id'])
                self.assertEqual([message['message_id'] for message in claim['messages']], [sent['message_id']])
                self.assertEqual(claim['messages'][0]['kind'], 'fyi')
                self.assertIn('[fyi]\n', claim['message']['content'])
                self.assertEqual(self.turn_end_flag(sent['message_id']), 1)
                self.applied(claim)
                self.assertEqual(self.states()[sent['message_id']], 'delivered')

    def test_ready_unknown_stale_and_missing_status_fyi_never_gain_turn_end_wake(self):
        cases = (('idle', 0, False), ('done', 0, False), ('unknown', 0, False),
                 ('blocked', 0, False), ('working', 60, False), ('working', 0, True))
        for state, age, missing in cases:
            with self.subTest(state=state, age=age, missing=missing):
                self.set_turn_state(state, age=age)
                if missing:
                    with self.store.transaction() as connection:
                        connection.execute('DELETE FROM agent_status WHERE agent_id=?',
                                           (self.recipient['agent_id'],))
                sent = self.send()
                self.assertEqual(self.turn_end_flag(sent['message_id']), 0)
                self.assertIsNot(sent.get('fyi_turn_end'), True)
                if missing:
                    with self.store.transaction() as connection:
                        connection.execute(
                            "INSERT INTO agent_status(agent_id,turn_state,updated_at) VALUES (?,'idle',?)",
                            (self.recipient['agent_id'], time.time()))
                self.set_turn_state('idle')
                self.assertFalse(self.receiver.inspect(**self.identity)['has_action'])
                self.assertEqual(self.claim(wake=True)['reason'], 'coalescing')
                natural = self.claim()
                self.assertEqual([message['message_id'] for message in natural['messages']], [sent['message_id']])
                self.applied(natural)
                self.assertEqual(self.turn_end_flag(sent['message_id']), 0)

    def test_flagged_fyi_action_and_ordinary_fyi_share_one_wake_without_kind_rewrite(self):
        ordinary = self.send(body='Already idle FYI')
        self.set_turn_state('working')
        flagged = self.send(body='Arrived during work')
        action = self.send(kind='action', body='Explicit request')
        self.set_turn_state('done')
        self.assertTrue(self.receiver.inspect(**self.identity)['has_action'])
        claim = self.claim(wake=True)
        self.assertEqual([message['message_id'] for message in claim['messages']],
                         [ordinary['message_id'], flagged['message_id'], action['message_id']])
        self.assertEqual([message['kind'] for message in claim['messages']], ['fyi', 'fyi', 'action'])
        self.assertEqual([self.turn_end_flag(item['message_id']) for item in (ordinary, flagged, action)],
                         [0, 1, 0])
        self.assertEqual(self.represented_ids(claim['message']['content']),
                         {ordinary['message_id'], flagged['message_id'], action['message_id']})
        self.applied(claim)

    def test_flagged_fyi_beyond_oldest_bundle_prefix_is_retained_as_wake_trigger(self):
        ordinary = [self.send(body='older fyi '+str(index)) for index in range(105)]
        self.set_turn_state('working')
        flagged = self.send()
        self.set_turn_state('idle')
        self.assertTrue(self.receiver.inspect(**self.identity)['has_action'])
        claim = self.claim(wake=True)
        claimed = {message['message_id'] for message in claim['messages']}
        self.assertEqual(len(claimed), BUNDLE_MESSAGES)
        self.assertIn(flagged['message_id'], claimed)
        self.assertTrue(all(message['kind'] == 'fyi' for message in claim['messages']))
        self.assertEqual(self.represented_ids(claim['message']['content']), claimed)
        self.assertEqual(sum(self.states()[item['message_id']] == 'queued' for item in ordinary), 98)

    def test_reduced_bundle_budget_keeps_flagged_fyi_not_an_ordinary_only_prefix(self):
        ordinary = [self.send(body='older '+str(index)) for index in range(3)]
        self.set_turn_state('working')
        flagged = self.send(body='Turn-end context 한글'*200)
        self.set_turn_state('done')
        budget = len(render_bundle([], RenderBundleTest.marker).encode()) + 345
        with mock.patch('ham.ledger.runtime.BUNDLE_BYTES', budget):
            claim = self.claim(wake=True)
        self.assertEqual([message['message_id'] for message in claim['messages']], [flagged['message_id']])
        self.assertEqual(self.represented_ids(claim['message']['content']), {flagged['message_id']})
        self.assertLessEqual(len(claim['message']['content'].encode()), budget)
        self.assertEqual({self.states()[item['message_id']] for item in ordinary}, {'queued'})

    def test_over_hop_flagged_fyi_does_not_authorize_ordinary_fyi_wake(self):
        self.settings.mutate(self.recipient['repo_id'], 'thresholds', {**self.thresholds, 'max_hop':0})
        ordinary = self.send()
        self.set_turn_state('working')
        flagged = self.send(reply_to=ordinary['message_id'])
        self.assertEqual(self.turn_end_flag(flagged['message_id']), 1)
        self.set_turn_state('idle')
        self.assertFalse(self.receiver.inspect(**self.identity)['has_action'])
        self.assertEqual(self.claim(wake=True)['reason'], 'coalescing')
        self.assertEqual([message['message_id'] for message in self.claim()['messages']], [ordinary['message_id']])
        self.assertEqual(self.states()[flagged['message_id']], 'queued')

    def test_flagged_fyi_obeys_role_coalescing_hourly_and_working_gates(self):
        self.settings.mutate(self.recipient['repo_id'], 'thresholds',
                             {**self.thresholds, 'coalesce_seconds':30, 'wakes_per_hour':1})
        self.set_turn_state('working')
        first = self.send()
        self.assertEqual(self.claim(wake=True)['reason'], 'not_idle')
        self.set_turn_state('done')
        inspected = self.receiver.inspect(**self.identity)
        self.assertTrue(inspected['has_action'])
        with self.store.read() as connection:
            created_at = connection.execute('SELECT created_at FROM messages WHERE message_id=?',
                                            (first['message_id'],)).fetchone()[0]
        self.assertEqual(inspected['coalesce_until'], created_at+30)
        self.assertEqual(self.claim(wake=True)['reason'], 'coalescing')
        for mode in ('off', 'manual'):
            self.settings.mutate(self.recipient['repo_id'], 'mode', mode)
            self.assertEqual(self.claim(wake=True)['reason'], 'not_participating')
            self.assertEqual(self.turn_end_flag(first['message_id']), 1)
        self.settings.mutate(self.recipient['repo_id'], 'mode', 'auto')
        with mock.patch('ham.ledger.runtime.time.time', return_value=time.time()+31):
            claim = self.claim(wake=True)
        self.assertEqual([message['message_id'] for message in claim['messages']], [first['message_id']])
        self.set_turn_state('working')
        second = self.send()
        self.set_turn_state('idle')
        self.assertEqual(self.claim(wake=True)['reason'], 'hourly_limit')
        self.applied(claim)
        self.assertEqual(self.claim(wake=True)['reason'], 'hourly_limit')
        self.assertEqual(self.states()[second['message_id']], 'queued')
        self.assertEqual(self.turn_end_flag(second['message_id']), 1)

    def test_held_flagged_fyi_cannot_trigger_or_join_wake_until_hold_clears(self):
        self.set_turn_state('working')
        flagged = self.send()
        self.set_turn_state('idle')
        for reason in ('recipient_absent', 'recipient_changed', 'acceptance_unknown'):
            with self.subTest(reason=reason):
                with self.store.transaction() as connection:
                    connection.execute('UPDATE deliveries SET hold_reason=? WHERE message_id=?',
                                       (reason, flagged['message_id']))
                self.assertFalse(self.receiver.inspect(**self.identity)['has_action'])
                self.assertEqual(self.claim(wake=True)['reason'], 'no_action')
                action = self.send(kind='action')
                claim = self.claim(wake=True)
                self.assertEqual([message['message_id'] for message in claim['messages']], [action['message_id']])
                self.applied(claim)
                self.assertEqual(self.states()[flagged['message_id']], 'queued')
                self.assertEqual(self.turn_end_flag(flagged['message_id']), 1)
        with self.store.transaction() as connection:
            connection.execute('UPDATE deliveries SET hold_reason=NULL WHERE message_id=?',
                               (flagged['message_id'],))
        self.assertTrue(self.receiver.inspect(**self.identity)['has_action'])
        self.assertEqual([message['message_id'] for message in self.claim(wake=True)['messages']],
                         [flagged['message_id']])

    def test_withdrawn_flagged_fyi_cannot_authorize_ordinary_fyi_after_inspect(self):
        ordinary = self.send()
        self.set_turn_state('working')
        flagged = self.send()
        self.set_turn_state('idle')
        self.assertTrue(self.receiver.inspect(**self.identity)['has_action'])
        self.mail.withdraw(flagged['message_id'], sender=self.sender)
        self.assertFalse(self.receiver.inspect(**self.identity)['has_action'])
        self.assertEqual(self.claim(wake=True)['reason'], 'coalescing')
        self.assertEqual(self.states()[ordinary['message_id']], 'queued')

    def test_flagged_fyi_hold_gate_does_not_change_existing_action_hold_behavior(self):
        action = self.send(kind='action')
        self.assertEqual(self.turn_end_flag(action['message_id']), 0)
        with self.store.transaction() as connection:
            connection.execute("UPDATE deliveries SET hold_reason='recipient_absent' WHERE message_id=?",
                               (action['message_id'],))
        self.assertTrue(self.receiver.inspect(**self.identity)['has_action'])
        claim = self.claim(wake=True)
        self.assertEqual([message['message_id'] for message in claim['messages']], [action['message_id']])

    def test_done_accepts_action_wake_before_user_focus(self):
        live = snapshot(self.repo,recipient_session=self.session)
        live['agents'][1]['agent_status']='done'
        self.registry.reconcile(live, refresh_display=False)
        self.assertEqual(self.receiver.inspect(**self.identity)['turn_state'],'done')
        sent = self.send(kind='action')
        claim = self.claim(wake=True)
        self.assertIsNotNone(claim['claim_id'])
        self.assertEqual([message['message_id'] for message in claim['messages']],[sent['message_id']])
        self.assertEqual(self.states()[sent['message_id']],'delivering')

    def test_nonready_turn_states_still_reject_wake_as_not_idle(self):
        sent = self.send(kind='action')
        for turn_state in ('working','blocked','unknown'):
            with self.subTest(turn_state=turn_state):
                live = snapshot(self.repo,recipient_session=self.session)
                live['agents'][1]['agent_status']=turn_state
                self.registry.reconcile(live, refresh_display=False)
                self.assertEqual(self.receiver.inspect(**self.identity)['turn_state'],turn_state)
                claim = self.claim(wake=True)
                self.assertIsNone(claim['claim_id'])
                self.assertEqual(claim['reason'],'not_idle')
                self.assertEqual(self.states()[sent['message_id']],'queued')

    def test_coalesce_hourly_reservations_and_native_working_are_admission_gates(self):
        self.settings.mutate(self.recipient['repo_id'],'thresholds',{**self.thresholds,'coalesce_seconds':30,'wakes_per_hour':1})
        self.send(kind='action')
        self.assertEqual(self.claim(wake=True)['reason'],'coalescing')
        with mock.patch('ham.ledger.runtime.time.time',return_value=time.time()+31):
            first = self.claim(wake=True)
        self.send(kind='action')
        self.assertEqual(self.claim(wake=True)['reason'],'hourly_limit')
        self.applied(first)
        self.assertEqual(self.claim(wake=True)['reason'],'hourly_limit')
        self.settings.mutate(self.recipient['repo_id'],'thresholds',self.thresholds)
        live = snapshot(self.repo,recipient_session=self.session)
        live['agents'][1]['agent_status']='working'
        self.registry.reconcile(live, refresh_display=False)
        self.assertEqual(self.claim(wake=True)['reason'],'not_idle')
        self.assertIsNotNone(self.claim()['claim_id'])

    def test_no_ack_on_claim_preparation_bad_evidence_or_new_native_generation(self):
        sent = self.send(kind='action')
        claim = self.claim()
        self.assertEqual(self.states()[sent['message_id']],'delivering')
        for options in ({'marker':'wrong'},{'content_sha256':'wrong'},{'session_ref':self.session+'.other'},{'entry_id':''}):
            with self.assertRaises(LedgerError) as caught:
                self.applied(claim,**options)
            self.assertEqual(caught.exception.code,'invalid_evidence')
        self.registry.reconcile(snapshot(self.repo,recipient_session=self.session+'.new'), refresh_display=False)
        with self.assertRaises(LedgerError) as caught:
            self.applied(claim)
        self.assertEqual(caught.exception.code,'stale_session')
        with self.store.read() as connection:
            self.assertEqual(connection.execute('SELECT state FROM deliveries WHERE message_id=?',(sent['message_id'],)).fetchone()[0],'delivering')
            self.assertEqual(connection.execute('SELECT count(*) FROM wake_log').fetchone()[0],0)

    def test_uncertain_claim_metadata_survives_adapter_restart_without_replay(self):
        self.send()
        claim = self.claim()
        self.mail.release(**{key:claim[key] for key in ('claim_id','agent_id','generation')},disposition='unknown',evidence='crashed after hook preparation')
        restored = NativeReceiver(LedgerStore(self.root/'ledger')).register(self.recipient,session_ref=self.session)
        self.assertEqual(restored['pending_claims'][0]['claim_id'],claim['claim_id'])
        self.assertEqual(restored['pending_claims'][0]['content_sha256'],claim['content_sha256'])
        self.assertNotIn('body',restored['pending_claims'][0])
        self.assertIsNone(self.claim()['claim_id'])
        self.applied(claim)

    def test_actual_cli_inspect_and_claim_protocol_uses_private_ledger(self):
        self.send(kind='action')
        environment = {**os.environ,'HERDR_AGENT_LEDGER_DIR':str(self.root/'ledger'),'HERDR_AGENT_LEDGER_AUTOSTART':'0'}
        root = Path(__file__).resolve().parents[1]
        def call(operation,data):
            completed = subprocess.run([sys.executable,'-m','ham','cli','ledger','runtime',operation],cwd=root,input=json.dumps(data),text=True,capture_output=True,env=environment,check=True)
            return json.loads(completed.stdout)
        self.assertTrue(call('inspect',self.identity)['has_action'])
        claim = call('claim',{**self.identity,'wake':True})
        self.assertEqual(len(claim['messages']),1)
        self.assertEqual(claim['message']['attribution'],'agent')
        self.assertEqual(self.states()[claim['messages'][0]['message_id']],'delivering')


if __name__=='__main__':
    unittest.main()
