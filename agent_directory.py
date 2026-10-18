"""Discover local and SSH-hosted Herdr agents and dispatch prompts."""

from __future__ import annotations

from contextlib import suppress
from dataclasses import asdict, dataclass, field, replace
import glob
import json
import os
from pathlib import Path
import queue
import re
import selectors
import shlex
import socket
import subprocess
import tempfile
import threading
import time
from typing import Any, Callable, Iterable, Mapping, Sequence, TypeVar


DELETED_BINARY_SUFFIX = " (deleted)"
REMOTE_DISCOVERY_TIMEOUT_SECONDS = 5.0
REMOTE_CONNECT_TIMEOUT_SECONDS = 3
REMOTE_CACHE_TTL_SECONDS = 10.0
REMOTE_FAILURE_TTL_SECONDS = 30.0
MAX_REMOTE_WORKERS = 8
MAX_SEND_WORKERS = 8
MAX_CAPTURE_BYTES = 2 * 1024 * 1024
PLUGIN_ID = "herdr.agent-messenger"
LEGACY_PLUGIN_ID = "herdr.agent-labels"
SSH_CONFIG_ENV = "HERDR_AGENT_MESSENGER_SSH_CONFIG"
LEGACY_SSH_CONFIG_ENV = "HERDR_AGENT_LABELS_SSH_CONFIG"
SSH_HOSTS_FILE_ENV = "HERDR_AGENT_MESSENGER_SSH_HOSTS_FILE"
LEGACY_SSH_HOSTS_FILE_ENV = "HERDR_AGENT_LABELS_SSH_HOSTS_FILE"
TaskResult = TypeVar("TaskResult")


@dataclass(frozen=True)
class AgentRecord:
    host: str
    name: str
    pane_id: str
    workspace_id: str
    workspace_label: str
    status: str
    session_id: str
    cwd: str
    local: bool
    stale: bool = False
    revision: int = 0
    agent_kind: str = ""
    terminal_id: str = ""
    workspace_is_worktree: bool = False
    route_target: str = ""
    focused: bool = field(default=False, compare=False)

    @property
    def target(self) -> str:
        return self.route_target or self.name or self.pane_id

    @property
    def identity(self) -> str:
        transport = "local" if self.local else "ssh"
        occupant = self.session_id or (
            f"{self.pane_id}:{self.terminal_id}:{self.agent_kind}:"
            f"{self.cwd}:{self.target}:{self.revision}"
        )
        return f"{transport}:{self.host}:{occupant}"

    @property
    def qualified_name(self) -> str:
        return f"{self.host}/{self.name or self.pane_id}"


@dataclass(frozen=True)
class ProbeResult:
    host: str
    agents: tuple[AgentRecord, ...]
    success: bool
    error: str = ""


@dataclass(frozen=True)
class SendResult:
    agent: AgentRecord
    success: bool
    error: str = ""
    submission_unknown: bool = False


def submission_failure_is_uncertain(
    result: subprocess.CompletedProcess[str], *, remote: bool = False,
) -> bool:
    detail = result.stderr.strip().lower()
    return (
        result.returncode < 0
        or (remote and result.returncode == 255)
        or detail in {"cancelled", "output_limit_exceeded"}
        or "timed out after" in detail
    )


def decode_json_object(value: str) -> dict[str, Any]:
    try:
        decoded = json.loads(value)
    except (json.JSONDecodeError, TypeError, RecursionError):
        return {}
    return decoded if isinstance(decoded, dict) else {}


def herdr_executable(environment: Mapping[str, str] | None = None) -> str:
    values = os.environ if environment is None else environment
    recorded = values.get("HERDR_BIN_PATH") or ""
    # A server that outlives a binary upgrade reports its own path with this suffix.
    if recorded.endswith(DELETED_BINARY_SUFFIX):
        recorded = recorded[: -len(DELETED_BINARY_SUFFIX)]
    if recorded and os.path.isfile(recorded) and os.access(recorded, os.X_OK):
        return recorded
    return "herdr"


def local_host_name() -> str:
    return socket.gethostname().split(".", 1)[0] or "local"


