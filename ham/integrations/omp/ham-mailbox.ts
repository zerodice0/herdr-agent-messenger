/** HAM-owned opt-in receiver. No tools, nested agents, steering or provider ACK guesses. */
import { spawn } from "node:child_process";
import { createHash } from "node:crypto";
import { watch, type FSWatcher } from "node:fs";
import { access } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import type { ExtensionAPI, ExtensionContext } from "@oh-my-pi/pi-coding-agent";

type Request = Record<string, unknown>;
export interface NativeAgent { terminal_id: string; agent_id: string; generation: number; session_ref: string }
export interface NativeClaim {
  claim_id: string; agent_id: string; generation: number; session_ref: string; marker: string; content_sha256: string;
}
interface ClaimDetails { protocol: 1; claim_id: string; agent_id: string; generation: number; session_ref: string; marker: string }
export interface NativeMessage {
  customType: "ham_mailbox"; content: string; details: ClaimDetails; display: true; attribution: "agent";
}
export interface ClaimBundle extends NativeClaim { message: NativeMessage }
interface Thresholds { idle_minutes: number; coalesce_seconds: number; wakes_per_hour: number; max_hop: number }
interface Inspection {
  thresholds: Thresholds; role: "main" | "worker" | null; turn_state: string; wake_count: number;
  pending_claims: NativeClaim[]; has_action: boolean; coalesce_until: number;
}
interface Registration extends Inspection { agent: NativeAgent; bell_directory: string; bell_name: string }
export interface ReceiverReplies {
  register: Registration; inspect: Inspection; claim: ClaimBundle | { claim_id: null; reason: string };
  applied: { receipts: unknown[] }; release: { released: number; uncertain: number };
}
type Operation = keyof ReceiverReplies;
type Call = <O extends Operation>(operation: O, data: Request) => Promise<ReceiverReplies[O]>;
type Pending = NativeClaim & { phase: "prepared" | "sent" | "unknown"; preparation?: string; message?: NativeMessage };
interface EndEvent { willContinue?: boolean; messages?: readonly { role: string; stopReason?: string }[] }
const WAKE_READY_STATES: Record<string, true> = { idle: true, done: true };
const sha256 = (value: string) => createHash("sha256").update(value).digest("hex");
const identity = (agent: NativeAgent) => ({ terminal_id: agent.terminal_id, agent_id: agent.agent_id, generation: agent.generation });

