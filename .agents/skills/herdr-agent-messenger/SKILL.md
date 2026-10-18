---
name: herdr-agent-messenger
description: Use HAM (Herdr Agent Messenger) for durable same-repository agent mail, inbox handling, status/roles, and explicitly requested new labeled Herdr sessions. Also supports legacy prompt routing where coordination policy permits. Trigger on HAM, Herdr delegation, or host/label addresses; supports Codex, Claude, and omp.
---

# HAM — Herdr Agent Messenger

## Choose the operation

- **Existing same-repository collaborator:** inspect `repo show` and `list`, copy the exact address, then `mail send`. Mail does not steer an ongoing turn. `--to main` needs user-configured home/main.
- **New session, only when asked:** one `spawn --message TEXT` or `spawn --message-file PATH` submits the first prompt after readiness; never follow with a separate legacy request. Missing labels are errors, not permission to guess/spawn. Raw `herdr agent start` bypasses HAM records.
- **Receive or finish work:** `inbox read` is view-only. At a useful work boundary, inspect relevant messages, perform the work, then explicitly `inbox handle --message UUID`. A useful reply uses `mail send --reply-to UUID`; a receipt alone needs no reply. Do not create acknowledgement loops or token-spending polling loops.
- **Several collaborators:** verify each recipient and assign non-overlapping work with separate tailored `mail send` operations. HAM stores messages; it does not split tasks or verify answers.
- **Nonparticipating legacy traffic:** `request` / `send` / `batch` remain available where permitted. Manual/auto participating same-repo pairs get `use_mailbox`; never bypass it. Batch preflights all targets; any lookup/refusal prevents all submissions.

Use bundled `scripts/herdr_agent_messenger.py` relative to this skill directory, or its absolute path, not reconstructed SSH. Agent operations use verified stable terminal/session identity after pane moves, not an inherited pane ID alone. Never guess labels. Legacy transport uses literal `local` or concrete configured SSH aliases; mail also accepts registered canonical hosts.

## Mailbox and status commands

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
python3 scripts/herdr_agent_messenger.py mail show --thread THREAD_UUID
python3 scripts/herdr_agent_messenger.py mail withdraw --message MESSAGE_UUID
python3 scripts/herdr_agent_messenger.py mail threads --limit 100
python3 scripts/herdr_agent_messenger.py mail direct-list --repo 'git:host/stable/repo'
python3 scripts/herdr_agent_messenger.py status board --repo --host local
python3 scripts/herdr_agent_messenger.py status set review --summary 'Reviewing mailbox boundaries'
python3 scripts/herdr_agent_messenger.py status list --repo
python3 scripts/herdr_agent_messenger.py status clear
```

`--kind` defaults to `fyi`; use exactly one of `--body` / `--body-file` (nonblank UTF-8, ≤16 KiB). Only eligible OMP `action` can start a later turn; FYI alone cannot. Sending stores/routes mail, never starts a turn itself. Local mail normally waits queued; remote pending, withdrawal and terminal-closed final results are also possible. Handling is explicit/atomic; reads never ACK. Pull handling needs no delivered timestamp; replies handle their addressed queued/delivered parent. Repeated successful withdrawal stays `withdrawn:true`; a winning claim/other final result returns false.

`status set` takes a **positional phase**, not `--phase`. Summary/phase reporting works even in off mode. Herdr owns turn state; do not invent it from phase text.

### Read-only history and board

`mail threads` and `mail direct-list` accept `[--repo REPO_ID] [--limit N] [--cursor TOKEN]`. Default is the verified caller's repo; explicit ID avoids Herdr caller-identity fetch. Limit100, range1–1000. Results are `{repo_id,threads|submissions,next_cursor}`; `null` ends traversal. They cover all participants' retained local-ledger sent/received records, including pending remote thread copies, not replicated all-host history. Direct-list reads user-direct evidence, never sends input.

Pass HMAC-signed opaque cursors unchanged: bound to ledger/query/repo, invalid/tampered/wrong-scope tokens give `invalid_cursor`. Live keyset pages sort `updated_at DESC`, UUID ascending, not OFFSET or a frozen snapshot. Updates can move unseen rows before the boundary; restart the first page to see newer updates.

`status board [--repo [ID]] [--host local|CANONICAL|REGISTERED_SSH_ALIAS]` defaults to all local repos + registered peers. Bare `--repo` selects the caller's repo; explicit ID filters. Output `{repo_id,agents,hosts}` includes roles/turn/phase/summary/unread, receiver/placement/generation, status_updated_at, last_seen and stale. Status and registry-observation timestamps differ. Reads never refresh registry/status/display/claims; peers get `refresh=false`. Failed hosts have structured error, stale and unread `?`; stateless CLI invents no rows. TUI may retain verified prior rows stale. Existing `list` / local `status list` are narrower.
Older peers may ignore `refresh=false` and perform their legacy refresh. A peer
failure still exits with code 0: inspect `hosts[].error`, stale and unread.
Unknown/ambiguous `--host` returns `peer_not_configured` with exit code 1.

All three accept `-h` / `--help`, no `--verbose`, and emit one JSON object; domain errors use `{"error":{"code":"CODE","message":"TEXT"}}` (exit1), usage errors exit2. No read ACK or public settings/user-compose/direct-send/operator ACK bypass. See reference terminology for inbox, mixed TUI Threads, transport outbox, states and unread (`queued` + `delivering`, not a view flag).

## New labeled session and first prompt

The working directory must exist on the selected host. A message file is read on the caller's host. All HAM flags precede `--` agent arguments.

```bash
python3 scripts/herdr_agent_messenger.py spawn \
  --kind omp --cwd /path/to/project --placement tab \
  --message 'Review the project and report concrete blockers through HAM mail.'
