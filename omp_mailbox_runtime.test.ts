import { afterEach, beforeEach, expect, test, setSystemTime } from "bun:test";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import type { ExtensionAPI, ExtensionContext } from "@oh-my-pi/pi-coding-agent";
import { MailboxAdapter, callReceiver, parseReply, type ClaimBundle, type NativeMessage } from "./integrations/omp/ham-mailbox";

const root = path.dirname(fileURLToPath(import.meta.url));

const herdrEnvironmentKeys = ["HERDR_ENV", "HERDR_SOCKET_PATH", "HERDR_PANE_ID", "HERDR_TERMINAL_ID", "OMPCODE"] as const;
let previousHerdrEnvironment = new Map<string, string | undefined>();
beforeEach(() => {
  previousHerdrEnvironment = new Map(herdrEnvironmentKeys.map(key => [key, process.env[key]]));
  process.env.HERDR_ENV = "1";
  process.env.HERDR_SOCKET_PATH = path.join(process.env.TMPDIR || root, "ham-omp-test-unused.sock");
  process.env.HERDR_PANE_ID = "fixture-pane";
  delete process.env.HERDR_TERMINAL_ID;
  delete process.env.OMPCODE;
});
const fixtures: Fixture[] = [];
interface ViewMessage { state: string; claim_id: string | null; hold_reason: string | null; claimed_generation: number | null }
interface View { messages: ViewMessage[]; wakes: number }
type AcceptedMessage = NativeMessage & { type: "custom_message"; id: string; parentId: null; timestamp: string };

function ledgerView(value: unknown): View {
  if (typeof value !== "object" || value === null || !("messages" in value) || !Array.isArray(value.messages) ||
      !("wakes" in value) || typeof value.wakes !== "number") throw new Error("Invalid test ledger view");
  return { wakes: value.wakes, messages: value.messages.map((row: unknown) => {
    if (typeof row !== "object" || row === null || !("state" in row) || typeof row.state !== "string" ||
        !("claim_id" in row) || (row.claim_id !== null && typeof row.claim_id !== "string") ||
        !("hold_reason" in row) || (row.hold_reason !== null && typeof row.hold_reason !== "string") ||
        !("claimed_generation" in row) || (row.claimed_generation !== null && typeof row.claimed_generation !== "number"))
      throw new Error("Invalid test delivery row");
    return { state: row.state, claim_id: row.claim_id, hold_reason: row.hold_reason, claimed_generation: row.claimed_generation };
  }) };
}
const fixtureCode = `
import json,sys
from pathlib import Path
from ledger_store import LedgerStore
from ledger_status import AgentRegistry
from ledger_mail import Mailbox
from ledger_runtime import NativeReceiver
from ledger_settings import RepoSettings,DEFAULT_THRESHOLDS
from test_ledger_core import snapshot
root=Path(sys.argv[1]); op=sys.argv[2]; data=json.load(sys.stdin)
repo=root/'repo'; repo.mkdir(exist_ok=True)
store=LedgerStore(root/'ledger',canonical_name='alpha'); registry=AgentRegistry(store)
if op=='setup':
 registry.reconcile(snapshot(repo,recipient_session=str(root/'session.jsonl')))
 settings=RepoSettings(store); rid=registry.list()[0]['repo_id']
 settings.mutate(rid,'mode','auto')
 settings.mutate(rid,'thresholds',{**DEFAULT_THRESHOLDS,'idle_minutes':data.get('idle_minutes',0),'coalesce_seconds':0})
if op=='session': registry.reconcile(snapshot(repo,recipient_session=data['session_ref']))
agents=registry.list(); sender=next(a for a in agents if a['terminal_id']=='term-a'); recipient=next(a for a in agents if a['terminal_id']=='term-b')
mail=Mailbox(store)
if op in ('setup','session'): result={'agent':recipient}
elif op=='register': result=NativeReceiver(store).register(recipient,**data)
elif op=='send': result=mail.send(sender,recipient,**data)
elif op=='view':
 with store.read() as c:
  result={'messages':[dict(r) for r in c.execute('SELECT m.message_id,m.kind,d.state,d.claim_id,d.hold_reason,d.claimed_generation FROM messages m JOIN deliveries d USING(message_id) ORDER BY m.created_at,m.message_id')], 'wakes':c.execute('SELECT count(*) FROM wake_log').fetchone()[0]}
else: raise Exception(op)
print(json.dumps(result))
`;

