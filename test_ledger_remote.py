"""Real two/three-store CLI wire tests with transport fault injection, not DB replication."""
import ham_test_support  # Protect the live ledger before product imports.

import json
import os
from pathlib import Path
import shlex
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
from ledger_remote import RemoteLedger, SSHTransport
from ledger_settings import DEFAULT_THRESHOLDS, RepoSettings
from ledger_status import AgentRegistry, repository_identity
from ledger_store import LedgerError, LedgerStore, public_receipt, queue_outbox, receipt
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
        if command == 'status-export':
            # Fixtures reconcile their own snapshots; the CLI must not query a
            # real Herdr registry while resolving an explicit remote address.
            payload = {**payload, 'refresh': False}
        envelope = RemoteLedger(self.owner).envelope(payload)
        arguments = [sys.executable, str(CLI), 'ledger', command]
        if command == 'status-export':
            arguments.append('--wire')
        result = subprocess.run(arguments,
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

    def show_settings(self, host, repo_id=None):
        return self.settings_for(host).show(
            repo_id or self.repo_id, host, self.agents[host]['workspace_id'])

    def add_agent(self, host, terminal_id, workspace_id):
        pane = {'terminal_id': terminal_id, 'pane_id': workspace_id+':'+terminal_id,
                'workspace_id': workspace_id}
        raw = {**pane, 'agent': 'codex', 'name': terminal_id, 'agent_status': 'idle',
               'agent_session': {'value': 'session-'+terminal_id}, 'cwd': str(self.repo)}
        snapshot = {'panes': [*self.snapshots[host]['panes'], pane],
                    'agents': [*self.snapshots[host]['agents'], raw]}
        self.snapshots[host] = snapshot
        registry = AgentRegistry(self.stores[host])
        registry.reconcile(snapshot, refresh_display=False)
        return next(agent for agent in registry.list() if agent['terminal_id'] == terminal_id)

    def legacy_message(self, recipient_host='alpha'):
        message_id = str(uuid.uuid4())
        sender = self.agents['alpha']
        return {'message_id': message_id, 'thread_id': message_id, 'repo_id': self.repo_id,
                'reply_to': None, 'origin_host': 'alpha', 'sender': sender['agent_id'],
                'sender_label': sender['alias'], 'kind': 'fyi', 'hop': 0, 'body': 'Legacy main',
                'subject': '', 'created_at': time.time(), 'recipient_host': recipient_host,
                'recipient_terminal_id': None, 'recipient_address': 'main'}

    def send(self, source='alpha', target='beta', **options):
        return Mailbox(self.stores[source]).send(self.agents[source], self.agents[target], body='Immutable remote body 한글', **options)

    def test_ssh_invoke_status_export_finds_herdr_without_login_path(self):
        home = self.root / "remote home"
        path = self.root / "remote-path"
        path.mkdir()
        (path / "python3").symlink_to(sys.executable)
        (path / "env").symlink_to("/usr/bin/env")
        binary = home / ".local/bin/herdr"
        binary.parent.mkdir(parents=True)
        snapshot = json.dumps({"result": {"snapshot": {"panes": [], "agents": []}}})
        binary.write_text("#!/bin/sh\nprintf '%s\\n' " + shlex.quote(snapshot) + "\n")
        binary.chmod(0o700)
        environment = {**self.environment, "HOME": str(home), "PATH": str(path)}
        environment.pop("HERDR_BIN_PATH", None)
        transport = SSHTransport(self.stores["alpha"], self.environment)
        peer = next(peer for peer in self.services["alpha"].peers()
                    if peer["canonical_name"] == "beta")
        run = subprocess.run

        def run_remote_shell_locally(command, **kwargs):
            # Keep the production SSH argv/remote-shell construction, replacing
            # only the SSH connection with an isolated non-login local shell.
            self.assertEqual(command[0], "ssh")
            self.assertEqual(command[-2], "b")
            shell = shlex.split(command[-1])
            self.assertEqual(shell[:2], ["sh", "-c"])
            return run(["/bin/sh", *shell[1:]], env=environment, **kwargs)

        with mock.patch("ledger_remote.subprocess.run", side_effect=run_remote_shell_locally):
            result = transport._invoke(peer, "status-export",
                                       self.services["alpha"].envelope({}))

        self.assertEqual(result["protocol"], 1)
        self.assertEqual(result["host"]["canonical_name"], "beta")
        self.assertEqual(result["agents"], [])

    def test_exported_remote_receiver_absence_warns_but_unknown_does_not(self):
        beta = self.stores["beta"]
        with beta.transaction() as connection:
            connection.execute("UPDATE agents SET client='omp',extension_present=0 WHERE active=1")
        recipient = self.services["alpha"].query_status("beta", self.repo_id, refresh=False)["agents"][0]
        sent = Mailbox(self.stores["alpha"], self.environment).send(
            self.agents["alpha"], recipient, body="Remote receiver absent")
        self.assertEqual(sent["state"], "pending")
        self.assertEqual(sent["warnings"][0]["code"], "recipient_no_receiver")
        self.assertEqual(sent["warnings"][0]["client"], "omp")
        self.assertIn("beta", sent["warnings"][0]["message"])
        for client, present in (("omp", None), ("omp", 1), ("claude", None), ("codex", 0)):
            with self.subTest(client=client, present=present):
                exported = {**recipient, "client": client}
                if present is None:
                    exported.pop("extension_present", None)
                else:
                    exported["extension_present"] = present
                result = Mailbox(self.stores["alpha"], self.environment).send(
                    self.agents["alpha"], exported, body="No absence evidence")
                self.assertNotIn("warnings", result)

    def test_fyi_remote_ready_receiver_advisory_requires_current_known_context(self):
        self.settings_for("beta").mutate(self.repo_id, "mode", "auto")
        with self.stores["beta"].transaction() as connection:
            connection.execute("UPDATE agents SET client='omp',extension_present=1 WHERE active=1")
        mailbox = Mailbox(self.stores["alpha"], self.environment)
        for state in ("idle", "done", "working", "blocked", "unknown"):
            with self.subTest(state=state):
                with self.stores["beta"].transaction() as connection:
                    connection.execute("UPDATE agent_status SET turn_state=?", (state,))
                recipient = self.services["alpha"].resolve_recipient("beta/beta-owl", self.agents["alpha"])
                sent = mailbox.send(self.agents["alpha"], recipient, body="Remote decision")
                self.assertEqual(sent["state"], "pending")
                self.assertEqual("fyi_no_wake" in [item["code"] for item in sent.get("warnings", [])],
                                 state in {"idle", "done"})
        recipient = {**recipient, "turn_state": "idle"}
        cases = ({"stale": True}, {"turn_state": None}, {"active": 0},
                 {"extension_present": None}, {"client": "claude"}, {"role": None})
        for fields in cases:
            with self.subTest(fields=fields):
                sent = mailbox.send(self.agents["alpha"], {**recipient, **fields}, body="Unknown or ineligible")
                self.assertNotIn("fyi_no_wake", [item["code"] for item in sent.get("warnings", [])])
        for state in ("idle", "done"):
            with self.subTest(action_state=state):
                sent = mailbox.send(self.agents["alpha"], {**recipient, "turn_state": state},
                                    kind="action", body="Continue now")
                self.assertNotIn("fyi_no_wake", [item["code"] for item in sent.get("warnings", [])])
        unknown = dict(recipient)
        unknown.pop("turn_state")
        sent = mailbox.send(self.agents["alpha"], unknown, body="Status omitted by older peer")
        self.assertNotIn("fyi_no_wake", [item["code"] for item in sent.get("warnings", [])])

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
            before = self.show_settings(host, repo_id)['settings']
            self.assertEqual(before['mode']['value'], 'auto')
            self.assertNotIn('computed_main', before)
            with store.read() as connection:
                queued = connection.execute(
                    "SELECT count(*) FROM outbox WHERE json_extract(payload,'$.repo_id')=?", (repo_id,)).fetchone()[0]
            self.assertEqual(queued, 4)  # Only repo-wide home and mode, to two peers.
            registry.reconcile(observed, refresh_display=False)
            self.assertEqual(self.show_settings(host, repo_id)['settings'], before)
            with store.read() as connection:
                self.assertEqual(connection.execute(
                    "SELECT count(*) FROM outbox WHERE json_extract(payload,'$.repo_id')=?", (repo_id,)).fetchone()[0], queued)
        self.drain()
        for host, store in self.stores.items():
            current = self.show_settings(host, repo_id)
            self.assertEqual((current['mode'], current['home_host']), ('auto', 'gamma'))
            with store.read() as connection:
                self.assertEqual(self.settings_for(host).main(
                    connection, repo_id, host, self.agents[host]['workspace_id'])['host'], host)

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
                seed = self.show_settings('gamma', repo_id)['settings']['mode']
                # The seeding host sorts AFTER the explicit writer: using its
                # real origin would wrongly beat the existing version-1 choice.
                result = self.wire('alpha', 'setting', self.services['gamma'].envelope(seed))
                self.assertFalse(result['applied'])
                result = self.wire('gamma', 'setting', self.services['alpha'].envelope(explicit))
                self.assertTrue(result['applied'])
                self.drain()
                for host in self.stores:
                    current = self.show_settings(host, repo_id)
                    self.assertEqual(current['settings']['mode'], explicit)
                    self.assertEqual(current['mode'], mode)
                if mode != 'auto':
                    conflicts = self.show_settings('alpha', repo_id)['conflicts']
                    self.assertTrue(any(item['field'] == 'mode' and item['winner_host'] == 'alpha'
                                        and item['loser_host'] == '!default' for item in conflicts))

    def test_disjoint_settings_and_equal_version_ties_converge(self):
        alpha, beta = self.settings_for('alpha'), self.settings_for('beta')
        alpha.mutate(self.repo_id, 'mode', 'manual')
        beta.mutate(self.repo_id, 'thresholds', {**DEFAULT_THRESHOLDS, 'idle_minutes': 4})
        self.drain()
        for host in self.stores:
            current = self.show_settings(host)
            self.assertEqual(current['mode'], 'manual')
            self.assertEqual(current['settings']['thresholds']['value']['idle_minutes'], 4)
        # Both writers start from the same observed versions, then diverge.
        alpha.mutate(self.repo_id, 'mode', 'auto')
        beta.mutate(self.repo_id, 'mode', 'off')
        self.drain()
        for host in self.stores:
            current = self.show_settings(host)
            self.assertEqual(current['mode'], 'off')
            self.assertTrue(current['conflicts'])

    def test_concurrent_initial_homes_converge_without_changing_workspace_mains(self):
        alpha, beta = self.settings_for('alpha'), self.settings_for('beta')
        alpha.mutate(self.repo_id, 'mode', 'auto')
        beta.mutate(self.repo_id, 'mode', 'auto')
        self.assertEqual(self.show_settings('alpha')['home_host'], 'alpha')
        self.assertEqual(self.show_settings('beta')['home_host'], 'beta')
        self.drain()
        for host in self.stores:
            current = self.show_settings(host)
            self.assertEqual(current['home_host'], 'beta')
            self.assertEqual(current['main'], {'host': host, 'terminal_id': 'term-'+host})


    def test_stale_explicit_main_is_inert_without_rewriting_workspace_settings(self):
        settings = self.settings_for('beta')
        settings.mutate(self.repo_id, 'mode', 'manual')
        settings.mutate(self.repo_id, 'main', {'host': 'beta', 'terminal_id': 'term-beta'},
                        host='beta', workspace_id='w2')
        before = self.show_settings('beta')['settings']['main']
        AgentRegistry(self.stores['beta']).reconcile({'panes': [], 'agents': []}, refresh_display=False)
        after = self.show_settings('beta')
        self.assertIsNone(after['main'])
        self.assertEqual(after['settings']['main'], before)
        self.drain()
        self.assertNotIn('main', self.show_settings('alpha')['settings'])


    def test_main_unresolved_names_sender_workspace_before_any_message_is_stored(self):
        self.settings_for('alpha').mutate(self.repo_id, 'mode', 'manual')
        self.settings_for('beta').mutate(self.repo_id, 'main',
            {'host': 'beta', 'terminal_id': 'term-beta'}, host='beta', workspace_id='w2')
        self.drain()
        with self.assertRaises(LedgerError) as failure:
            self.services['alpha'].resolve_recipient('main', self.agents['alpha'])
        self.assertEqual(failure.exception.code, 'main_unresolved')
        self.assertIn('w1', failure.exception.message)
        self.assertIn('alpha', failure.exception.message)
        self.assertEqual(self.stores['alpha'].doctor()['counts']['messages'], 0)


    def test_main_resolution_uses_sender_workspace_and_never_contacts_repo_home(self):
        self.settings_for('beta').mutate(self.repo_id, 'mode', 'auto')
        self.drain()
        self.links['alpha'].failures['beta'] = ['transient']
        worktree_agent = self.add_agent('alpha', 'worktree-main', 'worktree')
        service = self.services['alpha']
        for sender in (self.agents['alpha'], worktree_agent):
            target = service.resolve_recipient('main', sender)
            self.assertEqual((target['host'], target['terminal_id']),
                             ('alpha', sender['terminal_id']))
            sent = Mailbox(self.stores['alpha']).send(sender, target, body='Workspace main')
            self.assertEqual(sent['state'], 'queued')
            with self.stores['alpha'].read() as connection:
                value = connection.execute(
                    'SELECT original_host,original_terminal,original_address FROM message_routes WHERE message_id=?',
                    (sent['message_id'],)).fetchone()
            self.assertEqual(tuple(value), ('alpha', sender['terminal_id'], 'main'))
        self.assertFalse(any(item['kind'] == 'message' for item in service.outbox()))
        self.assertEqual(self.links['alpha'].failures['beta'], ['transient'])


    def test_main_send_time_target_survives_later_designation(self):
        store = self.stores['alpha']
        other = self.add_agent('alpha', 'other-main', 'w1')
        settings = self.settings_for('alpha')
        settings.mutate(self.repo_id, 'mode', 'manual')
        settings.mutate(self.repo_id, 'main', {'host': 'alpha', 'terminal_id': other['terminal_id']},
                        host='alpha', workspace_id='w1')
        target = self.services['alpha'].resolve_recipient('main', self.agents['alpha'])
        sent = Mailbox(store).send(self.agents['alpha'], target, body='Concrete main')
        settings.mutate(self.repo_id, 'main', {'host': 'alpha', 'terminal_id': 'term-alpha'},
                        host='alpha', workspace_id='w1')
        self.assertEqual(self.current('alpha', sent['message_id'])['recipient_terminal_id'], other['terminal_id'])
        claim = Mailbox(store).claim(terminal_id=other['terminal_id'], agent_id=other['agent_id'],
                                    generation=other['generation'])
        self.assertEqual([row['message_id'] for row in claim['messages']], [sent['message_id']])
        self.assertEqual(store.doctor()['counts']['messages'], 1)

    def test_main_same_workspace_id_on_different_hosts_stays_host_local(self):
        self.settings_for('alpha').mutate(self.repo_id, 'mode', 'auto')
        self.drain()
        beta = self.add_agent('beta', 'beta-w1', 'w1')
        for host, sender in (('alpha', self.agents['alpha']), ('beta', beta)):
            target = self.services[host].resolve_recipient('main', sender)
            self.assertEqual((target['host'], target['terminal_id']), (host, sender['terminal_id']))

    def test_ambiguous_sender_workspace_does_not_fall_back_to_other_workspace(self):
        self.settings_for('alpha').mutate(self.repo_id, 'mode', 'auto')
        self.add_agent('alpha', 'second-w1-main-checkout', 'w1')
        other = self.add_agent('alpha', 'only-w2', 'w2')
        with self.assertRaises(LedgerError) as failure:
            self.services['alpha'].resolve_recipient('main', self.agents['alpha'])
        self.assertEqual(failure.exception.code, 'main_unresolved')
        self.assertIn('w1', failure.exception.message)
        self.assertEqual(self.services['alpha'].resolve_recipient('main', other)['terminal_id'],
                         other['terminal_id'])

    def test_legacy_symbolic_submit_requires_upgrading_every_host(self):
        for host in ('alpha', 'beta'):
            with self.subTest(host=host):
                with self.assertRaises(LedgerError) as failure:
                    with self.stores[host].transaction() as connection:
                        self.services[host].submit(connection, self.legacy_message(host))
                self.assertEqual(failure.exception.code, 'legacy_symbolic_main')
                self.assertIn('upgrade every host', failure.exception.message)
                self.assertEqual(self.stores[host].doctor()['counts']['messages'], 0)

    def test_legacy_remote_origin_and_home_relay_share_explicit_upgrade_error(self):
        value = self.legacy_message('beta')
        for terminal in (None, 'term-beta'):
            for source in ('alpha', 'gamma'):
                with self.subTest(terminal=terminal, source=source):
                    payload = {'message': value, 'resolved': {
                        'recipient_host': 'beta', 'recipient_terminal_id': terminal,
                        'recipient_address': 'main'}}
                    result = self.wire('beta', 'accept', self.services[source].envelope(payload), expected=1)
                    self.assertEqual(result['error']['code'], 'legacy_symbolic_main')
                    self.assertIn('upgrade every host', result['error']['message'])
        self.assertEqual(self.stores['beta'].doctor()['counts']['messages'], 0)

    def test_stored_legacy_home_route_fails_visibly_with_upgrade_error(self):
        self.settings_for('beta').mutate(self.repo_id, 'mode', 'auto')
        self.drain()
        value = self.legacy_message('beta')
        resolved = {key: value[key] for key in
                    ('recipient_host', 'recipient_terminal_id', 'recipient_address')}
        with self.stores['alpha'].transaction() as connection:
            Mailbox(self.stores['alpha'])._enqueue(connection, value, resolved=resolved, forward='beta')
            queue_outbox(connection, 'beta', 'message', {'message': value, 'resolved': resolved},
                         value['message_id'])
        self.services['alpha'].pump(now=time.time()+1000)
        failed = next(item for item in self.services['alpha'].outbox()
                      if item['message_id'] == value['message_id'])
        self.assertEqual(failed['state'], 'failed')
        error = json.loads(failed['last_error'])
        self.assertEqual(error['code'], 'legacy_symbolic_main')
        self.assertIn('upgrade every host', error['message'])
        self.assertEqual(self.stores['beta'].doctor()['counts']['messages'], 0)

    def test_concrete_main_wire_cannot_change_target_or_use_home_relay(self):
        sent = self.send()
        payload = json.loads(next(item for item in self.services['alpha'].outbox()
                                  if item['message_id'] == sent['message_id'])['payload'])
        payload['message']['recipient_address'] = 'main'
        payload['resolved']['recipient_address'] = 'main'
        changed = {**payload, 'resolved': {**payload['resolved'], 'recipient_terminal_id': 'another'}}
        result = self.wire('beta', 'accept', self.services['alpha'].envelope(changed), expected=1)
        self.assertEqual(result['error']['code'], 'invalid_recipient')
        result = self.wire('beta', 'accept', self.services['gamma'].envelope(payload), expected=1)
        self.assertEqual(result['error']['code'], 'wrong_origin')
        accepted = self.wire('beta', 'accept', self.services['alpha'].envelope(payload))
        self.assertEqual(accepted['recipient_terminal_id'], 'term-beta')

    def test_explicit_address_remains_cross_workspace_and_cross_host(self):
        other = self.add_agent('alpha', 'other-workspace', 'another')
        for address, expected in (('local/'+other['alias'], other),
                                  ('beta/'+self.agents['beta']['alias'], self.agents['beta'])):
            target = self.services['alpha'].resolve_recipient(address, self.agents['alpha'])
            self.assertEqual(target['agent_id'], expected['agent_id'])
            sent = Mailbox(self.stores['alpha']).send(self.agents['alpha'], target, body='Explicit target')
            self.assertEqual(sent['recipient_terminal_id'], expected['terminal_id'])


    def test_worker_observes_enabled_repo_after_designated_agent_returns_to_shell(self):
        store = self.stores['alpha']
        settings = self.settings_for('alpha')
        settings.mutate(self.repo_id, 'mode', 'manual')
        settings.mutate(self.repo_id, 'main', {'host': 'alpha', 'terminal_id': 'term-alpha'},
                        host='alpha', workspace_id='w1')
        self.drain()
        AgentRegistry(store).reconcile({'panes': self.snapshots['alpha']['panes'], 'agents': []},
                                       refresh_display=False)
        self.assertTrue(maintenance_pending(store))
        self.assertIsNone(self.show_settings('alpha')['main'])
        AgentRegistry(store).reconcile({'panes': [], 'agents': []}, refresh_display=False)
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

    def test_workspace_settings_mutation_projects_after_commit(self):
        store = self.stores['beta']
        observed = []

        def runner(command):
            with store.read() as connection:
                fields = RepoSettings.workspace_fields(connection, self.repo_id, 'beta', 'w2')
                observed.append(fields.get('main', {}).get('value'))
            return subprocess.CompletedProcess(list(command), 1, '', 'isolated display failure')

        settings = RepoSettings(store, environment=self.environment, display_runner=runner)
        target = {'host': 'beta', 'terminal_id': 'term-beta'}
        result = settings.mutate(self.repo_id, 'main', target, host='beta', workspace_id='w2')
        self.assertTrue(observed)
        self.assertTrue(all(value == target for value in observed))
        self.assertTrue(result['display']['failed'])


    def test_old_peer_scoped_setting_outbox_completes_instead_of_failing(self):
        settings = [
            ('main', {'host': 'alpha', 'terminal_id': 'term-alpha'}),
            ('members', [{'host': 'alpha', 'terminal_id': 'term-alpha'}]),
            ('computed_main', {'target': None, 'home_stamp':
                              {'version': 1, 'origin_host': 'alpha', 'value': 'alpha'}}),
        ]
        with self.stores['alpha'].transaction() as connection:
            for field, value in settings:
                queue_outbox(connection, 'beta', 'setting', {
                    'repo_id': self.repo_id, 'field': field, 'value': value,
                    'version': 1, 'origin_host': 'alpha', 'changed_by': 'user',
                    'changed_at': time.time()})
        result = self.services['alpha'].pump(now=time.time()+1000)
        self.assertEqual(result, {'sent': 3, 'failed': 0, 'deferred': 0})
        self.assertTrue(all(item['state'] == 'sent' for item in self.services['alpha'].outbox()))
        self.assertEqual(self.show_settings('beta')['settings'], {})
        self.assertEqual(self.services['beta'].outbox(), [])

    def test_old_peer_workspace_settings_are_successful_noops_without_forwarding(self):
        self.settings_for('beta').mutate(self.repo_id, 'mode', 'auto')
        self.drain()
        before = self.show_settings('beta')
        outbox = self.services['beta'].outbox()
        values = {
            'main': {'host': 'alpha', 'terminal_id': 'term-alpha'},
            'members': [{'host': 'alpha', 'terminal_id': 'term-alpha'}],
            'computed_main': {'target': {'host': 'alpha', 'terminal_id': 'term-alpha'},
                              'home_stamp': {'version': 1, 'origin_host': 'alpha', 'value': 'alpha'}},
        }
        with mock.patch('ledger_display.refresh', side_effect=AssertionError('no-op must not redraw')):
            for field, value in values.items():
                with self.subTest(field=field):
                    setting = {'repo_id': self.repo_id, 'field': field, 'value': value,
                               'version': 999, 'origin_host': 'alpha',
                               'changed_by': 'user', 'changed_at': time.time()}
                    result = self.wire('beta', 'setting', self.services['alpha'].envelope(setting))
                    self.assertEqual(result['protocol'], 1)
                    self.assertFalse(result['applied'])
                    local_result = self.services['beta'].receive(
                        'setting', self.services['alpha'].envelope(setting))
                    self.assertFalse(local_result['applied'])
        self.assertEqual(self.show_settings('beta'), before)
        self.assertEqual(self.services['beta'].outbox(), outbox)


    def test_peer_bootstrap_publishes_only_repo_wide_fields(self):
        settings = self.settings_for('alpha')
        settings.mutate(self.repo_id, 'mode', 'manual')
        settings.mutate(self.repo_id, 'thresholds', DEFAULT_THRESHOLDS)
        target = {'host': 'alpha', 'terminal_id': 'term-alpha'}
        settings.mutate(self.repo_id, 'main', target, host='alpha', workspace_id='w1')
        settings.mutate(self.repo_id, 'members', [target], host='alpha', workspace_id='w1')
        self.drain()
        store = self.stores['alpha']
        with store.transaction() as connection:
            connection.execute("DELETE FROM peer_options WHERE canonical_name='gamma'")
            connection.execute("DELETE FROM peers WHERE canonical_name='gamma'")
        before = {item['item_id'] for item in self.services['alpha'].outbox()}
        self.services['alpha'].add_peer(canonical_name='gamma', uuid=self.stores['gamma'].host_uuid,
            ssh_alias='c', remote_ham_root=str(ROOT), remote_ledger_dir=str(self.stores['gamma'].directory))
        bootstrapped = [json.loads(item['payload']) for item in self.services['alpha'].outbox()
                        if item['item_id'] not in before]
        self.assertEqual({value['field'] for value in bootstrapped}, {'home', 'mode', 'thresholds'})
        self.assertTrue(all(value['repo_id'] == self.repo_id for value in bootstrapped))


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
