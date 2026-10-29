import tests.ham_test_support as ham_test_support  # Protect the live ledger before product imports.
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
import uuid
from unittest import mock

from ham.ledger.mail import Mailbox
from ham.ledger.native_wake import (FAILURE_BACKOFF_SECONDS, UNAVAILABLE_BACKOFF_SECONDS, NativeWaker)
from ham.ledger.runtime import BUNDLE_MESSAGES, FYI_NOW_QUIET_SECONDS, NativeReceiver, wake_capable
from ham.ledger.settings import DEFAULT_THRESHOLDS, RepoSettings
from ham.ledger.status import AgentRegistry
from ham.ledger.store import LedgerError, LedgerStore
from ham.ledger.wake_clients import Observation, WakeDeliveryFailed, WakeTarget, WakeUnavailable
from tests.test_ledger_core import snapshot


class FakeAdapter:
    """Scriptable stand-in for a wake_<client> module; records every call."""

    def __init__(self, client, session_ref, journal):
        self.client = client
        self.session_ref = session_ref
        self.journal = str(journal)
        self.pid = 4242
        self.discover_error = None
        self.deliver_error = None
        self.observation = lambda **_kwargs: Observation('pending', 0)
        self.discovered, self.delivered, self.observed = [], [], []

    def discover(self, agent, *, environment=None):
        self.discovered.append(dict(agent))
        if self.discover_error is not None:
            raise self.discover_error
        return WakeTarget(self.client, self.session_ref, self.pid, self.journal, 100, {})

    def deliver(self, target, text):
        self.delivered.append((target, text))
        if self.deliver_error is not None:
            raise self.deliver_error

    def observe(self, target, **kwargs):
        self.observed.append((target, kwargs))
        return self.observation(**kwargs)


class WakeFixture(unittest.TestCase):
    client = 'codex'

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root/'repo'
        self.repo.mkdir()
        self.store = LedgerStore(self.root/'ledger', canonical_name='alpha')
        self.registry = AgentRegistry(self.store)
        self.registry.reconcile(snapshot(self.repo, recipient_session=str(self.root/'native.jsonl')),
                                refresh_display=False)
        agents = self.registry.list()
        self.sender = next(a for a in agents if a['terminal_id'] == 'term-a')
        self.recipient = next(a for a in agents if a['terminal_id'] == 'term-b')
        self.mail = Mailbox(self.store)
        self.session_ref = str(uuid.uuid4())
        self.adapter = FakeAdapter(self.client, self.session_ref, self.root/'journal.jsonl')
        self.environment = {**os.environ, 'HERDR_AGENT_LEDGER_AUTOSTART': '0'}
        self.receiver = NativeReceiver(self.store, wake_adapters={'claude': self.adapter, 'codex': self.adapter},
                                       environment=self.environment)
        self.now = time.time()
        self.waker = self.new_waker()
        self.settings = RepoSettings(self.store, display_runner=lambda command: subprocess.CompletedProcess(
            list(command), 1, '', 'isolated fixture'))
        self.thresholds = {**DEFAULT_THRESHOLDS, 'idle_minutes': 0, 'coalesce_seconds': 0}
        self.set_thresholds()
        self.settings.mutate(self.recipient['repo_id'], 'mode', 'auto')
        with self.store.transaction() as connection:
            if self.client == 'claude':
                connection.execute("UPDATE agents SET client='claude',extension_present=1,session_ref=?,"
                                   "native_session_pid=4242 WHERE agent_id=?",
                                   (self.session_ref, self.recipient['agent_id']))
            else:
                connection.execute("UPDATE agents SET client='codex',extension_present=0 WHERE agent_id=?",
                                   (self.recipient['agent_id'],))
        self.set_turn_state('idle')

    def new_waker(self):
        receiver = NativeReceiver(self.store, wake_adapters={'claude': self.adapter, 'codex': self.adapter},
                                  environment=self.environment)
        return NativeWaker(self.store, receiver, clock=lambda: self.now, environment=self.environment)

    def set_thresholds(self, **changes):
        self.thresholds = {**self.thresholds, **changes}
        self.settings.mutate(self.recipient['repo_id'], 'thresholds', self.thresholds)

    def set_turn_state(self, state, *, log=True):
        """Change the turn state; ``log`` mirrors the status_log row Herdr events write."""
        with self.store.transaction() as connection:
            connection.execute('UPDATE agent_status SET turn_state=? WHERE agent_id=?',
                               (state, self.recipient['agent_id']))
            if log:
                connection.execute("INSERT INTO status_log(agent_id,field,value,at) VALUES (?,'turn_state',?,?)",
                                   (self.recipient['agent_id'], state, self.now))

    def send(self, kind='action', body='Please look 한글'):
        sent = self.mail.send(self.sender, self.recipient, kind=kind, body=body)
        self.now = max(self.now, time.time())  # The fake clock never runs behind message creation.
        return sent

    @property
    def agent_id(self):
        return self.recipient['agent_id']

    def deliveries(self):
        with self.store.read() as connection:
            return [dict(row) for row in connection.execute(
                'SELECT * FROM deliveries ORDER BY updated_at,message_id')]

    def states(self):
        return sorted({row['state'] for row in self.deliveries()})

    def claims(self):
        with self.store.read() as connection:
            return [dict(row) for row in connection.execute('SELECT * FROM native_claims')]

    def wake_log(self):
        with self.store.read() as connection:
            return connection.execute('SELECT count(*) FROM wake_log WHERE agent_id=?',
                                      (self.agent_id,)).fetchone()[0]

    def sweep(self, advance=0):
        self.now += advance
        return self.waker.sweep()

    def skipped(self, result):
        return result['skipped'].get(self.agent_id)

    def wake_now(self):
        self.send()
        result = self.sweep()
        self.assertEqual(result['claimed'], 1, result)
        return result


