"""Deterministic real-mail consumer interleavings at the worker exit handoff."""
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from ledger_mail import Mailbox
from ledger_status import AgentRegistry
from ledger_store import LedgerStore
import ledger_worker
from test_ledger_core import snapshot


class WorkerHandoffTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root/'repo'
        self.repo.mkdir()
        self.store = LedgerStore(self.root/'ledger',canonical_name='handoff')
        registry = AgentRegistry(self.store)
        registry.reconcile(snapshot(self.repo), refresh_display=False)
        self.sender,self.recipient = registry.list()
        self.mail = Mailbox(self.store)
        self.environment = {**os.environ, 'HERDR_AGENT_LEDGER_AUTOSTART':'0',
                            'HERDR_SOCKET_PATH': str(self.root/'herdr-isolated.sock')}

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
                # Normal worker: its final check just before unlock. One-pass:
                # its only empty check, before the explicit once short-circuit.
                if empty_calls == (1 if once else 2):
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

    def test_repeated_diagnostic_is_one_last_seen_row_and_expires_after_thirty_days(self):
        for _ in range(100):
            self.store.note_notification_failure('worker sweep: Herdr unavailable')
        self.assertEqual(len(self.store.doctor()['notification_failures']),1)
        self.store.retention(now=time.time()+31*86400)
        self.assertEqual(self.store.doctor()['notification_failures'],[])


if __name__ == '__main__':
    unittest.main()
