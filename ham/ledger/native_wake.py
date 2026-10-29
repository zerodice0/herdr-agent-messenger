"""Worker-driven idle wake for Claude Code and Codex.

OMP wakes itself from its extension. Claude and Codex have no equivalent, so
the ledger worker (one per host, sweeping every few seconds while mail or a
participating agent exists) applies the same wake gates, claims the bundle as a
``via='wake'`` delivery, hands it to the client adapter and later confirms
acceptance from the client's own journal. Hook-driven Claude delivery and the
OMP extension are untouched; a pending claim of either kind blocks a wake.

Failure semantics: only a delivery that definitely did not happen is released
as ``not_applied``. Anything that may have reached the client stays
``delivering`` and is observed; a vanished target or replaced agent generation
before evidence is ``unknown`` for the operator, exactly like OMP and Claude
hook claims.
"""
from __future__ import annotations

import time
from typing import Any, Callable, Mapping

from ham.ledger.mail import Mailbox
from ham.ledger.runtime import NativeReceiver, WAKE_READY_STATES, wake_capable
from ham.ledger.store import LedgerError, LedgerStore
from ham.ledger.wake_clients import WakeDeliveryFailed, WakeUnavailable

# A definite failure retries slowly; an unavailable target is re-probed more
# often because discovery is cheap and the session may just not have a turn yet.
FAILURE_BACKOFF_SECONDS = 300
UNAVAILABLE_BACKOFF_SECONDS = 30
UNKNOWN_PREFIX = 'wake:unknown:'


