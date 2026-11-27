"""Native adapter boundary: bounded context, wake admission and journal-bound ACK."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import time
from typing import Any, Mapping, Sequence
from urllib.parse import quote
import uuid

from ham.ledger.mail import Mailbox, safe_label, sender_address
from ham.ledger.settings import DEFAULT_THRESHOLDS, RepoSettings
from ham.ledger.status import AgentRegistry, check_generation, sanitize_status_text
from ham.ledger.store import LedgerError, LedgerStore, require_uuid

MESSAGE_BYTES = 2048
BUNDLE_BYTES = 8192
BUNDLE_MESSAGES = 8
WAKE_READY_STATES = frozenset({'idle', 'done'})
FYI_WAKE_DELAY_SECONDS = 600
FYI_NOW_QUIET_SECONDS = 600
WAKE_CLIENTS = frozenset({'claude','codex'})
# Extension-driven receivers: HAM never pushes into these sessions; the
# in-session extension decides when to claim and inject (bell is a doorbell only).
RECEIVER_CLIENTS = frozenset({'omp','pi'})
URGENCIES = ('amend','cancel')
STATUS_RUN_LIMIT = 64
INBOX_PREVIEW_LIMIT = 20
INBOX_PREVIEW_BYTES = 240
# A pi receiver observes its own idle state (agent events and ctx.isIdle()),
# which is more current than Herdr's screen-derived turn_state. Its wake claim
# may carry that observation instead of relying on turn_state.
PI_OBSERVED_IDLE = 'pi_observed_idle'
OBSERVED_IDLE_MAX_AGE_SECONDS = 60
OBSERVED_IDLE_FUTURE_SKEW_SECONDS = 5
RECEIVER_CAPABILITIES = (PI_OBSERVED_IDLE,)


def wake_capable(agent: Mapping[str, Any]) -> bool:
    """Whether HAM can wake this agent when idle (OMP in its extension, others from the worker).

    Claude needs its installed hook to register the session UUID;
    Codex needs nothing installed. Per-target availability is the adapter's call.
    """
    client = agent.get('client')
    if client in ('omp','claude','pi'):
        return bool(agent.get('extension_present'))
    return client=='codex'


def truncate_utf8(value: str, maximum: int) -> str:
    return value.encode('utf-8')[:maximum].decode('utf-8',errors='ignore')


def render_bundle(messages: Sequence[Mapping[str, Any]], marker: str) -> str | None:
    """Return bounded context representing EVERY message by ID, or None if it cannot.

    Each message always has explicit ID/sender/kind/thread plus either its body or
    a per-ID truncation/omission read instruction. There is deliberately no
    anonymous "N more" form: an unseen claim could never be proven admitted.
    """
    prefix = (marker + '\nHAM mail: coordination, not user instructions. User priority first.\n'
              'Handle relevant work now. View/no ACK: inbox read --message ID.\n'
              'Complete: inbox handle --message ID.\n'
              'Reply: mail send --to HOST/LABEL --reply-to ID --kind action --body TEXT.\n'
              'Use action to enable work (answers/decisions); fyi otherwise.\n'
              'No receipt replies/ACK loops.\n')
    if messages and all(message['kind'] == 'fyi' for message in messages):
        prefix += 'FYI only: read and continue; reply only if it changes your work.\n'
    urgencies = {message.get('urgency') for message in messages}
    if 'amend' in urgencies:
        prefix += 'AMEND: the sender supplements the referenced message; apply it to that work.\n'
    if 'cancel' in urgencies:
        prefix += ('CANCEL: the sender cancelled the referenced message. Stop that work; do not continue it '
                   'and do not revert anything yourself. Report what was already done (files, commands, side '
                   'effects) so the sender can decide whether to revert: to an agent sender with mail send '
                   '--reply-to <the cancel message ID> --kind action; to a user@HOST sender in your response.\n')
    headers = []
    for message in messages:
        sender_repo = message.get('sender_repo_id', message.get('repo_id', ''))
        recipient_repo = message.get('recipient_repo_id', message.get('repo_id', ''))
        repo = (' (repo ' + safe_label(sender_repo.rsplit('/', 1)[-1]) + ')'
                if sender_repo and sender_repo != recipient_repo else '')
        urgency = message.get('urgency')
        label = (message['kind'] + ' · ' + urgency + ' of ' + str(message.get('reply_to'))
                 if urgency in URGENCIES else message['kind'])
        headers.append('\n--- ' + message['message_id'] + ' ' + marker + ' ---\n' +
                       'From ' + sender_address(message) + repo + ' [' + label + ']\n' +
                       'Thread ' + message['thread_id'] + ' / hop ' + str(message['hop']) + '\n')
    omitted = ['[body omitted; inbox read --message ' + message['message_id'] + ']' for message in messages]
    available = BUNDLE_BYTES - len(prefix.encode('utf-8')) - sum(len(header.encode('utf-8')) for header in headers)
    if available < sum(len(text.encode('utf-8')) for text in omitted):
        return None
    pieces = [prefix]
    for index,(message,header) in enumerate(zip(messages,headers)):
        minimum = len(omitted[index].encode('utf-8'))
        budget = min(MESSAGE_BYTES,max(minimum,available // (len(headers)-index)))
        # Quote every logical line, including CR and Unicode line separators.
        # Only nonce-bearing unquoted headers belong to the transport.
        body = '\n'.join('> ' + line for line in message['body'].splitlines())
        if len(body.encode('utf-8')) > budget:
            suffix = '\n[truncated; inbox read --message ' + message['message_id'] + ']'
            room = budget - len(suffix.encode('utf-8'))
            body = truncate_utf8(body,room) + suffix if room > 0 else omitted[index]
        pieces.extend((header,body))
        available -= len(body.encode('utf-8'))
    content = ''.join(pieces)
    return content if len(content.encode('utf-8')) <= BUNDLE_BYTES else None


def wake_ready_at(row: Mapping[str, Any], coalesce_seconds: float) -> float:
    """Use recipient-host queue age for idle FYI, never the sender's clock."""
    coalesced = row['created_at'] + coalesce_seconds
    if row['kind'] == 'fyi' and not row['fyi_turn_end']:
        return max(coalesced, row['queued_at'] + FYI_WAKE_DELAY_SECONDS)
    return coalesced


def fit_claim_rows(rows: Sequence[Mapping[str, Any]], *, wake_trigger: str | None = None) -> list[Mapping[str, Any]]:
    """Largest representable prefix; a wake retains its admitted trigger."""
    marker = 'HAM_CLAIM_' + '0' * 36 + '_' + '0' * 32  # same byte length as the real marker
    for count in range(len(rows),0,-1):
        chosen = list(rows[:count])
        if wake_trigger is not None and not any(row['message_id'] == wake_trigger for row in chosen):
            trigger = next((row for row in rows if row['message_id'] == wake_trigger),None)
            if trigger is None:
                return []
            chosen = sorted([*chosen[:-1],trigger],key=lambda row:(row['created_at'],row['message_id']))
        if render_bundle([dict(row) for row in chosen],marker) is not None:
            return chosen
    return []


