"""Claude 2.1.301 interactive journal evidence over an isolated real ledger."""
import ham_test_support  # Protect the live ledger before product imports.
from copy import deepcopy
import hashlib
import json
from unittest import mock
import uuid

from ledger_mail import Mailbox
from ledger_runtime import NativeReceiver
from ledger_status import AgentRegistry
from ledger_store import LedgerStore
from ledger_worker import run_worker
from test_claude_registration import PrivateClaudeTest
from test_ledger_core import snapshot


class ClaudeNativeJournalTest(PrivateClaudeTest):
    def setUp(self):
        super().setUp()
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
        self.latest = snapshot(self.repo, recipient_session=self.session)
        self.latest['agents'][1]['agent'] = 'claude'
        for patcher in (
            mock.patch('ledger_status.live_snapshot', side_effect=lambda environment=None: deepcopy(self.latest)),
            mock.patch('ledger_status.claude_foreground_pid', return_value=4242),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.reconcile()
        self.sender = self.current('term-a')

    def reconcile(self):
        self.registry.reconcile(self.latest, refresh_display=False, environment=self.environment)

    def current(self, terminal='term-b'):
        return next(row for row in self.registry.list() if row['terminal_id'] == terminal and row['active'])

    def admit(self):
        sent = self.mail.send(self.sender, self.current(), kind='fyi', body='Review 한글 🙂 café e\u0301.')
        result = self.receiver.claude_hook(
            self.current(), hook_event_name='SessionStart', session_id=self.session,
            transcript_path=str(self.transcript), environment=self.environment,
        )
        self.assertIsNotNone(result['claim'])
        return sent, result

    def delivery(self, sent):
        with self.store.read() as connection:
            return dict(connection.execute(
                'SELECT * FROM deliveries WHERE message_id=?', (sent['message_id'],),
            ).fetchone())

    def node(self, kind, parent=None, **options):
        return {
            'type': kind, 'uuid': str(uuid.uuid4()), 'parentUuid': parent,
            'isSidechain': False, 'sessionId': self.session, **options,
        }

    def context(self, result):
        # Observed in a genuine Claude 2.1.301 interactive SessionStart journal
        # (2026-10-18, e1bba417-73c9-402e-a06b-6bf9efb72669). Only the typed
        # hook shape is retained; session/claim content is generated privately.
        content = result['additional_context']
        return self.node('attachment', attachment={
            'type': 'hook_additional_context', 'content': [content],
            'hookName': 'SessionStart', 'toolUseID': 'SessionStart', 'hookEvent': 'SessionStart',
        }, renderedRole='system', rendered=[{
            'content': '<system-reminder>\nSessionStart hook additional context: ' + content + '\n</system-reminder>',
        }])

    def response(self, parent, **options):
        # Observed real API response metadata, not an api-request-blob and not
        # model-authored text. Model content is deliberately absent by default.
        return self.node('assistant', parent, requestId='req_011Cg8PfTC64x4bWceSsrpgM', message={
            'id': 'msg_011Cg8PfTSiczwoPsneUberR', 'model': 'claude-haiku-5-5', 'role': 'assistant',
        }, **options)

    def chain(self, result):
        hook = self.context(result)
        user = self.node('user', hook['uuid'], message={'role': 'user', 'content': 'Private fixture prompt.'})
        environment = self.node('attachment', user['uuid'], attachment={'type': 'environment'})
        model = self.node('attachment', environment['uuid'], attachment={'type': 'model'})
        return [hook, user, environment, model, self.response(model['uuid'])]

    def records(self, *records):
        with self.transcript.open('ab') as stream:
            for record in records:
                stream.write((json.dumps(record, ensure_ascii=False) + '\n').encode('utf-8'))

    def assert_uncertain(self, sent):
        self.assertEqual(self.receiver.observe_claude()['observed'], 0)
        row = self.delivery(sent)
        self.assertEqual(row['state'], 'delivering')
        self.assertIsNone(row['confirmation_source'])
        self.assertIsNone(row['delivered_agent_id'])

    def test_interactive_parent_chain_delivers_native_receipt_once_without_model_content(self):
        sent, result = self.admit()
        chain = self.chain(result)
        self.records(*chain[:-1])
        self.assert_uncertain(sent)
        self.records(chain[-1])
        observed = self.receiver.observe_claude()
        self.assertEqual(observed['observed'], 1)
        self.assertEqual(len(observed['receipts']), 1)
        delivered = self.delivery(sent)
        self.assertEqual((delivered['state'], delivered['via'], delivered['confirmation_source']),
                         ('delivered', 'hook', 'native'))
        self.assertEqual(delivered['delivered_agent_id'], result['claim']['agent_id'])
        self.assertEqual(delivered['evidence'],
                         'claude:accepted-journal:' + self.session + '#req_011Cg8PfTC64x4bWceSsrpgM' +
                         ';hook=' + chain[0]['uuid'] + ';sha256=' + result['claim']['content_sha256'])
        self.assertEqual(self.receiver.observe_claude(), {'observed': 0, 'receipts': []})
        self.assertEqual(NativeReceiver(self.store).observe_claude()['observed'], 0)
        self.assertEqual(self.delivery(sent), delivered)

    def test_worker_observer_consumes_private_native_journal(self):
        sent, result = self.admit()
        self.records(*self.chain(result))
        outcome = run_worker(self.store, self.environment, once=True)
        self.assertEqual(outcome['reason'], 'one_pass')
        receipt = self.delivery(sent)
        self.assertEqual((receipt['state'], receipt['confirmation_source']), ('delivered', 'native'))
        self.assertTrue(receipt['evidence'].startswith('claude:accepted-journal:'))
        run_worker(self.store, self.environment, once=True)
        self.assertEqual(self.delivery(sent), receipt)

    def test_raw_and_rendered_context_require_exact_utf8_hash_and_typed_system_role(self):
        sent, result = self.admit()
        content = result['additional_context']
        variants = [
            ('attachment.content', [content.replace('User priority first.', 'User priority last.')]),
            ('attachment.content', [content.replace('한글', '다름')]),
            ('attachment.content', [content[:-1]]),
            ('attachment.content', content),
            ('attachment.content', [{'type': 'text', 'text': content}]),
            ('attachment.type', 'hook_success'),
            ('attachment.hookName', 'Stop'),
            ('attachment.hookEvent', 'Stop'),
            ('attachment.hookEvent', 'UserPromptSubmit'),
            ('renderedRole', 'assistant'),
            ('renderedRole', 'user'),
            ('rendered', [{'content': content.replace('View/no ACK:', 'View/ACK:')}]),
            ('rendered', [{'content': '<system-reminder>' + content.replace('한글', '다름') + '</system-reminder>'}]),
            ('rendered', [{'content': content[:-1]}]),
            ('rendered', []),
            ('rendered', content),
        ]
        for field, value in variants:
            with self.subTest(field=field, value=value):
                hook = self.context(result)
                target = hook
                parts = field.split('.')
                for key in parts[:-1]:
                    target = target[key]
                target[parts[-1]] = value
                self.records(hook, self.response(hook['uuid']))
                self.assert_uncertain(sent)
        for field in ('renderedRole', 'rendered', 'attachment'):
            with self.subTest(missing=field):
                hook = self.context(result)
                del hook[field]
                self.records(hook, self.response(hook['uuid']))
                self.assert_uncertain(sent)

    def test_shared_hash_window_uses_bytes_and_searches_past_wrong_marker_occurrence(self):
        _, result = self.admit()
        claim, content = result['claim'], result['additional_context']
        encoded = content.encode('utf-8')
        self.assertGreater(len(encoded), len(content))
        self.assertEqual(claim['content_bytes'], len(encoded))
        self.assertEqual(claim['content_sha256'], hashlib.sha256(encoded).hexdigest())
        text = '🙂 prefix ' + claim['marker'] + ' invalid\n' + content + '\n한글 suffix'
        self.assertTrue(self.receiver._claude_text_matches(text, claim))
        self.assertTrue(self.receiver._claude_blob_matches({
            'message': {'role': 'system', 'content': [{'type': 'text', 'text': text}]},
        }, claim))
        hook = self.context(result)
        hook['attachment']['content'] = [text]
        hook['rendered'][0]['content'] = '<system-reminder>' + text + '</system-reminder>'
        self.assertTrue(self.receiver._claude_attachment_matches(hook, claim))
        for changed in (content[:-1], content.replace('한글', '다름'), content.replace('e\u0301', 'é')):
            with self.subTest(changed=changed):
                self.assertFalse(self.receiver._claude_text_matches(changed, claim))
        self.assertFalse(self.receiver._claude_text_matches(content, {**claim, 'content_bytes': len(content)}))

    def test_model_quote_or_untyped_hook_cannot_create_context_root(self):
        sent, result = self.admit()
        quote = self.response(None)
        quote['message']['content'] = [{'type': 'text', 'text': result['additional_context']}]
        user = self.node('user', message={'role': 'user', 'content': result['additional_context']})
        untyped = self.node('attachment', attachment={'content': [result['additional_context']]},
                            renderedRole='system', rendered=[{'content': result['additional_context']}])
        system = self.node('system', message={'role': 'system', 'content': result['additional_context']})
        for root in (quote, user, untyped, system):
            with self.subTest(kind=root['type']):
                self.records(root, self.response(root['uuid']))
                self.assert_uncertain(sent)

    def test_wrong_session_sidechain_or_missing_uuid_cannot_supply_or_bridge_context(self):
        sent, result = self.admit()
        invalid = [
            ('sessionId', str(uuid.uuid4())), ('isSidechain', True), ('isSidechain', None),
            ('isSidechain', 'false'), ('uuid', ''), ('uuid', None), ('uuid', 7),
        ]
        for position in ('hook', 'bridge', 'response'):
            for field, value in invalid:
                with self.subTest(position=position, field=field, value=value):
                    hook = self.context(result)
                    bridge = self.node('user', hook['uuid'])
                    response = self.response(bridge['uuid'])
                    chosen = {'hook': hook, 'bridge': bridge, 'response': response}[position]
                    chosen[field] = value
                    self.records(hook, bridge, response)
                    self.assert_uncertain(sent)
        for field in ('uuid', 'isSidechain', 'sessionId'):
            hook = self.context(result)
            del hook[field]
            self.records(hook, self.response(hook.get('uuid')))
            self.assert_uncertain(sent)

    def test_only_real_assistant_response_metadata_confirms_context(self):
        sent, result = self.admit()
        hook = self.context(result)
        self.records(hook)
        changes = [
            ('requestId', None), ('requestId', ''), ('requestId', 7),
            ('message', None), ('message.id', None), ('message.id', ''), ('message.id', 7),
            ('message.model', None), ('message.model', ''), ('message.model', '<synthetic>'),
            ('message.role', 'user'), ('message.role', 'system'), ('isApiErrorMessage', True),
            ('type', 'progress'),
        ]
        for field, value in changes:
            with self.subTest(field=field, value=value):
                response = self.response(hook['uuid'])
                parts = field.split('.')
                target = response if len(parts) == 1 else response[parts[0]]
                target[parts[-1]] = value
                self.records(response)
                self.assert_uncertain(sent)
        for field in ('requestId', 'message'):
            response = self.response(hook['uuid'])
            del response[field]
            self.records(response)
            self.assert_uncertain(sent)

    def test_unrelated_unknown_broken_and_cyclic_branches_stay_uncertain(self):
        sent, result = self.admit()
        hook = self.context(result)
        self.records(hook)
        unrelated = self.node('user')
        unknown = self.node('progress', hook['uuid'])
        broken = self.node('attachment', str(uuid.uuid4()), attachment={'type': 'environment'})
        malformed = self.node('user', {'not': 'a parent uuid'})
        cycle_a = self.node('user')
        cycle_b = self.node('user', cycle_a['uuid'])
        cycle_a['parentUuid'] = cycle_b['uuid']
        self_parent = self.node('user')
        self_parent['parentUuid'] = self_parent['uuid']
        self.records(unrelated, unknown, broken, malformed, cycle_a, cycle_b, self_parent)
        for parent in (None, '', str(uuid.uuid4()), unrelated['uuid'], unknown['uuid'], broken['uuid'],
                       malformed['uuid'], cycle_a['uuid'], cycle_b['uuid'], self_parent['uuid']):
            with self.subTest(parent=parent):
                self.records(self.response(parent))
                self.assert_uncertain(sent)

    def test_duplicate_uuid_cannot_rewrite_an_unlinked_node_as_matched_context(self):
        sent, result = self.admit()
        original = self.node('user')
        forged = self.context(result)
        forged['uuid'] = original['uuid']
        self.records(original, forged, self.response(original['uuid']))
        self.assert_uncertain(sent)
        hook = self.context(result)
        self.records(hook, deepcopy(hook), self.response(hook['uuid']))
        self.assertEqual(self.receiver.observe_claude()['observed'], 1)
        self.assertEqual(self.delivery(sent)['state'], 'delivered')

    def test_duplicate_response_uuid_cannot_gain_missing_native_metadata(self):
        sent, result = self.admit()
        hook = self.context(result)
        incomplete = self.response(hook['uuid'])
        del incomplete['requestId']
        completed = self.response(hook['uuid'])
        completed['uuid'] = incomplete['uuid']
        self.records(hook, incomplete, completed)
        self.assert_uncertain(sent)

    def test_incomplete_json_waits_for_newline_and_restart_rebuilds_parent_chain(self):
        self.records(self.node('user'))
        baseline = self.transcript.stat().st_size
        sent, result = self.admit()
        chain = self.chain(result)
        self.records(*chain[:-1])
        raw = json.dumps(chain[-1], ensure_ascii=False).encode('utf-8')
        with self.transcript.open('ab') as stream:
            stream.write(raw[:31])
        self.assert_uncertain(sent)
        with self.transcript.open('ab') as stream:
            stream.write(raw[31:])
        self.assert_uncertain(sent)
        with self.transcript.open('ab') as stream:
            stream.write(b'\n')
        restarted = NativeReceiver(self.store)
        self.assertEqual(restarted.observe_claude()['observed'], 1)
        with self.store.read() as connection:
            native = dict(connection.execute('SELECT * FROM native_claims').fetchone())
        self.assertEqual(native['transcript_start_offset'], baseline)
        self.assertEqual(native['content_sha256'], result['claim']['content_sha256'])
        self.assertEqual(self.delivery(sent)['state'], 'delivered')

    def test_restart_never_scans_parent_nodes_before_immutable_admission_baseline(self):
        preexisting = self.node('user')
        self.records(preexisting)
        baseline = self.transcript.stat().st_size
        sent, result = self.admit()
        self.records(self.response(preexisting['uuid']))
        self.assert_uncertain(sent)
        self.receiver = NativeReceiver(self.store)
        self.assert_uncertain(sent)
        scan = self.receiver._claude_scans[result['claim']['claim_id']]
        self.assertNotIn(preexisting['uuid'], scan['nodes'])
        with self.store.read() as connection:
            self.assertEqual(connection.execute('SELECT transcript_start_offset FROM native_claims').fetchone()[0], baseline)
        self.records(*self.chain(result))
        self.assertEqual(self.receiver.observe_claude()['observed'], 1)

    def test_node_map_is_bounded_and_unlinked_records_cannot_confirm(self):
        sent, result = self.admit()
        roots = [self.node('user') for _ in range(700)]
        self.records(*roots, self.response(roots[-1]['uuid']))
        self.assert_uncertain(sent)
        nodes = self.receiver._claude_scans[result['claim']['claim_id']]['nodes']
        self.assertLessEqual(len(nodes), 512)
        self.assertTrue(all(root is None for root in nodes.values()))

    def test_closed_generation_holds_claim_even_when_complete_native_evidence_arrives(self):
        sent, result = self.admit()
        old = self.current()
        self.latest = snapshot(self.repo, recipient_present=False)
        self.reconcile()
        before = self.delivery(sent)
        self.assertEqual((before['state'], before['hold_reason']), ('delivering', 'acceptance_unknown'))
        self.records(*self.chain(result))
        self.assert_uncertain(sent)
        self.assertEqual(self.delivery(sent), before)
        with self.store.read() as connection:
            historical = dict(connection.execute(
                'SELECT * FROM agents WHERE agent_id=?', (old['agent_id'],),
            ).fetchone())
        self.assertFalse(historical['active'])

    def test_superseded_generation_never_acknowledges_old_claim(self):
        sent, result = self.admit()
        old = self.current()
        self.latest['agents'][1]['agent_session'] = {'value': str(uuid.uuid4())}
        self.reconcile()
        replacement = self.current()
        self.assertNotEqual(replacement['agent_id'], old['agent_id'])
        self.assertEqual(replacement['generation'], old['generation'] + 1)
        held = self.delivery(sent)
        self.assertEqual((held['state'], held['hold_reason']), ('delivering', 'acceptance_unknown'))
        self.records(*self.chain(result))
        self.assert_uncertain(sent)
        self.assertEqual(self.delivery(sent), held)

    def test_generation_change_between_scan_and_started_still_holds_old_claim(self):
        sent, result = self.admit()
        self.records(*self.chain(result))
        started = Mailbox.started

        def replace_before_started(mailbox, **identity):
            self.latest['agents'][1]['agent_session'] = {'value': str(uuid.uuid4())}
            self.reconcile()
            return started(mailbox, **identity)

        with mock.patch.object(Mailbox, 'started', autospec=True, side_effect=replace_before_started):
            self.assert_uncertain(sent)
        held = self.delivery(sent)
        self.assertEqual((held['state'], held['hold_reason']), ('delivering', 'acceptance_unknown'))
        self.assertEqual(held['claim_id'], result['claim']['claim_id'])
        self.assertEqual(held['claimed_generation'], result['claim']['generation'])

    def test_response_evidence_names_its_ancestor_hook_not_latest_unrelated_hook(self):
        sent, result = self.admit()
        first = self.context(result)
        bridge = self.node('system', first['uuid'])
        second = self.context(result)
        self.records(first, bridge, second, self.response(bridge['uuid']))
        self.assertEqual(self.receiver.observe_claude()['observed'], 1)
        evidence = self.delivery(sent)['evidence']
        self.assertIn(';hook=' + first['uuid'] + ';', evidence)
        self.assertNotIn(second['uuid'], evidence)

    def test_malformed_complete_records_do_not_supply_or_prevent_native_evidence(self):
        sent, result = self.admit()
        hook = self.context(result)
        with self.transcript.open('ab') as stream:
            stream.write(b'{not-json}\nnull\n[]\n"untyped"\n')
        self.records(hook)
        self.assert_uncertain(sent)
        self.records(self.response(hook['uuid']))
        self.assertEqual(self.receiver.observe_claude()['observed'], 1)
        self.assertEqual(self.delivery(sent)['confirmation_source'], 'native')