def agent_label(raw_agent: Mapping[str, Any]) -> tuple[str, str]:
    """Return the visible label and a current Herdr routing target."""

    registered_name = str(
        raw_agent.get("name") or raw_agent.get("agent_name") or ""
    )
    pane_id = str(raw_agent.get("pane_id") or "")
    if registered_name:
        return registered_name, registered_name

    tokens = raw_agent.get("tokens")
    alias = str(tokens.get("alias") or "") if isinstance(tokens, dict) else ""
    display_agent = str(raw_agent.get("display_agent") or "")
    alias_is_displayed = display_agent == alias or display_agent.endswith(f" {alias}")
    if re.fullmatch(r"[a-z]+-[a-z]+", alias) and alias_is_displayed:
        # Display metadata can outlive Herdr's registered name. Keep the label
        # addressable while routing by the pane ID that belongs to this snapshot.
        return alias, pane_id
    return "", pane_id


def _agent_revision(value: Any) -> int:
    if value is None or value == "":
        return 0
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValueError("invalid agent revision")
    revision = int(value)
    if revision < 0:
        raise ValueError("invalid agent revision")
    return revision


def parse_agent_payload(
    payload: Mapping[str, Any],
    *,
    host: str,
    local: bool,
) -> list[AgentRecord]:
    result_data = payload.get("result")
    if not isinstance(result_data, dict):
        return []
    snapshot = result_data.get("snapshot")
    container = snapshot if isinstance(snapshot, dict) else result_data
    raw_agents = container.get("agents")
    if not isinstance(raw_agents, list):
        return []

    workspace_metadata: dict[str, tuple[str, bool]] = {}
    raw_workspaces = container.get("workspaces")
    if isinstance(raw_workspaces, list):
        for raw_workspace in raw_workspaces:
            if not isinstance(raw_workspace, dict):
                continue
            workspace_id = str(raw_workspace.get("workspace_id") or "")
            if not workspace_id:
                continue
            worktree = raw_workspace.get("worktree")
            workspace_metadata[workspace_id] = (
                str(raw_workspace.get("label") or workspace_id),
                isinstance(worktree, dict)
                and bool(worktree.get("is_linked_worktree")),
            )

    pane_focus: dict[str, bool] = {}
    raw_panes = container.get("panes")
    if isinstance(raw_panes, list):
        for raw_pane in raw_panes:
            if isinstance(raw_pane, dict) and isinstance(raw_pane.get("focused"), bool):
                pane_focus[str(raw_pane.get("pane_id") or "")] = raw_pane["focused"]
    focused_pane_id = container.get("focused_pane_id")

    records: list[AgentRecord] = []
    for raw_agent in raw_agents:
        if not isinstance(raw_agent, dict):
            continue
        pane_id = str(raw_agent.get("pane_id") or "")
        agent_kind = str(raw_agent.get("agent") or "")
        if not pane_id or not agent_kind:
            continue
        name, route_target = agent_label(raw_agent)
        cwd = str(raw_agent.get("cwd") or "")
        workspace_id = str(raw_agent.get("workspace_id") or "")
        fallback_label = Path(cwd).name if cwd else workspace_id
        workspace_label, workspace_is_worktree = workspace_metadata.get(
            workspace_id,
            (fallback_label, False),
        )
        session_data = raw_agent.get("agent_session")
        session_id = ""
        if isinstance(session_data, dict):
            session_id = str(session_data.get("value") or "")
        records.append(
            AgentRecord(
                host=host,
                name=name,
                pane_id=pane_id,
                workspace_id=workspace_id,
                workspace_label=workspace_label,
                status=str(raw_agent.get("agent_status") or "unknown"),
                session_id=session_id,
                cwd=cwd,
                local=local,
                revision=_agent_revision(raw_agent.get("revision")),
                agent_kind=agent_kind,
                terminal_id=str(raw_agent.get("terminal_id") or ""),
                workspace_is_worktree=workspace_is_worktree,
                route_target=route_target,
                focused=pane_focus.get(pane_id, pane_id == focused_pane_id),
            )
        )
    return records


def is_agent_list_payload(payload: Mapping[str, Any]) -> bool:
    result_data = payload.get("result")
    if not isinstance(result_data, dict):
        return False
    snapshot = result_data.get("snapshot")
    container = snapshot if isinstance(snapshot, dict) else result_data
    return isinstance(container.get("agents"), list)


def _probe_payload(payload: Mapping[str, Any], *, host: str, local: bool) -> ProbeResult:
    if not is_agent_list_payload(payload):
        return ProbeResult(host, (), False, "invalid_response")
    try:
        agents = tuple(parse_agent_payload(payload, host=host, local=local))
    except (TypeError, ValueError, OverflowError):
        return ProbeResult(host, (), False, "invalid_response")
    return ProbeResult(host, agents, True)


