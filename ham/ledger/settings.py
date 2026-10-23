"""Replicated repository settings and host-local Herdr workspace coordination."""

from __future__ import annotations

import json
import subprocess
import time
from collections.abc import Callable, Sequence
from typing import Any, Mapping

from ham.ledger.store import LedgerError, LedgerStore, json_text, queue_outbox

DEFAULT_THRESHOLDS = {"idle_minutes": 3, "coalesce_seconds": 60, "wakes_per_hour": 4, "max_hop": 10}
REPO_FIELDS = frozenset({"home", "mode", "thresholds"})
WORKSPACE_FIELDS = frozenset({"main", "members"})
LEGACY_FIELDS = WORKSPACE_FIELDS | {"computed_main"}
FIELDS = REPO_FIELDS | LEGACY_FIELDS


def _target(value: Any) -> bool:
    return value is None or (isinstance(value, dict) and set(value) == {"host", "terminal_id"}
                             and all(isinstance(v, str) and v for v in value.values()))


def validate_setting(value: Mapping[str, Any]) -> dict[str, Any]:
    required = {"repo_id", "field", "value", "version", "origin_host", "changed_by", "changed_at"}
    if set(value) not in (required, required | {"expected_main"}) or value.get("field") not in FIELDS:
        raise LedgerError("invalid_setting", "Setting fields do not match protocol 1.")
    result = dict(value)
    if (not isinstance(result["repo_id"], str) or not result["repo_id"] or
            not isinstance(result["origin_host"], str) or not result["origin_host"] or
            not isinstance(result["version"], int) or isinstance(result["version"], bool) or result["version"] < 1):
        raise LedgerError("invalid_setting", "Setting repository, origin and positive version are required.")
    field, data = result["field"], result["value"]
    valid = True
    if field == "home":
        valid = result["version"] == 1 and data == result["origin_host"]
    elif field == "mode":
        valid = isinstance(data, str) and data in {"off", "manual", "auto"}
    elif field == "main":
        valid = _target(data)
    elif field == "members":
        valid = isinstance(data, list) and all(item is not None and _target(item) for item in data)
        if valid:
            valid = len({json_text(item) for item in data}) == len(data)
    elif field == "thresholds":
        valid = isinstance(data, dict) and set(data) == set(DEFAULT_THRESHOLDS) and all(
            isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= 10000 for v in data.values())
    elif field == "computed_main":
        valid = (isinstance(data, dict) and set(data) == {"target", "home_stamp"} and _target(data["target"])
                 and isinstance(data["home_stamp"], dict) and set(data["home_stamp"]) == {"version", "origin_host", "value"})
    if "expected_main" in result:
        expected = result["expected_main"]
        valid = (valid and field == "main" and data is None and result["changed_by"] == "main_host" and
                 isinstance(expected, dict) and set(expected) == {"version", "origin_host", "value"} and
                 isinstance(expected["value"], dict) and expected["value"].get("host") == result["origin_host"])
    if not valid or result["changed_by"] not in {"user", "home", "main_host"}:
        raise LedgerError("invalid_setting", f"Invalid value for {field}.")
    if not isinstance(result["changed_at"], (int, float)):
        raise LedgerError("invalid_setting", "Setting timestamp is required.")
    return result


