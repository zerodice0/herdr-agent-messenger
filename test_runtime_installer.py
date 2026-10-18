import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from ledger_store import LedgerError
from runtime_installer import MANIFEST, RUNTIME, manage


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
        target.write_bytes((Path(__file__).parent/'integrations'/'omp'/RUNTIME).read_bytes())
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

    def test_actual_public_cli_does_not_create_a_ledger_or_require_herdr(self):
        environment = {**os.environ,'HERDR_AGENT_LEDGER_DIR':str(self.root/'must-not-exist'),'HERDR_BIN_PATH':str(self.root/'absent-herdr')}
        script = str(Path(__file__).parent/'agent_skill_cli.py')
        for operation in ('install','status','uninstall'):
            completed = subprocess.run([sys.executable,script,'runtime',operation,'omp','--destination',str(self.destination)],text=True,capture_output=True,env=environment,check=True)
            self.assertEqual(json.loads(completed.stdout)['installed'],operation!='uninstall')
        self.assertFalse((self.root/'must-not-exist').exists())


if __name__=='__main__':
    unittest.main()