class Fixture {
  directory = mkdtempSync(path.join(os.tmpdir(), "ham-omp-lifecycle-"));
  environment = { ...process.env, HERDR_ENV: "1", HERDR_SOCKET_PATH: path.join(this.directory, "herdr.sock"),
    HERDR_PANE_ID: "fixture-pane", HERDR_AGENT_LEDGER_DIR: path.join(this.directory, "ledger"),
    HERDR_AGENT_LEDGER_AUTOSTART: "0", PYTHONDONTWRITEBYTECODE: "1" };
  session = path.join(this.directory, "session.jsonl");
  branch: AcceptedMessage[] = [];
  editor = "";
  editorSupported = true;
  idle = true;
  queued = false;
  kind = "main";
  mode = "tui";
  sent: { message: NativeMessage; options: { deliverAs?: string; triggerTurn?: boolean } }[] = [];
  warnings: string[] = [];
  statuses = new Map<string, string>();
  statusChanges: { key: string; text: string | undefined }[] = [];
  operations: string[] = [];
  timers = new Map<object, () => unknown>();
  timeoutTokens = new Set<object>();
  terminalInput?: (data: string) => unknown;
  context: ExtensionContext;
  adapter: MailboxAdapter;
  afterClaim?: (data: Record<string, unknown>, result: ClaimBundle) => Promise<void>;
  beforeRegister?: (data: Record<string, unknown>) => Promise<void>;
  afterRegister?: (data: Record<string, unknown>) => Promise<void>;
  throwSend = false;

