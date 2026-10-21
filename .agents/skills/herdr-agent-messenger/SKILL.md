---
name: herdr-agent-messenger
description: Use HAM (Herdr Agent Messenger) for durable cross-repository agent mail, inbox handling, status/roles, installation/updates, and explicitly requested labeled Herdr sessions. Trigger on HAM, Herdr delegation, or host/label addresses; supports Codex, Claude, and omp.
---

# HAM — Herdr Agent Messenger

## Choose the operation

- **Existing collaborator:** inspect `repo show` / `status board`, copy the exact address, then `mail send`; no steering. Mail crosses repos on local/registered hosts. `main` uses the sender's anchor role group, including linked workspaces; `[HOST/]main@REPO` selects a repo's unique main across distinct groups (default local; exact ID/unique path suffix). Do not guess on `repo_ambiguous`, `main_ambiguous`, `main_unresolved`, `agent_ambiguous`; reference **Recipient selectors**.
- **New session, only when asked:** use one `spawn --message TEXT` or `--message-file PATH`, never a second legacy request. Missing labels are errors, not permission to guess/spawn. Raw `herdr agent start` bypasses HAM records.
- **Receive/finish work:** read relevant mail at work boundaries, perform it, then `inbox handle --message UUID`. Reads are view-only, never ACKs. Reply with `mail send --reply-to UUID`: choose `--kind action` when the recipient must act/continue (e.g. on an answer/decision), `fyi` otherwise; receipts need no reply. No ACK/polling loops. Native bodies need no redundant pull unless truncated/context is missing.
- **Several collaborators:** verify recipients; send tailored, non-overlapping assignments. HAM neither splits work nor verifies answers.
- **Legacy:** `request` / `send` / `batch` only where permitted. Participating manual/auto same-repo pairs get `use_mailbox`; never bypass it. Batch preflights all targets before submission.

Use the absolute bundled/copied `scripts/herdr_agent_messenger.py` from caller cwd; its pinned-root `-m ham --cwd CALLER cli` preserves local spawn defaults and relative file/destination paths. No reconstructed SSH. Operations verify stable terminal/session identity, not inherited pane IDs alone. Copy addresses/IDs; never guess labels or edit opaque handles. Legacy hosts: literal `local` or configured SSH aliases; mail also accepts registered canonical hosts. Details: reference **Recipient selectors**.

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

CLI `--kind` defaults to `fyi`; exactly one of `--body` / `--body-file` (nonblank UTF-8, ≤16 KiB). Action wakes promptly; FYI wakes idle participating OMP in batches after ~5–10 minutes from local enqueue, or right after a turn if enqueued during fresh `working` state; existing wake gates apply. TUI compose defaults to `action`; Ctrl+G toggles action/FYI, resetting on every new compose. Sending stores/routes mail, not a turn. Queued/pending is not delivery or completed work. Explicit/atomic handling needs no delivered timestamp for pull mail; replies handle their addressed queued/delivered parent.

`status set` takes a **positional phase**, not `--phase`; it works even off. Herdr owns turn state; do not infer it from phase. `status clear` removes phase/summary, retaining alias/active role and manual user borders.

`status board`: all local repos + registered peers by default; bare `--repo` selects caller repo, `--repo ID` filters, `--host local|CANONICAL|REGISTERED_SSH_ALIAS` selects one host. No registry/status/claim refresh. Inspect host errors/stale/unread even on exit 0. Older-peer omissions mean unknown, not zero.

History: `mail threads --repo REPO_ID` (default caller repo) includes participating threads. `mail direct-list` remains direct user-input history. Both accept `--limit N`, `--cursor TOKEN`; reference **History and cursor paging**. All commands support `-h`; domain errors exit 1, usage errors 2.

## Modes, receiving, and truth boundaries

Only the **user** changes mode/main/members/thresholds in Settings. `repo show` is read-only. Newly registered repos default to `auto`; existing settings survive, missing mode stays `off`, and reads never seed defaults. `off` disables roles/wakes/participating legacy rejection, not reads or natural receipt.

Mode/home/thresholds are repo-wide; roles use `(repo_id, canonical host, anchor_workspace_id)`. Linked workspaces join a deterministic open nonlinked workspace with matching Herdr `worktree.repo_key` on the same host. Missing metadata/anchor => own scope; no git-common-dir fallback. Anchor main/members apply; linked settings stay unchanged, ignored until ungrouped. `manual`: active group explicit main + chosen workers except main; nonmembers cannot wake. `auto`: active explicit main wins; otherwise an active anchor agent is required, then sole group agent or unique active main-checkout agent. Empty anchor => no auto main even with one linked agent; others remain workers. `repo show`: `workspace_id`=anchor, `requested_workspace_id`=original. Inactive/out-of-group targets are ignored, not rewritten; home never elects mains. Reference **Modes, roles, settings and status**; **Workspace role migration**.

Cross-repo wakes retain recipient repo settings/role and all gates. Occupancy holds compare the current recipient repo to its send-time delivery snapshot, not thread origin. Replies require participation. Upgrade hosts together: old receivers reject v8; failed outbox mail stays for manual retry after upgrade, not new sends.

Schema v9 adds open-workspace metadata, including no-agent anchors, without moving existing settings. Protocol 1 stays unchanged; optional scope export metadata lets old peers consume computed roles. Old code cannot open v9 storage: upgrade/restart all host HAM processes and copied skills together; downgrade needs the backed-up database. OMP extension/Claude hook contracts are unchanged. Reference **Workspace role migration**.

Schema v10 adds local `deliveries.fyi_turn_end` (default 0); existing v9 deliveries stay unflagged. Queued FYI becomes wake-eligible 300s after local enqueue without migration. Package cutover: all five hosts together, protocol 1 unchanged; refresh skills, reinstall enabled OMP/Claude runtimes, restart receiver/hook sessions and HAM workers/TUIs. Mixed peer paths fail; retry outbox after upgrade. Reference **Receiving runtimes**.

- **OMP:** opt-in root receiver; action or eligible FYI wake needs Herdr `turn_state` of `idle` or `done`, empty supported TUI editor, quiet/idle input, no pending work, eligible membership and rate/hop guards. Uses `nextTurn` + `triggerTurn`, never steering. Both kinds can arrive naturally even off. ACK proves same-session active-branch journal acceptance, not provider success.
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
