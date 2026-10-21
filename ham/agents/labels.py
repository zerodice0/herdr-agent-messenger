#!/usr/bin/env python3
"""Assign unique, human-readable names to unnamed Herdr agents."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from typing import Any, Iterator, NamedTuple

from ham.agents.directory import herdr_executable
from ham.ledger.display import LEGACY_SOURCE, SOURCE, report_agent_metadata


SEED_HASH_BYTES = 4
# Coprime with the current alias count, so every alias is visited exactly once.
CANDIDATE_STRIDE = 37


class Alias(NamedTuple):
    name: str
    marker: str
    color: str


COLORS = (
    ("white", "⬜"),
    ("blue", "🟦"),
    ("green", "🟩"),
    ("yellow", "🟨"),
    ("orange", "🟧"),
    ("red", "🟥"),
    ("purple", "🟪"),
    ("brown", "🟫"),
)
ANIMALS = (
    "owl",
    "otter",
    "fox",
    "raven",
    "panda",
    "koala",
    "tiger",
    "gecko",
    "heron",
    "wolf",
    "seal",
    "yak",
    "badger",
    "falcon",
    "rabbit",
    "turtle",
    "bear",
    "beaver",
    "bison",
    "camel",
    "cobra",
    "crane",
    "deer",
    "dolphin",
    "eagle",
    "ferret",
    "frog",
    "horse",
    "lemur",
    "lynx",
    "moose",
    "penguin",
)
ALIASES = tuple(
    Alias(name=f"{color}-{animal}", marker=marker, color=color)
    for color, marker in COLORS
    for animal in ANIMALS
)


def run_herdr(*args: str) -> subprocess.CompletedProcess[str]:
    command = [herdr_executable(), *args]
    try:
        return subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            check=False,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return subprocess.CompletedProcess(command, 1, "", str(error))


def report_command_failure(
    operation: str,
    result: subprocess.CompletedProcess[str],
) -> None:
    detail = result.stderr.strip() or result.stdout.strip()
    if not detail:
        detail = f"exit status {result.returncode}"
    print(f"agent labels: {operation} failed: {detail}", file=sys.stderr)


def decode_json(value: str) -> dict[str, Any]:
    try:
        decoded = json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return {}
    return decoded if isinstance(decoded, dict) else {}


def read_json_environment_variable(name: str) -> dict[str, Any]:
    value = os.environ.get(name)
    if value is None:
        return {}
    return decode_json(value)


def event_data() -> dict[str, Any]:
    payload = read_json_environment_variable("HERDR_PLUGIN_EVENT_JSON")
    data = payload.get("data")
    return data if isinstance(data, dict) else payload


def event_name() -> str:
    payload = read_json_environment_variable("HERDR_PLUGIN_EVENT_JSON")
    return str(os.environ.get("HERDR_PLUGIN_EVENT") or payload.get("event") or "")


def context_pane_id() -> str:
    context = read_json_environment_variable("HERDR_PLUGIN_CONTEXT_JSON")
    return str(
        context.get("focused_pane_id")
        or context.get("pane_id")
        or os.environ.get("HERDR_PANE_ID")
        or ""
    )


def fetch_agent_info(pane_id: str) -> dict[str, Any]:
    result = run_herdr("agent", "get", pane_id)
    if result.returncode != 0:
        report_command_failure(f"agent get {pane_id}", result)
        return {}
    payload = decode_json(result.stdout)
    result_data = payload.get("result")
    if not isinstance(result_data, dict):
        print(
            f"agent labels: agent get {pane_id} returned an invalid response",
            file=sys.stderr,
        )
        return {}
    agent_data = result_data.get("agent")
    if not isinstance(agent_data, dict):
        print(
            f"agent labels: agent get {pane_id} returned no agent record",
            file=sys.stderr,
        )
        return {}
    return agent_data


def fetch_agents() -> list[dict[str, Any]] | None:
    result = run_herdr("agent", "list")
    if result.returncode != 0:
        report_command_failure("agent list", result)
        return None
    payload = decode_json(result.stdout)
    result_data = payload.get("result")
    raw_agents = result_data.get("agents") if isinstance(result_data, dict) else None
    if not isinstance(raw_agents, list):
        print("agent labels: agent list returned an invalid response", file=sys.stderr)
        return None
    return [agent for agent in raw_agents if isinstance(agent, dict)]


def clear_agent_label_metadata(pane_id: str) -> bool:
    succeeded = True
    for source in (SOURCE, LEGACY_SOURCE):
        result = run_herdr(
            "pane",
            "report-metadata",
            pane_id,
            "--source",
            source,
            "--clear-display-agent",
            "--clear-token",
            "alias",
            "--clear-token",
            "color",
            "--clear-token",
            "recap",
        )
        if result.returncode != 0:
            report_command_failure(
                f"clear label metadata for {pane_id} from {source}",
                result,
            )
            succeeded = False
    return succeeded


def report_agent_label_metadata(
    pane_id: str,
    agent: str,
    alias: str,
    marker: str,
    color: str,
) -> bool:
    return report_agent_metadata(
        pane_id, agent, alias, marker, color,
        runner=lambda command: run_herdr(*command),
        on_failure=report_command_failure,
    )


def candidates(seed: str) -> Iterator[Alias]:
    digest = hashlib.sha256(seed.encode()).digest()
    start = int.from_bytes(digest[:SEED_HASH_BYTES], "big") % len(ALIASES)
    for offset in range(len(ALIASES)):
        index = (start + offset * CANDIDATE_STRIDE) % len(ALIASES)
        yield ALIASES[index]


def assign_label(pane_id: str) -> int:
    info = fetch_agent_info(pane_id)
    if not info:
        return 0
    agent = str(info.get("agent") or "")
    if not agent:
        return 0
    name = str(info.get("name") or info.get("agent_name") or "")
    if name:
        palette = next((alias for alias in ALIASES if alias.name == name), None)
        return 0 if report_agent_label_metadata(
            pane_id, agent, name,
            palette.marker if palette else "", palette.color if palette else "",
        ) else 1

    session_data = info.get("agent_session")
    session = session_data if isinstance(session_data, dict) else {}
    terminal_id = str(info.get("terminal_id") or pane_id)
    session_id = str(session.get("value") or "")
    seed = f"{terminal_id}:{session_id}"

    for candidate in candidates(seed):
        renamed = run_herdr("agent", "rename", pane_id, candidate.name)
        if renamed.returncode == 0:
            metadata_reported = report_agent_label_metadata(
                pane_id,
                agent,
                candidate.name,
                candidate.marker,
                candidate.color,
            )
            print(candidate.name)
            return 0 if metadata_reported else 1
        if "agent_name_taken" in renamed.stderr:
            current = fetch_agent_info(pane_id)
            current_name = str(current.get("name") or current.get("agent_name") or "")
            if current_name:
                palette = next((alias for alias in ALIASES if alias.name == current_name), None)
                reported = report_agent_label_metadata(
                    pane_id, str(current.get("agent") or agent), current_name,
                    palette.marker if palette else "", palette.color if palette else "",
                )
                return 0 if reported else 1
            continue
        report_command_failure(f"agent rename {pane_id}", renamed)
        return 1
    print(f"agent labels: no alias is available for {pane_id}", file=sys.stderr)
    return 1


def reconcile_labels() -> int:
    agents = fetch_agents()
    if agents is None:
        return 1

    result = 0
    visited: set[str] = set()
    for info in agents:
        pane_id = str(info.get("pane_id") or "")
        if (
            not pane_id
            or pane_id in visited
            or not info.get("agent")
        ):
            continue
        visited.add(pane_id)
        if assign_label(pane_id) != 0:
            result = 1
    return result


def handle_event() -> int:
    data = event_data()
    pane_id = str(data.get("pane_id") or "")
    if not pane_id:
        return 0
    # `event` was originally dedicated to pane.agent_detected and remains useful
    # for direct/manual invocations that provide only the raw event data.
    current_event = event_name() or "pane.agent_detected"
    if current_event == "pane.agent_detected":
        if data.get("released") or not data.get("agent"):
            return 0 if clear_agent_label_metadata(pane_id) else 1
        return assign_label(pane_id)
    if current_event == "pane.agent_status_changed":
        return assign_label(pane_id)
    return 0


def parse_cli_arguments(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Assign a readable label to an unnamed Herdr agent."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser(
        "event",
        help="Handle an agent detection event from Herdr.",
    )

    subparsers.add_parser(
        "reconcile",
        help="Assign labels to every currently unnamed Herdr agent.",
    )

    label_parser = subparsers.add_parser(
        "label",
        help="Assign a label to an agent pane.",
    )
    label_parser.add_argument(
        "pane_id",
        nargs="?",
        help="Pane ID to label; defaults to the focused pane.",
    )

    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    arguments = parse_cli_arguments(argv)

    if arguments.command == "event":
        return handle_event()

    if arguments.command == "reconcile":
        return reconcile_labels()

    pane_id = arguments.pane_id or context_pane_id()
    if not pane_id:
        return 1

    return assign_label(pane_id)


if __name__ == "__main__":
    raise SystemExit(main())
