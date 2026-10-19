"""Explicit, ownership-checked runtime installation; never invoked by plugin loading."""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
from pathlib import Path
import shlex
import sqlite3
import tempfile
from typing import Any, Mapping

from ledger_store import LedgerError, LedgerStore

OWNER = 'herdr.agent-messenger'
MANIFEST = 'ham-mailbox.install.json'
RUNTIME = 'ham-mailbox.ts'
_UNCHECKED = object()


def destinations(environment: Mapping[str, str] | None = None) -> dict[str, Path]:
    values = os.environ if environment is None else environment
    home = Path(values.get('HOME') or str(Path.home())).expanduser()
    return {
        'omp': home / '.omp' / 'agent' / 'extensions',
        'claude': Path(values.get('CLAUDE_CONFIG_DIR') or home / '.claude').expanduser(),
    }


def summary(environment: Mapping[str, str] | None = None, *,
            clients: tuple[str, ...] = ('omp', 'claude')) -> dict[str, Any]:
    """Inspect each selected default runtime independently, without installing it."""
    directories = destinations(environment)
    results = []
    for client in clients:
        destination = directories.get(client)
        result = {'client': client, 'destination': str(destination.absolute()) if destination else None,
                  'installed': False, 'owner': None, 'error': None}
        try:
            status = manage('status', destination, client=client)
            result.update(status)
        except LedgerError as error:
            result['error'] = {'code': error.code, 'message': error.message}
        except OSError as error:
            result['error'] = {'code': 'runtime_unavailable', 'message': str(error)}
        results.append(result)
    return {'clients': results}


