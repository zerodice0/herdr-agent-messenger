"""Project HAM identity, status and device claims into Herdr metadata and borders."""

from __future__ import annotations

import argparse
import json
import subprocess
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from ham.agents.directory import herdr_executable, run_bounded_command
from ham.ledger.settings import RepoSettings
from ham.ledger.status import AgentRegistry, sanitize_status_text
from ham.ledger.store import LedgerError, LedgerStore

SOURCE = "herdr.agent-messenger"
LEGACY_SOURCE = "herdr.agent-labels"
RECAP_SOURCE = "user:pane-recap"
HerdrRunner = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]


@dataclass(frozen=True)
class DisplayProjection:
    agent: str
    display_agent: str
    alias: str
    color: str
    recap: str
    label: str
    phone: str | None = None
    emu: str | None = None

    def metadata_command(self, pane_id: str) -> list[str]:
        command = [
            "pane", "report-metadata", pane_id, "--source", SOURCE,
            "--agent", self.agent, "--display-agent", self.display_agent,
            "--token", f"alias={self.alias}", "--token", f"color={self.color}",
            "--token", f"recap={self.recap}",
        ]
        for key, value in (("phone", self.phone), ("emu", self.emu)):
            if value is not None:
                command.extend(["--token", f"{key}={value}"] if value else ["--clear-token", key])
        return command


def build_display(agent: str, alias: str, marker: str = "", color: str = "", *,
                  role: str | None = None, phase: str = "", summary: str = "",
                  phone: str | None = None, emu: str | None = None) -> DisplayProjection:
    """Keep Agents addressable by suffix and put border identity before status."""
    phase = sanitize_status_text(phase, 64)
    summary = sanitize_status_text(summary, 200)
    role_label = {"main": "[메인]", "worker": "[워커]"}.get(role, "")
    display = " ".join(part for part in (marker, role_label, alias) if part)
    content = " · ".join(part for part in (alias, summary) if part)
    label = " ".join(part for part in (role_label, content) if part)
    return DisplayProjection(agent, display, alias, color, phase, label, phone, emu)


def normalize_metadata_text(value: str) -> str:
    """Match Herdr's 80-codepoint presentation/token normalization."""
    return "".join(character for character in value.strip()
                   if unicodedata.category(character) != "Cc")[:80].strip()


def metadata_matches(display: DisplayProjection, observed: Mapping[str, Any]) -> bool:
    """Compare effective HAM fields, not detection revisions or other sources."""
    tokens = observed.get("tokens") or {}
    if (not isinstance(tokens, Mapping)
            or observed.get("display_agent", "") != normalize_metadata_text(display.display_agent)):
        return False
    return all(tokens.get(key, "") == normalize_metadata_text(value) for key, value in (
        ("alias", display.alias), ("color", display.color), ("recap", display.recap),
        ("phone", display.phone), ("emu", display.emu),
    ) if value is not None)


