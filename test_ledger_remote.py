"""Real two/three-store CLI wire tests with transport fault injection, not DB replication."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import threading
from concurrent.futures import ThreadPoolExecutor
import unittest
from unittest import mock
import uuid

from ledger_mail import Mailbox
from ledger_remote import RemoteLedger
from ledger_settings import DEFAULT_THRESHOLDS, RepoSettings, stamp
from ledger_status import AgentRegistry, repository_identity
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
                            'HERDR_SOCKET_PATH': str(self.root/'herdr-isolated.sock'),
                            'HERDR_AGENT_MESSENGER_SSH_CONFIG': str(ssh_config),
                            'HERDR_AGENT_MESSENGER_SSH_HOSTS_FILE': str(allowlist)}
        self.environment.pop('HERDR_AGENT_CANONICAL_HOST', None)
        self.stores = {host: LedgerStore(self.root/host, canonical_name=host) for host in ('alpha', 'beta', 'gamma')}
        self.agents = {}
        self.snapshots = {}
        for index, (host, store) in enumerate(self.stores.items()):
            # Existing scenarios model a repo known before auto-default rollout.
            repo_id = repository_identity(str(self.repo), host)["repo_id"]
            with store.transaction() as connection:
                connection.execute("INSERT INTO repos VALUES (?,NULL,?)", (repo_id, 1.0))
            pane = {'terminal_id': 'term-'+host, 'pane_id': f'w{index+1}:p1', 'workspace_id': f'w{index+1}'}
            raw = {**pane, 'agent': 'codex', 'name': host+'-owl', 'agent_status': 'idle',
                   'agent_session': {'value': 'session-'+host}, 'cwd': str(self.repo)}
            self.snapshots[host] = {'panes': [pane], 'agents': [raw]}
            registry = AgentRegistry(store)
            registry.reconcile(self.snapshots[host], refresh_display=False)
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

    def settings_for(self, host):
        return RepoSettings(self.stores[host], environment=self.environment,
                            display_runner=self.display_runner)

    @staticmethod
    def display_runner(command):
        return subprocess.CompletedProcess(list(command), 1, "", "isolated fixture")

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

    def test_claim_winner_refuses_remote_withdraw_without_a_false_final_receipt(self):
        sent = self.send()
        item = next(item for item in self.services['alpha'].outbox() if item['kind']=='message')
        withdrawal = public_receipt(self.current('alpha',sent['message_id']))
        self.wire('beta','accept',self.services['alpha'].envelope(json.loads(item['payload'])))
        agent = self.agents['beta']
        mailbox = Mailbox(self.stores['beta'])
        claim = mailbox.claim(terminal_id=agent['terminal_id'],agent_id=agent['agent_id'],
                              generation=agent['generation'])
        before = self.current('beta',sent['message_id'])
        refused = self.wire('beta','withdraw',self.services['alpha'].envelope(withdrawal))
        self.assertFalse(refused['withdrawn'])
        self.assertEqual(refused['state'],'queued')  # delivering stays receiver-internal
        self.assertEqual(refused['reason'],'already_claimed_or_final')
        self.assertEqual(self.current('beta',sent['message_id']),before)
        self.wire('alpha','receipt',self.services['beta'].envelope(refused))
        self.assertEqual(self.current('alpha',sent['message_id'])['state'],'queued')
        mailbox.started(**{key:claim[key] for key in ('claim_id','agent_id','generation')},
                        evidence='fixture:accepted-after-withdraw-refusal')
        mailbox.handle(agent,[sent['message_id']])
        self.drain()
        self.assertEqual(self.current('alpha',sent['message_id'])['state'],'handled')

    def test_unknown_withdraw_atomically_binds_tombstone_hash_before_concurrent_accept(self):
        sent = self.send()
        item = next(item for item in self.services['alpha'].outbox() if item['kind']=='message')
        payload = json.loads(item['payload'])
        withdrawal = public_receipt(self.current('alpha',sent['message_id']))
        store, service = self.stores['beta'], self.services['beta']
        tombstone_written, accept_started, release = threading.Event(), threading.Event(), threading.Event()
        original = Mailbox._withdraw
        def pause_withdraw(mailbox,connection,message_id,**options):
            result = original(mailbox,connection,message_id,**options)
            if mailbox.store is store:
                tombstone_written.set()
                if not release.wait(5):
                    raise AssertionError('withdraw scheduling seam was not released')
            return result
        def accept():
            accept_started.set()
            return service.receive('accept',self.services['alpha'].envelope(payload))
        with ThreadPoolExecutor(max_workers=2) as executor:
            with mock.patch.object(Mailbox,'_withdraw',pause_withdraw):
                withdrawn = executor.submit(service.receive,'withdraw',self.services['alpha'].envelope(withdrawal))
                try:
                    self.assertTrue(tombstone_written.wait(5))
                    accepted = executor.submit(accept)
                    self.assertTrue(accept_started.wait(5))
                    # No other reader can observe a tombstone without its hash.
                    with store.read() as connection:
                        self.assertEqual(connection.execute('SELECT count(*) FROM withdraw_tombstones').fetchone()[0],0)
                        self.assertEqual(connection.execute('SELECT count(*) FROM dedupe_receipts').fetchone()[0],0)
                finally:
                    release.set()
                outcome = withdrawn.result(timeout=5)
                replay = accepted.result(timeout=5)
        self.assertTrue(outcome['withdrawn'])
        self.assertEqual(replay['state'],'withdrawn')
        self.assertEqual(replay['payload_hash'],sent['payload_hash'])
        self.assertEqual(store.doctor()['counts']['messages'],0)
        altered = {**payload,'message':{**payload['message'],'body':'Changed after atomic withdrawal'}}
        conflict = self.wire('beta','accept',self.services['alpha'].envelope(altered),expected=1)
        self.assertEqual(conflict['error']['code'],'message_conflict')

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

    def test_seeded_auto_publishes_once_and_concurrent_homes_converge(self):
        repo = self.root / 'new-repo'
        subprocess.run(['git', 'init', '-q', str(repo)], check=True)
        subprocess.run(['git', '-C', str(repo), 'config', 'ham.repo-id', 'git:defaults/new'], check=True)
        repo_id = 'git:defaults/new'
        for host, store in self.stores.items():
            original = self.snapshots[host]
            observed = {**original, 'agents': [{**original['agents'][0], 'cwd': str(repo)}]}
            registry = AgentRegistry(store)
            registry.reconcile(observed, refresh_display=False)
            before = self.settings_for(host).show(repo_id)['settings']
            self.assertEqual(before['mode']['value'], 'auto')
            self.assertEqual(before['computed_main']['value']['target']['host'], host)
            with store.read() as connection:
                queued = connection.execute(
                    "SELECT count(*) FROM outbox WHERE json_extract(payload,'$.repo_id')=?", (repo_id,)).fetchone()[0]
            self.assertEqual(queued, 6)  # home, mode and computed main, to two peers.
            registry.reconcile(observed, refresh_display=False)
            self.settings_for(host).reconcile_local(refresh_display=False)
            self.assertEqual(self.settings_for(host).show(repo_id)['settings'], before)
            with store.read() as connection:
                self.assertEqual(connection.execute(
                    "SELECT count(*) FROM outbox WHERE json_extract(payload,'$.repo_id')=?", (repo_id,)).fetchone()[0], queued)
        self.drain()
        for host, store in self.stores.items():
            current = self.settings_for(host).show(repo_id)
            self.assertEqual((current['mode'], current['home_host']), ('auto', 'gamma'))
            with store.read() as connection:
                self.assertEqual(self.settings_for(host).main(connection, repo_id)['host'], 'gamma')

    def test_explicit_peer_version_one_mode_beats_seed_in_both_arrival_orders(self):
        for mode in ('off', 'manual', 'auto'):
            with self.subTest(mode=mode):
                repo = self.root / ('explicit-' + mode)
                subprocess.run(['git', 'init', '-q', str(repo)], check=True)
                repo_id = 'git:defaults/' + mode
                subprocess.run(['git', '-C', str(repo), 'config', 'ham.repo-id', repo_id], check=True)
                explicit = {'repo_id': repo_id, 'field': 'mode', 'value': mode, 'version': 1,
                            'origin_host': 'alpha', 'changed_by': 'user', 'changed_at': 1.0}
                with self.stores['alpha'].transaction() as connection:
                    self.settings_for('alpha')._apply(connection, explicit)
                original = self.snapshots['gamma']
                observed = {**original, 'agents': [{**original['agents'][0], 'cwd': str(repo)}]}
                AgentRegistry(self.stores['gamma']).reconcile(observed, refresh_display=False)
                seed = self.settings_for('gamma').show(repo_id)['settings']['mode']
                # The seeding host sorts AFTER the explicit writer: using its
                # real origin would wrongly beat the existing version-1 choice.
                result = self.wire('alpha', 'setting', self.services['gamma'].envelope(seed))
                self.assertFalse(result['applied'])
                result = self.wire('gamma', 'setting', self.services['alpha'].envelope(explicit))
                self.assertTrue(result['applied'])
                self.drain()
                for host in self.stores:
                    current = self.settings_for(host).show(repo_id)
                    self.assertEqual(current['settings']['mode'], explicit)
                    self.assertEqual(current['mode'], mode)
                if mode != 'auto':
                    conflicts = self.settings_for('alpha').show(repo_id)['conflicts']
                    self.assertTrue(any(item['field'] == 'mode' and item['winner_host'] == 'alpha'
                                        and item['loser_host'] == '!default' for item in conflicts))

    def test_disjoint_settings_and_equal_version_ties_converge(self):
        alpha, beta = self.settings_for('alpha'), self.settings_for('beta')
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
        alpha, beta = self.settings_for('alpha'), self.settings_for('beta')
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
        settings = self.settings_for('beta')
        settings.mutate(self.repo_id, 'mode', 'manual')
        old = settings.mutate(self.repo_id, 'main', {'host':'beta','terminal_id':'term-beta'})['settings']['main']
        self.drain()
        AgentRegistry(self.stores['beta']).reconcile({'panes':[], 'agents':[]}, refresh_display=False)
        self.assertFalse(self.settings_for('alpha').clear_closed_main(self.repo_id, stamp(old)))
        self.assertTrue(settings.clear_closed_main(self.repo_id, stamp(old)))
        clear = settings.show(self.repo_id)['settings']['main']
        alpha = self.settings_for('alpha')
        alpha.mutate(self.repo_id, 'main', {'host':'gamma','terminal_id':'term-gamma'})
        # Delay the close packet until after a newer user designation.
        result = alpha.accept(clear, 'beta')
        self.assertFalse(result['applied'])
        self.assertEqual(result['settings']['main']['value']['host'], 'gamma')
        self.assertFalse(settings.clear_closed_main(self.repo_id, stamp(old)))

    def test_home_offline_stays_pending_and_no_main_is_visible_permanent_failure(self):
        home = self.settings_for('beta')
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
        home = self.settings_for('beta')
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

    def test_symbolic_home_can_return_to_another_terminal_on_origin_without_duplicate(self):
        store = self.stores['alpha']
        pane = {'terminal_id':'term-alpha-main','pane_id':'w1:p2','workspace_id':'w1'}
        agent = {**pane,'agent':'omp','name':'origin-main','agent_status':'idle',
                 'agent_session':{'value':'main-session'},'cwd':str(self.repo)}
        AgentRegistry(store).reconcile(
            {'panes':[*self.snapshots['alpha']['panes'],pane],
             'agents':[*self.snapshots['alpha']['agents'],agent]}, refresh_display=False)
        main = next(item for item in AgentRegistry(store).list() if item['terminal_id']==pane['terminal_id'])
        home = self.settings_for('beta')
        home.mutate(self.repo_id,'mode','manual')
        home.mutate(self.repo_id,'main',{'host':'alpha','terminal_id':main['terminal_id']})
        self.drain()
        symbolic = self.services['alpha'].resolve_recipient('main',self.agents['alpha'])
        sent = Mailbox(store).send(self.agents['alpha'],symbolic,body='Return to origin main')
        self.drain()
        current = self.current('alpha',sent['message_id'])
        self.assertEqual(current['state'],'queued')
        self.assertTrue(current['authoritative'])
        self.assertEqual(current['recipient_terminal_id'],main['terminal_id'])
        self.assertEqual(store.doctor()['counts']['messages'],1)
        claim = Mailbox(store).claim(terminal_id=main['terminal_id'],agent_id=main['agent_id'],generation=main['generation'])
        self.assertEqual([row['message_id'] for row in claim['messages']],[sent['message_id']])
        with store.read() as connection:
            claimed = dict(connection.execute('SELECT * FROM deliveries WHERE message_id=?',
                                              (sent['message_id'],)).fetchone())
        home_copy = public_receipt(self.current('beta',sent['message_id']))
        for delayed in (sent,home_copy,{**home_copy,'updated_at':time.time()+10}):
            result = self.wire('alpha','receipt',self.services['beta'].envelope(delayed))
            self.assertEqual(result['state'],'queued')
            self.assertIsNone(result['delivered_at'])
            with store.read() as connection:
                self.assertEqual(dict(connection.execute('SELECT * FROM deliveries WHERE message_id=?',
                                                         (sent['message_id'],)).fetchone()),claimed)
        Mailbox(store).started(claim_id=claim['claim_id'],agent_id=main['agent_id'],
                              generation=main['generation'],evidence='fixture:accepted-origin-context')
        Mailbox(store).handle(main,[sent['message_id']])
        self.drain()
        self.assertEqual(self.current('beta',sent['message_id'])['state'],'handled')
        item = next(item for item in self.services['beta'].outbox() if item['kind']=='message')
        replay = json.loads(item['payload'])
        store.retention(now=time.time()+91*86400)
        retained = self.wire('alpha','accept',self.services['beta'].envelope(replay))
        self.assertEqual(retained['state'],'handled')
        self.assertEqual(retained['payload_hash'],sent['payload_hash'])
        self.assertEqual(store.doctor()['counts']['messages'],0)

    def test_worker_observes_designated_main_terminal_even_after_agent_returns_to_shell(self):
        store = self.stores['alpha']
        settings = RepoSettings(store, environment=self.environment, display_runner=self.display_runner)
        settings.mutate(self.repo_id,'mode','manual')
        settings.mutate(self.repo_id,'main',{'host':'alpha','terminal_id':'term-alpha'})
        self.drain()
        AgentRegistry(store).reconcile({'panes':self.snapshots['alpha']['panes'],'agents':[]}, refresh_display=False)
        self.assertTrue(maintenance_pending(store))
        AgentRegistry(store).reconcile({'panes':[],'agents':[]}, refresh_display=False)
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

    def test_settings_mutation_projects_only_after_commit_and_reports_failure(self):
        store = self.stores['alpha']
        observed = []

        def runner(command):
            with store.read() as connection:
                observed.append(RepoSettings.fields(connection, self.repo_id)['mode']['value'])
            return subprocess.CompletedProcess(list(command), 1, '', 'isolated display failure')

        settings = RepoSettings(store, environment=self.environment, display_runner=runner)
        result = settings.mutate(self.repo_id, 'mode', 'manual')
        self.assertTrue(observed)
        self.assertEqual(set(observed), {'manual'})
        self.assertTrue(result['display']['failed'])
        self.assertEqual(result['warnings'][0]['code'], 'display_refresh_failed')

    def test_remote_setting_receipt_projects_after_commit(self):
        store = self.stores['beta']
        observed = []

        def project(ledger, environment=None, *, runner=None):
            with ledger.read() as connection:
                observed.append(RepoSettings.fields(connection, self.repo_id)['mode']['value'])
            return {'updated': [], 'failed': ['isolated-pane']}

        setting = {
            'repo_id': self.repo_id,
            'field': 'mode',
            'value': 'manual',
            'version': 1,
            'origin_host': 'alpha',
            'changed_by': 'user',
            'changed_at': time.time(),
        }
        envelope = self.services['alpha'].envelope(setting)
        with mock.patch('ledger_display.refresh', side_effect=project):
            result = self.services['beta'].receive('setting', envelope)
        self.assertEqual(observed, ['manual'])
        self.assertEqual(result['display']['failed'], ['isolated-pane'])
        self.assertEqual(result['warnings'][0]['code'], 'display_refresh_failed')

    def test_closed_main_projection_runs_after_settings_commit(self):
        store = self.stores['beta']
        observed = []

        def runner(command):
            with store.read() as connection:
                fields = RepoSettings.fields(connection, self.repo_id)
                observed.append(fields.get('main', {}).get('value'))
            return subprocess.CompletedProcess(list(command), 1, '', 'isolated display failure')

        pane = {'terminal_id': 'term-beta-secondary', 'pane_id': 'w2:p2', 'workspace_id': 'w2'}
        raw = {**pane, 'agent': 'codex', 'name': 'beta-secondary', 'agent_status': 'idle',
               'agent_session': {'value': 'secondary-session'}, 'cwd': str(self.repo)}
        snapshot = {'panes': [*self.snapshots['beta']['panes'], pane],
                    'agents': [*self.snapshots['beta']['agents'], raw]}
        AgentRegistry(store).reconcile(snapshot, refresh_display=False)
        self.snapshots['beta'] = snapshot
        settings = RepoSettings(store, environment=self.environment, display_runner=runner)
        settings.mutate(self.repo_id, 'mode', 'manual')
        designated = settings.mutate(
            self.repo_id, 'main', {'host': 'beta', 'terminal_id': self.agents['beta']['terminal_id']}
        )['settings']['main']
        remaining = {'panes': [pane], 'agents': [raw]}
        AgentRegistry(store).reconcile(remaining, refresh_display=False)
        observed.clear()

        self.assertTrue(settings.clear_closed_main(self.repo_id, stamp(designated)))
        self.assertTrue(observed)
        self.assertEqual(set(observed), {None})

    def test_reconcile_local_reports_deferred_redraw_after_commit(self):
        store = self.stores['beta']
        settings = self.settings_for('beta')
        settings.mutate(self.repo_id, 'mode', 'manual')
        settings.mutate(self.repo_id, 'main', {'host': 'beta', 'terminal_id': self.agents['beta']['terminal_id']})
        AgentRegistry(store).reconcile({'panes': [], 'agents': []}, refresh_display=False)

        result = settings.reconcile_local(refresh_display=False, environment=self.environment)
        self.assertEqual(result, {'changed': True, 'display': None})
        self.assertIsNone(settings.show(self.repo_id)['settings']['main']['value'])
        self.assertEqual(settings.reconcile_local(refresh_display=False),
                         {'changed': False, 'display': None})

    def test_reconcile_local_returns_postcommit_display_result(self):
        store = self.stores['beta']
        settings = self.settings_for('beta')
        settings.mutate(self.repo_id, 'mode', 'manual')
        settings.mutate(self.repo_id, 'main', {'host': 'beta', 'terminal_id': self.agents['beta']['terminal_id']})
        AgentRegistry(store).reconcile({'panes': [], 'agents': []}, refresh_display=False)
        observed = []

        def project(ledger, environment=None, *, runner=None):
            with ledger.read() as connection:
                observed.append(RepoSettings.fields(connection, self.repo_id)['main']['value'])
            return {'updated': [], 'failed': ['isolated-pane']}

        with mock.patch('ledger_display.refresh', side_effect=project):
            result = settings.reconcile_local(environment=self.environment)
        self.assertEqual(result, {'changed': True, 'display': {'updated': [], 'failed': ['isolated-pane']}})
        self.assertEqual(observed, [None])

    def test_status_export_and_recipient_resolution_do_not_scan_by_default(self):
        service = self.services["beta"]
        envelope = self.services["alpha"].envelope({"repo_id": self.repo_id})
        calls = []

        class StoredPeerTransport:
            def call(_, peer, command, payload):
                calls.append(payload)
                return service.receive(command, self.services["alpha"].envelope(payload))

        reader = RemoteLedger(self.stores["alpha"], self.environment, StoredPeerTransport())
        with mock.patch("resource_scan.scan_host_resources", side_effect=AssertionError("unexpected scan")):
            exported = service.status_export(self.repo_id)
            received = service.receive("status-export", envelope)
            self.assertNotIn("resources", exported)
            self.assertNotIn("servers", exported["agents"][0])
            self.assertNotIn("resources", received)
            self.assertEqual(exported["agents"][0]["unread"], 0)
            self.assertEqual(reader.resolve_recipient(
                "beta/"+self.agents["beta"]["alias"], self.agents["alpha"])["agent_id"],
                self.agents["beta"]["agent_id"])
        self.assertEqual(calls, [{"repo_id": self.repo_id}])

    def test_status_wire_resources_are_explicitly_opted_in(self):
        service = self.services["beta"]
        scanned = {"listeners": [], "serve": [], "error": "fixture scan failure"}
        with mock.patch("resource_scan.scan_host_resources", return_value=scanned) as scanner:
            exported = service.receive("status-export", self.services["alpha"].envelope(
                {"repo_id": self.repo_id, "refresh": False, "resources": True}))
        scanner.assert_called_once_with()
        self.assertEqual(exported["resources"], {
            "servers_unowned": [], "dead_serve": [], "stale_devices": [],
            "error": "fixture scan failure"})
        self.assertEqual(exported["agents"][0]["servers"], [])

    def test_status_export_counts_queued_and_delivering_by_repo_and_terminal(self):
        beta_store = self.stores['beta']
        first = self.send()
        self.drain()
        beta_agent = self.agents['beta']
        claimed = Mailbox(beta_store).claim(
            terminal_id=beta_agent['terminal_id'],
            agent_id=beta_agent['agent_id'],
            generation=beta_agent['generation'],
        )
        self.assertEqual([item['message_id'] for item in claimed['messages']], [first['message_id']])
        second = self.send()
        self.drain()

        secondary_pane = {
            'terminal_id': 'term-beta-secondary',
            'pane_id': 'w2:p2',
            'workspace_id': 'w2',
        }
        secondary_agent_data = {
            **secondary_pane,
            'agent': 'codex',
            'name': 'beta-secondary',
            'agent_status': 'idle',
            'agent_session': {'value': 'secondary-session'},
            'cwd': str(self.repo),
        }
        beta_snapshot = {
            'panes': [*self.snapshots['beta']['panes'], secondary_pane],
            'agents': [*self.snapshots['beta']['agents'], secondary_agent_data],
        }
        AgentRegistry(beta_store).reconcile(beta_snapshot, refresh_display=False)
        self.snapshots['beta'] = beta_snapshot
        secondary = next(
            item for item in AgentRegistry(beta_store).list()
            if item['terminal_id'] == secondary_pane['terminal_id']
        )
        third = Mailbox(self.stores['alpha']).send(
            self.agents['alpha'], secondary, body='Other terminal unread'
        )
        self.drain()

        other_repo = self.root/'other-repo'
        subprocess.run(['git', 'init', '-q', str(other_repo)], check=True)
        subprocess.run(['git', '-C', str(other_repo), 'remote', 'add', 'origin',
                        'https://example.test/team/other.git'], check=True)
        other_agents = {}
        for host in ('alpha', 'beta'):
            pane = {'terminal_id': f'term-{host}-other-repo',
                    'pane_id': f'w{host}:p2', 'workspace_id': f'w{host}'}
            raw = {**pane, 'agent': 'codex', 'name': host+'-other',
                   'agent_status': 'idle', 'agent_session': {'value': host+'-other-session'},
                   'cwd': str(other_repo)}
            snapshot = {'panes': [*self.snapshots[host]['panes'], pane],
                        'agents': [*self.snapshots[host]['agents'], raw]}
            AgentRegistry(self.stores[host]).reconcile(snapshot, refresh_display=False)
            self.snapshots[host] = snapshot
            other_agents[host] = next(
                item for item in AgentRegistry(self.stores[host]).list()
                if item['terminal_id'] == pane['terminal_id']
            )
        fourth = Mailbox(self.stores['alpha']).send(
            other_agents['alpha'], other_agents['beta'], body='Other repository unread'
        )
        self.drain()

        repo_export = self.services['beta'].status_export(self.repo_id)
        unread = {item['terminal_id']: item['unread'] for item in repo_export['agents']}
        self.assertEqual(unread[beta_agent['terminal_id']], 2)
        self.assertEqual(unread[secondary['terminal_id']], 1)
        other_export = self.services['beta'].status_export(other_agents['beta']['repo_id'])
        self.assertEqual([item['unread'] for item in other_export['agents']], [1])
        self.assertEqual(self.current('beta', first['message_id'])['state'], 'delivering')
        self.assertEqual(self.current('beta', second['message_id'])['state'], 'queued')
        self.assertEqual(self.current('beta', third['message_id'])['state'], 'queued')
        self.assertEqual(self.current('beta', fourth['message_id'])['state'], 'queued')
        Mailbox(beta_store).started(
            claim_id=claimed['claim_id'], agent_id=beta_agent['agent_id'],
            generation=beta_agent['generation'], evidence='fixture:delivery-started',
        )
        Mailbox(beta_store).handle(beta_agent, [first['message_id']])
        refreshed = self.services['beta'].status_export(self.repo_id)
        updated = {item['terminal_id']: item['unread'] for item in refreshed['agents']}
        self.assertEqual(updated[beta_agent['terminal_id']], 1)

    def test_remote_receipt_preserves_operator_confirmation_and_original_claim(self):
        sent = self.send()
        self.drain()
        recipient = self.agents['beta']
        claim = Mailbox(self.stores['beta']).claim(
            terminal_id=recipient['terminal_id'],
            agent_id=recipient['agent_id'],
            generation=recipient['generation'],
        )
        Mailbox(self.stores['beta']).started(
            claim_id=claim['claim_id'], agent_id=recipient['agent_id'],
            generation=recipient['generation'], evidence='journal-entry:original',
        )
        remote = self.current('beta', sent['message_id'])
        operator_via = remote['via'] or 'wake'
        operator_receipt = {
            **remote,
            'state': 'delivered',
            'updated_at': time.time() + 100,
            'via': operator_via,
            'delivered_agent_id': remote['delivered_agent_id'] or recipient['agent_id'],
            'confirmation_source': 'operator',
            'evidence': 'user-confirmed-context-application',
        }
        origin = self.stores['alpha']
        with origin.transaction() as connection:
            self.services['alpha'].apply_receipt(connection, operator_receipt, 'beta')

        later_native_receipt = {
            **operator_receipt,
            'state': 'handled',
            'handled_at': time.time() + 200,
            'updated_at': time.time() + 200,
            'via': 'native-journal',
            'delivered_agent_id': 'different-later-agent',
            'confirmation_source': 'native',
            'evidence': 'journal-entry:later',
        }
        with origin.transaction() as connection:
            self.services['alpha'].apply_receipt(connection, later_native_receipt, 'beta')
            current = receipt(connection, sent['message_id'])
        self.assertEqual(current['state'], 'handled')
        self.assertEqual(current['confirmation_source'], 'operator')
        self.assertEqual(current['evidence'], 'user-confirmed-context-application')
        self.assertEqual(current['via'], operator_via)
        self.assertEqual(current['delivered_agent_id'], recipient['agent_id'])

        stale_receipt = {
            **later_native_receipt,
            'updated_at': time.time() + 300,
            'confirmation_source': None,
            'evidence': None,
            'via': None,
            'delivered_agent_id': None,
        }
        with origin.transaction() as connection:
            self.services['alpha'].apply_receipt(connection, stale_receipt, 'beta')
            current = receipt(connection, sent['message_id'])
        self.assertEqual(current['confirmation_source'], 'operator')
        self.assertEqual(current['evidence'], 'user-confirmed-context-application')
        self.assertEqual(current['via'], operator_via)
        self.assertEqual(current['delivered_agent_id'], recipient['agent_id'])

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
