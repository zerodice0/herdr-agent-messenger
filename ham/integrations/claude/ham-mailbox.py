#!/usr/bin/env python3
"""HAM's opt-in root Claude hook. Stdout is exclusively Claude hook JSON."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any, Callable


def run_json(command: list[str], payload: dict[str, Any] | None = None, *, timeout: float = 10,
             cwd: Path | None = None) -> dict[str, Any]:
    child = subprocess.Popen(command, cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    try:
        stdout, _ = child.communicate(None if payload is None else json.dumps(payload).encode('utf-8'), timeout=timeout)
    except BaseException:
        child.kill()
        child.wait()
        raise
    finally:
        for stream in (child.stdin,child.stdout):
            if stream is not None:
                stream.close()
    if child.returncode or len(stdout)>1024*1024:
        raise ValueError('HAM subprocess failed')
    value = json.loads(stdout)
    if not isinstance(value,dict) or 'error' in value:
        raise ValueError('HAM subprocess returned no valid result')
    return value


def find_root() -> Path:
    explicit = os.environ.get('HERDR_AGENT_MESSENGER_ROOT')
    if explicit:
        root = Path(explicit)
        if (not root.is_absolute() or not (root/'ham'/'__init__.py').is_file()
                or not (root/'herdr-plugin.toml').is_file()):
            raise ValueError('Invalid explicit HAM root')
        return root
    value = run_json([os.environ.get('HERDR_BIN_PATH','herdr'),'plugin','list','--json',
                      '--plugin','herdr.agent-messenger'],timeout=3)
    result = value.get('result')
    plugins = result.get('plugins') if isinstance(result,dict) else None
    if (not isinstance(plugins,list) or len(plugins)!=1 or not isinstance(plugins[0],dict) or
            not isinstance(plugins[0].get('plugin_root'),str)):
        raise ValueError('HAM plugin root unavailable')
    root = Path(plugins[0]['plugin_root'])
    if (not root.is_absolute() or not (root/'ham'/'__init__.py').is_file()
            or not (root/'herdr-plugin.toml').is_file()):
        raise ValueError('Invalid installed HAM root')
    return root


def emit(result: dict[str, Any], event: str, call: Callable[..., dict[str, Any]],
         writer: Callable[[int, bytes], int] = os.write) -> None:
    claim = result.get('claim')
    if not isinstance(claim,dict):
        return
    written = 0
    try:
        context = result['additional_context']
        if not isinstance(context,str):
            raise ValueError('Invalid context')
        # Finish serialization before the first unbuffered byte can escape.
        content = (json.dumps({'hookSpecificOutput':{'hookEventName':event,
                    'additionalContext':context}},ensure_ascii=False)+'\n').encode('utf-8')
        while written<len(content):
            count = writer(1,content[written:])
            if count<=0:
                raise OSError('Hook output closed')
            written += count
    except (OSError,ValueError,TypeError,KeyError):
        if written==0:
            try:
                identity = {key:claim[key] for key in ('claim_id','agent_id','generation')}
                call('release',{**identity,'disposition':'not_applied',
                               'evidence':'claude:hook-output:known-zero-bytes'},timeout=3)
            except (OSError,ValueError,KeyError,subprocess.SubprocessError):
                pass
        # Partial output, lost child receipts or discarded successful output is
        # uncertain. Only actual transcript request linkage can confirm admission.


def main() -> int:
    # Activation only: ancestry and native session binding remain the CLI's job.
    # Inherited pane IDs can be stale after a move, so do not validate them here.
    if not any(os.environ.get(key) for key in
               ('HERDR_ENV','HERDR_PANE_ID','HERDR_TERMINAL_ID')):
        return 0
    try:
        raw = sys.stdin.buffer.read(1024*1024+1)
        if len(raw)>1024*1024:
            return 0
        value = json.loads(raw)
        if not isinstance(value,dict):
            return 0
        event = value.get('hook_event_name')
        if event not in ('SessionStart','UserPromptSubmit') or value.get('agent_id'):
            return 0
        if not all(isinstance(value.get(key),str) and value[key] for key in ('session_id','transcript_path')):
            return 0
        root = find_root()
        def call(operation: str, payload: dict[str, Any], *, timeout: float = 10) -> dict[str, Any]:
            group = 'delivery' if operation=='release' else 'runtime'
            return run_json([sys.executable,'-m','ham','cli','ledger',group,operation],
                            payload,timeout=timeout,cwd=root)
        result = call('claude-hook',{'hook_event_name':event,'session_id':value['session_id'],
                      'transcript_path':value['transcript_path'],'agent_id':value.get('agent_id')})
        emit(result,event,call)
    except (OSError,ValueError,TypeError,KeyError,RecursionError,subprocess.SubprocessError):
        # Never block a user turn, continue a Stop hook, print prompts or guess ACK.
        pass
    return 0


if __name__=='__main__':
    raise SystemExit(main())
