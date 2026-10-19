import ham_test_support  # Protect the live ledger before product imports.
import contextlib
import io
import json
from pathlib import Path
import re
import subprocess
import tempfile
import unittest
from collections.abc import Sequence
from unittest import mock

try:
    import tomllib  # noqa: F401
except ModuleNotFoundError:  # Python 3.10 Herdr hosts run the plugin, not rollout.
    raise unittest.SkipTest("rollout_plugin.py requires Python 3.11 (tomllib)")

import plugin_rollout


EXACT_REF = "1" * 40


def target_snapshot() -> plugin_rollout.TargetSnapshot:
    return plugin_rollout.TargetSnapshot(
        EXACT_REF,
        "0.8.0",
        {
            "agent_labels.py": plugin_rollout.FileFingerprint("file", "a" * 64),
            "herdr-plugin.toml": plugin_rollout.FileFingerprint("file", "b" * 64),
        },
    )


def plugin_list_payload(
    *,
    ref: str = EXACT_REF,
    enabled: bool = True,
    present: bool = True,
    plugin_id: str = plugin_rollout.PLUGIN_ID,
) -> str:
    plugins = []
    if present:
        plugins.append(
            {
                "plugin_id": plugin_id,
                "enabled": enabled,
                "version": "0.8.0",
                "plugin_root": "/managed/plugin",
                "source": {
                    "kind": "github",
                    "owner": "zerodice0",
                    "repo": "herdr-agent-messenger",
                    "requested_ref": ref,
                    "resolved_commit": ref,
                    "managed_path": "/managed/plugin",
                },
            }
        )
    return json.dumps(
        {
            "result": {
                "plugins": plugins,
            }
        }
    )


def plugins_payload(plugins: Sequence[dict[str, object]]) -> str:
    return json.dumps({"result": {"plugins": list(plugins)}})


def installed_plugin(
    plugin_id: str,
    *,
    ref: str = EXACT_REF,
    enabled: bool = True,
) -> dict[str, object]:
    return {
        "plugin_id": plugin_id,
        "enabled": enabled,
        "version": "0.8.0",
        "plugin_root": "/managed/plugin",
        "source": {
            "kind": "github",
            "owner": "zerodice0",
            "repo": "herdr-agent-messenger",
            "requested_ref": ref,
            "resolved_commit": ref,
            "managed_path": "/managed/plugin",
        },
    }


def action_list_payload() -> str:
    return json.dumps(
        {
            "result": {
                "actions": [
                    {"action_id": action_id}
                    for action_id in sorted(plugin_rollout.CORE_ACTION_IDS)
                ]
            }
        }
    )


class FakeRemoteRunner:
    def __init__(
        self,
        *,
        failing_host: str | None = None,
        bad_hashes: bool = False,
        previous_ref: str | None = EXACT_REF,
        previous_enabled: bool = True,
        failing_fragments: Sequence[str] = (),
        install_applies_then_fails: bool = False,
        uninstall_noop: bool = False,
    ):
        self.failing_host = failing_host
        self.bad_hashes = bad_hashes
        self.calls: list[tuple[list[str], float]] = []
        self.installed_ref = previous_ref
        self.enabled = previous_enabled
        self.failing_fragments = tuple(failing_fragments)
        self.install_applies_then_fails = install_applies_then_fails
        self.uninstall_noop = uninstall_noop

    def __call__(
        self,
        command: list[str] | tuple[str, ...],
        timeout: float,
    ) -> subprocess.CompletedProcess[str]:
        argv = list(command)
        self.calls.append((argv, timeout))
        host = argv[-2]
        remote = argv[-1]
        if any(fragment in remote for fragment in self.failing_fragments):
            return subprocess.CompletedProcess(argv, 1, "", "forced failure")
        if "plugin install" in remote:
            match = re.search(r"--ref ([0-9a-f]{40})", remote)
            ref = match.group(1) if match else ""
            if host == self.failing_host and ref == EXACT_REF:
                return subprocess.CompletedProcess(argv, 1, "", "install refused")
            self.installed_ref = ref
            self.enabled = True
            if self.install_applies_then_fails and ref == EXACT_REF:
                return subprocess.CompletedProcess(argv, 1, "", "connection lost")
            return subprocess.CompletedProcess(argv, 0, "{}", "")
        if "plugin uninstall" in remote:
            if not self.uninstall_noop:
                self.installed_ref = None
                self.enabled = False
            return subprocess.CompletedProcess(argv, 0, "{}", "")
        if "plugin enable" in remote:
            self.enabled = True
            return subprocess.CompletedProcess(argv, 0, "{}", "")
        if "plugin disable" in remote:
            self.enabled = False
            return subprocess.CompletedProcess(argv, 0, "{}", "")
        if "plugin list" in remote:
            return subprocess.CompletedProcess(
                argv,
                0,
                plugin_list_payload(
                    ref=self.installed_ref or EXACT_REF,
                    enabled=self.enabled,
                    present=self.installed_ref is not None,
                ),
                "",
            )
        if "plugin action list" in remote:
            return subprocess.CompletedProcess(argv, 0, action_list_payload(), "")
        if "hashlib.sha256" in remote:
            fingerprints = {
                path: {
                    "kind": fingerprint.kind,
                    "sha256": (
                        "0" * 64 if self.bad_hashes else fingerprint.sha256
                    ),
                }
                for path, fingerprint in target_snapshot().files.items()
            }
            return subprocess.CompletedProcess(argv, 0, json.dumps(fingerprints), "")
        return subprocess.CompletedProcess(argv, 0, "{}", "")


