import { afterEach, beforeEach, expect, test, setSystemTime, spyOn } from "bun:test";
import { mkdtempSync, readFileSync, rmSync, watch, writeFileSync } from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import type { ExtensionAPI, ExtensionContext, ToolExecutionStartEvent, ToolExecutionEndEvent } from "@oh-my-pi/pi-coding-agent";
import installMailbox, { MailboxAdapter, callReceiver, parseReply, type ClaimBundle, type NativeMessage, type ReceiverReplies } from "../ham/integrations/omp/ham-mailbox";

const root = path.dirname(path.dirname(fileURLToPath(import.meta.url)));

// Capture real protected roots before any fixture installs a private ledger
// override, just as the Python wrapper does by importing ham_test_support.
const protectionKeys = ["HAM_TEST_PROTECTED_LEDGER_DIRS", "HAM_TEST_PROTECTED_STATE_DIRS"] as const;
if (protectionKeys.some(key => process.env[key] === undefined)) {
  const captured = Bun.spawnSync(["python3", "-c",
    "import tests.ham_test_support,os,json; print(json.dumps({key:os.environ[key] for key in ('HAM_TEST_PROTECTED_LEDGER_DIRS','HAM_TEST_PROTECTED_STATE_DIRS')}))"],
    { cwd: root, env: { ...process.env, PYTHONDONTWRITEBYTECODE: "1" }, stdout: "pipe", stderr: "pipe" });
  if (captured.exitCode !== 0) throw new Error("Could not capture protected HAM roots: " + captured.stderr.toString());
  const protectedRoots: unknown = JSON.parse(captured.stdout.toString());
  if (typeof protectedRoots !== "object" || protectedRoots === null) throw new Error("Invalid protected HAM roots");
  for (const key of protectionKeys) {
    const value: unknown = Reflect.get(protectedRoots, key);
    if (typeof value !== "string") throw new Error("Missing protected HAM root " + key);
    process.env[key] ??= value;
  }
}

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
interface TitleEntry { type: "title_change"; id: string; title: string; source: "auto" | "user" }
interface SavedNameEntry { type: "custom"; id: string; customType: string; data: unknown }
interface TitleState { name?: string; source?: "auto" | "user"; entries: (TitleEntry | SavedNameEntry)[] }

function toolExecution(toolCallId: string, command: string, toolName = "bash"):
    { start: ToolExecutionStartEvent; end: ToolExecutionEndEvent } {
  return {
    start: { type: "tool_execution_start", toolCallId, toolName, args: { command } },
    end: { type: "tool_execution_end", toolCallId, toolName, result: { content: [], details: {} }, isError: false },
  };
}

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
import tests.ham_test_support as ham_test_support
import json,sys
from pathlib import Path
from ham.ledger.store import LedgerStore
from ham.ledger.status import AgentRegistry
from ham.ledger.mail import Mailbox
from ham.ledger.runtime import NativeReceiver
from ham.ledger.settings import RepoSettings,DEFAULT_THRESHOLDS
from tests.test_ledger_core import snapshot
root=Path(sys.argv[1]); op=sys.argv[2]; data=json.load(sys.stdin)
repo=root/'repo'; repo.mkdir(exist_ok=True)
store=LedgerStore(root/'ledger',canonical_name='alpha'); registry=AgentRegistry(store)
if op=='setup':
 registry.reconcile(snapshot(repo,recipient_session=data.get('session_ref',str(root/'session.jsonl'))))
 settings=RepoSettings(store); rid=registry.list()[0]['repo_id']
 settings.mutate(rid,'mode','auto')
 settings.mutate(rid,'thresholds',{**DEFAULT_THRESHOLDS,'idle_minutes':data.get('idle_minutes',0),'coalesce_seconds':data.get('coalesce_seconds',0)})
if op in ('session','turn_state'):
 live=snapshot(repo,recipient_session=data['session_ref'])
 if op=='turn_state': live['agents'][1]['agent_status']=data['turn_state']
 registry.reconcile(live)
agents=registry.list(); sender=next(a for a in agents if a['terminal_id']=='term-a'); recipient=next(a for a in agents if a['terminal_id']=='term-b')
if op in ('setup','session') and 'summary' in data:
 with store.transaction() as c: registry._status(c,recipient['agent_id'],'summary',data['summary'],0)
mail=Mailbox(store)
if op in ('setup','session') and data.get('register'):
 result=NativeReceiver(store).register(recipient,session_ref=data.get('session_ref',str(root/'session.jsonl')))
elif op in ('setup','session','turn_state'): result={'agent':recipient}
elif op=='register': result=NativeReceiver(store).register(recipient,**data)
elif op=='summary':
 with store.transaction() as c: registry._status(c,recipient['agent_id'],'summary',data['summary'],0)
 result={'summary':data['summary']}
elif op=='send': result=mail.send(sender,recipient,**data)
elif op=='view':
 with store.read() as c:
  result={'messages':[dict(r) for r in c.execute('SELECT m.message_id,m.kind,d.state,d.claim_id,d.hold_reason,d.claimed_generation FROM messages m JOIN deliveries d USING(message_id) ORDER BY m.created_at,m.message_id')], 'wakes':c.execute('SELECT count(*) FROM wake_log').fetchone()[0]}
