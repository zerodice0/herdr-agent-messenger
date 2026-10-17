"""Real two/three-store CLI wire tests with transport fault injection, not DB replication."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
import uuid

from ledger_mail import Mailbox
from ledger_remote import RemoteLedger
from ledger_settings import DEFAULT_THRESHOLDS, RepoSettings, stamp
from ledger_status import AgentRegistry
from ledger_store import LedgerError, LedgerStore, public_receipt, receipt
from ledger_worker import maintenance_pending, worker_lock

ROOT = Path(__file__).resolve().parent
CLI = ROOT / '.agents/skills/herdr-agent-messenger/scripts/herdr_agent_messenger.py'


class CLITransport:
    """Real public CLI stdin/JSON on another private store; inject only link faults."""
    def __init__(self, owner, stores, environment):
        self.owner, self.stores, self.environment = owner, stores, environment
        self.failures = {}
        self.events = []

    def call(self, peer, command, payload):
        target = peer['canonical_name']
        self.events.append((target, command, payload.get('message_id') or payload.get('message', {}).get('message_id')))
        faults = self.failures.get(target, [])
        fault = faults.pop(0) if faults else None
        if fault == 'transient':
            raise LedgerError('transport_unavailable', 'Injected link unavailable before send')
        if fault == 'permanent':
            raise LedgerError('invalid_message', 'Injected endpoint validation refusal')
        envelope = RemoteLedger(self.owner).envelope(payload)
        result = subprocess.run([sys.executable, str(CLI), 'ledger', command],
                                input=json.dumps(envelope), capture_output=True, text=True, check=False,
                                env={**self.environment, 'HERDR_AGENT_LEDGER_DIR': str(self.stores[target].directory)})
        response = json.loads(result.stdout)
        if 'error' in response:
            raise LedgerError(response['error']['code'], response['error']['message'])
        if result.returncode:
            raise AssertionError(result.stderr)
        if fault == 'lost_ack':
            raise LedgerError('transport_unavailable', 'Injected lost ACK AFTER durable remote accept')
        return response


class RemoteLedgerTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root/'repo'
        subprocess.run(['git', 'init', '-q', str(self.repo)], check=True)
        subprocess.run(['git', '-C', str(self.repo), 'remote', 'add', 'origin', 'https://example.test/team/repo.git'], check=True)
        ssh_config, allowlist = self.root/'ssh-config', self.root/'ssh-hosts'
        ssh_config.write_text('Host a b c\n  HostName invalid.example.test\n')
        allowlist.write_text('a\nb\nc\n')
        self.environment = {**os.environ, 'HERDR_AGENT_LEDGER_AUTOSTART': '0', 'PYTHONDONTWRITEBYTECODE': '1',
                            'HERDR_AGENT_MESSENGER_SSH_CONFIG': str(ssh_config),
                            'HERDR_AGENT_MESSENGER_SSH_HOSTS_FILE': str(allowlist)}
        self.environment.pop('HERDR_AGENT_CANONICAL_HOST', None)
        self.stores = {host: LedgerStore(self.root/host, canonical_name=host) for host in ('alpha', 'beta', 'gamma')}
        self.agents = {}
        self.snapshots = {}
        for index, (host, store) in enumerate(self.stores.items()):
            pane = {'terminal_id': 'term-'+host, 'pane_id': f'w{index+1}:p1', 'workspace_id': f'w{index+1}'}
            raw = {**pane, 'agent': 'codex', 'name': host+'-owl', 'agent_status': 'idle',
                   'agent_session': {'value': 'session-'+host}, 'cwd': str(self.repo)}
            self.snapshots[host] = {'panes': [pane], 'agents': [raw]}
            registry = AgentRegistry(store)
            registry.reconcile(self.snapshots[host])
            self.agents[host] = registry.list()[0]
        self.repo_id = self.agents['alpha']['repo_id']
        self.links = {host: CLITransport(store, self.stores, self.environment) for host, store in self.stores.items()}
        self.services = {host: RemoteLedger(store, self.environment, self.links[host]) for host, store in self.stores.items()}
        aliases = {'alpha': 'a', 'beta': 'b', 'gamma': 'c'}
        for host, service in self.services.items():
            for target, store in self.stores.items():
                if target != host:
                    service.add_peer(canonical_name=target, uuid=store.host_uuid, ssh_alias=aliases[target],
                                     remote_ham_root=str(ROOT), remote_ledger_dir=str(store.directory))

    def send(self, source='alpha', target='beta', **options):
        return Mailbox(self.stores[source]).send(self.agents[source], self.agents[target], body='Immutable remote body 한글', **options)

    def current(self, host, message_id):
        with self.stores[host].read() as connection:
            return receipt(connection, message_id)

    def drain(self):
        for _ in range(20):
            pending = sum(sum(item['state'] == 'pending' for item in service.outbox()) for service in self.services.values())
            if not pending:
                return
            for service in self.services.values():
                service.pump(now=time.time()+1000)
        self.fail('Outbox failed to reach quiescence')

    def wire(self, target, command, envelope, expected=0):
        result = subprocess.run([sys.executable, str(CLI), 'ledger', command], input=json.dumps(envelope),
                                capture_output=True, text=True, check=False,
                                env={**self.environment, 'HERDR_AGENT_LEDGER_DIR': str(self.stores[target].directory)})
        self.assertEqual(result.returncode, expected, result.stderr + result.stdout)
        return json.loads(result.stdout)

    def test_cli_accept_handle_receipt_roundtrip_without_agent_replication(self):
        sent = self.send()
        self.assertEqual(sent['state'], 'pending')
        self.drain()
        self.assertEqual(self.current('alpha', sent['message_id'])['state'], 'queued')
        Mailbox(self.stores['beta']).handle(self.agents['beta'], [sent['message_id']])
        self.drain()
        final = self.current('alpha', sent['message_id'])
        self.assertEqual(final['state'], 'handled')
        self.assertIsNone(final['delivered_at'])
        self.assertEqual(final['recipient_host'], 'beta')
        self.assertEqual([agent['host'] for agent in AgentRegistry(self.stores['alpha']).list()], ['alpha'])

    def test_lost_ack_replays_same_message_id_and_returns_current_state(self):
        sent = self.send()
        self.links['alpha'].failures['beta'] = ['lost_ack']
        result = self.services['alpha'].pump()
        self.assertEqual(result['deferred'], 1)
        Mailbox(self.stores['beta']).handle(self.agents['beta'], [sent['message_id']])
        self.services['alpha'].pump(now=time.time()+1000)
        self.assertEqual(self.current('alpha', sent['message_id'])['state'], 'handled')
        self.assertEqual(self.stores['beta'].doctor()['counts']['messages'], 1)
        self.assertEqual(self.services['alpha'].outbox()[0]['attempts'], 2)

    def test_malformed_json_kind_is_typed_refusal_without_partial_accept(self):
        self.send()
        payload = json.loads(self.services['alpha'].outbox()[0]['payload'])
        for kind in ([], {}, None, 7):
            payload['message']['kind'] = kind
            result = self.wire('beta','accept',self.services['alpha'].envelope(payload),expected=1)
            self.assertEqual(result['error']['code'],'invalid_kind')
        self.assertEqual(self.stores['beta'].doctor()['counts']['messages'],0)
        self.assertEqual(self.stores['beta'].doctor()['counts']['dedupe_receipts'],0)

    def test_changed_payload_rejected_after_accept_and_after_body_retention(self):
        sent = self.send()
        item = self.services['alpha'].outbox()[0]
        payload = json.loads(item['payload'])
        accepted = self.wire('beta', 'accept', self.services['alpha'].envelope(payload))
        self.assertEqual(accepted['state'], 'queued')
        changed = json.loads(item['payload'])
        changed['message']['body'] = 'altered'
        refusal = self.wire('beta', 'accept', self.services['alpha'].envelope(changed), expected=1)
        self.assertEqual(refusal['error']['code'], 'message_conflict')
        Mailbox(self.stores['beta']).handle(self.agents['beta'], [sent['message_id']])
        self.stores['beta'].retention(now=time.time()+91*86400)
        replay = self.wire('beta', 'accept', self.services['alpha'].envelope(payload))
        self.assertEqual(replay['state'], 'handled')
        refusal = self.wire('beta', 'accept', self.services['alpha'].envelope(changed), expected=1)
        self.assertEqual(refusal['error']['code'], 'message_conflict')
        self.assertEqual(self.stores['beta'].doctor()['counts']['messages'], 0)

    def test_receipt_cannot_change_already_resolved_terminal(self):
        sent = self.send()
        self.services['alpha'].pump()
        incoming = public_receipt(self.current('beta', sent['message_id']))
        changed = {**incoming, 'recipient_terminal_id':'other-terminal', 'updated_at':time.time()+10}
        result = self.wire('alpha','receipt',self.services['beta'].envelope(changed),expected=1)
        self.assertEqual(result['error']['code'],'invalid_receipt')
        self.assertEqual(self.current('alpha',sent['message_id'])['recipient_terminal_id'],'term-beta')

    def test_receipts_reordered_and_final_conflicts_do_not_regress(self):
        sent = self.send()
        self.services['alpha'].pump()
        queued = public_receipt(self.current('beta', sent['message_id']))
        handled = Mailbox(self.stores['beta']).handle(self.agents['beta'], [sent['message_id']])['receipts'][0]
        self.wire('alpha', 'receipt', self.services['beta'].envelope(public_receipt(handled)))
        self.wire('alpha', 'receipt', self.services['beta'].envelope(queued))
        conflict = {**public_receipt(handled), 'state': 'withdrawn', 'updated_at': time.time()+10}
        self.wire('alpha', 'receipt', self.services['beta'].envelope(conflict))
        self.assertEqual(self.current('alpha', sent['message_id'])['state'], 'handled')
        self.assertEqual(self.current('alpha', sent['message_id'])['handled_at'], handled['handled_at'])

    def test_delivered_receipt_cannot_turn_into_withdrawn(self):
        sent = self.send()
        self.services['alpha'].pump()
        claim = Mailbox(self.stores['beta']).claim(terminal_id=self.agents['beta']['terminal_id'],
                agent_id=self.agents['beta']['agent_id'], generation=self.agents['beta']['generation'])
        delivered = Mailbox(self.stores['beta']).started(**{key: claim[key] for key in ('claim_id','agent_id','generation')},
                                                       evidence='fixture:actual-context')['receipts'][0]
        self.wire('alpha', 'receipt', self.services['beta'].envelope(public_receipt(delivered)))
        conflict = {**public_receipt(delivered), 'state': 'undeliverable', 'updated_at': time.time()+10}
        self.wire('alpha', 'receipt', self.services['beta'].envelope(conflict))
        self.assertEqual(self.current('alpha', sent['message_id'])['state'], 'delivered')

    def test_wrong_peer_uuid_or_receipt_route_refused(self):
        sent = self.send()
        item = self.services['alpha'].outbox()[0]
        bad = {**self.services['alpha'].envelope(json.loads(item['payload'])), 'transport_uuid': str(uuid.uuid4())}
        self.assertEqual(self.wire('beta', 'accept', bad, expected=1)['error']['code'], 'peer_identity_conflict')
        self.services['alpha'].pump()
        incoming = public_receipt(self.current('beta', sent['message_id']))
        self.assertEqual(self.wire('alpha', 'receipt', self.services['gamma'].envelope(incoming), expected=1)['error']['code'], 'wrong_receipt_host')

    def test_unattempted_remote_withdraw_cancels_without_contact(self):
        sent = self.send()
        withdrawn = Mailbox(self.stores['alpha']).withdraw(sent['message_id'], sender=self.agents['alpha'])
        self.assertTrue(withdrawn['withdrawn'])
        self.assertEqual(self.services['alpha'].outbox()[0]['state'], 'cancelled')
        self.services['alpha'].pump()
        self.assertEqual(self.stores['beta'].doctor()['counts']['messages'], 0)

    def test_attempted_remote_withdraw_waits_for_authoritative_recipient(self):
        sent = self.send()
        self.links['alpha'].failures['beta'] = ['lost_ack']
        self.services['alpha'].pump()
        result = Mailbox(self.stores['alpha']).withdraw(sent['message_id'], sender=self.agents['alpha'])
        self.assertTrue(result['withdraw_requested'])
        self.drain()
        self.assertEqual(self.current('alpha', sent['message_id'])['state'], 'withdrawn')
        self.assertEqual(self.current('beta', sent['message_id'])['state'], 'withdrawn')

    def test_withdraw_before_accept_is_permanent_tombstone(self):
        sent = self.send()
        current = public_receipt(self.current('alpha', sent['message_id']))
        result = self.wire('beta', 'withdraw', self.services['alpha'].envelope(current))
        self.assertEqual(result['state'], 'withdrawn')
        item = self.services['alpha'].outbox()[0]
        replay = self.wire('beta', 'accept', self.services['alpha'].envelope(json.loads(item['payload'])))
        self.assertEqual(replay['state'], 'withdrawn')
        self.assertEqual(self.stores['beta'].doctor()['counts']['messages'], 0)

    def test_fifo_transient_blocks_one_peer_but_other_peer_progresses(self):
        first, second, other = self.send(), self.send(), self.send(target='gamma')
        self.links['alpha'].failures['beta'] = ['transient']
        self.services['alpha'].pump()
        self.assertEqual(self.current('alpha', first['message_id'])['state'], 'pending')
        self.assertEqual(self.current('alpha', second['message_id'])['state'], 'pending')
        self.assertEqual(self.current('alpha', other['message_id'])['state'], 'queued')
        self.assertEqual(self.stores['beta'].doctor()['counts']['messages'], 0)
        self.drain()
        with self.stores['beta'].read() as connection:
            ids = [row[0] for row in connection.execute('SELECT message_id FROM messages ORDER BY rowid')]
        self.assertEqual(ids, [first['message_id'], second['message_id']])

    def test_permanent_failure_unblocks_later_items_and_retry_preserves_id(self):
        first, second = self.send(), self.send()
        self.links['alpha'].failures['beta'] = ['permanent']
        result = self.services['alpha'].pump()
        self.assertEqual((result['failed'], result['sent']), (1,1))
        self.assertEqual(self.current('alpha', second['message_id'])['state'], 'queued')
        failed = self.services['alpha'].outbox()[0]
        self.assertEqual(json.loads(failed['last_error'])['code'], 'invalid_message')
        self.services['alpha'].retry(failed['item_id'])
        self.drain()
        self.assertEqual(self.current('alpha', first['message_id'])['state'], 'queued')
        self.assertEqual(self.services['alpha'].outbox()[0]['item_id'], failed['item_id'])

    def test_disjoint_settings_and_equal_version_ties_converge(self):
        alpha, beta = RepoSettings(self.stores['alpha']), RepoSettings(self.stores['beta'])
        alpha.mutate(self.repo_id, 'mode', 'manual')
        beta.mutate(self.repo_id, 'thresholds', {**DEFAULT_THRESHOLDS, 'idle_minutes': 4})
        self.drain()
        for store in self.stores.values():
            current = RepoSettings(store).show(self.repo_id)
            self.assertEqual(current['mode'], 'manual')
            self.assertEqual(current['settings']['thresholds']['value']['idle_minutes'], 4)
        # Both writers start from the same observed versions, then diverge.
        alpha.mutate(self.repo_id, 'mode', 'auto')
        beta.mutate(self.repo_id, 'mode', 'off')
        self.drain()
        for store in self.stores.values():
            current = RepoSettings(store).show(self.repo_id)
            self.assertEqual(current['mode'], 'off')
            self.assertTrue(current['conflicts'])

    def test_concurrent_initial_homes_converge_without_waiting_for_offline_peer(self):
        alpha, beta = RepoSettings(self.stores['alpha']), RepoSettings(self.stores['beta'])
        a = alpha.mutate(self.repo_id, 'mode', 'auto')
        b = beta.mutate(self.repo_id, 'mode', 'auto')
        self.assertEqual(a['home_host'], 'alpha')
        self.assertEqual(b['home_host'], 'beta')
        with self.stores['alpha'].read() as connection:
            self.assertEqual(alpha.main(connection, self.repo_id)['host'], 'alpha')
        old_computed = a['settings']['computed_main']
        self.drain()
        for store in self.stores.values():
            self.assertEqual(RepoSettings(store).show(self.repo_id)['home_host'], 'beta')
        stale = {**old_computed, 'version': 999}
        result = self.wire('beta', 'setting', self.services['alpha'].envelope(stale))
        self.assertFalse(result['applied'])
        with self.stores['beta'].read() as connection:
            self.assertEqual(beta.main(connection, self.repo_id)['host'], 'beta')

    def test_stale_main_close_cannot_clear_later_designation_and_only_main_host_clears(self):
        settings = RepoSettings(self.stores['beta'])
        settings.mutate(self.repo_id, 'mode', 'manual')
        old = settings.mutate(self.repo_id, 'main', {'host':'beta','terminal_id':'term-beta'})['settings']['main']
        self.drain()
        AgentRegistry(self.stores['beta']).reconcile({'panes':[], 'agents':[]})
        self.assertFalse(RepoSettings(self.stores['alpha']).clear_closed_main(self.repo_id, stamp(old)))
        self.assertTrue(settings.clear_closed_main(self.repo_id, stamp(old)))
        clear = settings.show(self.repo_id)['settings']['main']
        alpha = RepoSettings(self.stores['alpha'])
        alpha.mutate(self.repo_id, 'main', {'host':'gamma','terminal_id':'term-gamma'})
        # Delay the close packet until after a newer user designation.
        result = alpha.accept(clear, 'beta')
        self.assertFalse(result['applied'])
        self.assertEqual(result['settings']['main']['value']['host'], 'gamma')
        self.assertFalse(settings.clear_closed_main(self.repo_id, stamp(old)))

    def test_home_offline_stays_pending_and_no_main_is_visible_permanent_failure(self):
        home = RepoSettings(self.stores['beta'])
        home.mutate(self.repo_id, 'mode', 'manual')
        self.drain()
        symbolic = self.services['alpha'].resolve_recipient('main', self.agents['alpha'])
        sent = Mailbox(self.stores['alpha']).send(self.agents['alpha'], symbolic, body='Main message')
        self.links['alpha'].failures['beta'] = ['transient']
        self.services['alpha'].pump()
        self.assertEqual(self.current('alpha', sent['message_id'])['state'], 'pending')
        self.services['alpha'].pump(now=time.time()+1000)
        failed = next(item for item in self.services['alpha'].outbox() if item['message_id']==sent['message_id'])
        self.assertEqual(failed['state'], 'failed')
        self.assertEqual(json.loads(failed['last_error'])['code'], 'main_unavailable')

    def test_symbolic_home_relays_to_third_host_receipts_and_withdraw_follow_route(self):
        home = RepoSettings(self.stores['beta'])
        home.mutate(self.repo_id, 'mode', 'manual')
        home.mutate(self.repo_id, 'main', {'host':'gamma','terminal_id':'term-gamma'})
        self.drain()
        symbolic = self.services['alpha'].resolve_recipient('main', self.agents['alpha'])
        sent = Mailbox(self.stores['alpha']).send(self.agents['alpha'], symbolic, body='Relayed message')
        self.services['alpha'].pump()
        self.assertEqual(self.current('alpha', sent['message_id'])['state'], 'pending')
        self.drain()
        self.assertEqual(self.current('alpha', sent['message_id'])['recipient_host'], 'gamma')
        self.assertEqual(self.current('alpha', sent['message_id'])['state'], 'queued')
        # A later main selection cannot move an accepted message's resolution.
        home.mutate(self.repo_id, 'main', {'host':'beta','terminal_id':'term-beta'})
        result = Mailbox(self.stores['alpha']).withdraw(sent['message_id'], sender=self.agents['alpha'])
        self.assertTrue(result['withdraw_requested'])
        self.drain()
        self.assertEqual(self.current('alpha', sent['message_id'])['state'], 'withdrawn')
        self.assertEqual(self.current('gamma', sent['message_id'])['state'], 'withdrawn')

    def test_worker_observes_designated_main_terminal_even_after_agent_returns_to_shell(self):
        store = self.stores['alpha']
        settings = RepoSettings(store)
        settings.mutate(self.repo_id,'mode','manual')
        settings.mutate(self.repo_id,'main',{'host':'alpha','terminal_id':'term-alpha'})
        self.drain()
        AgentRegistry(store).reconcile({'panes':self.snapshots['alpha']['panes'],'agents':[]})
        self.assertTrue(maintenance_pending(store))
        AgentRegistry(store).reconcile({'panes':[],'agents':[]})
        self.assertTrue(settings.clear_closed_main(self.repo_id,stamp(settings.show(self.repo_id)['settings']['main'])))
        self.drain()
        self.assertFalse(maintenance_pending(store))

    def test_worker_flock_is_single_instance_and_empty_worker_exits(self):
        store = self.stores['alpha']
        environment = {**self.environment, 'HERDR_AGENT_LEDGER_DIR': str(store.directory)}
        with worker_lock(store) as acquired:
            self.assertTrue(acquired)
            result = subprocess.run([sys.executable,str(CLI),'ledger','worker','once'], env=environment,
                                    capture_output=True,text=True,check=False)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertEqual(json.loads(result.stdout)['reason'],'already_running')
        self.assertFalse(maintenance_pending(store))
        result = subprocess.run([sys.executable,str(CLI),'ledger','worker','run'], env=environment,
                                capture_output=True,text=True,check=False,timeout=10)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(json.loads(result.stdout)['reason'],'no_pending_work')
        self.assertFalse((store.directory/'worker.pid').exists())

    def test_peer_identity_alias_mapping_and_setup_are_public(self):
        peer = self.services['alpha'].peer('b')
        self.assertEqual(peer['canonical_name'],'beta')
        with self.assertRaises(LedgerError):
            self.services['alpha'].add_peer(canonical_name='beta',uuid=str(uuid.uuid4()),ssh_alias='b',remote_ham_root=str(ROOT))
        environment = {**self.environment,'HERDR_AGENT_LEDGER_DIR':str(self.root/'new-host')}
        result = subprocess.run([sys.executable,str(CLI),'ledger','setup','--name','new-canonical'],env=environment,
                                capture_output=True,text=True,check=False)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(json.loads(result.stdout)['host']['canonical_name'],'new-canonical')


if __name__ == '__main__':
    unittest.main()
