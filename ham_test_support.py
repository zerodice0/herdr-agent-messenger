"""Test-only tripwire: a missing fixture must never bootstrap a user's ledger.

Import this before product modules in each test module, including when running a
single test. Explicit private stores and fixture HOME/XDG environments retain
normal product behavior; this guard never silently redirects a ledger.
"""
from __future__ import annotations

from functools import wraps
import json
import os
from pathlib import Path
import pwd
import sys
import tempfile
import unittest
from unittest import mock

import ledger_store


def protected_ledger_directories(environment: dict[str, str], account_home: Path) -> frozenset[Path]:
    home = Path(environment.get("HOME") or account_home)
    directories = {
        account_home / ".local/state/herdr-agent-messenger/ledger",
        home / ".local/state/herdr-agent-messenger/ledger",
        Path(environment.get("XDG_STATE_HOME") or home / ".local/state")
        / "herdr-agent-messenger/ledger",
    }
    if environment.get("HERDR_AGENT_LEDGER_DIR"):
        directories.add(Path(environment["HERDR_AGENT_LEDGER_DIR"]))
    return frozenset(path.expanduser().resolve() for path in directories)


# A subprocess may import a shared test fixture after its parent has already
# supplied a private ledger override. Keep the original runner's protected
# roots, rather than mistakenly treating that new fixture ledger as live.
PROTECTION_ENV = "HAM_TEST_PROTECTED_LEDGER_DIRS"
if PROTECTION_ENV in os.environ:
    PROTECTED_LEDGER_DIRECTORIES = frozenset(
        Path(value).resolve() for value in json.loads(os.environ[PROTECTION_ENV])
    )
else:
    PROTECTED_LEDGER_DIRECTORIES = protected_ledger_directories(
        dict(os.environ), Path(pwd.getpwuid(os.getuid()).pw_dir),
    )
    os.environ[PROTECTION_ENV] = json.dumps(sorted(map(str, PROTECTED_LEDGER_DIRECTORIES)))
_violations: list[str] = []


def require_private_ledger(directory: Path | str) -> None:
    path = Path(directory).expanduser().resolve()
    for protected in PROTECTED_LEDGER_DIRECTORIES:
        if path == protected or protected in path.parents:
            raise AssertionError(
                f"Test attempted to bootstrap protected ledger {path}. "
                "Give the fixture a private HERDR_AGENT_LEDGER_DIR or HOME/XDG_STATE_HOME."
            )


_original_init = ledger_store.LedgerStore.__init__


@wraps(_original_init)
def _private_ledger_init(self, directory=None, *, environment=None, canonical_name=None):
    selected = directory if directory is not None else ledger_store.ledger_directory(environment)
    try:
        require_private_ledger(selected)
    except AssertionError as error:
        _violations.append(str(error))
        raise
    _original_init(self, directory, environment=environment, canonical_name=canonical_name)


ledger_store.LedgerStore.__init__ = _private_ledger_init
_original_run = unittest.TestCase.run


@wraps(_original_run)
def _run_with_ledger_tripwire(self, result=None):
    # Some product fallback paths deliberately catch Exception. Still make a
    # swallowed tripwire an explicit test failure, rather than hiding the leak.
    if result is None:
        result = self.defaultTestResult()
    before = len(_violations)
    returned = _original_run(self, result)
    if len(_violations) > before:
        try:
            raise AssertionError("\n".join(_violations[before:]))
        except AssertionError:
            result.addFailure(self, sys.exc_info())
    return returned


unittest.TestCase.run = _run_with_ledger_tripwire


class IsolatedLedgerTest(unittest.TestCase):
    """The private HOME/ledger fixture used by legacy CLI transport tests."""

    def setUp(self):
        super().setUp()
        directory = tempfile.TemporaryDirectory(prefix="ham-test-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        home = self.root / "home"
        self.environment = {
            "HOME": str(home),
            "XDG_CONFIG_HOME": str(home / ".config"),
            "XDG_STATE_HOME": str(home / ".local/state"),
            "XDG_CACHE_HOME": str(home / ".cache"),
            "HERDR_AGENT_LEDGER_DIR": str(self.root / "ledger"),
            "HERDR_AGENT_REQUEST_STATE_DIR": str(self.root / "requests"),
            "HERDR_AGENT_LEDGER_AUTOSTART": "0",
        }
        patcher = mock.patch.dict(os.environ, self.environment)
        patcher.start()
        self.addCleanup(patcher.stop)
