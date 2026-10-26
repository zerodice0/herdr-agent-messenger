import tests.ham_test_support as ham_test_support  # Seal live state before product imports.
import json
import os
from pathlib import Path
import shlex
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from ham.agents import directory


class RemoteShellTransportTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.binary = self.root / "argv-recorder"
        self.binary.write_text(
            f"#!{sys.executable}\n"
            "import json, os, sys\n"
            "print(json.dumps({'argv': sys.argv[1:], 'cwd': os.getcwd(), "
            "'socket': os.environ.get('HERDR_SOCKET_PATH'), "
            "'stdin': sys.stdin.read() if os.environ.get('READ_STDIN') else None}))\n",
            encoding="utf-8",
        )
        self.binary.chmod(0o700)
        self.environment = {**os.environ, "HERDR_BIN_PATH": str(self.binary)}
        self.marker = self.root / "INJECTED"
        self.inputs = [
            "\\'", "\\\\", "trailing\\", "$(touch " + str(self.marker) + ")",
            "(", "line one\nline two\n", "single ' and double \" quotes",
            "한국어 🐦 café", "", "\\'\"; echo INJECTED; #",
            "\\'\"; touch " + str(self.marker) + "; #",
        ]

    def run_shell(self, shell, command, **kwargs):
        result = subprocess.run(
            [shell, "-c", command], env=self.environment, capture_output=True,
            text=True, check=False, timeout=5, **kwargs,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.marker.exists(), "untrusted content executed a command")
        return json.loads(result.stdout)

    def assert_herdr_round_trip(self, shell):
        for prompt in self.inputs:
            with self.subTest(shell=shell, prompt=prompt):
                arguments = ["agent", "prompt", "target", prompt]
                socket_path = "socket ' \\\"\n$() 경로"
                command = directory._remote_herdr_command(arguments, remote_socket=socket_path)
                shell_name, flag, decoder, name, encoded = shlex.split(command)
                self.assertEqual((shell_name, flag, name), ("sh", "-c", "ham"))
                self.assertNotIn("'", decoder)
                self.assertNotIn("\\", decoder)
                self.assertRegex(encoded, r"\A[A-Za-z0-9+/=]+\Z")
                payload = self.run_shell(shell, command)
                self.assertEqual(payload["argv"], arguments)
                self.assertEqual(payload["socket"], socket_path)

    def test_exact_argv_round_trip_under_sh(self):
        self.assert_herdr_round_trip("/bin/sh")

    @unittest.skipUnless(Path("/usr/bin/fish").exists(), "fish is not installed")
    def test_exact_argv_round_trip_under_fish(self):
        self.assert_herdr_round_trip("/usr/bin/fish")

    def assert_program_round_trip(self, shell):
        cwd = self.root / "cwd \\'\" $() 경로"
        cwd.mkdir(exist_ok=True)
        self.environment["READ_STDIN"] = "1"
        stdin = '{"body":"ledger wire stays on stdin\\n한국어"}\n'
        arguments = ["env", "TRANSPORT_VALUE=\\'\"; echo INJECTED; #", str(self.binary), *self.inputs]
        with mock.patch.object(directory, "_ssh_control_path", return_value=None):
            command = directory.ssh_program_command(
                "remote", arguments, cwd=str(cwd), config_path=self.root / "ssh-config",
            )
        payload = self.run_shell(shell, command[-1], input=stdin)
        self.assertEqual(payload["argv"], self.inputs)
        self.assertEqual(payload["cwd"], str(cwd))
        self.assertEqual(payload["stdin"], stdin)

    def test_program_arguments_cwd_and_wire_stdin_under_sh(self):
        self.assert_program_round_trip("/bin/sh")

    @unittest.skipUnless(Path("/usr/bin/fish").exists(), "fish is not installed")
    def test_program_arguments_cwd_and_wire_stdin_under_fish(self):
        self.assert_program_round_trip("/usr/bin/fish")

    def test_bsd_decoder_fallback_discards_failed_decode_output(self):
        decoder = self.root / "bsd-base64"
        decoder.write_text(
            f"#!{sys.executable}\n"
            "import base64, sys\n"
            "if sys.argv[1] == '-d':\n"
            "    print('partial failed decode')\n"
            "    sys.exit(1)\n"
            "assert sys.argv[1] == '-D'\n"
            "sys.stdout.buffer.write(base64.b64decode(sys.stdin.buffer.read()))\n",
            encoding="utf-8",
        )
        decoder.chmod(0o700)
        command = shlex.split(directory._remote_herdr_command(["agent", "list"]))
        command[2] = command[2].replace("/usr/bin/base64", shlex.quote(str(decoder)))
        result = subprocess.run(command, env=self.environment, capture_output=True,
                                text=True, check=False, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["argv"], ["agent", "list"])


