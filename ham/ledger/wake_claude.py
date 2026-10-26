"""Claude Code idle-session wake: discovery, messaging-socket delivery, transcript evidence.

Claude Code (2.1.x) listens on a per-session Unix socket, advertised in
``<config>/sessions/<pid>.json`` as ``messagingSocketPath``.  A single JSON
line ``{"type":"user","from":...,"message":{"role":"user","content":TEXT}}``
written to it is queued by Claude: it starts a turn when idle or absorbs the
text between tool calls when busy.  The socket gives no acknowledgement and no
authentication beyond same-uid, so everything that matters is verified here:

* the target is the verified foreground Claude root of the agent's pane, whose
  registry entry carries the session UUID the HAM hook bound;
* acceptance is evidence in the session's own transcript (an enqueue alone is
  not acceptance), never the socket write.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import socket
import stat
import sys
from typing import Any, Callable, Mapping

from ham.ledger.wake_clients import Observation, WakeDeliveryFailed, WakeTarget, WakeUnavailable

SOCKET_TIMEOUT_SECONDS = 5.0
MAX_MESSAGE_BYTES = 256 * 1024
MAX_REGISTRY_BYTES = 64 * 1024
MAX_SETTINGS_BYTES = 1024 * 1024
MODE_TAIL_BYTES = 2 * 1024 * 1024
MAX_SCAN_BYTES = 16 * 1024 * 1024
SENDER = "ham"

_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")

ForegroundPid = Callable[..., "int | None"]
ProcStart = Callable[[int], "str | None"]
Connector = Callable[[str, float], socket.socket]


def config_dir(environment: Mapping[str, str] | None = None) -> Path:
    env = os.environ if environment is None else environment
    configured = env.get("CLAUDE_CONFIG_DIR")
    if configured:
        return Path(configured).expanduser()
    return Path(env.get("HOME") or os.path.expanduser("~")) / ".claude"


def linux_proc_start(pid: int) -> str | None:
    """Field 22 of /proc/<pid>/stat (clock ticks since boot); None where unavailable."""
    if not sys.platform.startswith("linux"):
        return None
    try:
        with open(f"/proc/{pid}/stat", "r", encoding="utf-8", errors="replace") as handle:
            data = handle.read()
        return data[data.rindex(")") + 2:].split()[19]
    except (OSError, ValueError, IndexError):
        return None


def _valid_pid(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 1


def _read_owned_json(path: Path, limit: int, uid: int) -> Any:
    """Parse a regular, uid-owned, non-symlink JSON file; None when it cannot be trusted."""
    try:
        info = os.lstat(path)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != uid or info.st_size > limit:
            return None
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


def _registry(directory: Path, pid: int, uid: int) -> Mapping[str, Any] | None:
    data = _read_owned_json(directory / "sessions" / f"{pid}.json", MAX_REGISTRY_BYTES, uid)
    return data if isinstance(data, dict) else None


def permission_mode(path: str | Path, size: int | None = None) -> str | None:
    """Last ``permissionMode`` in the transcript tail (the mode Claude is in now)."""
    try:
        with open(path, "rb") as handle:
            end = os.fstat(handle.fileno()).st_size if size is None else size
            start = max(0, end - MODE_TAIL_BYTES)
            handle.seek(start)
            data = handle.read(end - start)
    except OSError:
        return None
    lines = data.split(b"\n")
    if start:
        lines = lines[1:]  # first line is cut mid-record
    for line in reversed(lines):
        if b"permissionMode" not in line:
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        mode = record.get("permissionMode") if isinstance(record, dict) else None
        if isinstance(mode, str):
            return mode
    return None


def _cross_session_inbound(directory: Path, uid: int) -> str | None:
    data = _read_owned_json(directory / "settings.json", MAX_SETTINGS_BYTES, uid)
    value = data.get("crossSessionInbound") if isinstance(data, dict) else None
    return value if isinstance(value, str) else None


def _transcript(directory: Path, session: str, uid: int) -> Path:
    projects = directory / "projects"
    try:
        candidates = sorted(projects.glob(f"*/{session}.jsonl"))
    except OSError:
        candidates = []
    if not candidates:
        raise WakeUnavailable("claude_transcript_missing", "The Claude transcript for this session was not found.")
    if len(candidates) > 1:
        raise WakeUnavailable("claude_transcript_ambiguous", "More than one Claude transcript matches this session.")
    path = candidates[0]
    try:
        info = os.lstat(path)
        real = os.path.realpath(path)
        expected = os.path.join(os.path.realpath(projects), path.parent.name, path.name)
    except OSError as error:
        raise WakeUnavailable("claude_transcript_unreadable", str(error)) from error
    if not stat.S_ISREG(info.st_mode) or info.st_uid != uid or real != expected:
        raise WakeUnavailable("claude_transcript_untrusted", "The Claude transcript is not a plain file owned by this user.")
    return path


def discover(agent: Mapping[str, Any], *, environment: Mapping[str, str] | None = None,
             foreground_pid: ForegroundPid | None = None, proc_start: ProcStart | None = None,
             uid: int | None = None) -> WakeTarget:
    """Verify a live session; an absent stored PID needs exact registry/start identity."""
    if agent.get("client") != "claude":
        raise WakeUnavailable("claude_not_claude", "The agent is not a Claude Code session.")
    if not agent.get("extension_present"):
        raise WakeUnavailable("claude_hook_missing", "The HAM Claude hook has not bound this session.")
    session = agent.get("session_ref")
    if not isinstance(session, str) or not _UUID.match(session.lower()):
        raise WakeUnavailable("claude_session_unbound", "The Claude session id is not known yet.")
    session = session.lower()
    bound_pid = agent.get("native_session_pid")
    if bound_pid is not None and not _valid_pid(bound_pid):
        raise WakeUnavailable("claude_pid_unbound", "The Claude process id is not verified yet.")
    if foreground_pid is None:
        from ham.ledger.status import claude_foreground_pid as foreground_pid
    live = foreground_pid(agent, environment, require_ancestor=False)
    if live is None:
        raise WakeUnavailable("claude_process_unverified", "The pane's foreground Claude process could not be verified.")
    if bound_pid is not None and live != bound_pid:
        raise WakeUnavailable("claude_pid_mismatch", "The pane's foreground Claude is not the session HAM bound.")
    pid = live
    owner = os.getuid() if uid is None else uid
    directory = config_dir(environment)
    entry = _registry(directory, pid, owner)
    if entry is None:
        raise WakeUnavailable("claude_registry_missing", "The Claude session registry entry is missing or untrusted.")
    registered = entry.get("sessionId")
    if entry.get("pid") != pid or not isinstance(registered, str) or registered.lower() != session:
        raise WakeUnavailable("claude_session_mismatch", "The registry entry belongs to a different session.")
    if entry.get("kind") != "interactive":
        raise WakeUnavailable("claude_not_interactive", "Only interactive Claude sessions can be woken.")
    started = entry.get("procStart")
    actual = (proc_start or linux_proc_start)(pid)
    if bound_pid is None and (started is None or actual is None):
        raise WakeUnavailable("claude_pid_unbound", "The Claude process start time cannot verify an unbound process id.")
    if started is not None and actual is not None and str(started) != actual:
        raise WakeUnavailable("claude_pid_recycled", "The process start time does not match the registry entry.")
    socket_path = entry.get("messagingSocketPath")
    if not isinstance(socket_path, str) or not os.path.isabs(socket_path):
        raise WakeUnavailable("claude_socket_missing", "The Claude session advertises no messaging socket.")
    try:
        info = os.lstat(socket_path)
    except OSError as error:
        raise WakeUnavailable("claude_socket_missing", f"The Claude messaging socket is unavailable: {error}") from error
    if not stat.S_ISSOCK(info.st_mode) or info.st_uid != owner:
        raise WakeUnavailable("claude_socket_untrusted", "The messaging socket is not a socket owned by this user.")
    path = _transcript(directory, session, owner)
    try:
        size = os.stat(path).st_size
    except OSError as error:
        raise WakeUnavailable("claude_transcript_unreadable", str(error)) from error
    if permission_mode(path, size) == "bypassPermissions" and _cross_session_inbound(directory, owner) != "accept":
        raise WakeUnavailable(
            "claude_holds_peer_messages",
            "Claude is in bypassPermissions mode and holds peer messages for approval. "
            "Warning: crossSessionInbound: \"accept\" lets any same-user socket writer drive "
            "this bypassPermissions session; do not enable it for bypass sessions.")
    return WakeTarget("claude", session, pid, str(path), size,
                      {"socket_path": socket_path, "config_dir": str(directory)})


def _connect(path: str, timeout: float) -> socket.socket:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.settimeout(timeout)
        sock.connect(path)
    except BaseException:
        sock.close()
        raise
    return sock


def deliver(target: WakeTarget, text: str, *, timeout: float = SOCKET_TIMEOUT_SECONDS,
            connect: Connector | None = None) -> None:
    """Write one message line.  Only failures before any byte is written are definite."""
    path = target.extra.get("socket_path")
    if not isinstance(path, str) or not path:
        raise WakeDeliveryFailed("claude_socket_missing", "The wake target has no messaging socket.",
                                 definitely_not_delivered=True)
    data = (json.dumps({"type": "user", "from": SENDER, "message": {"role": "user", "content": text}})
            + "\n").encode("utf-8")
    if len(data) > MAX_MESSAGE_BYTES:
        raise WakeDeliveryFailed("claude_message_too_large", "The wake message exceeds the socket limit.",
                                 definitely_not_delivered=True)
    try:
        sock = (connect or _connect)(path, timeout)
    except OSError as error:
        raise WakeDeliveryFailed("claude_socket_unreachable", f"Cannot connect to the Claude socket: {error}",
                                 definitely_not_delivered=True) from error
    try:
        sock.sendall(data)
    except OSError as error:
        raise WakeDeliveryFailed("claude_socket_write_failed", f"The Claude socket write failed: {error}",
                                 definitely_not_delivered=False) from error
    finally:
        sock.close()


def _texts(content: Any) -> list[str]:
    if isinstance(content, str):
        return [content]
    if isinstance(content, list):
        return [block["text"] for block in content
                if isinstance(block, dict) and isinstance(block.get("text"), str)]
    return []


def _peer(origin: Any) -> bool:
    return isinstance(origin, dict) and origin.get("kind") == "peer"


def _accepts(record: Any, text: str, session: str) -> bool:
    """Whether a transcript record proves Claude took this exact text from a peer.

    A ``queue-operation`` enqueue alone is not acceptance (the text may be
    removed unread); the user turn that carries it, or the mid-turn
    ``queued_command`` attachment that absorbed it, is.
    """
    if not isinstance(record, dict):
        return False
    claimed = record.get("sessionId", record.get("session_id"))
    if isinstance(claimed, str) and claimed.lower() != session:
        return False
    kind = record.get("type")
    if kind == "user":
        message = record.get("message")
        return (_peer(record.get("origin")) and isinstance(message, dict)
                and any(text in part for part in _texts(message.get("content"))))
    if kind == "attachment":
        attachment = record.get("attachment")
        if not isinstance(attachment, dict) or attachment.get("type") != "queued_command":
            return False
        origin = attachment.get("origin", record.get("origin"))
        prompt = attachment.get("prompt")
        return (isinstance(prompt, str) and prompt.strip() == text) and (origin is None or _peer(origin))
    return False


def scan_transcript(path: str, offset: int, *, text: str, marker: str, session: str) -> tuple[int, str | None]:
    """Scan complete lines from ``offset``: (next offset, record id when accepted).

    A trailing partial line is left for the next scan, unless it fills the
    entire bounded window: oversized records are discarded a chunk at a time.
    A transcript shorter than the offset is rescanned from the start.
    """
    needle = marker.encode("utf-8")
    text = text.strip()  # Claude may trim surrounding whitespace; the marker keeps it unique.
    with open(path, "rb") as handle:
        size = os.fstat(handle.fileno()).st_size
        start = offset if 0 <= offset <= size else 0
        handle.seek(max(start - 1, 0))
        discarding = start > 0 and handle.read(1) != b"\n"
        data = handle.read(min(size - start, MAX_SCAN_BYTES))
    cursor = 0
    while True:
        end = data.find(b"\n", cursor)
        if end < 0:
            break
        line = data[cursor:end]
        here = start + cursor
        cursor = end + 1
        if discarding:
            discarding = False
            continue
        if needle not in line:
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if _accepts(record, text, session):
            attachment = record.get("attachment") if isinstance(record.get("attachment"), dict) else {}
            identity = record.get("uuid") or attachment.get("delivery_id") or f"offset-{here}"
            return start + cursor, str(identity)
    if cursor == 0 and len(data) == MAX_SCAN_BYTES:
        return start + len(data), None
    return start + cursor, None


def alive(pid: int, session_ref: str, *, environment: Mapping[str, str] | None = None,
          config: str | Path | None = None, uid: int | None = None) -> bool:
    """The Claude process still exists and its registry entry still names this session."""
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
    directory = Path(config) if config is not None else config_dir(environment)
    entry = _registry(directory, pid, os.getuid() if uid is None else uid)
    registered = entry.get("sessionId") if entry else None
    return isinstance(registered, str) and registered.lower() == session_ref.lower()


def observe(target: WakeTarget | None, *, journal_path: str, offset: int, marker: str, content: str,
            content_sha256: str, session_ref: str, pid: int | None,
            environment: Mapping[str, str] | None = None) -> Observation:
    session = session_ref.lower()
    try:
        next_offset, identity = scan_transcript(journal_path, max(offset, 0), text=content,
                                                marker=marker, session=session)
    except OSError:
        next_offset, identity = offset, None
    if identity is not None:
        return Observation("accepted", next_offset,
                           f"claude:peer-journal:{session}#{identity};sha256={content_sha256}")
    # Progress is not exhaustion: evidence may be in the next bounded window,
    # including after the native process has died or during journal-only recovery.
    if next_offset != max(offset, 0):
        return Observation("pending", next_offset)
    config = target.extra.get("config_dir") if target is not None else None
    if pid is None or not alive(pid, session, environment=environment, config=config):
        return Observation("gone", next_offset)
    return Observation("pending", next_offset)