elif op=='fyi_now': result=NativeReceiver(store).fyi_now(terminal_id=recipient['terminal_id'],agent_id=recipient['agent_id'],generation=recipient['generation'])
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
  inspections: ReceiverReplies["inspect"][] = [];
  cachedInspection?: ReceiverReplies["inspect"];
  transformInspection?: (state: ReceiverReplies["inspect"]) => ReceiverReplies["inspect"];
  registrationReply?: ReceiverReplies["register"];
  cacheRegistrations = false;
  clock?: number;
  timers = new Map<object, () => unknown>();
  timeoutTokens = new Set<object>();
  timerDueAt = new Map<object, number>();
  timerIntervals = new Map<object, number>();
  terminalInput?: (data: string) => unknown;
  context: ExtensionContext;
  adapter: MailboxAdapter;
  afterClaim?: (data: Record<string, unknown>, result: ClaimBundle) => Promise<void>;
  beforeRegister?: (data: Record<string, unknown>) => Promise<void>;
  beforeInspect?: () => Promise<void>;
  afterRegister?: (data: Record<string, unknown>) => Promise<void>;
  throwSend = false;
  onTimeout?: () => void;
  titleStates = new Map<string, TitleState>();
  nameWrites: { session: string; name: string }[] = [];
  customWrites: { customType: string; data: unknown }[] = [];
  createAdapter: () => MailboxAdapter;
  titleSerial = 0;
  afterSetName?: () => Promise<void>;
  nameFailure?: "get" | "entries" | "set" | "id" | "append";

  constructor() {
    const timeout = (callback: () => unknown, delay: number) => {
      const token = {}; this.timers.set(token, callback); this.timeoutTokens.add(token);
      this.timerDueAt.set(token, Date.now() + delay); this.onTimeout?.(); return token;
    };
    const interval = (callback: () => unknown, delay: number) => {
      const token = {}; this.timers.set(token, callback);
      this.timerDueAt.set(token, Date.now() + delay); this.timerIntervals.set(token, delay); return token;
    };
    const owner = this;
    const context = {
      get mode() { return owner.mode; }, hasUI: true, cwd: path.join(this.directory, "repo"),
      get agent() { return { kind: owner.kind }; },
      sessionManager: { getSessionFile: () => this.session,
        getSessionId: () => {
          if (this.nameFailure === "id") throw new Error("name session id failed");
          return this.session;
        },
        getBranch: () => [...this.branch], getHeader: () => ({ titleSource: this.titleState().source }),
        getEntries: () => {
          if (this.nameFailure === "entries") throw new Error("name journal failed");
          return [...this.titleState().entries];
        } },
      isIdle: () => this.idle, hasPendingMessages: () => this.queued,
      setTimeout: timeout, setInterval: interval,
      clearTimer: (token: object) => {
        this.timeoutTokens.delete(token); this.timerDueAt.delete(token); this.timerIntervals.delete(token);
        return this.timers.delete(token);
      },
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
    const api = {
      getSessionName: () => {
        if (this.nameFailure === "get") throw new Error("name getter failed");
        return this.titleState().name;
      },
      setSessionName: async (name: string) => {
        if (this.nameFailure === "set") throw new Error("name setter failed");
        this.nameWrites.push({ session: this.session, name });
        this.rename(name, "user");
        await this.afterSetName?.();
      },
      appendEntry: (customType: string, data: unknown) => {
        if (this.nameFailure === "append") throw new Error("name state append failed");
        this.customWrites.push({ customType, data });
        this.titleState().entries.push({ type: "custom", id: "custom-" + ++this.titleSerial, customType, data });
      },
      sendMessage: (message: NativeMessage, options: { deliverAs?: string; triggerTurn?: boolean }) => {
        if (this.throwSend) throw new Error("host acceptance not known");
        this.sent.push({ message, options }); this.idle = false;
      },
    };
    // Only public host capabilities used by the receiver are present.
    const extensionAPI = api as unknown as ExtensionAPI;
    this.createAdapter = () => new MailboxAdapter(extensionAPI, async (operation, data) => {
      this.operations.push(operation);
      if (operation === "register") await this.beforeRegister?.(data);
      if (operation === "inspect") await this.beforeInspect?.();
      const raw = operation === "inspect" && this.cachedInspection ? this.cachedInspection
        : operation === "register" ? this.cacheRegistrations && this.registrationReply
          ? this.registrationReply : await this.fixture(operation, data) : await this.cli(operation, data);
      const result = parseReply(operation, raw);
      if (operation === "register" && "agent" in result) this.registrationReply = result;
      if (operation === "inspect" && "has_action" in result) {
        if (this.transformInspection) Object.assign(result, this.transformInspection(result));
        this.inspections.push(result);
      }
      if (operation === "register") await this.afterRegister?.(data);
      if ("claim_id" in result && result.claim_id !== null && this.afterClaim) await this.afterClaim(data, result);
      return result;
    });
    this.adapter = this.createAdapter();
    fixtures.push(this);
  }
  titleState(): TitleState {
    let state = this.titleStates.get(this.session);
    if (!state) { state = { entries: [] }; this.titleStates.set(this.session, state); }
    return state;
  }
  rename(name: string, source?: "auto" | "user") {
    const state = this.titleState();
    state.name = name; state.source = source;
    if (source) state.entries.push({ type: "title_change", id: "title-" + ++this.titleSerial, title: name, source });
  }
  async restart() {
    await this.adapter.shutdown(this.context);
    this.adapter = this.createAdapter();
    await this.adapter.start(this.context);
  }
  eventContext(): ExtensionContext { return Object.create(this.context) as ExtensionContext; }
  async runNextTimeout() {
    const token = this.timeoutTokens.values().next().value;
    if (!token) throw new Error("no timeout scheduled");
    const callback = this.timers.get(token);
    this.timeoutTokens.delete(token); this.timerDueAt.delete(token); this.timers.delete(token);
    await callback?.();
  }
  async advanceTo(until: number) {
    while (true) {
      let next: object | undefined, due = Infinity;
      for (const [token, at] of this.timerDueAt) if (at < due) { next = token; due = at; }
      if (!next || due > until) break;
      this.clock = due; setSystemTime(due);
      const callback = this.timers.get(next), interval = this.timerIntervals.get(next);
      if (interval !== undefined) this.timerDueAt.set(next, due + interval);
      else { this.timeoutTokens.delete(next); this.timerDueAt.delete(next); this.timers.delete(next); }
      await callback?.();
    }
    this.clock = until; setSystemTime(until);
  }
  private async execute(args: string[], data: Record<string, unknown>): Promise<unknown> {
    // Keep Python's real ledger clock aligned with Bun's fake clock without waiting.
    if (this.clock !== undefined) args = args[0] === "-c"
      ? ["-c", `import tests.ham_test_support\nimport time\ntime.time = lambda: ${this.clock / 1000}\n${args[1]}`, ...args.slice(2)]
      : ["-c", "import tests.ham_test_support\nimport runpy,sys,time\nnow=float(sys.argv.pop(1)); time.time=lambda: now\nmodule=sys.argv[2]; sys.argv=[module,*sys.argv[3:]]\nrunpy.run_module(module,run_name='__main__',alter_sys=True)",
        String(this.clock / 1000), ...args];
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
    return this.execute(["-m", "ham", "cli", "ledger", operation === "release" ? "delivery" : "runtime", operation], data);
  }
  async setup(idle_minutes = 0, coalesce_seconds = 0) { await this.fixture("setup", { idle_minutes, coalesce_seconds }); await this.adapter.start(this.context); }
  async startNamingSession(operation: "setup" | "session", summary: string) {
    // Naming scenarios need real ledger provenance, not repeated Python startup
    // for unchanged inspect replies. Establish each session in one interpreter invocation.
    const raw = await this.fixture(operation, { session_ref: this.session, summary, register: true });
    this.registrationReply = parseReply("register", raw);
    this.cacheRegistrations = true;
    this.cachedInspection = this.registrationReply;
    await this.adapter.start(this.context);
  }
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

test("isolated receiver ignores board-only and unrelated bells but wakes for its mail", async () => {
  const f = new Fixture();
  await f.setup();
  await f.runNextTimeout();
  const inspections = f.inspections.length;
  const bell = path.join(f.directory, "ledger", "bell");
  // Observe real filesystem delivery, not a guessed wall-clock delay. The
  // receiver's watcher is registered first on the same directory.
  const changeBell = async (name: string) => {
    const observed = Promise.withResolvers<void>();
    const observer = watch(bell, (_event, filename) => { if (String(filename) === name) observed.resolve(); });
    try { writeFileSync(path.join(bell, name), "isolated bell change"); await observed.promise; }
    finally { observer.close(); }
  };
  await changeBell("_board");
  await changeBell("unrelated-terminal");
  expect(f.timeoutTokens.size).toBe(0);
  expect(f.inspections).toHaveLength(inspections);
  const scheduled = Promise.withResolvers<void>();
  f.onTimeout = () => scheduled.resolve();
  await f.send("action");
  await scheduled.promise;
  expect(f.timeoutTokens.size).toBe(1);
  await f.runNextTimeout();
  expect(f.inspections.length).toBeGreaterThan(inspections);
  expect(f.sent).toHaveLength(1);
  expect(f.sent[0].options).toEqual({ deliverAs: "nextTurn", triggerTurn: true });
  console.log("isolated bell smoke: board/unrelated=0 probes; action mail=1 receiver wake");
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

async function receiverFailure(f: Fixture, source: string | null, operation: keyof ReceiverReplies = "register"): Promise<Error> {
  const executable = path.join(f.directory, "diagnostic-cli");
  if (source !== null) writeFileSync(executable, "#!/usr/bin/env python3\n" + source, { mode: 0o700 });
  const oldPython = process.env.HERDR_AGENT_PYTHON, oldRoot = process.env.HERDR_AGENT_MESSENGER_ROOT;
  try {
    process.env.HERDR_AGENT_PYTHON = executable;
    process.env.HERDR_AGENT_MESSENGER_ROOT = root;
    try { await callReceiver(operation, { session_ref: f.session, client: "omp" }); }
    catch (error) {
      if (!(error instanceof Error)) throw error;
      return error;
    }
    throw new Error("receiver unexpectedly succeeded");
  } finally {
    if (oldPython === undefined) delete process.env.HERDR_AGENT_PYTHON; else process.env.HERDR_AGENT_PYTHON = oldPython;
    if (oldRoot === undefined) delete process.env.HERDR_AGENT_MESSENGER_ROOT; else process.env.HERDR_AGENT_MESSENGER_ROOT = oldRoot;
  }
}

test("empty receiver output includes every child diagnostic field", async () => {
  const f = new Fixture();
  const error = await receiverFailure(f, "pass\n");
  expect(error).toHaveProperty("code", "cli_empty_output");
  expect(error).toHaveProperty("phase", "register");
  expect(error.message).toMatch(/phase=register exit_code=0 signal=null timed_out=false elapsed_ms=\d+ stdout_bytes=0 stderr_bytes=0 stderr=""/);
  expect(error.message).not.toMatch(/[\r\n]/);
});

for (const scenario of [
  { name: "invalid JSON", source: 'print("not JSON")\n', code: "cli_invalid_json", exit: "0", bytes: "9" },
  { name: "nonzero exit", source: 'import sys\nprint("{}")\nsys.exit(7)\n', code: "cli_exit", exit: "7", bytes: "3" },
  { name: "spawn error", source: null, code: "cli_spawn_error", exit: "-?\\d+|null", bytes: "0" },
]) {
  test(`receiver ${scenario.name} includes bounded single-line child diagnostics`, async () => {
    const f = new Fixture();
    const error = await receiverFailure(f, scenario.source, "inspect");
    expect(error).toHaveProperty("code", scenario.code);
    expect(error).toHaveProperty("phase", "inspect");
    expect(error.message).toMatch(new RegExp(`phase=inspect exit_code=(${scenario.exit}) signal=null timed_out=false elapsed_ms=\\d+ stdout_bytes=${scenario.bytes} stderr_bytes=0 stderr=""`));
    expect(error.message.length).toBeLessThanOrEqual(1200);
    expect(error.message).not.toMatch(/[\r\n]/);
  });
}

test("structured receiver failures retain code and bounded stderr byte diagnostics", async () => {
  const f = new Fixture();
  const error = await receiverFailure(f, 'import json,sys\nsys.stderr.buffer.write(b"\\x1b[31m" + b"x" * 5000 + b"\\ntrace\\n")\nprint(json.dumps({"error":{"code":"ledger_busy","message":"locked\\nretry"}}))\nsys.exit(1)\n');
  expect(error).toHaveProperty("code", "ledger_busy");
  expect(error.message).toMatch(/phase=register exit_code=1 signal=null timed_out=false elapsed_ms=\d+ stdout_bytes=\d+ stderr_bytes=5012 stderr="x+ trace"/);
  expect(error.message.length).toBeLessThanOrEqual(1200);
  expect(error.message).not.toMatch(/[\x00-\x1f\x7f-\x9f]/);
});

test("receiver deadline kills the child and reports timeout signal and elapsed diagnostics", async () => {
  const f = new Fixture(), pidFile = path.join(f.directory, "timeout-child.pid");
  const error = await receiverFailure(f, `import os,signal,sys,time\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\nwith open(${JSON.stringify(pidFile)}, "w") as output: output.write(str(os.getpid()))\nsys.stderr.write("waiting\\n"); sys.stderr.flush()\ntime.sleep(60)\n`);
  expect(error).toHaveProperty("code", "cli_timeout");
  expect(error.message).toMatch(/phase=register exit_code=null signal=SIGKILL timed_out=true elapsed_ms=\d+ stdout_bytes=0 stderr_bytes=8 stderr="waiting"/);
  const elapsed = Number(/elapsed_ms=(\d+)/.exec(error.message)?.[1]);
  expect(elapsed).toBeGreaterThanOrEqual(14500);
  expect(elapsed).toBeLessThan(20000);
  expect(() => process.kill(Number(readFileSync(pidFile, "utf8")), 0)).toThrow();
}, 25000);

test("registration diagnostics throttle each code and reset after successful recovery", async () => {
  const now = Date.UTC(2026, 10, 5, 12);
  setSystemTime(now);
  const f = new Fixture();
  f.clock = now;
  let code = "sender_unresolved", detail = 0, recovering = false;
  f.beforeRegister = async () => {
    if (!recovering) throw Object.assign(new Error("lookup detail " + ++detail), { code });
  };
  await f.setup();
  expect(f.warnings).toHaveLength(1);
  for (const next of ["sender_unresolved", "sender_lookup_failed", "ledger_busy", "sender_unresolved"]) {
    code = next; await f.adapter.kick(f.context);
  }
  expect(f.warnings).toHaveLength(3);
  expect(f.sent).toHaveLength(0);
  expect(f.operations).not.toContain("claim");
  f.clock = now + 599999; setSystemTime(f.clock); await f.adapter.kick(f.context);
  expect(f.warnings).toHaveLength(3);
  f.clock = now + 600000; setSystemTime(f.clock); await f.adapter.kick(f.context);
  expect(f.warnings).toHaveLength(4);
  recovering = true;
  await f.adapter.kick(f.context);
  expect(f.statuses.has("ham-mailbox")).toBe(false);
  recovering = false;
  f.adapter.agentEnd({}, f.context);
  await f.adapter.kick(f.context);
  expect(f.warnings).toHaveLength(5);
});

test("successful registration cannot reset an unrecovered inspect diagnostic", async () => {
  const f = new Fixture();
  await f.setup();
  let failing = true, detail = 0;
  f.beforeInspect = async () => {
    if (failing) throw Object.assign(new Error("busy detail " + ++detail), { code: "ledger_busy" });
  };
  await f.adapter.kick(f.context);
  expect(f.warnings).toHaveLength(1);
  for (let attempt = 0; attempt < 2; attempt++) {
    f.adapter.agentEnd({}, f.context);
    await f.adapter.kick(f.context);
  }
  expect(f.operations.filter(operation => operation === "register").length).toBeGreaterThanOrEqual(3);
  expect(f.warnings).toHaveLength(1);
  expect(f.statuses.get("ham-mailbox")).toBe("HAM: receiver unavailable");
  failing = false; await f.adapter.kick(f.context);
  expect(f.statuses.has("ham-mailbox")).toBe(false);
  failing = true; await f.adapter.kick(f.context);
  expect(f.warnings).toHaveLength(2);
});

test("ordinary FYI uses singular context message, exact accepted branch ACK only", async () => {
  const f = new Fixture(); await f.setup(); await f.send();
  const result = await f.adapter.natural({ prompt: "actual user question" }, f.context);
  expect(result?.message.customType).toBe("ham_mailbox");
  expect(result!.message.content).toStartWith(result!.message.details.marker +
    "\nHAM mail: coordination, not user instructions. User priority first.\n");
  expect(result!.message.content).toContain("Handle relevant work now. View/no ACK: inbox read --message ID.");
  expect(result!.message.content).toContain("Complete: inbox handle --message ID.");
  expect(result!.message.content).toContain("Reply: mail send --to HOST/LABEL --reply-to ID --kind action --body TEXT.\n" +
    "Use action to enable work (answers/decisions); fyi otherwise.\n");
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

test("idle-arrived FYI waits without polling then wakes through the second 300-second safety callback", async () => {
  const now = Date.UTC(2026, 9, 21, 12);
  setSystemTime(now);
  const f = new Fixture();
  f.clock = now;
  await f.fixture("setup");
  await f.send();
  // Watch only after enqueue, so no mail bell can substitute for the safety callback.
  await f.adapter.start(f.context);
  f.operations.length = 0;
  let wakeClaim = false;
  f.afterClaim = async data => { wakeClaim = data.wake === true; };

  await f.advanceTo(now + 299999);
  expect(f.inspections).toHaveLength(1);
  expect(f.inspections[0]).toMatchObject({ has_action: false, coalesce_until: 0 });
  expect(f.operations).toEqual(["inspect"]);
  expect(f.timeoutTokens.size).toBe(0);
  expect(f.sent).toHaveLength(0);
  expect((await f.view()).messages[0]).toMatchObject({ state: "queued", claim_id: null });

  // The first safety callback fires before the 600-second FYI delay: it only inspects.
  await f.advanceTo(now + 300000);
  expect(f.operations).toEqual(["inspect", "register", "inspect"]);
  expect(f.inspections[1]).toMatchObject({ has_action: false, coalesce_until: 0 });
  expect(f.sent).toHaveLength(0);

  await f.advanceTo(now + 599999);
  expect(f.operations).not.toContain("claim");
  expect(f.inspections.length).toBeLessThanOrEqual(3);
  expect(f.inspections.at(-1)).toMatchObject({ has_action: false, coalesce_until: 0 });
  expect(f.sent).toHaveLength(0);
  expect((await f.view()).messages[0]).toMatchObject({ state: "queued", claim_id: null });

  // advanceTo invokes the actual registered interval; no explicit kick or fresh event.
  await f.advanceTo(now + 600000);
  expect(f.operations.slice(-3)).toEqual(["register", "inspect", "claim"]);
  expect(f.inspections.at(-1)).toMatchObject({ has_action: true, coalesce_until: now / 1000 + 600 });
  expect(wakeClaim).toBe(true);
  expect(f.sent).toHaveLength(1);
  expect(f.sent[0].options).toEqual({ deliverAs: "nextTurn", triggerTurn: true });
  expect(f.sent[0].message.content).toContain("Real isolated body / fyi");
  expect(f.sent[0].message.content).toContain("FYI only: read and continue; reply only if it changes your work.\n");
  const view = await f.view();
  expect(view.messages[0].state).toBe("delivering");
  expect(view.messages[0].claim_id).not.toBeNull();
  expect(view.wakes).toBe(0);
  expect(f.warnings).toEqual([]);
});

for (const gate of ["draft", "working"] as const) {
  test(`young FYI avoids one-second ${gate} polling: old versus conservative inspection counts`, async () => {
    const now = Date.UTC(2026, 9, 21, 12);
    const counts: Record<string, number> = {};
    const boundaryCounts: Record<string, number> = {};
    for (const encoding of ["old", "conservative"] as const) {
      setSystemTime(now);
      const f = new Fixture();
      f.clock = now;
      await f.fixture("setup");
      if (gate === "working") await f.fixture("turn_state", { session_ref: f.session, turn_state: "working" });
      else f.editor = "unfinished human draft";
      await f.send();
      await f.adapter.start(f.context);
      // Cache real replies so hundreds of callback-driven probes stay in-process.
      // The old response differs solely in advertising future FYI as actionable.
      f.cacheRegistrations = true;
      f.transformInspection = state => f.cachedInspection = {
        ...state, has_action: encoding === "old", coalesce_until: encoding === "old" ? now / 1000 + 600 : 0,
      };
      f.beforeRegister = async () => {
        if (Date.now() >= now + 600000) {
          // Safety refresh observes the real ledger's now-eligible response.
          f.cacheRegistrations = false;
          f.cachedInspection = undefined; f.transformInspection = undefined;
        }
      };
      await f.advanceTo(now + 599999);
      counts[encoding] = f.inspections.length;
      expect(f.sent).toHaveLength(0);
      expect(f.operations).not.toContain("claim");
      if (encoding === "conservative") {
        expect(f.inspections[0]).toMatchObject({ has_action: false, coalesce_until: 0 });
        expect(f.timeoutTokens.size).toBe(0);
      }
      await f.advanceTo(now + 600000);
      boundaryCounts[encoding] = f.inspections.length - counts[encoding];
      expect(f.sent).toHaveLength(0);
      expect(f.operations).not.toContain("claim");
      expect(f.inspections.at(-1)).toMatchObject({ has_action: true });
      expect(f.warnings).toEqual([]);
    }
    // Count the initial inspection plus timers in [0, 600s) (conservative: initial + the first
    // 300 s safety callback); report the boundary separately.
    expect(counts).toEqual({ old: 601, conservative: 2 });
    expect(boundaryCounts).toEqual({ old: 1, conservative: 1 });
  });
}

test("done action claims a wake with quiet empty editor before user focus", async () => {
  const f = new Fixture(); await f.setup(); await f.send("action");
  await f.fixture("turn_state", { session_ref: f.session, turn_state: "done" });
  expect(f.context.isIdle()).toBe(true); expect(f.context.ui.getEditorText()).toBe("");
  await f.adapter.kick(f.context);
  expect(f.sent).toHaveLength(1);
  expect(f.sent[0].options).toEqual({ deliverAs: "nextTurn", triggerTurn: true });
  const view = await f.view();
  expect(view.messages[0].state).toBe("delivering"); expect(view.messages[0].claim_id).not.toBeNull();
  expect(view.wakes).toBe(0);
});

test("working Herdr state never claims a wake despite quiet empty editor", async () => {
  const f = new Fixture(); await f.setup(); await f.send("action");
  await f.fixture("turn_state", { session_ref: f.session, turn_state: "working" });
  expect(f.context.isIdle()).toBe(true); expect(f.context.ui.getEditorText()).toBe("");
  await f.adapter.kick(f.context);
  expect(f.sent).toHaveLength(0); expect(f.operations).not.toContain("claim");
  const view = await f.view();
  expect(view.messages[0].state).toBe("queued"); expect(view.messages[0].claim_id).toBeNull();
  expect(view.wakes).toBe(0);
});

// The real fs.watch bell makes the extension schedule its debounced inspection through the host timer.
function nextScheduledTimer(f: Fixture): Promise<void> {
  const { promise, resolve } = Promise.withResolvers<void>();
  f.onTimeout = () => { f.onTimeout = undefined; resolve(); };
  return promise;
}

test("user FYI-now request wakes an idle agent through the extension right after the bell", async () => {
  const f = new Fixture(); await f.setup(); await f.send();
  while (f.timeoutTokens.size) await f.runNextTimeout();
  await f.adapter.kick(f.context);
  expect(f.sent).toHaveLength(0);
  expect((await f.view()).messages[0].state).toBe("queued");
  while (f.timeoutTokens.size) await f.runNextTimeout();
  f.operations.length = 0;

  const scheduled = nextScheduledTimer(f);
  const result = await f.fixture("fyi_now") as { outcome: string; flagged: number };
  expect(result).toMatchObject({ outcome: "requested", flagged: 1 });
  await scheduled;  // only the user's bell can schedule this inspection
  await f.runNextTimeout();
  expect(f.operations.slice(-2)).toEqual(["inspect", "claim"]);
  expect(f.sent).toHaveLength(1);
  expect(f.sent[0].options).toEqual({ deliverAs: "nextTurn", triggerTurn: true });
  expect(f.sent[0].message.content).toContain("Real isolated body / fyi");
  expect(f.sent[0].message.content).toContain("FYI only: read and continue; reply only if it changes your work.\n");
  const view = await f.view();
  expect(view.messages[0].state).toBe("delivering"); expect(view.wakes).toBe(0);
  expect(f.warnings).toEqual([]);
});

test("user FYI-now request on a working agent still waits for the turn to end", async () => {
  const f = new Fixture(); await f.setup(); await f.send();  // enqueued while idle: not flagged
  await f.fixture("turn_state", { session_ref: f.session, turn_state: "working" });
  const result = await f.fixture("fyi_now") as { outcome: string; flagged: number };
  expect(result).toMatchObject({ outcome: "after_turn", flagged: 1 });
  await f.adapter.kick(f.context);
  expect(f.sent).toHaveLength(0); expect(f.operations).not.toContain("claim");
  await f.fixture("turn_state", { session_ref: f.session, turn_state: "done" });
  await f.adapter.kick(f.context);
  expect(f.sent).toHaveLength(1);
  expect(f.sent[0].message.content).toContain("Real isolated body / fyi");
});

test("young FYI waits for eligibility; task agents never register or consume", async () => {
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

test("input-quiet permission is opt-in and rejects malformed wire values", async () => {
  const f = new Fixture(); await f.setup(); await f.send();
  const { skip_input_quiet, ...legacy } = f.registrationReply!;
  expect(parseReply("inspect", legacy).skip_input_quiet).toBe(false);
  expect(parseReply("inspect", { ...legacy, skip_input_quiet: true }).skip_input_quiet).toBe(true);
  expect(() => parseReply("inspect", { ...legacy, skip_input_quiet: "true" })).toThrow("input-quiet permission");
  const result = await f.adapter.natural({ prompt: "real user" }, f.context);
  const raw = { ...result!.message.details, session_ref: f.session, content_sha256: "fixture-digest",
    message: result!.message };
  expect(parseReply("claim", raw)).toMatchObject({ skip_input_quiet: false });
  expect(parseReply("claim", { ...raw, skip_input_quiet: true })).toMatchObject({ skip_input_quiet: true });
  expect(() => parseReply("claim", { ...raw, skip_input_quiet: 1 })).toThrow("input-quiet permission");
});

test("explicit FYI now wakes after recent input without waiting three minutes", async () => {
  const now = Date.now(); setSystemTime(now);
  const f = new Fixture(); f.clock = now; await f.setup(3); await f.send();
  f.clock = now + 1000; setSystemTime(f.clock); f.terminalInput!("x");
  await f.adapter.kick(f.context);
  expect(f.sent).toHaveLength(0);
  expect(await f.fixture("fyi_now")).toMatchObject({ outcome: "requested" });
  await f.adapter.kick(f.context);
  expect(f.inspections.at(-1)).toMatchObject({ skip_input_quiet: true });
  expect(f.sent).toHaveLength(1);
  expect(f.sent[0].options).toEqual({ deliverAs: "nextTurn", triggerTurn: true });
});

test("FYI now retains empty supported editor and genuine runtime idle requirements", async () => {
  const now = Date.now(); setSystemTime(now);
  const f = new Fixture(); f.clock = now; await f.setup(3); await f.send();
  f.clock = now + 1000; setSystemTime(f.clock); f.terminalInput!("x");
  await f.fixture("fyi_now");
  f.editor = "draft"; await f.adapter.kick(f.context);
  f.editor = ""; f.editorSupported = false; await f.adapter.kick(f.context);
  f.editorSupported = true; f.mode = "rpc"; await f.adapter.kick(f.context);
  f.mode = "tui"; f.idle = false; await f.adapter.kick(f.context);
  f.idle = true; f.queued = true; await f.adapter.kick(f.context);
  expect(f.sent).toHaveLength(0);
  expect(f.operations).not.toContain("claim");
  f.queued = false; await f.adapter.kick(f.context);
  expect(f.sent).toHaveLength(1);
});

test("FYI now during working still waits for idle or done, not for input quiet", async () => {
  const now = Date.now(); setSystemTime(now);
  const f = new Fixture(); f.clock = now; await f.setup(3);
  await f.fixture("turn_state", { session_ref: f.session, turn_state: "working" });
  await f.send();
  f.clock = now + 1000; setSystemTime(f.clock); f.terminalInput!("x");
  expect(await f.fixture("fyi_now")).toMatchObject({ outcome: "after_turn", flagged: 0 });
  await f.adapter.kick(f.context);
  expect(f.sent).toHaveLength(0); expect(f.operations).not.toContain("claim");
  await f.fixture("turn_state", { session_ref: f.session, turn_state: "done" });
  await f.adapter.kick(f.context);
  expect(f.sent).toHaveLength(1);
});

test("automatic turn-end FYI never bypasses the three-minute input quiet gate", async () => {
  const now = Date.now(); setSystemTime(now);
  const f = new Fixture(); f.clock = now; await f.setup(3);
  await f.fixture("turn_state", { session_ref: f.session, turn_state: "working" });
  await f.send();
  await f.fixture("turn_state", { session_ref: f.session, turn_state: "done" });
  f.clock = now + 1000; setSystemTime(f.clock); f.terminalInput!("x");
  await f.adapter.kick(f.context);
  expect(f.inspections.at(-1)).toMatchObject({ has_action: true, skip_input_quiet: false });
  f.clock = now + 180999; setSystemTime(f.clock); await f.adapter.kick(f.context);
  expect(f.sent).toHaveLength(0); expect(f.operations).not.toContain("claim");
  f.clock = now + 181000; setSystemTime(f.clock); await f.adapter.kick(f.context);
  expect(f.sent).toHaveLength(1);
});

test("explicit FYI now preserves the sixty-second coalescing deadline", async () => {
  const now = Date.now(); setSystemTime(now);
  const f = new Fixture(); f.clock = now; await f.setup(3, 60); await f.send();
  f.clock = now + 1000; setSystemTime(f.clock); f.terminalInput!("x");
  expect(await f.fixture("fyi_now")).toMatchObject({ outcome: "requested", ready_in: 59 });
  await f.adapter.kick(f.context);
  f.clock = now + 59999; setSystemTime(f.clock); await f.adapter.kick(f.context);
  expect(f.sent).toHaveLength(0);
  f.clock = now + 60000; setSystemTime(f.clock); await f.adapter.kick(f.context);
  expect(f.sent).toHaveLength(1);
});

test("claim-time expired FYI-now permission cannot use a stale inspection to skip quiet", async () => {
  const now = Date.now(); setSystemTime(now);
  const f = new Fixture(); f.clock = now; await f.setup(3); await f.send();
  await f.fixture("fyi_now");
  f.clock = now + 600001; setSystemTime(f.clock); f.terminalInput!("x");
  f.transformInspection = state => ({ ...state, skip_input_quiet: true });
  await f.adapter.kick(f.context);
  expect(f.operations).toContain("claim");
  expect(f.operations).toContain("release");
  expect(f.sent).toHaveLength(0);
  expect((await f.view()).messages[0]).toMatchObject({ state: "queued", claim_id: null });
});

test("FYI-now quiet bypass does not override input arriving during claim", async () => {
  const now = Date.now(); setSystemTime(now);
  const f = new Fixture(); f.clock = now; await f.setup(3); await f.send();
  await f.fixture("fyi_now");
  f.afterClaim = async data => { if (data.wake) f.terminalInput!("x"); };
  await f.adapter.kick(f.context);
  expect(f.sent).toHaveLength(0);
  expect(f.operations).toContain("release");
  expect((await f.view()).messages[0]).toMatchObject({ state: "queued", claim_id: null });
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
  const ask = toolExecution("ask-1", "", "ask");
  f.adapter.tool(ask.start, f.context); await f.adapter.kick(f.context); expect(f.sent).toHaveLength(0);
  f.adapter.tool(ask.end, f.context); f.queued = true;
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

test("status summary names the session on register and deduplicates active inspections without waking", async () => {
  const f = new Fixture();
  await f.fixture("setup");
  await f.fixture("summary", { summary: "Inspect mailbox boundaries" });
  await f.adapter.start(f.context);
  const alias = f.registrationReply!.agent.alias!;
  expect(f.nameWrites.map(row => row.name)).toEqual([alias + " · Inspect mailbox boundaries"]);
  expect(f.titleState().source).toBe("user"); // Public OMP setter has no auto-source argument.
  f.adapter.agentStart(f.context);
  await f.send("action");
  await f.adapter.kick(f.eventContext());
  await f.adapter.kick(f.eventContext());
  expect(f.nameWrites).toHaveLength(1);
  await f.fixture("summary", { summary: "Review current status" });
  await f.adapter.kick(f.context);
  expect(f.titleState().name).toBe(alias + " · Review current status");
  expect(f.sent).toEqual([]);
  expect(f.operations).not.toContain("claim");
});

for (const source of ["user", undefined, "auto"] as const) {
  test(`existing ${source ?? "unknown"}-source name keeps its provenance when adopting HAM status`, async () => {
    const f = new Fixture();
    f.rename("Existing session", source);
    // Header-only auto titles are a supported source of provenance too.
    if (source === "auto") f.titleState().entries.length = 0;
    await f.startNamingSession("setup", "New summary");
    await f.adapter.kick(f.context);
    if (source === "auto") expect(f.titleState().name).toBe(f.registrationReply!.agent.alias + " · New summary");
    else { expect(f.titleState().name).toBe("Existing session"); expect(f.nameWrites).toEqual([]); }
  });
}

for (const sameName of [false, true]) {
  test(`later user ${sameName ? "same-name reassertion" : "rename"} relinquishes HAM ownership`, async () => {
    const f = new Fixture(); await f.setup();
    await f.fixture("summary", { summary: "Owned" });
    await f.adapter.kick(f.context);
    const humanName = sameName ? f.titleState().name! : "Human chosen name";
    f.rename(humanName, "user");
    await f.fixture("summary", { summary: "Must not replace the user" });
    await f.adapter.kick(f.context);
    await f.fixture("summary", { summary: "" });
    await f.adapter.kick(f.context);
    expect(f.titleState().name).toBe(humanName);
    expect(f.nameWrites).toHaveLength(1);
  });
}

test("registration refresh picks up alias changes without role or emoji decorations", async () => {
  const f = new Fixture(); await f.setup();
  await f.fixture("summary", { summary: "Alias work" });
  await f.adapter.kick(f.context);
  f.registrationReply = { ...f.registrationReply!, summary: "Alias work",
    agent: { ...f.registrationReply!.agent, alias: "new-alias" } };
  f.cacheRegistrations = true;
  await f.adapter.natural({ prompt: "refresh identity" }, f.context);
  expect(f.titleState().name).toBe("new-alias · Alias work");
  expect(f.nameWrites).toHaveLength(2);
});

test("session titles sanitize whitespace and controls and truncate UTF-8 only at code-point boundaries", async () => {
  const f = new Fixture(); await f.setup();
  f.cachedInspection = { ...f.registrationReply!, summary: "\t Inspect\u0000   status\nnow\u2028 " };
  await f.adapter.kick(f.context);
  expect(f.titleState().name).toBe(f.registrationReply!.agent.alias + " · Inspect status now");
  f.cachedInspection = { ...f.cachedInspection, summary: "한글🐦".repeat(40) };
  await f.adapter.kick(f.context);
  const name = f.titleState().name!;
  expect(Buffer.byteLength(name, "utf8")).toBeLessThanOrEqual(96);
  expect(name.endsWith("…")).toBe(true);
  expect(name).not.toContain("\ufffd");
  expect(Buffer.from(name, "utf8").toString("utf8")).toBe(name);
});

test("old inspect replies omit summary without clearing a name and malformed summary is rejected", async () => {
  const f = new Fixture(); await f.setup();
  await f.fixture("summary", { summary: "Keep this name" });
  await f.adapter.kick(f.context);
  const name = f.titleState().name;
  const { summary, ...legacy } = f.registrationReply!;
  f.cachedInspection = parseReply("inspect", legacy);
  expect(f.cachedInspection.summary).toBeUndefined();
  await f.adapter.kick(f.context);
  expect(f.titleState().name).toBe(name);
  expect(f.nameWrites).toHaveLength(1);
  expect(() => parseReply("inspect", { ...legacy, summary: null })).toThrow("HAM summary");
  expect(() => parseReply("inspect", { ...legacy, summary: 1 })).toThrow("HAM summary");
  expect(parseReply("inspect", { ...legacy, summary: "" }).summary).toBe("");
});

test("explicit clear restores only an owned prior auto name and can name it again afterward", async () => {
  const f = new Fixture(); f.rename("Original auto title", "auto"); await f.setup();
  expect(f.nameWrites).toEqual([]);
  await f.fixture("summary", { summary: "First task" });
  await f.adapter.kick(f.context);
  await f.fixture("summary", { summary: "" });
  await f.adapter.kick(f.context);
  expect(f.titleState().name).toBe("Original auto title");
  expect(f.titleState().source).toBe("user");
  await f.adapter.kick(f.context);
  expect(f.nameWrites).toHaveLength(2);
  await f.fixture("summary", { summary: "Second task" });
  await f.adapter.kick(f.context);
  expect(f.titleState().name).toBe(f.registrationReply!.agent.alias + " · Second task");
  expect(f.nameWrites).toHaveLength(3);
});

test("explicit clear leaves an originally unnamed session's last owned name without a fake clear", async () => {
  const f = new Fixture(); await f.setup();
  expect(f.nameWrites).toEqual([]);
  await f.fixture("summary", { summary: "Last owned title" });
  await f.adapter.kick(f.context);
  const name = f.titleState().name;
  await f.fixture("summary", { summary: "" });
  await f.adapter.kick(f.context);
  expect(f.titleState().name).toBe(name);
  expect(f.nameWrites).toHaveLength(1);
});

test("registered execution events refresh successful own status once without a tool_result subscription", async () => {
  const now = Date.now(); setSystemTime(now);
  const f = new Fixture(); await f.setup(); await f.runNextTimeout();
  const handlers = new Map<string, (event: ToolExecutionStartEvent | ToolExecutionEndEvent, ctx: ExtensionContext) => unknown>();
  installMailbox({ on: (type: string, handler: (event: ToolExecutionStartEvent | ToolExecutionEndEvent, ctx: ExtensionContext) => unknown) => {
    handlers.set(type, handler);
  } } as unknown as ExtensionAPI);
  expect(handlers.has("tool_result")).toBe(false);
  expect(handlers.has("tool_execution_start")).toBe(true);
  expect(handlers.has("tool_execution_end")).toBe(true);
  // Route the installed callbacks through the fixture's isolated receiver.
  const handleTool = f.adapter.tool.bind(f.adapter);
  const tool = spyOn(MailboxAdapter.prototype, "tool").mockImplementation(handleTool);
  const dispatch = (event: ToolExecutionStartEvent | ToolExecutionEndEvent, ctx = f.eventContext()) =>
    handlers.get(event.type)!(event, ctx);
  try {
    f.adapter.agentStart(f.context);
    await f.fixture("summary", { summary: "Prompt status update" });
    const event = toolExecution("status-1",
      'python3 "$HOME/.omp/agent/skills/herdr-agent-messenger/scripts/herdr_agent_messenger.py" status set review --summary "Prompt status update"');
    f.operations.length = 0;
    expect(dispatch(event.start)).toBeUndefined();
    expect(f.operations).toEqual([]);
    expect([...f.timeoutTokens].map(token => f.timerDueAt.get(token))).toEqual([now + 250]);
    expect(dispatch(event.end)).toBeUndefined();
    expect(f.operations).toEqual([]);
    expect([...f.timeoutTokens].map(token => f.timerDueAt.get(token))).toEqual([now]);
    await f.runNextTimeout();
    expect(f.operations).toEqual(["inspect"]);
    expect(f.titleState().name).toBe(f.registrationReply!.agent.alias + " · Prompt status update");
    dispatch(event.end);
    dispatch(event.start); dispatch(event.end);
    expect(f.timeoutTokens.size).toBe(0);
    for (const command of ["echo 'python3 -m ham cli status set review --summary text'",
      "python3 -m ham cli status board", "python3 -m ham cli mail send --to someone",
      "ssh other python3 -m ham cli status clear", "python3 unrelated.py status clear"]) {
      const unrelated = toolExecution(command, command);
      dispatch(unrelated.start); dispatch(unrelated.end);
    }
    const failed = toolExecution("failed", "python3 -m ham cli status clear");
    dispatch(failed.start); dispatch({ ...failed.end, isError: true });
    dispatch(failed.start); dispatch(failed.end);
    const notBash = toolExecution("not-bash", "python3 -m ham cli status clear", "read");
    dispatch(notBash.start); dispatch(notBash.end);
    dispatch(toolExecution("unmatched-end", "python3 -m ham cli status clear").end);
    f.kind = "sub";
    const nested = toolExecution("nested", "python3 -m ham cli status clear");
    dispatch(nested.start); dispatch(nested.end);
    f.kind = "main";
    dispatch(nested.end);
    const invalidArgs = toolExecution("invalid-args", "");
    dispatch({ ...invalidArgs.start, args: null }); dispatch(invalidArgs.end);
    expect(f.operations).toEqual(["inspect"]);
    expect(f.timeoutTokens.size).toBe(0);
    await f.fixture("summary", { summary: "" });
    const clear = toolExecution("clear", "python3 -m ham --cwd /some/repo cli status clear");
    dispatch(clear.start); dispatch(clear.end);
    expect(f.operations).toEqual(["inspect"]);
    expect([...f.timeoutTokens].map(token => f.timerDueAt.get(token))).toEqual([now]);
    await f.runNextTimeout();
    expect(f.operations).toEqual(["inspect", "inspect"]);
    expect(f.sent).toEqual([]);
  } finally { tool.mockRestore(); }
});

test("status refresh during an existing probe requests one serialized trailing inspection", async () => {
  const f = new Fixture(); await f.setup(); await f.runNextTimeout();
  f.adapter.agentStart(f.context);
  const entered = Promise.withResolvers<void>(), release = Promise.withResolvers<void>();
  f.beforeInspect = async () => { entered.resolve(); await release.promise; };
  f.operations.length = 0;
  const first = f.adapter.kick(f.context);
  await entered.promise;
  const event = toolExecution("coalesced-status", "python3 -m ham.cli status set review --summary coalesced");
  expect(f.adapter.tool(event.start, f.context)).toBeUndefined();
  expect(f.adapter.tool(event.end, f.context)).toBeUndefined();
  f.adapter.tool(event.start, f.context); f.adapter.tool(event.end, f.context);
  expect(f.timeoutTokens.size).toBe(1);
  await f.runNextTimeout(); // Busy kick only records one trailing probe.
  expect(f.operations).toEqual(["inspect"]);
  release.resolve(); await first;
  f.beforeInspect = undefined;
  await f.runNextTimeout();
  expect(f.operations).toEqual(["inspect", "inspect"]);
  expect(f.timeoutTokens.size).toBe(0);
});

test("status call tracking evicts the oldest ID and still refreshes the newest completion", async () => {
  const f = new Fixture(); await f.setup(); await f.runNextTimeout();
  const command = "python3 -m ham cli status set review --summary bounded";
  for (let index = 0; index < 129; index++)
    f.adapter.tool(toolExecution("bounded-" + index, command).start, f.context);
  f.operations.length = 0;
  f.adapter.tool(toolExecution("bounded-0", command).end, f.context);
  expect(f.timeoutTokens.size).toBe(0);
  expect(f.operations).toEqual([]);
  f.cachedInspection = { ...f.registrationReply!, summary: "Bounded status history" };
  const newest = toolExecution("bounded-128", command);
  f.adapter.tool(newest.end, f.context);
  expect(f.timeoutTokens.size).toBe(1);
  expect(f.operations).toEqual([]);
  await f.runNextTimeout();
  expect(f.operations).toEqual(["inspect"]);
  expect(f.titleState().name).toBe(f.registrationReply!.agent.alias + " · Bounded status history");
  f.adapter.tool(newest.start, f.context); f.adapter.tool(newest.end, f.context);
  expect(f.timeoutTokens.size).toBe(0);
});

test("session switches discard pending status calls and reject stale execution contexts", async () => {
  const f = new Fixture(); await f.setup(); await f.runNextTimeout();
  const firstSession = f.session;
  const staleContext = Object.create(f.context) as ExtensionContext;
  Object.defineProperty(staleContext, "sessionManager", { value: {
    ...f.context.sessionManager, getSessionFile: () => firstSession,
  } });
  const event = toolExecution("switched-status", "python3 -m ham cli status set review --summary switched");
  f.adapter.tool(event.start, staleContext);
  f.session = path.join(f.directory, "status-next-session.jsonl");
  await f.fixture("session", { session_ref: f.session });
  await f.adapter.start(f.context);
  await f.runNextTimeout();
  f.operations.length = 0;
  f.adapter.tool(event.end, f.eventContext());
  f.adapter.tool(event.start, staleContext); f.adapter.tool(event.end, staleContext);
  expect(f.timeoutTokens.size).toBe(0);
  expect(f.operations).toEqual([]);
  await f.fixture("summary", { summary: "Current session status" });
  f.adapter.tool(event.start, f.eventContext());
  f.adapter.tool(event.end, staleContext);
  expect(f.timeoutTokens.size).toBe(0);
  f.adapter.tool(event.end, f.eventContext());
  expect(f.operations).toEqual([]);
  expect(f.timeoutTokens.size).toBe(1);
  await f.runNextTimeout();
  expect(f.operations).toEqual(["inspect"]);
  expect(f.titleState().name).toBe(f.registrationReply!.agent.alias + " · Current session status");
  expect(f.nameWrites.every(row => row.session === f.session)).toBe(true);
});

test("session switches isolate ownership and preserve it when returning to an owned session", async () => {
  const f = new Fixture();
  await f.startNamingSession("setup", "First session");
  await f.adapter.kick(f.context);
  const firstSession = f.session, firstTitle = f.titleState().name;
  f.session = path.join(f.directory, "named-session.jsonl");
  f.rename("Protected other session", "user");
  await f.startNamingSession("session", "Other task");
  await f.adapter.kick(f.context);
  expect(f.titleState().name).toBe("Protected other session");
  expect(f.nameWrites).toHaveLength(1);
  f.session = firstSession;
  await f.startNamingSession("session", "Returning task");
  await f.adapter.kick(f.context);
  expect(f.titleState().name).not.toBe(firstTitle);
  expect(f.titleState().name).toBe(f.registrationReply!.agent.alias + " · Returning task");
  expect(f.nameWrites.every(row => row.session === firstSession)).toBe(true);
});

test("an old inspect response cannot rename a switched session", async () => {
  const f = new Fixture(); await f.setup();
  await f.fixture("summary", { summary: "Stale task" });
  f.cachedInspection = { ...f.registrationReply!, summary: "Stale task" };
  const entered = Promise.withResolvers<void>(), release = Promise.withResolvers<void>();
  f.beforeInspect = async () => { entered.resolve(); await release.promise; };
  const oldWork = f.adapter.kick(f.context);
  await entered.promise;
  f.session = path.join(f.directory, "protected-next-session.jsonl");
  f.rename("Next user title", "user");
  await f.fixture("session", { session_ref: f.session });
  await f.adapter.start(f.context);
  f.beforeInspect = undefined;
  release.resolve(); await oldWork;
  expect(f.titleState().name).toBe("Next user title");
  expect(f.nameWrites).toEqual([]);
});

test("a same-name user rename during an awaited setter cannot be recorded as HAM ownership", async () => {
  const f = new Fixture(); await f.setup();
  const entered = Promise.withResolvers<void>(), release = Promise.withResolvers<void>();
  f.afterSetName = async () => { entered.resolve(); await release.promise; };
  await f.fixture("summary", { summary: "Pending name" });
  const work = f.adapter.kick(f.context);
  await entered.promise;
  const userName = f.titleState().name!;
  f.rename(userName, "user");
  release.resolve(); await work;
  f.afterSetName = undefined;
  await f.fixture("summary", { summary: "Do not overwrite" });
  await f.adapter.kick(f.context);
  expect(f.titleState().name).toBe(userName);
  expect(f.nameWrites).toHaveLength(1);
});

test("a setter resolving after session switch never claims ownership of the next session", async () => {
  const f = new Fixture(); await f.setup();
  const entered = Promise.withResolvers<void>(), release = Promise.withResolvers<void>();
  f.afterSetName = async () => { entered.resolve(); await release.promise; };
  await f.fixture("summary", { summary: "Old pending title" });
  const work = f.adapter.kick(f.context);
  await entered.promise;
  const firstSession = f.session;
  f.session = path.join(f.directory, "setter-next-session.jsonl");
  f.rename("Next protected name", "user");
  await f.fixture("session", { session_ref: f.session });
  const start = f.adapter.start(f.context);
  release.resolve(); await work; await start;
  f.afterSetName = undefined;
  await f.fixture("summary", { summary: "Next summary" });
  await f.adapter.kick(f.context);
  expect(f.titleState().name).toBe("Next protected name");
  expect(f.nameWrites).toHaveLength(1);
  expect(f.nameWrites[0].session).toBe(firstSession);
});

for (const failure of ["get", "entries", "set", "id"] as const) {
  test(`name API ${failure} failure never interrupts registration, natural receipt, or action wake`, async () => {
    const f = new Fixture(); f.nameFailure = failure;
    await f.fixture("setup");
    await f.fixture("summary", { summary: "Naming is cosmetic" });
    await f.adapter.start(f.context);
    expect(f.operations).toEqual(["register"]);
    expect(f.warnings).toHaveLength(1);
    expect(f.timeoutTokens.size).toBe(1); // Only the ordinary start timer.
    await f.send();
    const received = await f.adapter.natural({ prompt: "Receive despite naming failure" }, f.context);
    expect(received?.message.customType).toBe("ham_mailbox");
    f.accept(received!.message);
    await f.adapter.confirm(f.context);
    await f.send("action");
    await f.adapter.kick(f.context);
    expect(f.sent).toHaveLength(1);
    expect(f.warnings).toHaveLength(1); // Same name phase remains deduplicated.
    expect(f.statuses.has("ham-mailbox")).toBe(true);
    f.nameFailure = undefined;
    await f.adapter.kick(f.context);
    expect(f.statuses.has("ham-mailbox")).toBe(false);
    expect(f.titleState().name).toBe(f.registrationReply!.agent.alias + " · Naming is cosmetic");
  });
}

test("successful name synchronization clears only name errors, not an outstanding inspect failure", async () => {
  const f = new Fixture(); f.nameFailure = "get"; await f.setup();
  f.beforeInspect = async () => { throw new Error("unrelated inspect failure"); };
  await f.adapter.kick(f.context);
  expect(f.warnings).toHaveLength(2);
  f.nameFailure = undefined;
  await f.adapter.natural({ prompt: "Recover name through registration only" }, f.context);
  expect(f.statuses.has("ham-mailbox")).toBe(true);
  f.beforeInspect = undefined;
  await f.adapter.kick(f.context);
  expect(f.statuses.has("ham-mailbox")).toBe(false);
});

test("journal-only ownership resumes with a fresh adapter and deduplicates both names and saved state", async () => {
  const f = new Fixture(); await f.setup();
  await f.fixture("summary", { summary: "Before restart" });
  await f.adapter.kick(f.context);
  const firstTitle = f.titleState().name;
  const titleEntry = f.titleState().entries.findLast(entry => entry.type === "title_change")!;
  expect(f.customWrites).toEqual([{ customType: "ham-session-name", data: {
    version: 1, session_id: f.session, name: firstTitle, title_entry_id: titleEntry.id, at: expect.any(Number),
  } }]);
  await f.restart();
  await f.adapter.kick(f.context);
  expect(f.nameWrites).toHaveLength(1);
  expect(f.customWrites).toHaveLength(1);
  await f.fixture("summary", { summary: "After resume" });
  await f.adapter.kick(f.context);
  expect(f.titleState().name).toBe(f.registrationReply!.agent.alias + " · After resume");
  expect(f.nameWrites).toHaveLength(2);
  expect(f.customWrites).toHaveLength(2);
  expect(f.titleState().entries.filter(entry => entry.type === "custom")).toHaveLength(2);
  expect(f.branch).toEqual([]);
  expect(f.sent).toEqual([]);
});

for (const beforeRestart of [true, false]) {
  for (const sameName of [true, false]) {
    test(`user ${sameName ? "same-name reassertion" : "rename"} ${beforeRestart ? "before" : "after"} restart remains protected`, async () => {
      const f = new Fixture(); await f.setup();
      await f.fixture("summary", { summary: "Previously owned" });
      await f.adapter.kick(f.context);
      const humanName = sameName ? f.titleState().name! : "Human title around restart";
      if (beforeRestart) f.rename(humanName, "user");
      await f.restart();
      if (!beforeRestart) f.rename(humanName, "user");
      await f.fixture("summary", { summary: "Do not reclaim" });
      await f.adapter.kick(f.context);
      await f.fixture("summary", { summary: "" });
      await f.adapter.kick(f.context);
      expect(f.titleState().name).toBe(humanName);
      expect(f.nameWrites).toHaveLength(1);
      expect(f.customWrites).toHaveLength(1);
    });
  }
}

test("prior auto name restoration and ownership survive restarts before and after clear", async () => {
  const f = new Fixture(); f.rename("Original automatic name", "auto"); await f.setup();
  await f.fixture("summary", { summary: "Owned before restart" });
  await f.adapter.kick(f.context);
  await f.restart();
  await f.fixture("summary", { summary: "" });
  await f.adapter.kick(f.context);
  expect(f.titleState().name).toBe("Original automatic name");
  expect(f.titleState().source).toBe("user");
  expect(f.customWrites).toHaveLength(2);
  expect(f.customWrites[1].data).toMatchObject({ name: "Original automatic name", previous: "Original automatic name" });
  await f.restart();
  expect(f.nameWrites).toHaveLength(2);
  expect(f.customWrites).toHaveLength(2);
  await f.fixture("summary", { summary: "Owned again after clear and resume" });
  await f.adapter.kick(f.context);
  expect(f.titleState().name).toBe(f.registrationReply!.agent.alias + " · Owned again after clear and resume");
  expect(f.customWrites).toHaveLength(3);
  expect(f.customWrites[2].data).toMatchObject({ previous: "Original automatic name" });
});

test("an originally unnamed cleared session retains resumable ownership without a synthetic blank entry", async () => {
  const f = new Fixture(); await f.setup();
  await f.fixture("summary", { summary: "Last visible name" });
  await f.adapter.kick(f.context);
  const name = f.titleState().name;
  await f.fixture("summary", { summary: "" });
  await f.restart();
  expect(f.titleState().name).toBe(name);
  expect(f.customWrites).toHaveLength(1);
  await f.fixture("summary", { summary: "Resumed work" });
  await f.adapter.kick(f.context);
  expect(f.titleState().name).toBe(f.registrationReply!.agent.alias + " · Resumed work");
  expect(f.customWrites).toHaveLength(2);
});

test("resume ignores malformed or foreign saved state and uses the latest valid current-session record", async () => {
  const f = new Fixture(); await f.setup();
  await f.fixture("summary", { summary: "Valid saved name" });
  await f.adapter.kick(f.context);
  const valid = f.customWrites[0].data as Record<string, unknown>;
  for (const data of [null, [], {}, { ...valid, version: 2 }, { ...valid, at: "invalid" },
    { ...valid, previous: 123 }, { ...valid, title_entry_id: "" }, { ...valid, session_id: "foreign-session" }]) {
    f.titleState().entries.push({ type: "custom", id: "malformed-" + ++f.titleSerial, customType: "ham-session-name", data });
  }
  await f.restart();
  await f.fixture("summary", { summary: "Resume valid ownership" });
  await f.adapter.kick(f.context);
  expect(f.titleState().name).toBe(f.registrationReply!.agent.alias + " · Resume valid ownership");
  expect(f.customWrites).toHaveLength(2);
});

test("saved state for another session cannot adopt a preexisting user name", async () => {
  const f = new Fixture(); f.rename("User-owned name", "user");
  const title = f.titleState().entries[0];
  f.titleState().entries.push({ type: "custom", id: "foreign-record", customType: "ham-session-name",
    data: { version: 1, session_id: "other-session", name: "User-owned name", title_entry_id: title.id, at: Date.now() } });
  await f.setup();
  await f.fixture("summary", { summary: "Must preserve" });
  await f.adapter.kick(f.context);
  expect(f.titleState().name).toBe("User-owned name");
  expect(f.nameWrites).toEqual([]);
  expect(f.customWrites).toEqual([]);
});

test("appendEntry failures are isolated from natural mail receipt and action wake", async () => {
  const f = new Fixture(); await f.setup(); f.nameFailure = "append";
  await f.fixture("summary", { summary: "Name changed but state write failed" });
  await f.adapter.kick(f.context);
  expect(f.nameWrites).toHaveLength(1);
  expect(f.customWrites).toEqual([]);
  expect(f.warnings).toHaveLength(1);
  await f.send();
  const received = await f.adapter.natural({ prompt: "Receive despite state append failure" }, f.context);
  expect(received?.message.customType).toBe("ham_mailbox");
  f.accept(received!.message); await f.adapter.confirm(f.context);
  await f.send("action");
  await f.adapter.kick(f.context);
  expect(f.sent).toHaveLength(1);
  expect(f.nameWrites).toHaveLength(1);
  expect(f.customWrites).toEqual([]);
});