function record(value: unknown): Request {
  if (typeof value !== "object" || value === null || Array.isArray(value)) throw new Error("Invalid HAM JSON object");
  // Object narrowing retains unknown member values; each consumed field is checked below.
  return value as Request;
}
function string(value: unknown): string {
  if (typeof value !== "string" || !value) throw new Error("Invalid HAM string field");
  return value;
}
function number(value: unknown): number {
  if (typeof value !== "number" || !Number.isFinite(value)) throw new Error("Invalid HAM numeric field");
  return value;
}
function array(value: unknown): unknown[] {
  if (!Array.isArray(value)) throw new Error("Invalid HAM array field");
  return value;
}
function nativeClaim(value: unknown): NativeClaim {
  const row = record(value);
  return { claim_id: string(row.claim_id), agent_id: string(row.agent_id), generation: number(row.generation),
    session_ref: string(row.session_ref), marker: string(row.marker), content_sha256: string(row.content_sha256) };
}
function inspection(value: Request): Inspection {
  const thresholds = record(value.thresholds);
  if (value.role !== null && value.role !== "main" && value.role !== "worker") throw new Error("Invalid HAM role");
  if (typeof value.has_action !== "boolean") throw new Error("Invalid HAM action eligibility");
  return { thresholds: { idle_minutes: number(thresholds.idle_minutes), coalesce_seconds: number(thresholds.coalesce_seconds),
      wakes_per_hour: number(thresholds.wakes_per_hour), max_hop: number(thresholds.max_hop) },
    role: value.role, turn_state: string(value.turn_state), wake_count: number(value.wake_count),
    pending_claims: array(value.pending_claims).map(nativeClaim), has_action: value.has_action, coalesce_until: number(value.coalesce_until) };
}
export function parseReply<O extends Operation>(operation: O, raw: unknown): ReceiverReplies[O] {
  const value = record(raw);
  let parsed: ReceiverReplies[Operation];
  if (operation === "register") {
    const agent = record(value.agent);
    parsed = { ...inspection(value), agent: { terminal_id: string(agent.terminal_id), agent_id: string(agent.agent_id),
      generation: number(agent.generation), session_ref: string(agent.session_ref) },
      bell_directory: string(value.bell_directory), bell_name: string(value.bell_name) };
  } else if (operation === "inspect") parsed = inspection(value);
  else if (operation === "claim") {
    if (value.claim_id === null) parsed = { claim_id: null, reason: string(value.reason) };
    else {
      const message = record(value.message), details = record(message.details);
      if (message.customType !== "ham_mailbox" || message.attribution !== "agent" || message.display !== true || details.protocol !== 1)
        throw new Error("Invalid HAM context message");
      parsed = { ...nativeClaim(value), message: { customType: "ham_mailbox", content: string(message.content),
        display: true, attribution: "agent", details: { protocol: 1, claim_id: string(details.claim_id),
          agent_id: string(details.agent_id), generation: number(details.generation), session_ref: string(details.session_ref), marker: string(details.marker) } } };
    }
  } else if (operation === "applied") parsed = { receipts: array(value.receipts) };
  else parsed = { released: number(value.released), uncertain: number(value.uncertain) };
  // All external fields above were validated. TS cannot correlate a generic O
  // with its runtime switch, so only this checked discriminant mapping is asserted.
  return parsed as ReceiverReplies[O];
}

class ReceiverError extends Error {
  constructor(readonly code: string, message: string) {
    super("HAM CLI: " + code + ": " + message);
  }
}

async function runJSON(program: string, args: string[], input?: Request, cwd?: string): Promise<unknown> {
  const { promise, resolve, reject } = Promise.withResolvers<unknown>();
  const child = spawn(program, args, { cwd, env: process.env, stdio: ["pipe", "pipe", "pipe"], timeout: 15000 });
  let stdout = "", stderr = "";
  child.stdout.setEncoding("utf8");
  child.stderr.setEncoding("utf8");
  child.stdout.on("data", (data: string) => {
    stdout += data;
    if (Buffer.byteLength(stdout) > 1024 * 1024) child.kill();
  });
  child.stderr.on("data", (data: string) => { stderr = (stderr + data).slice(-4096); });
  child.on("error", reject);
  child.on("close", code => {
    try {
      const result: unknown = JSON.parse(stdout), response = record(result);
      if (code !== 0 || response.error !== undefined) {
        const error = record(response.error);
        throw new ReceiverError(string(error.code), string(error.message));
      }
      resolve(result);
    } catch (error) {
      reject(error instanceof ReceiverError ? error : new Error("HAM CLI: " + (stdout ? String(error) : stderr || "no JSON response")));
    }
  });
  child.stdin.on("error", () => undefined);
  child.stdin.end(input === undefined ? "" : JSON.stringify(input));
  return promise;
}

export async function findRoot(): Promise<string> {
  const explicit = process.env.HERDR_AGENT_MESSENGER_ROOT;
  if (explicit) {
    if (!path.isAbsolute(explicit)) throw new Error("HAM explicit root must be absolute");
    await access(path.join(explicit, "ham", "__init__.py"));
    await access(path.join(explicit, "herdr-plugin.toml"));
    return explicit;
  }
  // Explicit loading from an owned source checkout also works before installation.
  const bundled = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../../..");
  try {
    await access(path.join(bundled, "ham", "__init__.py"));
    await access(path.join(bundled, "herdr-plugin.toml"));
    return bundled;
  } catch { /* Installed copies discover the CURRENT structured root on every operation. */ }
  const result = record(await runJSON(process.env.HERDR_BIN_PATH || "herdr",
    ["plugin", "list", "--json", "--plugin", "herdr.agent-messenger"]));
  const payload = record(result.result);
  const root = record(array(payload.plugins)[0]).plugin_root;
  if (typeof root !== "string" || !path.isAbsolute(root)) throw new Error("HAM plugin_root unavailable");
  await access(path.join(root, "ham", "__init__.py"));
  await access(path.join(root, "herdr-plugin.toml"));
  return root;
}

