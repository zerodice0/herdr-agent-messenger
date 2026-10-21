"""Opaque, refreshable route tokens for current Herdr pane occupants."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
from dataclasses import dataclass
from typing import Any, Sequence

from ham.agents.directory import AgentRecord


LEGACY_ROUTE_TOKEN_VERSION = 1
ROUTE_TOKEN_VERSION = 2
MAX_ROUTE_TOKEN_LENGTH = 4096
_HEX_DIGITS = frozenset("0123456789abcdef")
_LABEL_KINDS = frozenset({"registered", "display", "unlabeled"})


class AgentRouteError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class RouteResolution:
    agent: AgentRecord
    route_refreshed: bool
    route: str


@dataclass(frozen=True)
class _DecodedRoute:
    version: int
    host: str
    occupant: str
    continuity: dict[str, Any] | None = None


def _fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _occupant_fingerprint(agent: AgentRecord) -> str:
    return _fingerprint(agent.identity)


def _continuity_fingerprint(field: str, value: str) -> str:
    return _fingerprint(f"herdr-route-v2:{field}\0{value}")


def _label_kind(agent: AgentRecord) -> str:
    if not agent.name:
        return "unlabeled"
    if agent.route_target and agent.route_target != agent.name:
        return "display"
    return "registered"


def _continuity_payload(agent: AgentRecord) -> dict[str, Any]:
    return {
        "agent_kind": _continuity_fingerprint("agent-kind", agent.agent_kind),
        "basic_ready": bool(
            agent.pane_id and agent.workspace_id and agent.agent_kind
        ),
        "cwd": _continuity_fingerprint("cwd", agent.cwd),
        "kind": _label_kind(agent),
        "label": _continuity_fingerprint("label", agent.name),
        "pane": _continuity_fingerprint("pane", agent.pane_id),
        "revision": _continuity_fingerprint("revision", str(agent.revision)),
        "session_bound": bool(agent.session_id),
        "strict_ready": bool(
            agent.pane_id
            and agent.workspace_id
            and agent.agent_kind
            and agent.terminal_id
            and agent.cwd
            and agent.revision > 0
        ),
        "target": _continuity_fingerprint("target", agent.target),
        "terminal": _continuity_fingerprint("terminal", agent.terminal_id),
        "workspace": _continuity_fingerprint("workspace", agent.workspace_id),
    }


def encode_agent_route(agent: AgentRecord) -> str:
    """Encode an agent route without exposing session IDs or working paths."""

    payload = {
        "continuity": _continuity_payload(agent),
        "host": "local" if agent.local else agent.host,
        "occupant": _occupant_fingerprint(agent),
        "version": ROUTE_TOKEN_VERSION,
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return base64.urlsafe_b64encode(encoded).decode("ascii").rstrip("=")


def _is_fingerprint(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in _HEX_DIGITS for character in value)
    )


def _invalid_route() -> AgentRouteError:
    return AgentRouteError("invalid_route", "The agent route token is invalid.")


def _validated_continuity(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise _invalid_route()
    fingerprint_fields = {
        "agent_kind",
        "cwd",
        "label",
        "pane",
        "revision",
        "target",
        "terminal",
        "workspace",
    }
    expected_fields = fingerprint_fields | {
        "basic_ready",
        "kind",
        "session_bound",
        "strict_ready",
    }
    if set(value) != expected_fields:
        raise _invalid_route()
    if any(not _is_fingerprint(value[field]) for field in fingerprint_fields):
        raise _invalid_route()
    if not isinstance(value["kind"], str) or value["kind"] not in _LABEL_KINDS:
        raise _invalid_route()
    if any(
        not isinstance(value[field], bool)
        for field in ("basic_ready", "session_bound", "strict_ready")
    ):
        raise _invalid_route()
    return value


def _decode_agent_route(route: str) -> _DecodedRoute:
    if not isinstance(route, str) or not route or len(route) > MAX_ROUTE_TOKEN_LENGTH:
        raise _invalid_route()
    try:
        padding = "=" * (-len(route) % 4)
        decoded = base64.b64decode(
            route + padding,
            altchars=b"-_",
            validate=True,
        )
        payload = json.loads(decoded)
    except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError):
        raise _invalid_route() from None
    if not isinstance(payload, dict):
        raise _invalid_route()

    version = payload.get("version")
    host = payload.get("host")
    occupant = payload.get("occupant")
    if (
        not isinstance(version, int)
        or isinstance(version, bool)
        or not isinstance(host, str)
        or not host
        or not _is_fingerprint(occupant)
    ):
        raise _invalid_route()
    if version == LEGACY_ROUTE_TOKEN_VERSION:
        if set(payload) != {"host", "occupant", "version"}:
            raise _invalid_route()
        return _DecodedRoute(version, host, occupant)
    if version == ROUTE_TOKEN_VERSION:
        if set(payload) != {"continuity", "host", "occupant", "version"}:
            raise _invalid_route()
        return _DecodedRoute(
            version,
            host,
            occupant,
            _validated_continuity(payload.get("continuity")),
        )
    raise _invalid_route()


def route_host(route: str) -> str:
    return _decode_agent_route(route).host


def _matches_basic_continuity(
    agent: AgentRecord,
    continuity: dict[str, Any],
) -> bool:
    if not continuity["basic_ready"]:
        return False
    candidate = _continuity_payload(agent)
    return all(
        continuity[field] == candidate[field]
        for field in ("pane", "workspace", "agent_kind")
    )


def _matches_strict_continuity(
    agent: AgentRecord,
    continuity: dict[str, Any],
) -> bool:
    if not continuity["strict_ready"] or continuity["session_bound"]:
        return False
    candidate = _continuity_payload(agent)
    if not candidate["strict_ready"]:
        return False
    return all(
        continuity[field] == candidate[field]
        for field in (
            "kind",
            "label",
            "pane",
            "workspace",
            "agent_kind",
            "terminal",
            "cwd",
            "target",
            "revision",
        )
    )


def _expired_route() -> AgentRouteError:
    return AgentRouteError(
        "route_expired",
        "The selected agent is no longer the safely identifiable pane occupant.",
    )


def resolve_agent_route(
    route: str,
    agents: Sequence[AgentRecord],
) -> RouteResolution:
    """Resolve an exact occupant, or conservatively refresh a v2 route."""

    decoded = _decode_agent_route(route)
    current_agents = [
        agent
        for agent in agents
        if (
            (decoded.host == "local" and agent.local)
            or (
                decoded.host != "local"
                and not agent.local
                and agent.host == decoded.host
            )
        )
    ]
    exact_matches = [
        agent
        for agent in current_agents
        if _occupant_fingerprint(agent) == decoded.occupant
    ]
    if len(exact_matches) > 1:
        raise AgentRouteError(
            "route_ambiguous",
            "The selected agent route matches more than one current occupant.",
        )
    if exact_matches:
        refreshed_route = encode_agent_route(exact_matches[0])
        return RouteResolution(
            exact_matches[0],
            route_refreshed=refreshed_route != route,
            route=refreshed_route,
        )

    continuity = decoded.continuity
    if decoded.version != ROUTE_TOKEN_VERSION or continuity is None:
        raise _expired_route()

    if continuity["kind"] == "registered":
        label_matches = [
            agent
            for agent in current_agents
            if _label_kind(agent) == "registered"
            and _continuity_fingerprint("label", agent.name)
            == continuity["label"]
        ]
        if len(label_matches) != 1 or not _matches_basic_continuity(
            label_matches[0], continuity
        ):
            raise _expired_route()
        refreshed = label_matches[0]
    else:
        strict_matches = [
            agent
            for agent in current_agents
            if _matches_strict_continuity(agent, continuity)
        ]
        if len(strict_matches) != 1:
            raise _expired_route()
        refreshed = strict_matches[0]

    return RouteResolution(
        refreshed,
        route_refreshed=True,
        route=encode_agent_route(refreshed),
    )
