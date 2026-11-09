"""Real CLI registration must not inherit unrelated host reconciliation work."""
import tests.ham_test_support as ham_test_support

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

from ham.ledger.status import AgentRegistry
from ham.ledger.store import LedgerStore
from tests.test_ledger_core import snapshot

ROOT = Path(__file__).resolve().parents[1]


class RuntimeRegistrationContentionTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="ham-registration-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        for name in ("home", "tmp", "bin", "caller", "unrelated"):
            (self.root / name).mkdir()
        self.environment = {
            **os.environ,
            "HOME": str(self.root / "home"),
            "XDG_CONFIG_HOME": str(self.root / "home/.config"),
            "XDG_STATE_HOME": str(self.root / "home/.local/state"),
            "XDG_CACHE_HOME": str(self.root / "home/.cache"),
            "CLAUDE_CONFIG_DIR": str(self.root / "home/.claude"),
            "TMPDIR": str(self.root / "tmp"),
            "HERDR_AGENT_LEDGER_DIR": str(self.root / "ledger"),
            "HERDR_AGENT_REQUEST_STATE_DIR": str(self.root / "requests"),
            "HERDR_AGENT_LEDGER_AUTOSTART": "0",
            "PYTHONDONTWRITEBYTECODE": "1",
            "HERDR_TERMINAL_ID": "term-b",
            "HERDR_PANE_ID": "w1:p2",
            "HAM_FIXTURE_ROOT": str(self.root),
            "HAM_FIXTURE_ANCESTOR": str(os.getpid()),
            "HERDR_BIN_PATH": str(self.root / "bin/herdr"),
            "PATH": str(self.root / "bin") + os.pathsep + os.environ["PATH"],
        }
        for name in ("caller", "unrelated"):
            subprocess.run(["/usr/bin/git", "init", "-q", str(self.root / name)], check=True,
                           env=self.environment, capture_output=True)
        self.session = str(self.root / "session.jsonl")
        self.snapshot = snapshot(self.root / "unrelated", recipient_session=self.session,
                                 recipient_repo=self.root / "caller")
        self.snapshot["panes"][1]["label"] = "my manual border"
        self.save_snapshot()
        self.executable("herdr", '''import json, os, pathlib, sys
root = pathlib.Path(os.environ["HAM_FIXTURE_ROOT"])
args = sys.argv[1:]
with (root / "herdr-calls").open("a") as log:
    log.write(json.dumps(args) + "\\n")
if args == ["api", "snapshot"]:
    print(json.dumps({"result": {"snapshot": json.loads((root / "snapshot.json").read_text())}}))
elif args[:2] == ["pane", "process-info"]:
    pane = args[args.index("--pane") + 1]
    pid = int(os.environ["HAM_FIXTURE_ANCESTOR"]) if pane == os.environ["HERDR_PANE_ID"] else 1
    print(json.dumps({"result": {"process_info": {"pane_id": pane, "shell_pid": pid}}}))
elif args[:2] == ["pane", "get"]:
    snapshot = json.loads((root / "snapshot.json").read_text())
    pane = next((pane for pane in snapshot["panes"] if pane["pane_id"] == args[2]), None)
    print(json.dumps({"result": {"pane": pane}}))
elif args[:2] in (["pane", "rename"], ["pane", "report-metadata"]):
    snapshot = json.loads((root / "snapshot.json").read_text())
    rows = snapshot["panes"] if args[1] == "rename" else snapshot["agents"]
    row = next(item for item in rows if item["pane_id"] == args[2])
    if args[1] == "rename":
        row["label"] = "" if args[3] == "--clear" else args[3]
    else:
        index = 3
        while index < len(args):
            if args[index] == "--clear-display-agent":
                row.pop("display_agent", None)
                index += 1
                continue
            option, value = args[index:index + 2]
            index += 2
            if option == "--token":
                key, text = value.split("=", 1)
                row.setdefault("tokens", {})[key] = text
            elif option == "--clear-token":
                row.setdefault("tokens", {}).pop(value, None)
            elif option in ("--agent", "--display-agent"):
                row[option[2:].replace("-", "_")] = value
    pending = root / ("snapshot-" + str(os.getpid()) + ".new")
    pending.write_text(json.dumps(snapshot))
    pending.replace(root / "snapshot.json")
    print(json.dumps({"result": {}}))
else:
    print(json.dumps({"result": {}}))
''')
        self.executable("git", '''import os, pathlib, sys, time
root = pathlib.Path(os.environ["HAM_FIXTURE_ROOT"])
args = sys.argv[1:]
parent = os.getppid()
if str(root / "unrelated") in args and (root / "block-git").exists():
    (root / "git-entered").touch()
    while not (root / "release-git").exists():
        if os.getppid() != parent or not root.exists():
            raise SystemExit(1)
        time.sleep(0.01)
if str(root / "caller") in args and (root / "block-caller-git").exists():
    (root / "caller-git-entered").touch()
    while not (root / "release-git").exists():
        if os.getppid() != parent or not root.exists():
            raise SystemExit(1)
        time.sleep(0.01)
os.execv("/usr/bin/git", ["git", *args])
''')
        self.store = LedgerStore(environment=self.environment, canonical_name="fixture")
        self.registry = AgentRegistry(self.store)
        self.registry.reconcile(self.snapshot, refresh_display=False, environment=self.environment)
        self.agent = next(agent for agent in self.registry.list() if agent["terminal_id"] == "term-b")
        self.children = []
        self.addCleanup(self.stop_children)

    def executable(self, name, body):
        path = self.root / "bin" / name
        path.write_text(f"#!{sys.executable}\n" + body)
        path.chmod(0o700)

    def save_snapshot(self):
        destination = self.root / "snapshot.json"
        pending = destination.with_suffix(".new")
        pending.write_text(json.dumps(self.snapshot))
        pending.replace(destination)

    def stop_children(self):
        (self.root / "release-git").touch()
        for child in self.children:
            if child.poll() is None:
                child.terminate()
            try:
                child.communicate(timeout=3)
            except subprocess.TimeoutExpired:
                child.kill()
                child.communicate(timeout=3)
            for stream in (child.stdin, child.stdout, child.stderr):
                if stream is not None:
                    stream.close()

    def start_cli(self, *arguments, payload=None):
        # The optional lock-entry signal schedules a race; the real flock and
        # SQL path still run unchanged.
        program = '''import tests.ham_test_support
import os
from pathlib import Path
from ham.ledger.store import LedgerStore
original_lock = LedgerStore.lock
def watched_lock(self, name, **kwargs):
    if name == os.environ.get("HAM_FIXTURE_WATCH_LOCK"):
        (Path(os.environ["HAM_FIXTURE_ROOT"]) / "binding-lock-entered").touch()
    return original_lock(self, name, **kwargs)
LedgerStore.lock = watched_lock
from ham.__main__ import main
raise SystemExit(main())
'''
        child = subprocess.Popen([sys.executable, "-c", program, "cli", *arguments],
                                 cwd=ROOT, env=self.environment, stdin=subprocess.PIPE,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.children.append(child)
        child.stdin.write(json.dumps(payload or {}))
        child.stdin.close()
        child.stdin = None
        return child

    def finish(self, child, *, success=True, timeout=3):
        try:
            stdout, stderr = child.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.fail("Runtime CLI waited for unrelated host work instead of completing")
        self.assertEqual(stderr, "", stderr)
        self.assertEqual(child.returncode, 0 if success else 1, stdout)
        return json.loads(stdout)

    def runtime(self, operation, *, success=True, **payload):
        return self.finish(self.start_cli("ledger", "runtime", operation, payload=payload), success=success)

    def inspect_payload(self):
        return {key: self.agent[key] for key in ("terminal_id", "agent_id", "generation")}

    def blocked_sweep(self):
        (self.root / "block-git").touch()
        child = self.start_cli("ledger", "reconcile")
        deadline = time.monotonic() + 3
        while not (self.root / "git-entered").exists():
            if child.poll() is not None or time.monotonic() >= deadline:
                self.fail("Fixture sweep did not reach the controlled unrelated Git probe")
            time.sleep(0.01)
        return child

    def release_sweep(self, child, *, superseded=False):
        (self.root / "release-git").touch()
        result = self.finish(child, success=not superseded)
        if superseded:
            self.assertEqual(result["error"]["code"], "stale_snapshot")
        return result

    def test_register_does_not_probe_unrelated_git_or_project_manual_border(self):
        (self.root / "block-git").touch()
        result = self.runtime("register", session_ref=self.session)
        self.assertEqual(result["agent"]["agent_id"], self.agent["agent_id"])
        self.assertTrue(result["agent"]["extension_present"])
        self.assertFalse((self.root / "git-entered").exists())
        calls = [json.loads(line) for line in (self.root / "herdr-calls").read_text().splitlines()]
        self.assertFalse(any(call[:2] == ["pane", "rename"] for call in calls))

    def test_register_and_inspect_complete_while_full_sweep_is_in_unrelated_git(self):
        sweep = self.blocked_sweep()
        registered = self.runtime("register", session_ref=self.session)
        self.assertEqual(registered["agent"]["generation"], self.agent["generation"])
        self.runtime("inspect", **self.inspect_payload())
        self.assertIsNone(sweep.poll())
        self.release_sweep(sweep)

    def test_register_does_not_wait_for_another_panes_display_lock(self):
        with self.store.display_lock(self.store.host, "w1:p1", timeout=1):
            result = self.runtime("register", session_ref=self.session)
        self.assertEqual(result["agent"]["agent_id"], self.agent["agent_id"])

    def test_stable_registration_and_inspection_do_not_wait_for_caller_projection(self):
        with self.store.display_lock(self.store.host, "w1:p2", timeout=1):
            self.runtime("register", session_ref=self.session)
            self.runtime("inspect", **self.inspect_payload())

    def test_session_replaced_during_caller_git_is_refused_before_registration(self):
        (self.root / "block-caller-git").touch()
        child = self.start_cli("ledger", "runtime", "register", payload={"session_ref": self.session})
        deadline = time.monotonic() + 3
        while not (self.root / "caller-git-entered").exists():
            if child.poll() is not None or time.monotonic() >= deadline:
                self.fail("Caller did not reach its controlled Git probe")
            time.sleep(0.01)
        self.snapshot["agents"][1]["agent_session"]["value"] = str(self.root / "replacement.jsonl")
        self.save_snapshot()
        (self.root / "release-git").touch()
        result = self.finish(child, success=False)
        self.assertEqual(result["error"]["code"], "stale_session")
        with self.store.read() as connection:
            row = connection.execute("SELECT session_ref,extension_present FROM agents WHERE agent_id=?",
                                     (self.agent["agent_id"],)).fetchone()
        self.assertEqual(tuple(row), (self.session, 0))

    def test_concurrent_registrations_share_one_current_generation(self):
        children = [self.start_cli("ledger", "runtime", "register", payload={"session_ref": self.session})
                    for _ in range(4)]
        results = [self.finish(child) for child in children]
        self.assertEqual({result["agent"]["agent_id"] for result in results}, {self.agent["agent_id"]})
        self.assertEqual({result["agent"]["generation"] for result in results}, {self.agent["generation"]})
        active = [agent for agent in self.registry.list() if agent["terminal_id"] == "term-b"]
        self.assertEqual([agent["agent_id"] for agent in active], [self.agent["agent_id"]])

    def test_late_full_snapshot_cannot_restore_replaced_caller_session(self):
        sweep = self.blocked_sweep()
        replacement = str(self.root / "replacement.jsonl")
        self.snapshot["agents"][1]["agent_session"]["value"] = replacement
        self.save_snapshot()
        registered = self.runtime("register", session_ref=replacement)["agent"]
        self.assertGreater(registered["generation"], self.agent["generation"])
        self.release_sweep(sweep, superseded=True)
        current = next(agent for agent in self.registry.list() if agent["terminal_id"] == "term-b")
        self.assertEqual((current["agent_id"], current["session_ref"], current["generation"]),
                         (registered["agent_id"], replacement, registered["generation"]))
        self.assertFalse(self.runtime("inspect", success=False, **self.inspect_payload()).get("ok", False))

    def test_inspect_refuses_ended_or_replaced_caller_without_a_worker_sweep(self):
        for ended in (False, True):
            with self.subTest(ended=ended):
                if ended:
                    self.snapshot["agents"] = self.snapshot["agents"][:1]
                else:
                    self.snapshot["agents"][1]["agent_session"]["value"] = str(self.root / "other.jsonl")
                self.save_snapshot()
                result = self.runtime("inspect", success=False, **self.inspect_payload())
                self.assertIn(result["error"]["code"], {"stale_session", "sender_unresolved", "recipient_absent"})

    def test_wrong_session_cannot_register_extension_on_current_caller(self):
        result = self.runtime("register", success=False, session_ref=str(self.root / "wrong.jsonl"))
        self.assertEqual(result["error"]["code"], "stale_session")
        with self.store.read() as connection:
            row = connection.execute("SELECT extension_present FROM agents WHERE agent_id=?",
                                     (self.agent["agent_id"],)).fetchone()
        self.assertEqual(row[0], 0)

    def test_first_registration_does_not_need_a_host_sweep(self):
        self.environment["HERDR_AGENT_LEDGER_DIR"] = str(self.root / "fresh-ledger")
        (self.root / "block-git").touch()
        registered = self.runtime("register", session_ref=self.session)["agent"]
        self.assertEqual((registered["terminal_id"], registered["session_ref"], registered["generation"]),
                         ("term-b", self.session, 1))
        self.assertTrue(registered["extension_present"])
        self.assertFalse((self.root / "git-entered").exists())

    def test_inspect_cannot_borrow_another_terminals_generation(self):
        other = next(agent for agent in self.registry.list() if agent["terminal_id"] == "term-a")
        result = self.runtime("inspect", success=False,
                              **{key: other[key] for key in ("terminal_id", "agent_id", "generation")})
        self.assertEqual(result["error"]["code"], "stale_session")

    def test_caller_registration_preserves_unrelated_active_identity(self):
        other = next(agent for agent in self.registry.list() if agent["terminal_id"] == "term-a")
        self.runtime("register", session_ref=self.session)
        current = next(agent for agent in self.registry.list() if agent["terminal_id"] == "term-a")
        self.assertEqual(current, other)

    def test_pane_rebind_and_repository_move_use_current_live_identity(self):
        self.snapshot["panes"][1]["pane_id"] = "w2:p8"
        self.snapshot["panes"][1]["workspace_id"] = "w2"
        self.snapshot["agents"][1].update(pane_id="w2:p8", workspace_id="w2", cwd=str(self.root / "unrelated"))
        self.environment["HERDR_PANE_ID"] = "w2:p8"
        self.save_snapshot()
        registered = self.runtime("register", session_ref=self.session)["agent"]
        self.assertEqual((registered["pane_id"], registered["workspace_id"]), ("w2:p8", "w2"))
        self.assertNotEqual(registered["repo_id"], self.agent["repo_id"])
        self.assertGreater(registered["generation"], self.agent["generation"])
        self.assertEqual(registered["last_ham_label"], None)

    def test_rebinding_waits_for_existing_projector_before_committing_location(self):
        old_pane = self.agent["pane_id"]
        self.snapshot["panes"][1].update(pane_id="w2:p8", workspace_id="w2")
        self.snapshot["agents"][1].update(pane_id="w2:p8", workspace_id="w2")
        self.environment["HERDR_PANE_ID"] = "w2:p8"
        self.environment["HAM_FIXTURE_WATCH_LOCK"] = self.store.display_lock_key(
            self.store.host, old_pane).removesuffix(".lock")
        self.save_snapshot()
        with self.store.display_lock(self.store.host, old_pane, timeout=1):
            child = self.start_cli("ledger", "runtime", "register", payload={"session_ref": self.session})
            deadline = time.monotonic() + 3
            while not (self.root / "binding-lock-entered").exists():
                if child.poll() is not None or time.monotonic() >= deadline:
                    self.fail("Rebinding bypassed the old pane projector lock")
                time.sleep(0.01)
            current = next(agent for agent in self.registry.list() if agent["terminal_id"] == "term-b")
            self.assertEqual(current["pane_id"], old_pane)
            self.assertIsNone(child.poll())
        registered = self.finish(child)["agent"]
        self.assertEqual((registered["agent_id"], registered["generation"]),
                         (self.agent["agent_id"], self.agent["generation"]))
        self.assertEqual((registered["pane_id"], registered["workspace_id"]), ("w2:p8", "w2"))

    def test_session_replaced_while_waiting_for_projector_is_not_registered(self):
        old_pane = self.agent["pane_id"]
        self.snapshot["panes"][1].update(pane_id="w2:p8", workspace_id="w2")
        self.snapshot["agents"][1].update(pane_id="w2:p8", workspace_id="w2")
        self.environment["HERDR_PANE_ID"] = "w2:p8"
        self.environment["HAM_FIXTURE_WATCH_LOCK"] = self.store.display_lock_key(
            self.store.host, old_pane).removesuffix(".lock")
        self.save_snapshot()
        with self.store.display_lock(self.store.host, old_pane, timeout=1):
            child = self.start_cli("ledger", "runtime", "register", payload={"session_ref": self.session})
            deadline = time.monotonic() + 3
            while not (self.root / "binding-lock-entered").exists():
                if child.poll() is not None or time.monotonic() >= deadline:
                    self.fail("Caller did not reach the projector lock barrier")
                time.sleep(0.01)
            self.snapshot["agents"][1]["agent_session"]["value"] = str(self.root / "replacement.jsonl")
            self.save_snapshot()
        result = self.finish(child, success=False)
        self.assertEqual(result["error"]["code"], "stale_session")
        with self.store.read() as connection:
            row = connection.execute("SELECT pane_id,session_ref,extension_present FROM agents WHERE agent_id=?",
                                     (self.agent["agent_id"],)).fetchone()
        self.assertEqual(tuple(row), (old_pane, self.session, 0))

    def test_successful_unchanged_inspect_fences_an_older_different_full_snapshot(self):
        self.snapshot["agents"][1]["agent_session"]["value"] = str(self.root / "temporary-session.jsonl")
        self.save_snapshot()
        sweep = self.blocked_sweep()
        self.snapshot["agents"][1]["agent_session"]["value"] = self.session
        self.save_snapshot()
        self.runtime("inspect", **self.inspect_payload())
        self.release_sweep(sweep, superseded=True)
        current = next(agent for agent in self.registry.list() if agent["terminal_id"] == "term-b")
        self.assertEqual((current["agent_id"], current["session_ref"], current["generation"]),
                         (self.agent["agent_id"], self.session, self.agent["generation"]))

    def test_contended_event_leaves_off_mode_worker_work_until_closure_is_reconciled(self):
        from ham.ledger.settings import RepoSettings
        from ham.ledger.worker import maintenance_pending
        for repo_id in {agent["repo_id"] for agent in self.registry.list()}:
            RepoSettings(self.store, environment=self.environment).mutate(repo_id, "mode", "off")
        self.assertFalse(maintenance_pending(self.store))
        self.snapshot = json.loads((self.root / "snapshot.json").read_text())
        self.snapshot["panes"] = self.snapshot["panes"][1:]
        self.snapshot["agents"] = self.snapshot["agents"][1:]
        self.save_snapshot()
        with self.store.display_lock(self.store.host, self.agent["pane_id"], timeout=1):
            result = self.finish(self.start_cli("ledger", "event"), success=False)
        self.assertEqual(result["error"]["code"], "ledger_busy")
        self.assertTrue(maintenance_pending(self.store))
        worker = self.start_cli("ledger", "worker", "once")
        stdout, stderr = worker.communicate(timeout=5)
        self.assertEqual(worker.returncode, 0, stdout + stderr)
        self.assertEqual([agent["terminal_id"] for agent in self.registry.list()], ["term-b"])
        self.assertFalse(maintenance_pending(self.store))

    def test_scoped_pane_swap_does_not_replace_the_other_live_session(self):
        self.runtime("register", session_ref=self.session)
        original = {agent["terminal_id"]: agent for agent in self.registry.list()}
        for rows in (self.snapshot["panes"], self.snapshot["agents"]):
            rows[0]["pane_id"], rows[1]["pane_id"] = rows[1]["pane_id"], rows[0]["pane_id"]
        self.environment["HERDR_PANE_ID"] = "w1:p1"
        self.save_snapshot()
        child = self.start_cli("ledger", "runtime", "register", payload={"session_ref": self.session})
        stdout, stderr = child.communicate(timeout=3)
        self.assertEqual(stderr, "")
        result = json.loads(stdout)
        if child.returncode:
            self.assertEqual(result["error"]["code"], "stale_snapshot")
            self.finish(self.start_cli("ledger", "reconcile"))
            self.runtime("register", session_ref=self.session)
        identity = ("agent_id", "generation", "session_ref", "extension_present", "phase", "summary")
        current = {agent["terminal_id"]: agent for agent in self.registry.list()}
        for terminal, pane in (("term-a", "w1:p2"), ("term-b", "w1:p1")):
            self.assertEqual(tuple(current[terminal][key] for key in identity),
                             tuple(original[terminal][key] for key in identity))
            self.assertEqual(current[terminal]["pane_id"], pane)

    def test_old_caller_metadata_cannot_reverse_a_newer_shared_workspace_observation(self):
        self.snapshot["workspaces"] = [{
            "workspace_id": "w1", "label": "Old workspace",
            "worktree": {"repo_key": "old-key", "is_linked_worktree": False},
        }]
        self.save_snapshot()
        self.registry.reconcile(self.snapshot, refresh_display=False, environment=self.environment)
        (self.root / "block-caller-git").touch()
        old = self.start_cli("ledger", "runtime", "register", payload={"session_ref": self.session})
        deadline = time.monotonic() + 3
        while not (self.root / "caller-git-entered").exists():
            if old.poll() is not None or time.monotonic() >= deadline:
                self.fail("Old workspace observation did not reach the Git barrier")
            time.sleep(0.01)
        self.snapshot["workspaces"][0].update(
            label="New workspace", worktree={"repo_key": "new-key", "is_linked_worktree": True})
        self.save_snapshot()
        other = next(agent for agent in self.registry.list() if agent["terminal_id"] == "term-a")
        self.environment.update(HERDR_TERMINAL_ID="term-a", HERDR_PANE_ID="w1:p1")
        newer = self.start_cli("ledger", "runtime", "inspect",
                               payload={key: other[key] for key in ("terminal_id", "agent_id", "generation")})
        newer_stdout, newer_stderr = newer.communicate(timeout=3)
        self.assertEqual(newer_stderr, "")
        if newer.returncode:
            self.assertEqual(json.loads(newer_stdout)["error"]["code"], "stale_snapshot")
        (self.root / "release-git").touch()
        old_stdout, old_stderr = old.communicate(timeout=3)
        self.assertEqual(old_stderr, "")
        self.assertEqual(old.returncode, 1, old_stdout)
        self.assertIn(json.loads(old_stdout)["error"]["code"], {"stale_session", "stale_snapshot"})
        if newer.returncode == 0:
            with self.store.read() as connection:
                before_sweep = connection.execute(
                    "SELECT repo_key,is_linked_worktree,label FROM workspaces WHERE workspace_id='w1'").fetchone()
            self.assertEqual(tuple(before_sweep), ("new-key", 1, "New workspace"))
        self.finish(self.start_cli("ledger", "reconcile"))
        with self.store.read() as connection:
            row = connection.execute(
                "SELECT repo_key,is_linked_worktree,label FROM workspaces WHERE workspace_id='w1'").fetchone()
        self.assertEqual(tuple(row), ("new-key", 1, "New workspace"))


if __name__ == "__main__":
    unittest.main()