class WakeGateTests(WakeFixture):
    def assertRefused(self, reason, advance=0):
        result = self.sweep(advance)
        self.assertEqual(self.skipped(result), reason)
        self.assertEqual(result['claimed'], 0)
        self.assertEqual(self.adapter.discovered, [])
        self.assertEqual(self.adapter.delivered, [])
        self.assertEqual(self.states(), ['queued'])
        self.assertEqual(self.claims(), [])

    def test_idle_participant_with_an_action_is_woken(self):
        self.send()
        result = self.sweep()
        self.assertEqual(result['claimed'], 1)
        self.assertEqual(len(self.adapter.delivered), 1)
        self.assertEqual(self.states(), ['delivering'])

    def test_done_counts_as_ready(self):
        self.set_turn_state('done')
        self.send()
        self.assertEqual(self.sweep()['claimed'], 1)

    def test_not_participating_agent_is_never_woken(self):
        self.send()
        self.settings.mutate(self.recipient['repo_id'], 'mode', 'off')
        self.assertRefused('not_participating')

    def test_manual_mode_non_member_is_never_woken(self):
        self.send()
        self.settings.mutate(self.recipient['repo_id'], 'mode', 'manual')
        self.assertRefused('not_participating')

    def test_busy_turn_states_are_never_woken(self):
        self.send()
        for state in ('working', 'blocked', 'unknown'):
            with self.subTest(state=state):
                self.set_turn_state(state)
                self.assertRefused('not_idle')

    def test_nothing_queued_does_not_probe_the_adapter(self):
        result = self.sweep()
        self.assertEqual((result['claimed'], result['skipped']), (0, {}))
        self.assertEqual(self.adapter.discovered, [])

    def forget_status_log(self):
        with self.store.transaction() as connection:
            connection.execute('DELETE FROM status_log')

    def test_quiet_proxy_falls_back_to_the_workers_first_ready_observation(self):
        self.set_thresholds(idle_minutes=3)
        self.send()
        self.forget_status_log()
        self.assertRefused('quiet')
        self.assertRefused('quiet', advance=179)
        self.assertEqual(self.sweep(advance=2)['claimed'], 1)

    def test_going_busy_resets_the_in_memory_quiet_period(self):
        self.set_thresholds(idle_minutes=3)
        self.send()
        self.forget_status_log()
        self.assertRefused('quiet')
        self.set_turn_state('working', log=False)
        self.assertRefused('not_idle', advance=170)
        self.set_turn_state('idle', log=False)
        self.assertRefused('quiet', advance=20)
        self.assertRefused('quiet', advance=170)
        self.assertEqual(self.sweep(advance=20)['claimed'], 1)

    def test_status_log_quiet_period_restarts_when_the_agent_goes_busy(self):
        self.set_thresholds(idle_minutes=3)
        self.send()
        self.assertRefused('quiet', advance=100)
        self.set_turn_state('working')
        self.assertRefused('not_idle', advance=100)
        self.set_turn_state('idle')
        self.assertRefused('quiet', advance=179)
        self.assertEqual(self.sweep(advance=2)['claimed'], 1)

    def test_durable_status_log_gives_the_quiet_period_after_a_worker_restart(self):
        self.set_thresholds(idle_minutes=3)
        self.send()
        with self.store.transaction() as connection:
            for value, at in (('working', self.now-1000), ('idle', self.now-200), ('done', self.now-100)):
                connection.execute("INSERT INTO status_log(agent_id,field,value,at) VALUES (?,'turn_state',?,?)",
                                   (self.agent_id, value, at))
        self.assertEqual(self.sweep()['claimed'], 1)

    def test_durable_ready_run_that_is_too_young_is_not_quiet(self):
        self.set_thresholds(idle_minutes=3)
        self.send()
        with self.store.transaction() as connection:
            for value, at in (('idle', self.now-900), ('working', self.now-120), ('idle', self.now-60)):
                connection.execute("INSERT INTO status_log(agent_id,field,value,at) VALUES (?,'turn_state',?,?)",
                                   (self.agent_id, value, at))
        self.assertRefused('quiet')
        self.assertEqual(self.sweep(advance=121)['claimed'], 1)

    def test_zero_idle_minutes_is_quiet_immediately(self):
        self.send()
        self.assertEqual(self.sweep()['claimed'], 1)

    def test_coalescing_delays_the_wake(self):
        self.set_thresholds(coalesce_seconds=60)
        self.send()
        self.assertRefused('coalescing')
        self.assertEqual(self.sweep(advance=61)['claimed'], 1)

    def test_young_ordinary_fyi_waits_for_the_fyi_delay_but_action_does_not(self):
        self.send(kind='fyi')
        self.assertRefused('coalescing')
        self.assertRefused('coalescing', advance=500)
        self.assertEqual(self.sweep(advance=200)['claimed'], 1)

    def test_hourly_limit_blocks_a_second_wake_until_the_window_frees(self):
        self.set_thresholds(wakes_per_hour=1)
        self.wake_now()
        self.adapter.observation = lambda **_k: Observation('accepted', 200, 'ev:1')
        self.assertEqual(len(self.sweep()['receipts']), 1)
        self.assertEqual(self.wake_log(), 1)
        self.send()
        result = self.sweep()
        self.assertEqual((self.skipped(result), result['claimed']), ('hourly_limit', 0))
        self.assertEqual(len(self.adapter.delivered), 1)

    def test_wakes_per_hour_zero_disables_wakes(self):
        self.set_thresholds(wakes_per_hour=0)
        self.send()
        self.assertRefused('hourly_limit')

    def test_hop_limit_keeps_over_hop_mail_queued(self):
        self.set_thresholds(max_hop=0)
        self.send()
        with self.store.transaction() as connection:
            connection.execute('UPDATE messages SET hop=1')
        self.assertRefused('no_action')

    def test_second_wake_is_blocked_while_a_claim_is_pending(self):
        self.wake_now()
        self.send(body='A second action')
        result = self.sweep()
        self.assertEqual((self.skipped(result), result['claimed']), ('claim_pending', 0))
        self.assertEqual(len(self.adapter.delivered), 1)
        self.assertEqual(sorted(self.states()), ['delivering', 'queued'])

    def test_a_pending_claim_of_another_delivery_path_also_blocks(self):
        self.send()
        with self.store.transaction() as connection:
            agent = dict(connection.execute('SELECT * FROM agents WHERE agent_id=?', (self.agent_id,)).fetchone())
            rows = self.receiver._eligible(connection, agent, 10, wake=False)
            claim = Mailbox._claim_messages(connection, rows, self.agent_id, agent['generation'], 'hook')
            connection.execute('INSERT INTO native_claims(claim_id,client,session_ref,marker,content_sha256,'
                               'created_at) VALUES (?,?,?,?,?,?)',
                               (claim['claim_id'], self.client, self.session_ref, 'marker', 'hash', self.now))
        self.send(body='Another')
        self.assertEqual(self.skipped(self.sweep()), 'claim_pending')
        self.assertEqual(self.adapter.delivered, [])

    def test_bundle_content_is_the_claim_marker_bundle_with_the_message(self):
        self.send(body='Exact body 한글')
        self.sweep()
        (_target, text), = self.adapter.delivered
        claim, = self.claims()
        self.assertTrue(text.startswith(claim['marker']+'\n'))
        self.assertIn('Exact body 한글', text)
        self.assertEqual(claim['content_sha256'], hashlib.sha256(text.encode()).hexdigest())
        self.assertEqual(claim['content_bytes'], len(text.encode()))
        self.assertEqual((claim['client'], claim['session_ref'], claim['transcript_path'],
                          claim['transcript_start_offset']),
                         (self.client, self.session_ref, self.adapter.journal, 100))

    def test_discovery_and_delivery_run_outside_any_ledger_transaction(self):
        writes = []

        def write_during(name):
            def call(*_args, **_kwargs):
                # A held write transaction would make this second writer fail with "database is locked".
                with self.store.transaction() as connection:
                    connection.execute("INSERT INTO status_log(agent_id,field,value,at) VALUES (?,'probe',?,0)",
                                       (self.agent_id, name))
                writes.append(name)
                raise WakeUnavailable('probe', name) if name == 'discover' else WakeDeliveryFailed(
                    'probe', name, definitely_not_delivered=True)
            return call
        self.adapter.discover = write_during('discover')
        self.send()
        self.sweep()
        self.adapter.discover = FakeAdapter.discover.__get__(self.adapter)
        self.adapter.deliver = write_during('deliver')
        self.sweep(advance=UNAVAILABLE_BACKOFF_SECONDS+1)
        self.assertEqual(writes, ['discover', 'deliver'])

    def test_working_idle_cycle_during_discovery_restarts_transaction_quiet_period(self):
        self.set_thresholds(idle_minutes=3)
        self.send()
        self.now += 181
        discover = self.adapter.discover

        def cycle_during_discovery(*args, **kwargs):
            self.set_turn_state('working')
            self.set_turn_state('idle')
            return discover(*args, **kwargs)

        self.adapter.discover = cycle_during_discovery
        result = self.sweep()
        self.assertEqual((result['claimed'], self.skipped(result)), (0, 'quiet'))
        self.assertEqual((self.states(), self.claims(), self.adapter.delivered), (['queued'], [], []))
        self.adapter.discover = discover
        self.assertEqual(self.skipped(self.sweep(advance=179)), 'quiet')
        self.assertEqual(self.sweep(advance=2)['claimed'], 1)