export async function callReceiver<O extends Operation>(operation: O, data: Request): Promise<ReceiverReplies[O]> {
  const root = await findRoot();
  const args = operation === "release" ? ["ledger", "delivery", "release"] : ["ledger", "runtime", operation];
  const response = await runJSON(process.env.HERDR_AGENT_PYTHON || "python3", ["-m", "ham", "cli", ...args], data, root);
  return parseReply(operation, response);
}

/** Public read-only active-branch acceptance, not provider/response success.
 * A later context/provider transform can still omit host-accepted history.
 * Full content hash prevents ACK of a marker whose actual bundle was changed.
 */
export function acceptedEntry(ctx: ExtensionContext, pending: NativeClaim): string | undefined {
  if (ctx.sessionManager.getSessionFile() !== pending.session_ref) return undefined;
  const entries = ctx.sessionManager.getBranch();
  for (let index = entries.length - 1; index >= 0; index--) {
    const entry = entries[index];
    if (entry.type !== "custom_message" || entry.customType !== "ham_mailbox" || entry.attribution !== "agent") continue;
    if (typeof entry.details !== "object" || entry.details === null || Array.isArray(entry.details)) continue;
    const details = record(entry.details);
    if (details.protocol !== 1 || details.claim_id !== pending.claim_id || details.agent_id !== pending.agent_id ||
        details.generation !== pending.generation || details.session_ref !== pending.session_ref || details.marker !== pending.marker) continue;
    if (typeof entry.content !== "string" || !entry.content.startsWith(pending.marker + "\n") ||
        sha256(entry.content) !== pending.content_sha256) continue;
    return typeof entry.id === "string" ? entry.id : undefined;
  }
  return undefined;
}

export class MailboxAdapter {
  private epoch = 0;
  private sessionRef?: string;
  // Kept only to stop resources created by session_start; event contexts themselves are fresh per dispatch.
  private context?: ExtensionContext;
  private registration?: Registration;
  private registering?: Promise<Registration | undefined>;
  private watcher?: FSWatcher;
  private unsubscribe?: () => void;
  private timer?: Timer;
  private timerAt = Infinity;
  private safety?: Timer;
  private work?: { epoch: number; operation: Promise<void> };
  private recheckAfterWork?: number;
  private acknowledging = false;
  private pending = new Map<string, Pending>();
  private inputSerial = 0;
  private lastKey = Date.now();
  private preparing = 0;
  private active = false;
  private continuation = false;
  private retrying = false;
  private retryHoldUntil = 0;
  private failureBlocked = false;
  private blocked = 0;
  private compacting = false;
  private refreshNeeded = true;
  private lastError = "";
  private registrationFailures = 0;

  constructor(private pi: ExtensionAPI, private call: Call = callReceiver) {}