class SSHConnectionReuseTest(unittest.TestCase):
    def test_private_control_directory_and_short_socket_path(self):
        home = Path("/Users/example")
        metadata = mock.Mock(st_mode=stat.S_IFDIR | 0o755, st_uid=os.getuid())
        with mock.patch.object(Path, "home", return_value=home), \
                mock.patch.object(Path, "mkdir") as mkdir, \
                mock.patch.object(Path, "lstat", return_value=metadata), \
                mock.patch.object(Path, "chmod") as chmod:
            path = directory._ssh_control_path()
        self.assertEqual(path, "/Users/example/.ssh/ham-cm/%C")
        self.assertLess(len(os.fsencode(path.replace("%C", "a" * 40))), 104)
        mkdir.assert_called_once_with(mode=0o700, parents=True, exist_ok=True)
        chmod.assert_called_once_with(0o700)

    def test_control_directory_rejects_symlinks_and_foreign_owners(self):
        for mode, uid in ((stat.S_IFLNK | 0o777, os.getuid()),
                          (stat.S_IFDIR | 0o700, os.getuid() + 1)):
            with self.subTest(mode=mode, uid=uid), \
                    mock.patch.object(Path, "home", return_value=Path("/Users/example")), \
                    mock.patch.object(Path, "mkdir"), \
                    mock.patch.object(Path, "lstat", return_value=mock.Mock(st_mode=mode, st_uid=uid)), \
                    mock.patch.object(Path, "chmod") as chmod:
                with self.assertRaises(ValueError):
                    directory._ssh_control_path()
                chmod.assert_not_called()

    def test_long_home_disables_reuse_without_creating_an_overlong_socket(self):
        with mock.patch.object(Path, "home", return_value=Path("/Users/" + "x" * 100)), \
                mock.patch.object(Path, "mkdir") as mkdir:
            command = directory._ssh_transport_command("remote", "true", config_path=Path("/config"))
        mkdir.assert_not_called()
        self.assertIn("ControlMaster=no", command)
        self.assertIn("ControlPath=none", command)

    def test_control_path_temporary_socket_boundaries(self):
        metadata = mock.Mock(st_mode=stat.S_IFDIR | 0o700, st_uid=os.getuid())
        for platform, limit in (("darwin", 104), ("linux", 108)):
            for offset in (-1, 0, 1):
                for prefix in ("/", "/한"):
                    home_bytes = limit + offset - (1 + 40 + 17) - len(b"/.ssh/ham-cm")
                    home = Path(prefix + "x" * (home_bytes - len(os.fsencode(prefix))))
                    control_directory = home / ".ssh" / "ham-cm"
                    self.assertEqual(
                        len(os.fsencode(control_directory)) + 1 + 40 + 17,
                        limit + offset,
                    )
                    with self.subTest(platform=platform, offset=offset, prefix=prefix), \
                            mock.patch.object(sys, "platform", platform), \
                            mock.patch.object(Path, "home", return_value=home), \
                            mock.patch.object(Path, "mkdir") as mkdir, \
                            mock.patch.object(Path, "lstat", return_value=metadata) as lstat, \
                            mock.patch.object(Path, "chmod") as chmod:
                        command = directory._ssh_transport_command(
                            "remote", "true", config_path=Path("/config"),
                        )
                    if offset < 0:
                        self.assertIn("ControlMaster=auto", command)
                        self.assertEqual(
                            command[command.index("-S") + 1], str(control_directory / "%C"),
                        )
                        mkdir.assert_called_once_with(mode=0o700, parents=True, exist_ok=True)
                        lstat.assert_called_once_with()
                        chmod.assert_called_once_with(0o700)
                    else:
                        self.assertIn("ControlMaster=no", command)
                        self.assertIn("ControlPath=none", command)
                        self.assertNotIn("ControlMaster=auto", command)
                        self.assertNotIn("-S", command)
                        mkdir.assert_not_called()
                        lstat.assert_not_called()
                        chmod.assert_not_called()

    def test_reuse_preserves_noninteractive_hardening_and_connection_timeout(self):
        path = "/Users/example/.ssh/ham-cm/%C"
        with mock.patch.object(directory, "_ssh_control_path", return_value=path):
            command = directory._ssh_transport_command("remote", "true", config_path=Path("/config"))
        for option in ("ControlMaster=auto", "ControlPersist=60", "BatchMode=yes",
                       "ConnectionAttempts=1", "ConnectTimeout=3", "ClearAllForwardings=yes",
                       "ForwardAgent=no", "ForwardX11=no", "PermitLocalCommand=no"):
            self.assertIn(option, command)
        self.assertEqual(command[command.index("-S") + 1], path)
        self.assertEqual(command[-3:], ["-T", "remote", "true"])


if __name__ == "__main__":
    unittest.main()
