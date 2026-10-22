---
name: herdr-agent-messenger
description: Use HAM (Herdr Agent Messenger) for durable cross-repository agent mail, inbox handling, status/roles, installation/updates, and explicitly requested labeled Herdr sessions. Trigger on HAM, Herdr delegation, or host/label addresses; supports Codex, Claude, and omp.
---

# HAM — Herdr Agent Messenger

## Choose the operation

- **Existing collaborator:** inspect `repo show` / `status board`, copy the exact address, then `mail send`; no steering. `main` uses the sender's anchor role group; `[HOST/]main@REPO` selects a repo's unique main. Do not guess on `repo_ambiguous`, `main_ambiguous`, `main_unresolved`, `agent_ambiguous`; reference **Recipient selectors**.
- **New session, only when asked:** use one `spawn --message TEXT` or `--message-file PATH`, never a second legacy request. Missing labels are errors, not permission to guess/spawn. Raw `herdr agent start` bypasses HAM records.
- **Receive/finish work:** read relevant mail at work boundaries, perform it, then `inbox handle --message UUID`. Reads are view-only, never ACKs. Reply with `mail send --reply-to UUID`: `--kind action` when the recipient must act/continue (e.g. on an answer/decision), `fyi` otherwise; receipts need no reply. No ACK/polling loops.
- **Several collaborators:** verify recipients; send tailored, non-overlapping assignments. HAM neither splits work nor verifies answers.
- **Legacy:** `request` / `request-status` / `send` / `batch` only where policy permits; participating manual/auto same-repo pairs get `use_mailbox`, never bypass it. Never resend after wait/read/status failures, `submitted_*`, `deferred`/`awaiting_decision`; exit 1 is not proof of non-delivery, settled/exit 0 not proof of correctness. `request-cancel --request-id ID` cancels only your held, unsubmitted request. Reference **Legacy request handling**, **`request-cancel`**.

Use the absolute bundled/copied `scripts/herdr_agent_messenger.py` from caller cwd (`-m ham --cwd CALLER cli` preserves spawn defaults and relative paths). No reconstructed SSH. Operations verify stable terminal/session identity. Copy addresses/IDs; never guess labels or edit opaque handles. Legacy hosts: `local` or configured SSH aliases; mail also accepts registered canonical hosts. Reference **Recipient selectors**.

## Token economy

- **Content first:** decision/next action in the first lines; keep the body ≤ ~2 KB (native bundles cap each body at 2,048 B and truncate the rest); put long evidence in a file and send its path.
- **One mail per recipient per boundary** (results + question + next step); one completion report per task, no progress pings.
- **No ACK-only/thanks replies**: they wake the recipient. FYI also wakes after its delay, so `action`→`fyi` does not avoid a wake.
- **Natively injected mail is already read**: `inbox read --message UUID` only if truncated or context is missing.
- **Handle in one call**: `inbox handle --message A --message B`. A successful `mail send --reply-to UUID` already handles its addressed parent; do not add `handle`.
- `status board` only when coordination needs it; default output is brief.

## Common mailbox and status commands

```bash
python3 scripts/herdr_agent_messenger.py repo show
python3 scripts/herdr_agent_messenger.py list --host local
python3 scripts/herdr_agent_messenger.py mail send \
  --to local/blue-owl --kind action --subject 'Review' \
  --body 'Inspect the boundary and report concrete findings.'
python3 scripts/herdr_agent_messenger.py inbox read
python3 scripts/herdr_agent_messenger.py inbox read --message MESSAGE_UUID
python3 scripts/herdr_agent_messenger.py inbox handle --message MESSAGE_UUID
python3 scripts/herdr_agent_messenger.py mail send \
  --to CANONICAL_HOST/EXACT_LABEL --reply-to MESSAGE_UUID --kind fyi --body 'Findings and evidence.'
python3 scripts/herdr_agent_messenger.py mail withdraw --message MESSAGE_UUID
python3 scripts/herdr_agent_messenger.py mail dismiss --message MESSAGE_UUID --reason 'Recipient closed; no action remains'
python3 scripts/herdr_agent_messenger.py status board --repo --host local
python3 scripts/herdr_agent_messenger.py status set review --summary 'Reviewing mailbox boundaries'
```

CLI `--kind` defaults to `fyi`; exactly one of `--body` / `--body-file` (nonblank UTF-8, ≤16 KiB). Action wakes promptly; FYI wakes idle participating OMP in batches no sooner than 600 s after local enqueue (typically ~10–15 min), or right after a turn if enqueued during fresh `working` state; wake gates apply (coalesce 60 s, 4 wakes/hour, hop ≤10). TUI compose defaults to `action`; Ctrl+G toggles. Sending stores/routes mail, not a turn. Queued/pending is not delivery or completed work. Handling needs no delivered timestamp for pull mail; replies handle their addressed queued/delivered parent.