  constructor() {
    const timeout = (callback: () => unknown) => {
      const token = {}; this.timers.set(token, callback); this.timeoutTokens.add(token); return token;
    };
    const interval = (callback: () => unknown) => { const token = {}; this.timers.set(token, callback); return token; };
    const owner = this;
    const context = {
      get mode() { return owner.mode; }, hasUI: true, cwd: path.join(this.directory, "repo"),
      get agent() { return { kind: owner.kind }; },
      sessionManager: { getSessionFile: () => this.session, getBranch: () => [...this.branch] },
      isIdle: () => this.idle, hasPendingMessages: () => this.queued,
      setTimeout: timeout, setInterval: interval,
      clearTimer: (token: object) => { this.timeoutTokens.delete(token); return this.timers.delete(token); },
      ui: {
        get supportsEditor() { return owner.editorSupported; },
        getEditorText: () => this.editor, getEditorComponent: () => undefined,
        onTerminalInput: (callback: (data: string) => unknown) => { this.terminalInput = callback; return () => { this.terminalInput = undefined; }; },
        notify: (message: string) => this.warnings.push(message),
        setStatus: (key: string, text: string | undefined) => {
          this.statusChanges.push({ key, text });
          if (text === undefined) this.statuses.delete(key); else this.statuses.set(key, text);
        },
      },
    };
    // In-process public capability fixture: unrelated host APIs are intentionally absent.
    this.context = context as unknown as ExtensionContext;
    const api = { sendMessage: (message: NativeMessage, options: { deliverAs?: string; triggerTurn?: boolean }) => {
      if (this.throwSend) throw new Error("host acceptance not known");
      this.sent.push({ message, options }); this.idle = false;
    } };
    // Only the public sendMessage capability is exercised by this in-process host.
    const extensionAPI = api as unknown as ExtensionAPI;
    this.adapter = new MailboxAdapter(extensionAPI, async (operation, data) => {
      this.operations.push(operation);
      if (operation === "register") await this.beforeRegister?.(data);
      const raw = operation === "register" ? await this.fixture(operation, data) : await this.cli(operation, data);
      const result = parseReply(operation, raw);
      if (operation === "register") await this.afterRegister?.(data);
      if ("claim_id" in result && result.claim_id !== null && this.afterClaim) await this.afterClaim(data, result);
      return result;
    });
    fixtures.push(this);
  }
  eventContext(): ExtensionContext { return Object.create(this.context) as ExtensionContext; }
  async runNextTimeout() {
    const token = this.timeoutTokens.values().next().value;
    if (!token) throw new Error("no timeout scheduled");
    const callback = this.timers.get(token);
    this.timeoutTokens.delete(token); this.timers.delete(token);
    await callback?.();
  }
  private async execute(args: string[], data: Record<string, unknown>): Promise<unknown> {
    const child = Bun.spawn(["python3", ...args], { cwd: root, env: this.environment, stdin: "pipe", stdout: "pipe", stderr: "pipe" });
    child.stdin.write(JSON.stringify(data)); child.stdin.end();
    const text = await new Response(child.stdout).text();
    const error = await new Response(child.stderr).text();
    const exit = await child.exited;
    if (exit !== 0) throw new Error(text + error);
    const parsed: unknown = JSON.parse(text);
    return parsed;
  }
  fixture(operation: string, data: Record<string, unknown> = {}) { return this.execute(["-c", fixtureCode, this.directory, operation], data); }
  cli(operation: string, data: Record<string, unknown>) {
    return this.execute([path.join(root, "agent_skill_cli.py"), "ledger", operation === "release" ? "delivery" : "runtime", operation], data);
  }
  async setup(idle_minutes = 0) { await this.fixture("setup", { idle_minutes }); await this.adapter.start(this.context); }
  async send(kind = "fyi") { return this.fixture("send", { kind, body: "Real isolated body / " + kind }); }
  async view() { return ledgerView(await this.fixture("view")); }
  accept(message: NativeMessage) { this.branch.push({ ...message, type: "custom_message", id: "accepted-" + this.branch.length, parentId: null, timestamp: new Date().toISOString() }); }
  async close() { await this.adapter.shutdown(this.context); rmSync(this.directory, { recursive: true, force: true }); }
}

beforeEach(() => setSystemTime());
afterEach(async () => {
  for (const fixture of fixtures.splice(0)) await fixture.close();
  setSystemTime();
  for (const key of herdrEnvironmentKeys) {
    const value = previousHerdrEnvironment.get(key);
    if (value === undefined) delete process.env[key]; else process.env[key] = value;
  }
});

test("pending native publication waits without warning then clears status on registration", async () => {
  const f = new Fixture();
  let attempts = 0;
  f.beforeRegister = async () => {
    if (++attempts === 1) throw Object.assign(new Error("publication has not arrived"), { code: "native_session_pending" });
  };
  await f.setup();
  expect(f.statuses.get("ham-mailbox")).toBe("HAM: waiting for native session");
  expect(f.warnings).toEqual([]);
  await f.runNextTimeout();
  expect(attempts).toBe(2);
  expect(f.warnings).toEqual([]);
  expect(f.statuses.has("ham-mailbox")).toBe(false);
  expect(f.statusChanges.at(-1)).toEqual({ key: "ham-mailbox", text: undefined });
  expect(f.operations).toContain("inspect");
});

test("transient registration failure clears unavailable status after success", async () => {
  const f = new Fixture();
  let attempts = 0;
  f.beforeRegister = async () => { if (++attempts === 1) throw new Error("temporary transport failure"); };
  await f.setup();
  expect(f.statuses.get("ham-mailbox")).toBe("HAM: receiver unavailable");
  expect(f.warnings).toHaveLength(1);
  await f.runNextTimeout();
  expect(f.statuses.has("ham-mailbox")).toBe(false);
  expect(f.operations).toContain("inspect");
});

