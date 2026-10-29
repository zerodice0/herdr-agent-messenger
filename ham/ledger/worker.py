"""One detached flock worker per host ledger; events remain finite one-shots."""

from __future__ import annotations

from ham.paths import PLUGIN_ROOT, trusted

from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import shutil
import sys
import time
from typing import Any, Callable, Iterator, Mapping

from ham.ledger.remote import RemoteLedger
from ham.ledger.display_repair import load as load_display_repair
from ham.ledger.settings import RepoSettings
from ham.ledger.status import AgentRegistry
from ham.ledger.store import LedgerError, LedgerStore


def worker_environment(environment: Mapping[str, str] | None = None) -> dict[str, str]:
    """Pin a usable local toolchain without trusting relative/writable PATH entries.

    Preserve trusted custom install directories, and supply common Linux/macOS
    directories even when the first CLI starter had a minimal non-login PATH.
    """
    values = dict(os.environ if environment is None else environment)
    home = Path(values.get("HOME") or Path.home())

    directories = [*values.get("PATH", "").split(os.pathsep),
                   str(home / ".local/bin"), str(home / ".cargo/bin"),
                   str(home / ".bun/bin"), str(home / ".local/share/mise/shims"),
                   "/opt/homebrew/bin", "/home/linuxbrew/.linuxbrew/bin",
                   "/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin"]
    selected = []
    for directory in directories:
        path = Path(directory)
        if path.is_absolute() and path.is_dir() and trusted(path):
            absolute = str(path.resolve())
            if absolute not in selected:
                selected.append(absolute)
    values["PATH"] = os.pathsep.join(selected)
    for name, key in (("herdr", "HERDR_BIN_PATH"), ("codex", "CODEX_BIN")):
        recorded = values.get(key, "").removesuffix(" (deleted)")
        candidate = Path(recorded) if recorded else None
        if not (candidate is not None and candidate.is_absolute() and
                candidate.is_file() and os.access(candidate, os.X_OK) and trusted(candidate)):
            found = shutil.which(name, path=values["PATH"])
            candidate = Path(found) if found else None
        if candidate is not None and trusted(candidate):
            values[key] = str(candidate.resolve())
        else:
            values.pop(key, None)
    return values


@contextmanager
def worker_lock(store: LedgerStore) -> Iterator[bool]:
    descriptor = os.open(store.directory / "worker.lock", os.O_CREAT | os.O_RDWR, 0o600)
    acquired = False
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except BlockingIOError:
            pass
        yield acquired
    finally:
        if acquired:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def maintenance_pending(store: LedgerStore) -> bool:
    with store.read() as connection:
        if any(entry["state"] == "pending" for entry in load_display_repair(connection).values()):
            return True
        if connection.execute("SELECT 1 FROM outbox WHERE state='pending' LIMIT 1").fetchone():
            return True
        if connection.execute("SELECT 1 FROM deliveries WHERE authoritative=1 AND state IN ('queued','delivering') LIMIT 1").fetchone():
            return True
        if connection.execute("SELECT 1 FROM agents a JOIN repo_settings r USING(repo_id) WHERE a.host=? "
                              "AND a.active=1 AND r.field='mode' "
                              "AND r.value IN ('\"manual\"','\"auto\"') LIMIT 1", (store.host,)).fetchone():
            return True
        for row in connection.execute(
                "SELECT w.repo_id,w.host,w.workspace_id,t.workspace_id AS target_workspace_id "
                "FROM workspace_settings w JOIN terminal_slots t ON t.host=w.host "
                "AND t.terminal_id=json_extract(w.value,'$.terminal_id') "
                "WHERE w.host=? AND w.field='main' AND json_extract(w.value,'$.host')=w.host "
                "AND t.present=1", (store.host,)):
            # Only effective anchor settings watch their designated shell. The
            # target may live in any linked workspace, even with no active agents.
            if (RepoSettings.scope_workspace(connection, row["repo_id"], row["host"], row["workspace_id"])
                    == row["workspace_id"] == RepoSettings.scope_workspace(
                        connection, row["repo_id"], row["host"], row["target_workspace_id"])):
                return True
    return False


def worker_status(store: LedgerStore) -> dict[str, Any]:
    with worker_lock(store) as acquired:
        running = not acquired
    pid = None
    if running:
        try:
            pid = int((store.directory / "worker.pid").read_text())
        except (OSError, ValueError):
            pass
    return {"running": running, "pid": pid, "maintenance_pending": maintenance_pending(store)}