class OutputLimitExceeded(Exception):
    pass


class CommandCancelled(Exception):
    pass


def _communicate_bounded(
    process: subprocess.Popen[bytes],
    *,
    timeout: float,
    output_limit: int = MAX_CAPTURE_BYTES,
    cancel_event: threading.Event | None = None,
) -> tuple[str, str]:
    selector = selectors.DefaultSelector()
    buffers = {"stdout": bytearray(), "stderr": bytearray()}
    streams = {"stdout": process.stdout, "stderr": process.stderr}
    for name, stream in streams.items():
        if stream is not None:
            selector.register(stream, selectors.EVENT_READ, name)
    deadline = time.monotonic() + timeout
    try:
        while selector.get_map():
            if cancel_event is not None and cancel_event.is_set():
                raise CommandCancelled
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(process.args, timeout)
            events = selector.select(min(remaining, 0.1))
            if not events:
                continue
            for key, _mask in events:
                try:
                    chunk = os.read(key.fd, 65536)
                except OSError:
                    chunk = b""
                if not chunk:
                    with suppress(Exception):
                        selector.unregister(key.fileobj)
                    continue
                buffers[key.data].extend(chunk)
                if sum(len(buffer) for buffer in buffers.values()) > output_limit:
                    raise OutputLimitExceeded
        # Closing both pipes does not mean the child has exited. Continue to
        # honor cancellation while it finishes without producing more output.
        while process.poll() is None:
            if cancel_event is not None and cancel_event.is_set():
                raise CommandCancelled
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(process.args, timeout)
            try:
                process.wait(timeout=min(remaining, 0.1))
            except subprocess.TimeoutExpired:
                continue
    finally:
        selector.close()
        for stream in streams.values():
            if stream is not None:
                with suppress(OSError):
                    stream.close()
    return (
        buffers["stdout"].decode("utf-8", errors="replace"),
        buffers["stderr"].decode("utf-8", errors="replace"),
    )


