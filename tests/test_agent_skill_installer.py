import tests.ham_test_support as ham_test_support  # Protect the live ledger before product imports.
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
import curses
import io
import os
import tempfile
import unittest
from unittest import mock

from ham.ui import skill_installer as agent_skill_installer


class AgentSkillInstallerTest(unittest.TestCase):
    @staticmethod
    def create_skill(directory, marker):
        (directory / "scripts").mkdir(parents=True)
        (directory / "SKILL.md").write_text(
            f"---\nname: herdr-agent-messenger\n---\n{marker}\n", encoding="utf-8",
        )
        (directory / "scripts" / "herdr_agent_messenger.py").write_text(marker, encoding="utf-8")
        (directory / "reference.md").write_text(marker, encoding="utf-8")
        return directory

    def test_failed_update_keeps_the_previous_installation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = self.create_skill(root / "source", "new")
            destination = self.create_skill(root / "installed", "old")
            original_copy = agent_skill_installer.shutil.copytree

            def interrupted_copy(src, dst, *args, **kwargs):
                result = original_copy(src, dst, *args, **kwargs)
                if Path(src).resolve() == source.resolve():
                    raise OSError("copy interrupted")
                return result

            with (
                mock.patch.object(agent_skill_installer.shutil, "copytree", side_effect=interrupted_copy),
                self.assertRaisesRegex(OSError, "copy interrupted"),
            ):
                agent_skill_installer.install_skill(destination, source)
            self.assertTrue((destination / "SKILL.md").read_text().endswith("old\n"))
            self.assertEqual((destination / "scripts/herdr_agent_messenger.py").read_text(), "old")
            self.assertEqual((destination / "reference.md").read_text(), "old")

    def test_activation_failure_restores_previous_installation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = self.create_skill(root / "source", "new")
            destination = self.create_skill(root / "installed", "old")
            original_replace = agent_skill_installer.os.replace

            def interrupted_activation(src, dst):
                if Path(src).name == "skill":
                    raise OSError("activation interrupted")
                return original_replace(src, dst)

            with (
                mock.patch.object(agent_skill_installer.os, "replace", side_effect=interrupted_activation),
                self.assertRaisesRegex(OSError, "activation interrupted"),
            ):
                agent_skill_installer.install_skill(destination, source)
            self.assertEqual((destination / "scripts/herdr_agent_messenger.py").read_text(), "old")
            self.assertEqual(sorted(path.name for path in root.iterdir()), ["installed", "source"])

    def test_keyboard_interrupt_during_activation_restores_previous_installation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = self.create_skill(root / "source", "new")
            destination = self.create_skill(root / "installed", "old")
            original_replace = agent_skill_installer.os.replace

            def interrupted_activation(src, dst):
                if Path(src).name == "skill":
                    raise KeyboardInterrupt
                return original_replace(src, dst)

            with (
                mock.patch.object(agent_skill_installer.os, "replace", side_effect=interrupted_activation),
                self.assertRaises(KeyboardInterrupt),
            ):
                agent_skill_installer.install_skill(destination, source)
            self.assertEqual((destination / "scripts/herdr_agent_messenger.py").read_text(), "old")

    def test_failed_restoration_retains_a_recoverable_backup(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = self.create_skill(root / "source", "new")
            destination = self.create_skill(root / "installed", "old")
            original_replace = agent_skill_installer.os.replace

            def interrupted_activation(src, dst):
                if Path(src).name in {"skill", "previous"}:
                    raise OSError("activation interrupted")
                return original_replace(src, dst)

            with (
                mock.patch.object(agent_skill_installer.os, "replace", side_effect=interrupted_activation),
                self.assertRaisesRegex(OSError, "backup retained at") as error,
            ):
                agent_skill_installer.install_skill(destination, source)
            backups = list(root.glob(".installed.*/previous"))
            self.assertEqual(len(backups), 1)
            self.assertIn(str(backups[0]), str(error.exception))
            self.assertEqual((backups[0] / "scripts/herdr_agent_messenger.py").read_text(), "old")

    def test_non_utf8_skill_is_a_conflict(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory)
            (destination / "SKILL.md").write_bytes(b"\xff")
            self.assertEqual(
                agent_skill_installer.installation_status(destination),
                agent_skill_installer.STATUS_CONFLICT,
            )

    def test_update_refuses_internal_symlinks_without_touching_their_targets(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = self.create_skill(root / "source", "new")
            destination = self.create_skill(root / "installed", "old")
            outside = root / "unrelated.py"
            outside.write_text("keep")
            script = destination / "scripts/herdr_agent_messenger.py"
            script.unlink()
            script.symlink_to(outside)
            with self.assertRaisesRegex(OSError, "symlink"):
                agent_skill_installer.install_skill(destination, source)
            self.assertEqual(outside.read_text(), "keep")
            self.assertTrue(script.is_symlink())

    def test_update_refuses_reference_symlinks_without_touching_their_targets(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = self.create_skill(root / "source", "new")
            destination = self.create_skill(root / "installed", "old")
            outside = root / "unrelated.txt"
            outside.write_text("keep", encoding="utf-8")
            reference = destination / "reference.md"
            reference.unlink()
            reference.symlink_to(outside)
            with self.assertRaisesRegex(OSError, "symlink"):
                agent_skill_installer.install_skill(destination, source)
            self.assertEqual(outside.read_text(encoding="utf-8"), "keep")
            self.assertTrue(reference.is_symlink())

    def test_dangling_destination_symlink_is_a_conflict(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "installed"
            destination.symlink_to(Path(directory) / "missing")
            self.assertEqual(
                agent_skill_installer.installation_status(destination),
                agent_skill_installer.STATUS_CONFLICT,
            )

    def test_install_omits_generated_files_and_preserves_local_extras(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = self.create_skill(root / "source", "new")
            destination = self.create_skill(root / "installed", "old")
            (destination / "notes.txt").write_text("keep")
            (source / "__pycache__").mkdir()
            (source / "__pycache__/cache.pyc").write_bytes(b"cache")
            (source / ".DS_Store").write_bytes(b"metadata")
            agent_skill_installer.install_skill(destination, source)
            self.assertEqual((destination / "notes.txt").read_text(), "keep")
            self.assertFalse((destination / "__pycache__").exists())
            self.assertFalse((destination / ".DS_Store").exists())
            self.assertEqual((destination / "reference.md").read_text(), "new")

    def test_destinations_use_selected_home(self):
        destinations = agent_skill_installer.install_destinations(
            {"HOME": "/tmp/example-home"}
        )

        self.assertEqual(
            destinations["codex"],
            Path("/tmp/example-home/.agents/skills/herdr-agent-messenger"),
        )
        self.assertEqual(
            destinations["claude"],
            Path("/tmp/example-home/.claude/skills/herdr-agent-messenger"),
        )
        self.assertEqual(
            destinations["omp"],
            Path("/tmp/example-home/.omp/agent/skills/herdr-agent-messenger"),
        )

    def test_targets_include_project_and_system_scopes(self):
        targets = agent_skill_installer.installation_targets(
            {
                "HOME": "/tmp/example-home",
                "HERDR_PLUGIN_CONTEXT_JSON": (
                    '{"workspace_cwd":"/tmp/example-project"}'
                ),
            }
        )

        self.assertEqual(
            [(target.scope, target.agent) for target in targets],
            [
                ("project", "codex"),
                ("project", "claude"),
                ("project", "omp"),
                ("system", "codex"),
                ("system", "claude"),
                ("system", "omp"),
            ],
        )
        self.assertEqual(
            targets[0].destination,
            Path("/tmp/example-project").resolve()
            / ".agents"
            / "skills"
            / "herdr-agent-messenger",
        )
        self.assertEqual(
            targets[2].destination,
            Path("/tmp/example-project").resolve()
            / ".omp"
            / "skills"
            / "herdr-agent-messenger",
        )
        self.assertEqual(
            targets[5].destination,
            Path("/tmp/example-home/.omp/agent/skills/herdr-agent-messenger"),
        )

    def test_omp_project_installation_is_recognized_and_can_be_updated(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            targets = agent_skill_installer.installation_targets(
                {
                    "HOME": str(root / "home"),
                    agent_skill_installer.SKILL_PROJECT_ROOT_ENV: str(root / "project"),
                }
            )
            target = next(
                target
                for target in targets
                if (target.scope, target.agent) == ("project", "omp")
            )
            destination = agent_skill_installer.install_target(target)
            source = agent_skill_installer.bundled_skill_path().parent
            self.assertEqual(
                (destination / "scripts/herdr_agent_messenger.py").read_bytes(),
                (source / "scripts/herdr_agent_messenger.py").read_bytes(),
            )
            self.assertEqual(
                agent_skill_installer.installation_status(destination),
                agent_skill_installer.STATUS_CURRENT,
            )
            (destination / "SKILL.md").write_text(
                "---\nname: herdr-agent-messenger\n---\nold version\n",
                encoding="utf-8",
            )
            self.assertEqual(
                agent_skill_installer.installation_status(destination),
                agent_skill_installer.STATUS_OUTDATED,
            )
            agent_skill_installer.install_target(target)
            self.assertEqual(
                agent_skill_installer.installation_status(destination),
                agent_skill_installer.STATUS_CURRENT,
            )

    def test_project_omp_is_unavailable_without_a_workspace(self):
        targets = agent_skill_installer.installation_targets(
            {"HOME": "/tmp/example-home"}
        )
        target = next(
            target
            for target in targets
            if (target.scope, target.agent) == ("project", "omp")
        )
        self.assertIsNone(target.destination)
        self.assertEqual(
            agent_skill_installer.installation_status(target.destination),
            agent_skill_installer.STATUS_UNAVAILABLE,
        )
        with self.assertRaises(OSError):
            agent_skill_installer.install_target(target)

    def test_installation_status_distinguishes_current_outdated_and_conflict(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            current = root / "current"
            agent_skill_installer.install_skill(current)
            self.assertEqual(
                agent_skill_installer.installation_status(current),
                agent_skill_installer.STATUS_CURRENT,
            )

            (current / "SKILL.md").write_text(
                "---\nname: herdr-agent-messenger\n---\nchanged\n",
                encoding="utf-8",
            )
            self.assertEqual(
                agent_skill_installer.installation_status(current),
                agent_skill_installer.STATUS_OUTDATED,
            )

            conflict = root / "conflict"
            conflict.mkdir()
            (conflict / "SKILL.md").write_text("unrelated", encoding="utf-8")
            self.assertEqual(
                agent_skill_installer.installation_status(conflict),
                agent_skill_installer.STATUS_CONFLICT,
            )
            self.assertEqual(
                agent_skill_installer.installation_status(root / "missing"),
                agent_skill_installer.STATUS_MISSING,
            )

    def test_missing_reference_is_outdated_and_restored_by_update(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "installed"
            agent_skill_installer.install_skill(destination)
            reference = destination / "reference.md"
            reference.unlink()
            self.assertEqual(
                agent_skill_installer.installation_status(destination),
                agent_skill_installer.STATUS_OUTDATED,
            )
            agent_skill_installer.install_skill(destination)
            source = agent_skill_installer.bundled_skill_path().parent
            self.assertEqual(
                reference.read_bytes(),
                (source / "reference.md").read_bytes(),
            )
            self.assertEqual(
                agent_skill_installer.installation_status(destination),
                agent_skill_installer.STATUS_CURRENT,
            )

    def test_modified_reference_is_outdated_and_restored_by_update(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "installed"
            agent_skill_installer.install_skill(destination)
            reference = destination / "reference.md"
            reference.write_text("old reference", encoding="utf-8")
            self.assertEqual(
                agent_skill_installer.installation_status(destination),
                agent_skill_installer.STATUS_OUTDATED,
            )
            agent_skill_installer.install_skill(destination)
            self.assertEqual(
                agent_skill_installer.installation_status(destination),
                agent_skill_installer.STATUS_CURRENT,
            )

    def test_install_all_copies_the_bundled_skill_to_three_targets(self):
        with tempfile.TemporaryDirectory() as directory:
            environment = {"HOME": directory}

            installed = agent_skill_installer.install_selected("all", environment)

            self.assertEqual(
                installed,
                list(agent_skill_installer.install_destinations(environment).values()),
            )
            self.assertEqual(len(installed), 3)
            for destination in installed:
                self.assertTrue((destination / "SKILL.md").is_file())
                self.assertTrue(
                    (destination / "scripts" / "herdr_agent_messenger.py").is_file()
                )
                self.assertEqual(
                    (destination / "reference.md").read_bytes(),
                    (agent_skill_installer.bundled_skill_path().parent / "reference.md").read_bytes(),
                )
                self.assertEqual(
                    agent_skill_installer.installation_status(destination),
                    agent_skill_installer.STATUS_CURRENT,
                )

    def test_install_omp_copies_only_the_user_skill(self):
        with tempfile.TemporaryDirectory() as directory:
            installed = agent_skill_installer.install_selected("omp", {"HOME": directory})
            destination = (
                Path(directory) / ".omp/agent/skills/herdr-agent-messenger"
            )
            self.assertEqual(installed, [destination])
            self.assertEqual(
                (destination / "SKILL.md").read_bytes(),
                agent_skill_installer.bundled_skill_path().read_bytes(),
            )
            self.assertTrue((destination / "scripts/herdr_agent_messenger.py").is_file())
            self.assertFalse((Path(directory) / ".agents").exists())
            self.assertFalse((Path(directory) / ".claude").exists())

    def test_parser_accepts_omp_and_all_but_rejects_the_removed_selection(self):
        for target in ("omp", "all"):
            with self.subTest(target=target):
                arguments = agent_skill_installer.parse_cli_arguments(["install", target])
                self.assertEqual(arguments.target, target)
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
            agent_skill_installer.parse_cli_arguments(["install", "both"])
        self.assertEqual(error.exception.code, 2)
        with self.assertRaises(ValueError):
            agent_skill_installer.install_selected("both", {"HOME": "/tmp/example-home"})

    def test_install_refuses_to_replace_unrelated_skill(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "herdr-agent-messenger"
            destination.mkdir()
            sentinel = destination / "keep.txt"
            sentinel.write_text("mine", encoding="utf-8")

            with self.assertRaisesRegex(OSError, "unrelated skill"):
                agent_skill_installer.install_skill(destination)

            self.assertEqual(sentinel.read_text(encoding="utf-8"), "mine")

    def test_launch_opens_installer_overlay(self):
        with mock.patch.object(
            agent_skill_installer,
            "launch_plugin_overlay",
            return_value=True,
        ) as launch_overlay:
            self.assertEqual(agent_skill_installer.launch({"LANG": "ko_KR.UTF-8"}), 0)

        launch_overlay.assert_called_once_with(
            agent_skill_installer.SKILL_INSTALLER_ENTRYPOINT,
            environment={"LANG": "ko_KR.UTF-8"},
            extra_arguments=(),
        )

    def test_overlay_launcher_preserves_project_when_child_context_changes(self):
        environment = {
            "HERDR_PLUGIN_CONTEXT_JSON": '{"workspace":{"cwd":"/work/opener"}}',
        }
        with mock.patch.object(agent_skill_installer, "launch_plugin_overlay",
                               return_value=True) as launch_overlay:
            self.assertEqual(agent_skill_installer.launch(environment), 0)
        root = Path("/work/opener").resolve()
        launch_overlay.assert_called_once_with(
            agent_skill_installer.SKILL_INSTALLER_ENTRYPOINT,
            environment=environment,
            extra_arguments=("--env", f"{agent_skill_installer.SKILL_PROJECT_ROOT_ENV}={root}"),
        )
        child = {
            "HERDR_PANE_ID": "w1:overlay",
            "HERDR_PLUGIN_CONTEXT_JSON": '{"workspace":{"cwd":"/plugin/root"}}',
            agent_skill_installer.SKILL_PROJECT_ROOT_ENV: str(root),
        }
        self.assertEqual(agent_skill_installer.project_root(child), root)

    def test_overlay_arrow_selection_installs_the_highlighted_target(self):
        screen = mock.Mock()
        screen.getmaxyx.return_value = (22, 60)
        screen.get_wch.side_effect = [curses.KEY_DOWN] * 5 + ["\n", "\x1b", "\x1b"]

        def run_wrapper(callback):
            return callback(screen)

        with (
            mock.patch.object(
                agent_skill_installer.curses,
                "wrapper",
                side_effect=run_wrapper,
            ),
            mock.patch.object(agent_skill_installer.curses, "curs_set"),
            mock.patch.object(agent_skill_installer.curses, "has_colors", return_value=False),
            mock.patch.object(agent_skill_installer, "install_target") as install,
        ):
            self.assertEqual(
                agent_skill_installer.overlay(
                    {"HOME": "/tmp/example-home", "LANG": "en_US.UTF-8"}
                ),
                0,
            )

        target = install.call_args.args[0]
        self.assertEqual((target.scope, target.agent), ("system", "omp"))

    def test_question_mark_opens_usage_and_escape_returns_to_targets(self):
        screen = mock.Mock()
        screen.getmaxyx.return_value = (22, 60)
        screen.get_wch.side_effect = ["?", "\x1b", "\x1b"]

        with (
            mock.patch.object(
                agent_skill_installer.curses,
                "wrapper",
                side_effect=lambda callback: callback(screen),
            ),
            mock.patch.object(agent_skill_installer.curses, "curs_set"),
            mock.patch.object(agent_skill_installer.curses, "has_colors", return_value=False),
        ):
            self.assertEqual(
                agent_skill_installer.overlay(
                    {"HOME": "/tmp/example-home", "LANG": "en_US.UTF-8"}
                ),
                0,
            )

        rendered = [call.args[2] for call in screen.addnstr.call_args_list]
        rendered_text = "".join(rendered)
        self.assertIn("Use HAM", rendered)
        self.assertIn(
            "Codex: type $ham, then select HAM — Herdr Agent Messenger.",
            rendered_text,
        )
        self.assertIn("Claude Code: invoke /herdr-agent-messenger.", rendered)
        self.assertIn("omp: ask in natural language", rendered_text)
        self.assertIn("skill://herdr-agent-messenger.", rendered_text)
        self.assertIn("Example target: local/yellow-falcon", rendered)


class ReceivingRuntimeConsentTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.environment = {"HOME": str(self.root / "home"),
                            "CLAUDE_CONFIG_DIR": str(self.root / "claude"),
                            "LANG": "en_US.UTF-8"}

    def run_install(self, arguments):
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, self.environment), redirect_stdout(stdout), redirect_stderr(stderr):
            result = agent_skill_installer.main(arguments)
        self.assertEqual(stdout.getvalue(), "")
        return result, stderr.getvalue()

    def screen(self, keys, size=(22, 60)):
        screen = mock.Mock()
        screen.getmaxyx.return_value = size
        screen.get_wch.side_effect = keys
        return screen

    @staticmethod
    def rendered(screen):
        return "".join(call.args[2] for call in screen.addnstr.call_args_list)

    def test_noninteractive_skill_install_discloses_missing_runtimes_without_installing(self):
        result, report = self.run_install(["install", "omp"])
        self.assertEqual(result, 0)
        self.assertIn("Receiving runtimes", report)
        self.assertIn("runtime install omp", report)
        self.assertIn("runtime install claude", report)
        self.assertIn("Only sessions you choose to restart", report)
        self.assertTrue((self.root / "home/.omp/agent/skills/herdr-agent-messenger/SKILL.md").is_file())
        self.assertFalse((self.root / "home/.omp/agent/extensions").exists())
        self.assertFalse((self.root / "claude").exists())

    def test_runtime_flag_is_explicit_per_client_or_all_consent(self):
        for selection in ("omp", "claude", "all"):
            with self.subTest(selection=selection), tempfile.TemporaryDirectory(dir=self.root) as directory:
                self.environment = {"HOME": str(Path(directory) / "home"),
                                    "CLAUDE_CONFIG_DIR": str(Path(directory) / "claude"),
                                    "LANG": "en_US.UTF-8"}
                result, report = self.run_install(["install", "codex", "--runtime", selection])
                self.assertEqual(result, 0)
                runtimes = agent_skill_installer.runtime_installer.summary(self.environment)["clients"]
                for runtime in runtimes:
                    self.assertEqual(runtime["installed"], selection in (runtime["client"], "all"))
                    self.assertIsNone(runtime["error"])
                self.assertIn("activation pending", report)
                for client in (("omp", "claude") if selection == "all" else (selection,)):
                    self.assertIn(f"{client}: Installed", report)
                    self.assertNotIn(f"{client}: Current", report)
                self.assertFalse((Path(directory) / "home/.omp/agent/skills").exists())

    def test_runtime_flag_reports_conflict_verbatim_and_preserves_foreign_file(self):
        destination = agent_skill_installer.runtime_installer.destinations(self.environment)["omp"]
        destination.mkdir(parents=True)
        target = destination / "ham-mailbox.ts"
        target.write_text("foreign runtime", encoding="utf-8")
        error = agent_skill_installer.runtime_installer.summary(self.environment)["clients"][0]["error"]
        result, report = self.run_install(["install", "all", "--runtime", "all"])
        self.assertEqual(result, 1)
        self.assertIn(error["code"] + ": " + error["message"], report)
        self.assertEqual(target.read_text(encoding="utf-8"), "foreign runtime")
        self.assertTrue(agent_skill_installer.runtime_installer.summary(self.environment)["clients"][1]["installed"])

    def test_runtime_offer_defaults_to_not_now_and_shows_both_clients(self):
        screen = self.screen(["\n"])
        with mock.patch.object(agent_skill_installer.runtime_installer, "manage",
                               wraps=agent_skill_installer.runtime_installer.manage) as manage:
            result = agent_skill_installer.offer_runtime_install(screen, self.environment, agent_skill_installer.COPY["en"])
        self.assertFalse(any(call.args[0] == "install" for call in manage.call_args_list))
        rendered = self.rendered(screen)
        for expected in ("Receiving dependency", "omp: Not installed", "claude: Not installed",
                         "› Not now (default)", "Codex uses skill-guided pull"):
            self.assertIn(expected, rendered)
        self.assertEqual(result, "")
        self.assertNotIn("Skill installed", rendered)
        self.assertFalse((self.root / "home").exists())
        self.assertFalse((self.root / "claude").exists())

    def test_runtime_offer_preserves_skill_install_or_already_current_status(self):
        for key in ("already_current", "installed"):
            with self.subTest(status=key):
                text = agent_skill_installer.COPY["en"]
                screen = self.screen(["\n"], size=(30, 160))
                result = agent_skill_installer.offer_runtime_install(
                    screen, self.environment, text, text[key])
                self.assertEqual(result, text[key])
                self.assertIn(text[key], self.rendered(screen))
                if key == "already_current":
                    self.assertNotIn("Skill installed", self.rendered(screen))

    def test_overlay_passes_accurate_skill_result_to_runtime_offer(self):
        for current in (True, False):
            with self.subTest(current=current):
                screen = self.screen(["\n", "\n", "\x1b"], size=(40, 160))
                status = (agent_skill_installer.STATUS_CURRENT if current
                          else agent_skill_installer.STATUS_MISSING)
                with mock.patch.object(agent_skill_installer.curses, "wrapper",
                                       side_effect=lambda callback: callback(screen)), \
                        mock.patch.object(agent_skill_installer.curses, "curs_set"), \
                        mock.patch.object(agent_skill_installer.curses, "has_colors", return_value=False), \
                        mock.patch.object(agent_skill_installer, "installation_status", return_value=status), \
                        mock.patch.object(agent_skill_installer, "install_target") as install:
                    self.assertEqual(agent_skill_installer.overlay(self.environment), 0)
                rendered = self.rendered(screen)
                if current:
                    install.assert_not_called()
                    self.assertIn("Already current.", rendered)
                    self.assertNotIn("Skill installed", rendered)
                else:
                    install.assert_called_once()
                    self.assertIn("Skill installed. Start a new agent session", rendered)

    def test_installed_runtime_copy_is_not_a_freshness_claim_in_any_locale(self):
        for client, destination in agent_skill_installer.runtime_installer.destinations(self.environment).items():
            agent_skill_installer.runtime_installer.manage("install", destination, client=client)
        for language, text in agent_skill_installer.COPY.items():
            with self.subTest(language=language):
                screen = self.screen(["\n"], size=(30, 160))
                agent_skill_installer.offer_runtime_install(screen, self.environment, text)
                report = agent_skill_installer.runtime_report(self.environment, text)
                for client in ("omp", "claude"):
                    label = f'{client}: {text["runtime_installed"]}'
                    self.assertIn(label, self.rendered(screen))
                    self.assertIn(label, report)
                    self.assertNotIn(f'{client}: {text["current"]}', report)

    def test_runtime_offer_shows_writes_before_install_and_only_installs_selected_client(self):
        for client, down in (("omp", 1), ("claude", 2)):
            with self.subTest(client=client), tempfile.TemporaryDirectory(dir=self.root) as directory:
                environment = {"HOME": str(Path(directory) / "home"), "CLAUDE_CONFIG_DIR": str(Path(directory) / "claude")}
                screen = self.screen([curses.KEY_DOWN] * down + ["\n", "\n"])
                original = agent_skill_installer.runtime_installer.manage

                def manage(operation, destination=None, *, client="omp"):
                    if operation == "install":
                        rendered = self.rendered(screen)
                        self.assertIn("Writes:", rendered)
                        self.assertIn("ham-mailbox.install.json", rendered)
                        self.assertIn(str(destination), rendered)
                        self.assertIn("ham-mailbox.ts" if client == "omp" else "hooks/ham-mailbox.py", rendered)
                        if client == "claude":
                            self.assertIn("settings.json", rendered)
                    return original(operation, destination, client=client)

                with mock.patch.object(agent_skill_installer.runtime_installer, "manage", side_effect=manage):
                    result = agent_skill_installer.offer_runtime_install(screen, environment, agent_skill_installer.COPY["en"])
                self.assertIn("Only sessions you choose to restart", result)
                runtimes = agent_skill_installer.runtime_installer.summary(environment)["clients"]
                self.assertEqual([runtime["client"] for runtime in runtimes if runtime["installed"]], [client])

    def test_runtime_offer_displays_existing_conflict_without_offering_replacement(self):
        destination = agent_skill_installer.runtime_installer.destinations(self.environment)["omp"]
        destination.mkdir(parents=True)
        (destination / "ham-mailbox.ts").write_text("foreign", encoding="utf-8")
        screen = self.screen([curses.KEY_NPAGE, "\n"], size=(10, 60))
        agent_skill_installer.offer_runtime_install(screen, self.environment, agent_skill_installer.COPY["en"])
        rendered = self.rendered(screen)
        self.assertIn("foreign_runtime", rendered)
        self.assertNotIn("Install omp receiver", rendered)
        self.assertEqual((destination / "ham-mailbox.ts").read_text(encoding="utf-8"), "foreign")

    def test_runtime_offer_install_conflict_is_not_suppressed(self):
        screen = self.screen([curses.KEY_DOWN, "\n", "\n"])
        original = agent_skill_installer.runtime_installer.manage

        def manage(operation, destination=None, *, client="omp"):
            if operation == "install":
                raise agent_skill_installer.LedgerError("modified_runtime", "Conflict verbatim.")
            return original(operation, destination, client=client)

        with mock.patch.object(agent_skill_installer.runtime_installer, "manage", side_effect=manage):
            result = agent_skill_installer.offer_runtime_install(screen, self.environment, agent_skill_installer.COPY["ko"])
        self.assertEqual(result, "modified_runtime: Conflict verbatim.")
        self.assertIn("modified_runtime: Conflict verbatim.", self.rendered(screen))

    def test_runtime_flag_rejects_codex_adapter(self):
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
            agent_skill_installer.parse_cli_arguments(["install", "codex", "--runtime", "codex"])
        self.assertEqual(caught.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