test("persistent native publication pending exhausts bounded retry and ends unavailable", async () => {
  const f = new Fixture();
  let attempts = 0;
  f.beforeRegister = async () => {
    attempts++;
    throw Object.assign(new Error("publication has not arrived"), { code: "native_session_pending" });
  };
  await f.setup();
  for (let retry = 1; retry < 10; retry++) {
    expect(f.statuses.get("ham-mailbox")).toBe("HAM: waiting for native session");
    expect(f.warnings).toEqual([]);
    await f.runNextTimeout();
  }
  expect(attempts).toBe(10);
  expect(f.statuses.get("ham-mailbox")).toBe("HAM: receiver unavailable");
  expect(f.warnings).toHaveLength(1);
  expect(f.timeoutTokens.size).toBe(0);
});

test("old epoch registration success cannot clear newer session error", async () => {
  const f = new Fixture();
  await f.fixture("setup");
  const oldSession = f.session;
  let entered!: () => void, unblock!: () => void;
  const waiting = new Promise<void>(resolve => { entered = resolve; });
  const blocked = new Promise<void>(resolve => { unblock = resolve; });
  f.beforeRegister = async data => {
    if (data.session_ref !== oldSession) throw new Error("new session registration unavailable");
  };
  f.afterRegister = async data => {
    if (data.session_ref === oldSession) { entered(); await blocked; }
  };
  const oldStart = f.adapter.start(f.eventContext());
  await waiting;
  f.session = path.join(f.directory, "new-session.jsonl");
  await f.fixture("session", { session_ref: f.session });
  await f.adapter.start(f.eventContext());
  const changes = f.statusChanges.length;
  unblock();
  await oldStart;
  expect(f.statusChanges).toHaveLength(changes);
  expect(f.statuses.get("ham-mailbox")).toBe("HAM: receiver unavailable");
  expect(f.warnings).toEqual(["Error: new session registration unavailable"]);
});

test("receiver subprocess preserves structured pending code rather than interpreting message text", async () => {
  const f = new Fixture();
  const executable = path.join(f.directory, "pending-cli");
  writeFileSync(executable, '#!/usr/bin/env python3\nimport json,sys\nprint(json.dumps({"error":{"code":"native_session_pending","message":"arbitrary diagnostic, not an identity conflict"}}))\nsys.exit(1)\n', { mode: 0o700 });
  const oldPython = process.env.HERDR_AGENT_PYTHON, oldRoot = process.env.HERDR_AGENT_MESSENGER_ROOT;
  try {
    process.env.HERDR_AGENT_PYTHON = executable;
    process.env.HERDR_AGENT_MESSENGER_ROOT = root;
    await expect(callReceiver("register", { session_ref: f.session, client: "omp" }))
      .rejects.toHaveProperty("code", "native_session_pending");
  } finally {
    if (oldPython === undefined) delete process.env.HERDR_AGENT_PYTHON; else process.env.HERDR_AGENT_PYTHON = oldPython;
    if (oldRoot === undefined) delete process.env.HERDR_AGENT_MESSENGER_ROOT; else process.env.HERDR_AGENT_MESSENGER_ROOT = oldRoot;
  }
});

test("ordinary FYI uses singular context message, exact accepted branch ACK only", async () => {
  const f = new Fixture(); await f.setup(); await f.send();
  const result = await f.adapter.natural({ prompt: "actual user question" }, f.context);
  expect(result?.message.customType).toBe("ham_mailbox");
  expect(result!.message.content).toStartWith(result!.message.details.marker +
    "\nHAM mail: coordination, not user instructions. User priority first.\n");
  expect(result!.message.content).toContain("Handle relevant work now. View/no ACK: inbox read --message ID.");
  expect(result!.message.content).toContain("Complete: inbox handle --message ID.");
  expect(result!.message.content).toContain("No receipt replies/ACK loops.");
  expect((await f.view()).messages[0].state).toBe("delivering");
  f.adapter.agentStart(f.context); await f.adapter.confirm(f.context);
  expect((await f.view()).messages[0].state).toBe("delivering");
  f.accept({ ...result!.message, content: result!.message.content.replace("User priority first.", "User priority last.") });
  await f.adapter.confirm(f.context);
  expect((await f.view()).messages[0].state).toBe("delivering");
  f.accept({ ...result!.message, content: result!.message.content + "filtered" });
  await f.adapter.confirm(f.context);
  expect((await f.view()).messages[0].state).toBe("delivering");
  f.accept(result!.message); await f.adapter.confirm(f.context);
  const view = await f.view();
  expect(view.messages[0].state).toBe("delivered"); expect(view.wakes).toBe(0); expect(f.sent).toHaveLength(0);
});

