"""Global Claude hooks activate from Herdr hints, never use them as identity."""
import ham_test_support  # Protect the live ledger before product imports.
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


HOOK_PATH = Path(__file__).resolve().parent / 'integrations' / 'claude' / 'ham-mailbox.py'
SPEC = importlib.util.spec_from_file_location('ham_claude_activation_hook', HOOK_PATH)
HOOK = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(HOOK)


class ClaudeHookActivationTest(unittest.TestCase):
    def payload(self, event='UserPromptSubmit'):
        return {
            'hook_event_name': event,
            'session_id': '18d5d32c-f87c-4af3-85dc-d36cb21f94ac',
            'transcript_path': '/private/claude/18d5d32c-f87c-4af3-85dc-d36cb21f94ac.jsonl',
            'prompt': 'Private user input must not reach the runtime service.',
        }

    def assert_inactive(self, environment):
        stdin = mock.Mock()
        stdin.buffer.read.side_effect = AssertionError('Inactive hook read stdin')
        stdout, stderr = io.StringIO(), io.StringIO()
        with (
            mock.patch.dict(os.environ, environment, clear=True),
            mock.patch.object(HOOK.sys, 'stdin', stdin),
            mock.patch.object(HOOK.sys, 'stdout', stdout),
            mock.patch.object(HOOK.sys, 'stderr', stderr),
            mock.patch.object(HOOK, 'find_root') as root,
            mock.patch.object(HOOK, 'run_json') as service,
            mock.patch.object(HOOK.subprocess, 'Popen') as child,
            mock.patch.object(HOOK.os, 'write') as write,
        ):
            self.assertEqual(HOOK.main(), 0)
        stdin.buffer.read.assert_not_called()
        root.assert_not_called()
        service.assert_not_called()
        child.assert_not_called()
        write.assert_not_called()
        self.assertEqual((stdout.getvalue(), stderr.getvalue()), ('', ''))

    def test_outside_herdr_returns_before_stdin_lookup_or_subprocess(self):
        self.assert_inactive({
            'HERDR_BIN_PATH': '/configured/herdr',
            'HERDR_AGENT_MESSENGER_ROOT': '/configured/plugin',
            'HERDR_AGENT_LEDGER_DIR': '/configured/ledger',
            'HERDR_AGENT_REQUEST_STATE_DIR': '/configured/requests',
            'HERDR_AGENT_LEDGER_AUTOSTART': '1',
            'HERDR_ACTIVE_PANE_ID': 'unrelated-active-pane',
        })

    def test_socket_transport_configuration_does_not_activate(self):
        self.assert_inactive({'HERDR_SOCKET_PATH': '/configured/herdr.sock'})

    def test_empty_herdr_signals_do_not_activate(self):
        self.assert_inactive(dict.fromkeys(
            ('HERDR_ENV', 'HERDR_PANE_ID', 'HERDR_TERMINAL_ID'), ''))

    def test_outside_herdr_subprocess_exits_without_waiting_for_stdin(self):
        environment = {key: value for key, value in os.environ.items() if not key.startswith('HERDR_')}
        child = subprocess.Popen(
            [sys.executable, '-B', str(HOOK_PATH)], env=environment,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        try:
            # Keep the parent's stdin pipe open: even an empty read would block.
            self.assertEqual(child.wait(timeout=5), 0)
            self.assertEqual((child.stdout.read(), child.stderr.read()), (b'', b''))
        finally:
            if child.poll() is None:
                child.kill()
                child.wait()
            for stream in (child.stdin, child.stdout, child.stderr):
                stream.close()

    def test_outside_herdr_never_invokes_configured_plugin_lookup_or_cli(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sentinel = root / 'child-invoked'
            fake_herdr = root / 'herdr'
            fake_herdr.write_text(
                '#!' + sys.executable + '\n'
                'from pathlib import Path\n'
                f'Path({str(sentinel)!r}).write_text("plugin lookup")\n'
                'print(' + repr(json.dumps({'result': {'plugins': [{'plugin_root': str(root)}]}})) + ')\n',
                encoding='utf-8',
            )
            fake_herdr.chmod(0o700)
            (root / 'ledger_cli.py').write_text(
                'from pathlib import Path\n'
                f'Path({str(sentinel)!r}).write_text("ledger CLI")\n'
                'print(\'{"claim": null}\')\n',
                encoding='utf-8',
            )
            environment = {key: value for key, value in os.environ.items() if not key.startswith('HERDR_')}
            environment.update(
                HERDR_BIN_PATH=str(fake_herdr),
                HERDR_SOCKET_PATH=str(root / 'configured-herdr.sock'),
                HERDR_AGENT_LEDGER_DIR=str(root / 'ledger'),
                HERDR_AGENT_REQUEST_STATE_DIR=str(root / 'requests'),
                HERDR_AGENT_LEDGER_AUTOSTART='1',
            )
            before = set(root.iterdir())
            for explicit_root in (False, True):
                with self.subTest(explicit_root=explicit_root):
                    values = dict(environment)
                    if explicit_root:
                        values['HERDR_AGENT_MESSENGER_ROOT'] = str(root)
                    result = subprocess.run(
                        [sys.executable, '-B', str(HOOK_PATH)],
                        input=json.dumps(self.payload()).encode('utf-8'),
                        capture_output=True, env=values, timeout=5,
                    )
                    self.assertEqual((result.returncode, result.stdout, result.stderr), (0, b'', b''))
                    self.assertFalse(sentinel.exists())
                    self.assertEqual(set(root.iterdir()), before)

    def test_each_herdr_signal_passes_both_events_to_existing_runtime_service(self):
        signals = (
            {'HERDR_ENV': '1'},
            {'HERDR_PANE_ID': 'old-workspace:old-pane'},
            {'HERDR_TERMINAL_ID': 'inherited-terminal'},
        )
        for environment in signals:
            for event in ('SessionStart', 'UserPromptSubmit'):
                with self.subTest(environment=environment, event=event):
                    payload = self.payload(event)
                    stdin = mock.Mock(buffer=io.BytesIO(json.dumps(payload).encode('utf-8')))
                    root = Path('/resolved/installed/plugin')
                    with (
                        mock.patch.dict(os.environ, environment, clear=True),
                        mock.patch.object(HOOK.sys, 'stdin', stdin),
                        mock.patch.object(HOOK, 'find_root', return_value=root) as lookup,
                        mock.patch.object(HOOK, 'run_json', return_value={'claim': None}) as service,
                        mock.patch.object(HOOK.os, 'write') as write,
                    ):
                        self.assertEqual(HOOK.main(), 0)
                        self.assertEqual(dict(os.environ), environment)
                    lookup.assert_called_once_with()
                    service.assert_called_once_with(
                        [sys.executable, str(root / 'ledger_cli.py'), 'ledger', 'runtime', 'claude-hook'],
                        {'hook_event_name': event, 'session_id': payload['session_id'],
                         'transcript_path': payload['transcript_path'], 'agent_id': None},
                        timeout=10,
                    )
                    write.assert_not_called()

    def test_herdr_activation_preserves_existing_event_and_subagent_filters(self):
        for options in ({'hook_event_name': 'Stop'}, {'agent_id': 'nested-agent'}):
            with self.subTest(options=options):
                payload = {**self.payload(), **options}
                stdin = mock.Mock(buffer=io.BytesIO(json.dumps(payload).encode('utf-8')))
                with (
                    mock.patch.dict(os.environ, {'HERDR_ENV': '1'}, clear=True),
                    mock.patch.object(HOOK.sys, 'stdin', stdin),
                    mock.patch.object(HOOK, 'find_root') as lookup,
                    mock.patch.object(HOOK, 'run_json') as service,
                ):
                    self.assertEqual(HOOK.main(), 0)
                lookup.assert_not_called()
                service.assert_not_called()


if __name__ == '__main__':
    unittest.main()