class RepoSettings:
    def __init__(self, store: LedgerStore, *,
                 environment: Mapping[str, str] | None = None,
                 display_runner: Callable[[Sequence[str]], subprocess.CompletedProcess[str]] | None = None) -> None:
        self.store = store
        self.environment = environment
        self.display_runner = display_runner

    def _refresh_display(self, environment: Mapping[str, str] | None = None) -> dict[str, list[str]]:
        from ham.ledger.display import refresh
        return refresh(self.store, self.environment if environment is None else environment,
                       runner=self.display_runner)

    def _display_result(self, environment: Mapping[str, str] | None = None) -> dict[str, Any]:
        display = self._refresh_display(environment)
        result: dict[str, Any] = {"display": display}
        if display["failed"]:
            result["warnings"] = [{"code": "display_refresh_failed", "panes": display["failed"]}]
        return result

    @staticmethod
    def fields(connection: Any, repo_id: str) -> dict[str, dict[str, Any]]:
        return {row["field"]: {**dict(row), "value": json.loads(row["value"])} for row in connection.execute(
            "SELECT * FROM repo_settings WHERE repo_id=?", (repo_id,))}

    @staticmethod
    def scope_workspace(connection: Any, repo_id: str, host: str, workspace_id: str) -> str:
        """Resolve only Herdr's host-local linked-worktree metadata, never Git paths."""
        workspace = connection.execute(
            "SELECT repo_key,is_linked_worktree FROM workspaces WHERE host=? AND workspace_id=?",
            (host, workspace_id)).fetchone()
        if workspace is None or not workspace["repo_key"] or not workspace["is_linked_worktree"]:
            return workspace_id
        anchor = connection.execute(
            "SELECT workspace_id FROM workspaces WHERE host=? AND repo_key=? AND is_linked_worktree=0 "
            "ORDER BY workspace_id LIMIT 1", (host, workspace["repo_key"])).fetchone()
        return anchor["workspace_id"] if anchor is not None else workspace_id

    @staticmethod
    def scope_agents(connection: Any, repo_id: str, host: str, workspace_id: str) -> list[dict[str, Any]]:
        anchor = RepoSettings.scope_workspace(connection, repo_id, host, workspace_id)
        return [dict(row) for row in connection.execute(
            "SELECT * FROM agents WHERE repo_id=? AND host=? AND active=1 ORDER BY workspace_id,terminal_id",
            (repo_id, host))
            if RepoSettings.scope_workspace(connection, repo_id, host, row["workspace_id"]) == anchor]

    @staticmethod
    def workspace_fields(connection: Any, repo_id: str, host: str,
                         workspace_id: str) -> dict[str, dict[str, Any]]:
        workspace_id = RepoSettings.scope_workspace(connection, repo_id, host, workspace_id)
        return {row["field"]: {**dict(row), "value": json.loads(row["value"])} for row in connection.execute(
            "SELECT * FROM workspace_settings WHERE repo_id=? AND host=? AND workspace_id=?",
            (repo_id, host, workspace_id))}

    def show(self, repo_id: str, host: str | None = None, workspace_id: str | None = None) -> dict[str, Any]:
        requested_workspace_id = workspace_id
        with self.store.read() as connection:
            fields = self.fields(connection, repo_id)
            if host is not None and workspace_id is not None:
                workspace_id = self.scope_workspace(connection, repo_id, host, workspace_id)
            scoped = self.workspace_fields(connection, repo_id, host, workspace_id) if host is not None and workspace_id is not None else {}
            conflicts = [dict(row) for row in connection.execute(
                "SELECT * FROM setting_conflicts WHERE repo_id=? ORDER BY seq", (repo_id,))]
            pending = connection.execute("SELECT count(*) FROM outbox WHERE kind='setting' AND state='pending' "
                                         "AND json_extract(payload,'$.repo_id')=?", (repo_id,)).fetchone()[0]
            mode = fields.get("mode", {}).get("value", "off")
            main = main_source = computed = None
            if host is not None and workspace_id is not None:
                main, main_source, computed = self._election(connection, repo_id, host, workspace_id, mode)
        return {"repo_id": repo_id, "host": host, "workspace_id": workspace_id,
                "requested_workspace_id": requested_workspace_id,
                "home_host": fields.get("home", {}).get("value"),
                "mode": fields.get("mode", {}).get("value", "off"), "settings": {**fields, **scoped},
                "main": main, "main_source": main_source,
                "members": scoped.get("members", {}).get("value", []), "computed_main": computed,
                "conflicts": conflicts, "propagation_pending": pending}

    @staticmethod
    def _next_version(connection: Any, repo_id: str) -> int:
        return connection.execute("SELECT coalesce(max(version),0)+1 FROM repo_settings WHERE repo_id=?", (repo_id,)).fetchone()[0]

    def _publish(self, connection: Any, value: Mapping[str, Any], *, exclude: str | None = None) -> None:
        if value["repo_id"].startswith("local:") or value["field"] not in REPO_FIELDS:
            return
        for peer in connection.execute("SELECT canonical_name FROM peers ORDER BY canonical_name").fetchall():
            if peer[0] != exclude:
                queue_outbox(connection, peer[0], "setting", value)

    def _apply(self, connection: Any, value: Mapping[str, Any], *, source: str | None = None) -> bool:
        incoming = validate_setting(value)
        repo_id, field = incoming["repo_id"], incoming["field"]
        # Protocol-1 peers may still publish their old repository-wide roles.
        # Acknowledge without storing or forwarding them.
        if field in LEGACY_FIELDS:
            return False
        old = self.fields(connection, repo_id).get(field)
        if old is not None:
            if (old["version"], old["origin_host"]) == (incoming["version"], incoming["origin_host"]):
                if old["value"] != incoming["value"]:
                    raise LedgerError("setting_conflict", "A setting stamp cannot identify two values.")
                return False
            incoming_wins = (incoming["version"], incoming["origin_host"]) > (old["version"], old["origin_host"])
            if old["version"] == incoming["version"] and old["value"] != incoming["value"]:
                winner, loser = (incoming, old) if incoming_wins else (old, incoming)
                exists = connection.execute(
                    "SELECT 1 FROM setting_conflicts WHERE repo_id=? AND field=? AND version=? AND winner_host=? "
                    "AND loser_host=? AND winner_value=? AND loser_value=?",
                    (repo_id, field, incoming["version"], winner["origin_host"], loser["origin_host"],
                     json_text(winner["value"]), json_text(loser["value"]))).fetchone()
                if not exists:
                    connection.execute("INSERT INTO setting_conflicts(repo_id,field,version,winner_host,loser_host,winner_value,loser_value,at) "
                                       "VALUES (?,?,?,?,?,?,?,?)", (repo_id, field, incoming["version"], winner["origin_host"],
                                       loser["origin_host"], json_text(winner["value"]), json_text(loser["value"]), time.time()))
            if not incoming_wins:
                return False
        connection.execute("INSERT OR IGNORE INTO repos VALUES (?,NULL,?)", (repo_id, time.time()))
        connection.execute("INSERT INTO repo_settings VALUES (?,?,?,?,?,?,?) ON CONFLICT(repo_id,field) DO UPDATE SET "
                           "value=excluded.value,version=excluded.version,origin_host=excluded.origin_host,"
                           "changed_by=excluded.changed_by,changed_at=excluded.changed_at",
                           (repo_id, field, json_text(incoming["value"]), incoming["version"], incoming["origin_host"],
                            incoming["changed_by"], incoming["changed_at"]))
        if field == "home":
            connection.execute("UPDATE repos SET home_host=? WHERE repo_id=?", (incoming["value"], repo_id))
        self._publish(connection, incoming, exclude=source)
        return True

    def _seed_new_repo(self, connection: Any, repo_id: str, at: float) -> None:
        """Called only after registry insertion, never for already-known repositories."""
        if self.fields(connection, repo_id):
            return
        for field, value, origin in (("home", self.store.host, self.store.host),
                                     ("mode", "auto", "!default")):
            self._apply(connection, {"repo_id": repo_id, "field": field, "value": value,
                                     "version": 1, "origin_host": origin,
                                     "changed_by": "home", "changed_at": at})

    def _mutate_workspace(self, connection: Any, repo_id: str, field: str, value: Any,
                          host: str | None, workspace_id: str | None) -> None:
        if host != self.store.host or not isinstance(workspace_id, str) or not workspace_id:
            raise LedgerError("invalid_scope", "Workspace settings require a local host and workspace ID.")
        workspace_id = self.scope_workspace(connection, repo_id, host, workspace_id)
        validate_setting({"repo_id": repo_id, "field": field, "value": value, "version": 1,
                          "origin_host": host, "changed_by": "user", "changed_at": time.time()})
        scoped = self.workspace_fields(connection, repo_id, host, workspace_id)
        existing = scoped.get("members", {}).get("value", [])
        for target in ([value] if value else []) if field == "main" else value:
            agent = connection.execute(
                "SELECT repo_id,workspace_id FROM agents WHERE host=? AND terminal_id=? AND active=1",
                (target["host"], target["terminal_id"])).fetchone()
            if (target["host"] != host or
                    (agent is not None and (agent["repo_id"] != repo_id or
                     self.scope_workspace(connection, repo_id, host, agent["workspace_id"]) != workspace_id)) or
                    (agent is None and (field == "main" or target not in existing))):
                raise LedgerError("target_out_of_scope", f"Target must be an active agent in role scope {workspace_id} on {host}.")
        connection.execute("INSERT OR IGNORE INTO repos VALUES (?,NULL,?)", (repo_id, time.time()))
        version = max((row["version"] for row in scoped.values()), default=0) + 1
        connection.execute("INSERT INTO workspace_settings VALUES (?,?,?,?,?,?,?,?) "
                           "ON CONFLICT(repo_id,host,workspace_id,field) DO UPDATE SET "
                           "value=excluded.value,version=excluded.version,changed_by=excluded.changed_by,changed_at=excluded.changed_at",
                           (repo_id, host, workspace_id, field, json_text(value), version, "user", time.time()))

    def mutate(self, repo_id: str, field: str, value: Any, *, host: str | None = None,
               workspace_id: str | None = None) -> dict[str, Any]:
        """Local user-TUI service. Intentionally not exposed by a mutation CLI."""
        if field not in {"mode", "main", "members", "thresholds"}:
            raise LedgerError("invalid_setting", "Only user settings can be changed here.")
        terminals: list[str] = []
        with self.store.transaction(terminals) as connection:
            if field in WORKSPACE_FIELDS:
                self._mutate_workspace(connection, repo_id, field, value, host, workspace_id)
                terminals.extend(agent["terminal_id"] for agent in
                                 self.scope_agents(connection, repo_id, host, workspace_id))
            else:
                fields = self.fields(connection, repo_id)
                if field == "mode" and value != "off" and "home" not in fields:
                    self._apply(connection, {"repo_id": repo_id, "field": "home", "value": self.store.host,
                                             "version": 1, "origin_host": self.store.host, "changed_by": "user", "changed_at": time.time()})
                self._apply(connection, {"repo_id": repo_id, "field": field, "value": value,
                                         "version": self._next_version(connection, repo_id), "origin_host": self.store.host,
                                         "changed_by": "user", "changed_at": time.time()})
                terminals.extend(row["terminal_id"] for row in connection.execute(
                    "SELECT terminal_id FROM agents WHERE repo_id=? AND host=? AND active=1",
                    (repo_id, self.store.host)))
        return {**self.show(repo_id, host, workspace_id), **self._display_result()}

    def accept(self, setting: Mapping[str, Any], source: str) -> dict[str, Any]:
        incoming = validate_setting(setting)
        if incoming["field"] in LEGACY_FIELDS:
            return {"applied": False, "ignored": "workspace_local", **self.show(incoming["repo_id"])}
        if incoming["repo_id"].startswith("local:"):
            raise LedgerError("local_repo", "Host-local repositories do not propagate settings.")
        terminals: list[str] = []
        with self.store.transaction(terminals) as connection:
            changed = self._apply(connection, incoming, source=source)
            if changed and incoming["field"] in {"mode", "thresholds"}:
                terminals.extend(row["terminal_id"] for row in connection.execute(
                    "SELECT terminal_id FROM agents WHERE repo_id=? AND host=? AND active=1",
                    (incoming["repo_id"], self.store.host)))
        return {"applied": changed, **self.show(incoming["repo_id"]), **self._display_result()}

    @staticmethod
    def _computed_from(agents: Sequence[Mapping[str, Any]], host: str, anchor: str) -> dict[str, str] | None:
        # An open empty anchor never elects a linked worktree, even if it is the
        # sole active agent. Ordinary ungrouped workspaces keep the sole-agent rule.
        candidates = (agents if len(agents) == 1 else [agent for agent in agents if agent["is_main_checkout"]])
        candidates = [agent for agent in candidates if agent["workspace_id"] == anchor]
        return {"host": host, "terminal_id": candidates[0]["terminal_id"]} if len(candidates) == 1 else None

    @staticmethod
    def _pin_from(connection: Any, repo_id: str, host: str, anchor: str,
                  agents: Sequence[Mapping[str, Any]]) -> dict[str, str] | None:
        explicit = RepoSettings.workspace_fields(connection, repo_id, host, anchor).get("main", {}).get("value")
        if explicit and explicit in [{"host": agent["host"], "terminal_id": agent["terminal_id"]} for agent in agents]:
            return explicit
        return None

    @staticmethod
    def _incumbent_from(connection: Any, repo_id: str, host: str, anchor: str,
                        agents: Sequence[Mapping[str, Any]]) -> dict[str, str] | None:
        row = connection.execute(
            "SELECT terminal_id FROM auto_main_incumbents WHERE repo_id=? AND host=? AND anchor_workspace_id=?",
            (repo_id, host, anchor)).fetchone()
        if row and any(agent["terminal_id"] == row["terminal_id"] and agent["workspace_id"] == anchor
                       for agent in agents):
            return {"host": host, "terminal_id": row["terminal_id"]}
        return None

    @staticmethod
    def _election(connection: Any, repo_id: str, host: str, workspace_id: str,
                  mode: str) -> tuple[dict[str, str] | None, str | None, dict[str, str] | None]:
        """Return (main, source, computed) from one scope scan; computed exists only in auto mode."""
        if mode == "off":
            return None, None, None
        anchor = RepoSettings.scope_workspace(connection, repo_id, host, workspace_id)
        agents = RepoSettings.scope_agents(connection, repo_id, host, anchor)
        computed = RepoSettings._computed_from(agents, host, anchor) if mode == "auto" else None
        pin = RepoSettings._pin_from(connection, repo_id, host, anchor, agents)
        if pin:
            return pin, "pinned", computed
        if mode != "auto":
            return None, None, None
        elected = RepoSettings._incumbent_from(connection, repo_id, host, anchor, agents) or computed
        return elected, "auto" if elected else None, computed

    def reconcile_auto_main(self, connection: Any, at: float) -> set[tuple[str, str, str]]:
        """Maintain local election memory only inside the authoritative registry transaction."""
        host = self.store.host
        stored = {(row["repo_id"], row["anchor_workspace_id"]): row["terminal_id"] for row in connection.execute(
            "SELECT repo_id,anchor_workspace_id,terminal_id FROM auto_main_incumbents WHERE host=?", (host,))}
        anchors: dict[str, str] = {}
        groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
        # One pass gives each active scope the same agent order as scope_agents().
        for row in connection.execute("SELECT * FROM agents WHERE host=? AND active=1 ORDER BY workspace_id,terminal_id", (host,)):
            anchor = anchors.get(row["workspace_id"])
            if anchor is None:
                anchor = anchors[row["workspace_id"]] = self.scope_workspace(
                    connection, row["repo_id"], host, row["workspace_id"])
            groups.setdefault((row["repo_id"], anchor), []).append(dict(row))
        modes: dict[str, str] = {}
        changed: set[tuple[str, str, str]] = set()
        for key in groups.keys() | stored.keys():
            repo_id, anchor = key
            agents = groups.get(key, [])
            terminal = stored.get(key)
            valid = terminal is not None and any(
                agent["terminal_id"] == terminal and agent["workspace_id"] == anchor for agent in agents)
            if terminal is not None and not valid:
                connection.execute("DELETE FROM auto_main_incumbents WHERE repo_id=? AND host=? "
                                   "AND anchor_workspace_id=?", (repo_id, host, anchor))
                changed.add((repo_id, host, anchor))
            if valid or not agents:
                continue
            if repo_id not in modes:
                modes[repo_id] = self.fields(connection, repo_id).get("mode", {}).get("value", "off")
            if modes[repo_id] != "auto" or self._pin_from(connection, repo_id, host, anchor, agents):
                continue
            candidate = self._computed_from(agents, host, anchor)
            if candidate is not None:
                connection.execute("INSERT INTO auto_main_incumbents VALUES (?,?,?,?,?)",
                                   (repo_id, host, anchor, candidate["terminal_id"], at))
                changed.add((repo_id, host, anchor))
        return changed

    def main(self, connection: Any, repo_id: str, host: str, workspace_id: str) -> dict[str, str] | None:
        mode = self.fields(connection, repo_id).get("mode", {}).get("value", "off")
        return self._election(connection, repo_id, host, workspace_id, mode)[0]

    def role(self, agent: Mapping[str, Any], connection: Any = None) -> str | None:
        if connection is None:
            with self.store.read() as current:
                return self.role(agent, connection=current)
        fields = self.fields(connection, agent["repo_id"])
        mode = fields.get("mode", {}).get("value", "off")
        if mode == "off" or not agent["active"]:
            return None
        target = {"host": agent["host"], "terminal_id": agent["terminal_id"]}
        if self.main(connection, agent["repo_id"], agent["host"], agent["workspace_id"]) == target:
            return "main"
        scoped = self.workspace_fields(connection, agent["repo_id"], agent["host"], agent["workspace_id"])
        return "worker" if mode == "auto" or target in scoped.get("members", {}).get("value", []) else None
