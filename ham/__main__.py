"""Run HAM entrypoints with ``python3 -m ham ENTRY ...`` from the plugin root."""
from __future__ import annotations

import argparse
import importlib
import os


ENTRYPOINTS = {
    "cli": "ham.cli",
    "labels": "ham.agents.labels",
    "delivery": "ham.agents.delivery",
    "mailbox": "ham.ui.mailbox",
    "messenger": "ham.ui.messenger",
    "skill-installer": "ham.ui.skill_installer",
    "worker": "ham.ledger.worker",
    "display": "ham.ledger.display",
    "rollout": "ham.ops.plugin_rollout",
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cwd", help="Preserve a caller directory after locating HAM from the plugin root.")
    parser.add_argument("entry", choices=ENTRYPOINTS)
    parser.add_argument("arguments", nargs=argparse.REMAINDER)
    arguments = parser.parse_args(argv)
    if arguments.cwd is not None:
        try:
            os.chdir(arguments.cwd)
        except OSError as error:
            parser.error(str(error))
    module = importlib.import_module(ENTRYPOINTS[arguments.entry])
    return module.main(arguments.arguments)


if __name__ == "__main__":
    raise SystemExit(main())
