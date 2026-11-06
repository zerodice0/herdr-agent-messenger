"""Project HAM identity, status and device claims into Herdr metadata and borders."""

from __future__ import annotations

import argparse
import json
import os
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
    marker = "" if "NO_COLOR" in os.environ else marker
    phase = sanitize_status_text(phase, 64)
    summary = sanitize_status_text(summary, 200)
    role_label = {"main": "[메인]", "worker": "[워커]"}.get(role, "")
    display = " ".join(part for part in (marker, role_label, alias) if part)
    content = " · ".join(part for part in (alias, summary) if part)
    label = " ".join(part for part in (marker, role_label, content) if part)
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
        from ham.agents.labels import alias_identity
        from ham.ledger.devices import device_rows

        with self.store.read() as connection:
            claims = [claim for claim in device_rows(connection)
                      if not claim["stale"] and claim["holder"]["agent_id"] == agent["agent_id"]]
        phones = " ".join(claim["label"] for claim in claims if claim["kind"] == "phone")
        emulators = " ".join(claim["label"] for claim in claims if claim["kind"] == "emu")
        name = str(agent["alias"] if alias is None else alias)
        identity = alias_identity(name)
        role = agent["role"] if "role" in agent else self.settings.role(agent)
        return build_display(
            str(agent["client"]), name,
            identity.marker if marker is None else marker,
            identity.color if color is None else color,
            role=role, phase=str(agent.get("phase") or ""),
            summary=str(agent.get("summary") or ""),
            phone=f"📱 {phones}" if phones else "",
            emu=f"🤖 {emulators}" if emulators else "",
        )

    def _active_agent(self, agent: Mapping[str, Any]) -> dict[str, Any] | None:
        """Resolve the current binding while the caller holds its pane lock."""
        if agent.get("host") != self.store.host:
            return None
        rows = [row for row in self.registry.list()
                if row["host"] == agent["host"] and row["pane_id"] == agent["pane_id"]]
        if len(rows) != 1:
            return None
        current = rows[0]
        identity = ("agent_id", "host", "terminal_id", "pane_id", "workspace_id", "repo_id", "client")
        return current if all(current.get(key) == agent.get(key) for key in identity) else None

    def refresh_agent(self, agent: Mapping[str, Any], *, alias: str | None = None,
                      marker: str | None = None, color: str | None = None,
                      legacy_label: str | None = None,
                      feature_invoked: bool = False,
                      observed: Mapping[str, Any] | None = None,
                      on_failure: Callable[[str, subprocess.CompletedProcess[str]], None] | None = None) -> bool | None:
        try:
            with self.store.display_lock(str(agent["host"]), str(agent["pane_id"])):
                return self._refresh_agent_locked(
                    agent, alias=alias, marker=marker, color=color, legacy_label=legacy_label,
                    feature_invoked=feature_invoked, observed=observed, on_failure=on_failure)
        except LedgerError as error:
            if error.code != "display_busy":
                raise
            return None

    def _refresh_agent_locked(self, agent: Mapping[str, Any], *, alias: str | None = None,
                              marker: str | None = None, color: str | None = None,
                              legacy_label: str | None = None,
                              feature_invoked: bool = False,
                              observed: Mapping[str, Any] | None = None,
                              on_failure: Callable[[str, subprocess.CompletedProcess[str]], None] | None = None) -> bool | None:
        current_agent = self._active_agent(agent)
        if current_agent is None:
            return None
        agent = current_agent
        display = self.projection(agent, alias=alias, marker=marker, color=color)
        name = display.alias
        role = agent["role"] if "role" in agent else self.settings.role(agent)
        pane_id = str(agent["pane_id"])
        invoked = feature_invoked or role is not None or bool(
            display.recap or agent.get("summary")
            or agent.get("last_ham_label") not in (None, name, display.display_agent))
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
        latest = self._active_agent(agent)
        if latest is None:
            return None
        if latest != agent:
            # Status or ownership changed during metadata I/O. Do not rename
            # from the old projection; the next refresh uses the latest state.
            return None
        agent = latest
        # Read the actual border after all metadata commands, immediately before
        # the ownership decision. Legacy ownership requires explicit confirmation.
        current = self._border_label(agent)
        if current is None:
            return False
        if current == display.label:
            if self._active_agent(agent) != agent:
                return None
            saved = self.registry.set_display_label(
                agent["agent_id"], agent.get("last_ham_label"), display.label)
            return succeeded and saved["updated"]
        if not border_owned(current, agent.get("last_ham_label"), legacy_label=legacy_label):
            return succeeded
        if self._active_agent(agent) != agent:
            return None
        command = ["pane", "rename", pane_id, display.label] if display.label else ["pane", "rename", pane_id, "--clear"]
        try:
            renamed = self.runner(command)
        except (OSError, subprocess.SubprocessError) as error:
            renamed = subprocess.CompletedProcess(command, 1, "", str(error))
        if not _succeeded(renamed):
            if on_failure is not None:
                on_failure(f"rename status border for {pane_id}", renamed)
            return False
        latest = self._active_agent(agent)
        if latest is None or latest["last_ham_label"] != agent["last_ham_label"]:
            return None
        saved = self.registry.set_display_label(
            agent["agent_id"], agent.get("last_ham_label"), display.label)
        return succeeded and saved["updated"]

    def refresh(self, pane_id: str | None = None, *,
                legacy_labels: Mapping[str, str] | None = None,
                feature_invoked: bool = False,
                observed: Mapping[str, Any] | None = None) -> dict[str, list[str]]:
        result: dict[str, list[str]] = {"updated": [], "failed": [], "skipped": []}
        for agent in self.registry.list():
            if agent["host"] != self.store.host or (pane_id is not None and agent["pane_id"] != pane_id):
                continue
            confirmed = None if legacy_labels is None else legacy_labels.get(agent["pane_id"])
            refreshed = self.refresh_agent(
                agent, legacy_label=confirmed, feature_invoked=feature_invoked, observed=observed)
            key = "skipped" if refreshed is None else "updated" if refreshed else "failed"
            result[key].append(agent["pane_id"])
        if pane_id is not None and not any(result.values()):
            try:
                with self.store.display_lock(self.store.host, pane_id):
                    # A new binding may have appeared after the outer list.
                    if any(row["host"] == self.store.host and row["pane_id"] == pane_id
                           for row in self.registry.list()):
                        result["skipped"].append(pane_id)
                        return result
                    command = ["pane", "report-metadata", pane_id, "--source", SOURCE,
                               "--clear-token", "phone", "--clear-token", "emu"]
                    try:
                        cleared = _succeeded(self.runner(command))
                    except (OSError, subprocess.SubprocessError):
                        cleared = False
                    result["updated" if cleared else "failed"].append(pane_id)
            except LedgerError as error:
                if error.code != "display_busy":
                    raise
                result["skipped"].append(pane_id)
        return result

    def _reclaim_candidate(self, agent: Mapping[str, Any], snapshot: Mapping[str, Any], *,
                           allow_repo_mismatch: bool = False) -> tuple[dict[str, Any], str] | None:
        from ham.ledger.status import recoverable_display_owner

        if agent.get("last_ham_label") is not None:
            return None
        panes = snapshot.get("panes")
        if not isinstance(panes, list):
            raise LedgerError("invalid_snapshot", "Reclaim requires a complete Herdr pane snapshot.")
        matches = [pane for pane in panes if pane.get("pane_id") == agent["pane_id"]]
        if len(matches) != 1 or matches[0].get("terminal_id") != agent["terminal_id"]:
            return None
        border = matches[0].get("label", "")
        if not isinstance(border, str) or not border:
            return None
        terminals = {pane["terminal_id"] for pane in panes if pane.get("terminal_id")}
        with self.store.read() as connection:
            owner = recoverable_display_owner(
                connection, agent, border, live_terminals=terminals,
                allow_repo_mismatch=allow_repo_mismatch)
        return None if owner is None else (owner, border)

    def reclaim(self, *, apply: bool = False, snapshot: Mapping[str, Any] | None = None,
                pane_id: str | None = None, pane_ids: Sequence[str] | None = None) -> dict[str, Any]:
        """Recover only historical HAM display ownership, never mail identity."""
        from ham.ledger.status import live_snapshot

        selected_panes = set(pane_ids or ())
        if pane_id is not None:
            selected_panes.add(pane_id)
        supplied_snapshot = snapshot
        snapshot = live_snapshot() if snapshot is None else snapshot
        result: dict[str, Any] = {
            "applied": apply, "panes": [], "updated": [], "failed": [], "skipped": [],
        }
        for agent in self.registry.list():
            if agent["host"] != self.store.host or (selected_panes and agent["pane_id"] not in selected_panes):
                continue
            explicit = agent["pane_id"] in selected_panes
            candidate = self._reclaim_candidate(agent, snapshot)
            if candidate is None:
                candidate = self._reclaim_candidate(agent, snapshot, allow_repo_mismatch=True)
            if candidate is None:
                continue
            owner, border = candidate
            explicit_only = (owner["terminal_id"] != agent["terminal_id"]
                             and owner["repo_id"] != agent["repo_id"])
            before = {"border": border, "last_ham_label": agent["last_ham_label"]}
            projected = self.projection(agent)
            entry = {
                "host": agent["host"], "pane_id": agent["pane_id"], "agent_id": agent["agent_id"],
                "owner_agent_id": owner["agent_id"], "before": before,
                "after": {"border": projected.label, "last_ham_label": projected.label},
                "status": "candidate",
            }
            entry["prior_repo_id"] = owner["repo_id"]
            entry["current_repo_id"] = agent["repo_id"]
            if explicit_only:
                entry["status"] = "explicit_only"
                entry["reason"] = (
                    f"Prior HAM border ownership belongs to repo {owner['repo_id']!r}; "
                    f"the current agent belongs to repo {agent['repo_id']!r}. "
                    "Cross-repository recovery requires explicit --pane selection.")
            result["panes"].append(entry)
            if not apply:
                continue
            entry["after"] = dict(before)
            if explicit_only and not explicit:
                result["skipped"].append(agent["pane_id"])
                continue
            outcome: bool | None = None
            try:
                with self.store.display_lock(agent["host"], agent["pane_id"]):
                    current = self._active_agent(agent)
                    latest_snapshot = live_snapshot() if supplied_snapshot is None else supplied_snapshot
                    latest = None if current is None else self._reclaim_candidate(
                        current, latest_snapshot, allow_repo_mismatch=explicit)
                    # The real border must still equal the exact historical
                    # label; even a fresh snapshot can race a manual rename.
                    if latest is not None and self._border_label(current) == latest[1]:
                        entry["owner_agent_id"] = latest[0]["agent_id"]
                        entry["prior_repo_id"] = latest[0]["repo_id"]
                        entry["before"] = {
                            "border": latest[1], "last_ham_label": current["last_ham_label"],
                        }
                        if self._active_agent(current) == current:
                            saved = self.registry.set_display_label(current["agent_id"], None, latest[1])
                            if saved["updated"]:
                                outcome = self._refresh_agent_locked(current)
                            else:
                                outcome = None
                    refreshed = self._active_agent(agent)
                    entry["after"] = {
                        "border": self._border_label(refreshed) if refreshed is not None else None,
                        "last_ham_label": refreshed["last_ham_label"] if refreshed is not None else None,
                    }
            except LedgerError as error:
                if error.code != "display_busy":
                    raise
            key = "skipped" if outcome is None else "updated" if outcome else "failed"
            entry["status"] = key
            result[key].append(agent["pane_id"])
        return result