class WakeLifecycleTests(WakeFixture):
    def accept(self, offset=300):
        self.adapter.observation = lambda **_k: Observation('accepted', offset, 'ev:accepted')

    def test_pending_evidence_keeps_delivering_and_advances_the_scan_offset(self):
        self.wake_now()
        self.adapter.observation = lambda **_k: Observation('pending', 180)
        self.assertEqual(self.sweep()['receipts'], [])
        self.sweep()
        (_t1, first), (_t2, second) = self.adapter.observed
        self.assertEqual((first['offset'], second['offset']), (100, 180))
        self.assertEqual(self.states(), ['delivering'])
        self.assertEqual(self.wake_log(), 0)

    def test_observation_is_given_the_exact_claim_identity(self):
        self.wake_now()
        self.sweep()
        target, kwargs = self.adapter.observed[0]
        claim, = self.claims()
        delivered = self.adapter.delivered[0][1]
        self.assertEqual(target.pid, 4242)
        self.assertEqual((kwargs['marker'], kwargs['content'], kwargs['content_sha256'], kwargs['session_ref'],
                          kwargs['journal_path'], kwargs['pid']),
                         (claim['marker'], delivered, claim['content_sha256'], self.session_ref,
                          self.adapter.journal, 4242))

    def test_accepted_evidence_delivers_and_logs_the_wake(self):
        self.wake_now()
        self.accept()
        result = self.sweep()
        self.assertEqual(len(result['receipts']), 1)
        row, = self.deliveries()
        self.assertEqual((row['state'], row['via'], row['evidence'], row['delivered_agent_id']),
                         ('delivered', 'wake', 'ev:accepted', self.agent_id))
        self.assertEqual(self.wake_log(), 1)
        self.assertEqual(self.sweep()['receipts'], [])  # Nothing left to observe.
        self.assertEqual(len(self.adapter.observed), 1)

    def test_unavailable_adapter_claims_nothing(self):
        self.adapter.discover_error = WakeUnavailable('why_not', 'no socket')
        self.send()
        result = self.sweep()
        self.assertEqual(self.skipped(result), 'unavailable:why_not')
        self.assertEqual((self.states(), self.claims(), self.adapter.delivered), (['queued'], [], []))
        self.assertEqual(self.waker.unavailable[self.agent_id], ('why_not', 'no socket'))

    def test_unavailable_target_is_reprobed_after_the_short_backoff(self):
        self.adapter.discover_error = WakeUnavailable('why_not', 'no socket')
        self.send()
        self.sweep()
        self.assertEqual(self.skipped(self.sweep(advance=UNAVAILABLE_BACKOFF_SECONDS-1)), 'backoff')
        self.assertEqual(len(self.adapter.discovered), 1)
        self.adapter.discover_error = None
        self.assertEqual(self.sweep(advance=2)['claimed'], 1)
        self.assertNotIn(self.agent_id, self.waker.unavailable)

    def test_an_adapter_crash_in_discovery_is_unavailable_not_fatal(self):
        self.adapter.discover_error = RuntimeError('boom')
        self.send()
        self.assertEqual(self.skipped(self.sweep()), 'unavailable:discover_failed')
        self.assertEqual(self.states(), ['queued'])

    def test_definite_delivery_failure_releases_not_applied_and_backs_off(self):
        self.adapter.deliver_error = WakeDeliveryFailed('refused', 'connect refused', definitely_not_delivered=True)
        self.send()
        result = self.sweep()
        self.assertEqual((result['claimed'], result['released']), (1, 1))
        row, = self.deliveries()
        self.assertEqual((row['state'], row['claim_id'], row['claimed_agent_id'], row['via'], row['evidence']),
                         ('queued', None, None, None, 'wake:not-delivered:refused'))
        self.assertEqual(self.wake_log(), 0)
        self.assertEqual(self.skipped(self.sweep(advance=FAILURE_BACKOFF_SECONDS-1)), 'backoff')
        self.assertEqual(len(self.adapter.delivered), 1)
        self.adapter.deliver_error = None
        self.assertEqual(self.sweep(advance=2)['claimed'], 1)  # The same mail is retried.
        self.assertEqual(self.states(), ['delivering'])

    def test_delivery_failure_that_may_have_written_keeps_the_claim_and_observes_it(self):
        for error in (WakeDeliveryFailed('partial', 'half written', definitely_not_delivered=False),
                      RuntimeError('adapter crashed')):
            with self.subTest(error=type(error).__name__):
                self.setUp()
                self.adapter.deliver_error = error
                self.send()
                result = self.sweep()
                self.assertEqual((result['claimed'], result['released']), (1, 0))
                row, = self.deliveries()
                self.assertEqual(row['state'], 'delivering')
                self.assertTrue(row['evidence'].startswith('wake:delivery-uncertain:'))
                self.assertEqual(self.sweep(advance=FAILURE_BACKOFF_SECONDS+1)['claimed'], 0)
                self.assertEqual(len(self.adapter.delivered), 1)  # No second wake over a maybe-delivered claim.
                self.accept()
                self.assertEqual(len(self.sweep()['receipts']), 1)
                self.assertEqual(self.states(), ['delivered'])
                self.assertEqual(self.wake_log(), 1)

    def test_session_death_before_evidence_is_unknown_and_never_redelivered(self):
        self.wake_now()
        self.adapter.observation = lambda **_k: Observation('gone', 100)
        self.sweep()
        row, = self.deliveries()
        self.assertEqual((row['state'], row['hold_reason'], row['evidence']),
                         ('delivering', 'acceptance_unknown', 'wake:unknown:target_gone'))
        self.assertEqual(self.wake_log(), 0)
        observed = len(self.adapter.observed)
        self.sweep(advance=FAILURE_BACKOFF_SECONDS*2)
        self.assertEqual(len(self.adapter.observed), observed)  # Operator territory now.
        self.assertEqual(len(self.adapter.delivered), 1)

    def test_deactivated_agent_ends_as_unknown_without_observing(self):
        self.wake_now()
        with self.store.transaction() as connection:
            connection.execute('UPDATE agents SET active=0 WHERE agent_id=?', (self.agent_id,))
        self.sweep()
        row, = self.deliveries()
        self.assertEqual((row['state'], row['evidence']), ('delivering', 'wake:unknown:agent_changed'))
        self.assertEqual(self.adapter.observed, [])

    def test_changed_client_ends_as_unknown(self):
        self.wake_now()
        with self.store.transaction() as connection:
            connection.execute("UPDATE agents SET client='omp' WHERE agent_id=?", (self.agent_id,))
        self.sweep()
        self.assertEqual(self.deliveries()[0]['evidence'], 'wake:unknown:agent_changed')

    def test_observation_error_is_not_a_release_and_recovers(self):
        self.wake_now()

        def broken(**_kwargs):
            raise OSError('journal unreadable')
        self.adapter.observation = broken
        self.sweep()
        self.sweep()
        self.assertEqual(self.states(), ['delivering'])
        self.assertEqual(self.deliveries()[0]['evidence'], None)
        self.accept()
        self.assertEqual(len(self.sweep()['receipts']), 1)

    def test_worker_restart_scans_persisted_journal_and_rebuilds_the_exact_content(self):
        self.wake_now()
        delivered = self.adapter.delivered[0][1]
        restarted = self.new_waker()
        self.adapter.observation = lambda **_k: Observation('accepted', 300, 'ev:after-restart')
        result = restarted.sweep()
        self.assertEqual(len(result['receipts']), 1)
        (_target, kwargs), = self.adapter.observed
        self.assertEqual(kwargs['content'], delivered)
        self.assertEqual(kwargs['offset'], 100)  # The claim's own baseline, not a fresh one.
        self.assertIsNone(_target)
        self.assertIsNone(kwargs['pid'])
        self.assertEqual(len(self.adapter.discovered), 1)  # Acceptance needs no rediscovery.
        self.assertEqual(self.wake_log(), 1)

    def test_restart_with_a_tampered_bundle_never_observes(self):
        self.wake_now()
        with self.store.transaction() as connection:
            connection.execute('DROP TRIGGER IF EXISTS messages_immutable')
            connection.execute("UPDATE messages SET body='tampered'")
        self.assertEqual(self.new_waker().sweep()['receipts'], [])
        self.assertEqual(self.adapter.observed, [])
        self.assertEqual(self.states(), ['delivering'])

    def test_restart_with_unavailable_target_scans_before_waiting(self):
        self.wake_now()
        self.adapter.discover_error = WakeUnavailable('why_not', 'later')
        self.adapter.observation = lambda **kwargs: Observation('gone', kwargs['offset'])
        self.new_waker().sweep()
        (target, kwargs), = self.adapter.observed
        self.assertIsNone(target)
        self.assertEqual((kwargs['journal_path'], kwargs['offset']), (self.adapter.journal, 100))
        self.assertIsNone(self.deliveries()[0]['evidence'])
        self.assertEqual(self.states(), ['delivering'])

    def test_restart_accepts_prior_journal_when_current_session_is_dead_or_unavailable(self):
        self.wake_now()
        self.adapter.discover_error = WakeUnavailable('dead', 'native root exited')
        self.accept()
        result = self.new_waker().sweep()
        self.assertEqual(len(result['receipts']), 1)
        self.assertEqual(self.states(), ['delivered'])
        self.assertEqual(len(self.adapter.discovered), 1)
        self.assertEqual(self.wake_log(), 1)

    def test_restart_accepts_prior_thread_before_discovering_a_new_thread(self):
        self.wake_now()
        prior_session = self.session_ref
        self.adapter.session_ref = str(uuid.uuid4())
        self.accept()
        result = self.new_waker().sweep()
        self.assertEqual(len(result['receipts']), 1)
        self.assertEqual(self.adapter.observed[0][1]['session_ref'], prior_session)
        self.assertEqual(len(self.adapter.discovered), 1)
        self.assertEqual(self.states(), ['delivered'])

    def test_restart_exhausts_prior_journal_before_releasing_a_changed_session(self):
        self.wake_now()
        self.adapter.session_ref = str(uuid.uuid4())
        self.adapter.observation = lambda **kwargs: Observation(
            'pending' if kwargs['offset']==100 else 'gone', 200)
        restarted = self.new_waker()
        restarted.sweep()
        self.assertIsNone(self.deliveries()[0]['evidence'])
        self.assertEqual(len(self.adapter.discovered), 1)
        restarted.sweep()
        self.assertEqual(self.deliveries()[0]['evidence'], 'wake:unknown:session_changed')
        self.assertEqual([kwargs['offset'] for _target, kwargs in self.adapter.observed], [100, 200])
        self.assertEqual(len(self.adapter.discovered), 2)


