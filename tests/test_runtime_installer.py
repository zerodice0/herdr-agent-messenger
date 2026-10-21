import tests.ham_test_support as ham_test_support  # Protect the live ledger before product imports.
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from ham.ledger.store import LedgerError
from ham.ops.runtime_installer import MANIFEST, OWNER, RUNTIME, manage, summary


class RuntimeInstallerTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.destination = self.root/'extensions'

    def test_install_status_repeat_and_uninstall_preserve_foreign_and_herdr_owned_files(self):
        self.destination.mkdir()
        herdr = self.destination/'herdr-omp-agent-state.ts'
        foreign = self.destination/'my-extension.ts'
        herdr.write_text('Herdr-owned immutable fixture')
        foreign.write_text('foreign extension')
        installed = manage('install',self.destination)
        self.assertTrue(installed['installed'])
        runtime = (self.destination/RUNTIME).read_bytes()
        self.assertEqual(manage('install',self.destination)['sha256'],installed['sha256'])
        self.assertEqual((self.destination/RUNTIME).read_bytes(),runtime)
        self.assertTrue(manage('status',self.destination)['installed'])
        self.assertTrue(manage('uninstall',self.destination)['removed'])
        self.assertFalse((self.destination/RUNTIME).exists())
        self.assertEqual(herdr.read_text(),'Herdr-owned immutable fixture')
        self.assertEqual(foreign.read_text(),'foreign extension')
        self.assertFalse(manage('uninstall',self.destination)['removed'])

    def test_foreign_identical_or_different_file_is_never_silently_adopted(self):
        self.destination.mkdir()
        target = self.destination/RUNTIME
        target.write_bytes((Path(__file__).parents[1]/'ham'/'integrations'/'omp'/RUNTIME).read_bytes())
        for operation in ('install','uninstall','status'):
            with self.assertRaises(LedgerError) as caught:
                manage(operation,self.destination)
            self.assertEqual(caught.exception.code,'foreign_runtime')
        self.assertFalse((self.destination/MANIFEST).exists())

    def test_modified_owned_file_and_foreign_manifest_are_not_overwritten_or_removed(self):
        manage('install',self.destination)
        target = self.destination/RUNTIME
        target.write_text('local change')
        for operation in ('install','uninstall'):
            with self.assertRaises(LedgerError) as caught:
                manage(operation,self.destination)
            self.assertEqual(caught.exception.code,'modified_runtime')
        self.assertEqual(target.read_text(),'local change')
        (self.destination/MANIFEST).write_text(json.dumps({'owner':'herdr','client':'omp','version':1,'files':{RUNTIME:'anything'}}))
        with self.assertRaises(LedgerError) as caught:
            manage('uninstall',self.destination)
        self.assertEqual(caught.exception.code,'foreign_runtime')
        self.assertEqual(target.read_text(),'local change')

    def test_symlinks_cannot_redirect_runtime_or_manifest_writes(self):
        self.destination.mkdir()
        other = self.root/'other'
        other.write_text('unchanged')
        (self.destination/RUNTIME).symlink_to(other)
        with self.assertRaises(LedgerError):
            manage('install',self.destination)
        (self.destination/RUNTIME).unlink()
        (self.destination/MANIFEST).symlink_to(other)
        with self.assertRaises(LedgerError):
            manage('install',self.destination)
        self.assertEqual(other.read_text(),'unchanged')

    def test_summary_normalizes_missing_clients_without_creating_their_home(self):
        home = self.root/'absent-home'
        environment = {'HOME':str(home)}
        with mock.patch('ham.ops.runtime_installer.manage', wraps=manage) as status:
            result = summary(environment)
        destinations = [home/'.omp'/'agent'/'extensions', home/'.claude']
        self.assertEqual(result, {'clients':[
            {'client':client,'destination':str(destination),'installed':False,'owner':None,'error':None}
            for client,destination in zip(('omp','claude'),destinations)
        ]})
        self.assertEqual(status.call_args_list, [
            mock.call('status',destination,client=client)
            for client,destination in zip(('omp','claude'),destinations)
        ])
        self.assertFalse(home.exists())

    def test_summary_respects_environment_home_claude_override_and_selected_client_order(self):
        home = self.root/'selected-home'
        claude = self.root/'selected-claude'
        destinations = {'omp':home/'.omp'/'agent'/'extensions','claude':claude}
        environment = {'HOME':str(home),'CLAUDE_CONFIG_DIR':str(claude)}
        for client,destination in destinations.items():
            manage('install',destination,client=client)
        before = {path:path.read_bytes() for path in self.root.rglob('*') if path.is_file()}
        with mock.patch.dict(os.environ, {'HOME':str(self.root/'wrong-home'),
                                         'CLAUDE_CONFIG_DIR':str(self.root/'wrong-claude')}):
            result = summary(environment,clients=('claude','omp'))
        self.assertEqual(result, {'clients':[
            {**manage('status',destinations[client],client=client),'error':None}
            for client in ('claude','omp')
        ]})
        for row in result['clients']:
            self.assertEqual(set(row), {'client','destination','installed','owner','error'})
            self.assertIs(row['installed'],True)
            self.assertEqual(row['owner'],OWNER)
        self.assertEqual({path:path.read_bytes() for path in self.root.rglob('*') if path.is_file()},before)
        self.assertFalse((home/'.claude').exists())
        self.assertFalse((self.root/'wrong-home').exists())
        self.assertFalse((self.root/'wrong-claude').exists())
        self.assertEqual(summary(environment,clients=('claude',))['clients'],result['clients'][:1])

    def test_summary_keeps_reporting_other_runtime_after_ownership_or_modification_errors(self):
        for broken_client in ('omp','claude'):
            for kind in ('foreign','modified'):
                with self.subTest(client=broken_client,kind=kind):
                    home = self.root/(broken_client+'-'+kind)
                    environment = {'HOME':str(home)}
                    destinations = {'omp':home/'.omp'/'agent'/'extensions','claude':home/'.claude'}
                    for client,destination in destinations.items():
                        manage('install',destination,client=client)
                    destination = destinations[broken_client]
                    if kind=='foreign':
                        (destination/MANIFEST).unlink()
                    else:
                        runtime = RUNTIME if broken_client=='omp' else 'hooks/ham-mailbox.py'
                        (destination/runtime).write_text('User modification; do not replace.')
                    with self.assertRaises(LedgerError) as expected:
                        manage('status',destination,client=broken_client)
                    before = {path:path.read_bytes() for path in home.rglob('*') if path.is_file()}
                    rows = {row['client']:row for row in summary(environment)['clients']}
                    broken = rows[broken_client]
                    self.assertEqual(broken, {'client':broken_client,'destination':str(destination),
                                             'installed':False,'owner':None,
                                             'error':{'code':expected.exception.code,
                                                      'message':expected.exception.message}})
                    other = 'claude' if broken_client=='omp' else 'omp'
                    self.assertEqual(rows[other], {
                        **manage('status',destinations[other],client=other),'error':None})
                    self.assertEqual({path:path.read_bytes() for path in home.rglob('*') if path.is_file()},before)

    def test_summary_isolates_filesystem_error_and_still_validates_the_other_runtime(self):
        home = self.root/'home'
        claude = home/'.claude'
        manage('install',claude,client='claude')

        def status(operation,destination,*,client):
            if client=='omp':
                raise PermissionError('Private fixture permission denied')
            return manage(operation,destination,client=client)

        with mock.patch('ham.ops.runtime_installer.manage',side_effect=status) as validator:
            result = summary({'HOME':str(home)})
        self.assertEqual(validator.call_count,2)
        self.assertEqual(result['clients'][0], {
            'client':'omp','destination':str(home/'.omp'/'agent'/'extensions'),
            'installed':False,'owner':None,
            'error':{'code':'runtime_unavailable','message':'Private fixture permission denied'}})
        self.assertEqual(result['clients'][1], {
            **manage('status',claude,client='claude'),'error':None})
        self.assertFalse((home/'.omp').exists())

    def test_actual_public_cli_does_not_create_a_ledger_or_require_herdr(self):
        environment = {**os.environ,'HERDR_AGENT_LEDGER_DIR':str(self.root/'must-not-exist'),'HERDR_BIN_PATH':str(self.root/'absent-herdr')}
        root = Path(__file__).resolve().parents[1]
        for operation in ('install','status','uninstall'):
            completed = subprocess.run([sys.executable,'-m','ham','cli','runtime',operation,'omp','--destination',str(self.destination)],cwd=root,text=True,capture_output=True,env=environment,check=True)
            self.assertEqual(json.loads(completed.stdout)['installed'],operation!='uninstall')
        self.assertFalse((self.root/'must-not-exist').exists())


if __name__=='__main__':
    unittest.main()
