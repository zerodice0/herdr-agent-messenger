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
from unittest import mock

from ledger_mail import Mailbox
from ledger_runtime import NativeReceiver
from ledger_settings import DEFAULT_THRESHOLDS, RepoSettings
from ledger_status import AgentRegistry
from ledger_store import LedgerError, LedgerStore
from test_ledger_core import snapshot


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
        with mock.patch('ledger_runtime.BUNDLE_BYTES',2000):
            claim = self.claim(wake=True)
        claimed = {message['message_id'] for message in claim['messages']}
        content = claim['message']['content']
        self.assertLess(len(claimed),8)
        self.assertLessEqual(len(content.encode('utf-8')),2000)
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
        with mock.patch('ledger_runtime.BUNDLE_BYTES',850):
            claim = self.claim(wake=True)
        self.assertEqual([message['message_id'] for message in claim['messages']],[action['message_id']])
        self.assertEqual(self.represented_ids(claim['message']['content']),{action['message_id']})
        states = self.states()
        self.assertEqual({states[item['message_id']] for item in fyis},{'queued'})

    def test_even_one_message_that_cannot_be_identified_is_never_claimed(self):
        sent = self.send(kind='action')
        with mock.patch('ledger_runtime.BUNDLE_BYTES',400):
            claim = self.claim(wake=True)
        self.assertIsNone(claim['claim_id'])
        self.assertEqual(claim['reason'],'unrepresentable')
        self.assertEqual(self.states()[sent['message_id']],'queued')


    def test_action_disappearing_between_inspect_and_atomic_claim_never_wakes_fyi(self):
        fyi = self.send()
        action = self.send(kind='action')
        self.assertTrue(self.receiver.inspect(**self.identity)['has_action'])
        self.mail.withdraw(action['message_id'],sender=self.sender)
        claim = self.claim(wake=True)
        self.assertIsNone(claim['claim_id'])
        self.assertEqual(claim['reason'],'no_action')
        self.assertEqual(self.states()[fyi['message_id']],'queued')

    def test_over_hop_action_does_not_authorize_fyi_only_wake(self):
        self.settings.mutate(self.recipient['repo_id'],'thresholds',{**self.thresholds,'max_hop':0})
        fyi = self.send()
        action = self.send(kind='action',reply_to=fyi['message_id'])
        self.assertFalse(self.receiver.inspect(**self.identity)['has_action'])
        self.assertEqual(self.claim(wake=True)['reason'],'no_action')
        self.assertEqual(self.states()[action['message_id']],'queued')
        self.assertEqual([message['message_id'] for message in self.claim()['messages']],[fyi['message_id']])

    def test_fyi_alone_never_wakes_but_is_eligible_on_user_turn(self):
        self.send()
        self.assertEqual(self.claim(wake=True)['reason'],'no_action')
        self.assertIsNotNone(self.claim()['claim_id'])

    def test_coalesce_hourly_reservations_and_native_working_are_admission_gates(self):
        self.settings.mutate(self.recipient['repo_id'],'thresholds',{**self.thresholds,'coalesce_seconds':30,'wakes_per_hour':1})
        self.send(kind='action')
        self.assertEqual(self.claim(wake=True)['reason'],'coalescing')
        with mock.patch('ledger_runtime.time.time',return_value=time.time()+31):
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
        script = str(Path(__file__).parent/'agent_skill_cli.py')
        def call(operation,data):
            completed = subprocess.run([sys.executable,script,'ledger','runtime',operation],input=json.dumps(data),text=True,capture_output=True,env=environment,check=True)
            return json.loads(completed.stdout)
        self.assertTrue(call('inspect',self.identity)['has_action'])
        claim = call('claim',{**self.identity,'wake':True})
        self.assertEqual(len(claim['messages']),1)
        self.assertEqual(claim['message']['attribution'],'agent')
        self.assertEqual(self.states()[claim['messages'][0]['message_id']],'delivering')


if __name__=='__main__':
    unittest.main()