`mail dismiss` resolves attention for locally originated `undeliverable` mail only; it never means delivered/handled and emits no recipient receipt. Only the verified original terminal or current main of the sender's host-local role group may dismiss (same repo alone grants no authority). Repeat `--message` for an atomic batch; nonblank `--reason` required. Reference **Sender dismissal**.

`status set` takes a **positional phase**, not `--phase`; it works even off. Herdr owns turn state; do not infer it from phase. `status clear` removes phase/summary, retaining alias/active role and manual user borders.

`status board`: all local repos + registered peers by default; bare `--repo` selects caller repo, `--repo ID` filters, `--host local|CANONICAL|REGISTERED_SSH_ALIAS` one host. Inspect host errors/stale/unread even on exit 0; older-peer omissions mean unknown, not zero.

History: `mail threads --repo REPO_ID` (default caller repo) includes participating threads. `mail direct-list` remains direct user-input history. Both accept `--limit N`, `--cursor TOKEN`; reference **History and cursor paging**. All commands support `-h`; domain errors exit 1, usage errors 2.

## Modes, receiving, and truth boundaries

Only the **user** changes mode/main/members/thresholds in Settings. `repo show` is read-only. Newly registered repos default to `auto`; existing settings survive, missing mode stays `off`, and reads never seed defaults. `off` disables roles/wakes/participating legacy rejection, not reads or natural receipt.

Mode/home/thresholds are repo-wide; roles are per `(repo_id, canonical host, anchor_workspace_id)` and linked worktrees use their anchor's main/members. `manual`: explicit main + chosen workers; nonmembers cannot wake. `auto`: explicit main, then valid incumbent, then the sole/unique main-checkout agent; a sibling opening never demotes the incumbent. Cross-repo wakes keep the recipient repo's settings, role and gates; replies require participation. Upgrade all hosts and copied skills together; failed outbox mail stays for manual retry. Reference **Role-election internals**, **Upgrades and schema migrations**, **Modes, roles, settings and status**.

- **OMP:** opt-in root receiver; action or eligible FYI wake needs Herdr `turn_state` `idle`/`done`, empty TUI editor, quiet input, no pending work, eligible membership and rate/hop guards; uses `nextTurn` + `triggerTurn`, never steering. Both kinds can arrive naturally even off. ACK proves journal acceptance, not provider success.
- **Claude Code:** opt-in bounded `SessionStart` / `UserPromptSubmit`, plus a worker-driven idle wake (hook registered, idle/done + quiet, peer socket, transcript evidence). ACK proves host-context application or transcript acceptance, not provider-wire bytes/understanding; stdout alone is not delivery. Reference **Receiving runtimes**, **Native idle wake**.
- **Codex:** worker-driven idle wake (`codex queue`, rollout evidence; no runtime install) plus skill-guided pull at work boundaries, then explicit handling; never an invented delivered receipt. Reference **Native idle wake**.

Native bundles retain IDs/sender/kind/thread/hop; bodies may truncate. Marker/hash proves claimed content, not work. Reference **Native mailbox bundles**.

Never replay uncertain `delivering` after crashes/session changes. User evidence-based Threads audit is operator resolution, not native ACK/handled work. User direct input may interrupt; terminal acceptance is separate from mailbox delivery. Controls/recovery: reference **Mailbox TUI and operator recovery**.

## Device claims

Same launcher: `device claim phone|emu DEVICE [--label TEXT]` before use; `device release [phone|emu|all] [DEVICE]` after; `device list` to inspect. Release only your claims. `device_claimed`: stop; `--force` only on explicit user instruction. Reference **Device claims and resource fields**.

## Spawn with first prompt

The cwd must exist on the selected host; message files are read on the caller's host. HAM flags precede `--` agent arguments.

```bash
python3 scripts/herdr_agent_messenger.py spawn \
  --kind omp --cwd /path/to/project --placement tab \
  --message 'Review the project and report concrete blockers through HAM mail.'
```

`--message-file PATH` reads an assignment; no message creates only the session. `initial_prompt.acceptance=accepted|unknown|failed` is terminal acceptance, not delivery/response/understanding. Unknown may already be submitted: retain operation/pane evidence, inspect, never blindly resend/spawn a replacement. Each spawn is a new operation. Flags: reference **`spawn`**.

## Installing or updating HAM

On each selected host, install/update plugin + skill, run read-only `runtime status`, explain missing OMP/Claude receivers and **ask consent before** `runtime install omp|claude`. Never overwrite foreign/modified files; restart only user-selected sessions; confirm `extension_present=1` or report **installed, activation pending**. Codex needs no runtime install; its absence is normal. Reference **Client onboarding and installation**, **Receiving runtimes**.

`mail send|dismiss`, `inbox read|handle`, `repo show`, `status set|clear|board|list` default to brief output; `--verbose` restores full fields/diagnostics. CLI JSON is whitespace-free UTF-8. Reference **Compact versus verbose payloads**; read only needed sections, never the whole reference.
