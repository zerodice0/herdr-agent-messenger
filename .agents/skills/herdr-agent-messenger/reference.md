# HAM — on-demand reference

Read `SKILL.md` first. This reference covers the complete CLI, diagnostic payloads, recovery, and installation. Paths to `scripts/herdr_agent_messenger.py` are relative to this skill directory; use an absolute path when working elsewhere. Messaging runs from the coordinator's active Herdr agent pane (`HERDR_PANE_ID`).

## Command and flag reference

The eight prompt-routing commands below accept `-h` / `--help` (usage, exit 0) and `--verbose` (full diagnostic JSON instead of compact output; default off). The separate mailbox commands also accept help, but have no verbose flag. Numeric arguments are integers. Optional flags are unset unless a default is listed. Milliseconds are denoted `MS`.

### Recipient selectors

`status`, `send`, `read`, and `request` require **exactly one** of `--address`, `--label`, or `--route`:

| Flag | Meaning / default |
| --- | --- |
| `--address HOST/LABEL` | Exact `address` returned by `list` or `spawn`; carries its own host. |
| `--label LABEL` | Exact current Agent Labels name on `--host`. |
| `--route TOKEN` | Opaque verified GUI/CLI route; carries its own host. Pass unchanged, never decode or edit it. Supports unlabeled GUI selections. |
| `--host HOST` | Default `local`; otherwise a concrete configured SSH alias. An explicit non-default host must agree with `--address` or `conflicting_address` results. For routes use the token's host, not an override. |

Only literal `local` means local transport. Even a hostname matching this machine means SSH. Labels can repeat across hosts and do not guarantee the same occupant after restart. Missing/ambiguous hosts or labels must be reported, not guessed. Never substitute a pane ID or create a missing agent unless asked.

A verified route can refresh only when continuity checks identify one safe occupant. In verbose output, retain a new `route` when `route_refreshed: true`. Compact output intentionally omits routes; use addresses for normal agent workflows.

### Local mailbox

Mailbox messages are durable data, not terminal prompts. Local same-repository
agents can exchange them without interrupting an ongoing turn:

```bash
python3 scripts/herdr_agent_messenger.py mail send \
  --to local/blue-owl --kind action --subject 'Review' --body 'Inspect the boundary.'
python3 scripts/herdr_agent_messenger.py inbox read
python3 scripts/herdr_agent_messenger.py inbox handle --message MESSAGE_UUID
python3 scripts/herdr_agent_messenger.py mail show --thread THREAD_UUID
python3 scripts/herdr_agent_messenger.py mail withdraw --message MESSAGE_UUID
python3 scripts/herdr_agent_messenger.py repo show
```

| Command | Flags and semantics |
| --- | --- |
| `mail send` | Required `--to local/LABEL` (the canonical local host also works). `--kind fyi\|action`, default `fyi`; optional `--reply-to UUID`, `--subject TEXT` (1024 UTF-8 bytes); exactly one of `--body TEXT` / `--body-file PATH` (UTF-8, nonblank, at most 16 KiB). Replies keep the thread and increment hop; replying to addressed queued/delivered mail also marks that parent handled atomically. |
| `inbox read` | Optional `--message UUID`, `--all`, `--limit N` (default 100, range 1–1000). Without `--all`, shows queued, uncertain delivering, and delivered mail. Reads never change delivery, logs, or bells, even from subagents. |
| `inbox handle` | Required repeatable `--message UUID`. The entire batch is validated before changing any row. A queued pull-only message becomes handled with no fabricated delivered timestamp. |
| `mail show` | Required `--thread UUID`; returns retained bodies and delivery state. |
| `mail withdraw` | Required `--message UUID`; originating terminal only. Withdraw races atomically with queued→delivering claim. Already-claimed/delivered/handled messages are not withdrawn; inspect the returned `withdrawn` flag. |
| `repo show` | Read-only repository identity, current settings, and local agents. No public setting mutation command. |