def _run_herdr(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return run_bounded_command([herdr_executable(), *command], timeout=10)


def _payload(result: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    try:
        value = json.loads(result.stdout)
    except (ValueError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def _succeeded(result: subprocess.CompletedProcess[str]) -> bool:
    payload = _payload(result)
    return result.returncode == 0 and not payload.get("error") and payload.get("ok") is not False


def report_metadata(runner: HerdrRunner, pane_id: str, display: DisplayProjection, *,
                    clear_legacy_recap: bool = False,
                    clear_legacy_identity: bool = True,
                    on_failure: Callable[[str, subprocess.CompletedProcess[str]], None] | None = None) -> bool:
    """Use the caller's selected-host runner, never a local ledger for remote panes."""
    commands = []
    if clear_legacy_identity:
        commands.append(
            ["pane", "report-metadata", pane_id, "--source", LEGACY_SOURCE,
             "--clear-display-agent", "--clear-token", "alias", "--clear-token", "color"])
    if clear_legacy_recap:
        commands.append(["pane", "report-metadata", pane_id, "--source", RECAP_SOURCE, "--clear-token", "recap"])
    commands.append(display.metadata_command(pane_id))
    succeeded = True
    for command in commands:
        try:
            result = runner(command)
        except (OSError, subprocess.SubprocessError) as error:
            result = subprocess.CompletedProcess(command, 1, "", str(error))
        if not _succeeded(result):
            succeeded = False
            if on_failure is not None:
                on_failure(f"report label metadata for {pane_id} from {command[4]}", result)
    return succeeded


def border_owned(current: str, last_ham_label: str | None, *, legacy_label: str | None = None) -> bool:
    """A legacy recap token alone cannot prove who last renamed a pane border."""
    return ((not current and last_ham_label is None)
            or (last_ham_label is not None and current == last_ham_label)
            or (legacy_label is not None and current == legacy_label))


class LedgerDisplay:
    def __init__(self, store: LedgerStore, *, runner: HerdrRunner = _run_herdr) -> None:
        self.store = store
        self.registry = AgentRegistry(store)
        self.settings = RepoSettings(store)
        self.runner = runner

    def _border_label(self, agent: Mapping[str, Any]) -> str | None:
        """Read a complete current binding; Herdr omits the label when blank."""
        try:
            result = self.runner(["pane", "get", str(agent["pane_id"])])
            container = _payload(result).get("result")
            pane = container.get("pane") if isinstance(container, dict) else None
            if (not _succeeded(result) or not isinstance(pane, dict)
                    or not agent.get("pane_id") or not agent.get("terminal_id")
                    or pane.get("pane_id") != agent["pane_id"]
                    or pane.get("terminal_id") != agent["terminal_id"]):
                return None
            label = pane.get("label", "")
            return label if isinstance(label, str) else None
        except (OSError, subprocess.SubprocessError):
            return None

    def projection(self, agent: Mapping[str, Any], *, alias: str | None = None,
                   marker: str | None = None, color: str | None = None) -> DisplayProjection:
        from ham.agents.labels import ALIASES
        from ham.ledger.devices import device_rows

        with self.store.read() as connection:
            claims = [claim for claim in device_rows(connection)
                      if not claim["stale"] and claim["holder"]["agent_id"] == agent["agent_id"]]
        phones = " ".join(claim["label"] for claim in claims if claim["kind"] == "phone")
        emulators = " ".join(claim["label"] for claim in claims if claim["kind"] == "emu")
        name = str(agent["alias"] if alias is None else alias)
        palette = next((item for item in ALIASES if item.name == name), None)
        role = agent["role"] if "role" in agent else self.settings.role(agent)
        return build_display(
            str(agent["client"]), name,
            (palette.marker if palette else "") if marker is None else marker,
            (palette.color if palette else "") if color is None else color,
            role=role, phase=str(agent.get("phase") or ""),
            summary=str(agent.get("summary") or ""),
            phone=f"📱 {phones}" if phones else "",
            emu=f"🤖 {emulators}" if emulators else "",
        )

    def refresh_agent(self, agent: Mapping[str, Any], *, alias: str | None = None,
                      marker: str | None = None, color: str | None = None,
                      legacy_label: str | None = None,
                      feature_invoked: bool = False,
                      observed: Mapping[str, Any] | None = None,
                      on_failure: Callable[[str, subprocess.CompletedProcess[str]], None] | None = None) -> bool:
        display = self.projection(agent, alias=alias, marker=marker, color=color)
        name = display.alias
        role = agent["role"] if "role" in agent else self.settings.role(agent)
        pane_id = str(agent["pane_id"])
        invoked = feature_invoked or role is not None or bool(
            display.recap or agent.get("summary") or agent.get("last_ham_label") not in (None, name))
        if self._border_label(agent) is None:
            return False
        succeeded = True
        if observed is None or not metadata_matches(display, observed):
            # A snapshot repair needs only one authoritative patch. Legacy
            # cleanup is reserved for explicit/migration calls; clearing flat
            # tokens before every repair would itself create presentation drift.
            succeeded = report_metadata(
                self.runner, pane_id, display, clear_legacy_recap=invoked and observed is None,
                clear_legacy_identity=observed is None, on_failure=on_failure)
        # Read the actual border after all metadata commands, immediately before
        # the ownership decision. Legacy ownership requires explicit confirmation.
        current = self._border_label(agent)
        if current is None:
            return False
        if current == display.label:
            saved = self.registry.set_display_label(
                agent["agent_id"], agent.get("last_ham_label"), display.label)
            return succeeded and saved["updated"]
        if not border_owned(current, agent.get("last_ham_label"), legacy_label=legacy_label):
            return succeeded
        command = ["pane", "rename", pane_id, display.label] if display.label else ["pane", "rename", pane_id, "--clear"]
        try:
            renamed = self.runner(command)
        except (OSError, subprocess.SubprocessError) as error:
            renamed = subprocess.CompletedProcess(command, 1, "", str(error))
        if not _succeeded(renamed):
            if on_failure is not None:
                on_failure(f"rename status border for {pane_id}", renamed)
            return False
        saved = self.registry.set_display_label(
            agent["agent_id"], agent.get("last_ham_label"), display.label)
        return succeeded and saved["updated"]

    def refresh(self, pane_id: str | None = None, *,
                legacy_labels: Mapping[str, str] | None = None,
                feature_invoked: bool = False,
                observed: Mapping[str, Any] | None = None) -> dict[str, list[str]]:
        result: dict[str, list[str]] = {"updated": [], "failed": []}
        for agent in self.registry.list():
            if agent["host"] != self.store.host or (pane_id is not None and agent["pane_id"] != pane_id):
                continue
            confirmed = None if legacy_labels is None else legacy_labels.get(agent["pane_id"])
            key = "updated" if self.refresh_agent(
                agent, legacy_label=confirmed, feature_invoked=feature_invoked, observed=observed) else "failed"
            result[key].append(agent["pane_id"])
        if pane_id is not None and not result["updated"] and not result["failed"]:
            command = ["pane", "report-metadata", pane_id, "--source", SOURCE,
                       "--clear-token", "phone", "--clear-token", "emu"]
            try:
                cleared = _succeeded(self.runner(command))
            except (OSError, subprocess.SubprocessError):
                cleared = False
            result["updated" if cleared else "failed"].append(pane_id)
        return result


def report_agent_metadata(pane_id: str, agent: str, alias: str, marker: str, color: str, *,
                          runner: HerdrRunner = _run_herdr, store: LedgerStore | None = None,
                          observed: Mapping[str, Any] | None = None,
                          on_failure: Callable[[str, subprocess.CompletedProcess[str]], None] | None = None) -> bool:
    """Refresh a named local agent, including a session not yet in the registry."""
    ledger = LedgerStore() if store is None else store
    display = LedgerDisplay(ledger, runner=runner)
    matches = [row for row in display.registry.list()
               if row["host"] == ledger.host and row["pane_id"] == pane_id and row["client"] == agent]
    if len(matches) == 1:
        desired = display.projection(matches[0], alias=alias, marker=marker, color=color)
        if observed is not None and metadata_matches(desired, observed):
            return True
        if observed is not None:
            from ham.ledger import display_repair
            with ledger.read() as connection:
                repairs = display_repair.load(connection)
            pending = repairs.get(pane_id)
            if ((pending and pending["fingerprint"] == display_repair.fingerprint(matches[0], desired))
                    or len(repairs) >= display_repair.MAX_PANES):
                # The ledger's resident sweep owns this retry budget. A second
                # event handler must not bypass its backoff or exhausted cap.
                return True
        return display.refresh_agent(
            matches[0], alias=alias, marker=marker, color=color, observed=observed, on_failure=on_failure)
    desired = build_display(agent, alias, marker, color)
    if observed is not None and metadata_matches(desired, observed):
        return True
    return report_metadata(runner, pane_id, desired,
                           clear_legacy_identity=observed is None, on_failure=on_failure)


def refresh(store: LedgerStore | None = None, environment: Mapping[str, str] | None = None, *,
            runner: HerdrRunner | None = None) -> dict[str, list[str]]:
    ledger = LedgerStore(environment=environment) if store is None else store
    selected_runner = runner or (lambda command: run_bounded_command(
        [herdr_executable(environment), *command], timeout=10, environment=environment))
    return LedgerDisplay(ledger, runner=selected_runner).refresh()


def refresh_pane(pane_id: str, store: LedgerStore | None = None,
                 environment: Mapping[str, str] | None = None, *,
                 runner: HerdrRunner | None = None,
                 legacy_label: str | None = None) -> dict[str, list[str]]:
    ledger = LedgerStore(environment=environment) if store is None else store
    selected_runner = runner or (lambda command: run_bounded_command(
        [herdr_executable(environment), *command], timeout=10, environment=environment))
    legacy = None if legacy_label is None else {pane_id: legacy_label}
    return LedgerDisplay(ledger, runner=selected_runner).refresh(
        pane_id, legacy_labels=legacy, feature_invoked=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Refresh authoritative HAM role and status on this host.")
    parser.add_argument("--pane", required=True)
    arguments = parser.parse_args(argv)
    try:
        store = LedgerStore()
        AgentRegistry(store).refresh()
        result = refresh_pane(arguments.pane, store)
        print(json.dumps({"result": result}, ensure_ascii=False))
        return 0 if arguments.pane in result["updated"] and not result["failed"] else 1
    except (LedgerError, OSError, subprocess.SubprocessError) as error:
        print(json.dumps({"error": {"code": getattr(error, "code", "display_refresh_failed"),
                                  "message": str(error)}}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