class LegacyMigrationRunner:
    def __init__(self):
        self.calls: list[tuple[list[str], float]] = []
        self.plugins: dict[str, dict[str, object]] = {
            plugin_rollout.LEGACY_PLUGIN_ID: installed_plugin(
                plugin_rollout.LEGACY_PLUGIN_ID
            )
        }

    def __call__(
        self,
        command: list[str] | tuple[str, ...],
        timeout: float,
    ) -> subprocess.CompletedProcess[str]:
        argv = list(command)
        self.calls.append((argv, timeout))
        remote = argv[-1]
        if "plugin install" in remote:
            match = re.search(r"--ref ([0-9a-f]{40})", remote)
            ref = match.group(1) if match else EXACT_REF
            self.plugins[plugin_rollout.PLUGIN_ID] = installed_plugin(
                plugin_rollout.PLUGIN_ID,
                ref=ref,
            )
            return subprocess.CompletedProcess(argv, 0, "{}", "")
        for operation in ("uninstall", "enable", "disable"):
            if f"plugin {operation}" not in remote:
                continue
            plugin_id = next(
                (
                    candidate
                    for candidate in (
                        plugin_rollout.LEGACY_PLUGIN_ID,
                        plugin_rollout.PLUGIN_ID,
                    )
                    if candidate in remote
                ),
                plugin_rollout.PLUGIN_ID,
            )
            if operation == "uninstall":
                self.plugins.pop(plugin_id, None)
            elif plugin_id in self.plugins:
                self.plugins[plugin_id]["enabled"] = operation == "enable"
            return subprocess.CompletedProcess(argv, 0, "{}", "")
        if "plugin list" in remote:
            selected = list(self.plugins.values())
            for plugin_id in (
                plugin_rollout.LEGACY_PLUGIN_ID,
                plugin_rollout.PLUGIN_ID,
            ):
                if f"--plugin {plugin_id}" in remote:
                    selected = (
                        [self.plugins[plugin_id]]
                        if plugin_id in self.plugins
                        else []
                    )
                    break
            return subprocess.CompletedProcess(
                argv,
                0,
                plugins_payload(selected),
                "",
            )
        if "plugin action list" in remote:
            return subprocess.CompletedProcess(argv, 0, action_list_payload(), "")
        return subprocess.CompletedProcess(argv, 0, "{}", "")