All commands return one JSON object. Errors use the existing CLI convention
`{"error":{"code":"CODE","message":"TEXT"}}`, exit 1; argparse usage errors exit 2.
The legacy request lifecycle's `error_code` field is unchanged. Useful codes:
`sender_unresolved`, `stale_session`, `recipient_absent`, `repo_mismatch`,
`message_conflict`, `delivery_in_progress`, `body_too_large`.

Messages use the stable terminal slot, not inherited pane IDs. The CLI matches
its process ancestors against Herdr's public `pane process-info` shell PID;
cached old pane bindings only prioritize that lookup. Piped tool subprocesses
work after a move without `HERDR_TERMINAL_ID` or a stdin TTY. A runtime that has
already resolved its stable terminal can supply `HERDR_TERMINAL_ID`. If no
identity can be proven, HAM fails closed rather than guessing from a stale pane.

Git HTTPS/SSH/scp origins normalize to `git:host[:nondefault-port]/Path`, preserving
path case. Set `git config ham.repo-id 'git:host/stable/repo'` before a configured
origin rename to keep that explicit identity. Without an origin, identity is
local to the canonical host and real repository root. A different-repository
occupant holds queued mail; a same-repository new session can claim it. Missing
agents hold queued mail; closed terminals make unclaimed mail undeliverable.
Unknown claims are never automatically replayed across crashes/session changes.

Storage is `${XDG_STATE_HOME:-~/.local/state}/herdr-agent-messenger/ledger`
(directory 0700, database 0600, SQLite WAL). Use `HERDR_AGENT_LEDGER_DIR` for
isolated tests; `HERDR_AGENT_CANONICAL_HOST` sets a new ledger's fixed host name,
and reopening an existing ledger with a different name is rejected. Final
message bodies expire after 90 days; permanent compact hashes/receipts prevent
ID reuse even after deletion. Status/wake history expires after 30 days.

Internal plugin/runtime services use the same launcher: `ledger doctor`,
`ledger reconcile`, `ledger event`, `ledger retention`, `ledger agent-session`,
and `ledger delivery claim|started|release`. Reconcile/events fetch a fresh
complete snapshot under a per-ledger lock. `agent-session` takes JSON stdin with
`terminal_id`, `session_ref`, `client`, `repo_id`, `extension_present`.
`delivery claim` takes `terminal_id`, `agent_id`, `generation`, optional `kinds`
(`fyi`, `action`) and `via` (`wake`, `before_agent_start`, `hook`); it returns
`claim_id` and the claimed messages. `started` takes `claim_id`, `agent_id`,
`generation`, `evidence` and requires actual context-application evidence, not
hook return or terminal acceptance. `release` takes the same identity/evidence
and `disposition` (`not_applied` to requeue, `unknown` to retain the uncertain
claim). These APIs do not themselves prove native OMP/Claude admission; no
automatic receiver/wake is enabled by the local mailbox commands.

### `list`

```bash
python3 scripts/herdr_agent_messenger.py list --host local
python3 scripts/herdr_agent_messenger.py list --host macbook-pro --verbose
```

| Flags | Meaning |
| --- | --- |
| `--host HOST` | Host to discover; default `local`. |
| `--verbose` | Full agent records instead of compact agent summaries. |
| `--legacy` | Alias for `--verbose`. |
| `-h`, `--help` | Print help. |

Default output is `host` and `agents`; each compact agent includes `address`, `status`, and `workspace`.

### `status`

```bash
python3 scripts/herdr_agent_messenger.py status --address macbook-pro/purple-koala
```

Flags: `--address HOST/LABEL` / `--label LABEL` / `--route TOKEN` (exactly one), `--host HOST` (default `local`), `--verbose`, `-h` / `--help`. Checks one recipient without listing its host. Default output: `address`, `status`, optional `stale: true`.

### `send`

```bash
python3 scripts/herdr_agent_messenger.py send \
  --address macbook-pro/purple-koala --message 'Report your current task.' --no-wait
```

