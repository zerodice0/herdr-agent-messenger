import tests.ham_test_support as ham_test_support  # Protect the live ledger before product imports.
import copy
import hashlib
import json
import os
from pathlib import Path
import shlex
import tempfile
import unittest
from unittest import mock

from ham.ledger.store import LedgerError
from ham.ops.runtime_installer import MANIFEST, OWNER, manage
from ham.ops import runtime_installer as runtime_installer


class ClaudeRuntimeInstallerTest(unittest.TestCase):
    def setUp(self):
        # The runner supplies disk-backed work storage, never the system /tmp.
        self.temp = tempfile.TemporaryDirectory(dir=os.environ['TMPDIR'])
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.destination = self.root / 'Claude config with spaces'
        self.source = Path(__file__).resolve().parents[1] / 'ham' / 'integrations' / 'claude' / 'ham-mailbox.py'
        self.environment = {
            'PATH': os.environ.get('PATH', ''),
            'HOME': str(self.root / 'home'),
            'XDG_CONFIG_HOME': str(self.root / 'config'),
            'XDG_CACHE_HOME': str(self.root / 'cache'),
            'XDG_DATA_HOME': str(self.root / 'data'),
            'XDG_STATE_HOME': str(self.root / 'state'),
            'XDG_RUNTIME_DIR': str(self.root / 'run'),
            'TMPDIR': str(self.root / 'tmp'),
            'CLAUDE_CONFIG_DIR': str(self.destination),
            'HERDR_SOCKET_PATH': str(self.root / 'private-herdr.sock'),
            'HERDR_BIN_PATH': str(self.root / 'absent-herdr'),
            'HERDR_AGENT_LEDGER_DIR': str(self.root / 'ledger'),
            'HERDR_AGENT_REQUEST_STATE_DIR': str(self.root / 'requests'),
            'HERDR_PLUGIN_STATE_DIR': str(self.root / 'plugin-state'),
            'HERDR_AGENT_LEDGER_AUTOSTART': '0',
            'PYTHONDONTWRITEBYTECODE': '1',
        }
        for name in ('HOME', 'XDG_CONFIG_HOME', 'XDG_CACHE_HOME', 'XDG_DATA_HOME',
                     'XDG_STATE_HOME', 'XDG_RUNTIME_DIR', 'TMPDIR'):
            Path(self.environment[name]).mkdir()
        environment_patch = mock.patch.dict(os.environ, self.environment, clear=True)
        environment_patch.start()
        self.addCleanup(environment_patch.stop)

    def call(self, operation, destination=None):
        return manage(operation, self.destination if destination is None else destination, client='claude')

    def write_settings(self, settings, destination=None):
        directory = self.destination if destination is None else destination
        directory.mkdir(parents=True, exist_ok=True)
        (directory / 'settings.json').write_text(
            json.dumps(settings, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')

    def read_settings(self, destination=None):
        directory = self.destination if destination is None else destination
        return json.loads((directory / 'settings.json').read_text(encoding='utf-8'))

    def owned_entry(self, destination=None):
        directory = self.destination if destination is None else destination
        command = 'python3 ' + shlex.quote(str(directory / 'hooks' / 'ham-mailbox.py'))
        return {'hooks': [{'type': 'command', 'command': command, 'timeout': 20}]}

    def snapshot(self):
        result = {}
        for path in self.root.rglob('*'):
            relative = str(path.relative_to(self.root))
            if path.is_symlink():
                result[relative] = ('symlink', os.readlink(path))
            elif path.is_dir():
                result[relative] = ('directory',)
            else:
                result[relative] = ('file', path.read_bytes())
        return result

    def assert_refuses_without_mutation(self, code, destination=None):
        before = self.snapshot()
        for operation in ('install', 'status', 'uninstall'):
            with self.subTest(operation=operation):
                with self.assertRaises(LedgerError) as caught:
                    self.call(operation, destination)
                self.assertEqual(caught.exception.code, code)
                self.assertEqual(self.snapshot(), before)

    @staticmethod
    def foreign_settings():
        return {
            'permissions': {'allow': ['Read', 'Bash(git status)'], 'deny': ['Write(secret)']},
            'env': {'KEEP_ME': '사용자 설정'},
            'enabledPlugins': {'foreign@example': True},
            'custom': {'nested': [1, False, None, {'untouched': 'value'}]},
            'hooks': {
                'SessionStart': [{
                    'matcher': 'startup|resume',
                    'hooks': [
                        {'type': 'command', 'command': 'foreign-start --flag', 'timeout': 7},
                        {'type': 'prompt', 'prompt': 'Keep this foreign hook'},
                    ],
                    'customMetadata': {'keep': True},
                }],
                'UserPromptSubmit': [{'hooks': [
                    {'type': 'command', 'command': 'foreign-prompt', 'async': True},
                ]}],
                'Stop': [{'matcher': '*', 'hooks': [
                    {'type': 'command', 'command': 'foreign-stop'},
                ]}],
                'Notification': [],
            },
        }

    def test_install_repeat_status_and_uninstall_preserve_foreign_settings_and_files(self):
        original = self.foreign_settings()
        self.write_settings(original)
        hooks = self.destination / 'hooks'
        hooks.mkdir()
        foreign_file = hooks / 'foreign.py'
        foreign_file.write_bytes(b'foreign hook payload\n')
        other_file = self.destination / 'settings.local.json'
        other_file.write_bytes(b'{"unrelated":true}\n')

        installed = self.call('install')
        self.assertTrue(installed['installed'])
        self.assertEqual(installed['client'], 'claude')
        self.assertEqual(installed['destination'], str(self.destination))
        expected = copy.deepcopy(original)
        for event in ('SessionStart', 'UserPromptSubmit'):
            expected['hooks'][event].append(self.owned_entry())
        self.assertEqual(self.read_settings(), expected)
        before_repeat = self.snapshot()
        repeated = self.call('install')
        self.assertTrue(repeated['installed'])
        self.assertEqual(repeated['sha256'], installed['sha256'])
        self.assertEqual(self.snapshot(), before_repeat)
        status = self.call('status')
        self.assertTrue(status['installed'])
        self.assertEqual(status['owner'], OWNER)
        self.assertEqual(self.snapshot(), before_repeat)

        removed = self.call('uninstall')
        self.assertFalse(removed['installed'])
        self.assertTrue(removed['removed'])
        self.assertEqual(self.read_settings(), original)
        self.assertFalse((hooks / 'ham-mailbox.py').exists())
        self.assertFalse((self.destination / MANIFEST).exists())
        self.assertEqual(foreign_file.read_bytes(), b'foreign hook payload\n')
        self.assertEqual(other_file.read_bytes(), b'{"unrelated":true}\n')
        after_uninstall = self.snapshot()
        self.assertFalse(self.call('status')['installed'])
        self.assertFalse(self.call('uninstall')['removed'])
        self.assertEqual(self.snapshot(), after_uninstall)
        for name in ('ledger', 'requests', 'plugin-state'):
            self.assertFalse((self.root / name).exists())

    def test_foreign_additions_after_install_survive_repeat_and_uninstall(self):
        self.call('install')
        settings = self.read_settings()
        settings['newPreference'] = {'enabled': True}
        earlier = {'matcher': 'resume', 'hooks': [{'type': 'command', 'command': 'later-start'}]}
        later = {'hooks': [{'type': 'command', 'command': 'later-prompt'}]}
        settings['hooks']['SessionStart'].insert(0, earlier)
        settings['hooks']['UserPromptSubmit'].append(later)
        settings['hooks']['PreToolUse'] = [{'matcher': 'Bash', 'hooks': [
            {'type': 'command', 'command': 'later-tool'},
        ]}]
        self.write_settings(settings)
        foreign_file = self.destination / 'hooks' / 'added-after-install.py'
        foreign_file.write_bytes(b'new foreign payload')
        before = self.snapshot()
        self.assertTrue(self.call('status')['installed'])
        self.assertTrue(self.call('install')['installed'])
        self.assertEqual(self.snapshot(), before)
        self.assertTrue(self.call('uninstall')['removed'])
        expected = copy.deepcopy(settings)
        for event in ('SessionStart', 'UserPromptSubmit'):
            expected['hooks'][event].remove(self.owned_entry())
        self.assertEqual(self.read_settings(), expected)
        self.assertEqual(foreign_file.read_bytes(), b'new foreign payload')

    def test_installed_commands_target_actual_adapter_for_both_hook_events(self):
        result = self.call('install')
        target = self.destination / 'hooks' / 'ham-mailbox.py'
        self.assertEqual(target.read_bytes(), self.source.read_bytes())
        digest = hashlib.sha256(target.read_bytes()).hexdigest()
        self.assertEqual(result['sha256'], digest)
        settings = self.read_settings()
        self.assertEqual(set(settings['hooks']), {'SessionStart', 'UserPromptSubmit'})
        for event in ('SessionStart', 'UserPromptSubmit'):
            self.assertEqual(settings['hooks'][event], [self.owned_entry()])
            hook = settings['hooks'][event][0]['hooks'][0]
            self.assertEqual(shlex.split(hook['command']), ['python3', str(target)])
            self.assertEqual(hook['type'], 'command')
            self.assertEqual(hook['timeout'], 20)
        manifest = json.loads((self.destination / MANIFEST).read_text(encoding='utf-8'))
        self.assertEqual(manifest, {
            'version': 1, 'owner': OWNER, 'client': 'claude',
            'files': {'hooks/ham-mailbox.py': digest},
            'entries': {event: self.owned_entry() for event in ('SessionStart', 'UserPromptSubmit')},
            'created_hooks': True, 'created_events': ['SessionStart', 'UserPromptSubmit'],
        })

    def test_edited_owned_entries_refuse_all_operations_without_mutation(self):
        for event in ('SessionStart', 'UserPromptSubmit'):
            for change in ('matcher', 'timeout', 'command', 'extra-hook'):
                with self.subTest(event=event, change=change):
                    destination = self.root / (event + '-' + change)
                    self.call('install', destination)
                    settings = self.read_settings(destination)
                    entry = settings['hooks'][event][0]
                    if change == 'matcher':
                        entry['matcher'] = 'startup'
                    elif change == 'timeout':
                        entry['hooks'][0]['timeout'] = 99
                    elif change == 'command':
                        entry['hooks'][0]['command'] += ' --local-edit'
                    else:
                        entry['hooks'].append({'type': 'command', 'command': 'foreign-in-owned-entry'})
                    self.write_settings(settings, destination)
                    self.assert_refuses_without_mutation('modified_runtime', destination)

    def test_duplicate_or_missing_owned_entries_refuse_all_operations_without_mutation(self):
        for event in ('SessionStart', 'UserPromptSubmit'):
            for change in ('duplicate', 'missing-entry', 'missing-event'):
                with self.subTest(event=event, change=change):
                    destination = self.root / (event + '-' + change)
                    self.call('install', destination)
                    settings = self.read_settings(destination)
                    if change == 'duplicate':
                        settings['hooks'][event].append(copy.deepcopy(settings['hooks'][event][0]))
                    elif change == 'missing-entry':
                        settings['hooks'][event].clear()
                    else:
                        del settings['hooks'][event]
                    self.write_settings(settings, destination)
                    self.assert_refuses_without_mutation('modified_runtime', destination)

    def test_edited_or_missing_owned_adapter_refuses_all_operations_without_mutation(self):
        for change in ('edited', 'missing'):
            with self.subTest(change=change):
                destination = self.root / change
                self.call('install', destination)
                target = destination / 'hooks' / 'ham-mailbox.py'
                if change == 'edited':
                    target.write_bytes(target.read_bytes() + b'\n# local edit\n')
                else:
                    target.unlink()
                self.assert_refuses_without_mutation('modified_runtime', destination)

    def test_edited_ownership_manifest_refuses_all_operations_without_mutation(self):
        changes = ('owner', 'client', 'version', 'extra-file', 'hash', 'entry',
                   'missing-event', 'extra-event', 'invalid-json')
        for change in changes:
            with self.subTest(change=change):
                destination = self.root / change
                self.call('install', destination)
                path = destination / MANIFEST
                manifest = json.loads(path.read_text(encoding='utf-8'))
                if change == 'owner':
                    manifest['owner'] = 'foreign-owner'
                elif change == 'client':
                    manifest['client'] = 'omp'
                elif change == 'version':
                    manifest['version'] = 99
                elif change == 'extra-file':
                    manifest['files']['hooks/foreign.py'] = '0' * 64
                elif change == 'hash':
                    manifest['files']['hooks/ham-mailbox.py'] = '0' * 64
                elif change == 'entry':
                    manifest['entries']['SessionStart']['hooks'][0]['command'] = 'foreign-command'
                elif change == 'missing-event':
                    del manifest['entries']['UserPromptSubmit']
                elif change == 'extra-event':
                    manifest['entries']['Stop'] = self.owned_entry(destination)
                path.write_text('{' if change == 'invalid-json' else json.dumps(manifest), encoding='utf-8')
                code = 'modified_runtime' if change == 'hash' else 'foreign_runtime'
                self.assert_refuses_without_mutation(code, destination)

    def test_missing_manifest_does_not_adopt_previously_owned_files(self):
        self.call('install')
        (self.destination / MANIFEST).unlink()
        self.assert_refuses_without_mutation('foreign_runtime')

    def test_unowned_adapter_is_never_adopted_even_when_identical(self):
        for label, content in (('identical', self.source.read_bytes()), ('different', b'foreign adapter')):
            with self.subTest(content=label):
                destination = self.root / label
                (destination / 'hooks').mkdir(parents=True)
                (destination / 'hooks' / 'ham-mailbox.py').write_bytes(content)
                self.write_settings(self.foreign_settings(), destination)
                self.assert_refuses_without_mutation('foreign_runtime', destination)
                self.assertFalse((destination / MANIFEST).exists())

    def test_unowned_command_collisions_in_any_event_are_never_adopted(self):
        for event in ('SessionStart', 'UserPromptSubmit', 'Stop'):
            for variant in ('exact-target', 'other-target'):
                with self.subTest(event=event, variant=variant):
                    destination = self.root / (event + '-' + variant)
                    command = self.owned_entry(destination)['hooks'][0]['command']
                    if variant == 'other-target':
                        command = 'python3 /foreign/hooks/ham-mailbox.py --foreign'
                    settings = self.foreign_settings()
                    settings['hooks'].setdefault(event, []).append({
                        'matcher': '*',
                        'hooks': [{'type': 'command', 'command': 'unrelated-first'},
                                  {'type': 'command', 'command': command}],
                    })
                    self.write_settings(settings, destination)
                    self.assert_refuses_without_mutation('foreign_runtime', destination)
                    self.assertFalse((destination / MANIFEST).exists())

    def test_foreign_command_added_after_install_refuses_mutation(self):
        self.call('install')
        settings = self.read_settings()
        settings['hooks']['Stop'] = [{'hooks': [
            {'type': 'command', 'command': 'python3 /foreign/ham-mailbox.py'},
        ]}]
        self.write_settings(settings)
        self.assert_refuses_without_mutation('foreign_runtime')

    def test_concurrent_foreign_settings_edit_is_not_overwritten_on_install_or_uninstall(self):
        for operation in ('install','uninstall'):
            with self.subTest(operation=operation):
                destination = self.root/operation
                self.write_settings(self.foreign_settings(),destination)
                if operation=='uninstall':
                    self.call('install',destination)
                target = destination/'hooks'/'ham-mailbox.py'
                manifest = destination/MANIFEST
                before_target = target.read_bytes() if target.exists() else None
                before_manifest = manifest.read_bytes() if manifest.exists() else None
                foreign = self.read_settings(destination)
                foreign['concurrently-added'] = {'permission':'retained'}
                raw = (json.dumps(foreign)+'\n').encode()
                mkstemp = tempfile.mkstemp
                def concurrent_write(*args,**kwargs):
                    (destination/'settings.json').write_bytes(raw)
                    return mkstemp(*args,**kwargs)
                with mock.patch.object(runtime_installer.tempfile,'mkstemp',side_effect=concurrent_write):
                    with self.assertRaises(LedgerError) as caught:
                        self.call(operation,destination)
                self.assertEqual(caught.exception.code,'settings_changed')
                self.assertEqual((destination/'settings.json').read_bytes(),raw)
                self.assertEqual(target.read_bytes() if target.exists() else None,before_target)
                self.assertEqual(manifest.read_bytes() if manifest.exists() else None,before_manifest)
                self.assertEqual(list(destination.glob('.ham-install-*')),[])

    def test_preexisting_empty_hook_containers_are_preserved_exactly(self):
        for index,original in enumerate(({'hooks':{}},{'hooks':{'SessionStart':[]}},
                                        {'hooks':{'SessionStart':[],'UserPromptSubmit':[]}})):
            with self.subTest(original=original):
                destination = self.root/('empty-'+str(index))
                self.write_settings(original,destination)
                self.call('install',destination)
                self.call('install',destination)
                self.call('uninstall',destination)
                self.assertEqual(self.read_settings(destination),original)

    def test_symlink_settings_hooks_adapter_and_manifest_refuse_mutation(self):
        for installed in (False, True):
            for kind in ('settings', 'hooks', 'adapter', 'manifest'):
                with self.subTest(installed=installed, kind=kind):
                    destination = self.root / (str(installed) + '-' + kind)
                    if installed:
                        self.call('install', destination)
                    else:
                        destination.mkdir()
                    relative = {'settings': 'settings.json', 'hooks': 'hooks',
                                'adapter': 'hooks/ham-mailbox.py', 'manifest': MANIFEST}[kind]
                    path = destination / relative
                    external = self.root / ('external-' + str(installed) + '-' + kind)
                    if kind == 'hooks':
                        if installed:
                            path.rename(external)
                        else:
                            external.mkdir()
                            (external / 'foreign.py').write_bytes(b'foreign hook')
                    else:
                        path.parent.mkdir(parents=True, exist_ok=True)
                        external.write_bytes(path.read_bytes() if path.exists() else b'{}\n')
                        if path.exists():
                            path.unlink()
                    path.symlink_to(external, target_is_directory=kind == 'hooks')
                    self.assert_refuses_without_mutation('foreign_runtime', destination)

    def test_dangling_symlinks_are_not_treated_as_absent_targets(self):
        for kind, relative in (('settings', 'settings.json'), ('hooks', 'hooks'),
                               ('adapter', 'hooks/ham-mailbox.py'), ('manifest', MANIFEST)):
            with self.subTest(kind=kind):
                destination = self.root / ('dangling-' + kind)
                path = destination / relative
                path.parent.mkdir(parents=True)
                path.symlink_to(self.root / ('absent-' + kind))
                self.assert_refuses_without_mutation('foreign_runtime', destination)

    def test_destination_or_ancestor_symlinks_cannot_redirect_installer(self):
        real = self.root / 'real-config'
        real.mkdir()
        (real / 'foreign.txt').write_bytes(b'untouched destination')
        link = self.root / 'linked-config'
        link.symlink_to(real, target_is_directory=True)
        self.assert_refuses_without_mutation('foreign_runtime', link)
        self.assert_refuses_without_mutation('foreign_runtime', link / 'child-config')
        dangling = self.root / 'dangling-config'
        dangling.symlink_to(self.root / 'missing-config', target_is_directory=True)
        self.assert_refuses_without_mutation('foreign_runtime', dangling)

    def test_absent_status_and_uninstall_do_not_create_config_or_runtime_state(self):
        before = self.snapshot()
        for operation in ('status', 'uninstall'):
            result = self.call(operation)
            self.assertFalse(result['installed'])
            self.assertEqual(result['client'], 'claude')
            self.assertEqual(result['destination'], str(self.destination))
            self.assertEqual(self.snapshot(), before)

    def test_empty_config_uninstall_removes_owned_events_and_hooks_key(self):
        self.call('install')
        self.assertEqual(set(self.read_settings()['hooks']), {'SessionStart', 'UserPromptSubmit'})
        self.assertTrue(self.call('uninstall')['removed'])
        self.assertEqual(self.read_settings(), {})
        self.assertFalse((self.destination / 'hooks' / 'ham-mailbox.py').exists())
        self.assertFalse((self.destination / MANIFEST).exists())

    def test_default_destination_uses_private_claude_config_environment(self):
        result = manage('install', client='claude')
        self.assertEqual(result['destination'], str(self.destination))
        self.assertTrue(result['installed'])
        self.assertEqual(self.read_settings()['hooks']['SessionStart'], [self.owned_entry()])
        self.assertFalse((Path(self.environment['HOME']) / '.claude').exists())
        self.assertFalse((Path(self.environment['HOME']) / '.omp').exists())
        self.assertTrue(manage('uninstall', client='claude')['removed'])


if __name__ == '__main__':
    unittest.main()