  private root(ctx: ExtensionContext): boolean {
    return ctx.agent?.kind === "main" && process.env.HERDR_ENV === "1" &&
      !!process.env.HERDR_SOCKET_PATH && !!process.env.HERDR_PANE_ID && process.env.OMPCODE !== "1";
  }
  private belongs(ctx: ExtensionContext): boolean {
    return this.root(ctx) && this.sessionRef !== undefined && ctx.sessionManager.getSessionFile() === this.sessionRef;
  }
  private current(ctx: ExtensionContext, epoch = this.epoch): boolean {
    return this.belongs(ctx) && epoch === this.epoch && !!this.registration &&
      ctx.sessionManager.getSessionFile() === this.registration.agent.session_ref;
  }
  private report(ctx: ExtensionContext, error: unknown): void {
    const text = String(error);
    ctx.ui.setStatus("ham-mailbox", "HAM: receiver unavailable");
    if (text !== this.lastError) { ctx.ui.notify(text, "warning"); this.lastError = text; }
  }
  schedule(ctx: ExtensionContext, delay = 250): void {
    if (!this.belongs(ctx)) return;
    const at = Date.now() + Math.max(0, delay);
    if (this.timer && this.timerAt <= at) return;
    if (this.timer) ctx.clearTimer(this.timer);
    this.timerAt = at;
    const epoch = this.epoch;
    this.timer = ctx.setTimeout(async () => {
      this.timer = undefined; this.timerAt = Infinity;
      if (epoch !== this.epoch) return;
      await this.kick(ctx);
    }, Math.max(0, delay));
  }
  private stop(ctx: ExtensionContext): void {
    this.watcher?.close(); this.watcher = undefined;
    this.unsubscribe?.(); this.unsubscribe = undefined;
    if (this.timer) ctx.clearTimer(this.timer);
    if (this.safety) ctx.clearTimer(this.safety);
    this.timer = this.safety = undefined; this.timerAt = Infinity;
  }
  async start(ctx: ExtensionContext): Promise<void> {
    if (!this.root(ctx)) return;
    if (this.context) {
      this.stop(this.context);
      for (const pending of this.pending.values()) await this.release(pending, "unknown", "session replaced before correlated ACK", ctx);
    }
    this.epoch++;
    this.sessionRef = ctx.sessionManager.getSessionFile();
    this.context = ctx;
    this.registration = undefined; this.registering = undefined;
    this.pending.clear();
    this.registrationFailures = 0;
    this.active = this.continuation = this.retrying = this.failureBlocked = this.compacting = false;
    this.recheckAfterWork = undefined;
    this.blocked = this.preparing = 0; this.retryHoldUntil = 0;
    this.lastKey = Date.now(); this.inputSerial++;
    this.refreshNeeded = true;
    if (ctx.mode === "tui" && ctx.hasUI) {
      this.unsubscribe = ctx.ui.onTerminalInput(data => {
        // Focus is not a wake-defer rule. Actual typing/paste still resets quiet time.
        if (data !== "\x1b[I" && data !== "\x1b[O") { this.lastKey = Date.now(); this.inputSerial++; this.schedule(ctx); }
        return undefined;
      });
    }
    this.safety = ctx.setInterval(async () => { this.refreshNeeded = true; await this.kick(ctx); }, 300000);
    await this.register(ctx);
    this.schedule(ctx);
  }
  private async register(ctx: ExtensionContext): Promise<Registration | undefined> {
    if (!this.belongs(ctx)) return undefined;
    if (this.registering) return this.registering;
    if (!this.refreshNeeded && this.current(ctx)) return this.registration;
    const session = ctx.sessionManager.getSessionFile();
    if (!session || !path.isAbsolute(session)) return undefined;
    const epoch = this.epoch;
    const operation = (async () => {
      try {
        const result = await this.call("register", { session_ref: session, client: "omp" });
        if (epoch !== this.epoch || !this.belongs(ctx) || ctx.sessionManager.getSessionFile() !== session) return undefined;
        this.registration = result; this.refreshNeeded = false; this.lastError = ""; this.registrationFailures = 0;
        ctx.ui.setStatus("ham-mailbox", undefined);
        for (const claim of result.pending_claims || []) {
          if (!this.pending.has(claim.claim_id)) this.pending.set(claim.claim_id, { ...claim, phase: "unknown" });
        }
        if (!this.watcher) {
          try {
            this.watcher = watch(result.bell_directory, (_event, filename) => {
              if (epoch !== this.epoch) return;
              // _board is for the Mailbox TUI, not a broadcast to every receiver.
              // Nameless events still conservatively recover a missed own-bell event.
              if (!filename || String(filename) === result.bell_name) this.schedule(ctx);
            });
            this.watcher.on("error", error => { this.report(ctx, error); this.watcher?.close(); this.watcher = undefined; });
          } catch (error) { this.report(ctx, error); }
        }
        return result;
      } catch (error) {
        if (epoch === this.epoch && this.belongs(ctx)) {
          this.registrationFailures++;
          if (error instanceof Error && "code" in error && error.code === "native_session_pending" &&
              this.registrationFailures < 10) {
            ctx.ui.setStatus("ham-mailbox", "HAM: waiting for native session");
          } else this.report(ctx, error);
          if (this.registrationFailures < 10) this.schedule(ctx, Math.min(1000, 250 * this.registrationFailures));
        }
        return undefined;
      }
    })();
    this.registering = operation;
    try { return await operation; } finally { if (this.registering === operation) this.registering = undefined; }
  }
  private async release(pending: Pending, disposition: "unknown" | "not_applied", evidence: string, ctx: ExtensionContext): Promise<void> {
    pending.phase = "unknown";
    try {
      await this.call("release", { claim_id: pending.claim_id, agent_id: pending.agent_id,
        generation: pending.generation, disposition, evidence });
      if (disposition === "not_applied") this.pending.delete(pending.claim_id);
    } catch (error) { this.report(ctx, error); }
  }
  async confirm(ctx: ExtensionContext): Promise<void> {
    if (!this.root(ctx) || this.acknowledging || !this.current(ctx)) return;
    this.acknowledging = true;
    try {
      for (const pending of [...this.pending.values()]) {
        const entry = acceptedEntry(ctx, pending);
        if (!entry) continue;
        try {
          await this.call("applied", { claim_id: pending.claim_id, agent_id: pending.agent_id,
            generation: pending.generation, session_ref: pending.session_ref, marker: pending.marker,
            content_sha256: pending.content_sha256, entry_id: entry });
          this.pending.delete(pending.claim_id);
        } catch (error) { this.report(ctx, error); }
      }
    } finally { this.acknowledging = false; }
  }
  async natural(event: { prompt: string }, ctx: ExtensionContext): Promise<{ message: NativeMessage } | undefined> {
    if (!this.belongs(ctx)) return undefined;
    const epoch = this.epoch, serial = this.inputSerial;
    const preparation = serial + ":" + sha256(String(event.prompt || ""));
    this.preparing++;
    try {
      // User-containing preparation wins a race with a previously started wake check.
      const work = this.work;
      if (work?.epoch === epoch) await work.operation;
      this.refreshNeeded = true;
      const registration = await this.register(ctx);
      if (!registration || !this.current(ctx, epoch) || serial !== this.inputSerial) return undefined;
      await this.confirm(ctx);
      for (const pending of this.pending.values()) {
        if (pending.phase === "prepared" && pending.preparation === preparation && pending.message) return { message: pending.message };
        if (pending.phase === "prepared") await this.release(pending, "unknown", "different user submission after unconfirmed preparation", ctx);
      }
      const claim = await this.call("claim", { ...identity(registration.agent), wake: false });
      if (claim.claim_id === null) return undefined;
      const pending: Pending = { ...claim, phase: "prepared", preparation };
      this.pending.set(claim.claim_id, pending);
      if (!this.current(ctx, epoch) || serial !== this.inputSerial) {
        await this.release(pending, "not_applied", "receiver did not return context after input/session changed", ctx);
        return undefined;
      }
      this.schedule(ctx);
      return { message: claim.message };
    } catch (error) { if (epoch === this.epoch) this.report(ctx, error); return undefined; }
    finally { if (epoch === this.epoch) this.preparing--; }
  }
  private quiet(ctx: ExtensionContext, thresholds: Thresholds): boolean {
    return ctx.mode === "tui" && ctx.hasUI && ctx.ui.supportsEditor === true &&
      typeof ctx.ui.getEditorText === "function" && ctx.ui.getEditorText() === "" &&
      Date.now() - this.lastKey >= thresholds.idle_minutes * 60000;
  }
  private idle(ctx: ExtensionContext): boolean {
    return !this.active && !this.continuation && !this.retrying && !this.failureBlocked && !this.compacting &&
      !this.blocked && !this.preparing && Date.now() >= this.retryHoldUntil && ctx.isIdle() && !ctx.hasPendingMessages();
  }
  private async probe(ctx: ExtensionContext): Promise<void> {
    const epoch = this.epoch;
    const registration = await this.register(ctx);
    if (!registration || !this.current(ctx, epoch)) return;
    await this.confirm(ctx);
    const state = await this.call("inspect", identity(registration.agent));
    if (!this.current(ctx, epoch)) return;
    const remaining = new Set(state.pending_claims.map(claim => claim.claim_id));
    for (const [id, pending] of this.pending) if (pending.phase === "unknown" && !remaining.has(id)) this.pending.delete(id);
    if (!this.idle(ctx)) { if (!this.active && !this.retrying && !this.failureBlocked) this.schedule(ctx, 1000); return; }
    // A settled but unpublished preparation is uncertain, never automatic replay.
    for (const pending of this.pending.values()) if (pending.phase !== "unknown") await this.release(pending, "unknown", "settled without accepted active-branch context", ctx);
    if (this.pending.size || !state.has_action || state.role === null) return;
    if (!this.quiet(ctx, state.thresholds)) {
      if (ctx.mode === "tui") this.schedule(ctx, Math.max(1000, this.lastKey + state.thresholds.idle_minutes * 60000 - Date.now()));
      return;
    }
    if (!Object.hasOwn(WAKE_READY_STATES, state.turn_state)) { this.refreshNeeded = true; this.schedule(ctx, 1000); return; }
    if (state.wake_count >= state.thresholds.wakes_per_hour) { this.schedule(ctx, 300000); return; }
    if (state.coalesce_until * 1000 > Date.now()) { this.schedule(ctx, state.coalesce_until * 1000 - Date.now()); return; }
    const serial = this.inputSerial;
    const claim = await this.call("claim", { ...identity(registration.agent), wake: true });
    if (claim.claim_id === null) return;
    const pending: Pending = { ...claim, phase: "prepared" };
    this.pending.set(claim.claim_id, pending);
    // No await may separate this final public UI/idle/session check and sendMessage.
    if (!this.current(ctx, epoch) || serial !== this.inputSerial || !this.idle(ctx) || !this.quiet(ctx, state.thresholds)) {
      await this.release(pending, "not_applied", "wake never submitted after final user/idle/session gate changed", ctx);
      return;
    }
    pending.phase = "sent";
    try { this.pi.sendMessage(claim.message, { deliverAs: "nextTurn", triggerTurn: true }); }
    catch (error) { await this.release(pending, "unknown", "sendMessage acceptance uncertain: " + String(error), ctx); }
    this.schedule(ctx);
  }
  async kick(ctx: ExtensionContext): Promise<void> {
    if (!this.belongs(ctx)) return;
    const epoch = this.epoch;
    if (this.work?.epoch === epoch) { this.recheckAfterWork = epoch; return; }
    const operation = this.probe(ctx).catch(error => this.report(ctx, error));
    const work = { epoch, operation };
    this.work = work;
    try { await operation; }
    finally {
      if (this.work === work) this.work = undefined;
      const current = this.context;
      if (current && epoch !== this.epoch) this.schedule(current, 0);
      else if (current && this.recheckAfterWork === epoch) {
        this.recheckAfterWork = undefined;
        this.schedule(current, 0);
      }
    }
  }
  input(ctx: ExtensionContext): void {
    if (!this.belongs(ctx)) return;
    this.lastKey = Date.now(); this.inputSerial++; this.failureBlocked = false;
    this.schedule(ctx);
  }
  agentStart(ctx: ExtensionContext): void {
    if (!this.belongs(ctx)) return;
    this.active = true; this.continuation = false; this.failureBlocked = false;
    this.schedule(ctx);
  }
  agentEnd(event: EndEvent, ctx: ExtensionContext): void {
    if (!this.belongs(ctx)) return;
    if (this.active) {
      if (event.willContinue === true) this.continuation = true;
      else {
        this.active = false; this.continuation = false;
        let assistant: { role: string; stopReason?: string } | undefined;
        for (let index = (event.messages?.length ?? 0) - 1; index >= 0; index--) {
          const message = event.messages?.[index];
          if (message?.role === "assistant") { assistant = message; break; }
        }
        if (assistant?.stopReason === "error") {
          this.failureBlocked = true;
          this.retryHoldUntil = Date.now() + 2500;
        }
      }
    }
    this.refreshNeeded = true; this.schedule(ctx);
  }
  retry(start: boolean, event: { delayMs?: number; success?: boolean }, ctx: ExtensionContext): void {
    if (!this.belongs(ctx)) return;
    this.retrying = start;
    if (start) this.retryHoldUntil = Date.now() + Math.max(2500, event.delayMs || 0);
    else { this.failureBlocked = event.success !== true; if (event.success) this.retryHoldUntil = 0; }
    this.refreshNeeded = true; this.schedule(ctx);
  }
  tool(start: boolean, event: { toolName: string }, ctx: ExtensionContext): void {
    if (!this.belongs(ctx) || event.toolName !== "ask") return;
    this.blocked = Math.max(0, this.blocked + (start ? 1 : -1)); this.schedule(ctx);
  }
  compact(start: boolean, event: { type: string; willRetry?: boolean }, ctx: ExtensionContext): void {
    if (!this.belongs(ctx)) return;
    this.compacting = start;
    if (!start && event.willRetry) this.continuation = true;
    this.schedule(ctx);
  }
  async shutdown(ctx: ExtensionContext): Promise<void> {
    if (!this.belongs(ctx)) return;
    this.stop(ctx);
    await this.confirm(ctx);
    for (const pending of this.pending.values()) await this.release(pending, "unknown", "receiver shutdown without correlated ACK", ctx);
    this.epoch++; this.sessionRef = undefined; this.context = undefined; this.registration = undefined; this.pending.clear();
  }
}