def ensure_worker(store: LedgerStore, environment: Mapping[str, str] | None = None) -> dict[str, Any]:
    if not maintenance_pending(store):
        return {"started": False, "reason": "no_pending_work"}
    with worker_lock(store) as acquired:
        if not acquired:
            return {"started": False, "reason": "already_running"}
    # A double fork detaches without leaving Popen objects or zombies in a
    # short-lived CLI. The worker itself wins/rechecks flock after exec.
    process = os.fork()
    if process == 0:
        try:
            os.setsid()
            second = os.fork()
            if second:
                os._exit(0)
            values = dict(os.environ if environment is None else environment)
            values["HERDR_AGENT_LEDGER_DIR"] = str(store.directory.resolve())
            log = os.open(store.directory / "worker.log", os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600)
            null = os.open(os.devnull, os.O_RDONLY)
            os.dup2(null, 0)
            os.dup2(log, 1)
            os.dup2(log, 2)
            os.close(null)
            os.close(log)
            os.chdir(PLUGIN_ROOT)
            os.execve(sys.executable, [sys.executable, "-m", "ham", "worker", "run"], values)
        except BaseException:
            os._exit(1)
    os.waitpid(process, 0)
    return {"started": True, "reason": "detached_spawn_requested"}


def ensure_after_mutation(store: LedgerStore, payload: dict[str, Any],
                          environment: Mapping[str, str] | None = None, *,
                          ensure: Callable[..., Any] | None = None) -> dict[str, Any]:
    """Request the existing host worker without failing a committed mutation."""
    values = os.environ if environment is None else environment
    if values.get("HERDR_AGENT_LEDGER_AUTOSTART", "1") == "0":
        return payload
    try:
        (ensure_worker if ensure is None else ensure)(store, environment)
    except (OSError, LedgerError) as error:
        message = error.message if isinstance(error, LedgerError) else str(error)
        return {**payload, "warnings": [*payload.get("warnings", []),
                {"code": "worker_start_failed", "message": message, "committed": True}]}
    return payload


def run_worker(store: LedgerStore, environment: Mapping[str, str] | None = None, *,
               once: bool = False, remote: RemoteLedger | None = None) -> dict[str, Any]:
    from ham.ledger.native_wake import NativeWaker
    from ham.ledger.runtime import NativeReceiver
    environment = worker_environment(environment)
    observer = NativeReceiver(store, environment=environment)
    waker = NativeWaker(store, observer, environment=environment)
    service = remote or RemoteLedger(store, environment)
    totals = {"sent": 0, "failed": 0, "deferred": 0}
    last_retention: float | None = None
    while True:
        with worker_lock(store) as acquired:
            if not acquired:
                return {"running": False, "reason": "already_running", **totals}
            pidfile = store.directory / "worker.pid"
            descriptor = os.open(pidfile, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
            with os.fdopen(descriptor, 'w') as stream:
                stream.write(str(os.getpid()))
            try:
                print(json.dumps({"worker_binaries": {
                    "herdr": environment.get("HERDR_BIN_PATH"),
                    "ssh": shutil.which("ssh", path=environment["PATH"]),
                    "codex": environment.get("CODEX_BIN")},
                    "path": environment["PATH"]}), file=sys.stderr, flush=True)
                while True:
                    if last_retention is None or time.monotonic() - last_retention > 3600:
                        store.retention()
                        from ham.agents.request_lifecycle import prune_request_records
                        prune_request_records(environment)
                        last_retention = time.monotonic()
                    if maintenance_pending(store):
                        try:
                            AgentRegistry(store).refresh(environment)
                            observer.observe_claude()
                        except (LedgerError, OSError) as error:
                            # An unavailable Herdr or transcript is not terminal-closure evidence.
                            message = error.message if isinstance(error, LedgerError) else str(error)
                            store.note_notification_failure("worker sweep: " + message)
                        else:
                            # Wake only on a fresh registry; a stale snapshot must never wake anyone.
                            try:
                                waker.sweep()
                            except (LedgerError, OSError) as error:
                                message = error.message if isinstance(error, LedgerError) else str(error)
                                store.note_notification_failure("worker wake: " + message)
                    # Always do local refresh/observation/wake first. One bounded
                    # transport sweep also forwards receipts produced above.
                    for key, value in service.pump().items():
                        totals[key] += value
                    pending_after_sweep = maintenance_pending(store)
                    if once or not pending_after_sweep:
                        break
                    time.sleep(5)
            finally:
                pidfile.unlink(missing_ok=True)
        # Publish unlock BEFORE checking emptiness again. A sender committing
        # before unlock may have observed our flock and declined to start a
        # replacement; we resume that work ourselves. A later sender can acquire
        # the now-free flock. No new process/autostart is needed for this handoff,
        # and an explicit one-pass command never turns into a daemon.
        if once or not maintenance_pending(store):
            return {"running": False, "reason": "one_pass" if once else "no_pending_work", **totals}


def main(argv: list[str] | None = None) -> int:
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("run", "once", "ensure", "status"))
    arguments = parser.parse_args(argv)
    store = LedgerStore()
    if arguments.command in {"run", "once"}:
        payload = run_worker(store, once=arguments.command == "once")
    elif arguments.command == "ensure":
        payload = ensure_worker(store)
    else:
        payload = worker_status(store)
    print(json.dumps(store.annotate(payload), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
