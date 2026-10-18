"""Create interactive Herdr agents with immediately addressable HAM labels."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import uuid
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path, PurePosixPath
from dataclasses import dataclass
from typing import Any

from agent_directory import (
    REMOTE_DISCOVERY_TIMEOUT_SECONDS,
    AgentRecord,
    decode_json_object,
    extract_agent_list_payload,
    herdr_executable,
    is_agent_list_payload,
    parse_agent_payload,
    run_bounded_command,
    ssh_command,
    ssh_config_path,
    ssh_hosts,
    ssh_program_command,
    submission_failure_is_uncertain,
)
from agent_labels import ALIASES, Alias, candidates
from agent_output import compact_agent_payload, compact_spawn_payload
from agent_route import encode_agent_route
from ledger_display import LedgerDisplay
from ledger_mail import Mailbox
from ledger_remote import RemoteLedger
from ledger_status import AgentRegistry
from ledger_store import LedgerError, LedgerStore


class SpawnError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class _Placement:
    created: str
    pane_id: str
    terminal_id: str
    workspace_id: str
    tab_id: str

    @property
    def close_target(self) -> tuple[str, str]:
        if self.created == "workspace" and self.workspace_id:
            return "workspace", self.workspace_id
        if self.created == "tab" and self.tab_id:
            return "tab", self.tab_id
        return "pane", self.pane_id


def add_spawn_parser(subparsers: Any) -> None:
    parser = subparsers.add_parser(
        "spawn",
        help="Start a new interactive agent with a HAM address.",
        description="Start an interactive agent; pass agent arguments after --.",
    )
    parser.add_argument("--kind", required=True, help="Herdr agent kind (e.g. codex).")
    parser.add_argument("--host", default="local", help="Local server or a configured SSH host alias.")
    parser.add_argument("--cwd", help="Working directory; required for SSH hosts.")
    parser.add_argument(
        "--placement", choices=("split", "tab", "workspace"), default="split",
        help="Create a split pane (default), a tab, or a workspace.",
    )
    parser.add_argument("--from-pane", help="Source pane on the selected host.")
    parser.add_argument(
        "--direction", choices=("right", "down"), default="right",
        help="Split direction (default: right).",
    )
    parser.add_argument("--workspace", help="Workspace for a new tab.")
    parser.add_argument("--workspace-label", help="Label for the new tab or workspace.")
    parser.add_argument("--label", help="Agent label; defaults to a free HAM color-animal.")
    message = parser.add_mutually_exclusive_group()
    message.add_argument("--message", help="Submit this initial prompt once after interactive readiness.")
    message.add_argument("--message-file", help="Read the initial prompt from a caller-local UTF-8 file.")
    parser.add_argument(
        "--timeout", type=int, default=90_000, metavar="MS",
        help="Interactive readiness timeout in ms (1–300000; default: 90000).",
    )
    parser.add_argument(
        "--verbose", action="store_true",
        help="Print the full legacy payload including the route.",
    )
    parser.add_argument(
        "agent_args", nargs=argparse.REMAINDER,
        help="Arguments forwarded to the interactive agent after --.",
    )


def _command(
    host: str, arguments: Sequence[str], environment: Mapping[str, str],
    *, remote_socket: str | None = None,
) -> list[str]:
    if host == "local":
        return [herdr_executable(environment), *arguments]
    return ssh_command(host, arguments, config_path=ssh_config_path(environment),
                       remote_socket=remote_socket)


def _payload(stdout: str) -> dict[str, Any]:
    payload = decode_json_object(stdout)
    if payload:
        return payload
    # SSH login banners can precede Herdr's JSON response.
    for line in reversed(stdout.splitlines()):
        payload = decode_json_object(line)
        if payload:
            return payload
    return {}


def _failure(result: subprocess.CompletedProcess[str]) -> str:
    return result.stderr.strip() or result.stdout.strip() or "Herdr command failed."


def _failed(result: subprocess.CompletedProcess[str]) -> bool:
    payload = _payload(result.stdout)
    return result.returncode != 0 or bool(payload.get("error")) or payload.get("ok") is False


def _result_payload(result: subprocess.CompletedProcess[str], code: str) -> dict[str, Any]:
    if _failed(result):
        raise SpawnError(code, _failure(result))
    payload = _payload(result.stdout).get("result")
    if not isinstance(payload, dict):
        raise SpawnError(code, "Herdr returned no placement information.")
    return payload


def _placement(result: Mapping[str, Any], created: str) -> _Placement:
    pane = result.get("pane" if created == "pane" else "root_pane")
    pane = pane if isinstance(pane, dict) else {}
    workspace = result.get("workspace")
    workspace = workspace if isinstance(workspace, dict) else {}
    tab = result.get("tab")
    tab = tab if isinstance(tab, dict) else {}
    pane_id = str(pane.get("pane_id") or "")
    return _Placement(
        created=created,
        pane_id=pane_id,
        terminal_id=str(pane.get("terminal_id") or result.get("terminal_id") or pane_id),
        workspace_id=str(
            result.get("workspace_id") or pane.get("workspace_id")
            or workspace.get("workspace_id") or tab.get("workspace_id") or ""
        ),
        tab_id=str(result.get("tab_id") or pane.get("tab_id") or tab.get("tab_id") or ""),
    )


def _raw_agents(payload: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    result = payload.get("result")
    if not isinstance(result, dict):
        return []
    snapshot = result.get("snapshot")
    container = snapshot if isinstance(snapshot, dict) else result
    return [agent for agent in container.get("agents", []) if isinstance(agent, dict)]


def _report_metadata(
    run: Callable[[Sequence[str]], subprocess.CompletedProcess[str]],
    pane_id: str,
    kind: str,
    alias: Alias,
    *,
    host: str = "local",
    environment: Mapping[str, str] | None = None,
    command_runner: Callable[..., subprocess.CompletedProcess[str]] = run_bounded_command,
) -> bool:
    # Read authoritative state only on the selected host. Local storage is used
    # for remote routing configuration, never for the remote agent's role/status.
    try:
        store = LedgerStore(environment=environment)
        if host == "local":
            registry = AgentRegistry(store, display_runner=run)
            registry.refresh(environment)
            if not any(row["host"] == store.host and row["pane_id"] == pane_id
                       and row["client"] == kind and row["alias"] == alias.name
                       for row in registry.list()):
                return False
            refreshed = LedgerDisplay(store, runner=run).refresh(pane_id)
            return pane_id in refreshed["updated"] and not refreshed["failed"]
        peer = RemoteLedger(store, environment).peer(host)
        if not peer["remote_ham_root"]:
            return False
        arguments = ["env"]
        if peer.get("remote_ledger_dir"):
            arguments.append("HERDR_AGENT_LEDGER_DIR=" + peer["remote_ledger_dir"])
        if peer.get("remote_socket"):
            arguments.append("HERDR_SOCKET_PATH=" + peer["remote_socket"])
        if (peer.get("remote_ledger_dir") and environment is not None
                and environment.get("HERDR_AGENT_LEDGER_AUTOSTART") == "0"):
            arguments.append("HERDR_AGENT_LEDGER_AUTOSTART=0")
        arguments += ["python3", str(PurePosixPath(peer["remote_ham_root"]) / "ledger_display.py"),
                      "--pane", pane_id]
        result = command_runner(
            ssh_program_command(peer["ssh_alias"], arguments, config_path=ssh_config_path(environment)),
            timeout=REMOTE_DISCOVERY_TIMEOUT_SECONDS,
            environment=environment,
        )
        if _failed(result):
            return False
        refreshed = _payload(result.stdout).get("result")
        return (isinstance(refreshed, dict) and pane_id in refreshed.get("updated", [])
                and not refreshed.get("failed"))
    except (LedgerError, OSError, subprocess.SubprocessError):
        return False


def _initial_submission(
    operation_id: str, body: str, agent: AgentRecord, placement: _Placement, *,
    address: str, readiness_evidence: str, environment: Mapping[str, str],
    run: Callable[..., subprocess.CompletedProcess[str]],
    discover: Callable[[], tuple[dict[str, Any], list[AgentRecord]]],
) -> dict[str, Any]:
    """Commit the one permission before transport; uncertainty never retries."""
    result: dict[str, Any] = {"operation_id": operation_id, "acceptance": "failed"}
    mailbox: Mailbox | None = None
    claimed = False
    attempted = False
    try:
        if not agent.terminal_id or not agent.session_id or agent.agent_kind == "":
            raise SpawnError("recipient_unresolved", "The ready agent must report its terminal, native session and client.")
        store = LedgerStore(environment=environment)
        mailbox = Mailbox(store)
        sender: dict[str, Any] = {"kind": "unknown"}
        try:
            registry = AgentRegistry(store)
            snapshot, _ = registry.refresh(environment)
            current = registry.resolve_context(environment, snapshot)
            sender = {key: current[key] for key in
                      ("host", "terminal_id", "session_ref", "client", "agent_id")}
            sender["kind"] = "agent"
        except LedgerError:
            # A non-agent shell is a valid spawn initiator, not necessarily user.
            pass
        canonical_host: str | None = store.host if agent.local else None
        if not agent.local:
            try:
                canonical_host = RemoteLedger(store, environment).peer(agent.host)["canonical_name"]
            except LedgerError:
                # Ordinary SSH spawn does not require a mailbox peer or discovery.
                pass
        target = {
            "host": canonical_host, "transport_host": agent.host,
            "local": agent.local, "terminal_id": agent.terminal_id,
            "session_ref": agent.session_id, "client": agent.agent_kind,
            "pane_id": agent.pane_id, "address": address,
            "workspace_id": agent.workspace_id or placement.workspace_id,
            "tab_id": placement.tab_id, "created": placement.created,
        }
        claim = mailbox.claim_spawn_submission(
            operation_id, target=target, body=body, sender=sender,
            readiness_evidence=readiness_evidence,
        )
        result["submission_id"] = claim["submission_id"]
        if not claim["should_submit"]:
            record = claim["record"]
            result["acceptance"] = record["acceptance"] if record["acceptance"] != "submitting" else "unknown"
            result["evidence"] = record.get("evidence") or "This operation already owns a submission; it was not replayed."
            return result
        claimed = True
        _, current_agents = discover()
        matches = [candidate for candidate in current_agents
                   if candidate.terminal_id == agent.terminal_id]
        if (len(matches) != 1 or matches[0].session_id != agent.session_id
                or matches[0].agent_kind != agent.agent_kind
                or matches[0].pane_id != agent.pane_id):
            raise SpawnError("recipient_changed", "The ready terminal/session changed before initial submission.")
        attempted = True
        response = run(["agent", "prompt", agent.pane_id, body])
        if response.returncode == 0 and not _payload(response.stdout):
            result.update(acceptance="unknown", error_code="prompt_acceptance_unknown",
                          evidence="Herdr prompt response was missing or invalid after possible acceptance.")
        elif not _failed(response):
            result.update(acceptance="accepted", evidence="Herdr terminal prompt accepted; native context application is not asserted.")
        elif submission_failure_is_uncertain(response, remote=not agent.local):
            result.update(acceptance="unknown", error_code="prompt_acceptance_unknown", evidence=_failure(response))
        else:
            result.update(acceptance="failed", error_code="prompt_failed", evidence=_failure(response))
    except Exception as error:
        result.update(
            acceptance="unknown" if attempted else "failed",
            error_code="prompt_acceptance_unknown" if attempted else getattr(error, "code", "initial_prompt_failed"),
            evidence=str(error),
        )
    if claimed and mailbox is not None:
        try:
            mailbox.finish_spawn_submission(
                operation_id, acceptance=result["acceptance"], evidence=result["evidence"],
            )
        except Exception as error:
            # Durable submitting is uncertain even when this process saw success.
            result.update(acceptance="unknown", error_code="spawn_state_unavailable",
                          evidence=f"Submission outcome could not be persisted: {error}")
    return result


def spawn_command(
    arguments: argparse.Namespace,
    environment: Mapping[str, str] | None = None,
    *,
    command_runner: Callable[..., subprocess.CompletedProcess[str]] = run_bounded_command,
) -> dict[str, Any]:
    values = os.environ if environment is None else environment
    body = getattr(arguments, "message", None)
    message_file = getattr(arguments, "message_file", None)
    if message_file is not None:
        try:
            body = Path(message_file).read_text(encoding="utf-8")
        except (OSError, UnicodeError) as error:
            raise SpawnError("message_file_unreadable", str(error)) from None
    if body is not None and not body.strip():
        raise SpawnError("empty_message", "The initial message must not be empty.")
    operation_id = str(uuid.uuid4()) if body is not None else None
    host = arguments.host
    kind = arguments.kind
    if not kind or not kind.strip() or any(character.isspace() for character in kind):
        raise SpawnError("agent_start_failed", "--kind must be a non-empty agent-kind token.")
    if not 1 <= arguments.timeout <= 300_000:
        raise SpawnError("invalid_timeout", "--timeout must be between 1 and 300000 milliseconds.")
    label = arguments.label
    if label is not None and (not label.strip() or "/" in label):
        raise SpawnError("invalid_label", "--label must be non-empty and must not contain '/'.")
    local = host == "local"
    from_pane = arguments.from_pane
    if arguments.placement == "split" and not from_pane:
        if not local or not values.get("HERDR_PANE_ID"):
            raise SpawnError(
                "sender_pane_unavailable",
                "A split needs --from-pane on SSH hosts, or HERDR_PANE_ID locally.",
            )
        from_pane = values["HERDR_PANE_ID"]
    cwd = arguments.cwd or (os.getcwd() if local else "")
    if not cwd:
        raise SpawnError("cwd_required", "--cwd is required for an SSH host.")
    if not local and host not in ssh_hosts(values):
        raise SpawnError(
            "host_not_configured",
            f"SSH host {host!r} is not a concrete alias in the configured SSH config.",
        )
    remote_socket = None
    if not local:
        try:
            remote_socket = RemoteLedger(LedgerStore(environment=values), values).peer(host).get("remote_socket") or None
        except (LedgerError, OSError):
            # No new mailbox-peer prerequisite for ordinary remote creation.
            pass

    def run(
        command: Sequence[str], *, timeout: float = REMOTE_DISCOVERY_TIMEOUT_SECONDS,
    ) -> subprocess.CompletedProcess[str]:
        return command_runner(_command(host, command, values, remote_socket=remote_socket),
                              timeout=timeout, environment=values)

    def discover() -> tuple[dict[str, Any], list[AgentRecord]]:
        try:
            result = run(["agent", "list"])
        except (OSError, subprocess.SubprocessError) as error:
            raise SpawnError("host_unavailable", str(error)) from None
        if _failed(result):
            raise SpawnError("host_unavailable", _failure(result))
        payload = extract_agent_list_payload(result.stdout)
        if not is_agent_list_payload(payload):
            raise SpawnError("host_unavailable", "Herdr returned no agent list.")
        return payload, parse_agent_payload(payload, host=host, local=local)

    placement_name = "pane" if arguments.placement == "split" else arguments.placement
    create_label = arguments.workspace_label or label
    if arguments.placement == "split":
        create = [
            "pane", "split", "--pane", from_pane,
            "--direction", arguments.direction, "--cwd", cwd, "--no-focus",
        ]
    elif arguments.placement == "tab":
        workspace = arguments.workspace
        source = arguments.from_pane or (values.get("HERDR_PANE_ID") if local else None)
        if not workspace and source:
            try:
                result = _result_payload(run(["pane", "get", source]), "placement_failed")
            except (OSError, subprocess.SubprocessError) as error:
                raise SpawnError("placement_failed", str(error)) from None
            workspace = _placement(result, "pane").workspace_id
            if not workspace:
                raise SpawnError("placement_failed", "The source pane has no workspace ID.")
        create = ["tab", "create", "--cwd", cwd, "--no-focus"]
        if workspace:
            create.extend(["--workspace", workspace])
        if create_label is not None:
            create.extend(["--label", create_label])
    else:
        create = ["workspace", "create", "--cwd", cwd, "--no-focus"]
        if create_label is not None:
            create.extend(["--label", create_label])
    try:
        placement = _placement(_result_payload(run(create), "placement_failed"), placement_name)
    except (OSError, subprocess.SubprocessError) as error:
        raise SpawnError("placement_failed", str(error)) from None

    def cleanup() -> str:
        entity, target = placement.close_target
        if not target:
            return "cleanup: failed (Herdr returned no created-resource ID)"
        try:
            result = run([entity, "close", target])
        except (OSError, subprocess.SubprocessError) as error:
            return f"cleanup: failed ({error})"
        if _failed(result):
            return f"cleanup: failed ({_failure(result)})"
        return f"cleanup: closed {entity} {target}"

    try:
        if not placement.pane_id:
            raise SpawnError("placement_failed", "Herdr returned no new pane ID.")
        # Inspect both registered-name fields: agent_label() deliberately selects
        # just one, but either name can prevent Herdr from registering a candidate.
        snapshot, _ = discover()
        taken = {
            str(agent.get(key))
            for agent in _raw_agents(snapshot)
            for key in ("name", "agent_name")
            if agent.get(key)
        }
        aliases = (
            [Alias(label, "", "")] if label is not None
            else (alias for alias in candidates(f"{placement.terminal_id}:") if alias.name not in taken)
        )
        selected: Alias | None = None
        readiness_evidence = ""
        for alias in aliases:
            command = [
                "agent", "start", alias.name, "--kind", kind, "--pane", placement.pane_id,
                "--timeout", str(arguments.timeout),
            ]
            agent_args = list(arguments.agent_args)
            if agent_args[:1] == ["--"]:
                agent_args = agent_args[1:]
            if agent_args:
                command.extend(["--", *agent_args])
            try:
                result = run(
                    command, timeout=arguments.timeout / 1000 + REMOTE_DISCOVERY_TIMEOUT_SECONDS,
                )
            except (OSError, subprocess.SubprocessError) as error:
                raise SpawnError("agent_start_failed", str(error)) from None
            if not _failed(result):
                if body is not None:
                    ready = _payload(result.stdout).get("result")
                    if not isinstance(ready, dict) or not ready or ready.get("ready") is False:
                        raise SpawnError("agent_not_ready", "Herdr did not confirm interactive readiness.")
                    readiness_evidence = json.dumps(ready, ensure_ascii=False, sort_keys=True)
                selected = alias
                break
            detail = _failure(result)
            if label is None and "agent_name_taken" in (result.stderr + result.stdout):
                continue
            raise SpawnError("agent_start_failed", detail)
        if selected is None:
            raise SpawnError("label_unavailable", "No HAM color-animal label is available.")
        label = selected.name
        palette_alias = next((alias for alias in ALIASES if alias.name == label), None)
        metadata_reported = _report_metadata(
            run, placement.pane_id, kind, palette_alias or Alias(label, "", ""),
            host=host, environment=values, command_runner=command_runner,
        )
        placement_label_reported = True
        if placement.created != "pane" and create_label is None:
            entity, target = placement.close_target
            if entity != placement.created or not target:
                placement_label_reported = False
            else:
                try:
                    placement_label_reported = not _failed(run([entity, "rename", target, label]))
                except (OSError, subprocess.SubprocessError):
                    placement_label_reported = False
        _, agents = discover()
        matches = [
            agent for agent in agents
            if agent.pane_id == placement.pane_id and agent.name == label
        ]
        if len(matches) != 1:
            raise SpawnError("agent_start_failed", "The started agent could not be resolved uniquely.")
        agent = matches[0]
        # Short, decision-relevant fields first: agent tool outputs often truncate
        # long single-line JSON, so the opaque route and nested record go last.
        payload: dict[str, Any] = {
            "address": f"{host}/{label}",
            "label": label,
            "marker": palette_alias.marker if palette_alias else "",
            "color": palette_alias.color if palette_alias else "",
            "host": host,
            "created": placement.created,
            "pane_id": placement.pane_id,
        }
        if placement.tab_id:
            payload["tab_id"] = placement.tab_id
        payload.update(
            {
                "workspace_id": agent.workspace_id or placement.workspace_id,
                "kind": kind,
                "cwd": cwd,
                "label_metadata_reported": metadata_reported,
                "placement_label_reported": placement_label_reported,
                "agent": compact_agent_payload(agent),
                "route": encode_agent_route(agent),
            }
        )
        if body is not None:
            assert operation_id is not None
            payload["initial_prompt"] = _initial_submission(
                operation_id, body, agent, placement, address=payload["address"],
                readiness_evidence=readiness_evidence, environment=values,
                run=run, discover=discover,
            )
        return payload if arguments.verbose else compact_spawn_payload(payload)
    except SpawnError as error:
        if body is not None:
            payload = {
                "address": f"{host}/{label}" if label else "",
                "marker": "", "created": placement.created, "pane_id": placement.pane_id,
                "workspace_id": placement.workspace_id,
                "initial_prompt": {"operation_id": operation_id, "acceptance": "failed",
                                   "error_code": error.code, "evidence": error.message},
            }
            if placement.tab_id:
                payload["tab_id"] = placement.tab_id
            return payload if arguments.verbose else compact_spawn_payload(payload)
        raise SpawnError(error.code, f"{error.message}; {cleanup()}") from None