class WakeClaimTests(WakeFixture):
    def target(self, **changes):
        values = {'client': self.client, 'session_ref': self.session_ref, 'pid': 4242,
                  'journal_path': self.adapter.journal, 'journal_offset': 7, 'extra': {}}
        return WakeTarget(**{**values, **changes})

    def agent(self):
        with self.store.read() as connection:
            return dict(connection.execute('SELECT * FROM agents WHERE agent_id=?', (self.agent_id,)).fetchone())

    def test_a_target_for_another_client_or_session_is_stale_and_claims_nothing(self):
        self.send()
        other = 'claude' if self.client == 'codex' else 'codex'
        stale = [self.target(client=other)]
        if self.client == 'claude':
            stale.append(self.target(session_ref=str(uuid.uuid4())))
        for target in stale:
            result = self.receiver.claim_wake(self.agent(), target, ready_since=self.now-10, now=self.now)
            self.assertEqual((result['claim_id'], result['reason']), (None, 'stale_target'))
        self.assertEqual((self.states(), self.claims()), (['queued'], []))

    def test_claim_wake_rechecks_every_gate_inside_the_claim_transaction(self):
        self.send()
        self.set_turn_state('working')
        result = self.receiver.claim_wake(self.agent(), self.target(), ready_since=self.now-10, now=self.now)
        self.assertEqual((result['claim_id'], result['reason']), (None, 'not_idle'))
        self.set_turn_state('idle')
        result = self.receiver.claim_wake(self.agent(), self.target(), ready_since=None, now=self.now)
        self.assertEqual((result['claim_id'], result['reason']), (None, 'quiet'))
        self.assertEqual((self.states(), self.claims()), (['queued'], []))

    def test_stale_generation_is_refused(self):
        self.send()
        agent = {**self.agent(), 'generation': self.recipient['generation']+5}
        with self.assertRaises(LedgerError):
            self.receiver.claim_wake(agent, self.target(), ready_since=self.now-10, now=self.now)