class NativeWaker:
    def __init__(self, store: LedgerStore, receiver: NativeReceiver | None = None, *,
                 clock: Callable[[], float] = time.time,
                 environment: Mapping[str, str] | None = None) -> None:
        self.store = store
        self.receiver = receiver or NativeReceiver(store, environment=environment)
        self.environment = environment
        self.clock = clock
        self.unavailable: dict[str, tuple[str, str]] = {}
        self._ready_seen: dict[str, float] = {}
        self._backoff: dict[str, float] = {}
        self._targets: dict[str, Any] = {}
        self._offsets: dict[str, int] = {}
        self._contents: dict[str, str] = {}
        self._errors: dict[str, str] = {}

    def sweep(self) -> dict[str, Any]:
        """One pass: observe pending wake claims, then wake eligible idle agents.

        Returns ``claimed``, ``released``, ``receipts`` (newly delivered, for the
        caller to forward) and ``skipped`` (agent_id -> refusing gate).
        """
        now = self.clock()
        result: dict[str, Any] = {'claimed':0,'released':0,'receipts':[],'skipped':{}}
        agents = self._agents()
        ids = {agent['agent_id'] for agent in agents}
        for table in (self._ready_seen,self._backoff,self.unavailable):
            for stale in [key for key in table if key not in ids]:
                del table[stale]
        for agent in agents:
            if agent['turn_state'] in WAKE_READY_STATES:
                self._ready_seen.setdefault(agent['agent_id'],now)
            else:
                self._ready_seen.pop(agent['agent_id'],None)
        self._observe(result)
        for agent in agents:
            if not agent['queued']:
                continue
            try:
                self._wake(agent,now,result)
            except LedgerError as error:
                # The agent changed under us; the next sweep sees the new truth.
                result['skipped'][agent['agent_id']] = error.code
        return result

    def _agents(self) -> list[dict[str, Any]]:
        with self.store.read() as connection:
            rows = connection.execute(
                "SELECT a.*,s.turn_state,(SELECT count(*) FROM deliveries d WHERE d.recipient_host=a.host "
                "AND d.recipient_terminal_id=a.terminal_id AND d.recipient_repo_id=a.repo_id "
                "AND d.state='queued') AS queued "
                "FROM agents a JOIN agent_status s USING(agent_id) "
                "WHERE a.host=? AND a.active=1 AND a.client IN ('claude','codex') "
                "ORDER BY a.terminal_id",(self.store.host,)).fetchall()
        return [dict(row) for row in rows if wake_capable(dict(row))]

    def _ready_since(self, connection: Any, agent: Mapping[str, Any]) -> float | None:
        """Current durable ready run, or the worker's first ready observation."""
        return self.receiver.wake_ready_since(
            connection,agent['agent_id'],self._ready_seen.get(agent['agent_id']))

    def _wake(self, agent: dict[str, Any], now: float, result: dict[str, Any]) -> None:
        key = agent['agent_id']
        if self._backoff.get(key,0)>now:
            result['skipped'][key] = 'backoff'
            return
        with self.store.read() as connection:
            ready_since = self._ready_since(connection,agent)
            _state,_rows,_trigger,reason = self.receiver.wake_gate(connection,agent,now,ready_since=ready_since)
        if reason:
            result['skipped'][key] = reason
            return
        try:
            adapter = self.receiver.wake_adapter(agent['client'])
            target = adapter.discover(agent,environment=self.environment)
        except WakeUnavailable as error:
            self._unavailable(key,now,result,error.code,error.message)
            return
        except Exception as error:  # An adapter bug must never stop the worker.
            self._unavailable(key,now,result,'discover_failed',str(error))
            return
        self.unavailable.pop(key,None)
        claim = self.receiver.claim_wake(agent,target,ready_since=ready_since,now=self.clock())
        if not claim['claim_id']:
            result['skipped'][key] = claim['reason']
            return
        result['claimed'] += 1
        claim_id = claim['claim_id']
        self._targets[claim_id] = target
        self._offsets[claim_id] = target.journal_offset
        self._contents[claim_id] = claim['content']
        try:
            adapter.deliver(target,claim['content'])
        except WakeDeliveryFailed as error:
            self._delivery_failed(agent,claim,now,result,error.code,error.message,
                                  definite=error.definitely_not_delivered)
        except Exception as error:
            # The adapter did not say nothing was written: assume it may have been.
            self._delivery_failed(agent,claim,now,result,'deliver_error',str(error),definite=False)

    def _unavailable(self, key: str, now: float, result: dict[str, Any], code: str, message: str) -> None:
        self.unavailable[key] = (code,message)
        self._backoff[key] = now+UNAVAILABLE_BACKOFF_SECONDS
        result['skipped'][key] = 'unavailable:'+code

    def _delivery_failed(self, agent: Mapping[str, Any], claim: Mapping[str, Any], now: float,
                         result: dict[str, Any], code: str, message: str, *, definite: bool) -> None:
        self._backoff[agent['agent_id']] = now+FAILURE_BACKOFF_SECONDS
        name = agent.get('alias') or agent['terminal_id']
        self.store.note_notification_failure(f"wake {agent['client']} {name}: {code}: {message}"[:500])
        mailbox = Mailbox(self.store)
        if definite:
            mailbox.release(claim_id=claim['claim_id'],agent_id=claim['agent_id'],generation=claim['generation'],
                            disposition='not_applied',evidence='wake:not-delivered:'+code)
            result['released'] += 1
            self._forget(claim['claim_id'])
        else:
            mailbox.release(claim_id=claim['claim_id'],agent_id=claim['agent_id'],generation=claim['generation'],
                            disposition='unknown',evidence='wake:delivery-uncertain:'+code)
        result['skipped'][agent['agent_id']] = 'delivery_failed:'+code

    def _forget(self, claim_id: str) -> None:
        for table in (self._targets,self._offsets,self._contents,self._errors):
            table.pop(claim_id,None)

    def _release_unknown(self, claim: Mapping[str, Any], reason: str) -> None:
        Mailbox(self.store).release(claim_id=claim['claim_id'],agent_id=claim['agent_id'],
                                    generation=claim['generation'],disposition='unknown',
                                    evidence=UNKNOWN_PREFIX+reason)
        self._forget(claim['claim_id'])

    def _observe(self, result: dict[str, Any]) -> None:
        with self.store.read() as connection:
            claims = [dict(row) for row in connection.execute(
                "SELECT DISTINCT n.*,d.claimed_agent_id AS agent_id,d.claimed_generation AS generation,"
                "d.recipient_terminal_id AS terminal_id FROM native_claims n JOIN deliveries d USING(claim_id) "
                "WHERE d.state='delivering' AND d.via='wake' "
                "AND (d.evidence IS NULL OR d.evidence NOT LIKE ?) ORDER BY n.created_at",(UNKNOWN_PREFIX+'%',))]
            agents = {claim['agent_id']:connection.execute('SELECT * FROM agents WHERE agent_id=?',
                                                            (claim['agent_id'],)).fetchone() for claim in claims}
        pending = {claim['claim_id'] for claim in claims}
        for table in (self._targets,self._offsets,self._contents,self._errors):
            for stale in [key for key in table if key not in pending]:
                del table[stale]
        for claim in claims:
            claim_id = claim['claim_id']
            agent = agents[claim['agent_id']]
            try:
                if (agent is None or not agent['active'] or agent['client']!=claim['client']
                        or (claim['client']=='claude' and agent['session_ref']!=claim['session_ref'])):
                    self._release_unknown(claim,'agent_changed')
                    continue
                if not isinstance(claim['transcript_path'],str) or not isinstance(claim['transcript_start_offset'],int):
                    continue
                content = self._contents.get(claim_id)
                if content is None:
                    content = self.receiver.claim_content(claim)
                    if content is None:
                        continue
                    self._contents[claim_id] = content
                target = self._targets.get(claim_id)
                adapter = self.receiver.wake_adapter(claim['client'])
                # Recovery observes the persisted journal before asking whether
                # the current process/thread is still discoverable. A bounded
                # scanner returns pending while making progress through backlog;
                # target=None reports gone only once that evidence is exhausted.
                observation = adapter.observe(
                    target,journal_path=claim['transcript_path'],
                    offset=self._offsets.get(claim_id,claim['transcript_start_offset']),
                    marker=claim['marker'],content=content,content_sha256=claim['content_sha256'],
                    session_ref=claim['session_ref'],pid=target.pid if target is not None else None)
                if target is None and observation.status=='gone':
                    try:
                        target = adapter.discover(dict(agent),environment=self.environment)
                    except WakeUnavailable:
                        continue
                    if target.session_ref!=claim['session_ref']:
                        self._release_unknown(claim,'session_changed')
                        continue
                    self._targets[claim_id] = target
                    # Recheck liveness only after scanning all available records.
                    observation = adapter.observe(
                        target,journal_path=claim['transcript_path'],offset=observation.offset,
                        marker=claim['marker'],content=content,content_sha256=claim['content_sha256'],
                        session_ref=claim['session_ref'],pid=target.pid)
                self._errors.pop(claim_id,None)
                if observation.status=='accepted' and observation.evidence:
                    delivered = Mailbox(self.store).started(
                        claim_id=claim_id,agent_id=claim['agent_id'],generation=claim['generation'],
                        evidence=observation.evidence)
                    result['receipts'].extend(delivered['receipts'])
                    self._forget(claim_id)
                elif observation.status=='gone':
                    self._release_unknown(claim,'target_gone')
                else:
                    self._offsets[claim_id] = observation.offset
            except LedgerError:
                # Resolved by an operator or replaced concurrently; the next sweep re-reads.
                self._forget(claim_id)
            except Exception as error:
                detail = f"wake observe {claim['client']}: {type(error).__name__}: {error}"[:500]
                if self._errors.get(claim_id)!=detail:
                    self._errors[claim_id] = detail
                    self.store.note_notification_failure(detail)
