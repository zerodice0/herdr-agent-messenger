---
name: herdr-agent-messenger
description: Use HAM (Herdr Agent Messenger) for durable same-repository agent mail, inbox handling, status/roles, installation/updates, and explicitly requested labeled Herdr sessions. Trigger on HAM, Herdr delegation, or host/label addresses; supports Codex, Claude, and omp.
---

# HAM — Herdr Agent Messenger

## Choose the operation

- **Existing collaborator:** inspect `repo show` and `list`, copy the exact address, then `mail send`; no ongoing-turn steering. `--to main` stores the sender's current host-qualified workspace main as a concrete target; no repo-home/other-workspace fallback.
- **New session, only when asked:** use one `spawn --message TEXT` or `--message-file PATH`, never a second legacy request. Missing labels are errors, not permission to guess/spawn. Raw `herdr agent start` bypasses HAM records.
- **Receive/finish work:** read relevant mail at work boundaries, perform it, then `inbox handle --message UUID`. Reads are view-only, never ACKs. Reply usefully with `mail send --reply-to UUID`; receipts need no reply. No ACK/polling loops. Native bodies need no redundant pull unless truncated/context is missing.
- **Several collaborators:** verify recipients; send tailored, non-overlapping assignments. HAM neither splits work nor verifies answers.
- **Legacy:** `request` / `send` / `batch` only where permitted. Participating manual/auto same-repo pairs get `use_mailbox`; never bypass it. Batch preflights all targets before submission.

Use bundled `scripts/herdr_agent_messenger.py` relative to this skill directory, or its absolute path, not reconstructed SSH. Operations verify stable terminal/session identity, not inherited pane IDs alone. Copy addresses/IDs; never guess labels or edit opaque handles. Legacy hosts: literal `local` or configured SSH aliases; mail also accepts registered canonical hosts. Details: reference **Recipient selectors**.

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
python3 scripts/herdr_agent_messenger.py status board --repo --host local
python3 scripts/herdr_agent_messenger.py status set review --summary 'Reviewing mailbox boundaries'
```

`--kind` defaults to `fyi`; exactly one of `--body` / `--body-file` (nonblank UTF-8, ≤16 KiB). Only eligible OMP `action` can start a later turn; FYI alone cannot. Sending stores/routes mail, not a turn. Queued/pending is not delivery or completed work. Explicit/atomic handling needs no delivered timestamp for pull mail; replies handle their addressed queued/delivered parent.

`status set` takes a **positional phase**, not `--phase`; it works even off. Herdr owns turn state; do not infer it from phase. `status clear` removes phase/summary, retaining alias/active role and manual user borders.

`status board`: all local repos + registered peers by default; bare `--repo` selects caller repo, `--repo ID` filters, `--host local|CANONICAL|REGISTERED_SSH_ALIAS` selects one host. No registry/status/claim refresh. Inspect host errors/stale/unread even on exit 0. Older-peer omissions mean unknown, not zero.

History: `mail threads` / `mail direct-list` accept `--repo REPO_ID`, `--limit N`, `--cursor TOKEN`; reference **History and cursor paging**. Board fields: **Status board output and fields**. All commands accept `-h` / `--help`; domain errors exit 1, usage errors exit 2.

## Modes, receiving, and truth boundaries

Only the **user** changes mode/main/members/thresholds in Settings. `repo show` is read-only. Newly registered repos default to `auto`; existing settings survive, missing mode stays `off`, and reads never seed defaults. `off` disables roles/wakes/participating legacy rejection, not reads or natural receipt.

Mode/thresholds are repo-wide; roles are scoped by `(repo_id, canonical host, workspace_id)`, including independent worktree workspaces. `manual`: active in-scope explicit main + selected workers, excluding main; nonmembers cannot wake. `auto`: active explicit main, else sole active agent, else exactly one active main-checkout agent; otherwise no main. Others remain workers. Inactive/moved-out targets are ignored, not rewritten; home does not choose mains. Details: reference **Modes, roles, settings and status**; internals: **Workspace role migration**.

- **OMP:** opt-in root receiver; action wake needs empty supported TUI editor, quiet/idle input, no pending work, eligible membership and rate/hop guards. Uses `nextTurn` + `triggerTurn`, never steering. Both kinds can arrive naturally even off. ACK proves same-session active-branch journal acceptance, not provider success.
- **Claude Code:** opt-in bounded `SessionStart` / `UserPromptSubmit`, no idle/Stop wake. ACK needs exact SDK context + fresh request correlation or typed hook bytes/hash + causal journal witness. The latter proves host-context application, not provider-wire bytes/understanding. Stdout alone is not delivery. Limits: reference **Receiving runtimes**.
- **Codex:** skill-guided pull at work boundaries, then explicit handling; no automatic receiver/wake or invented delivered receipt.

Native bundles retain IDs/sender/kind/thread/hop; bodies may truncate. Marker/hash proves claimed content, not work. Reference **Native mailbox bundles**.

Never replay uncertain `delivering` after crashes/session changes. User evidence-based Threads audit is operator resolution, not native ACK/handled work. User direct input may interrupt; terminal acceptance is separate from mailbox delivery. Controls/recovery: reference **Mailbox TUI and operator recovery**.

## Device claims

Same launcher: `device claim phone|emu DEVICE [--label TEXT]` before use; `device release [phone|emu|all] [DEVICE]` afterward; `device list` to inspect. Release only your claims. `device_claimed`: stop; claim `--force` only on explicit user instruction. Servers are discovered, not claimed; heavy slots are separate. Schemas: reference **Device claims and resource fields**.

## Spawn with first prompt

The cwd must exist on the selected host; message files are read on the caller's host. HAM flags precede `--` agent arguments.

```bash
python3 scripts/herdr_agent_messenger.py spawn \
  --kind omp --cwd /path/to/project --placement tab \
  --message 'Review the project and report concrete blockers through HAM mail.'
