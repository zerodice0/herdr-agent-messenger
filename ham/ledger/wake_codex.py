"""Codex idle-session wake: discovery, `codex queue` delivery and rollout evidence.

Herdr reports no Codex session id, so the thread is derived from what the
pane's foreground Codex process holds open: its rollout
(`$CODEX_HOME/sessions/YYYY/MM/DD/rollout-<ts>-<thread>.jsonl`) and its
thread-writer lock (`$CODEX_HOME/thread-writer-locks/<thread>.lock`).  Embedded
mode (`features.daemon_auto_start = false`, the mode HAM was verified against)
keeps both open in the TUI process itself.  When Codex runs with a shared
managed app-server daemon the TUI holds neither, and the daemon's threads
cannot be attributed to one pane, so no wake is attempted.

`codex queue` exit 0 proves only that a row was written.  Acceptance is the
exact message text appearing as a user message in the thread's rollout.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
from typing import Any, Callable, Mapping, Sequence

from ham.agents.directory import herdr_executable, run_bounded_command
from ham.ledger.wake_clients import Observation, WakeDeliveryFailed, WakeTarget, WakeUnavailable
from ham.paths import trusted

CODEX_QUEUE_TIMEOUT_SECONDS = 30.0
# One argv element is capped near 128 KiB on Linux; stay well below it.
MAX_QUEUE_MESSAGE_BYTES = 100_000
MAX_SCAN_BYTES = 16 * 1024 * 1024
MAX_META_LINE_BYTES = 4 * 1024 * 1024

_UUID = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
_ROLLOUT_NAME = re.compile(r"rollout-\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}-(" + _UUID + r")\.jsonl")
_LOCK_NAME = re.compile(r"(" + _UUID + r")\.lock")

_BINARY_DIRECTORIES = (
    "~/.local/bin",
    "/opt/homebrew/bin",
    "/home/linuxbrew/.linuxbrew/bin",
    "/usr/local/bin",
    "/usr/bin",
)

PaneQuery = Callable[..., Mapping[str, Any]]
OpenPaths = Callable[[int], "list[str] | None"]
BinaryFinder = Callable[[Mapping[str, str] | None], "str | None"]
QueueRunner = Callable[[Sequence[str], Mapping[str, str], float], "subprocess.CompletedProcess[str]"]


# ----------------------------------------------------------------- binary


def find_codex_binary(environment: Mapping[str, str] | None = None) -> str | None:
    """CODEX_BIN, PATH, then the usual install directories."""
    env = os.environ if environment is None else environment

    def usable(candidate: str) -> bool:
        return (os.path.isabs(candidate) and os.path.isfile(candidate) and
                os.access(candidate, os.X_OK) and trusted(Path(candidate)))

    configured = env.get("CODEX_BIN")
    if configured and usable(configured):
        return configured
    found = shutil.which("codex", path=env.get("PATH"))
    if found and usable(found):
        return found
    home = env.get("HOME") or os.path.expanduser("~")
    for directory in _BINARY_DIRECTORIES:
        expanded = os.path.join(home, directory[2:]) if directory.startswith("~/") else directory
        candidate = os.path.join(expanded, "codex")
        if usable(candidate):
            return candidate
    return None


# ------------------------------------------------------------ process info


def _herdr_query(environment: Mapping[str, str] | None, *arguments: str) -> Mapping[str, Any]:
    result = run_bounded_command([herdr_executable(environment), *arguments],
                                 timeout=5, environment=environment)
    payload = json.loads(result.stdout[result.stdout.index('{'):])
    if result.returncode or payload.get("error") or payload.get("ok") is False:
        raise ValueError("Unreadable herdr response")
    return payload["result"]


def _is_codex_process(process: Mapping[str, Any]) -> bool:
    argv = process.get("argv")
    argv = [str(item) for item in argv] if isinstance(argv, list) else []
    if process.get("name") == "codex" or (argv and Path(argv[0]).name == "codex"):
        return True
    # npm launcher: `node …/bin/codex` in front of the native binary.
    return (len(argv) > 1 and Path(argv[0]).name == "node"
            and Path(argv[1]).name in {"codex", "codex.js"})


def _valid_pid(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 1


def _codex_roots(agent: Mapping[str, Any], environment: Mapping[str, str] | None,
                 query: PaneQuery | None) -> list[int]:
    """Verified foreground Codex processes of the agent's pane."""
    pane_id = str(agent.get("pane_id") or "")
    terminal_id = str(agent.get("terminal_id") or "")
    if not pane_id or not terminal_id:
        raise WakeUnavailable("codex_pane_unknown", "The agent has no pane/terminal identity.")
    ask = query or (lambda *arguments: _herdr_query(environment, *arguments))
    try:
        pane = ask("pane", "get", pane_id)["pane"]
        info = ask("pane", "process-info", "--pane", pane_id)["process_info"]
        if (pane["pane_id"] != pane_id or pane["terminal_id"] != terminal_id
                or info["pane_id"] != pane_id):
            raise WakeUnavailable("codex_pane_mismatch", "Herdr reports a different pane/terminal.")
        group = info["foreground_process_group_id"]
        if not _valid_pid(group):
            raise WakeUnavailable("codex_not_foreground", "The pane has no foreground process group.")
        roots = [process["pid"] for process in info["foreground_processes"]
                 if _is_codex_process(process)]
    except WakeUnavailable:
        raise
    except (OSError, ValueError, TypeError, KeyError, AttributeError, subprocess.SubprocessError):
        raise WakeUnavailable("codex_process_unreadable",
                              "Herdr process information for the pane is unreadable.") from None
    roots = [pid for pid in roots if _valid_pid(pid)]
    if not roots:
        raise WakeUnavailable("codex_not_foreground", "Codex is not the pane's foreground process.")
    return sorted(set(roots))


