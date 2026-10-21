"""Read-only, host-local listener discovery and HAM resource attribution."""

from __future__ import annotations

from datetime import datetime
import ipaddress
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import socket
import subprocess
import sys
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import urlsplit


class ResourceScanError(RuntimeError):
    """A discovery source is unavailable; callers may still show other sources."""


_MAC_TAILSCALE = "/Applications/Tailscale.app/Contents/MacOS/Tailscale"
_RUNTIME_NAMES = {"omp", "claude", "codex"}


def _command(runner: Callable, command: list[str], *, empty_exit: bool = False) -> str:
    try:
        result = runner(command, capture_output=True, text=True, errors="replace", timeout=2,
                        check=False, env={**os.environ, "LC_ALL": "C"})
    except (OSError, subprocess.SubprocessError) as error:
        raise ResourceScanError(f"{command[0]}: {error}") from None
    if result.returncode and not (empty_exit and result.returncode == 1
                                  and not result.stdout.strip() and not result.stderr.strip()):
        raise ResourceScanError(f"{command[0]}: {result.stderr.strip() or 'exit ' + str(result.returncode)}")
    return result.stdout


def _optional_command(runner: Callable, command: list[str]) -> str:
    try:
        return _command(runner, command)
    except ResourceScanError:
        # Processes may disappear or deny access between discovery and metadata reads.
        return ""


def _endpoint(value: str) -> tuple[str, int] | None:
    bind, separator, port = value.rpartition(":")
    if not separator or not port.isdigit() or not 0 < int(port) <= 65535:
        return None
    return bind.strip("[]"), int(port)


