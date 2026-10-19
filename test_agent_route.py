import ham_test_support  # Protect the live ledger before product imports.
import base64
import hashlib
import json
import unittest
from dataclasses import replace

import agent_directory
import agent_route


def agent(
    *,
    host: str = "local",
    name: str = "white-bison",
    pane_id: str = "w1:p1",
    local: bool = True,
    session_id: str = "session-1",
    route_target: str = "",
) -> agent_directory.AgentRecord:
    return agent_directory.AgentRecord(
        host=host,
        name=name,
        pane_id=pane_id,
        workspace_id="w1",
        workspace_label="project",
        status="idle",
        session_id=session_id,
        cwd="/work/project",
        local=local,
        revision=3,
        agent_kind="codex",
        terminal_id=f"terminal-{pane_id}",
        route_target=route_target,
    )


def decode_payload(route: str) -> dict[str, object]:
    padding = "=" * (-len(route) % 4)
    return json.loads(base64.urlsafe_b64decode(route + padding))


def legacy_route(recipient: agent_directory.AgentRecord) -> str:
    payload = {
        "host": "local" if recipient.local else recipient.host,
        "occupant": hashlib.sha256(recipient.identity.encode("utf-8")).hexdigest(),
        "version": 1,
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return base64.urlsafe_b64encode(encoded).decode("ascii").rstrip("=")


class AgentRouteTest(unittest.TestCase):
    def assert_route_expired(self, route: str, agents):
        with self.assertRaises(agent_route.AgentRouteError) as raised:
            agent_route.resolve_agent_route(route, agents)
        self.assertEqual(raised.exception.code, "route_expired")

    def test_v2_token_exposes_no_raw_session_or_cwd(self):
        recipient = agent(session_id="private-session-id")
        route = agent_route.encode_agent_route(recipient)
        payload = decode_payload(route)

        self.assertEqual(payload["version"], 2)
        self.assertEqual(payload["host"], "local")
        self.assertEqual(len(payload["occupant"]), 64)
        self.assertNotIn("private-session-id", route)
        self.assertNotIn("/work/project", json.dumps(payload))
        self.assertNotIn("session_id", json.dumps(payload))

    def test_v1_exact_route_is_read_and_upgraded_to_v2(self):
        recipient = agent()
        route = legacy_route(recipient)

        resolution = agent_route.resolve_agent_route(route, [recipient])

        self.assertEqual(resolution.agent, recipient)
        self.assertTrue(resolution.route_refreshed)
        self.assertEqual(decode_payload(resolution.route)["version"], 2)

    def test_v1_stale_route_does_not_refresh(self):
        original = agent()
        replacement = replace(original, session_id="replacement-session")
        self.assert_route_expired(legacy_route(original), [replacement])

    def test_exact_occupant_wins_before_ambiguous_label_refresh(self):
        original = agent()
        duplicate_label = agent(pane_id="w1:p2", session_id="session-2")
        route = agent_route.encode_agent_route(original)

        resolution = agent_route.resolve_agent_route(
            route, [original, duplicate_label]
        )

        self.assertEqual(resolution.agent, original)
        self.assertFalse(resolution.route_refreshed)

    def test_registered_label_refreshes_with_basic_continuity(self):
        original = agent()
        replacement = replace(
            original,
            session_id="replacement-session",
            revision=original.revision + 1,
        )
        route = agent_route.encode_agent_route(original)

        resolution = agent_route.resolve_agent_route(route, [replacement])

        self.assertEqual(resolution.agent, replacement)
        self.assertTrue(resolution.route_refreshed)
        self.assertEqual(
            agent_route.resolve_agent_route(resolution.route, [replacement]).agent,
            replacement,
        )

    def test_registered_label_refresh_requires_pane_workspace_and_kind(self):
        original = agent()
        route = agent_route.encode_agent_route(original)
        replacements = (
            replace(original, session_id="new", pane_id="w1:p2"),
            replace(original, session_id="new", workspace_id="w2"),
            replace(original, session_id="new", agent_kind="claude"),
        )
        for replacement in replacements:
            with self.subTest(replacement=replacement):
                self.assert_route_expired(route, [replacement])

    def test_registered_label_must_be_unique_on_the_recorded_host(self):
        original = agent()
        route = agent_route.encode_agent_route(original)
        first = replace(original, session_id="new-1")
        second = replace(original, session_id="new-2")

        self.assert_route_expired(route, [first, second])

    def test_refresh_never_uses_a_matching_agent_from_another_host(self):
        original = agent(
            host="macbook-pro",
            local=False,
            session_id="remote-session",
        )
        route = agent_route.encode_agent_route(original)
        other_host = replace(
            original,
            host="other-host",
            session_id="replacement-session",
        )

        self.assert_route_expired(route, [other_host])

    def test_session_bound_unlabeled_route_expires_on_occupant_change(self):
        original = agent(name="")
        replacement = replace(original, session_id="replacement-session")

        self.assert_route_expired(
            agent_route.encode_agent_route(original), [replacement]
        )

    def test_sessionless_unlabeled_route_uses_strict_continuity(self):
        original = agent(name="", session_id="")
        replacement = replace(original, session_id="newly-observed-session")
        route = agent_route.encode_agent_route(original)

        resolution = agent_route.resolve_agent_route(route, [replacement])

        self.assertEqual(resolution.agent, replacement)
        self.assertTrue(resolution.route_refreshed)

    def test_sessionless_unlabeled_route_stays_expired_when_ambiguous(self):
        original = agent(name="", session_id="")
        route = agent_route.encode_agent_route(original)
        candidates = [
            replace(original, session_id="observed-1"),
            replace(original, session_id="observed-2"),
        ]

        self.assert_route_expired(route, candidates)

    def test_display_label_requires_strict_revision_continuity(self):
        original = agent(
            name="purple-koala",
            session_id="",
            route_target="w1:p1",
        )
        route = agent_route.encode_agent_route(original)
        continuous = replace(original, session_id="newly-observed-session")
        changed = replace(continuous, revision=original.revision + 1)

        self.assertEqual(
            agent_route.resolve_agent_route(route, [continuous]).agent,
            continuous,
        )
        self.assert_route_expired(route, [changed])

    def test_session_bound_display_label_expires_on_occupant_change(self):
        original = agent(name="purple-koala", route_target="w1:p1")
        replacement = replace(original, session_id="replacement-session")

        self.assert_route_expired(
            agent_route.encode_agent_route(original), [replacement]
        )


class InvalidRouteTest(unittest.TestCase):
    def test_malformed_continuity_returns_a_route_error(self):
        record = agent()
        original = decode_payload(agent_route.encode_agent_route(record))
        for value in ([], {}, None, 3):
            with self.subTest(kind=value):
                payload = dict(original)
                payload["continuity"] = {**original["continuity"], "kind": value}
                malformed = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()
                with self.assertRaises(agent_route.AgentRouteError) as raised:
                    agent_route.resolve_agent_route(malformed, [record])
                self.assertEqual(raised.exception.code, "invalid_route")

    def test_excessive_json_nesting_returns_a_route_error(self):
        malformed = base64.urlsafe_b64encode(("[" * 1100 + "]" * 1100).encode()).decode()
        with self.assertRaises(agent_route.AgentRouteError) as raised:
            agent_route.route_host(malformed)
        self.assertEqual(raised.exception.code, "invalid_route")


if __name__ == "__main__":
    unittest.main()