| Flags | Meaning / default / range |
| --- | --- |
| Recipient selectors | `--address HOST/LABEL` / `--label LABEL` / `--route TOKEN`; `--host HOST` defaults to `local`. |
| `--message TEXT` | Required, nonblank prompt text. |
| `--wait`, `--no-wait` | Mutually exclusive; default `--wait`. Waits for Herdr's lower-level completion signal, not safe lifecycle correlation. Prefer `request` for correlated replies. |
| `--timeout MS` | Wait deadline; default 120000, >0, no HAM upper bound. |
| `--defer-timeout MS` | Focus-hold deadline; default 180000, >0. Expiry requires a user decision, never automatic push. |
| `--verbose` | Full payload. |
| `-h`, `--help` | Print help. |

If focused, the recipient is not prompted yet: `send` returns a `deferred` request with a `request_id` and exit 3 immediately, even with `--wait`. Observe that ID with `request-status`; never resend. Otherwise send immediately. Immediate compact output is `sent`, `address`, `waited`, and `warnings` only when nonempty. Successful no-wait submission does not mean completion. If the recipient was already working, a lower-level wait can describe its old turn instead.

### `batch`

```bash
python3 scripts/herdr_agent_messenger.py batch \
  --requests-json '[{"address":"local/white-bison","message":"Inspect API behavior."},{"address":"macbook-pro/purple-koala","message":"Review the UI."}]' \
  --wait --timeout 120000 --max-workers 4
```

| Flags | Meaning / default / range |
| --- | --- |
| `--requests-json JSON` | Required nonempty array, or `{"requests":[...]}`. `-` reads JSON from stdin. |
| `--wait`, `--no-wait` | Mutually exclusive; default `--wait`. Wait mode observes each request lifecycle with 120 response lines / 12000 characters. No-wait mode returns after acceptance or focus deferral. |
| `--timeout MS` | Per-request wait deadline; default 120000, >0, no HAM upper bound. |
| `--max-workers N` | Concurrent dispatch limit; default 4, range 1–8. |
| `--defer-timeout MS` | Per-request focus-hold deadline; default 180000, >0. |
| `--verbose` | Full batch items and responses. |
| `-h`, `--help` | Print help. |

Each item must have exactly one `address` / `route` and a nonblank string `message`. Duplicate resolved targets, including an address and route for the same agent, fail with `duplicate_route`. Invalid items do not block independent valid items. Results retain input order. HAM dispatches the supplied messages, not a semantic decomposition: tailor each assignment yourself.

### `read`

```bash
python3 scripts/herdr_agent_messenger.py read \
  --address macbook-pro/purple-koala --lines 160 --max-bytes 65536
python3 scripts/herdr_agent_messenger.py read \
  --address macbook-pro/purple-koala --cursor PREVIOUS_CURSOR
```

| Flags | Meaning / default / range |
| --- | --- |
| Recipient selectors | `--address HOST/LABEL` / `--label LABEL` / `--route TOKEN`; `--host HOST` defaults to `local`. |
| `--lines N` | Maximum recent output lines; default 120, >0. |
| `--max-bytes N` | Maximum UTF-8 output bytes; default 65536, >0. |
| `--cursor TOKEN`, `--watermark TOKEN` | Aliases; opaque prior read cursor. Never fabricate/edit it. |
| `--verbose` | Full diagnostic fields and terminal output. |
| `-h`, `--help` | Print help. |

Positive limits have no HAM upper bound. Compact output contains `address`, cleaned `output`, `delta`, `truncated`, `cursor`. Pass the cursor to the next read. Terminal screens can be rewritten: do not infer progress from line numbers. Expired cursors return a fresh bounded snapshot (`delta: false`), not a safe append-only delta. Verbose `cursor_status` explains the cursor result. A read is not proof that a requested turn completed.

### `request`

```bash
python3 scripts/herdr_agent_messenger.py request \
  --address macbook-pro/purple-koala \
  --message 'Inspect the current task and report blockers.' \
  --timeout 120000 --output-lines 120 --output-chars 12000
```

