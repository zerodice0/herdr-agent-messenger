import ham_test_support  # Protect the live ledger before product imports.
from contextlib import redirect_stderr
from pathlib import Path
import curses
import io
import tempfile
import unittest
from unittest import mock

import agent_skill_installer


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

    def test_launch_opens_a_responsive_installer_popup(self):
        with mock.patch.object(
            agent_skill_installer,
            "launch_plugin_popup",
            return_value=True,
        ) as launch_popup:
            self.assertEqual(agent_skill_installer.launch({"LANG": "ko_KR.UTF-8"}), 0)

        launch_popup.assert_called_once_with(
            agent_skill_installer.SKILL_INSTALLER_ENTRYPOINT,
            width=60,
            height=22,
            environment={"LANG": "ko_KR.UTF-8"},
            extra_arguments=(),
        )

    def test_popup_arrow_selection_installs_the_highlighted_target(self):
        screen = mock.Mock()
        screen.getmaxyx.return_value = (22, 60)
        screen.get_wch.side_effect = [curses.KEY_DOWN] * 5 + ["\n", "\x1b"]

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
                agent_skill_installer.popup(
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
                agent_skill_installer.popup(
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


if __name__ == "__main__":
    unittest.main()