class WakeCapabilityTests(unittest.TestCase):
    def test_wake_capability_by_client(self):
        for client, present, expected in (('omp', 1, True), ('omp', 0, False), ('claude', 1, True),
                                          ('claude', 0, False), ('codex', 0, True), ('codex', 1, True),
                                          ('other', 1, False)):
            with self.subTest(client=client, present=present):
                self.assertIs(wake_capable({'client': client, 'extension_present': present}), expected)


class FyiNowWakeTests(WakeFixture):
    def fyi_now(self):
        with mock.patch('ham.ledger.worker.ensure_worker') as ensure, \
                mock.patch('ham.ledger.runtime.time.time', return_value=self.now):
            self.environment = {**os.environ, 'HERDR_AGENT_LEDGER_AUTOSTART': '1'}
            self.receiver.environment = self.environment
            result = self.receiver.fyi_now(terminal_id=self.recipient['terminal_id'])
        return result, ensure

    def flags(self):
        return {row['fyi_turn_end'] for row in self.deliveries()}

    def test_wake_capable_agent_gets_the_request_and_the_worker_is_ensured(self):
        self.send(kind='fyi')
        result, ensure = self.fyi_now()
        self.assertEqual((result['outcome'], result['flagged']), ('requested', 1))
        self.assertEqual(self.flags(), {1})
        ensure.assert_called_once()
        self.assertIs(ensure.call_args.args[0], self.store)

    def test_unavailable_wake_target_reports_no_receiver_with_the_reason_and_flags_nothing(self):
        self.adapter.discover_error = WakeUnavailable('claude_pid_mismatch', 'not the bound session')
        self.send(kind='fyi')
        result, ensure = self.fyi_now()
        self.assertEqual((result['outcome'], result['reason'], result['detail']),
                         ('no_receiver', 'claude_pid_mismatch', 'not the bound session'))
        self.assertEqual(self.flags(), {0})
        ensure.assert_not_called()

    def test_an_adapter_crash_is_reported_as_unavailable(self):
        self.adapter.discover_error = ValueError('bad registry')
        self.send(kind='fyi')
        result, _ = self.fyi_now()
        self.assertEqual((result['outcome'], result['reason']), ('no_receiver', 'discover_failed'))

    def test_autostart_off_does_not_start_a_worker(self):
        self.send(kind='fyi')
        self.receiver.environment = {**os.environ, 'HERDR_AGENT_LEDGER_AUTOSTART': '0'}
        with mock.patch('ham.ledger.worker.ensure_worker') as ensure:
            result = self.receiver.fyi_now(terminal_id=self.recipient['terminal_id'])
        self.assertEqual(result['outcome'], 'requested')
        ensure.assert_not_called()

    def test_worker_start_failure_is_a_warning_not_an_error(self):
        self.send(kind='fyi')
        self.receiver.environment = {**os.environ, 'HERDR_AGENT_LEDGER_AUTOSTART': '1'}
        with mock.patch('ham.ledger.worker.ensure_worker', side_effect=OSError('no spawn')):
            result = self.receiver.fyi_now(terminal_id=self.recipient['terminal_id'])
        self.assertEqual(result['outcome'], 'requested')
        self.assertIn('worker_start_failed', [warning['code'] for warning in result.get('warnings', [])])

    def test_nothing_queued_needs_no_worker(self):
        result, ensure = self.fyi_now()
        self.assertEqual(result['outcome'], 'nothing_queued')
        ensure.assert_not_called()

    def test_requested_fyi_is_then_woken_by_the_sweep_once_ready(self):
        self.send(kind='fyi')
        self.fyi_now()
        self.assertEqual(self.sweep()['claimed'], 1)

    def test_explicit_request_skips_recent_continuous_idle_without_waiting_three_minutes(self):
        self.set_thresholds(idle_minutes=3)
        self.send(kind='fyi')
        self.fyi_now()
        self.assertEqual(self.sweep()['claimed'], 1)
        self.assertEqual(len(self.adapter.delivered), 1)

    def test_automatic_turn_end_does_not_skip_the_continuous_idle_proxy(self):
        self.set_thresholds(idle_minutes=3)
        self.set_turn_state('working')
        self.send(kind='fyi')
        self.assertEqual(self.flags(), {1})
        self.set_turn_state('idle')
        self.assertEqual(self.skipped(self.sweep()), 'quiet')
        self.assertEqual(self.adapter.delivered, [])
        self.assertEqual(self.sweep(advance=181)['claimed'], 1)

    def test_explicit_request_waits_for_working_to_finish_but_not_another_quiet_period(self):
        self.set_thresholds(idle_minutes=3)
        self.set_turn_state('working')
        self.send(kind='fyi')
        result, _ = self.fyi_now()
        self.assertEqual(result['outcome'], 'after_turn')
        self.assertEqual(self.skipped(self.sweep()), 'not_idle')
        self.set_turn_state('done')
        self.assertEqual(self.sweep()['claimed'], 1)

    def test_explicit_request_preserves_sixty_second_coalescing(self):
        self.set_thresholds(idle_minutes=3, coalesce_seconds=60)
        self.send(kind='fyi')
        self.fyi_now()
        self.assertEqual(self.skipped(self.sweep()), 'coalescing')
        self.assertEqual(self.sweep(advance=61)['claimed'], 1)

    def test_expiry_restores_the_quiet_proxy_until_a_repeat_press_refreshes_it(self):
        self.set_thresholds(idle_minutes=3)
        self.send(kind='fyi')
        self.fyi_now()
        self.now += FYI_NOW_QUIET_SECONDS
        self.set_turn_state('working')
        self.set_turn_state('idle')
        self.assertEqual(self.skipped(self.sweep()), 'quiet')
        result, _ = self.fyi_now()
        self.assertEqual(result['flagged'], 0)
        self.assertEqual(self.sweep()['claimed'], 1)

    def test_discovery_cannot_bypass_a_new_working_state_in_the_claim_transaction(self):
        self.set_thresholds(idle_minutes=3)
        self.send(kind='fyi')
        self.fyi_now()
        discover = self.adapter.discover

        def work_during_discovery(*args, **kwargs):
            self.set_turn_state('working')
            return discover(*args, **kwargs)

        self.adapter.discover = work_during_discovery
        result = self.sweep()
        self.assertEqual((result['claimed'], self.skipped(result)), (0, 'not_idle'))
        self.assertEqual(self.adapter.delivered, [])

    def test_claim_resamples_clock_when_discovery_crosses_request_expiry(self):
        self.set_thresholds(idle_minutes=20)
        self.send(kind='fyi')
        self.fyi_now()
        discover = self.adapter.discover

        def slow_discovery(*args, **kwargs):
            self.now += FYI_NOW_QUIET_SECONDS
            return discover(*args, **kwargs)

        self.adapter.discover = slow_discovery
        result = self.sweep()
        self.assertEqual((result['claimed'], self.skipped(result)), (0, 'quiet'))
        self.assertEqual(self.adapter.delivered, [])

    def test_claim_rechecks_request_consumption_during_external_discovery(self):
        self.set_thresholds(idle_minutes=3)
        for index in range(BUNDLE_MESSAGES+1):
            self.send(kind='fyi', body=str(index))
        self.fyi_now()
        discover = self.adapter.discover

        def receipt_during_discovery(*args, **kwargs):
            # A natural receiver confirms part of the same scoped request.
            with mock.patch('ham.ledger.mail.time.time', return_value=self.now):
                with self.store.transaction() as connection:
                    agent = dict(connection.execute('SELECT * FROM agents WHERE agent_id=?',
                                                    (self.agent_id,)).fetchone())
                    rows = self.receiver._eligible(connection,agent,10,wake=False)[:1]
                    claim = Mailbox._claim_messages(connection,rows,self.agent_id,agent['generation'],'hook')
                self.mail.started(**{key:claim[key] for key in ('claim_id','agent_id','generation')},
                                  evidence='natural journal receipt')
            return discover(*args, **kwargs)

        self.adapter.discover = receipt_during_discovery
        result = self.sweep()
        self.assertEqual((result['claimed'], self.skipped(result)), (0, 'quiet'))
        self.assertEqual(self.adapter.delivered, [])

    def test_delivery_consumes_request_for_remainder_but_a_new_press_can_request_it(self):
        self.set_thresholds(idle_minutes=3)
        for index in range(BUNDLE_MESSAGES+1):
            self.send(kind='fyi', body=str(index))
        self.fyi_now()
        self.assertEqual(self.sweep()['claimed'], 1)
        self.adapter.observation = lambda **_kwargs: Observation('accepted', 200, 'journal accepted')
        with mock.patch('ham.ledger.runtime.time.time', return_value=self.now):
            result = self.sweep()
        self.assertEqual(len(result['receipts']), BUNDLE_MESSAGES)
        self.assertEqual((result['claimed'], self.skipped(result)), (0, 'quiet'))
        self.fyi_now()
        self.assertEqual(self.sweep()['claimed'], 1)

    def test_definite_non_delivery_retains_request_but_pending_claim_still_blocks(self):
        self.set_thresholds(idle_minutes=3)
        self.send(kind='fyi')
        self.fyi_now()
        self.adapter.deliver_error = WakeDeliveryFailed('not_sent', 'no bytes written',
                                                      definitely_not_delivered=True)
        self.assertEqual(self.sweep()['claimed'], 1)
        self.assertEqual(self.states(), ['queued'])
        self.adapter.deliver_error = None
        self.assertEqual(self.sweep(advance=FAILURE_BACKOFF_SECONDS+1)['claimed'], 1)
        self.send(kind='fyi')
        self.fyi_now()
        self.assertEqual(self.skipped(self.sweep()), 'claim_pending')
        self.assertEqual(len(self.adapter.delivered), 2)

    def test_future_fyi_after_withdrawal_does_not_inherit_an_operator_request(self):
        self.set_thresholds(idle_minutes=3)
        original = self.send(kind='fyi')
        self.fyi_now()
        self.mail.withdraw(original['message_id'], sender=self.sender)
        self.set_turn_state('working')
        self.send(kind='fyi')
        self.set_turn_state('idle')
        self.assertEqual(self.skipped(self.sweep()), 'quiet')
        self.assertEqual(self.adapter.delivered, [])