| Flags | Meaning / default / range |
| --- | --- |
| Recipient selectors | `--address HOST/LABEL` / `--label LABEL` / `--route TOKEN`; `--host HOST` defaults to `local`. |
| `--message TEXT` | Required nonblank prompt text. |
| `--timeout MS` | Observation deadline; default 120000, >0. Includes time spent waiting for focus-held delivery. |
| `--output-lines N` | Maximum response lines; default 120, >0. |
| `--output-chars N` | Maximum response characters; default 12000, >0. |
| `--defer-timeout MS` | Focus-hold deadline; default 180000, >0, independent of the observation deadline. |
| `--verbose` | Full lifecycle record and raw response output. |
| `-h`, `--help` | Print help. |

Positive limits have no HAM upper bound. Requests submit once, observe a fresh turn, and capture bounded output. A focused recipient is held first; if the observation budget expires while held, the saved request remains HAM-owned. Continue with `request-status`, not another request.

### `request-status`

```bash
python3 scripts/herdr_agent_messenger.py request-status \
  --request-id REQUEST_ID --wait 120000
```

| Flags | Meaning / default / range |
| --- | --- |
| `--request-id ID` | Required returned lifecycle ID: 32 lowercase hexadecimal characters. |
| `--wait MS` | Default 0: one observation. A nonnegative wait stops on a change of state, phase or `error_code`, a terminal state, or the deadline, not response-text growth; it never resends. |
| `--verbose` | Always print the full public record, even when unchanged. |
| `-h`, `--help` | Print help. |

Run on the original sender host with access to the saved request state. Without a new state/phase/error/response, compact output contains only `request_id`, `state`, `terminal`, `submitted`, `changed: false`. Changed results contain full compact fields plus `changed: true`. A timeout returning unchanged or nonterminal output does not authorize resending. A held request can advance to submission during observation. Already-terminal results remain terminal.

The wait stop condition differs from changed-only reporting: response-text growth can set `changed: true` at return without ending the wait early.

### `spawn`

```bash
python3 scripts/herdr_agent_messenger.py spawn \
  --kind omp --cwd /path/to/project --placement tab
python3 scripts/herdr_agent_messenger.py request \
  --address RETURNED_HOST/RETURNED_LABEL --message 'Review the project and report blockers.'
python3 scripts/herdr_agent_messenger.py spawn \
  --kind codex --cwd /path/to/project --placement workspace -- --model MODEL
```

| Flag | Meaning / default / range |
| --- | --- |
| `--kind KIND` | Required Herdr-supported agent-kind token, e.g. `omp`, `codex`, `claude`; nonblank, no whitespace. |
| `--host HOST` | Default `local`; remote must be a concrete configured SSH alias with Herdr running. |
| `--cwd PATH` | Directory on selected host. Local default: current working directory. Required for SSH. Must exist. |
| `--placement split\|tab\|workspace` | Default `split`; create a pane, tab, or workspace without focusing it. |
| `--from-pane PANE` | Source pane on selected host. Local split default: `HERDR_PANE_ID`; required for remote split. For tabs, may identify source workspace. |
| `--direction right\|down` | Split direction, default `right`; relevant only to splits. |
| `--workspace WS` | Workspace for new tab. Infer from `--from-pane` or local `HERDR_PANE_ID` when available; otherwise use Herdr's default. Not used for splits/workspaces. |
| `--workspace-label TEXT` | Label for new tab/workspace. Default: explicit `--label`, otherwise generated agent label. Not used for splits. |
| `--label LABEL` | Default: free HAM `color-animal` label. Explicit label must be nonblank and contain no `/`. Collision fails, never silently chooses another explicit name. |
| `--timeout MS` | Interactive readiness timeout; default 90000, inclusive range 1–300000. Not a reply deadline. |
| `--verbose` | Full startup/placement/metadata result. |
| `-h`, `--help` | Print help. |
| `-- AGENT_ARGS...` | Optional positional `agent_args` forwarded to the interactive agent; no default arguments. Put all HAM flags before `--`. |

Creates a labeled interactive session, not a prompt. Send a separate `request` to the returned address. Metadata flags describe reporting/renaming success, not reply completion. A custom label outside HAM's palette has empty marker/color and no palette metadata. Inspect failure cleanup before creating another session: a failed startup may leave a resource behind.