def health_summary(environment: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Combine installation ownership with read-only, local registration counts."""
    result = summary(environment)
    sessions = {client: {'receiver_registered': None, 'receiver_absent': None}
                for client in ('omp', 'claude', 'codex')}
    ledger: dict[str, Any] = {'available': False, 'error': None}
    try:
        store = LedgerStore.open_readonly(environment)
        if store is None:
            raise LedgerError('ledger_not_initialized', 'No existing HAM ledger is available to read.')
        with store.read() as connection:
            rows = connection.execute(
                "SELECT client,extension_present,COUNT(*) AS count FROM agents "
                "WHERE active=1 AND host=? AND client IN ('omp','claude','codex') "
                "GROUP BY client,extension_present", (store.host,)).fetchall()
        sessions = {client: {'receiver_registered': 0, 'receiver_absent': 0}
                    for client in sessions}
        for row in rows:
            field = 'receiver_registered' if row['extension_present'] else 'receiver_absent'
            sessions[row['client']][field] += row['count']
        ledger['available'] = True
    except LedgerError as error:
        ledger['error'] = {'code': error.code, 'message': error.message}
    except (OSError, sqlite3.Error, ValueError) as error:
        ledger['error'] = {'code': 'ledger_unavailable', 'message': str(error)}
    next_steps = []
    if ledger['error']:
        next_steps.append('Ledger: ' + ledger['error']['message'] +
                          ' Run `ledger reconcile` to initialize or upgrade it before checking session registration.')
    for runtime in result['clients']:
        client = runtime['client']
        absent = sessions[client]['receiver_absent']
        prefix = f'{client}: {absent} sessions without receiver; ' if absent else f'{client}: '
        if runtime['error']:
            next_steps.append(prefix + runtime['error']['code'] + ': ' +
                              runtime['error']['message'] + ' Resolve the conflict; never replace foreign or modified files.')
        elif not runtime['installed']:
            next_steps.append(prefix + 'runtime not installed → ask the user, then `runtime install ' +
                              client + '`, then restart only selected sessions and confirm extension_present=1.')
        elif absent:
            next_steps.append(prefix + 'runtime installed, activation pending → restart only selected sessions; '
                              'confirm extension_present=1 before reporting receiving ready.')
    return {**result, 'sessions': sessions, 'ledger': ledger, 'next_steps': next_steps}


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _manifest(directory: Path, client: str = 'omp') -> dict[str, Any] | None:
    path = directory / MANIFEST
    if path.is_symlink():
        raise LedgerError('foreign_runtime','Runtime ownership manifest must not be a symlink.')
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
        runtime = RUNTIME if client=='omp' else 'hooks/ham-mailbox.py'
        if (value['owner'] != OWNER or value['client'] != client or value['version'] != 1 or
                set(value['files']) != {runtime} or not isinstance(value['files'][runtime],str)):
            raise ValueError('different ownership contract')
        if client=='claude' and (not isinstance(value.get('entries'),dict) or
                set(value['entries'])!={'SessionStart','UserPromptSubmit'} or
                not isinstance(value.get('created_hooks'),bool) or
                not isinstance(value.get('created_events'),list) or
                any(event not in value['entries'] for event in value['created_events']) or
                len(set(value['created_events']))!=len(value['created_events'])):
            raise ValueError('different hook ownership contract')
        return value
    except (OSError,ValueError,KeyError,TypeError) as error:
        raise LedgerError('foreign_runtime','Unrecognized runtime ownership manifest: '+str(error)) from None


def _check_owned(directory: Path, manifest: dict[str, Any] | None) -> None:
    target = directory / RUNTIME
    if target.is_symlink():
        raise LedgerError('foreign_runtime','Refusing a symlink at the HAM runtime target.')
    if manifest is None:
        if target.exists():
            raise LedgerError('foreign_runtime','An unowned ham-mailbox.ts already exists; it was not replaced.')
    elif not target.is_file() or _digest(target) != manifest['files'][RUNTIME]:
        raise LedgerError('modified_runtime','The owned runtime is missing or modified; it was not changed.')


def _atomic(path: Path, content: bytes, *, expected: Any = _UNCHECKED) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix='.ham-install-',dir=path.parent)
    try:
        with os.fdopen(descriptor,'wb') as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        if expected is not _UNCHECKED:
            if path.is_symlink():
                raise LedgerError('settings_changed','Claude settings changed during installation; no shared replacement was made.')
            current = path.read_bytes() if path.exists() else None
            if current!=expected:
                raise LedgerError('settings_changed','Claude settings changed during installation; retry with the current settings.')
        os.replace(temporary,path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def manage(operation: str, destination: str | Path | None = None, *, client: str = 'omp') -> dict[str, Any]:
    if client not in {'omp','claude'}:
        raise LedgerError('unsupported_client','Runtime installation supports omp or claude.')
    if client=='claude':
        return _manage_claude(operation,destination)
    directory = Path(destination).expanduser() if destination is not None else Path.home()/'.omp'/'agent'/'extensions'
    directory = directory.absolute()
    if operation not in {'install','uninstall','status'}:
        raise LedgerError('invalid_operation','Unsupported runtime operation.')
    if not directory.exists() and operation != 'install':
        return {'client':'omp','destination':str(directory),'installed':False}
    if operation == 'install':
        directory.mkdir(parents=True,exist_ok=True)
    # Lock the directory itself; no extra installer-owned lock file is left in a
    # user's extension directory, and concurrent first installs cannot race.
    descriptor = os.open(directory,os.O_RDONLY)
    try:
        fcntl.flock(descriptor,fcntl.LOCK_EX)
        manifest = _manifest(directory)
        _check_owned(directory,manifest)
        if operation == 'status':
            return {'client':'omp','destination':str(directory),'installed':manifest is not None,'owner':OWNER if manifest else None}
        if operation == 'uninstall':
            if manifest:
                (directory/RUNTIME).unlink()
                (directory/MANIFEST).unlink()
            return {'client':'omp','destination':str(directory),'installed':False,'removed':manifest is not None}
        source = Path(__file__).resolve().parent/'integrations'/'omp'/RUNTIME
        content = source.read_bytes()
        digest = hashlib.sha256(content).hexdigest()
        if manifest is None or manifest['files'][RUNTIME] != digest:
            _atomic(directory/RUNTIME,content)
        _atomic(directory/MANIFEST,(json.dumps({'version':1,'owner':OWNER,'client':'omp','files':{RUNTIME:digest}},sort_keys=True)+'\n').encode('utf-8'))
        return {'client':'omp','destination':str(directory),'installed':True,'owner':OWNER,'sha256':digest,
                'activation':'Restart or reload only the OMP sessions you explicitly choose. No live runtime was changed.'}
    finally:
        fcntl.flock(descriptor,fcntl.LOCK_UN)
        os.close(descriptor)

def _claude_entries(directory: Path) -> dict[str, Any]:
    command = 'python3 ' + shlex.quote(str(directory/'hooks'/'ham-mailbox.py'))
    return {event:{'hooks':[{'type':'command','command':command,'timeout':20}]}
            for event in ('SessionStart','UserPromptSubmit')}


def _settings(path: Path) -> tuple[dict[str, Any], bytes | None]:
    if path.is_symlink():
        raise LedgerError('foreign_runtime','Claude settings must not be a symlink.')
    if not path.exists():
        return {},None
    try:
        raw = path.read_bytes()
        value = json.loads(raw)
        if not isinstance(value,dict) or not isinstance(value.get('hooks',{}),dict):
            raise ValueError('settings and hooks must be objects')
        for entries in value.get('hooks',{}).values():
            if not isinstance(entries,list):
                raise ValueError('hook event entries must be arrays')
        return value,raw
    except (OSError,ValueError,TypeError) as error:
        raise LedgerError('foreign_runtime','Cannot safely merge Claude settings: '+str(error)) from None


def _manage_claude(operation: str, destination: str | Path | None) -> dict[str, Any]:
    if operation not in {'install','uninstall','status'}:
        raise LedgerError('invalid_operation','Unsupported runtime operation.')
    directory = (Path(destination).expanduser() if destination is not None else
                 Path(os.environ.get('CLAUDE_CONFIG_DIR',str(Path.home()/'.claude')))).absolute()
    if directory.is_symlink() or directory.resolve()!=directory:
        raise LedgerError('foreign_runtime','Claude runtime destination must not traverse symlinks.')
    result = {'client':'claude','destination':str(directory)}
    if not directory.exists() and operation!='install':
        return {**result,'installed':False}
    if operation == 'install':
        directory.mkdir(parents=True,exist_ok=True)
    descriptor = os.open(directory,os.O_RDONLY)
    try:
        fcntl.flock(descriptor,fcntl.LOCK_EX)
        manifest = _manifest(directory,'claude')
        hooks_directory = directory/'hooks'
        target = hooks_directory/'ham-mailbox.py'
        if hooks_directory.is_symlink() or target.is_symlink():
            raise LedgerError('foreign_runtime','Claude hook targets must not be symlinks.')
        if hooks_directory.exists() and not hooks_directory.is_dir():
            raise LedgerError('foreign_runtime','Claude hooks path is not a directory.')
        if manifest is None and target.exists():
            raise LedgerError('foreign_runtime','An unowned Claude HAM hook already exists.')
        if manifest is not None and (not target.is_file() or _digest(target)!=manifest['files']['hooks/ham-mailbox.py']):
            raise LedgerError('modified_runtime','The owned Claude hook is missing or modified.')
        settings_path = directory/'settings.json'
        settings,baseline = _settings(settings_path)
        hooks = settings.get('hooks',{})
        owned = manifest['entries'] if manifest else _claude_entries(directory)
        created_hooks = manifest['created_hooks'] if manifest else 'hooks' not in settings
        created_events = manifest['created_events'] if manifest else [event for event in owned if event not in hooks]
        if manifest:
            # Do not trust a hand-edited ownership record to remove arbitrary hooks.
            if owned!=_claude_entries(directory):
                raise LedgerError('foreign_runtime','Unrecognized owned Claude hook entries.')
            for event,entry in owned.items():
                if hooks.get(event,[]).count(entry)!=1:
                    raise LedgerError('modified_runtime','An owned Claude hook entry is missing, duplicated or modified.')
        for event,entries in hooks.items():
            for entry in entries:
                if manifest and event in owned and entry==owned[event]:
                    continue
                commands = entry.get('hooks',[]) if isinstance(entry,dict) else []
                if isinstance(commands,list) and any(isinstance(hook,dict) and
                        'ham-mailbox.py' in str(hook.get('command','')) for hook in commands):
                    raise LedgerError('foreign_runtime','A foreign or modified HAM hook entry already exists.')
        if operation=='status':
            return {**result,'installed':manifest is not None,'owner':OWNER if manifest else None}
        if operation=='uninstall':
            if manifest:
                for event,entry in owned.items():
                    hooks[event].remove(entry)
                    if not hooks[event] and event in created_events:
                        del hooks[event]
                if not hooks and created_hooks:
                    settings.pop('hooks',None)
                _atomic(settings_path,(json.dumps(settings,indent=2,ensure_ascii=False)+'\n').encode('utf-8'),expected=baseline)
                target.unlink()
                (directory/MANIFEST).unlink()
            return {**result,'installed':False,'removed':manifest is not None}
        content = (Path(__file__).resolve().parent/'integrations'/'claude'/'ham-mailbox.py').read_bytes()
        digest = hashlib.sha256(content).hexdigest()
        if manifest is None:
            settings['hooks'] = hooks
            for event,entry in owned.items():
                hooks.setdefault(event,[]).append(entry)
            _atomic(settings_path,(json.dumps(settings,indent=2,ensure_ascii=False)+'\n').encode('utf-8'),expected=baseline)
        hooks_directory.mkdir(exist_ok=True)
        if manifest is None or manifest['files']['hooks/ham-mailbox.py']!=digest:
            _atomic(target,content)
        _atomic(directory/MANIFEST,(json.dumps({'version':1,'owner':OWNER,'client':'claude',
                'files':{'hooks/ham-mailbox.py':digest},'entries':owned,'created_hooks':created_hooks,
                'created_events':created_events},sort_keys=True)+'\n').encode('utf-8'))
        return {**result,'installed':True,'owner':OWNER,'sha256':digest,
                'activation':'Only the selected Claude config was changed; restart selected sessions to load hooks.'}
    finally:
        fcntl.flock(descriptor,fcntl.LOCK_UN)
        os.close(descriptor)
