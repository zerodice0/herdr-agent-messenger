"""Deterministic real-mail consumer interleavings at the worker exit handoff."""
import tests.ham_test_support as ham_test_support  # Protect the live ledger before product imports.
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from ham.ledger.mail import Mailbox
from ham.ledger.settings import RepoSettings
from ham.ledger.status import AgentRegistry, repository_identity
from ham.ledger.store import LedgerStore
from ham.ledger import worker as ledger_worker
from tests.test_ledger_core import snapshot


class WorkerHandoffTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root/'repo'
        self.repo.mkdir()
        self.store = LedgerStore(self.root/'ledger',canonical_name='handoff')
        # Exit-handoff scenarios retain the pre-rollout, known-repo off policy.
        repo_id = repository_identity(str(self.repo), self.store.host)["repo_id"]
        with self.store.transaction() as connection:
            connection.execute("INSERT INTO repos VALUES (?,NULL,?)", (repo_id, 1.0))
        registry = AgentRegistry(self.store)
        registry.reconcile(snapshot(self.repo), refresh_display=False)
        self.sender,self.recipient = registry.list()
        self.mail = Mailbox(self.store)
        self.environment = {**os.environ, 'HERDR_AGENT_LEDGER_AUTOSTART':'0',
                            'HERDR_SOCKET_PATH': str(self.root/'herdr-isolated.sock')}

    def test_seeded_auto_keeps_maintenance_like_explicit_auto(self):
        repo = self.root / 'new-repo'
        repo.mkdir()
        registry = AgentRegistry(self.store)
        registry.reconcile(snapshot(repo), refresh_display=False)
        agent = registry.list()[0]
        repo_id = agent['repo_id']
        settings = RepoSettings(self.store)
        seeded = settings.show(repo_id, host=agent['host'], workspace_id=agent['workspace_id'])['settings']['mode']
        self.assertEqual((seeded['value'], seeded['changed_by']), ('auto', 'home'))
        self.assertTrue(ledger_worker.maintenance_pending(self.store))
        with patch.object(RepoSettings, '_display_result', return_value={}):
            settings.mutate(repo_id, 'mode', 'off')
            self.assertFalse(ledger_worker.maintenance_pending(self.store))
            settings.mutate(repo_id, 'mode', 'auto')
        self.assertTrue(ledger_worker.maintenance_pending(self.store))

    def test_designated_main_shell_watch_stays_in_its_host_workspace(self):
        settings = RepoSettings(self.store)
        with patch.object(RepoSettings, '_display_result', return_value={}):
            settings.mutate(self.sender['repo_id'], 'main',
                            {'host': self.store.host, 'terminal_id': self.sender['terminal_id']},
                            host=self.store.host, workspace_id=self.sender['workspace_id'])
        registry = AgentRegistry(self.store)
        shells = snapshot(self.repo)
        shells['agents'] = []
        registry.reconcile(shells, refresh_display=False)
        self.assertTrue(ledger_worker.maintenance_pending(self.store))
        designated = next(pane for pane in shells['panes']
                          if pane['terminal_id'] == self.sender['terminal_id'])
        designated['workspace_id'] = 'another-workspace'
        registry.reconcile(shells, refresh_display=False)
        self.assertFalse(ledger_worker.maintenance_pending(self.store))

    def test_anchor_main_watches_linked_shell_and_tracks_group_moves_and_close(self):
        registry = AgentRegistry(self.store)
        observed = snapshot(self.repo, moved=True)
        observed["workspaces"] = [
            {"workspace_id": "w1", "worktree": {"repo_key": "/repo/.git", "is_linked_worktree": False}},
            {"workspace_id": "w2", "worktree": {"repo_key": "/repo/.git", "is_linked_worktree": True}},
            {"workspace_id": "w3", "worktree": {"repo_key": "/repo/.git", "is_linked_worktree": True}},
        ]
        registry.reconcile(observed, refresh_display=False)
        settings = RepoSettings(self.store)
        with patch.object(RepoSettings, "_display_result", return_value={}):
            settings.mutate(self.sender["repo_id"], "main",
                            {"host": self.store.host, "terminal_id": "term-b"},
                            host=self.store.host, workspace_id="w2")
        observed["agents"] = []
        registry.reconcile(observed, refresh_display=False)
        self.assertTrue(ledger_worker.maintenance_pending(self.store))
        target = next(pane for pane in observed["panes"] if pane["terminal_id"] == "term-b")
        target["workspace_id"] = "w3"
        registry.reconcile(observed, refresh_display=False)
        self.assertTrue(ledger_worker.maintenance_pending(self.store))
        target["workspace_id"] = "unrelated"
        registry.reconcile(observed, refresh_display=False)
        self.assertFalse(ledger_worker.maintenance_pending(self.store))
        target["workspace_id"] = "w2"
        registry.reconcile(observed, refresh_display=False)
        self.assertTrue(ledger_worker.maintenance_pending(self.store))
        observed["workspaces"] = observed["workspaces"][1:]
        observed["panes"] = [target]
        registry.reconcile(observed, refresh_display=False)
        self.assertFalse(ledger_worker.maintenance_pending(self.store))

    def test_ignored_linked_main_setting_does_not_watch_until_anchor_closes(self):
        registry = AgentRegistry(self.store)
        observed = snapshot(self.repo, moved=True)
        registry.reconcile(observed, refresh_display=False)
        settings = RepoSettings(self.store)
        with patch.object(RepoSettings, "_display_result", return_value={}):
            settings.mutate(self.sender["repo_id"], "main",
                            {"host": self.store.host, "terminal_id": "term-b"},
                            host=self.store.host, workspace_id="w2")
        with self.store.read() as connection:
            retained = [tuple(row) for row in connection.execute("SELECT * FROM workspace_settings")]
        observed["agents"] = []
        observed["workspaces"] = [
            {"workspace_id": "w1", "worktree": {"repo_key": "/repo/.git", "is_linked_worktree": False}},
            {"workspace_id": "w2", "worktree": {"repo_key": "/repo/.git", "is_linked_worktree": True}},
        ]
        registry.reconcile(observed, refresh_display=False)
        self.assertFalse(ledger_worker.maintenance_pending(self.store))
        observed["workspaces"] = observed["workspaces"][1:]
        observed["panes"] = [pane for pane in observed["panes"] if pane["terminal_id"] == "term-b"]
        registry.reconcile(observed, refresh_display=False)
        self.assertTrue(ledger_worker.maintenance_pending(self.store))
        with self.store.read() as connection:
            self.assertEqual([tuple(row) for row in connection.execute("SELECT * FROM workspace_settings")], retained)

    def test_enabled_mode_keeps_worktree_workspace_maintenance_without_main(self):
        registry = AgentRegistry(self.store)
        registry.reconcile(snapshot(self.repo, moved=True), refresh_display=False)
        settings = RepoSettings(self.store)
        with patch.object(RepoSettings, '_display_result', return_value={}):
            settings.mutate(self.sender['repo_id'], 'mode', 'manual')
        with self.store.read() as connection:
            self.assertIsNone(settings.main(connection, self.sender['repo_id'], self.store.host, 'w1'))
            self.assertIsNone(settings.main(connection, self.sender['repo_id'], self.store.host, 'w2'))
        self.assertTrue(ledger_worker.maintenance_pending(self.store))

    def send(self):
        return self.mail.send(self.sender,self.recipient,body='Real local handoff consumer mail')

    def consumer(self):
        mail,recipient = self.mail,self.recipient
        class PullConsumer:
            def pump(self):
                messages = mail.read(recipient)['messages']
                if messages:
                    mail.handle(recipient,[message['message_id'] for message in messages])
                return {'sent':0,'failed':0,'deferred':0}
        return PullConsumer()

    def exercise(self,*,once):
        first = self.send()
        original = ledger_worker.maintenance_pending
        empty_calls = 0
        injected = []
        ensure_results = []
        def commit_after_empty_observation(store):
            nonlocal empty_calls
            pending = original(store)
            if not pending:
                empty_calls += 1
                # Local-first worker: the first empty observation is the
                # post-pump check, still under flock in normal and once modes.
                if empty_calls == 1:
                    injected.append(self.send())
                    ensure_results.append(ledger_worker.ensure_worker(store,self.environment))
            return pending
        with patch.object(ledger_worker,'maintenance_pending',commit_after_empty_observation):
            result = ledger_worker.run_worker(self.store,self.environment,once=once,remote=self.consumer())
        self.assertEqual(ensure_results,[{'started':False,'reason':'already_running'}])
        self.assertEqual(self.mail.show_thread(first['thread_id'])['messages'][0]['state'],'handled')
        state = self.mail.show_thread(injected[0]['thread_id'])['messages'][0]['state']
        self.assertFalse((self.store.directory/'worker.pid').exists())
        with ledger_worker.worker_lock(self.store) as acquired:
            self.assertTrue(acquired)
        return result,state

    def test_last_item_new_send_handoff_is_owned_after_unlock_without_autospawn(self):
        result,state = self.exercise(once=False)
        self.assertEqual(result['reason'],'no_pending_work')
        self.assertEqual(state,'handled')
        self.assertFalse(ledger_worker.maintenance_pending(self.store))

    def test_once_remains_one_pass_and_does_not_start_a_replacement(self):
        result,state = self.exercise(once=True)
        self.assertEqual(result['reason'],'one_pass')
        self.assertEqual(state,'queued')
        self.assertTrue(ledger_worker.maintenance_pending(self.store))

    def test_once_retains_current_data_and_purges_expired_rows_at_zero_monotonic_epoch(self):
        now = time.time()
        day = 86400
        with patch.dict(os.environ, {'HERDR_AGENT_LEDGER_AUTOSTART': '0'}):
            expired, recent, nonfinal = (self.send() for _ in range(3))
        self.mail.handle(self.recipient, [expired['message_id'], recent['message_id']])
        claim = self.mail.claim(terminal_id=self.recipient['terminal_id'],
                                agent_id=self.recipient['agent_id'],
                                generation=self.recipient['generation'])
        self.mail.resolve_claim(claim_id=claim['claim_id'], agent_id=claim['agent_id'],
                                generation=claim['generation'], disposition='applied',
                                evidence='Explicit operator-applied retention fixture, not native evidence')
        with self.store.transaction() as connection:
            for message, age in ((expired, 91), (recent, 89)):
                connection.execute('UPDATE messages SET created_at=?,final_at=? WHERE message_id=?',
                                   (now-age*day, now-age*day, message['message_id']))
            connection.execute('UPDATE messages SET created_at=? WHERE message_id=?',
                               (now-120*day, nonfinal['message_id']))
            connection.execute('DELETE FROM status_log')
            for seq, age in ((101, 31), (102, 29)):
                connection.execute('INSERT INTO wake_log VALUES (?,?,?,?)',
                                   (seq, self.recipient['agent_id'], now-age*day, '[]'))
                connection.execute('INSERT INTO status_log VALUES (?,?,?,?,?)',
                                   (seq, self.recipient['agent_id'], 'phase', 'retention fixture', now-age*day))
            current_status = [dict(row) for row in connection.execute('SELECT * FROM agent_status ORDER BY agent_id')]
        self.assertFalse(ledger_worker.maintenance_pending(self.store))
        with patch.object(ledger_worker.time, 'monotonic', return_value=0.0):
            result = ledger_worker.run_worker(self.store, self.environment, once=True)
        with self.store.read() as connection:
            observed = {
                'messages': sorted(row[0] for row in connection.execute('SELECT message_id FROM messages')),
                'wake_log': [row[0] for row in connection.execute('SELECT seq FROM wake_log ORDER BY seq')],
                'status_log': [row[0] for row in connection.execute('SELECT seq FROM status_log ORDER BY seq')],
            }
            self.assertEqual(observed, {
                'messages': sorted([recent['message_id'], nonfinal['message_id']]),
                'wake_log': [102],
                'status_log': [102],
            })
            self.assertEqual([dict(row) for row in connection.execute(
                'SELECT * FROM agent_status ORDER BY agent_id')], current_status)
            self.assertEqual(connection.execute('SELECT count(*) FROM dedupe_receipts').fetchone()[0], 3)
            retained = connection.execute(
                'SELECT m.final_at,d.state,d.confirmation_source FROM messages m '
                'JOIN deliveries d USING(message_id) WHERE message_id=?', (nonfinal['message_id'],)).fetchone()
            self.assertEqual(tuple(retained), (None, 'delivered', 'operator'))
        self.assertEqual(result['reason'], 'one_pass')
        self.assertFalse((self.store.directory/'worker.pid').exists())
        with ledger_worker.worker_lock(self.store) as acquired:
            self.assertTrue(acquired)

    def test_repeated_diagnostic_is_one_last_seen_row_and_expires_after_thirty_days(self):
        for _ in range(100):
            self.store.note_notification_failure('worker sweep: Herdr unavailable')
        self.assertEqual(len(self.store.doctor()['notification_failures']),1)
        self.store.retention(now=time.time()+31*86400)
        self.assertEqual(self.store.doctor()['notification_failures'],[])


if __name__ == '__main__':
    unittest.main()