def format_context(claim: Mapping[str, Any], agent: Mapping[str, Any], marker: str) -> dict[str, Any]:
    content = render_bundle(claim['messages'],marker)
    if content is None:  # fit_claim_rows already proved this; never ACK an unrepresented claim.
        raise LedgerError('context_too_large','Claimed messages are not all representable in the native context.')
    details = {'protocol':1,'claim_id':claim['claim_id'],'agent_id':claim['agent_id'],
               'generation':claim['generation'],'session_ref':agent['session_ref'],'marker':marker}
    return {'customType':'ham_mailbox','content':content,'details':details,'display':True,'attribution':'agent'}


class NativeReceiver:
    def __init__(self,store: LedgerStore, *, wake_adapters: Mapping[str, Any] | None = None,
                 environment: Mapping[str, str] | None = None) -> None:
        self.store = store
        self.wake_adapters = dict(wake_adapters or {})
        self.environment = os.environ if environment is None else environment
        self._claude_scans: dict[str, dict[str, Any]] = {}

    def register(self, agent: Mapping[str, Any], *, session_ref: str, client: str = 'omp') -> dict[str, Any]:
        if client not in RECEIVER_CLIENTS or agent['client'] != client:
            raise LedgerError('unsupported_client','Receiver registration requires the live OMP or pi agent slot.')
        if not isinstance(session_ref,str) or not Path(session_ref).is_absolute():
            raise LedgerError('invalid_session','Receiver session_ref must be its full supported session-file path.')
        registered = AgentRegistry(self.store).agent_session(terminal_id=agent['terminal_id'],
                session_ref=session_ref,client=client,repo_id=agent['repo_id'],extension_present=True,
                expected_agent_id=agent['agent_id'],expected_generation=agent['generation'],
                environment=self.environment)
        return {'agent':registered,'bell_directory':str(self.store.bell_directory),
                'bell_name':quote(registered['terminal_id'],safe='._-:'),
                **self.inspect(terminal_id=registered['terminal_id'],agent_id=registered['agent_id'],generation=registered['generation'])}

    def _state(self,connection: Any,agent: Mapping[str, Any],now: float) -> dict[str, Any]:
        fields = RepoSettings.fields(connection,agent['repo_id'])
        thresholds = fields.get('thresholds',{}).get('value',dict(DEFAULT_THRESHOLDS))
        mode = fields.get('mode',{}).get('value','off')
        role = RepoSettings(self.store).role(agent, connection=connection)
        wake_count = connection.execute('SELECT count(*) FROM wake_log WHERE agent_id=? AND at>?',
                                       (agent['agent_id'],now-3600)).fetchone()[0]
        reservations = connection.execute("SELECT count(DISTINCT claim_id) FROM deliveries WHERE claimed_agent_id=? "
                "AND via='wake' AND state='delivering' AND updated_at>?",(agent['agent_id'],now-3600)).fetchone()[0]
        status = connection.execute('SELECT turn_state,summary FROM agent_status WHERE agent_id=?',(agent['agent_id'],)).fetchone()
        pending_claims = [dict(row) for row in connection.execute(
                "SELECT DISTINCT n.*,d.claimed_agent_id AS agent_id,d.claimed_generation AS generation FROM native_claims n "
                "JOIN deliveries d USING(claim_id) WHERE d.state='delivering' AND d.claimed_agent_id=?",
                (agent['agent_id'],))]
        return {'thresholds':thresholds,'mode':mode,'role':role,'turn_state':status[0] if status else 'unknown',
                'summary':sanitize_status_text(status[1], 200) if status else '',
                'wake_count':wake_count+reservations,'pending_claims':pending_claims}

    def _eligible(self,connection: Any,agent: Mapping[str, Any],maximum_hop: int,*,wake: bool,
                  coalesce_seconds: float = 0, now: float = 0) -> list[Any]:
        hold_gate = "(m.kind='action' OR d.hold_reason IS NULL)" if wake else '(d.fyi_turn_end=0 OR d.hold_reason IS NULL)'
        base = ('SELECT m.*,d.recipient_address,d.recipient_repo_id,d.fyi_turn_end,d.updated_at AS queued_at '
                'FROM messages m JOIN deliveries d USING(message_id) '
                "WHERE d.recipient_host=? AND d.recipient_terminal_id=? AND d.state='queued' "
                'AND d.recipient_repo_id=? AND m.hop<=? '
                'AND '+hold_gate+' ')
        parameters = (self.store.host,agent['terminal_id'],agent['repo_id'],maximum_hop)
        rows = connection.execute(base+'ORDER BY m.created_at,m.message_id LIMIT ?',(*parameters,BUNDLE_MESSAGES)).fetchall()
        if wake and rows:
            # The earliest deadline may be beyond the oldest bounded prefix
            # (e.g. a young FYI backlog followed by an action or turn-end FYI).
            trigger = connection.execute(
                base+"ORDER BY CASE WHEN m.kind='action' AND m.created_at+?<=? THEN 0 ELSE 1 END,"
                "CASE WHEN m.kind='fyi' AND d.fyi_turn_end=0 "
                "THEN MAX(d.updated_at+?,m.created_at+?) ELSE m.created_at+? END,"
                "m.created_at,m.message_id LIMIT 1",
                (*parameters,coalesce_seconds,now,FYI_WAKE_DELAY_SECONDS,coalesce_seconds,coalesce_seconds)).fetchone()
            if not any(row['message_id'] == trigger['message_id'] for row in rows):
                rows = sorted([*rows[:BUNDLE_MESSAGES-1],trigger],key=lambda row:(row['created_at'],row['message_id']))
        return rows

    def _skip_input_quiet(self, connection: Any, agent: Mapping[str, Any],
                          rows: Sequence[Mapping[str, Any]], now: float) -> bool:
        """One bounded operator request, scoped to this session's requested FYI.

        Automatic turn-end flags and legacy audit arrays are not authorization.
        A confirmed delivery of any requested message consumes the whole request,
        including natural receipt; a definitely unapplied claim does not.
        """
        request = connection.execute(
                "SELECT value,at FROM status_log WHERE agent_id=? AND field='fyi_now' ORDER BY seq DESC LIMIT 1",
                (agent['agent_id'],)).fetchone()
        if request is None or not request['at'] <= now < request['at']+FYI_NOW_QUIET_SECONDS:
            return False
        try:
            value = json.loads(request['value'])
        except (ValueError, TypeError):
            return False
        if (not isinstance(value, dict) or value.get('generation') != agent['generation']
                or value.get('session_ref') != agent['session_ref']
                or not isinstance(value.get('message_ids'), list)):
            return False
        message_ids = {item for item in value['message_ids'] if isinstance(item, str)}
        if not any(row['kind']=='fyi' and row['message_id'] in message_ids for row in rows):
            return False
        delivered = connection.execute(
                'SELECT message_id FROM deliveries WHERE recipient_host=? AND recipient_terminal_id=? '
                'AND recipient_repo_id=? AND delivered_at>=?',
                (self.store.host,agent['terminal_id'],agent['repo_id'],request['at']))
        return not any(row['message_id'] in message_ids for row in delivered)

    def _settle_moot_cancels(self, connection: Any, agent: Mapping[str, Any], now: float) -> list[str]:
        """Close queued cancels whose original never reached this agent.

        A cross-host cancel follows its withdrawal request in the same FIFO. When
        that withdrawal won, the original is ``withdrawn`` here (or only a
        tombstone remains) and there is no work to stop or report.
        """
        rows = connection.execute(
            "SELECT m.message_id,m.reply_to FROM messages m JOIN deliveries d USING(message_id) "
            "WHERE d.recipient_host=? AND d.recipient_terminal_id=? AND d.recipient_repo_id=? "
            "AND d.state='queued' AND m.urgency='cancel'",
            (self.store.host,agent['terminal_id'],agent['repo_id'])).fetchall()
        settled = []
        for row in rows:
            target = connection.execute('SELECT state FROM deliveries WHERE message_id=?',(row['reply_to'],)).fetchone()
            tombstone = connection.execute('SELECT 1 FROM withdraw_tombstones WHERE message_id=?',
                                           (row['reply_to'],)).fetchone()
            if (target is not None and target['state']=='withdrawn') or (target is None and tombstone is not None):
                Mailbox._handled(connection,row['message_id'],now)
                connection.execute("UPDATE deliveries SET evidence='cancel:target_withdrawn' WHERE message_id=?",
                                   (row['message_id'],))
                settled.append(row['message_id'])
        return settled

    def _urgent_rows(self, connection: Any, agent: Mapping[str, Any], maximum_hop: int,
                     urgencies: Sequence[str]) -> list[Any]:
        placeholders = ','.join('?' for _ in urgencies)
        return connection.execute(
            'SELECT m.*,d.recipient_address,d.recipient_repo_id,d.fyi_turn_end,d.updated_at AS queued_at '
            'FROM messages m JOIN deliveries d USING(message_id) '
            "WHERE d.recipient_host=? AND d.recipient_terminal_id=? AND d.state='queued' "
            f'AND d.recipient_repo_id=? AND m.hop<=? AND m.urgency IN ({placeholders}) '
            'ORDER BY m.created_at,m.message_id LIMIT ?',
            (self.store.host,agent['terminal_id'],agent['repo_id'],maximum_hop,*urgencies,BUNDLE_MESSAGES)).fetchall()

    def _inbox_preview(self, connection: Any, agent: Mapping[str, Any]) -> list[dict[str, Any]]:
        """View-only rows for an in-session mailbox display; never a claim or ACK."""
        result = []
        for row in connection.execute(
                'SELECT m.message_id,m.thread_id,m.kind,m.urgency,m.reply_to,m.origin_host,m.sender,m.sender_label,'
                'm.body,m.hop,m.created_at,d.state,t.subject FROM messages m JOIN deliveries d USING(message_id) '
                'LEFT JOIN threads t ON t.thread_id=m.thread_id '
                "WHERE d.recipient_host=? AND d.recipient_terminal_id=? AND d.recipient_repo_id=? "
                "AND d.state IN ('queued','delivering') ORDER BY m.created_at,m.message_id LIMIT ?",
                (self.store.host,agent['terminal_id'],agent['repo_id'],INBOX_PREVIEW_LIMIT)):
            body = ' '.join(safe_label(line, INBOX_PREVIEW_BYTES) for line in str(row['body']).splitlines()).strip()
            result.append({'message_id':row['message_id'],'thread_id':row['thread_id'],'kind':row['kind'],
                           'urgency':row['urgency'],'target_message_id':row['reply_to'] if row['urgency'] else None,
                           'sender':sender_address(dict(row)),'subject':safe_label(row['subject'] or '',200),
                           'preview':truncate_utf8(body,INBOX_PREVIEW_BYTES),'state':row['state'],
                           'hop':row['hop'],'created_at':row['created_at']})
        return result

    def inspect(self,*,terminal_id: str,agent_id: str,generation: int) -> dict[str, Any]:
        now = time.time()
        with self.store.read() as connection:
            agent = check_generation(connection,self.store.host,terminal_id,agent_id,generation)
            state = self._state(connection,agent,now)
            urgent = []
            for row in self._urgent_rows(connection,agent,state['thresholds']['max_hop'],URGENCIES):
                if row['urgency']=='cancel':
                    target = connection.execute('SELECT state FROM deliveries WHERE message_id=?',
                                                (row['reply_to'],)).fetchone()
                    tombstone = connection.execute('SELECT 1 FROM withdraw_tombstones WHERE message_id=?',
                                                   (row['reply_to'],)).fetchone()
                    if (target is not None and target['state']=='withdrawn') or (target is None and tombstone):
                        continue  # moot: settled by the next claim, nothing to interrupt
                urgent.append({'message_id':row['message_id'],'urgency':row['urgency'],
                               'target_message_id':row['reply_to'],'thread_id':row['thread_id']})
            inbox = self._inbox_preview(connection,agent)
            queued_total = connection.execute(
                "SELECT count(*) FROM deliveries WHERE recipient_host=? AND recipient_terminal_id=? "
                "AND recipient_repo_id=? AND state='queued'",
                (self.store.host,terminal_id,agent['repo_id'])).fetchone()[0]
            rows = self._eligible(connection,agent,state['thresholds']['max_hop'],wake=True,
                                  coalesce_seconds=state['thresholds']['coalesce_seconds'],now=now)
            trigger = min(rows,key=lambda row:(
                not (row['kind']=='action' and wake_ready_at(row,state['thresholds']['coalesce_seconds'])<=now),
                wake_ready_at(row,state['thresholds']['coalesce_seconds'])),default=None)
            bundle = fit_claim_rows(rows,wake_trigger=trigger['message_id']) if trigger is not None else []
            skip_input_quiet = self._skip_input_quiet(connection,agent,bundle,now)
        # Keep young ordinary FYI out of the extension's one-second editor/status
        # guard retries. Its existing safety interval and natural events recheck.
        triggers = [row for row in rows if row['kind']=='action' or row['fyi_turn_end']
                    or wake_ready_at(row,state['thresholds']['coalesce_seconds'])<=now]
        return {**state,'capabilities':list(RECEIVER_CAPABILITIES),
                'queued_count':len(rows),'has_action':bool(triggers),'skip_input_quiet':skip_input_quiet,
                'coalesce_until':min((wake_ready_at(row,state['thresholds']['coalesce_seconds']) for row in triggers),default=0),
                'urgent':urgent,'inbox':inbox,'queued_total':queued_total}

    def fyi_now(self,*,terminal_id: str,agent_id: str | None = None,generation: int | None = None) -> dict[str, Any]:
        """Request queued FYI without its delay and with bounded input-quiet bypass.

        Every accepted press snapshots eligible FYI and refreshes a session-bound
        status_log request, even if enqueue-time/earlier requests already set
        ``fyi_turn_end``. OMP uses ``skip_input_quiet``; Claude/Codex skip their
        continuous-idle proxy. All other wake gates, including coalescing, remain.
        The request expires after 600 seconds or a scoped confirmed delivery.
        Discovery failures flag nothing; this local-only operation never writes
        wake_log or injects editor input.
        """
        now = time.time()
        # Wake availability is probed outside the write transaction: discovery
        # runs subprocesses and must never hold the ledger lock.
        unavailable = None
        with self.store.read() as connection:
            probe = connection.execute('SELECT * FROM agents WHERE host=? AND terminal_id=? AND active=1',
                                       (self.store.host,terminal_id)).fetchone()
        if probe is not None and probe['client'] in WAKE_CLIENTS and wake_capable(dict(probe)):
            unavailable = self.wake_unavailable(dict(probe))
            probe_id = probe['agent_id']
        with self.store.transaction((terminal_id,)) as connection:
            row = connection.execute('SELECT * FROM agents WHERE host=? AND terminal_id=? AND active=1',
                                     (self.store.host,terminal_id)).fetchone()
            if row is None:
                return {'outcome':'not_agent','terminal_id':terminal_id}
            agent = dict(row)
            if agent_id is not None and (agent['agent_id']!=agent_id or agent['generation']!=generation):
                raise LedgerError('stale_session','The agent session/generation has changed.')
            if unavailable is not None and probe_id != agent['agent_id']:
                unavailable = None
            state = self._state(connection,agent,now)
            thresholds = state['thresholds']
            result: dict[str, Any] = {
                'agent':{key:agent[key] for key in ('agent_id','terminal_id','alias','client','generation')},
                'role':state['role'],'turn_state':state['turn_state'],'wake_count':state['wake_count'],
                'wakes_per_hour':thresholds['wakes_per_hour'],'idle_minutes':thresholds['idle_minutes'],
                'queued_fyi':0,'queued_action':0,'held':0,'over_hop':0,'flagged':0,'eligible':0,
                'bundle':0,'remaining':0,'ready_in':0,'limit_frees_at':None}
            queued = connection.execute(
                "SELECT m.message_id,m.kind,m.hop,d.hold_reason,d.fyi_turn_end FROM messages m "
                "JOIN deliveries d USING(message_id) WHERE d.recipient_host=? AND d.recipient_terminal_id=? "
                "AND d.recipient_repo_id=? AND d.state='queued' ORDER BY m.created_at,m.message_id",
                (self.store.host,terminal_id,agent['repo_id'])).fetchall()
            fresh: list[str] = []
            requested: list[str] = []
            for item in queued:
                if item['kind']=='action':
                    result['queued_action'] += 1
                elif item['hold_reason'] is not None:
                    result['held'] += 1
                elif item['hop']>thresholds['max_hop']:
                    result['over_hop'] += 1
                else:
                    result['queued_fyi'] += 1
                    requested.append(item['message_id'])
                    if item['fyi_turn_end']:
                        result['eligible'] += 1
                    else:
                        fresh.append(item['message_id'])
            if not wake_capable(agent):
                return {**result,'outcome':'no_receiver'}
            if unavailable is not None:
                return {**result,'outcome':'no_receiver','reason':unavailable[0],'detail':unavailable[1]}
            if state['role'] is None:
                return {**result,'outcome':'not_participating'}
            if not result['queued_fyi']:
                return {**result,'outcome':'only_held' if result['held'] else 'nothing_queued'}
            if fresh:
                connection.execute('UPDATE deliveries SET fyi_turn_end=1 WHERE message_id IN (%s) '
                                   "AND state='queued' AND fyi_turn_end=0 AND hold_reason IS NULL"
                                   % ','.join('?' for _ in fresh),fresh)
            connection.execute("INSERT INTO status_log(agent_id,field,value,at) VALUES (?,'fyi_now',?,?)",
                               (agent['agent_id'],json.dumps({
                                   'generation':agent['generation'],'session_ref':agent['session_ref'],
                                   'message_ids':requested}),now))
            result['flagged'] = len(fresh)
            result['eligible'] += len(fresh)
            rows = self._eligible(connection,agent,thresholds['max_hop'],wake=True,
                                  coalesce_seconds=thresholds['coalesce_seconds'],now=now)
            trigger = min(rows,key=lambda item:(
                not (item['kind']=='action' and wake_ready_at(item,thresholds['coalesce_seconds'])<=now),
                wake_ready_at(item,thresholds['coalesce_seconds'])))
            result['bundle'] = len(fit_claim_rows(rows,wake_trigger=trigger['message_id']))
            result['remaining'] = connection.execute(
                "SELECT count(*) FROM messages m JOIN deliveries d USING(message_id) WHERE d.recipient_host=? "
                "AND d.recipient_terminal_id=? AND d.recipient_repo_id=? AND d.state='queued' AND m.hop<=? "
                "AND (m.kind='action' OR d.hold_reason IS NULL)",
                (self.store.host,terminal_id,agent['repo_id'],thresholds['max_hop'])).fetchone()[0]-result['bundle']
            result['ready_in'] = max(0,wake_ready_at(trigger,thresholds['coalesce_seconds'])-now)
            limit = thresholds['wakes_per_hour']
            if state['wake_count']>=limit:
                log = [item[0] for item in connection.execute(
                    'SELECT at FROM wake_log WHERE agent_id=? AND at>? ORDER BY at',(agent['agent_id'],now-3600))]
                if limit>0 and len(log)>=limit:
                    result['limit_frees_at'] = log[len(log)-limit]+3600
                result['outcome'] = 'hourly_limit'
            elif state['turn_state'] not in WAKE_READY_STATES:
                result['outcome'] = 'after_turn'
            else:
                result['outcome'] = 'requested'
        if result['agent']['client'] in WAKE_CLIENTS and result.get('queued_fyi'):
            # Claude/Codex are woken by the ledger worker, not by an extension
            # watching the bell: make sure that worker is running.
            result = self._ensure_worker(result)
        return self.store.annotate(result)

    @staticmethod
    def _observed_idle(observed_idle: Any, observed_at: Any, *, wake: bool, client: str | None,
                       now: float) -> str | None:
        """Validate a pi receiver's own idle observation; return 'fresh', 'stale' or None (absent)."""
        if observed_idle is None and observed_at is None:
            return None
        if observed_idle is not True:
            raise LedgerError('invalid_request','observed_idle must be true when given.')
        if not wake:
            raise LedgerError('invalid_request','observed_idle applies to a wake claim only.')
        if (isinstance(observed_at,bool) or not isinstance(observed_at,(int,float))
                or observed_at!=observed_at or observed_at in (float('inf'),float('-inf'))):
            raise LedgerError('invalid_request','observed_at must be the epoch seconds of the idle observation.')
        if client is not None and client!='pi':
            raise LedgerError('unsupported_client','observed_idle is accepted from a pi receiver only.')
        if observed_at>now+OBSERVED_IDLE_FUTURE_SKEW_SECONDS or now-observed_at>OBSERVED_IDLE_MAX_AGE_SECONDS:
            return 'stale'
        return 'fresh'

    def claim(self,*,terminal_id: str,agent_id: str,generation: int,wake: bool = False,
              urgent: Sequence[str] | None = None, message_ids: Sequence[str] | None = None,
              observed_idle: bool | None = None, observed_at: float | None = None) -> dict[str, Any]:
        """Claim one bounded bundle for the in-session receiver.

        ``urgent`` (a list of amend/cancel) claims only those queued urgent
        messages plus any still-queued originals they reference. It skips turn
        state, coalescing and the hourly wake limit (the sender explicitly asked
        to reach a working agent) but keeps registration and the hop limit.
        ``message_ids`` optionally narrows an urgent claim, e.g. to the amends
        whose original thread the receiver is currently working on.

        ``observed_idle=True`` with ``observed_at`` (epoch seconds, at most 60 s
        old) lets a pi receiver's wake claim stand on its own idle observation:
        only the Herdr turn_state gate is skipped. Role, hourly limit, coalescing
        and hop gates still apply. A stale observation refuses with
        ``observed_stale`` rather than falling back to turn_state.
        """
        if urgent is not None:
            if (isinstance(urgent,(str,bytes)) or not isinstance(urgent,Sequence) or not urgent
                    or any(value not in URGENCIES for value in urgent)):
                raise LedgerError('invalid_urgency','urgent must list amend and/or cancel.')
            if wake:
                raise LedgerError('invalid_request','An urgent claim is not a wake claim.')
        if message_ids is not None:
            if (urgent is None or isinstance(message_ids,(str,bytes)) or not isinstance(message_ids,Sequence)
                    or not message_ids):
                raise LedgerError('invalid_request','message_ids narrows a nonempty urgent claim only.')
            for value in message_ids:
                require_uuid(value,'message_ids')
        now = time.time()
        self._observed_idle(observed_idle,observed_at,wake=wake,client=None,now=now)
        with self.store.transaction((terminal_id,)) as connection:
            agent = check_generation(connection,self.store.host,terminal_id,agent_id,generation)
            if agent['client'] not in RECEIVER_CLIENTS or not agent['extension_present']:
                raise LedgerError('extension_absent','Only a registered OMP or pi receiver may admit native context.')
            # Re-evaluated inside the transaction: the ledger lock wait counts toward its age.
            observation = self._observed_idle(observed_idle,observed_at,wake=wake,client=agent['client'],
                                              now=time.time())
            if observation=='stale':
                return {'claim_id':None,'reason':'observed_stale','messages':[]}
            self._settle_moot_cancels(connection,agent,now)
            state = self._state(connection,agent,now)
            if urgent is not None:
                chosen = [row for row in self._urgent_rows(connection,agent,state['thresholds']['max_hop'],
                                                           tuple(dict.fromkeys(urgent)))
                          if message_ids is None or row['message_id'] in set(message_ids)]
                if not chosen:
                    return {'claim_id':None,'reason':'empty','messages':[]}
                targets = {row['reply_to'] for row in chosen}
                placeholders = ','.join('?' for _ in targets)
                originals = connection.execute(
                    'SELECT m.*,d.recipient_address,d.recipient_repo_id,d.fyi_turn_end,d.updated_at AS queued_at '
                    'FROM messages m JOIN deliveries d USING(message_id) '
                    "WHERE d.recipient_host=? AND d.recipient_terminal_id=? AND d.state='queued' "
                    f'AND d.recipient_repo_id=? AND m.message_id IN ({placeholders})',
                    (self.store.host,terminal_id,agent['repo_id'],*targets)).fetchall()
                rows = fit_claim_rows(sorted({row['message_id']:row for row in [*originals,*chosen]}.values(),
                                             key=lambda row:(row['created_at'],row['message_id'])))
                if not any(row['urgency'] for row in rows):
                    return {'claim_id':None,'reason':'unrepresentable','messages':[]}
                return self._native_claim(connection,agent,rows,'urgent',now,skip_input_quiet=False)
            rows = self._eligible(connection,agent,state['thresholds']['max_hop'],wake=wake,
                                  coalesce_seconds=state['thresholds']['coalesce_seconds'],now=now)
            trigger = min(rows,key=lambda row:(
                not (row['kind']=='action' and wake_ready_at(row,state['thresholds']['coalesce_seconds'])<=now),
                wake_ready_at(row,state['thresholds']['coalesce_seconds'])),default=None) if wake else None
            reason = (self._wake_reason(state,trigger,now,observed_idle=observation=='fresh')
                      if wake else None)
            if reason or not rows:
                return {'claim_id':None,'reason':reason or 'empty','messages':[]}
            rows = fit_claim_rows(rows,wake_trigger=trigger['message_id'] if trigger is not None else None)
            if not rows:
                return {'claim_id':None,'reason':'unrepresentable','messages':[]}
            skip_input_quiet = self._skip_input_quiet(connection,agent,rows,now)
            result = self._native_claim(connection,agent,rows,'wake' if wake else 'before_agent_start',now,
                                        skip_input_quiet=skip_input_quiet)
            if wake:
                result['idle_source'] = PI_OBSERVED_IDLE if observation=='fresh' else 'turn_state'
            return result

    @staticmethod
    def _native_claim(connection: Any, agent: Mapping[str, Any], rows: Sequence[Any], via: str, now: float, *,
                      skip_input_quiet: bool) -> dict[str, Any]:
        claim = Mailbox._claim_messages(connection,rows,agent['agent_id'],agent['generation'],via)
        marker = 'HAM_CLAIM_' + claim['claim_id'] + '_' + uuid.uuid4().hex
        message = format_context(claim,agent,marker)
        digest = hashlib.sha256(message['content'].encode('utf-8')).hexdigest()
        connection.execute('INSERT INTO native_claims '
                '(claim_id,client,session_ref,marker,content_sha256,created_at) VALUES (?,?,?,?,?,?)',
                (claim['claim_id'],agent['client'],agent['session_ref'],marker,digest,now))
        return {**claim,'session_ref':agent['session_ref'],'marker':marker,'content_sha256':digest,'message':message,
                'skip_input_quiet':skip_input_quiet,'via':via}

    def applied(self,*,claim_id: str,agent_id: str,generation: int,session_ref: str,
                marker: str,content_sha256: str,entry_id: str) -> dict[str, Any]:
        with self.store.read() as connection:
            row = connection.execute('SELECT * FROM native_claims WHERE claim_id=?',(claim_id,)).fetchone()
        if (row is None or row['client'] not in RECEIVER_CLIENTS or row['session_ref']!=session_ref or row['marker']!=marker or
                row['content_sha256']!=content_sha256 or not isinstance(entry_id,str) or not entry_id):
            raise LedgerError('invalid_evidence','ACK must match the exact session, claim marker and accepted context hash.')
        # Trust boundary: the root extension has inspected a public read-only
        # active-branch custom_message entry. This is host journal acceptance,
        # not proof of later context/provider transforms or model understanding.
        return Mailbox(self.store).started(claim_id=claim_id,agent_id=agent_id,generation=generation,
                evidence=row['client']+':accepted-journal:'+session_ref+'#'+entry_id+';sha256='+content_sha256)

    @staticmethod
    def _wake_reason(state: Mapping[str, Any], trigger: Mapping[str, Any] | None, now: float, *,
                     external: bool = False, ready_since: float | None = None,
                     observed_idle: bool = False) -> str | None:
        """First gate that refuses a wake; ``external`` adds the worker-driven gates.

        OMP evaluates quiet/idle/editor in its own extension, so its claim path
        never asks for them. A worker-driven wake cannot observe input, so it
        also requires no pending claim and, unless explicitly requested by the
        operator, a continuous ready period of ``idle_minutes`` (the quiet proxy).
        ``observed_idle`` (a fresh pi receiver observation) replaces only the
        turn_state gate.
        """
        thresholds = state['thresholds']
        if state['role'] is None:
            return 'not_participating'
        if not observed_idle and state['turn_state'] not in WAKE_READY_STATES:
            return 'not_idle'
        if external:
            if state['pending_claims']:
                return 'claim_pending'
            if not state.get('skip_input_quiet',False) and (
                    ready_since is None or now-ready_since<thresholds['idle_minutes']*60):
                return 'quiet'
        if state['wake_count']>=thresholds['wakes_per_hour']:
            return 'hourly_limit'
        if trigger is None:
            return 'no_action'
        if wake_ready_at(trigger,thresholds['coalesce_seconds'])>now:
            return 'coalescing'
        return None

    @staticmethod
    def wake_ready_since(connection: Any, agent_id: str, fallback: float | None) -> float | None:
        """Latest continuous ready run, read under the caller's ledger snapshot.

        A durable non-ready transition invalidates an older in-memory baseline,
        even if the agent returned to idle during external target discovery.
        With no status history, retain the worker's conservative first sighting.
        """
        since = None
        observed = False
        for value, at in connection.execute(
                "SELECT value,at FROM status_log WHERE agent_id=? AND field='turn_state' "
                "ORDER BY seq DESC LIMIT ?", (agent_id, STATUS_RUN_LIMIT)):
            observed = True
            if value not in WAKE_READY_STATES:
                break
            since = at
        return since if observed else fallback

    def wake_gate(self,connection: Any,agent: Mapping[str, Any],now: float,
                  *,ready_since: float | None) -> tuple[dict[str, Any], list[Any], Any, str | None]:
        """Worker-driven wake admission: (state, eligible rows, trigger row, refusal reason)."""
        state = self._state(connection,agent,now)
        thresholds = state['thresholds']
        rows = self._eligible(connection,agent,thresholds['max_hop'],wake=True,
                              coalesce_seconds=thresholds['coalesce_seconds'],now=now)
        trigger = min(rows,key=lambda row:(
            not (row['kind']=='action' and wake_ready_at(row,thresholds['coalesce_seconds'])<=now),
            wake_ready_at(row,thresholds['coalesce_seconds'])),default=None)
        bundle = fit_claim_rows(rows,wake_trigger=trigger['message_id']) if trigger is not None else []
        state['skip_input_quiet'] = self._skip_input_quiet(connection,agent,bundle,now)
        return state,rows,trigger,self._wake_reason(state,trigger,now,external=True,ready_since=ready_since)

    def wake_adapter(self, client: str) -> Any:
        """Client adapter module (discover/deliver/observe/alive); tests inject fakes."""
        if client in self.wake_adapters:
            return self.wake_adapters[client]
        import importlib
        return importlib.import_module('ham.ledger.wake_'+client)

    def wake_unavailable(self, agent: Mapping[str, Any]) -> tuple[str, str] | None:
        """Why a wake-capable agent cannot be woken right now, or None. Read-only probe."""
        from ham.ledger.wake_clients import WakeUnavailable
        try:
            self.wake_adapter(agent['client']).discover(agent,environment=self.environment)
        except WakeUnavailable as error:
            return error.code,error.message
        except (OSError,ValueError,TypeError,KeyError,ImportError) as error:
            return 'discover_failed',str(error)
        return None

    def _ensure_worker(self, result: dict[str, Any]) -> dict[str, Any]:
        if self.environment.get('HERDR_AGENT_LEDGER_AUTOSTART','1')=='0':
            return result
        from ham.ledger.worker import ensure_worker
        try:
            ensure_worker(self.store,self.environment)
        except (LedgerError,OSError) as error:
            return {**result,'warnings':[*result.get('warnings',[]),
                    {'code':'worker_start_failed','message':str(error),'committed':True}]}
        return result

    def claim_wake(self, agent: Mapping[str, Any], target: Any, *, ready_since: float | None,
                   now: float | None = None) -> dict[str, Any]:
        """Claim the eligible bundle for a worker-driven Claude/Codex wake.

        Every wake gate is re-evaluated in the claiming transaction. The claim
        is a normal ``via='wake'`` delivery (so ``wake_log`` and the hourly
        limit count it like OMP) whose native claim binds the journal baseline
        the client adapter will scan for evidence.
        """
        now = time.time() if now is None else now
        with self.store.transaction((agent['terminal_id'],)) as connection:
            current = check_generation(connection,self.store.host,agent['terminal_id'],
                                       agent['agent_id'],agent['generation'])
            if (current['client'] not in WAKE_CLIENTS or current['client']!=target.client
                    or not wake_capable(current)
                    or (current['client']=='claude' and current['session_ref']!=target.session_ref)):
                return {'claim_id':None,'reason':'stale_target','messages':[]}
            self._settle_moot_cancels(connection,current,now)
            durable_ready_since = self.wake_ready_since(connection,current['agent_id'],ready_since)
            ready_since = (max(ready_since,durable_ready_since)
                           if ready_since is not None and durable_ready_since is not None else None)
            _state,rows,trigger,reason = self.wake_gate(connection,current,now,ready_since=ready_since)
            if reason or not rows:
                return {'claim_id':None,'reason':reason or 'empty','messages':[]}
            rows = fit_claim_rows(rows,wake_trigger=trigger['message_id'])
            if not rows:
                return {'claim_id':None,'reason':'unrepresentable','messages':[]}
            claim = Mailbox._claim_messages(connection,rows,current['agent_id'],current['generation'],'wake')
            marker = 'HAM_CLAIM_' + claim['claim_id'] + '_' + uuid.uuid4().hex
            content = format_context(claim,current,marker)['content']
            encoded = content.encode('utf-8')
            digest = hashlib.sha256(encoded).hexdigest()
            connection.execute('INSERT INTO native_claims '
                    '(claim_id,client,session_ref,marker,content_sha256,created_at,'
                    'transcript_path,transcript_start_offset,content_bytes) VALUES (?,?,?,?,?,?,?,?,?)',
                    (claim['claim_id'],current['client'],target.session_ref,marker,digest,now,
                     target.journal_path,target.journal_offset,len(encoded)))
        return {**claim,'session_ref':target.session_ref,'marker':marker,'content':content,
                'content_sha256':digest,'content_bytes':len(encoded)}

    def claim_content(self, claim: Mapping[str, Any]) -> str | None:
        """Rebuild a wake claim's exact text from its immutable messages (worker restarts).

        Returns None unless the rebuilt bundle matches the stored hash, so an
        unverifiable claim is never observed against guessed content.
        """
        with self.store.read() as connection:
            rows = [dict(row) for row in connection.execute(
                'SELECT m.*,d.recipient_repo_id FROM messages m JOIN deliveries d USING(message_id) '
                'WHERE d.claim_id=? ORDER BY m.created_at,m.message_id',(claim['claim_id'],))]
        content = render_bundle(rows,claim['marker']) if rows else None
        if (content is None or len(content.encode('utf-8'))!=claim['content_bytes']
                or hashlib.sha256(content.encode('utf-8')).hexdigest()!=claim['content_sha256']):
            return None
        return content

    def claude_hook(self, agent: Mapping[str, Any], *, hook_event_name: str,
                    session_id: str, transcript_path: str,
                    subagent_id: str | None = None,
                    environment: Mapping[str, str] | None = None) -> dict[str, Any]:
        empty = {'agent':dict(agent),'claim':None,'additional_context':None}
        if hook_event_name not in ('SessionStart','UserPromptSubmit') or subagent_id:
            return empty
        if agent['client']!='claude' or not isinstance(session_id,str):
            return empty
        try:
            if (str(uuid.UUID(session_id)) != session_id.lower()
                    or agent['session_ref'] not in (session_id, 'unreported')):
                return empty
            path = Path(transcript_path)
            # A root transcript is a UUID file directly within a project folder.
            # Never follow Claude's continuation pointers or a subagent transcript.
            if (not path.is_absolute() or path.name!=session_id+'.jsonl' or
                    path.parent.parent.name!='projects' or 'subagents' in path.parts or path.is_symlink()):
                return empty
            resolved = path.resolve()
            if resolved!=path:
                return empty
            offset = path.stat().st_size if path.exists() else 0
        except (ValueError,TypeError,OSError):
            return empty
        registered = AgentRegistry(self.store).agent_session(terminal_id=agent['terminal_id'],
                session_ref=session_id,client='claude',repo_id=agent['repo_id'],extension_present=True,
                expected_agent_id=agent['agent_id'],expected_generation=agent['generation'],
                environment=environment)
        empty = {**empty, 'agent': registered}
        now = time.time()
        with self.store.transaction((registered['terminal_id'],)) as connection:
            current = check_generation(connection,self.store.host,registered['terminal_id'],
                                       registered['agent_id'],registered['generation'])
            if current['client']!='claude' or current['session_ref']!=session_id:
                return empty
            pending = connection.execute(
                    "SELECT 1 FROM native_claims n JOIN deliveries d USING(claim_id) "
                    "WHERE n.client='claude' AND d.state='delivering' AND d.claimed_agent_id=? "
                    "AND d.claimed_generation=? LIMIT 1",(registered['agent_id'],registered['generation'])).fetchone()
            if pending:
                return {**empty,'agent':registered}
            self._settle_moot_cancels(connection,current,now)
            state = self._state(connection,current,now)
            rows = fit_claim_rows(self._eligible(connection,current,state['thresholds']['max_hop'],wake=False))
            if not rows:
                return {**empty,'agent':registered}
            claim = Mailbox._claim_messages(connection,rows,registered['agent_id'],registered['generation'],'hook')
            marker = 'HAM_CLAIM_' + claim['claim_id'] + '_' + uuid.uuid4().hex
            content = format_context(claim,current,marker)['content']
            encoded = content.encode('utf-8')
            digest = hashlib.sha256(encoded).hexdigest()
            connection.execute('INSERT INTO native_claims '
                    '(claim_id,client,session_ref,marker,content_sha256,created_at,'
                    'transcript_path,transcript_start_offset,content_bytes) VALUES (?,?,?,?,?,?,?,?,?)',
                    (claim['claim_id'],'claude',session_id,marker,digest,now,str(path),offset,len(encoded)))
        return {'agent':registered,'claim':{**claim,'session_ref':session_id,'marker':marker,
                'content_sha256':digest,'content_bytes':len(encoded)},'additional_context':content}

    @staticmethod
    def _claude_text_matches(text: Any, claim: Mapping[str, Any]) -> bool:
        if not isinstance(text, str):
            return False
        encoded = text.encode('utf-8')
        marker = claim['marker'].encode('utf-8')
        start = encoded.find(marker)
        while start != -1:
            bundle = encoded[start:start + claim['content_bytes']]
            if (len(bundle) == claim['content_bytes']
                    and hashlib.sha256(bundle).hexdigest() == claim['content_sha256']):
                return True
            start = encoded.find(marker, start + 1)
        return False

    @staticmethod
    def _claude_blob_matches(record: Mapping[str, Any], claim: Mapping[str, Any]) -> bool:
        message = record.get('message')
        if not isinstance(message, dict) or message.get('role') != 'system':
            return False
        blocks = message.get('content')
        return isinstance(blocks, list) and any(
            isinstance(block, dict) and block.get('type') == 'text'
            and NativeReceiver._claude_text_matches(block.get('text'), claim) for block in blocks)

    @staticmethod
    def _claude_attachment_matches(record: Mapping[str, Any], claim: Mapping[str, Any]) -> bool:
        attachment = record.get('attachment')
        if (record.get('type') != 'attachment' or record.get('isSidechain') is not False
                or record.get('renderedRole') != 'system' or not isinstance(attachment, dict)
                or attachment.get('type') != 'hook_additional_context'
                or attachment.get('hookEvent') not in ('SessionStart', 'UserPromptSubmit')
                or attachment.get('hookName') != attachment.get('hookEvent')):
            return False
        content, rendered = attachment.get('content'), record.get('rendered')
        return (isinstance(content, list) and isinstance(rendered, list)
                and any(NativeReceiver._claude_text_matches(text, claim) for text in content)
                and any(isinstance(block, dict)
                        and NativeReceiver._claude_text_matches(block.get('content'), claim)
                        for block in rendered))

    @staticmethod
    def _claude_journal_evidence(record: Mapping[str, Any], claim: Mapping[str, Any],
                                scan: dict[str, Any]) -> str | None:
        """Link native system hook application to native API response metadata.

        Interactive journals persist rendered hook attachments and their causal
        parent chain instead of SDK api-request blobs. Assistant text is never
        evidence: only native request/message identities on that chain qualify.
        """
        if (record.get('sessionId') != claim['session_ref']
                or record.get('isSidechain') is not False
                or record.get('type') not in ('user', 'assistant', 'attachment', 'system')):
            return None
        node, parent = record.get('uuid'), record.get('parentUuid')
        if (not isinstance(node, str) or not node or len(node) > 128
                or (parent is not None and (not isinstance(parent, str) or len(parent) > 128))):
            return None
        nodes = scan['nodes']
        if node in nodes or len(nodes) >= 512:
            return None
        context = node if NativeReceiver._claude_attachment_matches(record, claim) else nodes.get(parent)
        nodes[node] = context
        message, request = record.get('message'), record.get('requestId')
        if (not context or record.get('type') != 'assistant'
                or not isinstance(request, str) or not request
                or not isinstance(message, dict) or message.get('role') != 'assistant'
                or not isinstance(message.get('id'), str) or not message['id']
                or not isinstance(message.get('model'), str) or not message['model']
                or message['model'] == '<synthetic>' or record.get('isApiErrorMessage') is True):
            return None
        return ('claude:accepted-journal:' + claim['session_ref'] + '#' + request
                + ';hook=' + context + ';sha256=' + claim['content_sha256'])

    def observe_claude(self) -> dict[str, Any]:
        """Observe ordinary persisted request records, never stdout or model echoes.

        The durable offset is the admission baseline. Scan progress and matching
        blob hashes are bounded, derived state; restarts rebuild from that baseline.
        Unsupported formats, missing files and incomplete lines remain uncertain.
        """
        with self.store.read() as connection:
            claims = [dict(row) for row in connection.execute(
                    "SELECT DISTINCT n.*,d.claimed_agent_id AS agent_id,d.claimed_generation AS generation,"
                    "d.recipient_terminal_id AS terminal_id FROM native_claims n JOIN deliveries d USING(claim_id) "
                    "JOIN agents a ON a.agent_id=d.claimed_agent_id AND a.generation=d.claimed_generation "
                    "WHERE n.client='claude' AND d.state='delivering' AND d.via='hook' AND a.active=1 AND a.client='claude' "
                    "AND a.session_ref=n.session_ref ORDER BY n.created_at")]
        active = {row['claim_id'] for row in claims}
        self._claude_scans = {key:value for key,value in self._claude_scans.items() if key in active}
        receipts: list[Any] = []
        for claim in claims:
            if not isinstance(claim['content_bytes'],int) or not 0<claim['content_bytes']<=BUNDLE_BYTES:
                continue
            baseline = claim['transcript_start_offset']
            if not isinstance(baseline,int) or baseline<0:
                continue
            try:
                path = Path(claim['transcript_path'])
                if (path.is_symlink() or path.resolve()!=path or path.name!=claim['session_ref']+'.jsonl' or
                        path.parent.parent.name!='projects' or 'subagents' in path.parts):
                    continue
                with path.open('rb') as stream:
                    stat = os.fstat(stream.fileno())
                    signature = (stat.st_dev,stat.st_ino)
                    scan = self._claude_scans.get(claim['claim_id'])
                    if scan is None or scan['file']!=signature or stat.st_size<scan['offset']:
                        scan = {'file':signature,'offset':baseline,'blobs':set(),'nodes':{}}
                        self._claude_scans[claim['claim_id']] = scan
                    stream.seek(scan['offset'])
                    # A malformed huge line is skipped through its newline without
                    # retaining its contents; complete normal records are bounded.
                    consumed = 0
                    while consumed<4*1024*1024:
                        line = stream.readline(1024*1024+1)
                        if not line:
                            break
                        consumed += len(line)
                        if not line.endswith(b'\n'):
                            if len(line)>1024*1024:
                                scan['discarding'] = True
                                scan['offset'] = stream.tell()
                                continue
                            break
                        scan['offset'] = stream.tell()
                        if scan.pop('discarding',False) or len(line)>1024*1024:
                            continue
                        try:
                            record = json.loads(line)
                        except (ValueError,UnicodeError,RecursionError):
                            continue
                        if not isinstance(record,dict) or record.get('sessionId')!=claim['session_ref']:
                            continue
                        evidence = None
                        if record.get('type')=='api-request-blob':
                            blob = record.get('hash')
                            if (isinstance(blob,str) and len(blob)==64 and
                                    self._claude_blob_matches(record,claim) and len(scan['blobs'])<128):
                                scan['blobs'].add(blob)
                        elif record.get('type')=='api-request':
                            tail, request = record.get('tail'), record.get('id')
                            if not isinstance(tail,list) or not isinstance(request,str) or not request:
                                continue
                            matched = next((value for value in tail if isinstance(value,str) and value in scan['blobs']),None)
                            if matched is not None:
                                evidence = ('claude:api-request:'+claim['session_ref']+'#'+request+
                                            ';blob='+matched+';sha256='+claim['content_sha256'])
                        else:
                            evidence = self._claude_journal_evidence(record, claim, scan)
                        if evidence is None:
                            continue
                        # The worker refreshed the live registry before this scan.
                        # started repeats the generation check transactionally.
                        with self.store.read() as connection:
                            current = check_generation(connection,self.store.host,claim['terminal_id'],
                                    claim['agent_id'],claim['generation'])
                            if current['client']!='claude' or current['session_ref']!=claim['session_ref']:
                                break
                        result = Mailbox(self.store).started(claim_id=claim['claim_id'],
                                agent_id=claim['agent_id'],generation=claim['generation'],evidence=evidence)
                        receipts.extend(result['receipts'])
                        self._claude_scans.pop(claim['claim_id'],None)
                        break
            except (OSError,ValueError,TypeError,LedgerError):
                # Absence/staleness is not confirmed non-application.
                continue
        return {'observed':len(receipts),'receipts':receipts}