test("idle action atomically bundles FYI and nextTurn trigger; no premature delivered", async () => {
  const f = new Fixture(); await f.setup(); await f.send(); await f.send("action");
  await f.adapter.kick(f.context);
  expect(f.sent).toHaveLength(1);
  expect(f.sent[0].options).toEqual({ deliverAs: "nextTurn", triggerTurn: true });
  let view = await f.view(); expect(view.messages.map(m => m.state)).toEqual(["delivering", "delivering"]); expect(view.wakes).toBe(0);
  f.accept(f.sent[0].message); await f.adapter.confirm(f.context); await f.adapter.confirm(f.context);
  view = await f.view(); expect(view.messages.map(m => m.state)).toEqual(["delivered", "delivered"]); expect(view.wakes).toBe(1);
});

test("FYI alone cannot create a turn; task agents never register or consume", async () => {
  const f = new Fixture(); await f.setup(); await f.send(); await f.adapter.kick(f.context);
  expect(f.sent).toHaveLength(0); expect((await f.view()).messages[0].state).toBe("queued");
  f.kind = "task";
  expect(await f.adapter.natural({ prompt: "nested" }, f.context)).toBeUndefined();
  await f.adapter.kick(f.context); expect((await f.view()).messages[0].state).toBe("queued");
  f.kind = "main";
});

test("default TUI editor wakes through supportsEditor with no custom component", async () => {
  const f = new Fixture(); await f.setup(); await f.send("action");
  expect(f.context.mode).toBe("tui");
  expect(f.context.ui.supportsEditor).toBe(true);
  expect(f.context.ui.getEditorText()).toBe("");
  expect(f.context.ui.getEditorComponent()).toBeUndefined();
  await f.adapter.kick(f.eventContext());
  expect(f.sent).toHaveLength(1);
  expect(f.sent[0].options).toEqual({ deliverAs: "nextTurn", triggerTurn: true });
});

test("unmarked or nested OMP processes have no receiver side effects", async () => {
  const f = new Fixture();
  delete process.env.HERDR_ENV; delete process.env.HERDR_SOCKET_PATH;
  delete process.env.HERDR_PANE_ID; delete process.env.HERDR_TERMINAL_ID;
  await f.adapter.start(f.eventContext());
  expect(await f.adapter.natural({ prompt: "outside Herdr" }, f.eventContext())).toBeUndefined();
  await f.adapter.kick(f.eventContext()); f.adapter.input(f.eventContext());
  expect(f.operations).toEqual([]);
  expect(f.timers.size).toBe(0);
  expect(f.warnings).toEqual([]);
  expect(f.sent).toHaveLength(0);

  process.env.HERDR_ENV = "1"; process.env.HERDR_SOCKET_PATH = path.join(f.directory, "unavailable.sock");
  process.env.HERDR_PANE_ID = "stale-or-current-pane"; process.env.OMPCODE = "1";
  await f.adapter.start(f.eventContext());
  await f.adapter.kick(f.eventContext());
  expect(f.operations).toEqual([]);
  expect(f.timers.size).toBe(0);
});

test("fresh event contexts preserve session identity across register, natural, schedule, and kick", async () => {
  const f = new Fixture(); await f.setup(); await f.send();
  f.operations.length = 0;
  const result = await f.adapter.natural({ prompt: "ordinary user turn" }, f.eventContext());
  expect(result?.message.customType).toBe("ham_mailbox");
  expect(f.operations).toContain("register");
  expect(f.operations).toContain("claim");
  f.accept(result!.message);
  await f.adapter.kick(f.eventContext());
  expect(f.operations).toContain("applied");
  expect(f.operations).toContain("inspect");
  expect((await f.view()).messages[0].state).toBe("delivered");
});