```

`--message-file PATH` reads an assignment; no message creates only the session. `initial_prompt.acceptance=accepted|unknown|failed` is terminal acceptance, not delivery/response/understanding. Unknown may already be submitted: retain operation/pane evidence, inspect, never blindly resend/spawn a replacement. Each spawn is a new operation. Flags: reference **`spawn`**.

## Installing or updating HAM

On each selected host, install/update plugin + skill, then run read-only `runtime status`. Explain missing OMP/Claude receivers and **ask consent before** `runtime install omp|claude` (or installer `--runtime`). Never overwrite foreign/modified files; restart only user-selected sessions. Confirm `extension_present=1` or report **installed, activation pending**. Plugin/skill files alone do not prove readiness; Codex pull-only absence is normal. Checklist: reference **Client onboarding and installation**; health/ownership: **Receiving runtimes**.

## Legacy requests where policy permits

```bash
python3 scripts/herdr_agent_messenger.py request \
  --address CONFIGURED_HOST/EXACT_LABEL --message 'Inspect this nonparticipating task.' --timeout 120000
python3 scripts/herdr_agent_messenger.py request-status --request-id REQUEST_ID --wait 120000
```

Keep `request_id`; prefer bounded `request-status --wait` to repeated status/board polling. It stops on state/phase/error change, terminal state or deadline. Unchanged/nonterminal is no resend permission. `deferred`/`awaiting_decision`: HAM owns delivery; never resend/unfocus. Push requires explicit user action. Observe `submitted_working|submitted_unknown`, never replay. Exit 3: nonsettled owned work; exit 1: not proof of non-delivery; settled/exit 0: not proof of correctness. Terminal text is not a structured assistant API. Never bypass SSH host-key checks.
`request-cancel --request-id ID` cancels only your held, unsubmitted request; verifies local sender, never contacts recipient. No CLI push; reference **`request-cancel`**.

`status board|list` defaults to brief rows/counts without duplicate host agents; `--verbose` restores full fields/resources.
`inbox read` defaults to IDs/sender/body/state/reply context; `--verbose` restores delivery diagnostics.
CLI JSON is whitespace-free UTF-8, even verbose. Legacy output is also compact; `--verbose` restores diagnostics. See reference **Compact versus verbose payloads**; read only needed sections, never the whole reference by default.