# -------------------------------------------------------------- open files


def open_paths(pid: int) -> list[str] | None:
    """Paths of the files a process has open; [] if it is gone, None if unknowable.

    Linux reads `/proc/<pid>/fd`; macOS falls back to `lsof -a -p <pid> -Fn`.
    """
    fd_directory = f"/proc/{pid}/fd"
    if os.path.isdir("/proc/self/fd"):
        try:
            names = os.listdir(fd_directory)
        except (FileNotFoundError, ProcessLookupError, NotADirectoryError):
            return []
        except OSError:
            return None
        paths: list[str] = []
        for name in names:
            try:
                paths.append(os.readlink(f"{fd_directory}/{name}"))
            except OSError:
                continue
        return paths
    try:
        result = subprocess.run(["lsof", "-nP", "-a", "-p", str(pid), "-Fn"], capture_output=True,
                                text=True, check=False, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return parse_lsof_names(result.stdout, returncode=result.returncode)


def parse_lsof_names(output: str, *, returncode: int = 0) -> list[str] | None:
    """`lsof -Fn` names; no `p` record at all means the process does not exist."""
    names = [line[1:] for line in output.splitlines() if line.startswith("n")]
    if any(line.startswith("p") for line in output.splitlines()):
        return names
    return [] if returncode == 1 and not output.strip() else None


def _held_threads(paths: Sequence[str]) -> tuple[dict[str, set[Path]], dict[str, set[Path]]]:
    """thread id -> rollout paths and -> lock paths among the open files."""
    rollouts: dict[str, set[Path]] = {}
    locks: dict[str, set[Path]] = {}
    for raw in paths:
        if not raw.startswith("/"):
            continue
        path = Path(raw)
        rollout = _ROLLOUT_NAME.fullmatch(path.name)
        if rollout and len(path.parents) > 4 and path.parents[3].name == "sessions":
            rollouts.setdefault(rollout[1].lower(), set()).add(path)
            continue
        lock = _LOCK_NAME.fullmatch(path.name)
        if lock and path.parent.name == "thread-writer-locks":
            locks.setdefault(lock[1].lower(), set()).add(path)
    return rollouts, locks


def _read_first_line(path: Path) -> bytes | None:
    try:
        with open(path, "rb") as handle:
            line = handle.readline(MAX_META_LINE_BYTES + 1)
    except OSError:
        return None
    if len(line) > MAX_META_LINE_BYTES or not line.endswith(b"\n"):
        return None
    return line


def _session_meta(path: Path) -> Mapping[str, Any] | None:
    line = _read_first_line(path)
    if line is None:
        return None
    try:
        record = json.loads(line)
        payload = record["payload"]
    except (ValueError, KeyError, TypeError):
        return None
    if record.get("type") != "session_meta" or not isinstance(payload, dict):
        return None
    return payload


def _is_subagent(meta: Mapping[str, Any]) -> bool:
    # [INFERENCE] Spawned sub-agent threads carry a structured `source`
    # (`{"subagent": …}`); top-level sessions use a plain string such as "cli".
    return isinstance(meta.get("source"), Mapping)


# --------------------------------------------------------------- discovery


def discover(agent: Mapping[str, Any], *, environment: Mapping[str, str] | None = None,
             query: PaneQuery | None = None, list_open_paths: OpenPaths | None = None,
             find_binary: BinaryFinder | None = None) -> WakeTarget:
    if agent.get("client") != "codex":
        raise WakeUnavailable("codex_not_codex", "The agent is not a Codex session.")
    binary = (find_binary or find_codex_binary)(environment)
    if not binary:
        raise WakeUnavailable("codex_missing", "The codex binary was not found (CODEX_BIN, PATH, install directories).")
    roots = _codex_roots(agent, environment, query)
    lister = list_open_paths or open_paths

    holders: list[tuple[int, dict[str, set[Path]], dict[str, set[Path]]]] = []
    for pid in roots:
        paths = lister(pid)
        if paths is None:
            raise WakeUnavailable("codex_fd_unreadable", f"The open files of Codex process {pid} cannot be read.")
        rollouts, locks = _held_threads(paths)
        if rollouts or locks:
            holders.append((pid, rollouts, locks))
    if not holders:
        raise WakeUnavailable(
            "codex_no_rollout",
            "Codex holds no thread rollout yet (no first turn, or it uses a shared app-server daemon).")
    if len(holders) > 1:
        raise WakeUnavailable("codex_ambiguous_root", "More than one Codex process in the pane holds a thread.")
    pid, rollouts, locks = holders[0]

    # Sub-agent threads live in the same process; only the top-level one counts.
    threads: dict[str, Path | None] = {}
    for thread in set(rollouts) | set(locks):
        candidates = rollouts.get(thread, set())
        if len(candidates) > 1:
            raise WakeUnavailable("codex_ambiguous_thread", f"Thread {thread} has several open rollouts.")
        rollout = next(iter(candidates), None)
        if rollout is not None:
            meta = _session_meta(rollout)
            if meta is not None and _is_subagent(meta):
                continue
        threads[thread] = rollout
    if len(threads) != 1:
        raise WakeUnavailable("codex_ambiguous_thread" if threads else "codex_no_rollout",
                              "The Codex process holds more than one top-level thread." if threads
                              else "Codex holds no top-level thread rollout yet.")
    thread, rollout = next(iter(threads.items()))
    if rollout is None:
        raise WakeUnavailable("codex_no_rollout", "Codex holds the thread lock but no rollout is open yet.")

    # Layout was verified in _held_threads: <home>/sessions/Y/M/D/<rollout>.
    home = rollout.parents[4]
    for lock in locks.get(thread, set()):
        if lock.parent.parent != home:
            raise WakeUnavailable("codex_home_mismatch", "Thread lock and rollout belong to different CODEX_HOME directories.")
    try:
        status = os.stat(rollout)
        if not stat.S_ISREG(status.st_mode) or os.path.islink(rollout) or status.st_uid != os.getuid():
            raise WakeUnavailable("codex_rollout_untrusted", "The rollout is not a regular file owned by this user.")
    except OSError:
        raise WakeUnavailable("codex_no_rollout", "The open rollout file cannot be read.") from None
    if status.st_size == 0:
        raise WakeUnavailable("codex_no_rollout", "The rollout is still empty (no first turn).")
    meta = _session_meta(rollout)
    if meta is None:
        raise WakeUnavailable("codex_session_meta_missing", "The rollout has no readable session_meta first line.")
    if str(meta.get("id", "")).lower() != thread:
        raise WakeUnavailable("codex_session_meta_mismatch", "session_meta.id does not match the rollout's thread id.")
    return WakeTarget(client="codex", session_ref=thread, pid=pid, journal_path=str(rollout),
                      journal_offset=status.st_size,
                      extra={"codex_home": str(home), "binary": binary})


# ---------------------------------------------------------------- delivery


def _run_queue(command: Sequence[str], env: Mapping[str, str], timeout: float) -> subprocess.CompletedProcess[str]:
    return subprocess.run(list(command), stdin=subprocess.DEVNULL, capture_output=True, text=True,
                          check=False, timeout=timeout, env=dict(env))


def deliver(target: WakeTarget, text: str, *, timeout: float = CODEX_QUEUE_TIMEOUT_SECONDS,
            run_queue: QueueRunner | None = None) -> None:
    binary = target.extra.get("binary")
    home = target.extra.get("codex_home")
    if not binary or not home:
        raise WakeDeliveryFailed("codex_target_incomplete", "The wake target lacks the codex binary or CODEX_HOME.",
                                 definitely_not_delivered=True)
    if len(text.encode("utf-8")) > MAX_QUEUE_MESSAGE_BYTES:
        raise WakeDeliveryFailed("codex_message_too_large", "The wake message exceeds the command-line limit.",
                                 definitely_not_delivered=True)
    command = [str(binary), "queue", "--thread", target.session_ref, f"--message={text}"]
    env = {**os.environ, "CODEX_HOME": str(home)}
    try:
        result = (run_queue or _run_queue)(command, env, timeout)
    except subprocess.TimeoutExpired:
        raise WakeDeliveryFailed("codex_queue_timeout", f"`codex queue` did not finish within {timeout:g}s.",
                                 definitely_not_delivered=False) from None
    except OSError as error:
        raise WakeDeliveryFailed("codex_missing", f"Cannot run codex: {error}",
                                 definitely_not_delivered=True) from None
    if result.returncode == 0:
        return
    detail = " ".join((result.stderr or result.stdout or "").split())[:300]
    # A signal death may follow the sqlite write; only a normal failure exit is a clean refusal.
    raise WakeDeliveryFailed("codex_queue_failed", f"`codex queue` exited {result.returncode}: {detail}",
                             definitely_not_delivered=result.returncode > 0)


# ------------------------------------------------------------- observation


def _text_parts(content: Any) -> list[str]:
    if not isinstance(content, list):
        return []
    return [part["text"] for part in content
            if isinstance(part, Mapping) and isinstance(part.get("text"), str)]


def _matches(parts: Sequence[str], content: str) -> bool:
    return bool(parts) and (content in parts or "".join(parts) == content)


def _record_matches(record: Mapping[str, Any], content: str) -> bool:
    payload = record.get("payload")
    if not isinstance(payload, Mapping):
        return False
    kind = record.get("type")
    if kind == "response_item":
        return (payload.get("type") == "message" and payload.get("role") == "user"
                and _matches(_text_parts(payload.get("content")), content))
    if kind == "event_msg":
        if payload.get("type") == "user_message":
            return payload.get("message") == content
        item = payload.get("item")
        return (payload.get("type") == "item_completed" and isinstance(item, Mapping)
                and item.get("type") == "UserMessage"
                and _matches(_text_parts(item.get("content")), content))
    return False


def _prefilter(marker: str) -> bytes | None:
    """Raw-bytes needle that must appear in any JSON line carrying the marker."""
    if marker and marker.isascii() and marker.isprintable() and not set(marker) & {'"', "\\"}:
        return marker.encode()
    return None


def scan_rollout(path: str, offset: int, *, content: str, marker: str) -> tuple[int, str | None]:
    """Scan complete lines after `offset`; returns (next offset, evidence position or None).

    A partially written trailing line is never consumed unless it fills the
    window. Oversized records advance in chunks; their final fragment is ignored.
    """
    needle = _prefilter(marker)
    with open(path, "rb") as handle:
        handle.seek(max(offset - 1, 0))
        discarding = offset > 0 and handle.read(1) != b"\n"
        window = handle.read(MAX_SCAN_BYTES)
    cursor = 0
    while True:
        end = window.find(b"\n", cursor)
        if end < 0:
            break
        line = window[cursor:end]
        line_offset = offset + cursor
        cursor = end + 1
        if discarding:
            discarding = False
            continue
        if needle is not None and needle not in line:
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, Mapping) and _record_matches(record, content):
            ordinal = record.get("ordinal")
            position = (str(ordinal) if isinstance(ordinal, int) and not isinstance(ordinal, bool)
                        else f"offset={line_offset}")
            return offset + cursor, position
    if cursor == 0 and len(window) == MAX_SCAN_BYTES:
        # A single line larger than the window cannot be our short message; skip it.
        return offset + len(window), None
    return offset + cursor, None


