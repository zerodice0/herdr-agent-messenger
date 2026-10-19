"""The test tripwire rejects live defaults without changing production storage."""
import ham_test_support

import ast
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from ledger_store import LedgerStore


class LedgerSafetyGuardTest(unittest.TestCase):
    def test_every_test_module_installs_guard_before_product_imports(self):
        for path in Path(__file__).parent.glob("test_*.py"):
            with self.subTest(module=path.name):
                nodes = ast.parse(path.read_text()).body
                imports = [node for node in nodes if isinstance(node, (ast.Import, ast.ImportFrom))]
                first = next(node for node in imports
                             if not isinstance(node, ast.ImportFrom) or node.module != "__future__")
                self.assertIsInstance(first, ast.Import)
                self.assertEqual(first.names[0].name, "ham_test_support")

    def test_protected_roots_include_home_xdg_override_and_account_home(self):
        roots = ham_test_support.protected_ledger_directories({
            "HOME": "/private-fixture/home", "XDG_STATE_HOME": "/private-fixture/state",
            "HERDR_AGENT_LEDGER_DIR": "/private-fixture/override",
        }, Path("/account-home"))
        self.assertEqual(roots, frozenset(map(Path, (
            "/private-fixture/home/.local/state/herdr-agent-messenger/ledger",
            "/private-fixture/state/herdr-agent-messenger/ledger",
            "/private-fixture/override",
            "/account-home/.local/state/herdr-agent-messenger/ledger",
        ))))

    def test_protected_paths_and_symlink_descendants_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            protected = root / "protected"
            protected.mkdir()
            alias = root / "alias"
            alias.symlink_to(protected, target_is_directory=True)
            with mock.patch.object(ham_test_support, "PROTECTED_LEDGER_DIRECTORIES", frozenset({protected})):
                for path in (protected, protected / "nested", alias / "nested"):
                    with self.subTest(path=path), self.assertRaisesRegex(AssertionError, "protected ledger"):
                        ham_test_support.require_private_ledger(path)
                ham_test_support.require_private_ledger(root / "protected-sibling")

    def test_explicit_private_store_retains_real_schema_behavior(self):
        with tempfile.TemporaryDirectory() as directory:
            store = LedgerStore(Path(directory) / "ledger", canonical_name="guard-test")
            self.assertTrue(store.path.is_file())
            with store.read() as connection:
                self.assertTrue(connection.execute("SELECT count(*) FROM ledger_meta").fetchone()[0])

    def test_default_constructor_fails_before_creating_home_or_xdg_ledger(self):
        for call in ("LedgerStore()", "LedgerStore(environment={})"):
            with self.subTest(call=call), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                environment = {**os.environ, "HOME": str(root / "home"),
                               "XDG_STATE_HOME": str(root / "state")}
                environment.pop("HERDR_AGENT_LEDGER_DIR", None)
                environment.pop(ham_test_support.PROTECTION_ENV, None)
                completed = subprocess.run([
                    sys.executable, "-c",
                    f"import ham_test_support; from ledger_store import LedgerStore; {call}",
                ], cwd=Path(__file__).parent, env=environment, capture_output=True, text=True)
                self.assertNotEqual(completed.returncode, 0)
                self.assertIn("Test attempted to bootstrap protected ledger", completed.stderr)
                self.assertFalse((root / "home").exists())
                self.assertFalse((root / "state").exists())

    def test_swallowed_violation_still_fails_the_test(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            environment = {**os.environ, "HOME": str(root / "home"),
                           "XDG_STATE_HOME": str(root / "state")}
            environment.pop("HERDR_AGENT_LEDGER_DIR", None)
            environment.pop(ham_test_support.PROTECTION_ENV, None)
            completed = subprocess.run([sys.executable, "-c", """
import ham_test_support
import unittest
from ledger_store import LedgerStore
class SwallowsError(unittest.TestCase):
    def runTest(self):
        try:
            LedgerStore(environment={})
        except Exception:
            pass
result = unittest.TestResult()
SwallowsError().run(result)
assert len(result.failures) == 1, result.failures
"""], cwd=Path(__file__).parent, env=environment, capture_output=True, text=True)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertFalse((root / "home").exists())
            self.assertFalse((root / "state").exists())

    def test_subprocess_inherits_runner_protection_not_its_private_override(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = Path(directory) / "ledger"
            environment = {**os.environ, "HERDR_AGENT_LEDGER_DIR": str(ledger)}
            completed = subprocess.run([sys.executable, "-c", """
import ham_test_support
from ledger_store import LedgerStore
LedgerStore(canonical_name='private-child')
for protected in ham_test_support.PROTECTED_LEDGER_DIRECTORIES:
    try:
        ham_test_support.require_private_ledger(protected)
    except AssertionError:
        continue
    raise AssertionError('Lost inherited live-ledger protection')
"""], cwd=Path(__file__).parent, env=environment, capture_output=True, text=True)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertTrue((ledger / "ledger.sqlite3").is_file())