export default function (pi: ExtensionAPI): void {
  const adapter = new MailboxAdapter(pi);
  pi.on("session_start", (_event, ctx) => adapter.start(ctx));
  pi.on("session_switch", (_event, ctx) => adapter.start(ctx));
  pi.on("session_before_switch", (_event, ctx) => adapter.confirm(ctx));
  pi.on("input", (event, ctx) => { if (event.source !== "extension") adapter.input(ctx); });
  pi.on("before_agent_start", (event, ctx) => adapter.natural(event, ctx));
  pi.on("agent_start", (_event, ctx) => adapter.agentStart(ctx));
  pi.on("agent_end", (event, ctx) => adapter.agentEnd(event, ctx));
  pi.on("message_end", (_event, ctx) => adapter.schedule(ctx));
  pi.on("turn_start", (_event, ctx) => adapter.schedule(ctx));
  pi.on("turn_end", (_event, ctx) => adapter.schedule(ctx));
  pi.on("auto_retry_start", (event, ctx) => adapter.retry(true, event, ctx));
  pi.on("auto_retry_end", (event, ctx) => adapter.retry(false, event, ctx));
  pi.on("tool_execution_start", (event, ctx) => adapter.tool(true, event, ctx));
  pi.on("tool_execution_end", (event, ctx) => adapter.tool(false, event, ctx));
  pi.on("auto_compaction_start", (event, ctx) => adapter.compact(true, event, ctx));
  pi.on("auto_compaction_end", (event, ctx) => adapter.compact(false, event, ctx));
  pi.on("session_shutdown", (_event, ctx) => adapter.shutdown(ctx));
}
