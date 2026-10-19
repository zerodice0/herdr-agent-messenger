"""Display ownership tests with a private real ledger and injected Herdr only."""
import ham_test_support  # Protect the live ledger before product imports.

import copy
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
import uuid
from unittest import mock

import agent_labels
import agent_spawn
import ledger_display
from agent_directory import ssh_program_command
from ledger_remote import RemoteLedger
from ledger_display import (LEGACY_SOURCE, RECAP_SOURCE, SOURCE, LedgerDisplay,
                            border_owned, build_display, refresh_pane, report_metadata)
from ledger_settings import RepoSettings
from ledger_status import AgentRegistry, repository_identity
from ledger_store import LedgerStore


class HerdrDisplay:
    def __init__(self, pane_ids, *, terminals=None):
        self.commands = []
        self.labels = dict.fromkeys(pane_ids, "")
        self.terminals = {} if terminals is None else dict(terminals)
        self.metadata = {pane: {
            "tokens": {"phone": "serial", "emu": "5554", "recap": "legacy phase", "other": "keep"},
            RECAP_SOURCE: {},
        } for pane in pane_ids}
        self.rename_fails = False
        self.metadata_fails = False
        self.pane_get_fails = False

    def __call__(self, command):
        command = list(command)
        self.commands.append(command)
        pane_id = command[2]
        result = {}
        code = 0
        if command[:2] == ["pane", "get"]:
            if self.pane_get_fails:
                code = 1
            else:
                pane = {"pane_id": pane_id, "terminal_id": self.terminals.get(pane_id, "unbound-fixture")}
                if self.labels[pane_id]:
                    pane["label"] = self.labels[pane_id]
                result = {"pane": pane}
        elif command[:2] == ["pane", "rename"]:
            if self.rename_fails:
                code = 1
            else:
                self.labels[pane_id] = "" if command[3] == "--clear" else command[3]
        elif command[:2] == ["pane", "report-metadata"]:
            if self.metadata_fails:
                code = 1
            else:
                source = command[command.index("--source") + 1]
                fields = self.metadata[pane_id].setdefault(source, {})
                tokens = self.metadata[pane_id]["tokens"]
                index = 5
                while index < len(command):
                    option = command[index]
                    if option == "--clear-display-agent":
                        fields.pop("display_agent", None)
                        index += 1
                        continue
                    value = command[index + 1]
                    if option == "--token":
                        key, _, text = value.partition("=")
                        tokens[key] = text
                    elif option == "--clear-token":
                        tokens.pop(value, None)
                    else:
                        fields[option.removeprefix("--").replace("-", "_")] = value
                    index += 2
        else:
            raise AssertionError(f"Unexpected command: {command}")
        return subprocess.CompletedProcess(command, code, json.dumps({"result": result}), "failure" if code else "")


class LedgerDisplayTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.worktree = self.root / "worker"
        self.git("init", "-q", str(self.repo))
        self.git("-C", str(self.repo), "-c", "user.name=Display Test", "-c", "user.email=display@example.invalid",
                 "commit", "-q", "--allow-empty", "-m", "initial")
        self.git("-C", str(self.repo), "config", "remote.origin.url", "https://example.invalid/ham/display.git")
        self.git("-C", str(self.repo), "worktree", "add", "-q", "-b", "worker", str(self.worktree))
        self.store = LedgerStore(self.root / "ledger", canonical_name="display-test")
        # Ownership/alias scenarios exercise an already-known off repository;
        # role-specific scenarios explicitly enable participation below.
        repo_id = repository_identity(str(self.repo), self.store.host)["repo_id"]
        with self.store.transaction() as connection:
            connection.execute("INSERT INTO repos VALUES (?,NULL,?)", (repo_id, 1.0))
        self.herdr = HerdrDisplay(["p-main", "p-worker"],
                                 terminals={"p-main": "main", "p-worker": "worker"})
        self.registry = AgentRegistry(self.store, display_runner=self.herdr)
        self.settings = RepoSettings(self.store, display_runner=self.herdr)
        panes = [{"terminal_id": "main", "pane_id": "p-main", "workspace_id": "w-main"},
                 {"terminal_id": "worker", "pane_id": "p-worker", "workspace_id": "w-worker"}]
        snapshot = {"panes": panes, "agents": [
            {**panes[0], "agent": "omp", "name": "orange-rabbit", "agent_status": "idle",
             "agent_session": {"value": "main-session"}, "cwd": str(self.repo)},
            {**panes[1], "agent": "codex", "name": "review-worker", "agent_status": "working",
             "agent_session": {"value": "worker-session"}, "cwd": str(self.worktree)},
        ]}
        self.registry.reconcile(snapshot, refresh_display=False)
        self.repo_id = self.agent("main")["repo_id"]
        self.display = LedgerDisplay(self.store, runner=self.herdr)

    @staticmethod
    def git(*arguments):
        subprocess.run(["git", *arguments], check=True, capture_output=True, text=True)

    def agent(self, terminal="main"):
        return next(row for row in self.registry.list() if row["terminal_id"] == terminal)

    def status(self, *, phase="구현 중", summary="Implement mailbox"):
        # These cases explicitly exercise the projector below. Keep the real
        # status commit, but defer its already-covered notification boundary.
        with mock.patch.object(LedgerDisplay, "refresh", return_value={"updated": [], "failed": []}):
            return self.registry.set_status(self.agent(), phase=phase, summary=summary)

    def claim(self, device_id, kind, label, *, terminal="main", claimed_at=1.0):
        agent_id = self.agent(terminal)["agent_id"]
        with self.store.transaction() as connection:
            connection.execute(
                "INSERT INTO device_claims(device_id,kind,label,agent_id,claimed_at) VALUES (?,?,?,?,?)",
                (device_id, kind, label, agent_id, claimed_at),
            )

    def test_metadata_tokens_are_flat_across_sources_and_isolated_by_pane(self):
        self.herdr(["pane", "report-metadata", "p-main", "--source", "device",
                    "--display-agent", "device-owned name", "--token", "phone=old"])
        self.herdr(["pane", "report-metadata", "p-main", "--source", SOURCE,
                    "--display-agent", "HAM-owned name", "--token", "phone=📱 Pixel"])
        self.assertEqual(self.herdr.metadata["p-main"]["tokens"]["phone"], "📱 Pixel")
        self.assertEqual(self.herdr.metadata["p-worker"]["tokens"]["phone"], "serial")
        self.herdr(["pane", "report-metadata", "p-main", "--source", "unrelated",
                    "--clear-display-agent", "--clear-token", "phone"])
        self.assertNotIn("phone", self.herdr.metadata["p-main"]["tokens"])
        self.assertEqual(self.herdr.metadata["p-main"][SOURCE]["display_agent"], "HAM-owned name")
        self.assertEqual(self.herdr.metadata["p-main"]["device"]["display_agent"], "device-owned name")
        self.assertTrue(all("tokens" not in fields for source, fields in self.herdr.metadata["p-main"].items()
                            if source != "tokens"))

    def test_refresh_projects_only_held_claims_with_exact_resource_tokens(self):
        self.claim("device-a", "phone", "Pixel 9")
        self.claim("device-b", "phone", "iPhone SE", claimed_at=2.0)
        self.claim("emulator-5554", "emu", "5554", claimed_at=3.0)
        self.claim("worker-device", "phone", "Worker phone", terminal="worker", claimed_at=4.0)
        self.herdr.labels["p-main"] = "Manual border"
        self.assertEqual(self.display.refresh()["failed"], [])
        self.assertEqual(self.herdr.metadata["p-main"]["tokens"], {
            "alias": "orange-rabbit", "color": "orange", "recap": "", "other": "keep",
            "phone": "📱 Pixel 9 iPhone SE", "emu": "🤖 5554",
        })
        self.assertEqual(self.herdr.metadata["p-worker"]["tokens"]["phone"], "📱 Worker phone")
        self.assertNotIn("emu", self.herdr.metadata["p-worker"]["tokens"])
        self.assertEqual(self.herdr.labels["p-main"], "Manual border")
        resource_reports = [command for command in self.herdr.commands
                            if command[:2] == ["pane", "report-metadata"]
                            and any(value.startswith(("phone=", "emu=")) for value in command)]
        self.assertTrue(resource_reports)
        self.assertTrue(all(command[4] == SOURCE for command in resource_reports))

    def test_status_set_and_clear_retain_claim_tokens(self):
        self.claim("device-a", "phone", "Pixel")
        self.claim("emulator-5554", "emu", "5554")
        for phase, summary in (("Implement", "First task"), ("Review", "Second task"), ("", "")):
            with self.subTest(phase=phase):
                result = self.registry.set_status(self.agent(), phase=phase, summary=summary,
                                                  feature_invoked=True)
                self.assertEqual(result["display"]["failed"], [])
                self.assertEqual(self.herdr.metadata["p-main"]["tokens"]["phone"], "📱 Pixel")
                self.assertEqual(self.herdr.metadata["p-main"]["tokens"]["emu"], "🤖 5554")
                self.assertEqual(self.herdr.metadata["p-main"]["tokens"]["recap"], phase)

    def test_refresh_clears_released_claim_keys_but_retains_remaining_kind(self):
        self.claim("device-a", "phone", "Pixel")
        self.claim("emulator-5554", "emu", "5554")
        self.assertEqual(self.display.refresh("p-main")["failed"], [])
        with self.store.transaction() as connection:
            connection.execute("DELETE FROM device_claims WHERE kind='phone'")
        self.assertEqual(self.display.refresh("p-main")["failed"], [])
        self.assertNotIn("phone", self.herdr.metadata["p-main"]["tokens"])
        self.assertEqual(self.herdr.metadata["p-main"]["tokens"]["emu"], "🤖 5554")
        with self.store.transaction() as connection:
            connection.execute("DELETE FROM device_claims")
        result = self.registry.set_status(self.agent(), phase="Released", summary="No devices",
                                          feature_invoked=True)
        self.assertEqual(result["display"]["failed"], [])
        self.assertNotIn("phone", self.herdr.metadata["p-main"]["tokens"])
        self.assertNotIn("emu", self.herdr.metadata["p-main"]["tokens"])
        self.assertEqual(self.herdr.metadata["p-main"]["tokens"]["other"], "keep")
        report = next(command for command in reversed(self.herdr.commands)
                      if command[:2] == ["pane", "report-metadata"] and command[4] == SOURCE)
        self.assertIn(["--clear-token", "phone"], [report[index:index + 2] for index in range(len(report) - 1)])
        self.assertIn(["--clear-token", "emu"], [report[index:index + 2] for index in range(len(report) - 1)])

    def test_claim_transfer_reprojects_previous_holder_without_cross_pane_leak(self):
        self.claim("device-a", "phone", "Pixel")
        self.assertEqual(self.display.refresh()["failed"], [])
        worker_id = self.agent("worker")["agent_id"]
        with self.store.transaction() as connection:
            connection.execute("UPDATE device_claims SET agent_id=?", (worker_id,))
        self.assertEqual(self.display.refresh()["failed"], [])
        self.assertNotIn("phone", self.herdr.metadata["p-main"]["tokens"])
        self.assertEqual(self.herdr.metadata["p-worker"]["tokens"]["phone"], "📱 Pixel")

    def test_stale_claim_does_not_project_even_for_its_former_holder(self):
        self.claim("device-a", "phone", "Stale phone")
        former_holder = self.agent()
        self.herdr.labels["p-main"] = "Manual border"
        with self.store.transaction() as connection:
            connection.execute("UPDATE agents SET active=0 WHERE agent_id=?", (former_holder["agent_id"],))
        self.assertTrue(self.display.refresh_agent(former_holder))
        self.assertNotIn("phone", self.herdr.metadata["p-main"]["tokens"])
        self.assertNotIn("emu", self.herdr.metadata["p-main"]["tokens"])
        with self.store.read() as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM device_claims").fetchone()[0], 1)

    def test_requested_pane_without_active_local_agent_clears_only_device_tokens(self):
        with self.store.transaction() as connection:
            connection.execute("UPDATE agents SET active=0 WHERE pane_id='p-main'")
        self.herdr.labels["p-main"] = "Shell border"
        before = copy.deepcopy(self.herdr.metadata["p-worker"])
        result = self.display.refresh("p-main")
        self.assertEqual(result, {"updated": ["p-main"], "failed": []})
        self.assertEqual(self.herdr.commands, [
            ["pane", "report-metadata", "p-main", "--source", SOURCE,
             "--clear-token", "phone", "--clear-token", "emu"],
        ])
        self.assertEqual(self.herdr.metadata["p-main"]["tokens"],
                         {"recap": "legacy phase", "other": "keep"})
        self.assertEqual(self.herdr.labels["p-main"], "Shell border")
        self.assertEqual(self.herdr.metadata["p-worker"], before)

    def test_requested_pane_with_only_remote_agent_still_clears_device_tokens(self):
        with self.store.transaction() as connection:
            connection.execute("UPDATE agents SET host='remote' WHERE pane_id='p-main'")
        result = self.display.refresh("p-main")
        self.assertEqual(result, {"updated": ["p-main"], "failed": []})
        self.assertNotIn("phone", self.herdr.metadata["p-main"]["tokens"])
        self.assertNotIn("emu", self.herdr.metadata["p-main"]["tokens"])

    def test_inactive_pane_device_clear_reports_metadata_and_runner_failures(self):
        with self.store.transaction() as connection:
            connection.execute("UPDATE agents SET active=0 WHERE pane_id='p-main'")
        self.herdr.metadata_fails = True
        self.assertEqual(self.display.refresh("p-main"), {"updated": [], "failed": ["p-main"]})
        for error in (OSError("offline"), subprocess.TimeoutExpired("herdr", 10)):
            with self.subTest(error=type(error).__name__):
                display = LedgerDisplay(self.store, runner=mock.Mock(side_effect=error))
                self.assertEqual(display.refresh("p-main"), {"updated": [], "failed": ["p-main"]})

    def test_unscoped_refresh_does_not_clear_inactive_panes(self):
        with self.store.transaction() as connection:
            connection.execute("UPDATE agents SET active=0 WHERE pane_id='p-main'")
        self.assertEqual(self.display.refresh(), {"updated": ["p-worker"], "failed": []})
        self.assertEqual(self.herdr.metadata["p-main"]["tokens"]["phone"], "serial")
        self.assertFalse(any(command[2] == "p-main" for command in self.herdr.commands))

    def test_status_service_projects_with_injected_selected_host_runner(self):
        result = self.registry.set_status(self.agent(), phase="구현 중", summary="Status service route",
                                          feature_invoked=True)
        self.assertEqual(result["display"]["failed"], [])
        self.assertEqual(self.herdr.labels["p-main"], "orange-rabbit · Status service route")
        self.assertEqual(self.herdr.metadata["p-main"]["tokens"]["recap"], "구현 중")

    def test_pure_builder_keeps_role_before_address_suffix(self):
        for role, prefix in ((None, ""), ("main", "[메인] "), ("worker", "[워커] ")):
            projected = build_display("omp", "orange-rabbit", "🟧", "orange", role=role,
                                      phase="검토 중", summary="Mailbox implementation")
            self.assertEqual(projected.display_agent, f"🟧 {prefix}orange-rabbit")
            self.assertTrue(projected.display_agent.endswith(" orange-rabbit"))
            self.assertEqual((projected.recap, projected.label),
                             ("검토 중", f"{prefix}orange-rabbit · Mailbox implementation"))
            command = projected.metadata_command("p")
            self.assertEqual(command[3:5], ["--source", SOURCE])
            self.assertEqual(command[5:9], ["--agent", "omp", "--display-agent", projected.display_agent])
            self.assertEqual(command[9:], ["--token", "alias=orange-rabbit", "--token", "color=orange",
                                          "--token", "recap=검토 중"])

    def test_border_builder_omits_missing_parts_and_keeps_alias_before_long_summary(self):
        for role, alias, summary, expected in (
            (None, "", "", ""),
            (None, "orange-rabbit", "", "orange-rabbit"),
            (None, "", "Review", "Review"),
            ("main", "", "", "[메인]"),
            ("main", "", "Review", "[메인] Review"),
            ("worker", "review-worker", "", "[워커] review-worker"),
        ):
            with self.subTest(role=role, alias=alias, summary=summary):
                self.assertEqual(build_display("omp", alias, role=role, summary=summary).label, expected)
        summary = "아주 긴 작업 요약 " * 100
        self.assertEqual(build_display("omp", "orange-rabbit", role="main", summary=summary).label,
                         f"[메인] orange-rabbit · {summary}")

    def test_preexisting_owned_role_summary_border_restores_alias(self):
        self.settings.mutate(self.repo_id, "mode", "auto")
        self.status()
        agent = self.agent()
        previous = "[메인] Implement mailbox"
        self.assertTrue(self.registry.set_display_label(
            agent["agent_id"], agent["last_ham_label"], previous)["updated"])
        self.herdr.labels["p-main"] = previous
        self.assertEqual(self.display.refresh("p-main")["failed"], [])
        self.assertEqual(self.herdr.labels["p-main"], "[메인] orange-rabbit · Implement mailbox")
        self.assertEqual(self.agent()["last_ham_label"], "[메인] orange-rabbit · Implement mailbox")
        self.assertEqual(self.herdr.metadata["p-main"]["tokens"]["alias"], "orange-rabbit")

    def test_auto_roles_use_actual_main_and_linked_worktree(self):
        self.settings.mutate(self.repo_id, "mode", "auto")
        self.status()
        result = self.display.refresh()
        self.assertCountEqual(result["updated"], ["p-main", "p-worker"])
        self.assertEqual(result["failed"], [])
        main = self.herdr.metadata["p-main"][SOURCE]
        worker = self.herdr.metadata["p-worker"][SOURCE]
        self.assertEqual(main["display_agent"], "🟧 [메인] orange-rabbit")
        self.assertEqual(worker["display_agent"], "[워커] review-worker")
        self.assertEqual(self.herdr.metadata["p-main"]["tokens"],
                         {"alias": "orange-rabbit", "color": "orange", "recap": "구현 중", "other": "keep"})
        self.assertEqual(self.herdr.metadata["p-worker"]["tokens"],
                         {"alias": "review-worker", "color": "", "recap": "", "other": "keep"})
        self.assertEqual(self.herdr.labels["p-main"], "[메인] orange-rabbit · Implement mailbox")
        self.assertEqual(self.agent()["last_ham_label"], "[메인] orange-rabbit · Implement mailbox")
        self.assertEqual(self.herdr.labels["p-worker"], "[워커] review-worker")
        for metadata in self.herdr.metadata.values():
            self.assertNotIn("phone", metadata["tokens"])
            self.assertNotIn("emu", metadata["tokens"])
            self.assertEqual(metadata["tokens"]["other"], "keep")

    def test_manual_roles_and_off_keep_status_without_role(self):
        self.settings.mutate(self.repo_id, "mode", "manual")
        target = {"host": self.store.host, "terminal_id": "main"}
        self.settings.mutate(self.repo_id, "main", target)
        self.settings.mutate(self.repo_id, "members", [{"host": self.store.host, "terminal_id": "worker"}])
        self.status()
        self.display.refresh()
        self.assertEqual(self.herdr.metadata["p-main"][SOURCE]["display_agent"], "🟧 [메인] orange-rabbit")
        self.assertEqual(self.herdr.metadata["p-worker"][SOURCE]["display_agent"], "[워커] review-worker")
        self.settings.mutate(self.repo_id, "mode", "off")
        self.display.refresh()
        self.assertEqual(self.herdr.metadata["p-main"][SOURCE]["display_agent"], "🟧 orange-rabbit")
        self.assertEqual(self.herdr.metadata["p-worker"][SOURCE]["display_agent"], "review-worker")
        self.assertEqual(self.herdr.metadata["p-main"]["tokens"]["recap"], "구현 중")

    def test_manual_border_is_not_captured_even_when_equal_to_summary(self):
        self.status()
        self.herdr.labels["p-main"] = "Implement mailbox"
        self.display.refresh("p-main")
        self.assertIsNone(self.agent()["last_ham_label"])
        self.status(summary="Next task")
        self.display.refresh("p-main")
        self.assertEqual(self.herdr.labels["p-main"], "Implement mailbox")
        self.assertIsNone(self.agent()["last_ham_label"])

    def test_owned_border_updates_clears_and_preserves_later_manual_rename(self):
        self.status()
        self.display.refresh("p-main")
        self.status(summary="Second task")
        self.display.refresh("p-main")
        self.assertEqual(self.herdr.labels["p-main"], "orange-rabbit · Second task")
        self.status(phase="", summary="")
        self.display.refresh("p-main")
        self.assertEqual(self.agent()["last_ham_label"], "orange-rabbit")
        self.assertEqual(self.herdr.labels["p-main"], "orange-rabbit")
        self.assertEqual(self.herdr.metadata["p-main"]["tokens"]["recap"], "")
        self.status(summary="Third task")
        self.display.refresh("p-main")
        self.herdr.labels["p-main"] = "My manual title"
        self.status(summary="Fourth task")
        self.display.refresh("p-main")
        self.assertEqual(self.herdr.labels["p-main"], "My manual title")
        self.assertEqual(self.agent()["last_ham_label"], "orange-rabbit · Third task")

    def test_legacy_token_is_not_border_ownership_confirmation(self):
        self.status()
        self.herdr.labels["p-main"] = "Old recap border"
        self.display.refresh("p-main")
        self.assertEqual(self.herdr.labels["p-main"], "Old recap border")
        self.assertIsNone(self.agent()["last_ham_label"])
        result = refresh_pane("p-main", self.store, runner=self.herdr, legacy_label="Old recap border")
        self.assertEqual(result["failed"], [])
        self.assertEqual(self.herdr.labels["p-main"], "orange-rabbit · Implement mailbox")
        self.assertEqual(self.agent()["last_ham_label"], "orange-rabbit · Implement mailbox")

    def test_border_ownership_distinguishes_null_and_empty(self):
        self.assertFalse(border_owned("manual", None))
        self.assertFalse(border_owned("manual", ""))
        self.assertTrue(border_owned("", None))
        self.assertTrue(border_owned("owned", "owned"))
        self.assertTrue(border_owned("legacy", None, legacy_label="legacy"))
        self.assertFalse(border_owned("manual", None, legacy_label="legacy"))

    def test_failed_border_rename_does_not_persist_ownership(self):
        self.status()
        self.herdr.rename_fails = True
        self.assertEqual(self.display.refresh("p-main")["failed"], ["p-main"])
        self.assertIsNone(self.agent()["last_ham_label"])
        self.assertEqual(self.herdr.labels["p-main"], "")

    def test_unreadable_binding_does_not_change_metadata_or_border(self):
        self.status()
        before = copy.deepcopy(self.herdr.metadata)
        self.herdr.pane_get_fails = True
        self.assertEqual(self.display.refresh("p-main")["failed"], ["p-main"])
        self.assertEqual(self.herdr.metadata, before)
        self.assertEqual(self.herdr.labels["p-main"], "")
        self.assertIsNone(self.agent()["last_ham_label"])

    def test_actual_omitted_blank_label_is_owned_after_verified_binding(self):
        before = json.loads(self.herdr(["pane", "get", "p-main"]).stdout)["result"]["pane"]
        self.assertEqual(before, {"pane_id": "p-main", "terminal_id": "main"})
        self.status()
        self.assertEqual(self.display.refresh("p-main")["failed"], [])
        self.assertEqual(self.herdr.labels["p-main"], "orange-rabbit · Implement mailbox")
        self.assertEqual(self.agent()["last_ham_label"], "orange-rabbit · Implement mailbox")
        self.assertEqual(self.herdr.metadata["p-main"]["tokens"]["recap"], "구현 중")

    def test_invalid_or_wrong_binding_fails_closed_before_any_metadata_or_rename(self):
        valid = {"pane_id": "p-main", "terminal_id": "main"}
        cases = [
            "", "not-json", {}, {"result": {}}, {"result": {"pane": {}}},
            {"result": {"pane": {"pane_id": "another-pane", "terminal_id": "main"}}},
            {"result": {"pane": {"pane_id": "p-main", "terminal_id": "replacement-terminal"}}},
            {"result": {"pane": {"pane_id": "p-main"}}},
            {"result": {"pane": {**valid, "label": None}}},
            {"error": {"code": "not_found"}, "result": {"pane": valid}},
            {"ok": False, "result": {"pane": valid}},
        ]
        self.status()
        before = copy.deepcopy(self.herdr.metadata)
        for payload in cases:
            with self.subTest(payload=payload):
                def runner(command):
                    if command[:2] == ["pane", "get"]:
                        body = payload if isinstance(payload, str) else json.dumps(payload)
                        return subprocess.CompletedProcess(command, 0, body, "")
                    return self.herdr(command)
                self.display.runner = runner
                self.assertEqual(self.display.refresh("p-main")["failed"], ["p-main"])
                self.assertEqual(self.herdr.metadata, before)
                self.assertEqual(self.herdr.labels["p-main"], "")
                self.assertIsNone(self.agent()["last_ham_label"])

    def test_plain_label_report_overwrites_flat_recap_without_legacy_cleanup(self):
        self.assertTrue(report_metadata(self.herdr, "p-main", build_display("omp", "orange-rabbit", "🟧", "orange")))
        self.assertEqual(self.herdr.metadata["p-main"]["tokens"]["recap"], "")
        self.assertEqual(self.herdr.metadata["p-main"]["tokens"]["other"], "keep")
        self.assertNotIn(RECAP_SOURCE, [command[4] for command in self.herdr.commands])

    def test_named_assignment_refreshes_arbitrary_alias_without_renaming(self):
        self.herdr.labels["p-worker"] = "User review border"
        with (mock.patch.object(agent_labels, "fetch_agent_info", return_value={"agent": "codex", "name": "review-worker"}),
              mock.patch("ledger_display.LedgerStore", return_value=self.store),
              mock.patch.object(agent_labels, "run_herdr", side_effect=lambda *args: self.herdr(args)),
              mock.patch("sys.stdout", new_callable=io.StringIO) as output):
            self.assertEqual(agent_labels.assign_label("p-worker"), 0)
        self.assertEqual(output.getvalue(), "")
        self.assertEqual(self.herdr.metadata["p-worker"][SOURCE]["display_agent"], "review-worker")
        self.assertEqual(self.herdr.metadata["p-worker"]["tokens"]["alias"], "review-worker")
        self.assertEqual(self.herdr.labels["p-worker"], "User review border")
        self.assertFalse(any(command[:2] == ["agent", "rename"] for command in self.herdr.commands))

    def test_name_taken_race_refreshes_the_winning_alias_metadata(self):
        info = {"agent": "codex", "terminal_id": "worker",
                "agent_session": {"value": "worker-session"}}
        self.herdr.labels["p-worker"] = "User review border"

        def run(*arguments):
            if arguments[:2] == ("agent", "get"):
                return subprocess.CompletedProcess(arguments, 0, json.dumps({"result": {"agent": info}}), "")
            if arguments[:2] == ("agent", "rename"):
                self.assertFalse(info.get("name"), "A concurrent owner's alias must not be replaced")
                info["name"] = "review-worker"
                return subprocess.CompletedProcess(arguments, 1, "", "agent_name_taken")
            return self.herdr(arguments)

        with (mock.patch("ledger_display.LedgerStore", return_value=self.store),
              mock.patch.object(agent_labels, "run_herdr", side_effect=run),
              mock.patch("sys.stdout", new_callable=io.StringIO) as output):
            self.assertEqual(agent_labels.assign_label("p-worker"), 0)
        self.assertEqual(output.getvalue(), "")
        self.assertEqual(self.herdr.metadata["p-worker"]["tokens"]["alias"], "review-worker")
        self.assertEqual(self.herdr.metadata["p-worker"][SOURCE]["display_agent"], "review-worker")
        self.assertEqual(self.herdr.labels["p-worker"], "User review border")

    def test_initial_named_agent_without_registry_row_uses_plain_builder_only(self):
        initial = HerdrDisplay(["p-unregistered"])
        initial.labels["p-unregistered"] = "User's existing border"
        with (mock.patch.object(agent_labels, "fetch_agent_info",
                                return_value={"agent": "codex", "name": "initial-reviewer"}),
              mock.patch("ledger_display.LedgerStore", return_value=self.store),
              mock.patch.object(agent_labels, "run_herdr", side_effect=lambda *args: initial(args)),
              mock.patch("sys.stdout", new_callable=io.StringIO) as output):
            self.assertEqual(agent_labels.assign_label("p-unregistered"), 0)
        self.assertEqual(output.getvalue(), "")
        self.assertEqual(initial.metadata["p-unregistered"][SOURCE], {
            "agent": "codex", "display_agent": "initial-reviewer",
        })
        self.assertEqual([command[4] for command in initial.commands], [LEGACY_SOURCE, SOURCE])
        self.assertTrue(all(command[:2] == ["pane", "report-metadata"] for command in initial.commands))
        self.assertEqual(initial.labels["p-unregistered"], "User's existing border")
        self.assertEqual(initial.metadata["p-unregistered"]["tokens"], {
            "alias": "initial-reviewer", "color": "", "recap": "", "other": "keep",
            "phone": "serial", "emu": "5554",
        })
        self.assertEqual(len(self.registry.list()), 2)

    def test_reconcile_refreshes_alias_metadata_without_replacing_existing_names(self):
        infos = {
            "p-main": {"agent": "omp", "pane_id": "p-main", "name": "orange-rabbit"},
            "p-worker": {"agent": "codex", "pane_id": "p-worker", "name": "review-worker"},
            "p-new": {"agent": "codex", "pane_id": "p-new", "terminal_id": "new",
                      "agent_session": {"value": "new-session"}},
        }
        listed = [*infos.values(), infos["p-new"], {"pane_id": "p-ignored"}]
        surface = HerdrDisplay(infos, terminals={"p-main": "main", "p-worker": "worker"})
        surface.labels["p-worker"] = "User review border"

        def run(*arguments):
            if arguments[:2] == ("agent", "list"):
                return subprocess.CompletedProcess(arguments, 0, json.dumps({"result": {"agents": listed}}), "")
            if arguments[:2] == ("agent", "get"):
                return subprocess.CompletedProcess(
                    arguments, 0, json.dumps({"result": {"agent": infos[arguments[2]]}}), "")
            if arguments[:2] == ("agent", "rename"):
                info = infos[arguments[2]]
                self.assertFalse(info.get("name"), "Existing palette and custom aliases must remain addressable")
                if arguments[3] in {item.get("name") for item in infos.values()}:
                    return subprocess.CompletedProcess(arguments, 1, "", "agent_name_taken")
                info["name"] = arguments[3]
                return subprocess.CompletedProcess(arguments, 0, "{}", "")
            return surface(arguments)

        with (mock.patch("ledger_display.LedgerStore", return_value=self.store),
              mock.patch.object(agent_labels, "run_herdr", side_effect=run),
              mock.patch("sys.stdout", new_callable=io.StringIO) as output):
            self.assertEqual(agent_labels.reconcile_labels(), 0)
        self.assertEqual(infos["p-main"]["name"], "orange-rabbit")
        self.assertEqual(infos["p-worker"]["name"], "review-worker")
        self.assertTrue(infos["p-new"].get("name"))
        self.assertEqual(output.getvalue(), infos["p-new"]["name"] + "\n")
        for pane_id, info in infos.items():
            self.assertEqual(surface.metadata[pane_id]["tokens"]["alias"], info["name"])
            self.assertIn(info["name"], surface.metadata[pane_id][SOURCE]["display_agent"])
            if pane_id == "p-new":
                self.assertEqual(surface.metadata[pane_id]["tokens"]["phone"], "serial")
                self.assertEqual(surface.metadata[pane_id]["tokens"]["emu"], "5554")
            else:
                self.assertNotIn("phone", surface.metadata[pane_id]["tokens"])
                self.assertNotIn("emu", surface.metadata[pane_id]["tokens"])
        self.assertEqual(surface.labels["p-worker"], "User review border")

    def test_spawn_local_refresh_preserves_authoritative_role_and_status(self):
        self.settings.mutate(self.repo_id, "mode", "auto")
        self.status()
        with (mock.patch.object(agent_spawn, "LedgerStore", return_value=self.store),
              mock.patch.object(AgentRegistry, "refresh", return_value=({}, {}))):
            self.assertTrue(agent_spawn._report_metadata(
                self.herdr, "p-main", "omp", agent_labels.Alias("orange-rabbit", "🟧", "orange")))
        metadata = self.herdr.metadata["p-main"][SOURCE]
        self.assertEqual(metadata["display_agent"], "🟧 [메인] orange-rabbit")
        self.assertEqual(self.herdr.metadata["p-main"]["tokens"]["recap"], "구현 중")
        self.assertEqual(self.herdr.labels["p-main"], "[메인] orange-rabbit · Implement mailbox")

    def test_metadata_failures_are_returned_without_dropping_other_reports(self):
        self.herdr.metadata_fails = True
        self.assertFalse(report_metadata(self.herdr, "p-main", build_display("omp", "named"), clear_legacy_recap=True))
        self.assertEqual([command[4] for command in self.herdr.commands], [LEGACY_SOURCE, RECAP_SOURCE, SOURCE])


    def test_global_refresh_off_reprojects_flat_recap_without_legacy_cleanup(self):
        self.assertEqual(self.display.refresh()["failed"], [])
        self.assertEqual(self.display.refresh()["failed"], [])
        self.assertFalse(border_owned("", "previous HAM title"))
        for metadata in self.herdr.metadata.values():
            self.assertEqual(metadata["tokens"]["recap"], "")
        self.assertNotIn(RECAP_SOURCE, [command[4] for command in self.herdr.commands
                                       if command[:2] == ["pane", "report-metadata"]])

    def test_failed_label_cas_is_reported_after_rename_without_fake_persistence(self):
        self.status()
        with mock.patch.object(self.display.registry, "set_display_label",
                               return_value={"updated": False, "last_ham_label": None}) as save:
            self.assertEqual(self.display.refresh("p-main")["failed"], ["p-main"])
        save.assert_called_once_with(self.agent()["agent_id"], None, "orange-rabbit · Implement mailbox")
        self.assertEqual(self.herdr.labels["p-main"], "orange-rabbit · Implement mailbox")
        self.assertIsNone(self.agent()["last_ham_label"])

    def test_released_agent_clears_flat_display_tokens_but_not_resources(self):
        with mock.patch.object(agent_labels, "run_herdr", side_effect=lambda *args: self.herdr(args)):
            self.assertTrue(agent_labels.clear_agent_label_metadata("p-main"))
        self.assertEqual([command[4] for command in self.herdr.commands], [SOURCE, LEGACY_SOURCE])
        self.assertEqual(self.herdr.metadata["p-main"]["tokens"],
                         {"phone": "serial", "emu": "5554", "other": "keep"})


    def test_manual_border_clear_after_ham_ownership_is_preserved(self):
        self.status()
        self.display.refresh("p-main")
        self.herdr.labels["p-main"] = ""
        self.status(summary="Do not recapture manually cleared border")
        self.display.refresh("p-main")
        self.assertEqual(self.herdr.labels["p-main"], "")
        self.assertEqual(self.agent()["last_ham_label"], "orange-rabbit · Implement mailbox")


    def test_metadata_side_effect_manual_rename_is_preserved_by_fresh_read(self):
        self.status()

        def rename_during_report(command):
            result = self.herdr(command)
            if command[:2] == ["pane", "report-metadata"] and command[4] == SOURCE:
                self.herdr.labels["p-main"] = "User renamed during report"
            return result

        self.display.runner = rename_during_report
        self.assertEqual(self.display.refresh("p-main")["failed"], [])
        self.assertEqual(self.herdr.labels["p-main"], "User renamed during report")
        self.assertIsNone(self.agent()["last_ham_label"])

    def test_spawn_remote_uses_registered_endpoint_and_overrides_not_local_roles(self):
        environment = {
            "HERDR_AGENT_LEDGER_DIR": str(self.store.directory),
            "HERDR_AGENT_MESSENGER_SSH_CONFIG": str(self.root / "ssh-config"),
            "HERDR_AGENT_LEDGER_AUTOSTART": "0",
        }
        with mock.patch("ledger_remote.ssh_hosts", return_value=["remote-alias"]):
            RemoteLedger(self.store, environment).add_peer(
                canonical_name="remote-canonical", uuid=str(uuid.uuid4()), ssh_alias="remote-alias",
                remote_ham_root="/work/HAM checkout", remote_ledger_dir="/state/isolated",
                remote_socket="/run/selected.sock")
        completed = subprocess.CompletedProcess([], 0, json.dumps({
            "result": {"updated": ["remote-pane"], "failed": []}}), "")
        run_program = mock.Mock(return_value=completed)
        run_herdr = mock.Mock(side_effect=AssertionError("plain remote metadata overwrite"))
        with (mock.patch.object(AgentRegistry, "refresh", side_effect=AssertionError("local roles read")),
              mock.patch.object(agent_spawn, "LedgerStore", return_value=self.store)):
            self.assertTrue(agent_spawn._report_metadata(
                run_herdr, "remote-pane", "codex", agent_labels.Alias("review-worker", "", ""),
                host="remote-alias", environment=environment, command_runner=run_program))
        expected = ssh_program_command("remote-alias", [
            "env", "HERDR_AGENT_LEDGER_DIR=/state/isolated", "HERDR_SOCKET_PATH=/run/selected.sock",
            "HERDR_AGENT_LEDGER_AUTOSTART=0", "python3", "/work/HAM checkout/ledger_display.py",
            "--pane", "remote-pane",
        ], config_path=self.root / "ssh-config")
        run_program.assert_called_once_with(
            expected, timeout=agent_spawn.REMOTE_DISCOVERY_TIMEOUT_SECONDS, environment=environment)
        run_herdr.assert_not_called()

    def test_spawn_unregistered_remote_refuses_without_any_metadata_write(self):
        run_program = mock.Mock(side_effect=AssertionError("unregistered endpoint invoked"))
        run_herdr = mock.Mock(side_effect=AssertionError("unknown source overwritten"))
        with mock.patch.object(agent_spawn, "LedgerStore", return_value=self.store):
            self.assertFalse(agent_spawn._report_metadata(
                run_herdr, "remote-pane", "codex", agent_labels.Alias("review-worker", "", ""),
                host="unregistered", environment={}, command_runner=run_program))
        run_program.assert_not_called()
        run_herdr.assert_not_called()

    def test_selected_host_cli_refreshes_registry_then_projects_real_roles(self):
        self.settings.mutate(self.repo_id, "mode", "auto")
        self.status()
        with (mock.patch.object(ledger_display, "LedgerStore", return_value=self.store),
              mock.patch.object(AgentRegistry, "refresh", return_value=({}, {})) as refresh_registry,
              mock.patch.object(ledger_display, "run_bounded_command",
                                side_effect=lambda command, **kwargs: self.herdr(command[1:])),
              mock.patch("sys.stdout", new_callable=io.StringIO) as output):
            self.assertEqual(ledger_display.main(["--pane", "p-main"]), 0)
        self.assertEqual(json.loads(output.getvalue()), {"result": {"updated": ["p-main"], "failed": []}})
        self.assertEqual(self.herdr.metadata["p-main"][SOURCE]["display_agent"], "🟧 [메인] orange-rabbit")
        self.assertEqual(self.herdr.labels["p-main"], "[메인] orange-rabbit · Implement mailbox")


    def test_refresh_entrypoints_forward_exact_environment_to_runner(self):
        environment = {"HERDR_SOCKET_PATH": "/run/isolated.sock", "HOME": str(self.root)}
        for entrypoint in (ledger_display.refresh, ledger_display.refresh_pane):
            with self.subTest(entrypoint=entrypoint.__name__):
                with (mock.patch.object(ledger_display, "herdr_executable", return_value="/selected/herdr"),
                      mock.patch.object(ledger_display, "run_bounded_command",
                                        side_effect=lambda command, **kwargs: self.herdr(command[1:])) as run):
                    if entrypoint is ledger_display.refresh:
                        result = entrypoint(self.store, environment)
                    else:
                        result = entrypoint("p-main", self.store, environment)
                self.assertEqual(result["failed"], [])
                self.assertTrue(run.called)
                for call in run.call_args_list:
                    self.assertEqual(call.args[0][0], "/selected/herdr")
                    self.assertIs(call.kwargs["environment"], environment)
                    self.assertEqual(call.kwargs["timeout"], 10)


if __name__ == "__main__":
    unittest.main()