def report_agent_metadata(pane_id: str, agent: str, alias: str, marker: str, color: str, *,
                          runner: HerdrRunner = _run_herdr, store: LedgerStore | None = None,
                          observed: Mapping[str, Any] | None = None,
                          on_failure: Callable[[str, subprocess.CompletedProcess[str]], None] | None = None) -> bool | None:
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
                return None if display.registry.defer_display(matches[0], desired) else False
        refreshed = display.refresh_agent(
            matches[0], alias=alias, marker=marker, color=color, observed=observed, on_failure=on_failure)
        if refreshed is None and not display.registry.defer_display(matches[0], desired):
            return False
        return refreshed
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
    parser = argparse.ArgumentParser(description="Refresh or reclaim HAM-owned pane borders on this host.")
    parser.add_argument("operation", nargs="?", choices=("refresh", "reclaim"), default="refresh")
    parser.add_argument("--pane", action="append",
                        help="Select a pane explicitly; repeat to reclaim multiple panes.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true", help="Apply proven historical border ownership.")
    mode.add_argument("--dry-run", action="store_true", help="List reclaim candidates without writes (default).")
    arguments = parser.parse_args(argv)
    if arguments.operation == "refresh" and (
            not arguments.pane or len(arguments.pane) != 1 or arguments.apply or arguments.dry_run):
        parser.error("refresh requires exactly one --pane; --apply and --dry-run are reclaim options")
    try:
        if arguments.operation == "reclaim":
            store = LedgerStore() if arguments.apply else LedgerStore.open_readonly()
            result = ({"applied": False, "panes": [], "updated": [], "failed": [], "skipped": []}
                      if store is None else LedgerDisplay(store).reclaim(
                          apply=arguments.apply, pane_ids=arguments.pane))
            print(json.dumps({"result": result}, ensure_ascii=False))
            return 1 if result["failed"] else 0
        store = LedgerStore()
        AgentRegistry(store).refresh()
        result = refresh_pane(arguments.pane[0], store)
        print(json.dumps({"result": result}, ensure_ascii=False))
        return 1 if result["failed"] else 0
    except (LedgerError, OSError, subprocess.SubprocessError) as error:
        print(json.dumps({"error": {"code": getattr(error, "code", "display_refresh_failed"),
                                  "message": str(error)}}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