def _deduplicate(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    unique: dict[tuple[int, int], dict[str, Any]] = {}
    for row in rows:
        key = (row["pid"], row["port"])
        previous = unique.get(key)
        if previous is None:
            unique[key] = {**row, "binds": [row["bind"]]}
        elif row["bind"] not in previous["binds"]:
            previous["binds"].append(row["bind"])
    for row in unique.values():
        row["binds"].sort(key=_bind_rank)
        row["bind"] = row["binds"][0]
    return sorted(unique.values(), key=lambda row: (row["port"], row["pid"]))


def _bind_rank(bind: str) -> tuple[int, str]:
    if bind in {"*", "0.0.0.0", "::"}:
        return 0, bind
    return (1 if _loopback(bind) else 2), bind


def _read_text(path: Path) -> str:
    try:
        return path.read_text(errors="replace")
    except OSError:
        return ""


def _linux_listeners(runner: Callable, proc_root: Path,
                     listener_endpoints: list[dict[str, Any]]) -> list[dict[str, Any]]:
    output = _command(runner, ["ss", "-ltnpH"])
    boot = next((float(line.split()[1]) for line in _read_text(proc_root / "stat").splitlines()
                 if line.startswith("btime ")), None)
    ticks = os.sysconf("SC_CLK_TCK")
    metadata: dict[int, dict[str, Any] | None] = {}
    rows = []
    for line in output.splitlines():
        fields = line.split()
        endpoint = _endpoint(fields[3]) if len(fields) >= 5 else None
        if endpoint is None:
            continue
        bind, port = endpoint
        listener_endpoints.append({"bind": bind, "port": port})
        for match in re.finditer(r'\("([^"\n]*)",pid=(\d+),', line):
            process, pid_text = match.groups()
            pid = int(pid_text)
            if pid not in metadata:
                proc = proc_root / str(pid)
                try:
                    owned = proc.stat().st_uid == os.getuid()
                except OSError:
                    owned = False
                if not owned:
                    metadata[pid] = None
                else:
                    env = dict(token.split("=", 1) for token in _read_text(proc / "environ").split("\0")
                               if "=" in token)
                    try:
                        cwd = str((proc / "cwd").readlink())
                    except OSError:
                        cwd = None
                    stat = _read_text(proc / "stat")
                    stat_fields = stat.rpartition(")")[2].split()
                    try:
                        started_at = boot + int(stat_fields[19]) / ticks if boot is not None else None
                    except (ValueError, IndexError):
                        started_at = None
                    metadata[pid] = {
                        "argv": _read_text(proc / "cmdline").replace("\0", " ").strip(),
                        "cwd": cwd, "pane_id": env.get("HERDR_PANE_ID"),
                        "plugin_id": env.get("HERDR_PLUGIN_ID"), "started_at": started_at,
                    }
            if metadata[pid] is not None:
                bind, port = endpoint
                rows.append({"pid": pid, "port": port, "bind": bind,
                             "process": process, **metadata[pid]})
    return _deduplicate(rows)


def _mac_start(value: str) -> float | None:
    # ps runs in the C locale, independently of this process's mutable LC_TIME.
    # strptime's English weekday/month directives instead follow global locale.
    parts = value.split()
    months = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
              "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
    try:
        if len(parts) != 5 or parts[0] not in ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"):
            return None
        hour, minute, second = map(int, parts[3].split(":"))
        return datetime(int(parts[4]), months.index(parts[1]) + 1, int(parts[2]),
                        hour, minute, second).timestamp()
    except (ValueError, OverflowError, OSError):
        return None


def _mac_ps(output: str) -> dict[int, tuple[str, str]]:
    rows = {}
    for line in output.splitlines():
        # lstart is five whitespace-delimited fields; commands may contain spaces.
        fields = line.split(None, 6)
        if len(fields) == 7 and fields[0].isdigit():
            rows[int(fields[0])] = (" ".join(fields[1:6]), fields[6])
    return rows


def _mac_listeners(runner: Callable, listener_endpoints: list[dict[str, Any]]) -> list[dict[str, Any]]:
    output = _command(runner, ["lsof", "-nP", "-iTCP", "-sTCP:LISTEN", "-F", "pucn"],
                      empty_exit=True)
    rows = []
    pid, uid, process = None, None, ""
    for line in output.splitlines():
        if line.startswith("p") and line[1:].isdigit():
            pid, uid, process = int(line[1:]), None, ""
        elif line.startswith("u") and line[1:].isdigit():
            uid = int(line[1:])
        elif line.startswith("c"):
            process = line[1:]
        elif line.startswith("n") and pid is not None:
            endpoint = _endpoint(line[1:])
            if endpoint is None:
                continue
            bind, port = endpoint
            listener_endpoints.append({"bind": bind, "port": port})
            if uid == os.getuid():
                rows.append({"pid": pid, "port": port, "bind": bind, "process": process})
    if not rows:
        return []
    pids = ",".join(str(pid) for pid in sorted({row["pid"] for row in rows}))
    # Keep plain argv separate: a HERDR_PANE_ID= token in argv is not environment.
    plain = _mac_ps(_optional_command(runner, ["ps", "-ww", "-p", pids, "-o", "pid=,lstart=,command="]))
    with_env = _mac_ps(_optional_command(runner, ["ps", "-E", "-ww", "-p", pids, "-o", "pid=,lstart=,command="]))
    cwd_output = _optional_command(runner, ["lsof", "-a", "-p", pids, "-d", "cwd", "-Fpn"])
    cwd_by_pid = {}
    pid = None
    for line in cwd_output.splitlines():
        if line.startswith("p") and line[1:].isdigit():
            pid = int(line[1:])
        elif line.startswith("n") and pid is not None:
            cwd_by_pid[pid] = line[1:]
    metadata = {}
    for row in rows:
        pid = row["pid"]
        if pid not in metadata:
            start, command = plain.get(pid, ("", ""))
            env_command = with_env.get(pid, ("", ""))[1]
            suffix = env_command[len(command):] if command and env_command.startswith(command) else ""
            env = dict(re.findall(r"(?:^|\s)(HERDR_PANE_ID|HERDR_PLUGIN_ID)=([^\s]*)(?=\s|$)", suffix))
            metadata[pid] = {"argv": command, "cwd": cwd_by_pid.get(pid),
                             "pane_id": env.get("HERDR_PANE_ID"), "plugin_id": env.get("HERDR_PLUGIN_ID"),
                             "started_at": _mac_start(start)}
        row.update(metadata[pid])
    return _deduplicate(rows)


def scan_listeners(*, runner: Callable = subprocess.run, platform: str | None = None,
                   now: Any = None, proc_root: str | Path = "/proc",
                   listener_endpoints: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Discover this user's listeners; unavailable discovery raises ResourceScanError.

    ``now`` is accepted consistently across the scan API; start times come from
    the OS, never from the time a board happens to be viewed. ``proc_root`` lets
    tests supply Linux procfs fixtures without touching the host's processes.
    ``listener_endpoints``, when supplied, also receives bare bind/port records
    for listeners without readable, current-user process ownership.
    """
    platform = sys.platform if platform is None else platform
    endpoints = [] if listener_endpoints is None else listener_endpoints
    if platform.startswith("linux"):
        return _linux_listeners(runner, Path(proc_root), endpoints)
    if platform == "darwin":
        return _mac_listeners(runner, endpoints)
    raise ResourceScanError(f"Listener discovery is unsupported on {platform}.")


def _loopback(host: str | None) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host or "").is_loopback
    except ValueError:
        return False


def scan_serve(*, runner: Callable = subprocess.run, platform: str | None = None,
               now: Any = None) -> list[dict[str, Any]]:
    """Read Tailscale Web handlers. A host without Tailscale has no routes."""
    platform = sys.platform if platform is None else platform
    binary = shutil.which("tailscale")
    if binary is None and platform == "darwin" and Path(_MAC_TAILSCALE).is_file():
        binary = _MAC_TAILSCALE
    if binary is None:
        return []
    output = _command(runner, [binary, "serve", "status", "--json"])
    try:
        config = json.loads(output)
        if not isinstance(config, dict) or not isinstance(config.get("Web", {}), dict):
            raise ValueError("expected Web object")
        routes = []
        for authority, web in config.get("Web", {}).items():
            parsed = urlsplit("https://" + authority)
            if not parsed.hostname:
                raise ValueError("missing serve host")
            authority = authority[:-4] if parsed.port == 443 and authority.endswith(":443") else authority
            for path, handler in web.get("Handlers", {}).items():
                if "Proxy" in handler:
                    target, kind = handler["Proxy"], "proxy"
                    backend = urlsplit(target if "://" in target else "http://" + target)
                    port = (backend.port or (443 if backend.scheme == "https" else 80)) if _loopback(backend.hostname) else None
                elif "Path" in handler:
                    target, kind, port = handler["Path"], "path", None
                else:
                    continue
                routes.append({"url": f"https://{authority}{path}", "port": port,
                               "path": path, "target": target, "kind": kind})
        return sorted(routes, key=lambda route: route["url"])
    except (ValueError, TypeError, AttributeError) as error:
        raise ResourceScanError(f"tailscale: invalid serve status: {error}") from None


def _excluded(listener: Mapping[str, Any], clients: set[str]) -> bool:
    process = os.path.basename(str(listener.get("process") or ""))
    try:
        argv = shlex.split(listener.get("argv") or "")
    except ValueError:
        argv = []
    executable = os.path.basename(argv[0]) if argv else ""
    return bool(listener.get("plugin_id") is not None or process in clients or executable in clients
                or process == "adb" or executable == "adb")


def _matches_backend(listener: Mapping[str, Any], route: Mapping[str, Any]) -> bool:
    if listener["port"] != route.get("port"):
        return False
    target = str(route.get("target") or "")
    backend = urlsplit(target if "://" in target else "http://" + target)
    host = backend.hostname
    if not _loopback(host):
        return False
    for bind in listener.get("binds") or [listener.get("bind", "")]:
        if host == "localhost":
            if _loopback(bind) or bind in {"*", "0.0.0.0", "::"}:
                return True
        elif bind in {"*", "::", host}:
            return True
        elif bind == "0.0.0.0" and ipaddress.ip_address(host).version == 4:
            return True
    return False


def attribute(listeners: Sequence[Mapping[str, Any]], serve_routes: Sequence[Mapping[str, Any]],
              agents: Sequence[Mapping[str, Any]], *, host: str | None = None,
              runner: Callable = subprocess.run, platform: str | None = None,
              now: Any = None, listener_endpoints: Sequence[Mapping[str, Any]] | None = None
              ) -> tuple[dict[str, list[dict[str, Any]]], dict[str, list[dict[str, Any]]]]:
    """Attribute pane-inherited servers only to active agents on the scanned host.

    Pass the ledger's canonical ``host`` when it differs from the OS hostname.
    Excluded plugin/runtime/adb listeners still prove a backend is alive, but
    never appear as an agent server or an unowned review server.
    ``listener_endpoints`` proves liveness independently of process ownership.
    """
    host = socket.gethostname() if host is None else host
    local_agents = [agent for agent in agents if agent.get("active") and agent.get("host") == host]
    by_pane = {agent["pane_id"]: agent for agent in sorted(local_agents, key=lambda row: row.get("first_seen", 0))
               if agent.get("pane_id")}
    clients = _RUNTIME_NAMES | {os.path.basename(str(agent["client"])) for agent in local_agents if agent.get("client")}
    per_agent: dict[str, list[dict[str, Any]]] = {}
    owners = {}
    for listener in listeners:
        if _excluded(listener, clients):
            continue
        agent = by_pane.get(listener.get("pane_id"))
        if agent is None:
            continue
        server = {key: listener.get(key) for key in ("port", "bind", "pid", "process", "cwd", "started_at")}
        started = listener.get("started_at")
        server["inherited"] = started is not None and started < agent["first_seen"]
        server["urls"] = sorted({route["url"] for route in serve_routes
                                 if route.get("kind") == "proxy" and _matches_backend(listener, route)})
        per_agent.setdefault(agent["agent_id"], []).append(server)
        owners[(listener["pid"], listener["port"])] = agent["agent_id"]
    resources: dict[str, list[dict[str, Any]]] = {"unowned_served": [], "dead_serve": []}
    for route in serve_routes:
        if route.get("kind") != "proxy" or route.get("port") is None:
            continue
        matches = [listener for listener in listeners if _matches_backend(listener, route)]
        if not matches and not any(_matches_backend(endpoint, route)
                                   for endpoint in listener_endpoints or ()):
            resources["dead_serve"].append(dict(route))
        elif any(not _excluded(listener, clients) and (listener["pid"], listener["port"]) not in owners
                 for listener in matches):
            resources["unowned_served"].append(dict(route))
    for servers in per_agent.values():
        servers.sort(key=lambda server: (server["port"], server["pid"]))
    return per_agent, resources


def scan_host_resources(*, runner: Callable = subprocess.run, platform: str | None = None,
                        now: Any = None, proc_root: str | Path = "/proc") -> dict[str, Any]:
    """Collect independent sources without allowing a discovery failure to break a board."""
    result: dict[str, Any] = {"listeners": [], "listener_endpoints": [], "serve": [], "error": None}
    errors = []
    for key, scan, extra in (("listeners", scan_listeners, {"proc_root": proc_root,
                                                          "listener_endpoints": result["listener_endpoints"]}),
                             ("serve", scan_serve, {})):
        try:
            result[key] = scan(runner=runner, platform=platform, now=now, **extra)
        except Exception as error:
            errors.append(f"{key}: {error}")
    result["error"] = "; ".join(errors) or None
    return result
