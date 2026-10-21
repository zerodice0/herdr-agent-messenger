"""Run the Agent Messenger skill CLI from a managed plugin checkout."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Mapping


PLUGIN_IDS = ("herdr.agent-messenger", "herdr.agent-labels")


def find_plugin_root(
    script_path: Path | None = None,
    runner=subprocess.run,
    environment: Mapping[str, str] | None = None,
) -> Path:
    source_path = Path(__file__) if script_path is None else script_path
    for bundled_root in source_path.resolve().parents:
        if ((bundled_root / "ham" / "__init__.py").is_file()
                and (bundled_root / "herdr-plugin.toml").is_file()):
            return bundled_root

    values = os.environ if environment is None else environment
    herdr = values.get("HERDR_BIN_PATH") or "herdr"
    last_error: Exception | None = None
    command_error = ""
    for plugin_id in PLUGIN_IDS:
        try:
            result = runner(
                [
                    herdr,
                    "plugin",
                    "list",
                    "--json",
                    "--plugin",
                    plugin_id,
                ],
                capture_output=True,
                check=False,
                text=True,
                timeout=10,
            )
            if result.returncode != 0:
                command_error = (
                    result.stderr.strip()
                    or result.stdout.strip()
                    or f"Herdr exited with status {result.returncode}"
                )
                continue
            payload = json.loads(result.stdout)
            plugins = payload["result"]["plugins"]
            installed_root = Path(plugins[0]["plugin_root"])
        except (OSError, subprocess.TimeoutExpired) as error:
            last_error = error
            command_error = str(error)
            continue
        except (
            IndexError,
            KeyError,
            TypeError,
            ValueError,
            json.JSONDecodeError,
        ) as error:
            last_error = error
            continue
        if ((installed_root / "ham" / "__init__.py").is_file()
                and (installed_root / "herdr-plugin.toml").is_file()):
            return installed_root

    if command_error:
        message = (
            "Could not query HAM plugins through Herdr: "
            f"{command_error}. If this is a sandbox or socket permission error, "
            "rerun with local Herdr socket access."
        )
    else:
        message = (
            "Could not locate HAM as herdr.agent-messenger or the legacy "
            "herdr.agent-labels plugin."
        )
    print(json.dumps({"error": {"code": "cli_not_found", "message": message}}))
    raise SystemExit(1) from last_error


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    return subprocess.call([sys.executable, "-m", "ham", "--cwd", os.getcwd(), "cli", *arguments],
                           cwd=find_plugin_root())


if __name__ == "__main__":
    raise SystemExit(main())