class ClaudeFyiNowWakeTests(FyiNowWakeTests):
    client = 'claude'


class WorkerLoopWakeTests(WakeFixture):
    def run_once(self, refresh=None):
        from ham.ledger.worker import run_worker
        remote = mock.Mock()
        remote.pump.return_value = {'sent': 0, 'failed': 0, 'deferred': 0}
        patches = (mock.patch.object(NativeReceiver, 'wake_adapter', lambda _self, _client: self.adapter),
                   mock.patch.object(AgentRegistry, 'refresh', **({'side_effect': refresh} if refresh else
                                                                  {'return_value': {}})))
        with patches[0], patches[1]:
            return run_worker(self.store, self.environment, once=True, remote=remote)

    def failures(self):
        with self.store.read() as connection:
            return [row['detail'] for row in connection.execute('SELECT detail FROM notification_failures')]

    def test_one_worker_pass_wakes_an_idle_agent_and_accepts_the_evidence(self):
        self.send()
        self.adapter.observation = lambda **_k: Observation('accepted', 300, 'ev:worker')
        self.run_once()
        self.assertEqual(len(self.adapter.delivered), 1)
        self.assertEqual(self.states(), ['delivering'])  # Evidence is only checked on the next sweep.
        self.run_once()
        self.assertEqual(self.states(), ['delivered'])
        self.assertEqual(self.wake_log(), 1)

    def test_failed_registry_refresh_never_wakes_anyone(self):
        self.send()
        self.run_once(refresh=LedgerError('herdr_unavailable', 'no herdr'))
        self.assertEqual((self.adapter.discovered, self.adapter.delivered, self.states()), ([], [], ['queued']))
        self.assertTrue(any(detail.startswith('worker sweep:') for detail in self.failures()))

    def test_a_ledger_error_in_the_wake_sweep_is_noted_and_not_fatal(self):
        self.send()
        with mock.patch.object(NativeWaker, 'sweep', side_effect=LedgerError('boom', 'wake exploded')):
            self.run_once()
        self.assertIn('worker wake: wake exploded', self.failures())