class PluginRolloutTest(unittest.TestCase):
    def test_selected_hosts_are_explicit_concrete_authorized_aliases(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "config"
            allowlist = root / "ssh-hosts"
            config.write_text(
                "Host desktop spare\nHost *.example.com\n",
                encoding="utf-8",
            )
            allowlist.write_text("desktop\nspare\n", encoding="utf-8")
            environment = {
                "HERDR_AGENT_MESSENGER_SSH_CONFIG": str(config),
                "HERDR_AGENT_MESSENGER_SSH_HOSTS_FILE": str(allowlist),
            }

            self.assertEqual(
                plugin_rollout.resolve_selected_hosts(["desktop"], environment),
                ["desktop"],
            )
            with self.assertRaises(plugin_rollout.RolloutError):
                plugin_rollout.resolve_selected_hosts(["local"], environment)
            with self.assertRaises(plugin_rollout.RolloutError):
                plugin_rollout.resolve_selected_hosts(["unknown"], environment)

    def test_install_uses_exact_ref_and_shared_hardened_transport(self):
        command = plugin_rollout.install_command(
            "desktop",
            EXACT_REF,
            config_path=Path("/tmp/ssh-config"),
        )

        self.assertEqual(command[-2], "desktop")
        self.assertIn(f"plugin install {plugin_rollout.PLUGIN_SOURCE}", command[-1])
        self.assertIn(f"--ref {EXACT_REF} --yes", command[-1])
        self.assertIn("ClearAllForwardings=yes", command)
        self.assertIn("ForwardAgent=no", command)
        self.assertNotIn("StrictHostKeyChecking=accept-new", command)

    def test_dry_run_constructs_commands_without_calling_subprocess(self):
        runner = mock.Mock(side_effect=AssertionError("runner must not be called"))

        results = plugin_rollout.rollout_hosts(
            ["desktop"],
            "full",
            target_snapshot(),
            config_path=Path("/tmp/ssh-config"),
            dry_run=True,
            runner=runner,
        )

        runner.assert_not_called()
        self.assertEqual([result.host for result in results], ["desktop"])
        self.assertTrue(all(check.status == "planned" for check in results[0].checks))
        self.assertEqual(
            [command["step"] for command in results[0].commands],
            [
                "preflight",
                "install",
                "stage",
                "metadata",
                "hashes",
                "unittest",
                "legacy-disable",
                "enable",
                "enabled-metadata",
                "config",
                "reload",
                "actions",
                "legacy-uninstall",
                "migration-metadata",
            ],
        )

    def test_preflight_recognizes_legacy_identity_and_rejects_duplicates(self):
        legacy = plugin_rollout._previous_installation(
            json.loads(
                plugin_list_payload(plugin_id=plugin_rollout.LEGACY_PLUGIN_ID)
            )
        )

        self.assertEqual(legacy.plugin_id, plugin_rollout.LEGACY_PLUGIN_ID)
        with self.assertRaises(plugin_rollout.RolloutError):
            plugin_rollout._previous_installation(
                json.loads(
                    plugins_payload(
                        [
                            installed_plugin(plugin_rollout.PLUGIN_ID),
                            installed_plugin(plugin_rollout.LEGACY_PLUGIN_ID),
                        ]
                    )
                )
            )

    def test_smoke_rollout_replaces_legacy_identity_after_validation(self):
        runner = LegacyMigrationRunner()

        result = plugin_rollout.rollout_hosts(
            ["desktop"],
            "smoke",
            target_snapshot(),
            config_path=Path("/tmp/ssh-config"),
            dry_run=False,
            runner=runner,
        )[0]

        self.assertTrue(result.success)
        self.assertNotIn(plugin_rollout.LEGACY_PLUGIN_ID, runner.plugins)
        self.assertTrue(runner.plugins[plugin_rollout.PLUGIN_ID]["enabled"])
        checks = {check.name: check.status for check in result.checks}
        self.assertEqual(checks["migration"], "pass")
        steps = [command["step"] for command in result.commands]
        self.assertLess(steps.index("legacy-disable"), steps.index("enable"))
        self.assertLess(steps.index("actions"), steps.index("legacy-uninstall"))

    def test_cli_requires_explicit_confirmation_before_preflight(self):
        stdout = io.StringIO()
        with (
            contextlib.redirect_stdout(stdout),
            mock.patch.object(plugin_rollout, "resolve_selected_hosts") as resolve,
        ):
            exit_code = plugin_rollout.main(
                [
                    "--host",
                    "desktop",
                    "--ref",
                    EXACT_REF,
                    "--format",
                    "json",
                ],
                {},
            )

        self.assertEqual(exit_code, 2)
        self.assertIn("--confirm", json.loads(stdout.getvalue())["error"])
        resolve.assert_not_called()

    def test_cli_confirmation_authorizes_the_non_dry_run_path(self):
        completed = plugin_rollout.HostResult("desktop", "smoke")
        completed.add("install", "pass", "ok")
        stdout = io.StringIO()
        with (
            contextlib.redirect_stdout(stdout),
            mock.patch.object(
                plugin_rollout,
                "resolve_selected_hosts",
                return_value=["desktop"],
            ),
            mock.patch.object(
                plugin_rollout,
                "load_target_snapshot",
                return_value=target_snapshot(),
            ),
            mock.patch.object(
                plugin_rollout,
                "rollout_hosts",
                return_value=[completed],
            ) as rollout,
        ):
            exit_code = plugin_rollout.main(
                [
                    "--host",
                    "desktop",
                    "--ref",
                    EXACT_REF,
                    "--confirm",
                    "--format",
                    "json",
                ],
                {"HERDR_AGENT_MESSENGER_SSH_CONFIG": "/tmp/ssh-config"},
            )

        self.assertEqual(exit_code, 0)
        self.assertTrue(json.loads(stdout.getvalue())["success"])
        self.assertFalse(rollout.call_args.kwargs["dry_run"])

    def test_ref_must_be_an_immutable_full_commit_before_git_is_called(self):
        with (
            mock.patch.object(plugin_rollout, "_run_local_bytes") as run,
            self.assertRaises(plugin_rollout.RolloutError),
        ):
            plugin_rollout.load_target_snapshot("main")

        run.assert_not_called()

    def test_partial_failure_is_reported_per_host_and_does_not_stop_next_host(self):
        runner = FakeRemoteRunner(failing_host="broken")

        results = plugin_rollout.rollout_hosts(
            ["broken", "healthy"],
            "smoke",
            target_snapshot(),
            config_path=Path("/tmp/ssh-config"),
            dry_run=False,
            runner=runner,
        )

        self.assertFalse(results[0].success)
        self.assertTrue(results[1].success)
        broken_checks = {check.name: check.status for check in results[0].checks}
        self.assertEqual(broken_checks["preflight"], "pass")
        self.assertEqual(broken_checks["install"], "fail")
        self.assertEqual(broken_checks["enabled"], "skipped")
        self.assertEqual(
            {call[0][-2] for call in runner.calls},
            {"broken", "healthy"},
        )

    def test_smoke_omits_hash_and_unittest_while_full_runs_both(self):
        smoke_runner = FakeRemoteRunner()
        smoke = plugin_rollout.rollout_hosts(
            ["desktop"],
            "smoke",
            target_snapshot(),
            config_path=Path("/tmp/ssh-config"),
            dry_run=False,
            runner=smoke_runner,
        )[0]
        full_runner = FakeRemoteRunner()
        full = plugin_rollout.rollout_hosts(
            ["desktop"],
            "full",
            target_snapshot(),
            config_path=Path("/tmp/ssh-config"),
            dry_run=False,
            runner=full_runner,
        )[0]

        self.assertTrue(smoke.success)
        self.assertNotIn("hashes", [check.name for check in smoke.checks])
        self.assertNotIn("unittest", [check.name for check in smoke.checks])
        self.assertTrue(full.success)
        self.assertEqual(
            [check.name for check in full.checks],
            list(plugin_rollout.FULL_CHECKS),
        )
        steps = [command["step"] for command in full.commands]
        self.assertLess(steps.index("hashes"), steps.index("reload"))
        self.assertLess(steps.index("unittest"), steps.index("reload"))

    def test_hash_mismatch_prevents_execution_of_plugin_tests(self):
        runner = FakeRemoteRunner(bad_hashes=True)

        result = plugin_rollout.rollout_hosts(
            ["desktop"],
            "full",
            target_snapshot(),
            config_path=Path("/tmp/ssh-config"),
            dry_run=False,
            runner=runner,
        )[0]

        self.assertFalse(result.success)
        checks = {check.name: check.status for check in result.checks}
        self.assertEqual(checks["hashes"], "fail")
        self.assertEqual(checks["unittest"], "skipped")
        self.assertEqual(checks["reload"], "skipped")
        self.assertEqual(checks["actions"], "skipped")
        self.assertEqual(checks["rollback"], "pass")
        self.assertFalse(
            any("PYTHONDONTWRITEBYTECODE=1" in call[0][-1] for call in runner.calls)
        )
        self.assertNotIn("reload", [command["step"] for command in result.commands])
        self.assertNotIn("actions", [command["step"] for command in result.commands])

    def test_validation_failure_restores_previous_ref_and_enabled_state(self):
        previous_ref = "2" * 40
        runner = FakeRemoteRunner(
            bad_hashes=True,
            previous_ref=previous_ref,
            previous_enabled=False,
        )

        result = plugin_rollout.rollout_hosts(
            ["desktop"],
            "full",
            target_snapshot(),
            config_path=Path("/tmp/ssh-config"),
            dry_run=False,
            runner=runner,
        )[0]

        self.assertFalse(result.success)
        self.assertEqual(runner.installed_ref, previous_ref)
        self.assertFalse(runner.enabled)
        rollback = next(check for check in result.checks if check.name == "rollback")
        self.assertEqual(rollback.status, "pass")
        self.assertIn(previous_ref, rollback.detail)

    def test_failed_rollback_restore_disables_target_without_reload(self):
        previous_ref = "2" * 40
        runner = FakeRemoteRunner(
            bad_hashes=True,
            previous_ref=previous_ref,
            failing_fragments=(f"--ref {previous_ref}",),
        )

        result = plugin_rollout.rollout_hosts(
            ["desktop"],
            "full",
            target_snapshot(),
            config_path=Path("/tmp/ssh-config"),
            dry_run=False,
            runner=runner,
        )[0]

        self.assertEqual(runner.installed_ref, EXACT_REF)
        self.assertFalse(runner.enabled)
        steps = [command["step"] for command in result.commands]
        self.assertIn("rollback-disable", steps)
        self.assertNotIn("rollback-config", steps)
        self.assertNotIn("rollback-reload", steps)
        rollback = next(check for check in result.checks if check.name == "rollback")
        self.assertEqual(rollback.status, "fail")

    def test_failed_rollback_config_disables_plugin_without_reload(self):
        previous_ref = "2" * 40
        runner = FakeRemoteRunner(
            bad_hashes=True,
            previous_ref=previous_ref,
            failing_fragments=("config check",),
        )

        result = plugin_rollout.rollout_hosts(
            ["desktop"],
            "full",
            target_snapshot(),
            config_path=Path("/tmp/ssh-config"),
            dry_run=False,
            runner=runner,
        )[0]

        self.assertEqual(runner.installed_ref, previous_ref)
        self.assertFalse(runner.enabled)
        steps = [command["step"] for command in result.commands]
        self.assertIn("rollback-disable", steps)
        self.assertNotIn("rollback-reload", steps)
        rollback = next(check for check in result.checks if check.name == "rollback")
        self.assertEqual(rollback.status, "fail")

    def test_failed_new_install_is_removed_when_plugin_was_absent(self):
        runner = FakeRemoteRunner(bad_hashes=True, previous_ref=None)

        result = plugin_rollout.rollout_hosts(
            ["desktop"],
            "full",
            target_snapshot(),
            config_path=Path("/tmp/ssh-config"),
            dry_run=False,
            runner=runner,
        )[0]

        self.assertFalse(result.success)
        self.assertIsNone(runner.installed_ref)
        self.assertIn(
            "rollback-uninstall",
            [command["step"] for command in result.commands],
        )

    def test_absent_rollback_disables_target_when_uninstall_lies(self):
        runner = FakeRemoteRunner(
            previous_ref=None,
            install_applies_then_fails=True,
            uninstall_noop=True,
        )

        result = plugin_rollout.rollout_hosts(
            ["desktop"],
            "smoke",
            target_snapshot(),
            config_path=Path("/tmp/ssh-config"),
            dry_run=False,
            runner=runner,
        )[0]

        self.assertFalse(result.success)
        self.assertEqual(runner.installed_ref, EXACT_REF)
        self.assertFalse(runner.enabled)
        steps = [command["step"] for command in result.commands]
        self.assertIn("rollback-verify-absent", steps)
        self.assertIn("rollback-disable", steps)
        self.assertNotIn("rollback-config", steps)
        self.assertNotIn("rollback-reload", steps)


if __name__ == "__main__":
    unittest.main()
