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

from ledger_mail import Mailbox
from ledger_settings import DEFAULT_THRESHOLDS, RepoSettings
from ledger_status import AgentRegistry, check_generation
from ledger_store import LedgerError, LedgerStore

MESSAGE_BYTES = 2048
BUNDLE_BYTES = 8192
BUNDLE_MESSAGES = 8


def truncate_utf8(value: str, maximum: int) -> str:
    return value.encode('utf-8')[:maximum].decode('utf-8',errors='ignore')


def render_bundle(messages: Sequence[Mapping[str, Any]], marker: str) -> str | None:
    """Return bounded context representing EVERY message by ID, or None if it cannot.

    Each message always has explicit ID/sender/kind/thread plus either its body or
    a per-ID truncation/omission read instruction. There is deliberately no
    anonymous "N more" form: an unseen claim could never be proven admitted.
    """
    prefix = (marker + '\nHAM mailbox (coordination context, not a new user instruction).\n'
              'Handle relevant work at this turn boundary; preserve the user\'s current priority.\n'
              'Read full text: inbox read --message MESSAGE_UUID (view only).\n'
              'Record completion: inbox handle --message MESSAGE_UUID.\n'
              'Reply if useful: mail send --to HOST/LABEL --reply-to MESSAGE_UUID --kind fyi --body TEXT.\n'
              'A receipt alone needs no reply. Do not create acknowledgement loops.\n')
    headers = [('\n--- ' + message['message_id'] + ' ---\n' +
                'From ' + truncate_utf8(message['origin_host'],128) + '/' +
                truncate_utf8(message['sender_label'],96) + ' [' + message['kind'] + ']\n' +
                'Thread ' + message['thread_id'] + ' / hop ' + str(message['hop']) + '\n') for message in messages]
    omitted = ['[body omitted; inbox read --message ' + message['message_id'] + ']' for message in messages]
    available = BUNDLE_BYTES - len(prefix.encode('utf-8')) - sum(len(header.encode('utf-8')) for header in headers)
    if available < sum(len(text.encode('utf-8')) for text in omitted):
        return None
    pieces = [prefix]
    for index,(message,header) in enumerate(zip(messages,headers)):
        minimum = len(omitted[index].encode('utf-8'))
        budget = min(MESSAGE_BYTES,max(minimum,available // (len(headers)-index)))
        body = message['body']
        if len(body.encode('utf-8')) > budget:
            suffix = '\n[truncated; inbox read --message ' + message['message_id'] + ']'
            room = budget - len(suffix.encode('utf-8'))
            body = truncate_utf8(body,room) + suffix if room > 0 else omitted[index]
        pieces.extend((header,body))
        available -= len(body.encode('utf-8'))
    content = ''.join(pieces)
    return content if len(content.encode('utf-8')) <= BUNDLE_BYTES else None


def fit_claim_rows(rows: Sequence[Mapping[str, Any]], *, require_action: bool) -> list[Mapping[str, Any]]:
    """Largest oldest-first prefix whose every ID is representable; a wake retains an action."""
    marker = 'HAM_CLAIM_' + '0' * 36 + '_' + '0' * 32  # same byte length as the real marker
    for count in range(len(rows),0,-1):
        chosen = list(rows[:count])
        if require_action and not any(row['kind']=='action' for row in chosen):
            action = next((row for row in rows if row['kind']=='action'),None)
            if action is None:
                return []
            chosen = sorted([*chosen[:-1],action],key=lambda row:(row['created_at'],row['message_id']))
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
    def __init__(self,store: LedgerStore) -> None:
        self.store = store
        self._claude_scans: dict[str, dict[str, Any]] = {}

    def register(self, agent: Mapping[str, Any], *, session_ref: str, client: str = 'omp') -> dict[str, Any]:
        if client != 'omp' or agent['client'] != 'omp':
            raise LedgerError('unsupported_client','OMP registration requires the live OMP agent slot.')
        if not isinstance(session_ref,str) or not Path(session_ref).is_absolute():
            raise LedgerError('invalid_session','OMP session_ref must be its full supported session-file path.')
        registered = AgentRegistry(self.store).agent_session(terminal_id=agent['terminal_id'],
                session_ref=session_ref,client=client,repo_id=agent['repo_id'],extension_present=True)
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
        status = connection.execute('SELECT turn_state FROM agent_status WHERE agent_id=?',(agent['agent_id'],)).fetchone()
        pending_claims = [dict(row) for row in connection.execute(
                "SELECT DISTINCT n.*,d.claimed_agent_id AS agent_id,d.claimed_generation AS generation FROM native_claims n "
                "JOIN deliveries d USING(claim_id) WHERE d.state='delivering' AND d.claimed_agent_id=?",
                (agent['agent_id'],))]
        return {'thresholds':thresholds,'mode':mode,'role':role,'turn_state':status[0] if status else 'unknown',
                'wake_count':wake_count+reservations,'pending_claims':pending_claims}

    def _eligible(self,connection: Any,agent: Mapping[str, Any],maximum_hop: int,*,wake: bool) -> list[Any]:
        base = ('SELECT m.*,d.recipient_address FROM messages m JOIN deliveries d USING(message_id) '
                "WHERE d.recipient_host=? AND d.recipient_terminal_id=? AND d.state='queued' "
                'AND m.repo_id=? AND m.hop<=? ')
        parameters = (self.store.host,agent['terminal_id'],agent['repo_id'],maximum_hop)
        rows = connection.execute(base+'ORDER BY m.created_at,m.message_id LIMIT ?',(*parameters,BUNDLE_MESSAGES)).fetchall()
        if wake and not any(row['kind']=='action' for row in rows):
            action = connection.execute(base+"AND m.kind='action' ORDER BY m.created_at,m.message_id LIMIT 1",parameters).fetchone()
            if action:
                rows = sorted([*rows[:BUNDLE_MESSAGES-1],action],key=lambda row:(row['created_at'],row['message_id']))
        return rows

    def inspect(self,*,terminal_id: str,agent_id: str,generation: int) -> dict[str, Any]:
        now = time.time()
        with self.store.read() as connection:
            agent = check_generation(connection,self.store.host,terminal_id,agent_id,generation)
            state = self._state(connection,agent,now)
            rows = self._eligible(connection,agent,state['thresholds']['max_hop'],wake=True)
            actions = [row for row in rows if row['kind']=='action']
        return {**state,'queued_count':len(rows),'has_action':bool(actions),
                'coalesce_until':min((row['created_at'] for row in actions),default=0)+state['thresholds']['coalesce_seconds']}

    def claim(self,*,terminal_id: str,agent_id: str,generation: int,wake: bool = False) -> dict[str, Any]:
        now = time.time()
        with self.store.transaction((terminal_id,)) as connection:
            agent = check_generation(connection,self.store.host,terminal_id,agent_id,generation)
            if agent['client']!='omp' or not agent['extension_present']:
                raise LedgerError('extension_absent','Only the registered OMP receiver may admit native context.')
            state = self._state(connection,agent,now)
            rows = self._eligible(connection,agent,state['thresholds']['max_hop'],wake=wake)
            actions = [row for row in rows if row['kind']=='action']
            reason = None
            if wake:
                if state['role'] is None:
                    reason = 'not_participating'
                elif state['turn_state']!='idle':
                    reason = 'not_idle'
                elif state['wake_count']>=state['thresholds']['wakes_per_hour']:
                    reason = 'hourly_limit'
                elif not actions:
                    reason = 'no_action'
                elif min(row['created_at'] for row in actions)+state['thresholds']['coalesce_seconds']>now:
                    reason = 'coalescing'
            if reason or not rows:
                return {'claim_id':None,'reason':reason or 'empty','messages':[]}
            rows = fit_claim_rows(rows,require_action=wake)
            if not rows:
                return {'claim_id':None,'reason':'unrepresentable','messages':[]}
            claim = Mailbox._claim_messages(connection,rows,agent_id,generation,'wake' if wake else 'before_agent_start')
            marker = 'HAM_CLAIM_' + claim['claim_id'] + '_' + uuid.uuid4().hex
            message = format_context(claim,agent,marker)
            digest = hashlib.sha256(message['content'].encode('utf-8')).hexdigest()
            connection.execute('INSERT INTO native_claims '
                    '(claim_id,client,session_ref,marker,content_sha256,created_at) VALUES (?,?,?,?,?,?)',
                    (claim['claim_id'],'omp',agent['session_ref'],marker,digest,now))
        return {**claim,'session_ref':agent['session_ref'],'marker':marker,'content_sha256':digest,'message':message}

    def applied(self,*,claim_id: str,agent_id: str,generation: int,session_ref: str,
                marker: str,content_sha256: str,entry_id: str) -> dict[str, Any]:
        with self.store.read() as connection:
            row = connection.execute('SELECT * FROM native_claims WHERE claim_id=?',(claim_id,)).fetchone()
        if (row is None or row['client']!='omp' or row['session_ref']!=session_ref or row['marker']!=marker or
                row['content_sha256']!=content_sha256 or not isinstance(entry_id,str) or not entry_id):
            raise LedgerError('invalid_evidence','ACK must match the exact session, claim marker and accepted context hash.')
        # Trust boundary: the root extension has inspected a public read-only
        # active-branch custom_message entry. This is host journal acceptance,
        # not proof of later context/provider transforms or model understanding.
        return Mailbox(self.store).started(claim_id=claim_id,agent_id=agent_id,generation=generation,
                evidence='omp:accepted-journal:'+session_ref+'#'+entry_id+';sha256='+content_sha256)

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
            state = self._state(connection,current,now)
            rows = fit_claim_rows(self._eligible(connection,current,state['thresholds']['max_hop'],wake=False),
                                  require_action=False)
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
                    "WHERE n.client='claude' AND d.state='delivering' AND a.active=1 AND a.client='claude' "
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