## Result contracts

### Exit codes

| Code | Meaning | Action |
| --- | --- | --- |
| `0` | `request`/`request-status`: settled; batch: every item succeeded; other operations: success (or help). | Inspect the payload and response quality. Exit 0 is not proof of a worker's claims. Immediate no-wait send does not mean settled. |
| `1` | JSON error envelope, terminal `submission_failed`, or batch with any failed/cancelled/timeout item. | Inspect each item and delivery evidence. Retry only known-undelivered work after fixing the cause; exit 1 alone does not prove non-delivery. |
| `2` | Argument-parser usage error (stderr, not the JSON envelope). | Correct command syntax. |
| `3` | `submitted_working`, `submitted_unknown`, `deferred`, `awaiting_decision`, or batch submitted/deferred items without failures. | **HAM still owns it: never resend.** Observe the saved request ID. Without an ID, inspect recipient status/output. Respect terminal occupant-change cases. |

### Lifecycle states

| `state` | Meaning / next step |
| --- | --- |
| `deferred` | Nonterminal; `submitted: false`, phase `deferred_recipient_focused`. Recipient pane focused; HAM holds delivery until focus leaves before deadline. Observe the original ID. |
| `awaiting_decision` | Nonterminal; `submitted: false`, phase `awaiting_decision`. Focus-hold deadline passed; user must Push or Cancel in **HAM: Held Messages**. No automatic push after timeout, even if focus later changes. |
| `submission_failed` | Terminal. Resolution failed, recipient disappeared/changed before held delivery, submission rejected, or held delivery cancelled. Inspect `error_code` before considering a new request. |
| `submitted_working` | Submitted/possibly submitted and recipient observed working; requested completion not proven. Advance with `request-status`, never another request. |
| `submitted_settled` | Terminal. Fresh requested working→settled cycle observed. Review the bounded response and verify claims. |
| `submitted_unknown` | Submitted/possibly submitted but the requested turn is not confirmed. Continue saved observation, never blindly resend. Can be terminal with `recipient_changed`. |

`submitted` is true/false/null for accepted/rejected/unknown delivery. `phase` explains the current boundary; `terminal` governs whether further progress is possible. Do not infer successful completion from `submitted: true` alone.

### Focus-held delivery

Agent CLI `request`, `send`, and `batch` respect recipient focus on the selected host, including remote Herdr. HAM popup sends and coordinator delegation initiated by the user are not focus-held. A deferred request is already accepted for eventual delivery: **do not resend, bypass HAM, or ask the user to unfocus**.

Self-send fails immediately with `recipient_is_sender`, before any focus check; the sender's focused pane never causes a self-addressed message to be deferred.

The default `--defer-timeout` is 180000 ms. Focus leaving before the deadline permits delivery; expiry while still focused changes to `awaiting_decision`. HAM notifies the sender host and, for remote recipients, the recipient host. The remote notification names the sender host, where **HAM: Held Messages** is available. Notification delivery is not proof that a user has read or decided.

In **HAM: Held Messages**, Up/Down selects, `p` pushes, `c` cancels, and `q`/Esc closes. Push can combine the message with unsent text already in that pane; the popup warns the user. Cancel produces terminal `submission_failed`, phase/error code `delivery_cancelled`, `submitted: false`. The agent must not automatically retry a user cancellation. A vanished/replaced recipient fails before held delivery with its resolver error or `recipient_changed`.

After deferred delivery or Push, the worker observes the submitted request about every 0.5 seconds until it is terminal or 30 minutes have passed since submission. Read the saved result with `request-status`; after the worker's observation limit, this command can still advance the request. The worker stays alive while any deferred, awaiting-decision, or watched request remains.

Focus is not a draft-text detector: a person who leaves a pane containing unsent text can still have that text combined with a later prompt. Never claim focus deferral guarantees empty input.

### Compact versus verbose payloads

`--verbose` is available on every command. Compact responses omit bulky identities/routes and optional null/absent fields. Verbose preserves the full public diagnostic result; private fields are never printed. Do not rely on compact output to provide a route token.