def _run_command(
    command: Sequence[str],
    *,
    timeout: float = REMOTE_DISCOVERY_TIMEOUT_SECONDS,
    cancel_event: threading.Event | None = None,
    environment: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    if cancel_event is not None and cancel_event.is_set():
        return subprocess.CompletedProcess(list(command), 1, "", "cancelled")
    try:
        process = subprocess.Popen(
            list(command),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=None if environment is None else dict(environment),
        )
    except OSError as error:
        return subprocess.CompletedProcess(list(command), 1, "", str(error))
    try:
        stdout, stderr = _communicate_bounded(
            process,
            timeout=timeout,
            cancel_event=cancel_event,
        )
    except subprocess.TimeoutExpired as error:
        process.kill()
        process.wait()
        return subprocess.CompletedProcess(list(command), 1, "", str(error))
    except OutputLimitExceeded:
        process.kill()
        process.wait()
        return subprocess.CompletedProcess(
            list(command),
            1,
            "",
            "output_limit_exceeded",
        )
    except CommandCancelled:
        process.kill()
        process.wait()
        return subprocess.CompletedProcess(list(command), 1, "", "cancelled")
    return subprocess.CompletedProcess(list(command), process.returncode, stdout, stderr)


def run_bounded_command(
    command: Sequence[str],
    *,
    timeout: float = REMOTE_DISCOVERY_TIMEOUT_SECONDS,
    cancel_event: threading.Event | None = None,
    environment: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a subprocess with the directory's timeout, cancellation, and output cap."""

    return _run_command(command, timeout=timeout, cancel_event=cancel_event,
                        environment=environment)


def query_local_agents(
    environment: Mapping[str, str] | None = None,
    *,
    cancel_event: threading.Event | None = None,
) -> list[AgentRecord]:
    """Return local agents, preserving the GUI's empty-on-failure behavior."""

    return list(
        probe_local_agents(
            environment,
            cancel_event=cancel_event,
        ).agents
    )


def probe_local_agents(
    environment: Mapping[str, str] | None = None,
    *,
    cancel_event: threading.Event | None = None,
) -> ProbeResult:
    """Query the local snapshot without conflating failure with no agents."""

    host = local_host_name()
    command = [herdr_executable(environment), "api", "snapshot"]
    result = _run_command(command, cancel_event=cancel_event)
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "unavailable"
        return ProbeResult(host, (), False, detail)
    payload = decode_json_object(result.stdout)
    return _probe_payload(payload, host=host, local=True)


def fetch_local_agent(
    pane_id: str,
    environment: Mapping[str, str] | None = None,
    *,
    cancel_event: threading.Event | None = None,
) -> AgentRecord | None:
    probe = probe_local_agents(environment, cancel_event=cancel_event)
    return next(
        (record for record in probe.agents if record.pane_id == pane_id),
        None,
    )


def ssh_config_path(environment: Mapping[str, str] | None = None) -> Path:
    values = os.environ if environment is None else environment
    configured = values.get(SSH_CONFIG_ENV) or values.get(LEGACY_SSH_CONFIG_ENV)
    return Path(configured).expanduser() if configured else Path.home() / ".ssh" / "config"


def ssh_hosts_allowlist_path(
    environment: Mapping[str, str] | None = None,
) -> Path:
    values = os.environ if environment is None else environment
    configured = values.get(SSH_HOSTS_FILE_ENV) or values.get(
        LEGACY_SSH_HOSTS_FILE_ENV
    )
    if configured:
        return Path(configured).expanduser()
    plugin_config = values.get("HERDR_PLUGIN_CONFIG_DIR")
    config_directory = (
        Path(plugin_config)
        if plugin_config
        else Path.home()
        / ".config"
        / "herdr"
        / "plugins"
        / "config"
        / PLUGIN_ID
    )
    current = config_directory / "ssh-hosts"
    if current.exists():
        return current
    legacy = config_directory.with_name(LEGACY_PLUGIN_ID) / "ssh-hosts"
    return legacy if legacy.exists() else current


def parse_ssh_hosts_allowlist(path: Path) -> list[str] | None:
    """Return an ordered host allowlist, or None when no allowlist exists.

    An existing but unreadable file intentionally disables remote discovery. It
    is safer than silently expanding the scope back to every SSH alias.
    """

    try:
        lines = path.expanduser().read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return None
    except (OSError, UnicodeError):
        return []
    hosts: list[str] = []
    for line in lines:
        try:
            fields = shlex.split(line, comments=True)
        except ValueError:
            continue
        for host in fields:
            if host not in hosts:
                hosts.append(host)
    return hosts


def parse_ssh_hosts(
    path: Path,
    *,
    _visited: set[Path] | None = None,
) -> list[str]:
    visited = set() if _visited is None else _visited
    try:
        resolved_path = path.expanduser().resolve()
    except OSError:
        resolved_path = path.expanduser().absolute()
    if resolved_path in visited:
        return []
    visited.add(resolved_path)
    try:
        lines = resolved_path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return []

    hosts: list[str] = []
    seen: set[str] = set()
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        try:
            fields = shlex.split(stripped, comments=True)
        except ValueError:
            continue
        if not fields:
            continue
        keyword = fields[0].lower()
        if keyword == "include":
            for pattern in fields[1:]:
                include_path = Path(pattern).expanduser()
                if not include_path.is_absolute():
                    include_path = resolved_path.parent / include_path
                for matched_path in sorted(glob.glob(os.fspath(include_path))):
                    for host in parse_ssh_hosts(Path(matched_path), _visited=visited):
                        if host not in seen:
                            seen.add(host)
                            hosts.append(host)
            continue
        if keyword != "host":
            continue
        for host in fields[1:]:
            if host.startswith("-") or any(character in host for character in "*!?"):
                continue
            if host not in seen:
                seen.add(host)
                hosts.append(host)
    return hosts


def ssh_hosts(environment: Mapping[str, str] | None = None) -> list[str]:
    configured_hosts = parse_ssh_hosts(ssh_config_path(environment))
    allowlist = parse_ssh_hosts_allowlist(ssh_hosts_allowlist_path(environment))
    if allowlist is None:
        return configured_hosts
    allowed = set(allowlist)
    return [host for host in configured_hosts if host in allowed]


def _remote_herdr_command(arguments: Sequence[str], *, remote_socket: str | None = None) -> str:
    quoted_arguments = " ".join(shlex.quote(argument) for argument in arguments)
    socket_override = (
        f"export HERDR_SOCKET_PATH={shlex.quote(remote_socket)}; "
        if remote_socket else ""
    )
    script = (
        "if command -v herdr >/dev/null 2>&1; then herdr_bin=$(command -v herdr); "
        "elif [ -x \"$HOME/.local/bin/herdr\" ]; then herdr_bin=\"$HOME/.local/bin/herdr\"; "
        "elif [ -x /opt/homebrew/bin/herdr ]; then herdr_bin=/opt/homebrew/bin/herdr; "
        "elif [ -x /usr/local/bin/herdr ]; then herdr_bin=/usr/local/bin/herdr; "
        "elif [ -x \"$HOME/.local/share/mise/shims/herdr\" ]; then "
        "herdr_bin=\"$HOME/.local/share/mise/shims/herdr\"; "
        "else exit 127; fi; "
        f"{socket_override}exec \"$herdr_bin\" {quoted_arguments}"
    )
    return f"sh -c {shlex.quote(script)}"


def _ssh_transport_command(
    host: str,
    remote_command: str,
    *,
    config_path: Path,
) -> list[str]:
    """Build the shared hardened SSH transport without changing trust policy."""

    return [
        "ssh",
        "-F",
        os.fspath(config_path),
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectionAttempts=1",
        "-o",
        f"ConnectTimeout={REMOTE_CONNECT_TIMEOUT_SECONDS}",
        "-o",
        "ClearAllForwardings=yes",
        "-o",
        "ForwardAgent=no",
        "-o",
        "ForwardX11=no",
        "-o",
        "PermitLocalCommand=no",
        "-T",
        host,
        remote_command,
    ]


def ssh_command(
    host: str,
    arguments: Sequence[str],
    *,
    config_path: Path,
    remote_socket: str | None = None,
) -> list[str]:
    return _ssh_transport_command(
        host,
        _remote_herdr_command(arguments, remote_socket=remote_socket),
        config_path=config_path,
    )


def ssh_program_command(
    host: str,
    arguments: Sequence[str],
    *,
    config_path: Path,
    cwd: str | None = None,
) -> list[str]:
    """Run an argv-style program over the same hardened SSH transport."""

    if not arguments:
        raise ValueError("remote program arguments must not be empty")
    command = "exec " + shlex.join(arguments)
    if cwd is not None:
        command = f"cd -- {shlex.quote(cwd)} && {command}"
    return _ssh_transport_command(
        host,
        f"sh -c {shlex.quote(command)}",
        config_path=config_path,
    )


def query_remote_agents(
    host: str,
    *,
    config_path: Path,
    timeout: float = REMOTE_DISCOVERY_TIMEOUT_SECONDS,
    cancel_event: threading.Event | None = None,
    environment: Mapping[str, str] | None = None,
    remote_sockets: Mapping[str, str] | None = None,
) -> ProbeResult:
    result = _run_command(
        ssh_command(
            host,
            ["api", "snapshot"],
            config_path=config_path,
            remote_socket=(remote_sockets or {}).get(host) or None,
        ),
        timeout=timeout,
        cancel_event=cancel_event,
        environment=environment,
    )
    if result.returncode != 0:
        return ProbeResult(host, (), False, result.stderr.strip() or "unavailable")
    payload = extract_agent_list_payload(result.stdout)
    return _probe_payload(payload, host=host, local=False)


def extract_agent_list_payload(value: str) -> dict[str, Any]:
    payload = decode_json_object(value)
    if is_agent_list_payload(payload):
        return payload
    for line in reversed(value.splitlines()):
        payload = decode_json_object(line)
        if is_agent_list_payload(payload):
            return payload
    return {}


class AgentCache:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.data = self._load()

    @classmethod
    def from_environment(
        cls,
        environment: Mapping[str, str] | None = None,
    ) -> "AgentCache":
        values = os.environ if environment is None else environment
        configured = values.get("HERDR_PLUGIN_STATE_DIR")
        if configured:
            state_dir = Path(configured)
            current = state_dir / "agent-directory.json"
            legacy = state_dir.with_name(LEGACY_PLUGIN_ID) / "agent-directory.json"
            if (
                state_dir.name == PLUGIN_ID
                and not current.exists()
                and legacy.exists()
            ):
                state_dir = legacy.parent
        else:
            state_root = Path.home() / ".local" / "state"
            state_dir = state_root / "herdr-agent-messenger"
            legacy = state_root / "herdr-agent-labels"
            if not state_dir.exists() and legacy.exists():
                state_dir = legacy
        return cls(state_dir / "agent-directory.json")

    def _load(self) -> dict[str, Any]:
        try:
            decoded = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError, RecursionError):
            return {"version": 1, "hosts": {}}
        if not isinstance(decoded, dict) or not isinstance(decoded.get("hosts"), dict):
            return {"version": 1, "hosts": {}}
        return decoded

    def host_entry(self, host: str) -> dict[str, Any]:
        hosts = self.data.get("hosts")
        if not isinstance(hosts, dict):
            return {}
        entry = hosts.get(host)
        return entry if isinstance(entry, dict) else {}

    def is_fresh(self, host: str, now: float | None = None) -> bool:
        entry = self.host_entry(host)
        updated_at = entry.get("updated_at")
        if not isinstance(updated_at, (int, float)):
            return False
        ttl = REMOTE_CACHE_TTL_SECONDS if entry.get("reachable") else REMOTE_FAILURE_TTL_SECONDS
        age = (time.time() if now is None else now) - float(updated_at)
        return 0 <= age <= ttl

    def agents(self, host: str, *, stale: bool = False) -> list[AgentRecord]:
        entry = self.host_entry(host)
        raw_agents = entry.get("agents")
        if not isinstance(raw_agents, list):
            return []
        records: list[AgentRecord] = []
        fields = set(AgentRecord.__dataclass_fields__)
        for raw_agent in raw_agents:
            if not isinstance(raw_agent, dict):
                continue
            try:
                values = {key: raw_agent[key] for key in fields if key in raw_agent}
                record = AgentRecord(**values)
            except (KeyError, TypeError):
                continue
            if (
                any(
                    not isinstance(getattr(record, field), str)
                    for field in (
                        "host", "name", "pane_id", "workspace_id", "workspace_label",
                        "status", "session_id", "cwd", "agent_kind", "terminal_id", "route_target",
                    )
                )
                or record.host != host
                or record.local is not False
                or type(record.revision) is not int
                or record.revision < 0
                or type(record.workspace_is_worktree) is not bool
                or type(record.focused) is not bool
            ):
                continue
            records.append(replace(record, stale=stale))
        return records

    def update(self, result: ProbeResult) -> None:
        hosts = self.data.setdefault("hosts", {})
        if not isinstance(hosts, dict):
            hosts = {}
            self.data["hosts"] = hosts
        existing = self.host_entry(result.host)
        existing_agents = existing.get("agents")
        if result.success:
            serialized_agents = [
                asdict(replace(agent, stale=False)) for agent in result.agents
            ]
        elif isinstance(existing_agents, list):
            serialized_agents = existing_agents
        else:
            serialized_agents = []
        hosts[result.host] = {
            "updated_at": time.time(),
            "reachable": result.success,
            "error": result.error,
            "agents": serialized_agents,
        }

    def save(self) -> None:
        temporary_path: Path | None = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                dir=self.path.parent,
                prefix=f".{self.path.name}.",
                delete=False,
            ) as temporary:
                json.dump(self.data, temporary, ensure_ascii=False, indent=2)
                temporary.write("\n")
                temporary_path = Path(temporary.name)
            temporary_path.chmod(0o600)
            os.replace(temporary_path, self.path)
        except OSError:
            pass
        finally:
            if temporary_path and temporary_path.exists():
                with suppress(OSError):
                    temporary_path.unlink()