def alive(pid: int, session_ref: str, *, list_open_paths: OpenPaths | None = None) -> bool:
    """The process still exists and still holds this thread's rollout/lock."""
    if not _valid_pid(pid):
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    except OSError:
        return False
    paths = (list_open_paths or open_paths)(pid)
    if paths is None:
        return True  # cannot refute; leave it to the generation checks of the core
    rollouts, locks = _held_threads(paths)
    thread = session_ref.lower()
    return thread in rollouts or thread in locks


def observe(target: WakeTarget | None, *, journal_path: str, offset: int, marker: str, content: str,
            content_sha256: str, session_ref: str, pid: int | None,
            list_open_paths: OpenPaths | None = None) -> Observation:
    try:
        next_offset, position = scan_rollout(journal_path, max(offset, 0), content=content, marker=marker)
    except OSError:
        next_offset, position = offset, None
    if position is not None:
        return Observation("accepted", next_offset,
                           f"codex:rollout:{session_ref}#{position};sha256={content_sha256}")
    # Drain bounded windows before treating a dead process as lost delivery.
    # With no target/pid this also supports journal-only restart recovery.
    if next_offset != max(offset, 0):
        return Observation("pending", next_offset)
    if pid is None or not alive(pid, session_ref, list_open_paths=list_open_paths):
        return Observation("gone", next_offset)
    return Observation("pending", next_offset)