| Operation | Compact fields | Additional/full `--verbose` fields |
| --- | --- | --- |
| `list` | `host`, `agents` with `address`, `status`, `workspace` | Full agent records with the fields listed below. |
| `status` | `address`, `status`; `stale: true` only if stale | `agent` full record, `route`, `route_refreshed`. |
| `spawn` | `address`, `marker`, `created`, `pane_id`, optional `tab_id`, `workspace_id`; `label_metadata_reported` / `placement_label_reported` only when false | `label`, `color`, `host`, `kind`, `cwd`, `route`, `agent`, both metadata flags even on success, plus compact identity/placement fields. `created` is `pane`, `tab`, or `workspace`. |
| Immediate `send` | `sent`, `address`, `waited`; `warnings` only when nonempty | Legacy `recipient`, `route`, `route_refreshed`, `wait_can_track_submitted_turn` when waited, and `warnings` including empty lists. Held send uses a lifecycle result instead. |
| `read` | `address`, cleaned `output`, `delta`, `truncated`, `cursor` | Raw terminal `output`, `cursor_status`, `route`, `route_refreshed`, plus read fields. |
| `request`, held `send`, `request-status` | `request_id`, `state`, `terminal`, `submitted`, optional `error_code` / `error`, `phase`, `address`, optional `defer` / `changed` / `response` | `sender`, `recipient`, route/refresh data when available, `submission_timed_out`, `timed_out`, `preexisting_working`, `requested_turn_observed`, `wait_safe`, `recipient_status`, optional `cancelled`, and `response.output` instead of cleaned `response.text`. |
| Batch item | `address`, `status`, optional `request_id`, `state`, `error_code`, `error`, `response` (`text`, `truncated`) | Full per-item route/recipient/dispatch detail and bounded response output when available. |

Full agent record fields are `host`, `name`, `label`, `qualified_name`, `address`, `pane_id`, `workspace_id`, `workspace_label`, `status`, `session_id`, `cwd`, `local`, `stale`, `revision`, `agent_kind`, `terminal_id`, `workspace_is_worktree`, `route_target`, and `focused`. Empty or false values can still be present in verbose records.

Compact request field order puts decision fields first. `defer` is shown only for deferred/awaiting states and includes `reason: recipient_focused` and ISO8601-UTC `deadline`. Full deferred records also expose ISO8601-UTC `since` and `notified` (null until attempted, then `local` and optional `remote` notification reasons such as `shown`, `disabled`, `rate_limited`, `no_foreground_client`, `busy`).

Compact `request-status` compares state, phase, error code, and response text with the last reported result. Unchanged output is exactly the decision subset `request_id`, `state`, `terminal`, `submitted`, `changed: false`; otherwise it includes the full compact result with `changed: true`. Verbose always returns the full public record, not the unchanged subset.

Preflight errors use `{"error":{"code":"...","message":"..."}}` and have no lifecycle ID. Lifecycle errors use `error_code` and human-readable `error`; optional fields can be absent in compact output or null in verbose output. Not every CLI result has a `request_id`.

### Response interpretation

- Compact `response.text` is cleaned terminal text, with TUI chrome and prompt echoes removed; verbose `response.output` is bounded terminal output. Neither is a structured assistant-message API.
- `response.truncated`: true only when the reply text itself was cut by output bounds or a failed read, in both compact and verbose results. Omitting older history or losing baseline overlap alone does not set it. False does not prove correlation or a reliable complete transcript.
- `response.correlated`: fresh requested working→settled cycle observed and output capture reliable. False means text is not proven to be this request's completed answer.
- `response.anchored`: optional, emitted only when true in compact output. Capture started at the last matching `Message from HOST/LABEL:` header because preceding-history overlap was unavailable. Removes older history; does not prove settlement or uniquely correlate concurrent requests from one sender. Check `correlated` and `truncated` separately.
- Empty compact responses may be omitted before settlement. Batch responses contain `text` and `truncated`, not the full lifecycle correlation fields; inspect by request ID when needed.