python3 scripts/herdr_agent_messenger.py spawn \
  --kind codex --cwd /path/to/project --placement workspace \
  --message-file /path/to/assignment.txt -- --model MODEL
```

No message flag means session creation only. Otherwise immutable target/body and operation are saved before transport. `initial_prompt.acceptance=accepted|unknown|failed` means terminal acceptance, not native delivery, response or understanding. Unknown/unfinished may already be submitted: preserve operation/pane evidence, inspect, never blindly resend or spawn a replacement. Another spawn is a new operation.

## Modes, receiving, and truth boundaries

Only the **user** changes mode/main/members/thresholds in Settings. `repo show` is read-only. Default `off` disables roles/wakes/participating legacy rejection, not reads or natural receipt. `manual` uses designated main/workers; nonmembers cannot wake. `auto` prefers explicit main; otherwise exactly one active home main-checkout agent becomes main. Zero/multiple means none; other same-repo agents across registered hosts are workers. Home is fixed by activation/concurrent proposal convergence, not agent-editable.

- **OMP:** opt-in root receiver. Action wake needs supported TUI empty editor, quiet/idle input, no pending work, eligible membership and coalescing/hour/hop guards. Uses agent-attributed `nextTurn` + `triggerTurn`, never steering. Both kinds can arrive naturally even off. ACK proves exact same-session active-branch journal acceptance, not provider success.
- **Claude Code:** opt-in bounded `SessionStart` / `UserPromptSubmit`, no idle/Stop wake. Native ACK needs exact SDK context + fresh request correlation (`claude:api-request:`), or typed hook bytes/hash + causal native witness (`claude:accepted-journal:`). The latter proves host-context application, not exact provider-wire bytes, attestation or understanding. Stdout alone is not delivery. Bounded live SessionStart/no-wake observations exclude provider success and cold-unreported binding CAS (unit-only); see reference for limits and safe-launcher notes.
- **Codex:** skill-guided pull at work boundaries, view-only `inbox read` followed by explicit handling. No automatic wake/receiver and no invented delivered receipt.

Never replay uncertain `delivering` after crashes/session changes. Threads lets the user audit the old claim/agent/generation as applied/not-applied with evidence: **operator**, not native ACK or handled work. User direct input may interrupt (`Message from user (HAM):`); its acceptance is separate from mailbox delivery.
The Mailbox opens as a split, tab or quick-send popup (closes after a successful
send). Responsive Agents/Threads/Detail regions replace exclusive screens.
NAV: Tab/Shift+Tab, ←/→ or h/l changes focus; ↑/↓ or j/k selects rows.
Agents Enter opens inline FYI input; Threads Enter replies. Enter sends
immediately (`sender=user`, no subject); Shift+Enter adds a newline using the
requested kitty keyboard protocol through herdr in compatible terminals.
Alt+Enter is the Moshi/mosh fallback; Ctrl+J is not a newline key.
`HAM_MAILBOX_NO_KITTY=1` disables the protocol request. Esc preserves the draft;
Ctrl+D toggles direct input with confirmation. `?` opens NAV help; F1 does so
while typing; `e` shows diagnostics. Settings (`s`) uses the selected Agents row:
`m` sets main, Space toggles membership, each with confirmation.
Threads `w`/`t`/`a`/`n` confirms exact targets; `a`/`n` first requires evidence.
Agents groups by host, marks stale unread `?`, and shows one summary line.
Detail auto-previews full fields and separate status/observation timestamps.
Viewing is read-only and never ACKs mail.

## Legacy requests where policy permits

```bash
python3 scripts/herdr_agent_messenger.py request \
  --address CONFIGURED_HOST/EXACT_LABEL --message 'Inspect this nonparticipating task.' --timeout 120000
python3 scripts/herdr_agent_messenger.py request-status --request-id REQUEST_ID --wait 120000
python3 scripts/herdr_agent_messenger.py read --address CONFIGURED_HOST/EXACT_LABEL --lines 160 --max-bytes 65536
```

Keep `request_id`; `deferred`/`awaiting_decision` means HAM owns delivery, not resend/unfocus. Held Messages offers Push/Cancel after default180000 ms; no auto-push. Observe `submitted_working|submitted_unknown`, never blindly resend. Self-send fails. Exit3 means nonsettled owned work; exit1 does not prove non-delivery; usage exits2. Exit0/settled proves no answer correctness. Terminal text is not a structured assistant-message API. Never bypass SSH host-key checks.

Read `reference.md` for complete flags, legacy batch/results, host setup, per-host runtime install/status/uninstall, TUI controls, durable worker/recovery and deployment order. Skill installation does not install a receiving runtime or change global bindings.
