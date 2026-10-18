"""Per-field repository settings and registration defaults, not an agent mutation CLI."""

from __future__ import annotations

import json
import subprocess
import time
from collections.abc import Callable, Sequence
from typing import Any, Mapping

from ledger_store import LedgerError, LedgerStore, json_text, queue_outbox

DEFAULT_THRESHOLDS = {"idle_minutes": 3, "coalesce_seconds": 30, "wakes_per_hour": 6, "max_hop": 10}
FIELDS = frozenset({"home", "mode", "main", "members", "thresholds", "computed_main"})


def stamp(setting: Mapping[str, Any]) -> dict[str, Any]:
    return {key: setting[key] for key in ("version", "origin_host", "value")}


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
        from ledger_display import refresh
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
        result = {row["field"]: {**dict(row), "value": json.loads(row["value"])} for row in connection.execute(
            "SELECT * FROM repo_settings WHERE repo_id=?", (repo_id,))}
        for field, setting in result.items():
            condition = connection.execute("SELECT expected_main FROM setting_conditions WHERE repo_id=? AND field=? "
                                           "AND version=? AND origin_host=?",
                                           (repo_id, field, setting["version"], setting["origin_host"])).fetchone()
            if condition:
                setting["expected_main"] = json.loads(condition[0])
        return result

    def show(self, repo_id: str) -> dict[str, Any]:
        with self.store.read() as connection:
            fields = self.fields(connection, repo_id)
            conflicts = [dict(row) for row in connection.execute(
                "SELECT * FROM setting_conflicts WHERE repo_id=? ORDER BY seq", (repo_id,))]
            pending = connection.execute("SELECT count(*) FROM outbox WHERE kind='setting' AND state='pending' "
                                         "AND json_extract(payload,'$.repo_id')=?", (repo_id,)).fetchone()[0]
        return {"repo_id": repo_id, "home_host": fields.get("home", {}).get("value"),
                "mode": fields.get("mode", {}).get("value", "off"), "settings": fields,
                "conflicts": conflicts, "propagation_pending": pending}

    @staticmethod
    def _next_version(connection: Any, repo_id: str) -> int:
        return connection.execute("SELECT coalesce(max(version),0)+1 FROM repo_settings WHERE repo_id=?", (repo_id,)).fetchone()[0]

    def _publish(self, connection: Any, value: Mapping[str, Any], *, exclude: str | None = None) -> None:
        if value["repo_id"].startswith("local:"):
            return
        for peer in connection.execute("SELECT canonical_name FROM peers ORDER BY canonical_name").fetchall():
            if peer[0] != exclude:
                queue_outbox(connection, peer[0], "setting", value)

    def _apply(self, connection: Any, value: Mapping[str, Any], *, source: str | None = None) -> bool:
        incoming = validate_setting(value)
        repo_id, field = incoming["repo_id"], incoming["field"]
        fields = self.fields(connection, repo_id)
        home = fields.get("home")
        if field == "computed_main":
            if (home is None or incoming["origin_host"] != home["value"] or
                    incoming["value"]["home_stamp"] != stamp(home)):
                # A delayed former-home publication is inert, even at a larger version.
                return False
        old = fields.get(field)
        if "expected_main" in incoming and old is not None and stamp(old) != incoming["expected_main"]:
            # Exact duplicate clears are harmless, but a delayed close must
            # never clear a newer user designation, irrespective of tie order.
            if (old["version"], old["origin_host"], old["value"]) != (
                    incoming["version"], incoming["origin_host"], incoming["value"]):
                return False
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
        if "expected_main" in incoming:
            connection.execute("INSERT OR IGNORE INTO setting_conditions VALUES (?,?,?,?,?)",
                               (repo_id, field, incoming["version"], incoming["origin_host"], json_text(incoming["expected_main"])))
        if field == "home":
            connection.execute("UPDATE repos SET home_host=? WHERE repo_id=?", (incoming["value"], repo_id))
        if field == "members":
            connection.execute("DELETE FROM repo_members WHERE repo_id=?", (repo_id,))
            connection.executemany("INSERT INTO repo_members VALUES (?,?,?)",
                                   [(repo_id, item["host"], item["terminal_id"]) for item in incoming["value"]])
        self._publish(connection, incoming, exclude=source)
        return True

    def _seed_new_repo(self, connection: Any, repo_id: str, at: float) -> None:
        """Called only after registry insertion, never for already-known repositories."""
        if self.fields(connection, repo_id):
            return
        # Protocol 1 (including 0.9.1) accepts "home" attribution and positive
        # versions only. The reserved mode origin sorts below every canonical
        # hostname, so any explicit host-origin setting at version >= 1 wins,
        # even on an unchanged peer. Home keeps its existing proposal shape.
        for field, value, origin in (("home", self.store.host, self.store.host),
                                     ("mode", "auto", "!default")):
            self._apply(connection, {"repo_id": repo_id, "field": field, "value": value,
                                     "version": 1, "origin_host": origin,
                                     "changed_by": "home", "changed_at": at})

    def mutate(self, repo_id: str, field: str, value: Any) -> dict[str, Any]:
        """Local user-TUI service. Intentionally not exposed by a mutation CLI."""
        if field not in {"mode", "main", "members", "thresholds"}:
            raise LedgerError("invalid_setting", "Only user settings can be changed here.")
        with self.store.transaction() as connection:
            fields = self.fields(connection, repo_id)
            if field == "mode" and value != "off" and "home" not in fields:
                self._apply(connection, {"repo_id": repo_id, "field": "home", "value": self.store.host,
                                         "version": 1, "origin_host": self.store.host, "changed_by": "user", "changed_at": time.time()})
            setting = {"repo_id": repo_id, "field": field, "value": value,
                       "version": self._next_version(connection, repo_id), "origin_host": self.store.host,
                       "changed_by": "user", "changed_at": time.time()}
            self._apply(connection, setting)
            self._compute(connection, repo_id)
        return {**self.show(repo_id), **self._display_result()}

    def accept(self, setting: Mapping[str, Any], source: str) -> dict[str, Any]:
        if setting.get("repo_id", "").startswith("local:"):
            raise LedgerError("local_repo", "Host-local repositories do not propagate settings.")
        with self.store.transaction() as connection:
            changed = self._apply(connection, setting, source=source)
            self._compute(connection, setting["repo_id"])
        return {"applied": changed, **self.show(setting["repo_id"]), **self._display_result()}

    def _compute(self, connection: Any, repo_id: str) -> None:
        fields = self.fields(connection, repo_id)
        home = fields.get("home")
        if not home or home["value"] != self.store.host:
            return
        target = None
        if fields.get("mode", {}).get("value") == "auto":
            agents = connection.execute("SELECT terminal_id FROM agents WHERE host=? AND repo_id=? AND active=1 AND is_main_checkout=1",
                                        (self.store.host, repo_id)).fetchall()
            if len(agents) == 1:
                target = {"host": self.store.host, "terminal_id": agents[0][0]}
        computed = {"target": target, "home_stamp": stamp(home)}
        if fields.get("computed_main", {}).get("value") == computed:
            return
        self._apply(connection, {"repo_id": repo_id, "field": "computed_main", "value": computed,
                                 "version": self._next_version(connection, repo_id), "origin_host": self.store.host,
                                 "changed_by": "home", "changed_at": time.time()})

    def clear_closed_main(self, repo_id: str, expected: Mapping[str, Any], *,
                          refresh_display: bool = True,
                          environment: Mapping[str, str] | None = None) -> bool:
        changed = False
        with self.store.transaction() as connection:
            fields = self.fields(connection, repo_id)
            main = fields.get("main")
            if main and stamp(main) == dict(expected) and main["value"] and main["value"]["host"] == self.store.host:
                terminal = connection.execute("SELECT present FROM terminal_slots WHERE host=? AND terminal_id=?",
                                              (self.store.host, main["value"]["terminal_id"])).fetchone()
                if terminal is None or not terminal[0]:
                    self._apply(connection, {"repo_id": repo_id, "field": "main", "value": None,
                                             "version": self._next_version(connection, repo_id), "origin_host": self.store.host,
                                             "changed_by": "main_host", "changed_at": time.time(), "expected_main": dict(expected)})
                    self._compute(connection, repo_id)
                    changed = True
        if changed and refresh_display:
            self._display_result(environment)
        return changed

    def reconcile_local(self, *, refresh_display: bool = True,
                        environment: Mapping[str, str] | None = None) -> dict[str, Any]:
        with self.store.read() as connection:
            repos = [row[0] for row in connection.execute("SELECT repo_id FROM repos")]
        changed = False
        for repo_id in repos:
            current = self.show(repo_id)["settings"].get("main")
            if current and self.clear_closed_main(repo_id, stamp(current), refresh_display=False):
                changed = True
            with self.store.transaction() as connection:
                before = self.fields(connection, repo_id)
                self._compute(connection, repo_id)
                after = self.fields(connection, repo_id)
                changed = changed or before != after
        display = self._refresh_display(environment) if changed and refresh_display else None
        return {"changed": changed, "display": display}

    def main(self, connection: Any, repo_id: str) -> dict[str, str] | None:
        fields = self.fields(connection, repo_id)
        mode = fields.get("mode", {}).get("value", "off")
        if mode == "off":
            return None
        main = fields.get("main", {}).get("value")
        if main:
            return main
        if mode == "auto":
            computed, home = fields.get("computed_main"), fields.get("home")
            if computed and home and computed["value"]["home_stamp"] == stamp(home):
                return computed["value"]["target"]
        return None

    def role(self, agent: Mapping[str, Any]) -> str | None:
        with self.store.read() as connection:
            fields = self.fields(connection, agent["repo_id"])
            mode = fields.get("mode", {}).get("value", "off")
            if mode == "off":
                return None
            target = {"host": agent["host"], "terminal_id": agent["terminal_id"]}
            if self.main(connection, agent["repo_id"]) == target:
                return "main"
            if mode == "auto" or target in fields.get("members", {}).get("value", []):
                return "worker"
        return None