test("stale session work cannot block a new wake and schedules a current-session trailing check", async () => {
  const f = new Fixture(); await f.setup(); await f.send("action");
  let reachedClaim!: () => void, releaseClaim!: () => void;
  const claimed = new Promise<void>(resolve => { reachedClaim = resolve; });
  const blocked = new Promise<void>(resolve => { releaseClaim = resolve; });
  f.afterClaim = async data => { if (data.wake) { reachedClaim(); await blocked; } };
  const oldKick = f.adapter.kick(f.eventContext());
  await claimed;
  const oldNatural = f.adapter.natural({ prompt: "old session turn" }, f.eventContext());

  const nextSession = path.join(f.directory, "next-session.jsonl");
  f.session = nextSession;
  await f.fixture("session", { session_ref: nextSession });
  await f.adapter.start(f.eventContext());
  await f.adapter.kick(f.eventContext());
  expect(f.sent).toHaveLength(0);

  f.afterClaim = undefined;
  releaseClaim();
  await oldKick;
  expect(await oldNatural).toBeUndefined();
  await f.runNextTimeout();
  expect(f.sent).toHaveLength(1);
  expect((await f.view()).messages[0]).toMatchObject({ state: "delivering", claimed_generation: 2 });
});

test("nonempty/unsupported editor and RPC's empty-string getter never authorize wake", async () => {
  const f = new Fixture(); await f.setup(); await f.send("action");
  f.editor = " "; await f.adapter.kick(f.context);
  f.editor = ""; f.editorSupported = false; await f.adapter.kick(f.context);
  f.editorSupported = true; f.mode = "rpc"; await f.adapter.kick(f.context);
  expect(f.sent).toHaveLength(0); expect((await f.view()).messages[0].state).toBe("queued");
  f.mode = "tui"; await f.adapter.kick(f.context); expect(f.sent).toHaveLength(1);
});

test("last actual key restarts N-minute quiet interval; focus does not defer", async () => {
  const now = Date.now(); setSystemTime(now);
  const f = new Fixture(); await f.setup(1); await f.send("action");
  setSystemTime(now + 30000); f.terminalInput!("x");
  setSystemTime(now + 61000); await f.adapter.kick(f.context); expect(f.sent).toHaveLength(0);
  setSystemTime(now + 85000); f.terminalInput!("\x1b[I");
  setSystemTime(now + 91000); await f.adapter.kick(f.context); expect(f.sent).toHaveLength(1);
});

test("public retry lifecycle and duplicate end cannot accidentally release retry hold", async () => {
  const f = new Fixture(); await f.setup(); await f.send("action");
  f.adapter.agentStart(f.context);
  f.adapter.agentEnd({ messages: [{ role: "assistant", stopReason: "error" }] }, f.context);
  f.adapter.retry(true, { delayMs: 5000 }, f.context);
  f.adapter.agentEnd({ messages: [] }, f.context);
  await f.adapter.kick(f.context); expect(f.sent).toHaveLength(0);
  f.adapter.retry(false, { success: false }, f.context); await f.adapter.kick(f.context); expect(f.sent).toHaveLength(0);
  f.adapter.retry(false, { success: true }, f.context); await f.adapter.kick(f.context); expect(f.sent).toHaveLength(1);
});

test("willContinue, ask and pending native messages hold wake until genuine idle", async () => {
  const f = new Fixture(); await f.setup(); await f.send("action");
  f.adapter.agentStart(f.context); f.adapter.agentEnd({ willContinue: true }, f.context);
  await f.adapter.kick(f.context); expect(f.sent).toHaveLength(0);
  f.adapter.agentStart(f.context); f.adapter.agentEnd({ willContinue: false }, f.context);
  f.adapter.tool(true, { toolName: "ask" }, f.context); await f.adapter.kick(f.context); expect(f.sent).toHaveLength(0);
  f.adapter.tool(false, { toolName: "ask" }, f.context); f.queued = true;
  await f.adapter.kick(f.context); expect(f.sent).toHaveLength(0);
  f.queued = false; await f.adapter.kick(f.context); expect(f.sent).toHaveLength(1);
});