class ClaudeWakeTests(WakeFixture):
    client = 'claude'

    def test_claude_without_a_bound_hook_is_not_wake_capable(self):
        with self.store.transaction() as connection:
            connection.execute('UPDATE agents SET extension_present=0 WHERE agent_id=?', (self.agent_id,))
        self.send(kind='fyi')
        result, ensure = FyiNowWakeTests.fyi_now(self)
        self.assertEqual(result['outcome'], 'no_receiver')
        self.assertNotIn('reason', result)
        ensure.assert_not_called()
        self.send()
        self.assertEqual(self.sweep()['skipped'], {})  # Not even probed.
        self.assertEqual(self.adapter.discovered, [])

    def test_a_session_swap_during_a_pending_claim_is_unknown(self):
        self.wake_now()
        with self.store.transaction() as connection:
            connection.execute('UPDATE agents SET session_ref=? WHERE agent_id=?',
                               (str(uuid.uuid4()), self.agent_id))
        self.sweep()
        self.assertEqual(self.deliveries()[0]['evidence'], 'wake:unknown:agent_changed')
        self.assertEqual(self.adapter.observed, [])

    def test_hook_observer_leaves_wake_claims_to_the_waker(self):
        self.wake_now()
        observed = self.receiver.observe_claude()
        self.assertEqual((observed['observed'], observed['receipts']), (0, []))
        self.assertEqual(self.states(), ['delivering'])
        self.assertIsNone(self.deliveries()[0]['evidence'])


if __name__ == '__main__':
    unittest.main()
