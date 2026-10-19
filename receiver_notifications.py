"""Best-effort, host-local receiving-runtime disclosure after registry refresh."""
from __future__ import annotations

from typing import Mapping

import agent_messenger
import runtime_installer
from ledger_store import LedgerStore, json_text

META_KEY = "receiver_runtime_notification"


def _record_failure(store: LedgerStore, error: Exception) -> None:
    message = "HAM receiver startup notification failed after refresh: " + str(error)
    warning = {"code": "notification_failed", "message": message,
               "committed": True, "recovery": "event_or_safety_sweep"}
    if warning not in store.notification_errors:
        store.notification_errors.append(warning)
    try:
        store.note_notification_failure(message)
    except Exception:
        pass


def notify_missing_runtime(store: LedgerStore, environment: Mapping[str, str] | None = None) -> None:
    """Notify once per changed set, never for installed-but-unregistered sessions.

    Claim the set durably before notifying so concurrent startup/event reconciles
    cannot duplicate the popup. A failed popup is diagnostic, not a retry loop.
    Recording the empty set lets a resolved dependency be reported if it recurs.
    """
    try:
        with store.read() as connection:
            active = {row[0] for row in connection.execute(
                "SELECT DISTINCT client FROM agents WHERE host=? AND active=1 "
                "AND client IN ('omp','claude')", (store.host,))}
        clients = runtime_installer.summary(environment)["clients"] if active else []
        missing = sorted((item["client"], "error:" + item["error"]["code"]
                          if item.get("error") else "not_installed")
                         for item in clients if item["client"] in active and
                         (item.get("error") or not item["installed"]))
        state = json_text(missing)
        with store.transaction() as connection:
            previous = connection.execute("SELECT value FROM ledger_meta WHERE key=?", (META_KEY,)).fetchone()
            if previous is not None and previous[0] == state:
                return
            connection.execute("INSERT INTO ledger_meta(key,value) VALUES (?,?) "
                               "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (META_KEY, state))
        if missing:
            names = ", ".join(f"{client} ({reason})" for client, reason in missing)
            body = (f"HAM receiving runtime unavailable: {names}. Run runtime status; "
                    "ask the user before runtime install omp/claude, then restart only the selected sessions.")
            if not agent_messenger.show_notification(body, environment):
                raise RuntimeError("Herdr notification was not accepted")
    except Exception as error:
        _record_failure(store, error)
