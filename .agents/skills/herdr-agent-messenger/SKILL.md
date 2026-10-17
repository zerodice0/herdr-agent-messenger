---
name: herdr-agent-messenger
description: Use HAM (Herdr Agent Messenger) for durable same-repository agent mail, inbox handling, status/roles, and explicitly requested new labeled Herdr sessions. Also supports legacy prompt routing where coordination policy permits. Trigger on HAM, Herdr delegation, or host/label addresses; supports Codex, Claude, and omp.
---

# HAM — Herdr Agent Messenger

## Choose the operation

- **Existing same-repository collaborator:** inspect `repo show` and `list`, copy the exact current address, then use `mail send`. Mail does not steer an ongoing turn. Use `--to main` only when a repository home/main has been configured by the user.
- **New session, only when asked:** `spawn --message TEXT` or `spawn --message-file PATH` submits the first prompt once after readiness. Do not spawn and then send a separate legacy request for that first prompt. A missing existing label is an error, not permission to create a session or guess another label. Raw `herdr agent start` bypasses HAM labeling and initial-submission records.
- **Receive or finish work:** `inbox read` is view-only. At a useful work boundary, inspect relevant messages, perform the work, then explicitly `inbox handle --message UUID`. A useful reply uses `mail send --reply-to UUID`; a receipt alone needs no reply. Do not create acknowledgement loops or token-spending polling loops.
- **Several collaborators:** verify each recipient and assign non-overlapping work with separate tailored `mail send` operations. HAM stores messages; it does not split tasks or verify answers.
- **Nonparticipating legacy traffic:** `request` / `send` / `batch` remain available where policy permits. In manual/auto, traffic between participating agents in the same repository is refused with `use_mailbox`; do not bypass it. Batch preflights every target before any submission. A failed lookup/refusal is not permission to submit to the other targets.

Use the bundled CLI, not reconstructed SSH commands. Its path is `scripts/herdr_agent_messenger.py` **relative to this skill directory**; run there or use its absolute path. Agent operations run from the current Herdr agent terminal. Stable terminal/session identity, not an inherited pane ID alone, determines the sender after pane moves. Labels are host-scoped and must not be guessed. Only literal `local` selects local legacy transport; remote legacy hosts are concrete configured SSH aliases. Mail also accepts registered canonical host names or their SSH aliases.

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
python3 scripts/herdr_agent_messenger.py status set review --summary 'Reviewing mailbox boundaries'
python3 scripts/herdr_agent_messenger.py status list --repo
python3 scripts/herdr_agent_messenger.py status clear
```

`--kind` defaults to `fyi`; exactly one of `--body` / `--body-file` is required (nonblank UTF-8, at most 16 KiB). `action` can make a participating, eligible OMP receiver start a later turn; `fyi` alone never starts one. The sender command itself only stores/routes mail. `inbox handle --message` is repeatable and atomic. Reads, including subagent reads in the same pane, never acknowledge anything. Pull handling can record `handled` without any `delivered_at`. A reply atomically handles its addressed queued/delivered parent; do not claim unrelated messages handled.

`status set` takes a **positional phase**, not `--phase`. Summary/phase reporting works even in off mode. Herdr owns turn state; do not invent it from phase text.

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

Without a message flag, spawn only creates the session. With one, the immutable target/body and submission operation are persisted before transport. `initial_prompt.acceptance` is `accepted`, `unknown`, or `failed`; terminal acceptance is **not** native mailbox delivery, a response, or model understanding. Unknown/unfinished acceptance may already have entered the session: preserve the returned operation/submission evidence and usable pane, inspect rather than resending or creating a replacement. Running another spawn is a new invocation, not a safe retry of the old operation.

## Modes, receiving, and truth boundaries

Only the **user** changes mode/main/manual members/thresholds in HAM Settings. `repo show` is read-only. Default `off` disables roles, automatic wakes and participating legacy rejection, **not** explicit reads or natural user-turn receipt. In `manual`, the user designates main and workers; nonmembers can receive naturally without waking. In `auto`, an explicit main wins; otherwise exactly one active main-checkout agent on the home host becomes main. Zero or multiple candidates means no default main; the other same-repository agents across registered hosts are workers. Home is fixed by initial activation/concurrent proposal convergence and displayed read-only, not an agent-editable field.

- **OMP:** separately opt-in receiver, Herdr root session only. Automatic action wake requires supported TUI empty-editor evidence, input quiet time, idle/no pending work, membership and configured coalescing/hour/hop limits. It uses agent-attributed `nextTurn` + `triggerTurn`, never steering. FYI and action can enter naturally at a valid turn boundary even off. Native ACK means exact same-session active-branch journal acceptance, not a later provider request or successful processing.
- **Claude Code:** separately opt-in bounded `SessionStart` / `UserPromptSubmit` hooks; no idle wake or Stop extension. Hook stdout/exit success alone is not delivery. Native confirmation uses exact same-session SDK context plus fresh request correlation, or typed hook raw/rendered claim bytes/hash plus a causal native request witness. The latter proves host-context application, not exact provider-wire bytes, provider attestation or model understanding. Unresolved binding leaves mail queued; unknown acceptance remains held. A bounded actual TUI run confirmed current-generation SessionStart receiving and registered manual-worker/auto-main no-wake windows; it did not exercise unreported-identity bootstrap. Auxiliary provider traffic and processing outcomes are separate; see reference for scope.
- **Codex:** skill-guided pull at work boundaries, view-only `inbox read` followed by explicit handling. No automatic wake/receiver and no invented delivered receipt.

Never automatically replay an uncertain `delivering` claim after a crash/session change. The user can inspect the old claim/agent/generation and evidence in Threads, then explicitly confirm applied/not-applied. That is **operator** evidence, not native ACK or handled work. User TUI direct input is a separate, potentially interrupting `Message from user (HAM):` submission, recorded as direct acceptance rather than mailbox delivery.

## Legacy requests where policy permits

```bash
python3 scripts/herdr_agent_messenger.py request \
  --address CONFIGURED_HOST/EXACT_LABEL --message 'Inspect this nonparticipating task.' --timeout 120000
python3 scripts/herdr_agent_messenger.py request-status --request-id REQUEST_ID --wait 120000
python3 scripts/herdr_agent_messenger.py read --address CONFIGURED_HOST/EXACT_LABEL --lines 160 --max-bytes 65536
```

Keep the original `request_id`. Focus-held `deferred` / `awaiting_decision` means HAM owns delivery, not permission to resend or ask the user to unfocus. Held Messages lets the user Push/Cancel after the default 180000 ms deadline; no automatic push. `submitted_working` / `submitted_unknown` likewise require observation, never blind resend. Self-send fails immediately. Exit `3` means nonsettled HAM-owned work; exit `1` does not prove non-delivery; usage errors exit `2`. Exit `0` or `submitted_settled` is not proof of answer correctness. Review actual evidence; terminal text/correlation/truncation is not a structured assistant-message API. Never bypass SSH host-key checks.

Read `reference.md` for complete flags, legacy batch/results, host setup, per-host runtime install/status/uninstall, TUI controls, durable worker/recovery and deployment order. Skill installation does not install a receiving runtime or change global bindings.
