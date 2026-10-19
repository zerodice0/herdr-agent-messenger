import ham_test_support  # Protect the live ledger before product imports.
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


class OmpMailboxLifecycleTest(unittest.TestCase):
    @unittest.skipUnless(shutil.which('bun'),'Bun is required for the opt-in OMP extension lifecycle tests')
    def test_public_lifecycle_with_real_private_ledger(self):
        with tempfile.TemporaryDirectory() as directory:
            environment = {**os.environ,'TMPDIR':directory,'HERDR_AGENT_LEDGER_AUTOSTART':'0','PYTHONDONTWRITEBYTECODE':'1'}
            completed = subprocess.run(['bun','test','./omp_mailbox_runtime.test.ts','--timeout','15000'],
                    cwd=Path(__file__).parent,env=environment,text=True,capture_output=True,timeout=120)
            self.assertEqual(completed.returncode,0,completed.stdout+completed.stderr)


if __name__=='__main__':
    unittest.main()