If the recipient was already working, its first settlement is only the old-turn boundary. HAM requires another working→settled cycle. `request-status` cannot reconstruct cycles missed between observations. An occupant change after submission stops capture and yields terminal `submitted_unknown` / `recipient_changed`; do not collect replacement-agent output. Narrow panes hard-wrap text; prefer tab/workspace placements for long replies. Anchoring cannot undo wrapping.

### Batch results

| Per-item `status` | Meaning |
| --- | --- |
| `succeeded` | Requested lifecycle settled in wait mode. |
| `submitted` | Acceptance confirmed, completion unverified (including immediate successful no-wait items). Never resend. |
| `deferred` | HAM holds delivery; may be `deferred` or `awaiting_decision` in `state`. Not submitted yet, but HAM owns it. Follow the request ID; never resend. |
| `failed` | Validation, resolution, submission, or held-delivery failure. Inspect error and delivery evidence. |
| `timeout` | Acceptance could not be confirmed or dispatch timed out. Possibly delivered; inspect before any retry. |
| `cancelled` | Dispatch/submission cancelled. Inspect evidence; cancellation of waiting may not cancel recipient work. |

Aggregate `status` is `succeeded` when every item is accepted (`succeeded`, `submitted`, or `deferred`), `partial` when only some are accepted, and `failed` when none are accepted. Deferred counts like submitted for aggregation, not as proof of delivery. Aggregate `succeeded` does **not** mean settled. Exit 1 takes precedence for any failed/cancelled/timeout item; otherwise any submitted/deferred item gives exit 3; all succeeded gives exit 0.

Lifecycle-backed items include `request_id` and bounded response when available. Preflight failures and immediate no-wait items may not have IDs. Compact batch items can expose `error_code`; verbose `error`/`detail` may carry per-item error codes or human-readable lifecycle failure text. Use the request ID for full lifecycle details. Never retry delivered, held, submitted, unknown, or timed-out peers when fixing another item's input.

### Error code catalog and retry policy

Codes can occur as envelope `error.code`, lifecycle `error_code`, or per-item error fields. Consult saved lifecycle state when a batch item has an ID.

| Group / codes | Meaning and safe recovery |
| --- | --- |
| Input: `invalid_address`, `conflicting_address`, `missing_address`, `invalid_label`, `empty_message`, `invalid_message`, `invalid_timeout`, `invalid_defer_timeout`, `invalid_lines`, `invalid_max_bytes`, `invalid_output_lines`, `invalid_output_chars`, `invalid_cursor`, `invalid_request_id` | Fix rejected input, then retry only known-undelivered work. Never fabricate an address, route, cursor, or ID. `invalid_defer_timeout` requires positive integer milliseconds; status `--wait` accepts nonnegative milliseconds (`invalid_timeout` on rejection). |
| Batch input: `invalid_batch_json`, `invalid_batch`, `empty_batch`, `invalid_request`, `invalid_workers`, `duplicate_route` | Correct schema/worker range or remove duplicate targets. `invalid_request` includes both/neither address and route. Retry only rejected items, never accepted peers. |
| Target: `agent_not_found`, `agent_ambiguous`, `invalid_route`, `route_expired`, `route_ambiguous`, `resolve_failed`, `recipient_is_sender` | Relist/reselect intended recipient or fix self-routing. Do not guess or create a missing agent unless asked. On a post-submission refresh error preserve the original request and retry status, not delivery. |
| Connectivity: `host_not_configured`, `host_unavailable`, `read_failed` | Configure a concrete authorized SSH alias or restore Herdr/connectivity/trust. Local sandbox/socket permission failures need local-socket access; discovery failure does not prove an agent is missing. Retry discovery/status/read; never resend merely because reading failed. |
| Sender: `sender_unavailable`, `sender_probe_failed` | Run from the current Herdr agent pane. Probe failure precedes submission; if sandbox/socket access denied, obtain access and retry that exact undelivered request. In batches retry only failed items, not accepted/unknown/timed-out peers. |
| Submission: `prompt_failed` | Prompt rejected. Fix reported cause before retrying known-undelivered work. |
| Uncertain/wait: `prompt_acceptance_unknown`, `prompt_timeout`, `wait_timed_out`, `timeout` | Delivery/completion uncertain or wait expired. Never blindly resend. Advance saved lifecycle or inspect recipient status/output without an ID. |
| Cancellation: `delivery_cancelled`, `prompt_cancelled`, `cancelled` | `delivery_cancelled` is a user-cancelled held request, terminal and not submitted; respect the decision, do not auto-retry. Other cancellations before submission may be retried after fixing cause; after submission they may only cancel waiting. Inspect `submitted` and saved state. |
| Occupant: `recipient_changed` | Before held delivery: terminal failure, not submitted. After submission: terminal unknown, no replacement output. Report uncertainty and reselect only for explicitly new work. |
| State: `request_not_found`, `request_state_invalid`, `request_state_unavailable` | Verify ID, original sender host/state location, and permissions. Storage failure can occur after submission; do not resend until delivery checked. |
| Spawn input: `cwd_required`, `sender_pane_unavailable`, `label_unavailable` | Supply remote cwd/source pane or an available valid label, then retry startup. |
| Spawn execution: `placement_failed`, `agent_start_failed` | Inspect Herdr failure and cleanup result. Check already-created sessions/resources before retrying; unsuccessful cleanup may leave one behind. |
| Installation/internal: `cli_not_found`, `internal_error`, `dispatch_failed`, `invalid_sender_result`, `invalid_command` | Restore source-backed CLI installation or investigate reported failure. Unexpected error does not prove non-delivery; inspect state/recipient before retry. `invalid_command` is a defensive fallback; unknown commands normally exit 2. |

