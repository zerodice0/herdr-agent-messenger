"""Herdr workspace metadata groups host-local roles without moving user settings."""
import tests.ham_test_support as ham_test_support  # Protect the live ledger before product imports.

import copy
from pathlib import Path
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from ham.ledger.display import SOURCE
from ham.ledger.mail import Mailbox
from ham.ledger.remote import RemoteLedger
from ham.ledger.settings import RepoSettings
from ham.ledger.status import AgentRegistry
from ham.ledger.store import LedgerError, LedgerStore, SCHEMA_VERSION
from ham.ledger.views import status_board
from tests.test_ledger_display import HerdrDisplay


class WorktreeRoleGroupsTest(unittest.TestCase):
    repo_id = "git:example/roles"
    repo_key = "/checkout/.git"

    @classmethod
    def setUpClass(cls):
        helper = Path.home() / ".config/herdr/scripts/agent_work.py"
        if helper.is_file():
            cls.work = Path(subprocess.run(
                [sys.executable, str(helper), "path", "--checkout", str(Path(__file__).resolve().parents[1]),
                 "--name", "worktree-role-tests"], check=True, text=True, capture_output=True).stdout.strip())
        else:
            cls.work = Path(os.environ.get("AGENT_WORK_ROOT", Path.home() / ".cache/agent-work")) / "worktree-role-tests"
            cls.work.mkdir(parents=True, exist_ok=True)

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=self.work)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = LedgerStore(self.root / "ledger", canonical_name="alpha")
        terminals = ("main", "worker", "worker2", "other", "second")
        self.herdr = HerdrDisplay(["p-" + terminal for terminal in terminals],
                                  terminals={"p-" + terminal: terminal for terminal in terminals})
        self.registry = AgentRegistry(self.store, display_runner=self.herdr)
        self.settings = RepoSettings(self.store, display_runner=self.herdr)
        identity = mock.patch("ham.ledger.status.repository_identity", side_effect=lambda cwd, host: {
            "repo_id": "git:example/other" if cwd == "other-repo" else self.repo_id,
            "is_main_checkout": cwd == "main-checkout"})
        identity.start()
        self.addCleanup(identity.stop)

    def workspace(self, workspace_id, linked=False, repo_key=None):
        return {"workspace_id": workspace_id, "label": "label-" + workspace_id,
                "worktree": {"repo_key": self.repo_key if repo_key is None else repo_key,
                             "is_linked_worktree": linked}}

    def snapshot(self, *placements, workspaces=None):
        panes = [{"terminal_id": terminal, "pane_id": "p-" + terminal, "workspace_id": workspace}
                 for terminal, workspace, _ in placements]
        result = {"panes": panes, "agents": [
            {**pane, "agent": "omp", "agent_status": "idle", "name": pane["terminal_id"],
             "agent_session": {"value": "session-" + pane["terminal_id"]}, "cwd": cwd}
            for pane, (_, _, cwd) in zip(panes, placements)]}
        if workspaces is not None:
            result["workspaces"] = workspaces
        return result

    def grouped(self, *, main=True):
        placements = ([("main", "anchor", "main-checkout")] if main else []) + [
            ("worker", "w1", "worktree"), ("worker2", "w2", "worktree")]
        return self.snapshot(*placements, workspaces=[self.workspace("anchor"),
            self.workspace("w1", True), self.workspace("w2", True)])

    def register(self, snapshot, *, display=False):
        self.registry.reconcile(snapshot, refresh_display=display)
        return {agent["terminal_id"]: agent for agent in self.registry.list()}

    def target(self, terminal):
        return {"host": "alpha", "terminal_id": terminal}

    def show(self, workspace="w1"):
        return self.settings.show(self.repo_id, "alpha", workspace)

    def mutate(self, field, value, workspace="w1"):
        return self.settings.mutate(self.repo_id, field, value, host="alpha", workspace_id=workspace)

    def assert_error(self, code, operation):
        with self.assertRaises(LedgerError) as failure:
            operation()
        self.assertEqual(failure.exception.code, code)

    def test_auto_main_members_and_scope_read_across_linked_workspaces(self):
        agents = self.register(self.grouped())
        self.assertEqual({key: row["role"] for key, row in agents.items()},
                         {"main": "main", "worker": "worker", "worker2": "worker"})
        self.assertEqual({row["role_workspace_id"] for row in agents.values()}, {"anchor"})
        for workspace in ("anchor", "w1", "w2"):
            shown = self.show(workspace)
            self.assertEqual(shown["workspace_id"], "anchor")
            self.assertEqual(shown["requested_workspace_id"], workspace)
            self.assertEqual(shown["main"], self.target("main"))
            self.assertEqual(shown["computed_main"], self.target("main"))
            with self.store.read() as connection:
                self.assertEqual({row["terminal_id"] for row in self.settings.scope_agents(
                    connection, self.repo_id, "alpha", workspace)}, set(agents))

    def test_empty_anchor_never_elects_even_a_single_linked_agent(self):
        observed = self.snapshot(("worker", "w1", "worktree"), workspaces=[
            self.workspace("anchor"), self.workspace("w1", True)])
        agents = self.register(observed)
        self.assertEqual(agents["worker"]["role"], "worker")
        self.assertIsNone(self.show()["main"])
        self.assertIsNone(self.show()["computed_main"])
        remote = RemoteLedger(self.store)
        for address in ("main", "main@roles"):
            self.assert_error("main_unresolved", lambda: remote.resolve_recipient(address, agents["worker"]))

    def test_linked_main_checkout_cwd_cannot_auto_elect_under_empty_anchor(self):
        observed = self.snapshot(("worker", "w1", "main-checkout"), workspaces=[
            self.workspace("anchor"), self.workspace("w1", True)])
        agents = self.register(observed)
        self.assertEqual(agents["worker"]["is_main_checkout"], 1)
        self.assertEqual(agents["worker"]["role"], "worker")
        self.assertIsNone(self.show()["main"])
        # Even several linked agents claiming a main-checkout cwd cannot elect.
        extra = self.snapshot(("worker2", "w2", "main-checkout"))
        observed["agents"].extend(extra["agents"])
        observed["panes"].extend(extra["panes"])
        observed["workspaces"].append(self.workspace("w2", True))
        self.register(observed)
        self.assertIsNone(self.show()["main"])

    def test_group_mutations_store_at_anchor_and_manual_members_cover_whole_group(self):
        agents = self.register(self.grouped())
        self.mutate("mode", "manual")
        result = self.mutate("main", self.target("worker2"))
        self.assertEqual((result["workspace_id"], result["requested_workspace_id"]), ("anchor", "w1"))
        self.mutate("members", [self.target("main"), self.target("worker2")], "w2")
        self.assertEqual({key: self.settings.role(row) for key, row in agents.items()},
                         {"main": "worker", "worker": None, "worker2": "main"})
        with self.store.read() as connection:
            rows = connection.execute("SELECT workspace_id,field FROM workspace_settings").fetchall()
            self.assertEqual({tuple(row) for row in rows}, {("anchor", "main"), ("anchor", "members")})
        self.mutate("mode", "off")
        self.assertTrue(all(self.settings.role(row) is None for row in agents.values()))

    def test_empty_anchor_accepts_explicit_worktree_main_and_members(self):
        agents = self.register(self.grouped(main=False))
        self.mutate("main", self.target("worker"))
        self.assertEqual(self.show("w2")["main"], self.target("worker"))
        self.mutate("mode", "manual")
        self.mutate("members", [self.target("worker2")])
        self.assertEqual(self.settings.role(agents["worker"]), "main")
        self.assertEqual(self.settings.role(agents["worker2"]), "worker")

    def test_existing_linked_settings_are_retained_ignored_and_restored_on_close(self):
        observed = self.grouped()
        ungrouped = copy.deepcopy(observed)
        ungrouped["workspaces"] = ungrouped["workspaces"][1:]
        self.register(ungrouped)
        self.mutate("main", self.target("worker"))
        self.mutate("members", [self.target("worker2")], "w2")
        with self.store.read() as connection:
            original = [tuple(row) for row in connection.execute("SELECT * FROM workspace_settings ORDER BY workspace_id,field")]
        self.register(observed)
        self.assertEqual(self.show()["main"], self.target("main"))
        self.assertEqual(self.show("w2")["members"], [])
        # An unset anchor main does not import a linked workspace's explicit main.
        self.mutate("mode", "manual")
        self.assertIsNone(self.show()["main"])
        self.mutate("main", self.target("main"), "anchor")
        self.assertEqual(self.show()["main"], self.target("main"))
        with self.store.read() as connection:
            retained = [tuple(row) for row in connection.execute(
                "SELECT * FROM workspace_settings WHERE workspace_id!='anchor' ORDER BY workspace_id,field")]
            self.assertEqual(retained, original)
        self.register(ungrouped)
        self.assertEqual(self.show()["main"], self.target("worker"))
        self.assertEqual(self.show("w2")["members"], [self.target("worker2")])

    def test_plain_missing_metadata_and_missing_anchor_keep_independent_roles(self):
        for workspaces in (None, [self.workspace("w1", True), self.workspace("w2", True)],
                           [{"workspace_id": "w1"}, {"workspace_id": "w2"}]):
            with self.subTest(workspaces=workspaces):
                agents = self.register(self.snapshot(("worker", "w1", "worktree"),
                    ("worker2", "w2", "worktree"), workspaces=workspaces))
                self.assertEqual({row["role"] for row in agents.values()}, {"main"})
                self.assertEqual({row["role_workspace_id"] for row in agents.values()}, {"w1", "w2"})

    def test_multiple_nonlinked_workspaces_choose_deterministic_anchor_only_for_links(self):
        observed = self.grouped()
        observed["workspaces"].insert(0, self.workspace("z-anchor"))
        self.register(observed)
        with self.store.read() as connection:
            self.assertEqual(self.settings.scope_workspace(connection, self.repo_id, "alpha", "w1"), "anchor")
            self.assertEqual(self.settings.scope_workspace(connection, self.repo_id, "alpha", "z-anchor"), "z-anchor")
        observed["workspaces"].reverse()
        self.register(observed)
        self.assertEqual(self.show()["workspace_id"], "anchor")

    def test_repo_host_and_unrelated_repo_keys_do_not_leak_members_or_settings(self):
        observed = self.grouped()
        extra = self.snapshot(("other", "w2", "other-repo"), ("second", "independent", "main-checkout"))
        observed["agents"].extend(extra["agents"])
        observed["panes"].extend(extra["panes"])
        observed["workspaces"].append(self.workspace("independent", True, "/another/.git"))
        agents = self.register(observed)
        with self.store.transaction() as connection:
            connection.execute("INSERT INTO agents (agent_id,host,terminal_id,workspace_id,pane_id,session_ref,"
                "generation,client,repo_id,is_main_checkout,alias,first_seen,last_seen) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("foreign", "beta", "foreign", "w1", "p-foreign", "session", 1, "omp", self.repo_id, 1, "foreign", 1, 1))
            connection.execute("INSERT INTO workspaces VALUES (?,?,?,?,?)", ("beta", "w1", self.repo_key, 0, "foreign"))
        with self.store.read() as connection:
            self.assertEqual({row["terminal_id"] for row in self.settings.scope_agents(
                connection, self.repo_id, "alpha", "w1")}, {"main", "worker", "worker2"})
            self.assertEqual({row["terminal_id"] for row in self.settings.scope_agents(
                connection, self.repo_id, "beta", "w1")}, {"foreign"})
        for target in (self.target("other"), self.target("second"), {"host": "beta", "terminal_id": "foreign"}):
            for field in ("main", "members"):
                with self.subTest(field=field, target=target):
                    self.assert_error("target_out_of_scope", lambda: self.mutate(
                        field, target if field == "main" else [target]))
        self.assertEqual(agents["second"]["role_workspace_id"], "independent")

    def test_main_routing_and_concrete_mail_delivery_use_group_target(self):
        agents = self.register(self.grouped())
        remote = RemoteLedger(self.store)
        for address in ("main", "main@roles", "local/main@roles", "alpha/main@" + self.repo_id):
            with self.subTest(address=address):
                recipient = remote.resolve_recipient(address, agents["worker"])
                self.assertEqual(recipient["terminal_id"], "main")
        recipient = remote.resolve_recipient("main", agents["worker"])
        sent = Mailbox(self.store).send(agents["worker"], recipient, body="Grouped coordinator")
        with self.store.read() as connection:
            delivery = connection.execute("SELECT * FROM deliveries WHERE message_id=?", (sent["message_id"],)).fetchone()
            self.assertEqual((delivery["recipient_terminal_id"], delivery["workspace_id"]), ("main", "anchor"))
        self.mutate("main", self.target("worker2"))
        self.assertEqual(remote.resolve_recipient("main", agents["worker"])["terminal_id"], "worker2")
        self.assertEqual(remote.resolve_recipient("main@roles", agents["worker"])["terminal_id"], "worker2")

    def test_main_at_repo_counts_groups_not_worktree_workspaces(self):
        observed = self.grouped()
        agents = self.register(observed)
        remote = RemoteLedger(self.store)
        self.assertEqual(remote.resolve_recipient("main@roles", agents["worker"])["terminal_id"], "main")
        extra = self.snapshot(("second", "independent", "main-checkout"))
        observed["agents"].extend(extra["agents"])
        observed["panes"].extend(extra["panes"])
        self.register(observed)
        self.assert_error("main_ambiguous", lambda: remote.resolve_recipient("main@roles", agents["worker"]))
        # Old peers without role_workspace_id retain independent-workspace semantics.
        rows = [{**agents["main"], "role": "main"},
                {**agents["main"], "terminal_id": "old-second", "workspace_id": "another", "role": "main"}]
        for row in rows:
            row.pop("role_workspace_id", None)
        with mock.patch.object(remote, "query_status", return_value={"agents": rows}):
            self.assert_error("main_ambiguous", lambda: remote.resolve_recipient("beta/main@roles", agents["worker"]))

    def test_member_moves_within_group_then_out_without_rewriting_anchor_settings(self):
        observed = self.grouped()
        self.register(observed)
        self.mutate("mode", "manual")
        self.mutate("main", self.target("main"))
        self.mutate("members", [self.target("worker")])
        before = self.show()["settings"]
        for rows in (observed["agents"], observed["panes"]):
            next(row for row in rows if row["terminal_id"] == "worker")["workspace_id"] = "w2"
        agents = self.register(observed)
        self.assertEqual(self.settings.role(agents["worker"]), "worker")
        for rows in (observed["agents"], observed["panes"]):
            next(row for row in rows if row["terminal_id"] == "worker")["workspace_id"] = "independent"
        agents = self.register(observed)
        self.assertIsNone(self.settings.role(agents["worker"]))
        self.assertEqual(self.show()["settings"], before)
        self.assert_error("target_out_of_scope", lambda: self.mutate("main", self.target("worker")))

    def test_stale_explicit_group_main_is_inert_without_rewriting_settings(self):
        observed = self.grouped()
        self.register(observed)
        self.mutate("main", self.target("worker2"))
        before = self.show()["settings"]["main"]
        observed["agents"] = [row for row in observed["agents"] if row["terminal_id"] != "worker2"]
        agents = self.register(observed)
        self.assertEqual(self.show()["main"], self.target("main"))
        self.assertEqual(self.show()["settings"]["main"], before)
        self.mutate("mode", "manual")
        self.assertIsNone(self.show()["main"])
        self.assertTrue(all(self.settings.role(row) is None for row in agents.values()))

    def test_auto_tie_and_sole_anchor_rules_apply_over_whole_group(self):
        observed = self.grouped()
        extra = self.snapshot(("second", "anchor", "main-checkout"))
        observed["agents"].extend(extra["agents"])
        observed["panes"].extend(extra["panes"])
        agents = self.register(observed)
        self.assertIsNone(self.show()["main"])
        self.assertEqual({row["role"] for row in agents.values()}, {"worker"})
        agents = self.register(self.snapshot(("main", "anchor", "worktree"), workspaces=[self.workspace("anchor")]))
        self.assertEqual(agents["main"]["role"], "main")

    def incumbents(self):
        with self.store.read() as connection:
            return [dict(row) for row in connection.execute(
                "SELECT * FROM auto_main_incumbents ORDER BY repo_id,host,anchor_workspace_id")]

    def test_incumbent_survives_sibling_then_transfers_and_reopen_does_not_steal(self):
        first = ("main", "anchor", "main-checkout")
        sibling = ("second", "anchor", "main-checkout")
        workspaces = [self.workspace("anchor"), self.workspace("w1", True)]
        agents = self.register(self.snapshot(first, workspaces=workspaces))
        self.assertEqual(agents["main"]["role"], "main")
        elected = self.incumbents()
        self.assertEqual([(row["host"], row["anchor_workspace_id"], row["terminal_id"]) for row in elected],
                         [("alpha", "anchor", "main")])
        joined = self.snapshot(first, sibling, ("worker", "w1", "main-checkout"), workspaces=workspaces)
        agents = self.register(joined)
        self.assertEqual({key: row["role"] for key, row in agents.items()},
                         {"main": "main", "second": "worker", "worker": "worker"})
        self.assertEqual(self.incumbents(), elected)
        self.assertIsNone(self.show()["computed_main"])
        self.assertEqual(self.show()["main_source"], "auto")
        self.assertEqual(RemoteLedger(self.store).resolve_recipient("main", agents["worker"])["terminal_id"], "main")
        agents = self.register(self.snapshot(sibling, ("worker", "w1", "main-checkout"), workspaces=workspaces))
        self.assertEqual(agents["second"]["role"], "main")
        self.assertEqual(self.incumbents()[0]["terminal_id"], "second")
        agents = self.register(joined)
        self.assertEqual(agents["second"]["role"], "main")
        self.assertEqual(agents["main"]["role"], "worker")
        self.assertEqual(self.show()["main"], self.target("second"))
        self.assertNotIn("main", self.show()["settings"])

    def test_terminal_restart_keeps_incumbent_but_anchor_departure_clears_it(self):
        observed = self.grouped()
        self.register(observed)
        elected = self.incumbents()
        restarted = copy.deepcopy(observed)
        restarted["agents"][0]["agent_session"]["value"] = "new-session"
        agents = self.register(restarted)
        self.assertEqual(agents["main"]["generation"], 2)
        self.assertEqual(agents["main"]["role"], "main")
        self.assertEqual(self.incumbents(), elected)
        # Staying active in a linked workspace is insufficient for incumbency.
        for rows in (restarted["agents"], restarted["panes"]):
            rows[0]["workspace_id"] = "w1"
        agents = self.register(restarted)
        self.assertIsNone(self.show()["main"])
        self.assertEqual(self.incumbents(), [])
        self.assertEqual({row["role"] for row in agents.values()}, {"worker"})

    def test_pin_override_clear_and_modes_preserve_local_incumbent_not_user_rows(self):
        observed = self.grouped()
        self.register(observed)
        elected = self.incumbents()
        pinned = self.mutate("main", self.target("worker"))
        self.assertEqual(pinned["main_source"], "pinned")
        before = pinned["settings"]["main"]
        self.register(observed)
        self.assertEqual(self.show()["main"], self.target("worker"))
        self.assertEqual(self.show()["settings"]["main"], before)
        self.assertEqual(self.incumbents(), elected)
        cleared = self.mutate("main", None)
        self.assertEqual(cleared["main"], self.target("main"))
        self.assertEqual(cleared["main_source"], "auto")
        for mode in ("manual", "off"):
            self.mutate("mode", mode)
            self.register(observed)
            self.assertIsNone(self.show()["main"])
            self.assertIsNone(self.show()["main_source"])
            self.assertEqual(self.incumbents(), elected)
        self.mutate("mode", "auto")
        self.assertEqual(self.show()["main"], self.target("main"))
        self.register(self.snapshot(workspaces=observed["workspaces"]))
        self.assertEqual(self.incumbents(), [])

    def test_pin_blocks_new_incumbent_and_clearing_computes_without_read_write(self):
        observed = self.grouped()
        self.register(observed)
        self.mutate("main", self.target("worker"))
        with self.store.transaction() as connection:
            connection.execute("DELETE FROM auto_main_incumbents")
        self.register(observed)
        self.assertEqual(self.incumbents(), [])
        self.assertEqual(self.show()["main_source"], "pinned")
        self.mutate("main", None)
        with self.store.read() as connection:
            before = list(connection.iterdump())
        readonly = LedgerStore.open_readonly({"HERDR_AGENT_LEDGER_DIR": str(self.store.directory)})
        settings = RepoSettings(readonly)
        self.assertEqual(settings.show(self.repo_id, "alpha", "w1")["main"], self.target("main"))
        self.assertEqual(settings.show(self.repo_id, "alpha", "w1")["main_source"], "auto")
        agents = AgentRegistry(readonly).list()
        self.assertEqual({row["terminal_id"]: settings.role(row) for row in agents},
                         {"main": "main", "worker": "worker", "worker2": "worker"})
        RemoteLedger(readonly).status_export()
        status_board(readonly, host="local", resource_scanner=lambda: {
            "listeners": [], "serve": [], "error": None})
        with readonly.read() as connection:
            self.assertEqual(list(connection.iterdump()), before)
        self.assertEqual(self.incumbents(), [])
        self.register(observed)
        self.assertEqual(self.incumbents()[0]["terminal_id"], "main")

    def test_v10_and_v11_upgrade_are_additive_and_election_waits_for_reconcile(self):
        observed = self.grouped()
        for version in (10, 11):
            with self.subTest(version=version):
                self.register(observed)
                self.mutate("members", [self.target("worker")])
                with self.store.transaction() as connection:
                    settings_before = [tuple(row) for row in connection.execute(
                        "SELECT * FROM workspace_settings ORDER BY workspace_id,field")]
                    agents_before = [tuple(row) for row in connection.execute("SELECT * FROM agents ORDER BY agent_id")]
                    connection.execute("DROP TABLE auto_main_incumbents")
                    if version == 10:
                        connection.execute("DROP TABLE mail_dismissals")
                        connection.execute("ALTER TABLE messages DROP COLUMN sender_context")
                    connection.execute("UPDATE ledger_meta SET value=? WHERE key='schema_version'", (str(version),))
                self.assert_error("schema_upgrade_required", lambda: LedgerStore.open_readonly(
                    {"HERDR_AGENT_LEDGER_DIR": str(self.store.directory)}))
                upgraded = LedgerStore(self.store.directory, canonical_name="alpha")
                with upgraded.read() as connection:
                    self.assertEqual(connection.execute(
                        "SELECT value FROM ledger_meta WHERE key='schema_version'").fetchone()[0], "12")
                    self.assertEqual(connection.execute("SELECT count(*) FROM auto_main_incumbents").fetchone()[0], 0)
                    self.assertEqual([tuple(row) for row in connection.execute(
                        "SELECT * FROM workspace_settings ORDER BY workspace_id,field")], settings_before)
                    self.assertEqual([tuple(row) for row in connection.execute(
                        "SELECT * FROM agents ORDER BY agent_id")], agents_before)
                    self.assertIn("sender_context", {row["name"] for row in connection.execute("PRAGMA table_info(messages)")})
                    self.assertEqual(connection.execute("SELECT count(*) FROM mail_dismissals").fetchone()[0], 0)
                self.assertEqual(self.show()["main"], self.target("main"))
                self.assertEqual(self.incumbents(), [])
                self.register(observed)
                self.assertEqual(self.incumbents()[0]["terminal_id"], "main")

    def test_anchor_open_close_refreshes_all_surviving_linked_sidebar_and_borders(self):
        observed = self.grouped(main=False)
        closed = copy.deepcopy(observed)
        closed["workspaces"] = closed["workspaces"][1:]
        self.register(closed, display=True)
        self.assertTrue(all(self.herdr.labels["p-" + terminal].startswith("[메인]")
                            for terminal in ("worker", "worker2")))
        self.herdr.commands.clear()
        self.register(observed, display=True)
        for terminal in ("worker", "worker2"):
            pane = "p-" + terminal
            self.assertTrue(self.herdr.labels[pane].startswith("[워커]"))
            self.assertIn("[워커]", self.herdr.metadata[pane][SOURCE]["display_agent"])
        self.assertEqual({command[2] for command in self.herdr.commands}, {"p-worker", "p-worker2"})
        self.herdr.commands.clear()
        self.register(closed, display=True)
        self.assertEqual({command[2] for command in self.herdr.commands}, {"p-worker", "p-worker2"})
        self.assertTrue(all(self.herdr.labels["p-" + terminal].startswith("[메인]")
                            for terminal in ("worker", "worker2")))

    def test_group_arrival_departure_and_missing_metadata_refresh_survivors_only(self):
        observed = self.grouped()
        self.register(observed, display=True)
        self.herdr.commands.clear()
        without_main = copy.deepcopy(observed)
        without_main["agents"] = without_main["agents"][1:]
        without_main["panes"] = without_main["panes"][1:]
        self.register(without_main, display=True)
        self.assertEqual({command[2] for command in self.herdr.commands}, {"p-worker", "p-worker2"})
        self.herdr.commands.clear()
        self.register(observed, display=True)
        self.assertEqual({command[2] for command in self.herdr.commands}, {"p-main", "p-worker", "p-worker2"})
        without_metadata = copy.deepcopy(observed)
        without_metadata.pop("workspaces")
        self.herdr.commands.clear()
        self.register(without_metadata, display=True)
        self.assertEqual({command[2] for command in self.herdr.commands}, {"p-main", "p-worker", "p-worker2"})
        self.assertEqual(self.show()["workspace_id"], "w1")
        self.assertEqual(self.show()["main"], self.target("worker"))

    def test_export_and_board_expose_scope_and_empty_anchor_metadata_readonly(self):
        self.register(self.grouped(main=False))
        with self.store.read() as connection:
            before = list(connection.iterdump())
        exported = RemoteLedger(self.store).status_export()
        self.assertEqual({row["role_workspace_id"] for row in exported["agents"]}, {"anchor"})
        self.assertEqual({row["role"] for row in exported["agents"]}, {"worker"})
        anchor = next(row for row in exported["workspaces"] if row["workspace_id"] == "anchor")
        self.assertEqual(anchor, {"host": "alpha", "workspace_id": "anchor", "repo_key": self.repo_key,
                                  "is_linked_worktree": 0, "label": "label-anchor"})
        board = status_board(self.store, host="local", resource_scanner=lambda: {
            "listeners": [], "serve": [], "error": None})
        self.assertEqual(board["workspaces"], exported["workspaces"])
        self.assertEqual({row["role_workspace_id"] for row in board["agents"]}, {"anchor"})
        with self.store.read() as connection:
            self.assertEqual(list(connection.iterdump()), before)

    def test_v8_migration_preserves_workspace_settings_and_needs_reconcile_for_groups(self):
        observed = self.grouped()
        ungrouped = copy.deepcopy(observed)
        ungrouped.pop("workspaces")
        self.register(ungrouped)
        self.mutate("main", self.target("main"), "anchor")
        self.mutate("main", self.target("worker"), "w1")
        self.mutate("members", [self.target("worker2")], "w2")
        with self.store.transaction() as connection:
            settings_before = [tuple(row) for row in connection.execute("SELECT * FROM workspace_settings ORDER BY workspace_id,field")]
            identities_before = [tuple(row) for row in connection.execute("SELECT agent_id,generation FROM agents ORDER BY agent_id")]
            connection.execute("DROP TABLE workspaces")
            connection.execute("UPDATE ledger_meta SET value='8' WHERE key='schema_version'")
        self.assert_error("schema_upgrade_required", lambda: LedgerStore.open_readonly(
            {"HERDR_AGENT_LEDGER_DIR": str(self.store.directory)}))
        upgraded = LedgerStore(self.store.directory, canonical_name="alpha")
        with upgraded.read() as connection:
            self.assertEqual(connection.execute("SELECT value FROM ledger_meta WHERE key='schema_version'").fetchone()[0], str(SCHEMA_VERSION))
            self.assertEqual(connection.execute("SELECT count(*) FROM workspaces").fetchone()[0], 0)
            self.assertEqual([tuple(row) for row in connection.execute("SELECT * FROM workspace_settings ORDER BY workspace_id,field")], settings_before)
            self.assertEqual([tuple(row) for row in connection.execute("SELECT agent_id,generation FROM agents ORDER BY agent_id")], identities_before)
        self.register(observed)
        self.assertEqual(self.show()["main"], self.target("main"))
        with self.store.read() as connection:
            self.assertEqual([tuple(row) for row in connection.execute("SELECT * FROM workspace_settings ORDER BY workspace_id,field")], settings_before)

    def test_peer_board_preserves_group_metadata_only_for_verified_previous_host(self):
        self.register(self.grouped(main=False))
        with self.store.transaction() as connection:
            connection.execute("INSERT INTO peers VALUES (?,?,?,?)", ("beta", "peer-uuid", "b", "/peer"))
        exported = RemoteLedger(self.store).status_export()
        peer_rows = [{**row, "host": "beta"} for row in exported["agents"]]
        peer_workspaces = [{**row, "host": "beta"} for row in exported["workspaces"]]
        with mock.patch.object(RemoteLedger, "query_status", return_value={
                "agents": peer_rows, "workspaces": peer_workspaces}):
            board = status_board(self.store, host="beta")
        self.assertEqual(board["workspaces"], peer_workspaces)
        self.assertEqual({row["role_workspace_id"] for row in board["agents"]}, {"anchor"})
        with mock.patch.object(RemoteLedger, "query_status", side_effect=LedgerError("host_unavailable", "offline")):
            stale = status_board(self.store, host="beta", previous=board)
            missing = status_board(self.store, host="beta")
        self.assertEqual(stale["workspaces"], peer_workspaces)
        self.assertTrue(all(row["stale"] for row in stale["agents"]))
        self.assertEqual(missing["workspaces"], [])
        with mock.patch.object(RemoteLedger, "query_status", return_value={
                "agents": peer_rows, "workspaces": exported["workspaces"]}):
            rejected = status_board(self.store, host="beta")
        self.assertEqual(rejected["hosts"][0]["error"]["code"], "peer_identity_conflict")
        self.assertEqual(rejected["workspaces"], [])

    def test_invalid_workspace_snapshot_does_not_replace_metadata(self):
        observed = self.grouped()
        self.register(observed)
        with self.store.read() as connection:
            before = list(connection.iterdump())
        for invalid in (None, {}, [self.workspace("w1"), self.workspace("w1")], [{}]):
            with self.subTest(workspaces=invalid):
                broken = {**observed, "workspaces": invalid}
                self.assert_error("invalid_snapshot", lambda: self.register(broken))
        with self.store.read() as connection:
            self.assertEqual(list(connection.iterdump()), before)
