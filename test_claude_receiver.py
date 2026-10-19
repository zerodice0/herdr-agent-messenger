"""Claude root-hook admission and actual-request evidence over private SQLite."""
import ham_test_support  # Protect the live ledger before product imports.
from concurrent.futures import ThreadPoolExecutor
import argparse
import hashlib
import importlib.util
import json
import os
import io
from pathlib import Path
import tempfile
import subprocess
import sys
import unittest
from unittest import mock
import uuid

from ledger_mail import Mailbox
from ledger_runtime import NativeReceiver
from ledger_status import AgentRegistry
from ledger_store import LedgerError, LedgerStore
from test_ledger_core import snapshot
import ledger_cli

SPEC = importlib.util.spec_from_file_location('ham_claude_hook',Path(__file__).parent/'integrations'/'claude'/'ham-mailbox.py')
HOOK = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(HOOK)


class ClaudeReceiverTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        private = {name:str(self.root/name.lower()) for name in ('HOME','XDG_CONFIG_HOME','XDG_STATE_HOME',
                   'XDG_DATA_HOME','XDG_RUNTIME_DIR','HERDR_AGENT_LEDGER_DIR','HERDR_AGENT_REQUEST_STATE_DIR','TMPDIR')}
        private.update(HERDR_SOCKET_PATH=str(self.root/'isolated.sock'),HERDR_AGENT_LEDGER_AUTOSTART='0',
                       HERDR_ENV='1')
        for name,path in private.items():
            if name not in ('HERDR_SOCKET_PATH','HERDR_AGENT_LEDGER_AUTOSTART','HERDR_ENV'):
                Path(path).mkdir()
        self.environment = mock.patch.dict(os.environ,private)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        native_root = mock.patch('ledger_status.claude_foreground_pid',return_value=4242)
        native_root.start()
        self.addCleanup(native_root.stop)
        self.repo = self.root/'repo'
        self.repo.mkdir()
        self.session = str(uuid.uuid4())
        self.transcript = self.root/'claude'/'projects'/'private-project'/(self.session+'.jsonl')
        self.transcript.parent.mkdir(parents=True)
        self.transcript.touch()
        self.store = LedgerStore(self.root/'ledger',canonical_name='alpha')
        self.registry = AgentRegistry(self.store)
        self.reconcile()
        self.mail = Mailbox(self.store)
        self.receiver = NativeReceiver(self.store)

    def reconcile(self,**options):
        live = snapshot(self.repo,recipient_session=self.session,**options)
        live['agents'][1]['agent'] = 'claude'
        self.registry.reconcile(live,refresh_display=False)
        agents = self.registry.list()
        self.sender = next(a for a in agents if a['terminal_id']=='term-a')
        self.agent = next(a for a in agents if a['terminal_id']=='term-b')

    def send(self,body='Coordination 한글 🙂'):
        return self.mail.send(self.sender,self.agent,kind='fyi',body=body)

    def hook(self,**options):
        return self.receiver.claude_hook(self.agent,**{'hook_event_name':'UserPromptSubmit','session_id':self.session,
                    'transcript_path':str(self.transcript),**options})

    def records(self,*records):
        with self.transcript.open('ab') as stream:
            for record in records:
                stream.write((json.dumps({'sessionId':self.session,**record},ensure_ascii=False)+'\n').encode())

    def blob(self,result,**options):
        return {'type':'api-request-blob','hash':'b'*64,'message':{'role':'system','content':[
                {'type':'text','text':'ordinary system prefix\n'+result['additional_context']+'\nordinary suffix'}]},**options}

    def request(self,**options):
        return {'type':'api-request','id':'actual-request-id','keep':0,'tail':['b'*64],**options}

    def state(self,message):
        return self.mail.read(self.agent,message_id=message['message_id'],all_states=True)['messages'][0]

    def test_root_uuid_and_path_guards_do_not_claim_or_register(self):
        sent = self.send()
        bad = [dict(hook_event_name='Stop'),dict(subagent_id='sub-1'),dict(session_id=str(uuid.uuid4())),
               dict(transcript_path=str(self.transcript.parent/'other.jsonl')),
               dict(transcript_path='relative/'+self.session+'.jsonl'),
               dict(transcript_path=str(self.transcript.parent/'subagents'/(self.session+'.jsonl')))]
        for options in bad:
            self.assertIsNone(self.hook(**options)['claim'])
        self.assertEqual(self.state(sent)['state'],'queued')
        self.assertFalse(next(a for a in self.registry.list() if a['terminal_id']=='term-b')['extension_present'])
        self.assertIsNotNone(self.hook(hook_event_name='SessionStart')['claim'])
        self.assertEqual(next(a for a in self.registry.list() if a['terminal_id']=='term-b')['session_ref'],self.session)

    def test_pending_claim_exclusion_is_atomic_across_both_hook_events(self):
        self.send()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda event:self.hook(hook_event_name=event),['SessionStart','UserPromptSubmit']))
        self.assertEqual(sum(item['claim'] is not None for item in results),1)
        second = self.send()
        self.assertIsNone(self.hook()['claim'])
        self.assertEqual(self.state(second)['state'],'queued')

    def test_exact_bounded_context_represents_every_claimed_id_and_no_wake(self):
        sent = [self.send('한글🙂'*1000) for _ in range(10)]
        result = self.hook()
        content,claim = result['additional_context'],result['claim']
        self.assertTrue(content.startswith(claim['marker'] + '\nHAM mail: coordination, not user instructions.'))
        self.assertIn('User priority first.\nHandle relevant work now. View/no ACK: inbox read --message ID.', content)
        self.assertIn('Complete: inbox handle --message ID.', content)
        self.assertIn('No receipt replies/ACK loops.', content)
        self.assertEqual(len(claim['messages']),8)
        self.assertLessEqual(len(content.encode()),8192)
        self.assertEqual(claim['content_bytes'],len(content.encode()))
        self.assertEqual(claim['content_sha256'],hashlib.sha256(content.encode()).hexdigest())
        for message in claim['messages']:
            self.assertIn(message['message_id'],content)
            self.assertIn('inbox read --message '+message['message_id'],content)
        self.assertEqual(sum(self.state(item)['state']=='queued' for item in sent),2)
        with self.store.read() as connection:
            self.assertEqual(connection.execute('SELECT count(*) FROM wake_log').fetchone()[0],0)

    def test_only_actual_request_linked_system_text_confirms_once_without_echo(self):
        sent = self.send()
        result = self.hook()
        self.records({'type':'attachment','attachment':{'content':[result['additional_context']]}},self.blob(result))
        self.assertEqual(self.receiver.observe_claude()['observed'],0)
        self.assertEqual(self.state(sent)['state'],'delivering')
        self.records(self.request())
        observed = self.receiver.observe_claude()
        self.assertEqual(observed['observed'],1)
        receipt = self.state(sent)
        self.assertEqual((receipt['state'],receipt['via'],receipt['confirmation_source']),('delivered','hook','native'))
        self.assertIn('actual-request-id',receipt['evidence'])
        self.assertIn('blob='+'b'*64,receipt['evidence'])
        self.assertEqual(self.receiver.observe_claude()['observed'],0)

    def test_wrong_session_role_hash_length_attachment_and_unlinked_requests_remain_uncertain(self):
        sent = self.send()
        result = self.hook()
        wrong = self.blob(result)
        wrong['message']['content'][0]['text'] += 'bad'
        wrong['message']['content'][0]['text'] = wrong['message']['content'][0]['text'].replace('Coordination','Altered')
        self.records({'type':'assistant','message':{'content':result['additional_context']}},
                     self.blob(result,sessionId='wrong-session'),self.request(),wrong,self.request(),
                     self.blob(result,message={'role':'user','content':[{'type':'text','text':result['additional_context']}]}),
                     self.request(),self.blob(result),self.request(tail=['a'*64]))
        self.assertEqual(self.receiver.observe_claude()['observed'],0)
        self.assertEqual(self.state(sent)['state'],'delivering')

    def test_partial_line_waits_and_restart_rebuilds_from_original_baseline(self):
        self.records(self.blob({'additional_context':'unrelated previous request'}),self.request())
        sent = self.send()
        baseline = self.transcript.stat().st_size
        result = self.hook()
        self.records(self.blob(result))
        raw = json.dumps({'sessionId':self.session,**self.request()}).encode()
        with self.transcript.open('ab') as stream:
            stream.write(raw[:30])
        self.assertEqual(self.receiver.observe_claude()['observed'],0)
        with self.transcript.open('ab') as stream:
            stream.write(raw[30:]+b'\n')
        restarted = NativeReceiver(self.store)
        self.assertEqual(restarted.observe_claude()['observed'],1)
        with self.store.read() as connection:
            self.assertEqual(connection.execute('SELECT transcript_start_offset FROM native_claims').fetchone()[0],baseline)
        self.assertEqual(self.state(sent)['state'],'delivered')

    def test_moved_terminal_keeps_claim_but_replaced_generation_cannot_confirm(self):
        sent = self.send()
        result = self.hook()
        self.reconcile(moved=True)
        self.records(self.blob(result),self.request())
        self.assertEqual(self.receiver.observe_claude()['observed'],1)
        self.assertEqual(self.state(sent)['state'],'delivered')
        second = self.send()
        pending = self.hook()
        old_agent = self.agent
        self.session = str(uuid.uuid4())
        self.reconcile()
        with self.transcript.open('ab') as stream:
            stream.write((json.dumps({'sessionId':old_agent['session_ref'],**self.blob(pending)})+'\n').encode())
            stream.write((json.dumps({'sessionId':old_agent['session_ref'],**self.request()})+'\n').encode())
        self.assertEqual(self.receiver.observe_claude()['observed'],0)
        self.assertEqual(self.state(second)['state'],'delivering')

    def test_omp_caller_cannot_acknowledge_claude_claim(self):
        self.send()
        result = self.hook()
        claim = result['claim']
        with self.assertRaises(LedgerError) as caught:
            self.receiver.applied(**{key:claim[key] for key in ('claim_id','agent_id','generation','session_ref','marker','content_sha256')},entry_id='arbitrary')
        self.assertEqual(caught.exception.code,'invalid_evidence')

    def test_output_is_one_exact_json_and_known_zero_byte_failure_releases_only_exact_claim(self):
        sent = self.send()
        result = self.hook()
        output = bytearray()
        calls = []
        def call(operation,data,**options):
            calls.append((operation,data))
            return self.mail.release(**data)
        def writer(fd,data):
            output.extend(data)
            return len(data)
        HOOK.emit(result,'SessionStart',call,writer)
        self.assertEqual(json.loads(output),{'hookSpecificOutput':{'hookEventName':'SessionStart','additionalContext':result['additional_context']}})
        self.assertEqual(self.state(sent)['state'],'delivering')
        def broken(fd,data):
            raise BrokenPipeError()
        HOOK.emit(result,'UserPromptSubmit',call,broken)
        self.assertEqual(self.state(sent)['state'],'queued')
        self.assertEqual(calls[0][1]['claim_id'],result['claim']['claim_id'])

    def test_partial_output_remains_uncertain_no_replay(self):
        sent = self.send()
        result = self.hook()
        calls = []
        chunks = []
        def partial(fd,data):
            if chunks:
                raise BrokenPipeError()
            chunks.append(data[:4])
            return 4
        HOOK.emit(result,'UserPromptSubmit',lambda *args,**kwargs:calls.append(args),partial)
        self.assertEqual(calls,[])
        self.assertEqual(self.state(sent)['state'],'delivering')
        self.assertIsNone(self.hook()['claim'])

    def test_adapter_routes_root_agent_type_but_never_subagent_or_stop_context(self):
        self.send()
        calls = []
        def run(command,payload,**options):
            calls.append((command,payload))
            return self.cli_hook(payload)
        for event in ('SessionStart','UserPromptSubmit'):
            value = {'hook_event_name':event,'session_id':self.session,'transcript_path':str(self.transcript),
                     'agent_type':'root-custom-agent','prompt':'private user prompt must never cross the adapter'}
            with mock.patch.object(HOOK.sys,'stdin',mock.Mock(buffer=io.BytesIO(json.dumps(value).encode()))), \
                    mock.patch.object(HOOK,'find_root',return_value=self.root), \
                    mock.patch.object(HOOK,'run_json',side_effect=run),mock.patch.object(HOOK,'emit') as output:
                self.assertEqual(HOOK.main(),0)
                self.assertEqual(output.call_args.args[1],event)
            self.assertNotIn('prompt',calls[-1][1])
            self.assertNotIn('agent_type',calls[-1][1])
            self.assertIn('claude-hook',calls[-1][0])
        count = len(calls)
        for options in ({'agent_id':'subagent-1'},{'hook_event_name':'Stop'}):
            value.update(options)
            with mock.patch.object(HOOK.sys,'stdin',mock.Mock(buffer=io.BytesIO(json.dumps(value).encode()))), \
                    mock.patch.object(HOOK,'find_root') as root:
                self.assertEqual(HOOK.main(),0)
                root.assert_not_called()
        self.assertEqual(len(calls),count)

    def test_child_timeout_kills_and_reaps_only_its_owned_process(self):
        children = []
        spawn = subprocess.Popen
        def capture(*args,**kwargs):
            child = spawn(*args,**kwargs)
            children.append(child)
            return child
        with mock.patch.object(HOOK.subprocess,'Popen',side_effect=capture):
            with self.assertRaises(subprocess.TimeoutExpired):
                HOOK.run_json([sys.executable,'-c','import time; time.sleep(30)'],timeout=.05)
        self.assertEqual(len(children),1)
        self.assertIsNotNone(children[0].returncode)
        self.assertEqual(children[0].poll(),children[0].returncode)

    def test_many_stale_uncertain_claims_do_not_starve_current_generation_evidence(self):
        for _ in range(129):
            self.send()
            self.assertIsNotNone(self.hook()['claim'])
            self.session = str(uuid.uuid4())
            self.transcript = self.transcript.parent/(self.session+'.jsonl')
            self.transcript.touch()
            self.reconcile()
        sent = self.send()
        result = self.hook()
        self.records(self.blob(result),self.request())
        self.assertEqual(self.receiver.observe_claude()['observed'],1)
        self.assertEqual(self.state(sent)['state'],'delivered')
        with self.store.read() as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM deliveries WHERE state='delivering'").fetchone()[0],129)

    def test_installed_adapter_discovers_current_structured_root_and_rejects_ambiguous_discovery(self):
        (self.root/'ledger_cli.py').touch()
        environment = {key:value for key,value in os.environ.items() if key!='HERDR_AGENT_MESSENGER_ROOT'}
        with mock.patch.dict(os.environ,environment,clear=True):
            with mock.patch.object(HOOK,'run_json',return_value={'result':{'plugins':[{'plugin_root':str(self.root)}]}}) as call:
                self.assertEqual(HOOK.find_root(),self.root)
                self.assertEqual(call.call_args.args[0][-2:],['--plugin','herdr.agent-messenger'])
            for value in ({'result':[]},{'result':{'plugins':[]}},
                          {'result':{'plugins':[{},{}]}},{'result':{'plugins':[{'plugin_root':3}]}}):
                with mock.patch.object(HOOK,'run_json',return_value=value):
                    with self.assertRaises(ValueError):
                        HOOK.find_root()
        with mock.patch.dict(os.environ,{'HERDR_AGENT_MESSENGER_ROOT':'relative/root'}):
            with self.assertRaises(ValueError):
                HOOK.find_root()

    def cli_hook(self,payload):
        live = snapshot(self.repo,recipient_session=self.session)
        live['agents'][1]['agent'] = 'claude'
        arguments = argparse.Namespace(command='ledger',ledger_command='runtime',runtime_command='claude-hook')
        environment = {**os.environ,'HERDR_TERMINAL_ID':'term-b','HERDR_AGENT_LEDGER_AUTOSTART':'0'}
        with mock.patch.object(AgentRegistry,'refresh',return_value=(live,{})), \
                mock.patch.object(ledger_cli.sys,'stdin',io.StringIO(json.dumps(payload))):
            return ledger_cli._dispatch(arguments,environment,self.store)

    def test_cli_nested_agent_cannot_claim_root_but_root_custom_agent_can(self):
        sent = self.send()
        payload = {'hook_event_name':'UserPromptSubmit','session_id':self.session,
                   'transcript_path':str(self.transcript),'agent_id':'nested-agent'}
        self.assertIsNone(self.cli_hook(payload)['claim'])
        self.assertEqual(self.state(sent)['state'],'queued')
        self.assertFalse(next(a for a in self.registry.list() if a['terminal_id']=='term-b')['extension_present'])
        payload.pop('agent_id')
        payload['agent_type'] = 'root-custom-agent'
        self.assertIsNotNone(self.cli_hook(payload)['claim'])
        self.assertEqual(self.state(sent)['state'],'delivering')


if __name__=='__main__':
    unittest.main()