## Coordination safety

List every requested host and verify each label; assign non-overlapping work; send worker-specific context unless explicitly asked to broadcast; batch dispatch; observe every worker through settlement or clear terminal failure; review responses against the workspace; follow up on inconsistencies; synthesize missing hosts, uncertainty, unverified claims, and risks. HAM does not perform semantic decomposition or verification.

Never modify SSH trust settings, bypass host-key verification, or send through an alias absent from configured SSH config. Prefer the bundled CLI over reconstructed remote commands. Never resend because a wait/read/status/storage operation failed.

## Client onboarding and installation

Install with **HAM Skill** in Herdr's command palette (or `Ctrl+G` in Agent Messenger). Installer targets are `codex`, `claude`, `omp`, or `all`.

From the plugin/source checkout root:

```bash
python3 agent_skill_installer.py install all
python3 agent_skill_installer.py install codex
python3 agent_skill_installer.py install claude
python3 agent_skill_installer.py install omp
```

`all` installs all three user-scope targets. The popup also offers project-scope installs when a project workspace is available. Canonical `SKILL.md`, this `reference.md`, scripts, and agent metadata belong together; do not copy only SKILL.md. The checkout's Claude skill is a pointer to the canonical `.agents` skill; read canonical SKILL.md fully, this reference only on demand. Installed client copies contain the complete canonical bundle.

| Client | User scope | Project scope | Invocation |
| --- | --- | --- | --- |
| Codex | `~/.agents/skills/herdr-agent-messenger` | `<project>/.agents/skills/herdr-agent-messenger` | Type `$ham` and select **HAM — Herdr Agent Messenger**, or invoke `$herdr-agent-messenger`. |
| Claude Code | `~/.claude/skills/herdr-agent-messenger` | `<project>/.claude/skills/herdr-agent-messenger` | `/herdr-agent-messenger` or a matching natural-language request. |
| omp | `~/.omp/agent/skills/herdr-agent-messenger` | `<project>/.omp/skills/herdr-agent-messenger` | Ask naturally to use HAM or read `skill://herdr-agent-messenger`. |

An installed copy without the current reference is outdated, not a reason to bypass the CLI. Update through the installer. Unrelated skill directories and symlink conflicts are not overwritten. The bundled launcher needs access to the HAM plugin/source CLI; `cli_not_found` means repair that installation rather than invent a replacement transport.

For expanded client setup, see `docs/skill-guide.md` in the plugin/source checkout.
