"""Test-only tripwires: missing fixtures must never mutate a user's HAM state.

Import this before product modules in each test module, including when running a
single test. Explicit private stores and fixture HOME/XDG environments retain
normal product behavior; these guards never silently redirect storage.
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

from ham.ledger import store as ledger_store


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
_inherited_ledger_protection = PROTECTION_ENV in os.environ
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


def protected_state_directories(environment: dict[str, str], account_home: Path) -> frozenset[Path]:
    home = Path(environment.get("HOME") or account_home)
    state = Path(environment.get("XDG_STATE_HOME") or home / ".local/state")
    roots = {
        base / name
        for base in (account_home / ".local/state", home / ".local/state", state)
        for name in ("herdr-agent-messenger", "herdr-agent-labels")
    }
    if environment.get("HERDR_AGENT_REQUEST_STATE_DIR"):
        roots.add(Path(environment["HERDR_AGENT_REQUEST_STATE_DIR"]))
    return frozenset(path.expanduser().resolve() for path in roots)


STATE_PROTECTION_ENV = "HAM_TEST_PROTECTED_STATE_DIRS"
if STATE_PROTECTION_ENV in os.environ:
    PROTECTED_STATE_DIRECTORIES = frozenset(
        Path(value).resolve() for value in json.loads(os.environ[STATE_PROTECTION_ENV])
    )
else:
    # An explicitly inherited ledger tripwire may accompany a sandbox HOME.
    # Derive its HAM roots without accidentally protecting the new sandbox.
    account_home = Path(pwd.getpwuid(os.getuid()).pw_dir)
    roots = set(protected_state_directories({}, account_home))
    for path in PROTECTED_LEDGER_DIRECTORIES:
        if path.name == "ledger" and path.parent.name in {"herdr-agent-messenger", "herdr-agent-labels"}:
            roots.update(path.parent.parent / name for name in ("herdr-agent-messenger", "herdr-agent-labels"))
    if not _inherited_ledger_protection:
        roots.update(protected_state_directories(dict(os.environ), account_home))
    PROTECTED_STATE_DIRECTORIES = frozenset(roots)
    os.environ[STATE_PROTECTION_ENV] = json.dumps(sorted(map(str, roots)))


def require_private_state(directory: Path | str) -> None:
    path = Path(directory).expanduser().resolve()
    for protected in PROTECTED_STATE_DIRECTORIES:
        if path == protected or protected in path.parents:
            raise AssertionError(
                f"Test attempted to mutate protected HAM state {path}. "
                "Give the fixture a private HOME/XDG_STATE_HOME and request state directory."
            )


def _state_write_tripwire(event, arguments):
    # Cover request JSON, delivery locks, registry files, renames and removals,
    # including direct filesystem writers which do not use LedgerStore.
    paths = ()
    if event == "open":
        path, mode, flags = arguments
        if flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND):
            paths = (path,)
    elif event in {"os.mkdir", "os.remove", "os.rmdir", "os.chmod", "os.utime", "os.truncate"}:
        paths = arguments[:1]
    elif event in {"os.rename", "os.link"}:
        paths = arguments[:2]
    elif event == "os.symlink":
        paths = arguments[1:2]
    for path in paths:
        if not isinstance(path, (str, bytes, os.PathLike)):
            continue
        try:
            require_private_state(os.fsdecode(path))
        except AssertionError as error:
            _violations.append(str(error))
            raise


sys.addaudithook(_state_write_tripwire)


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