test("user submission wins a deterministic in-flight wake claim race", async () => {
  const f = new Fixture(); await f.setup(); await f.send(); await f.send("action");
  const claimed = Promise.withResolvers<void>(), resume = Promise.withResolvers<void>();
  f.afterClaim = async data => { if (data.wake) { claimed.resolve(); await resume.promise; } };
  const wake = f.adapter.kick(f.context); await claimed.promise;
  f.adapter.input(f.context);
  const natural = f.adapter.natural({ prompt: "user priority" }, f.context);
  resume.resolve(); await wake;
  const result = await natural;
  expect(f.sent).toHaveLength(0); expect(result!.message.content).toContain("Real isolated body / action");
  const view = await f.view(); expect(view.messages.map(m => m.state)).toEqual(["delivering", "delivering"]);
  expect(new Set(view.messages.map(m => m.claim_id)).size).toBe(1);
});

test("preparation reentry reuses claim; settled cancellation remains unknown, not replayed", async () => {
  const f = new Fixture(); await f.setup(); await f.send();
  const first = await f.adapter.natural({ prompt: "same preparation" }, f.context);
  const second = await f.adapter.natural({ prompt: "same preparation" }, f.context);
  expect(second!.message.details.claim_id).toBe(first!.message.details.claim_id);
  await f.adapter.kick(f.context);
  expect((await f.view()).messages[0].hold_reason).toBe("acceptance_unknown");
  f.adapter.input(f.context);
  expect(await f.adapter.natural({ prompt: "different user submission" }, f.context)).toBeUndefined();
  expect((await f.view()).messages[0].state).toBe("delivering");
  f.accept(first!.message); await f.adapter.confirm(f.context);
  expect((await f.view()).messages[0].state).toBe("delivered");
});

test("session replacement during unreturned preparation releases only proven unapplied claim", async () => {
  const f = new Fixture(); await f.setup(); await f.send("action");
  const claimed = Promise.withResolvers<void>(), resume = Promise.withResolvers<void>();
  f.afterClaim = async data => { if (!data.wake) { claimed.resolve(); await resume.promise; } };
  const natural = f.adapter.natural({ prompt: "old session" }, f.context); await claimed.promise;
  f.session = path.join(f.directory, "new-session.jsonl"); await f.fixture("session", { session_ref: f.session });
  await f.adapter.start(f.context); resume.resolve(); expect(await natural).toBeUndefined();
  expect((await f.view()).messages[0].state).toBe("queued");
  f.afterClaim = undefined; await f.adapter.kick(f.context);
  expect(f.sent).toHaveLength(1); expect((await f.view()).messages[0].claimed_generation).toBe(2);
});

test("already returned old-session claim is never guessed delivered in replacement", async () => {
  const f = new Fixture(); await f.setup(); await f.send();
  const result = await f.adapter.natural({ prompt: "old context" }, f.context);
  f.accept(result!.message);
  f.session = path.join(f.directory, "replacement.jsonl"); await f.fixture("session", { session_ref: f.session });
  await f.adapter.start(f.context); await f.adapter.confirm(f.context);
  expect((await f.view()).messages[0].state).toBe("delivering");
});

test("sendMessage exception preserves uncertainty, no repeated wake or delivered receipt", async () => {
  const f = new Fixture(); await f.setup(); await f.send("action"); f.throwSend = true;
  await f.adapter.kick(f.context); await f.adapter.kick(f.context);
  const view = await f.view(); expect(view.messages[0].state).toBe("delivering"); expect(view.wakes).toBe(0);
  expect(f.sent).toHaveLength(0);
  await f.adapter.shutdown(f.context); expect(f.timers.size).toBe(0); expect(f.terminalInput).toBeUndefined();
});