class RemoteDiscovery:
    """Run bounded SSH probes without blocking the popup input loop."""

    def __init__(
        self,
        hosts: Iterable[str],
        *,
        config_path: Path,
        timeout: float = REMOTE_DISCOVERY_TIMEOUT_SECONDS,
    ) -> None:
        self.hosts = tuple(hosts)
        self.config_path = config_path
        self.timeout = timeout
        self.results: queue.Queue[ProbeResult] = queue.Queue()
        self._pending_hosts: queue.Queue[str] = queue.Queue()
        for host in self.hosts:
            self._pending_hosts.put(host)
        self.deadline = 0.0
        self._cancelled = threading.Event()
        self._processes: set[subprocess.Popen[str]] = set()
        self._process_lock = threading.Lock()

    def start(self) -> None:
        self.deadline = time.monotonic() + self.timeout
        worker_count = min(MAX_REMOTE_WORKERS, len(self.hosts))
        for _index in range(worker_count):
            threading.Thread(target=self._worker, daemon=True).start()

    def _worker(self) -> None:
        while not self._cancelled.is_set():
            try:
                host = self._pending_hosts.get_nowait()
            except queue.Empty:
                return
            self._probe_host(host)

    def _probe_host(self, host: str) -> None:
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            self.results.put(ProbeResult(host, (), False, "timeout"))
            return
        process: subprocess.Popen[str] | None = None
        try:
            if self._cancelled.is_set():
                self.results.put(ProbeResult(host, (), False, "cancelled"))
                return
            command = ssh_command(host, ["api", "snapshot"], config_path=self.config_path)
            try:
                process = subprocess.Popen(
                    command,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
            except OSError as error:
                self.results.put(ProbeResult(host, (), False, str(error)))
                return
            with self._process_lock:
                self._processes.add(process)
            if self._cancelled.is_set():
                process.kill()
            try:
                stdout, stderr = _communicate_bounded(
                    process,
                    timeout=max(0.1, self.deadline - time.monotonic()),
                    cancel_event=self._cancelled,
                )
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
                self.results.put(ProbeResult(host, (), False, "timeout"))
                return
            except OutputLimitExceeded:
                process.kill()
                process.wait()
                self.results.put(
                    ProbeResult(host, (), False, "output_limit_exceeded")
                )
                return
            except CommandCancelled:
                process.kill()
                process.wait()
                self.results.put(ProbeResult(host, (), False, "cancelled"))
                return
            if self._cancelled.is_set():
                self.results.put(ProbeResult(host, (), False, "cancelled"))
                return
            if process.returncode != 0:
                self.results.put(ProbeResult(host, (), False, stderr.strip() or "unavailable"))
                return
            payload = extract_agent_list_payload(stdout)
            self.results.put(_probe_payload(payload, host=host, local=False))
        finally:
            if process is not None:
                with self._process_lock:
                    self._processes.discard(process)

    def cancel(self) -> None:
        self._cancelled.set()
        with self._process_lock:
            processes = list(self._processes)
        for process in processes:
            if process.poll() is None:
                process.terminate()
        for process in processes:
            if process.poll() is None:
                process.kill()

    def poll(self) -> list[ProbeResult]:
        items: list[ProbeResult] = []
        while True:
            try:
                items.append(self.results.get_nowait())
            except queue.Empty:
                return items


def _matching_agent(selected: AgentRecord, current: Sequence[AgentRecord]) -> AgentRecord | None:
    if selected.session_id:
        matches = [candidate for candidate in current if candidate.session_id == selected.session_id]
        return matches[0] if len(matches) == 1 else None
    if selected.revision <= 0:
        return None
    matches = [
        candidate for candidate in current
        if (
            candidate.pane_id == selected.pane_id
            and candidate.target == selected.target
            and candidate.revision == selected.revision
            and candidate.agent_kind == selected.agent_kind
            and candidate.terminal_id == selected.terminal_id
            and candidate.cwd == selected.cwd
        )
    ]
    return matches[0] if len(matches) == 1 else None


def _run_bounded_tasks(
    tasks: Sequence[Callable[[], TaskResult]],
    *,
    max_workers: int,
    cancel_event: threading.Event | None = None,
) -> list[TaskResult]:
    if not tasks:
        return []
    pending: queue.Queue[Callable[[], TaskResult]] = queue.Queue()
    for task in tasks:
        pending.put(task)
    results: list[TaskResult] = []
    errors: list[Exception] = []
    result_lock = threading.Lock()

    def worker() -> None:
        while True:
            if cancel_event is not None and cancel_event.is_set():
                return
            try:
                task = pending.get_nowait()
            except queue.Empty:
                return
            try:
                result = task()
            except Exception as error:  # Propagate after all bounded workers exit.
                with result_lock:
                    errors.append(error)
            else:
                with result_lock:
                    results.append(result)

    threads = [
        threading.Thread(target=worker, daemon=True)
        for _index in range(min(max_workers, len(tasks)))
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    if errors:
        raise errors[0]
    return results


def _send_local_prompt(
    target: AgentRecord,
    prompt: str,
    environment: Mapping[str, str] | None,
    cancel_event: threading.Event | None = None,
) -> SendResult:
    result = _run_command(
        [herdr_executable(environment), "agent", "prompt", target.target, prompt],
        cancel_event=cancel_event,
        environment=environment,
    )
    return SendResult(
        target, result.returncode == 0, result.stderr.strip(),
        submission_unknown=result.returncode != 0 and submission_failure_is_uncertain(result),
    )


def _send_remote_prompt(
    target: AgentRecord,
    prompt: str,
    *,
    config_path: Path,
    cancel_event: threading.Event | None = None,
    environment: Mapping[str, str] | None = None,
    remote_socket: str | None = None,
) -> SendResult:
    result = _run_command(
        ssh_command(
            target.host,
            ["agent", "prompt", target.target, prompt],
            config_path=config_path,
            remote_socket=remote_socket,
        ),
        cancel_event=cancel_event,
        environment=environment,
    )
    return SendResult(
        target, result.returncode == 0, result.stderr.strip(),
        submission_unknown=result.returncode != 0 and submission_failure_is_uncertain(result, remote=True),
    )


def user_prompt_text(message: str) -> str:
    """Render the exact persisted user-origin text sent by direct dispatch."""
    return f"Message from user (HAM):\n\n{message}"


def _dispatch_host_group(
    host: str,
    targets: Sequence[AgentRecord],
    *,
    sender: AgentRecord | None,
    message: str,
    config_path: Path,
    environment: Mapping[str, str] | None,
    cancel_event: threading.Event | None,
    remote_sockets: Mapping[str, str] | None,
) -> list[SendResult]:
    if cancel_event is not None and cancel_event.is_set():
        return []
    if targets[0].local:
        current = query_local_agents(environment, cancel_event=cancel_event)
    else:
        probe = query_remote_agents(
            host,
            config_path=config_path,
            cancel_event=cancel_event,
            environment=environment,
            remote_sockets=remote_sockets,
        )
        if not probe.success:
            return [SendResult(target, False, "host_unavailable") for target in targets]
        current = list(probe.agents)

    prompt = user_prompt_text(message) if sender is None else (
        f"Message from {sender.qualified_name}:\n\n{message}"
    )
    remote_socket = (remote_sockets or {}).get(host) or None
    results: list[SendResult] = []
    send_tasks: list[Callable[[], SendResult]] = []
    for selected in targets:
        verified = _matching_agent(selected, current)
        if verified is None:
            results.append(SendResult(selected, False, "agent_unavailable"))
            continue
        if selected.local:
            send_tasks.append(
                lambda target=verified: _send_local_prompt(
                    target, prompt, environment, cancel_event,
                )
            )
        else:
            send_tasks.append(
                lambda target=verified: _send_remote_prompt(
                    target,
                    prompt,
                    config_path=config_path,
                    cancel_event=cancel_event,
                    environment=environment,
                    remote_socket=remote_socket,
                )
            )
    results.extend(
        _run_bounded_tasks(
            send_tasks,
            max_workers=MAX_SEND_WORKERS,
            cancel_event=cancel_event,
        )
    )
    return results


def _dispatch_prompt_groups(
    sender: AgentRecord | None,
    recipients: Sequence[AgentRecord],
    message: str,
    *,
    config_path: Path,
    environment: Mapping[str, str] | None,
    cancel_event: threading.Event | None,
    remote_sockets: Mapping[str, str] | None,
) -> list[SendResult]:
    groups: dict[tuple[bool, str], list[AgentRecord]] = {}
    for recipient in recipients:
        groups.setdefault((recipient.local, recipient.host), []).append(recipient)

    tasks: list[Callable[[], list[SendResult]]] = []
    for (_local, host), targets in groups.items():
        tasks.append(
            lambda host=host, targets=targets: _dispatch_host_group(
                host,
                targets,
                sender=sender,
                message=message,
                config_path=config_path,
                environment=environment,
                cancel_event=cancel_event,
                remote_sockets=remote_sockets,
            )
        )
    results: list[SendResult] = []
    for group_results in _run_bounded_tasks(
        tasks,
        max_workers=4,
        cancel_event=cancel_event,
    ):
        results.extend(group_results)
    return results


def dispatch_prompts(
    sender: AgentRecord,
    recipients: Sequence[AgentRecord],
    message: str,
    *,
    config_path: Path,
    environment: Mapping[str, str] | None = None,
    cancel_event: threading.Event | None = None,
) -> list[SendResult]:
    return _dispatch_prompt_groups(
        sender,
        recipients,
        message,
        config_path=config_path,
        environment=environment,
        cancel_event=cancel_event,
        remote_sockets=None,
    )


def dispatch_user_prompts(
    recipients: Sequence[AgentRecord],
    message: str,
    *,
    config_path: Path,
    environment: Mapping[str, str] | None = None,
    cancel_event: threading.Event | None = None,
    remote_sockets: Mapping[str, str] | None = None,
) -> list[SendResult]:
    """Send a user-origin message through the same verified Herdr transport."""
    return _dispatch_prompt_groups(
        None,
        recipients,
        message,
        config_path=config_path,
        environment=environment,
        cancel_event=cancel_event,
        remote_sockets=remote_sockets,
    )
