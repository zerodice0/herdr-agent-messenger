# HAM — on-demand reference

Read `SKILL.md` first. This reference covers the complete CLI, diagnostic payloads, recovery, and installation. Paths to `scripts/herdr_agent_messenger.py` are relative to this skill directory; use an absolute path when working elsewhere. Messaging runs from the coordinator's active Herdr agent pane (`HERDR_PANE_ID`).

## Command and flag reference

All commands accept `-h` / `--help`. Legacy prompt-routing commands (`list`, recipient `status`, `send`, `batch`, `read`, `request`, `request-status`, `spawn`), `status board|list` and `inbox read` accept `--verbose` (full diagnostic JSON; default off); other mailbox/ledger/runtime/device/status commands, including `mail threads` and `mail direct-list`, do not. Numeric arguments are integers; optional flags are unset unless a default is listed. Milliseconds are denoted `MS`.

### Recipient selectors

`status`, `send`, `read`, and `request` require **exactly one** of `--address`, `--label`, or `--route`:

| Flag | Meaning / default |
| --- | --- |
| `--address HOST/LABEL` | Exact `address` returned by `list` or `spawn`; carries its own host. |
| `--label LABEL` | Exact current Agent Labels name on `--host`. |
| `--route TOKEN` | Opaque verified GUI/CLI route; carries its own host. Pass unchanged, never decode or edit it. Supports unlabeled GUI selections. |
| `--host HOST` | Default `local`; otherwise a concrete configured SSH alias. An explicit non-default host must agree with `--address` or `conflicting_address` results. For routes use the token's host, not an override. |

Only literal `local` means local transport. Even a hostname matching this machine means SSH. Labels can repeat across hosts and do not guarantee the same occupant after restart. Missing/ambiguous hosts or labels must be reported, not guessed. Never substitute a pane ID or create a missing agent unless asked.

These selector rules describe legacy routing. Durable mail accepts
`local/LABEL`, registered canonical names or SSH aliases, `main`, and
`[HOST/]main@REPO`. Named local/remote targets are resolved across all repos;
zero label matches gives `agent_not_found`, several gives `agent_ambiguous`
with candidate addresses and repos. Peer registration remains the trust
boundary; cross-repo mail needs no repository allowlist.

`main` selects the sender's current repo/host/anchor role-group main, shared by
linked workspaces while their anchor is open.
`[HOST/]main@REPO` instead selects the main of REPO on HOST (default local).
REPO is an exact repo ID or a unique trailing path-component suffix, such as
`dxp-api` or `org/dxp-api`; quote the address when necessary. An exact ID wins.
Exactly one current main must exist across distinct role groups in that repo on
the chosen host; linked workspaces in one group do not add candidates.
Resolution stores its concrete target; replies/withdrawals do not re-resolve it.
Remote resolution uses computed exported roles and optional role-scope metadata.

| Resolution error | Meaning / action |
| --- | --- |
| `repo_ambiguous` | REPO matches several repo IDs; use an exact ID or longer unique suffix. |
| `main_ambiguous` | Several distinct role groups have mains; inspect listed anchor workspace IDs and choose a concrete `HOST/LABEL`. |
| `main_unresolved` | No current main in the selected group/repo; inspect status, never fall back to another host/home/group. |
| `agent_ambiguous` | A label matches several agents across repos; inspect candidate addresses/repos rather than guessing. |

Examples: `mail send --to main@dxp-api --body 'Review the API contract.'`
or `mail send --to beta/main@org/dxp-api --body 'Review the API contract.'`.
Before using legacy `send`, `request` or `batch`, inspect repository participation:
manual/auto same-repository traffic between participating agents is refused with
`use_mailbox`, with each endpoint's role evaluated in its own host-local
role group. Use `mail send`, not another transport. All batch targets are
resolved and policy-checked before any dispatch; a refusal or discovery/policy
failure aborts the coordinated batch without partial submission. Off-mode,
nonparticipating and other-repository legacy traffic retains its previous
behavior. The policy prevents accidents; it is not an authorization boundary.

A verified route can refresh only when continuity checks identify one safe occupant. In verbose output, retain a new `route` when `route_refreshed: true`. Compact output intentionally omits routes; use addresses for normal agent workflows.

### Local mailbox

Mailbox messages are durable data, not terminal prompts. Local agents and
registered remote agents can exchange them across repositories without
interrupting an ongoing turn:

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
| `mail send` | Required `--to local/LABEL`, `CANONICAL_HOST/LABEL`, registered `SSH_ALIAS/LABEL`, `main`, or `[HOST/]main@REPO`. `--kind fyi\|action`, default `fyi`; optional `--reply-to UUID`, `--subject TEXT` (1024 UTF-8 bytes); exactly one of `--body TEXT` / `--body-file PATH` (UTF-8, nonblank, at most 16 KiB). Replies keep the thread and increment hop; replying to addressed queued/delivered local mail also marks that parent handled atomically. Remote targets are selected from a fresh all-repository status export. |
| `inbox read` | Optional `--message UUID`, `--all`, `--limit N` (default 100, range 1–1000), `--verbose`. Without `--all`, shows queued, uncertain delivering, and delivered mail. Compact IDs/sender/body/state/reply context by default; verbose restores delivery diagnostics (see Compact versus verbose payloads). Reads never change delivery, logs, or bells, even from subagents. |
| `inbox handle` | Required repeatable `--message UUID`. The entire batch is validated before changing any row. A queued pull-only message becomes handled with no fabricated delivered timestamp. |
| `mail show` | Required `--thread UUID`; reads a known retained thread, including sent mail, without caller-recipient filtering. Returns retained bodies and delivery state from this ledger. |
| `mail threads` | Optional `--repo REPO_ID`, `--limit N` (default 100, 1–1000), `--cursor TOKEN`; retained threads originating in or involving that repo as sender/recipient, including pending remote copies. Returns `{repo_id,threads,next_cursor}`. |
| `mail direct-list` | Same `--repo REPO_ID`, `--limit N`, `--cursor TOKEN`; retained `via=direct` user-input submissions, not `spawn_initial`. Returns `{repo_id,submissions,next_cursor}`. Read-only, not a direct-send command. |
| `mail withdraw` | Required `--message UUID`; originating terminal only. Local withdrawal races atomically with queued→delivering claim. Remote mail never attempted on the wire is cancelled locally; otherwise the saved original route carries a withdrawal request to the final recipient. Inspect `withdrawn` and `withdraw_requested`; already-claimed/delivered/handled or other non-withdrawn final mail is not withdrawn. Repeating successful withdrawal returns `withdrawn:true`, preserving the first final receipt/time. |
| `repo show` | Read-only repository identity, current settings, and local agents. No public setting mutation command. |

Successful `mail send` may include advisory `warnings` entries:

- `recipient_no_receiver`: for example,
  `{"code":"recipient_no_receiver","message":"...","client":"omp"}` (or
  `claude`), when the resolved recipient explicitly has `extension_present=0`.
  The send still succeeds and remains queued; do not resend it. Remote absence
  is advisory only when the exported row says absent; unknown receiver state
  and Codex pull-only recipients do not produce this warning. Run `runtime status`
  on the recipient host, request consent before installing a missing runtime,
  then restart only the selected sessions if registration is pending.
- `fyi_no_wake`: `fyi` was sent to a known ready, non-stale, participating OMP
  recipient with a registered receiver and Herdr `turn_state` of `idle` or
  `done`. FYI enqueued while idle cannot wake it. FYI arriving during a turn
  can be marked locally for delivery right after that turn under existing wake
  gates (see below). This advisory does not reject, resend, or change the mail
  kind; the CLI default remains `fyi`. It is not emitted
  for `action`, busy recipients, unknown/stale state, missing receivers,
  non-OMP clients, mode `off`, or nonmembers. Replies use the same recipient-state
  rule, including replies to an `action` parent; the parent's kind does not
  exempt a currently idle recipient. A future action wake still needs all
  normal runtime/admission gates.

For replies, use `mail send --reply-to UUID --kind action` when the recipient
must act/continue (for example, on an answer to a question or a decision);
use `fyi` otherwise.

FYI never wakes an idle recipient. At local recipient enqueue, the ledger sets
`delivery.fyi_turn_end` only when its locally reconciled `agents.last_seen` is
within 15 seconds and `turn_state=working`. Such FYI is delivered right after
that turn under the existing wake gates. Idle, unknown or stale enqueue state
leaves FYI natural-turn only; no later status update retroactively sets the flag.
The flag is local delivery metadata, never a new kind or immutable wire field.

#### History and cursor paging

`mail threads` and `mail direct-list` default to the verified caller's repository.
Explicit `--repo REPO_ID` avoids fetching Herdr caller identity. A thread
participates when the selected repo is its origin, any message's `sender_repo_id`,
or any delivery's `recipient_repo_id`; cross-repo threads are therefore visible
to both sides. `mail direct-list` remains retained direct user-submission history
for the selected repo, not a cross-repo mail view. These are this ledger's records,
not only the caller's inbox and not replicated all-host history. Reads never
claim, acknowledge, handle, refresh the registry, or change status/last_seen/display state.

`next_cursor` is `null` at the end; otherwise pass it unchanged as `--cursor`.
The opaque HMAC-signed token binds the ledger, query kind and repository; malformed,
tampered or wrong-scope tokens fail with `invalid_cursor`. Keyset order is
`updated_at DESC`, UUID ascending (`thread_id` or `submission_id`); no OFFSET.
Pages are live, not a frozen snapshot. Concurrent updates can move unseen records
before the boundary, and deletions disappear; restart at the first page to see
newer updates. Thread summaries/messages are consistent within one call, not
across calls.

All commands return one JSON object. Errors use the existing CLI convention
`{"error":{"code":"CODE","message":"TEXT"}}`, exit 1; argparse usage errors exit 2.
The legacy request lifecycle's `error_code` field is unchanged. Useful codes:
`sender_unresolved`, `stale_session`, `native_session_pending`, `recipient_absent`,
`message_conflict`, `delivery_in_progress`, `body_too_large`, `invalid_limit`,
`invalid_repo`, `invalid_cursor` (including history tampering/scope mismatch).
Address resolution also reports `agent_not_found`, `agent_ambiguous`,
`repo_ambiguous`, `main_ambiguous`, or `main_unresolved` as described above.
Different sender/recipient repositories are not a `repo_mismatch` send error.
`native_session_pending` means Herdr has not yet published the OMP native session.
The receiver waits within its existing bounded registration retries, without
publishing the path itself; success removes HAM's waiting/unavailable status.
Exhausting the retries shows unavailable. A different already-reported path remains
`stale_session`, not a retryable identity replacement.

Messages use the stable terminal slot, not inherited pane IDs. The CLI matches
its process ancestors against Herdr's public `pane process-info` shell PID;
cached old pane bindings only prioritize that lookup. Piped tool subprocesses
work after a move without `HERDR_TERMINAL_ID` or a stdin TTY. A runtime that has
already resolved its stable terminal can supply `HERDR_TERMINAL_ID`. If no
identity can be proven, HAM fails closed rather than guessing from a stale pane.

Git HTTPS/SSH/scp origins normalize to `git:host[:nondefault-port]/Path`, preserving
path case. Set `git config ham.repo-id 'git:host/stable/repo'` before a configured
origin rename to keep that explicit identity. Without an origin, identity is
local to the canonical host and real repository root. Each delivery snapshots
the resolved recipient's repo at send time (`deliveries.recipient_repo_id`).
An occupant whose current repo differs from that snapshot holds queued mail as
`recipient_changed`; a new session in the snapshot repo can claim it, even
when the sender/thread belongs to another repo. Missing agents hold queued mail;
closed terminals make unclaimed mail undeliverable. Unknown claims are never
automatically replayed across crashes/session changes.

Schema v8 keeps `threads.repo_id` and `messages.repo_id` as the immutable thread
origin. `messages.sender_repo_id` snapshots each sender's send-time repo;
`deliveries.recipient_repo_id` snapshots each resolved recipient repo. Existing
rows backfill both snapshots from `messages.repo_id`, preserving legacy
same-repo semantics. New immutable wire payloads carry both `sender_repo_id` and
`recipient_repo_id` in the existing protocol envelope; the routed recipient
snapshot becomes the delivery repo. The new fields are integrity-bound without
invalidating legacy message hashes or receipts. Replies require the replier
to have sent or received a message in that thread (`not_participant` otherwise),
not repo equality.

Admission uses the recipient's repo mode/thresholds and own scoped role.
Idle/done readiness, hourly per-recipient limits, coalescing and max-hop gates
are unchanged. Cross-repo mail does not authorize a wake that same-repo mail
would fail. Upgrade all participating hosts together: older exact-field
receivers reject v8 messages as `invalid_message`. The originating outbox
retains the failed mail for manual retry after upgrade; it is not dropped or
delivered. Do not assume an old receiver automatically holds the new payload.
Preserve the message/item IDs and use `ledger retry --item OUTBOX_ITEM_UUID`
after upgrading, rather than creating replacement sends. A queued delivery held
as `recipient_changed` remains subject to the recipient-snapshot guard above.

Storage is `${XDG_STATE_HOME:-~/.local/state}/herdr-agent-messenger/ledger`
(directory 0700, database 0600, SQLite WAL). Use `HERDR_AGENT_LEDGER_DIR` for
isolated tests; `HERDR_AGENT_CANONICAL_HOST` sets a new ledger's fixed host name,
and reopening an existing ledger with a different name is rejected. Final
message bodies expire after 90 days; permanent compact hashes/receipts prevent
ID reuse even after deletion. Status/wake history expires after 30 days.
Initial WAL/schema/host-identity setup is serialized by a private ledger-local
bootstrap lock, including simultaneous first starts by separate CLI processes.

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
claim). `ledger runtime register|inspect|claim|applied` is the root OMP receiver
boundary: registration requires the full supported session-file path; `claim` is
one atomic bounded bundle (8 messages, 8 KiB, 2 KiB per body, every claimed ID
present) and a wake claim must retain a hop-eligible `action` or FYI with
`delivery.fyi_turn_end`, otherwise it returns `no_action` without claiming mail.
`inspect.has_action` includes flagged FYI for the unchanged extension's wake
decision; the persisted message kind remains `fyi`. `applied` ACKs only the exact session,
marker and content hash after the extension finds that custom message in the
public active-branch session history; that is host journal acceptance, not proof
of later provider transforms or model understanding.

Runtime installation is a separate, explicit per-host opt-in; plugin loading
and skill copying do not install adapters. See [receiving runtimes](#receiving-runtimes)
for both OMP and Claude ownership rules, commands and acceptance limits.

#### Mailbox terminology

- **Mailbox / inbox:** durable mail for a recipient's stable terminal slot.
  `inbox read` filters this ledger host, caller terminal and delivery recipient-repo snapshot matching its current repo:
  default `queued`, `delivering`, `delivered`; `--all` includes all retained
  states for that same recipient, and `--message UUID` can read a final message.
  Viewing never acknowledges application or work.
- **Thread:** the root message UUID is the thread UUID. `--reply-to` retains the
  parent thread/subject and increments its hop. `mail show --thread UUID` reads
  a known thread; `mail threads` discovers retained threads the repo participates in.
- **TUI Threads:** mixed repo-filtered sent/received threads (including remote
  pending copies) and direct results, plus host-wide pending/failed OUTBOX jobs.
  It is not just an inbox, a single chat transcript, or remote history replication.
- **Outbox:** per-peer FIFO transport jobs of kind `message`, `receipt`, `setting`
  or `withdraw`, with separate `pending/sent/failed/cancelled` transport states.
  `ledger outbox` can include completed jobs; the TUI selects pending/failed.
  Neither is a complete sent-mail folder or a direct-input history.
- **Message states:** `pending` awaits an authoritative recipient result;
  `queued` waits unclaimed; `delivering` reserves an exact agent/generation but
  may have unknown application (TUI `UNCERTAIN claim`, wire projects queued).
  `delivered` confirms application with `confirmation_source=native` or an
  audited `operator` resolution, not handling, provider success or understanding.
  Final states are `handled`, `withdrawn`, `undeliverable`. Remote pending,
  withdrawal and terminal closure mean neither kind must remain queued until a
  runtime claim. Direct `submitting/accepted/unknown/failed` is distinct transport
  acceptance, never a native mailbox ACK. `fyi`/`action` are kinds, not states.
- **Unread:** the board counts `queued` + `delivering` by delivery recipient repo,
  not thread origin or an actual-view flag; delivered-but-unhandled mail is excluded.

#### Hosts, FIFO transport and worker

Use the same protocol-capable checkout on both hosts and retain the existing
HAM concrete SSH-host allowlist. A peer is a canonical name+UUID with an SSH
alias and absolute checkout root; aliases remain valid addresses but never
become canonical host fields. Registration is bidirectional:

```bash
HAM_CLI="$PWD/.agents/skills/herdr-agent-messenger/scripts/herdr_agent_messenger.py"
python3 "$HAM_CLI" ledger setup --name alpha
python3 "$HAM_CLI" ledger doctor
python3 "$HAM_CLI" ledger peer add --ssh-alias beta-ssh --canonical-name beta
python3 "$HAM_CLI" ledger peer list
python3 "$HAM_CLI" mail send --to beta-ssh/blue-owl --body 'Inspect when ready.'
python3 "$HAM_CLI" ledger outbox --limit 100
python3 "$HAM_CLI" ledger worker status
python3 "$HAM_CLI" ledger retry --item OUTBOX_ITEM_UUID
```

Run the corresponding `setup --name beta` and reverse `peer add` on beta first.
Without `--remote-root`, discovery reads only structured
`herdr plugin list --json --plugin herdr.agent-messenger` →
`result.plugins[0].plugin_root`, then verifies `ledger doctor` protocol 1,
canonical name and UUID. `--uuid UUID` optionally pins the observed UUID.
An old installed plugin is not treated as a new mailbox endpoint. Updating a
registered root/SSH alias cannot silently replace its canonical name/UUID.

For isolated work-copy verification add `--remote-root /ABS/SNAPSHOT
--remote-ledger-dir /ABS/PRIVATE/ledger --remote-socket /SHORT/PRIVATE.sock`.
The local `HERDR_AGENT_LEDGER_DIR`, `HERDR_SOCKET_PATH`, HOME/XDG and legacy
request directory must also point to private resources. Set
`HERDR_AGENT_LEDGER_AUTOSTART=0` for finite transport tests: this suppresses
implicit local workers and is propagated to explicitly isolated remote ledgers.
Explicit `ledger worker once|run|ensure` still works. The optional remote socket
is retained in the peer mapping and inherited by that endpoint's worker; the
remote SSH config/allowlist remains the existing user's config. Never point a
verification command at the default production ledger or old installed root.

`ledger worker ensure` double-forks a code-only process; it rechecks the same
per-ledger flock as `run` / `once`. Startup/reconcile events and mutating CLI
commands ensure a worker only when maintenance is pending. Events themselves
are finite one-shots. It owns FIFO outbox retries, receipt transport, terminal
sweeps and retention; it exits when none remain. Failed Herdr reads do not
invent terminal closure. Worker log/PID/lock stay within the ledger directory.
There are no model calls, prompt submissions or AGENTS database replication.

FIFO is per destination host. A transient SSH/unavailable/busy error blocks
that peer until exponential backoff (capped at five minutes), not other peers.
A permanent endpoint/protocol error marks that item `failed`, leaves the JSON
error visible and lets later items progress. `ledger retry` retries the same
outbox/message IDs; it never creates a replacement message. Lost ACKs replay
the immutable payload and return its **current** resolved receipt. Hash
conflicts are refused even after 90-day body deletion; compact receipts and
withdrawal tombstones are permanent. Lower/reordered receipts never regress
state, a final state cannot conflict-transition, and a resolved recipient
terminal/address cannot change. Internal `delivering` is published as queued.

A bell write can fail **after** a successful DB commit. Such a command still
succeeds, with `warnings:[{code:"notification_failed",committed:true,...}]`;
`doctor.notification_failures` retains the diagnosis. Do not resend with a new
message ID. Events/safety sweeps recover notification loss.

After startup/event registry refresh, HAM shows one best-effort Herdr notification
only when an active local OMP/Claude session has `extension_present=0` and that
client's default runtime is missing or has an ownership error. Registered
receivers do not trigger default-path inspection or warnings (including custom
install/config paths). The last notified `(client,state)` set is stored in
`ledger_meta`; unchanged refreshes use plain reads, and changed sets are checked
again under the write transaction before claiming them. A changed or
resolved-then-missing set can notify again. Notification failure is recorded
without failing reconciliation and is not retried until the set changes.
Installed-but-unregistered sessions and Codex do not trigger this startup notice.

#### Wire and settings service

SSH executes `python3 /ABS/CHECKOUT/ledger_cli.py ledger COMMAND`, sending one
JSON envelope on stdin (1 MiB limit):

```json
{"protocol":1,"transport_host":"alpha","transport_uuid":"HOST_UUID","payload":{}}
```

The receiver validates a registered canonical host+UUID, not a human source
label. Success is one JSON object with `protocol:1`; errors retain
`{"error":{"code":"CODE","message":"TEXT"}}`, exit 1. Supported wire commands:

| Command | `payload` / result |
| --- | --- |
| `accept` | `{message:IMMUTABLE_MESSAGE,resolved:{recipient_host,recipient_terminal_id,recipient_address}}` → current receipt. New sends always carry a concrete terminal target matching the immutable target, including sends addressed to `main`. Legacy symbolic sends and relays fail with `legacy_symbolic_main`; upgrade every host before using `--to main` across hosts. |
| `receipt` | Current compact public receipt, including `message_id`, `origin_host`, `payload_hash`, resolved recipient fields, `state`, `authoritative_host`, times and `withdraw_requested` → retained nonregressing receipt. Only the saved downstream route can advance the origin/relay copy. |
| `withdraw` | Original receipt identifying `message_id`, `origin_host`, `payload_hash` and target → withdrawn/current receipt. Unknown IDs create a permanent hash-bound tombstone before late acceptance only when sent by the message origin. |
| `setting` | `{repo_id,field,value,version,origin_host,changed_by,changed_at}` → `{applied,...repo_settings}` for repo-wide `home`, `mode`, `thresholds`. Protocol-1 legacy `main`, `members`, `computed_main` payloads (including an old conditional `expected_main`) succeed as no-ops. Workspace settings never use this endpoint for replication. |
| `status-export --wire` | Optional `{repo_id,refresh,resources}` → `{host:{canonical_name,uuid},agents:[...],resources?:{...}}` with roles and optional resource fields. Default refresh gets a fresh local snapshot; `refresh:false` reads stored rows without registry/status/last_seen/display/claim mutation. Only `resources:true` opts into server discovery and adds agent `servers` plus host `resources`; `status board` sends both flags. Missing/false `resources` avoids scans for send/policy/recipient lookup and leaves those fields unknown. Plain `status-export [--repo ID]` remains a fresh local export without server discovery. Remote rows are never inserted into local AGENTS. |

The Python settings service is
`RepoSettings(store).show(repo_id,host,workspace_id)` and
`mutate(repo_id,field,value,*,host=None,workspace_id=None)`, reserved for the user
Settings UI, with **no agent mutation CLI**. `main` and `members` mutations
require an explicit local host/workspace request, resolve its anchor role group,
and reject inactive or out-of-group targets. `main` is `null` or `{host,terminal_id}`; `members` is a
unique list of those targets. `mode` (`off|manual|auto`) and `thresholds`
(`idle_minutes:3,coalesce_seconds:30,wakes_per_hour:6,max_hop:10` defaults)
remain repo-wide, as does read-only `home`.

`fields(connection,repo_id)` contains only repo-wide fields.
`scope_workspace` centrally resolves the requested workspace to its anchor;
`scope_agents` selects the active repo/host agents across that role group.
`workspace_fields(connection,repo_id,host,workspace_id)` reads anchor settings;
`main(connection,repo_id,host,workspace_id)` selects that group's main.
`show` reports `workspace_id` as anchor and `requested_workspace_id` as original.
`role(agent,connection=None)` shares that calculation across runtime, status
export, sidebar and borders. Replicated fields independently win by
`(version,origin_host)`; same-version different values record the deterministic
canonical-name winner and conflict. Different fields' concurrent edits survive.
Main/member settings stay host-local and never enter peer publication or
repo-wide conflict/member projections. Peers consume origin-host computed roles;
additive role-scope export metadata is optional under unchanged protocol 1.

First enabling a repository creates a version-1 home proposal; **offline peers
do not prevent local use**. Observed concurrent home proposals converge by
canonical-name tie order. Home is not the authority for workspace roles.
Main election is derived on read, not stored or published as `computed_main`.
A valid explicit main must be active in the same repo/host/anchor role group.
A target that becomes inactive or moves out is ignored without rewriting the
setting. Auto election otherwise requires an active agent in the anchor:
the sole active group agent wins, or, with several, the unique active
main-checkout agent. An open anchor with no agent yields no auto main even if
only one linked agent exists. All other auto group agents remain workers.

`--to main` resolves on the sender's host at send time against the sender's
current scope and stores the concrete terminal target in the immutable message.
Missing main produces `main_unresolved` naming the scope; there is no fallback
to a repo-global main, another role group, or an offline home route.
Receipts and withdrawals follow the saved target. Explicit named mail crosses
both workspaces and repositories. `[HOST/]main@REPO` selects the unique main
across distinct role groups on the chosen host, as described under
**Recipient selectors**; it does not change plain `main` semantics.
Legacy unresolved symbolic messages, including remote-origin sends and home
relays, fail with `legacy_symbolic_main` and an instruction to upgrade every
host. Upgrade and run `ledger reconcile` on every host before using these routes.

#### Workspace role migration
Schema v9 adds a host-local `workspaces` table with `host`, `workspace_id`,
`repo_key`, `is_linked_worktree` and `label` from the open Herdr snapshot,
including workspaces with no agents. For the same repo and host, linked
workspaces join a deterministically selected open nonlinked workspace with the
same Herdr `worktree.repo_key`. No metadata or no open anchor means their own
workspace scope. There is no git-common-directory fallback for plain workspaces.
Anchor opening/closing or group membership changes refresh affected roles.

Only the anchor's explicit main/member settings apply while grouped. Existing
linked-workspace settings are neither migrated nor copied nor deleted: they
remain unchanged, ignored while grouped, and apply again when ungrouped.
An explicit main may be any active agent in the group. Mode, home and thresholds
remain repo-wide. All main/member reads, mutation validation and recipient
resolution use the same group.

Back up the database before upgrade. Stop old HAM processes sharing the host
ledger, update host code and copied skills together, run upgraded `ledger reconcile`
to migrate writable storage to the current schema (v10, including v9 workspace metadata), then restart the host's HAM processes.
Read-only opens do not migrate. Protocol 1 is unchanged and old peers may consume
computed roles without optional scope metadata, but still compute old semantics
for their own agents. Old code cannot open schema-v9 storage: do not mix versions
on one host ledger. Downgrade requires restoring the backed-up database.
OMP extension and Claude hook contracts are unchanged.

Schema v10 adds `deliveries.fyi_turn_end INTEGER` with default 0. Existing v9
deliveries remain conservatively unflagged. Upgrade the ledger and CLI/copied
skills together so old enqueue code cannot omit the flag; run upgraded
`ledger reconcile` before read-only opens. The OMP extension is unchanged and
needs no restart for this policy. See [turn-end FYI migration](../../../docs/migration.md#turn-end-fyi-schema-v10).
See [current migration and rollout](../../../docs/migration.md#worktree-role-grouping-schema-v9).

The following v7 migration is historical; v9 supersedes its independent
workspace role semantics:


Schema v7 creates local workspace settings. During v6→v7 migration, legacy
repo-level main/member targets matching active local agents are mapped to each
agent's current scope; members are grouped by scope. Other hosts migrate their
own targets independently. Unmappable inactive/unknown local targets are dropped;
`ledger_meta.workspace_settings_migration_dropped` records JSON
`[{repo_id,field,target},...]`. Obsolete repo-level `main`, `members`,
`computed_main` rows and the old `repo_members` / `setting_conditions` tables
are removed. Protocol remains 1:
incoming legacy role fields succeed without applying changes, so older peer
outboxes do not fail permanently. New code publishes only repo-wide settings.
See [historical v7 migration](../../../docs/migration.md#workspace-role-migration-schema-v7).

### Modes, roles, settings and status

`repo show` is read-only. After first-registration defaults, only the user Settings
UI changes repository participation; there is no `repo set`, main/home mutation
command, or public policy-bypass flag.

Repos first seen by HAM from this version default to `auto`, including host-local
`local:` repos (whose settings do not propagate). All same-repo agents participate;
eligible queued action mail may auto-start an OMP turn, and same-repo legacy
`send`/`request`/`batch` must use mail. Already-known repos retain all stored
settings; missing mode remains `off` until changed. Reads, including board, inbox,
`repo show`, and status export with `refresh:false`, never seed defaults.

First registration atomically stores home plus auto through the settings protocol.
The mode seed has version 1, origin `!default`, and `changed_by:home`: protocol-1
peers (including 0.9.1) accept it, and every explicit canonical-host mode at version
1 or higher outranks it. Old peers may still log a differing version-1 value as a
conflict, with the explicit value winning. Home retains the existing version-1
canonical-host proposal; concurrent/offline proposals converge lexicographically,
not by wall-clock age, so an existing remote home can lose that tie. Offline
hosts may temporarily disagree until settings propagate; there is no home failover.

| Mode | Main | Workers / receiving |
| --- | --- | --- |
| `off` | None | No roles or automatic wakes, and no participation-based legacy rejection. Explicit inbox reads and natural user-turn receiving remain allowed. |
| `manual` | Active explicit user-designated terminal in the same repo/host/anchor role group, otherwise none | Anchor's explicit group members except main. Nonmembers can receive at natural boundaries but do not wake automatically. |
| `auto` | Active group explicit designation first; otherwise requires an active anchor agent, then the sole active group agent or unique active main-checkout agent | All other group agents, even without a main. An open no-agent anchor prevents auto main even with one linked agent. Ungrouped workspaces keep independent election; host IDs never collide. |

The stable terminal slot survives a pane move; roles follow its current scope,
and displays in both the old and new scopes are refreshed. A new native
session/client/repo changes generation. Agents keeps role before the addressable alias. HAM-owned
borders show `[메인] alias · summary` or `[워커] alias · summary`, omitting
missing parts and preserving manual user renames. Alias precedes summary so
Herdr's width truncation clips the summary first. `status clear` removes
phase/summary but retains alias and any active role. Phase supplies the HAM
`recap` token. Herdr's turn state is stored unchanged.
`status set`/`clear` also work off, without a role prefix; invoking this feature
removes the obsolete `user:pane-recap` token, preserves device projection and
leaves unrelated token keys untouched. Herdr's per-pane token map is flat;
metadata sources do not isolate identically named keys.

### Device claims and resource fields

```text
device claim phone|emu DEVICE [--label TEXT] [--force]
device release [phone|emu|all] [DEVICE]
device list
```

These use the bundled launcher and return one JSON object; domain errors are
`{"error":{"code":"CODE","message":"TEXT"}}` (exit 1), usage errors exit 2.
`phone` means a real device, including iOS when explicitly identified/labeled;
`emu` accepts a number or `emulator-N` and normalizes to `emulator-N`.
Without `--label`, a phone uses an adb model lookup with ID fallback, and an
emulator uses its number. Claim before device use and release afterward.

Claim/release refresh the registry, resolve the caller's stable terminal/session
identity and recheck its generation under the terminal lock. They never trust an
inherited pane ID alone. Unresolved callers or failed Herdr snapshots refuse
mutation; a refresh failure never means all holders are gone.
Claims belong to the agent's host+terminal+generation. A holder is live only
while its agent row is active in the current generation. Inactive/replaced
holders are stale, independent of whether the physical device is attached.

- Reclaiming your own device updates its label idempotently.
- Another live holder returns `device_claimed` with alias/pane/host/claimed_at.
  `--force` replaces it **only on explicit user instruction**.
- A stale holder can be replaced without force, with `replaced_stale:true`.
  Claim/release and registry-refresh mutations prune stale inactive claims;
  read paths never prune them.
- `release` defaults to `all` and releases **only the caller's own claims**.
  `release phone`, `release emu`, or a kind plus device narrows the selection.
  There is no release force option.

| Operation | JSON result |
| --- | --- |
| claim | `{device:{...},replaced:{...}\|null,replaced_stale:bool}` |
| release | `{released:[...]}` |
| list | `{devices:[{device_id,kind,label,claimed_at,holder,stale,attached}]}` |

`holder` is `{agent_id,alias,pane_id,terminal_id,generation,active,live}`.
`attached` is `true` for Android adb state `device`, otherwise `false`; it is
`null` when adb is unavailable or the ID is not adb-like (for example an iOS
UDID). adb lookups have bounded timeouts. `device list` has no registry refresh,
token writes, claim cleanup or directory-creation side effects.

HAM publishes tokens from the claim table via source `herdr.agent-messenger`,
with exact values `phone=📱 <labels space-joined>` and
`emu=🤖 <labels space-joined>`. It clears the corresponding token key when
there are no claims, preserves these values during status/role refresh and
reprojects both affected live panes after force replacement.
No legacy device JSON is imported by product code; any one-time rollout
migration happens outside the product. `heavy_slot.py` is a separate,
unchanged script and is neither read nor displayed by HAM.

`status board --verbose` and status-export payload agent rows add independently
optional `devices` and `servers` lists. `devices` contains held nonstale claims
(`stale:false`). Each server is
`{port,bind,pid,process,cwd,started_at,urls,inherited}`. Discovery reads current-user
Linux/macOS listeners and Tailscale Serve routes; ownership uses the active
agent on that host/pane. Plugin processes, adb and agent runtimes are excluded.
`inherited:true` means the listener started before `agent.first_seen`.

When requested with `resources:true`, status-export adds payload-level `resources`;
board requests it and places it on each host:
`{servers_unowned:[],dead_serve:[],stale_devices:[],error:null}`.
`servers_unowned` lists proxy routes with a backend listener but no agent owner;
`dead_serve` lists proxy routes with no backend listener, checking all users'
listeners and all binds; `stale_devices` lists
stale claims. Discovery failures populate `resources.error`, not invented empty
success. Agent devices/servers and host resources remain independent: an old
peer omitting any one field reports unknown (`?`) only for that field, not empty
or zero. The strict wire envelope is unchanged; resources live inside payload.

Board/TUI resource reads never refresh registry/status/display/claims or prune
stale claims. Agents shows centred PHN/EMU/SRV phone, emulator and server counts
before PHASE · SUMMARY. Full-width tables keep all columns when their minimums
fit, shrinking spare alias/role width first. Zeros show a dim `·`; missing fields
independently show `?`. Host labels retain agent counts and nonzero or unknown
dead/unowned/stale-device findings, truncating findings first. Agent Detail
groups Devices and Servers under Devices & servers, with `📱 label` (for example,
`📱 SM-A245N`) before technical IDs. Empty lists and resource errors stay explicit.
This listing does not claim servers or change device ownership.

### Mailbox TUI and operator recovery

Open the plugin action without changing a global binding:

```bash
herdr plugin action invoke mailbox-split --plugin herdr.agent-messenger
herdr plugin action invoke mailbox-tab --plugin herdr.agent-messenger
herdr plugin action invoke mailbox-popup --plugin herdr.agent-messenger
```

Opening checks pane/terminal IDs, not labels; a shell opener is supported.
The split action reuses the registered mailbox only in the opener's tab.
Otherwise it opens and registers a right split, closing the old mailbox only
when its visible first line contains `NAV ·`; active drafts and overlays stay open.
The tab action still reuses the workspace's live mailbox across tabs.
The **HAM: Mailbox (quick send)**
popup closes after a successful send result. `q` closes only the owned pane or
popup, confirming before discarding nonempty drafts (default Cancel). Top-level
Esc closes only popup mode; Esc leaves input/overlays without discarding drafts.

Responsive **Agents**, **Threads**, **Detail** regions use stacked lists above
full-width Detail with at least 80 columns and 18 available body rows. Lists
are side by side only when each half fits the complete Agents column model,
including PHN/EMU/SRV and the 16-cell summary minimum. The threshold derives
from current alias/role widths, gaps and borders (158 columns for a 15-cell alias
and minimum four-cell ROLE), not a fixed breakpoint. Otherwise one full-width
list has `Agents │ Threads` tabs: active is accent/bold, inactive dim.
Click a tab label to switch without opening input. Each tab preserves selection
and scroll. Detail gets about 40% of usable body height, at least nine outer
rows (six content rows, notice, borders), leaving eight outer rows for the list/tab bar.
Header, footer, status and full-width INPUT below Detail are excluded first.
Below 80 columns or 18 body rows, only the focused region appears; the unchanged
Agents → Threads → Detail cycle replaces the list with Detail and retains the
tabs for returning to a list. Below 32 columns or 10 terminal rows, resize
instructions appear without losing mode, focus, selections, text or cursor.
NAV keys: Tab/Shift+Tab, ←/→ or h/l changes focus and the active list tab;
Detail retains the last list's tab. ↑/↓ or j/k selects rows;
PgUp/PgDn/Home/End pages or jumps. Press `Space` on an Agents or Threads item,
including DIRECT/OUTBOX, to open a centered read-only Detail viewer (~90% screen,
maximum 100×40). Arrows, PgUp/PgDn, Home/End and wheel scroll; `t` toggles
technical information. It opens at the latest message and follows new arrivals
only while at that position. If the item disappears on refresh, it closes with
a status message. Esc, q, Space, or outside click closes; inside click and Enter
are inert. Viewing never ACKs. Detail Enter otherwise does nothing. Digits never
navigate; `s` opens Settings, `?` help, `e` diagnostics and `r` refresh.

Agents groups single-line table rows under host headers, showing turn, alias,
mail, receiver, role, devices/servers and phase/summary as space allows.
Disconnected peers retain stale snapshots with unread `?`, not closed agents.
When any shown auto/manual repository has active local OMP/Claude sessions
without registered receivers, the existing notice row persistently shows their
count and `runtime status` remedy (also available in Detail/help). All-repository
scope checks each local repository's mode; remote rows and Codex are excluded.
Detail previews the last-focused list. Agent Detail is single-column, grouped
as Work (turn, phase, bold summary), Identity & role, Devices & servers, Mail,
then dim Receiver/technical with full fields and separate status-update and
observation timestamps. Mail contains counts and the Threads/Space reading hint,
not bodies. Thread Detail keeps message history and optional technical fields.
Threads includes messages, direct submissions and OUTBOX.
**Viewing is read-only and never ACKs, delivers, handles or resolves mail.**

Agents Enter opens inline mail input; Threads Enter opens a reply to its latest
message, resolving the recipient by host/terminal to a live, non-stale agent.
Stale/unresolvable recipients are blocked; absent native receivers warn but
allow mailbox sending. Detail Enter does nothing. UI mail has `sender=user`,
no subject. Every new compose, including replies and reopened drafts, resets to
`action`; Ctrl+G toggles `action` / `fyi` for that compose. The selected kind is
sent explicitly; CLI default remains `fyi`. Enter sends immediately without
confirmation; blank bodies fail without sending. Shift+Enter inserts a newline using the mailbox's
kitty keyboard protocol request, including through herdr in compatible terminals.
Alt+Enter is the fallback for Moshi/mosh, which cannot negotiate that protocol.
Ctrl+J is not a newline key. `HAM_MAILBOX_NO_KITTY=1` disables the protocol request.
Printable characters (including q, ?, and digits) are text. Arrows/Home/End/
Backspace/Delete edit; up/down moves between lines. Esc preserves text/cursor
per recipient and reply; Ctrl+R refreshes in place; F1 opens help while typing
and returns to that input. Successful sends close input, clearing only the
unchanged submitted draft, and show a row result tag; failures preserve input.

Ctrl+D toggles direct terminal input. Enter requires confirmation with Cancel
selected by default; Tab or ←/→ selects a button and Enter executes it.
Direct may interrupt: it submits the exact `Message from user (HAM):` header
and records `via=direct`, `submitting` then `accepted|unknown|failed`, never a
native mailbox ACK. Retain unknown results and inspect before explicit resend.

Settings (`s`) has exactly seven editable rows:

| Row | Value / default |
| --- | --- |
| `mode` | `off`, `manual`, `auto`; newly seen repos default to `auto`; already-known repos without mode keep `off`. |
| `main` | JSON `{"host":"CANONICAL","terminal_id":"term_…"}` or `null`; null removes the explicit designation. |
| `members` | Unique JSON array of canonical-host/terminal targets; default empty. |
| `idle_minutes` | Integer 0–10000, default 3 minutes after input. |
| `coalesce_seconds` | Integer 0–10000, default 30 seconds to collect mail. |
| `wakes_per_hour` | Integer 0–10000, default 6 per agent/hour. |
| `max_hop` | Integer 0–10000, default 10; action above the cap cannot wake. |

Mode and thresholds apply repository-wide. Main/Members rows identify the
Mailbox opener's workspace on its canonical host, resolved from
`HERDR_HAM_OPENER_PANE_ID`, rather than the selected agent's workspace. Their
scope is the full `(repo_id,host,workspace_id)`. All-repositories view retains
repository selection; without one repo, settings is disabled with an
explanation. The Main picker includes only active in-scope agents and `(none)`.
Refreshes reuse the live Herdr snapshot to resolve a fresh shell opener or,
if it has closed, the Mailbox's own workspace. If neither workspace is known,
only Main/Members are unavailable; repository Mode/Thresholds remain editable.

↑/↓ selects a row and Enter edits its value; all mutations require confirmation.
The overlay shows `Target: repository REPO_ID · workspace WORKSPACE_ID @ HOST`.
For the selected Agents row, `m` designates it main and Space toggles manual
membership, only if the active target belongs to
the opener's full settings scope. The service rejects inactive/out-of-scope
targets. Esc closes Settings. The four threshold rows edit one repo-wide
versioned `thresholds` field, preserving its other values. Home, computed main,
version/origin stamps, pending propagation and conflict records are
**intentionally read-only**. Home is the first registration/activation proposal,
subject to deterministic concurrent convergence, not an omitted eighth setting.
Workspace main/member saves do not propagate; offline peers do not block local use.

Only one settings mutation is in flight. **Saving settings** is visible; a
second save/toggle is rejected, not queued. Wait for completion and retry the
intended submission. An open edit buffer is retained while its submit is gated.
Navigation and `q` outside text entry remain responsive. The authoritative
result is matched to the full repo/host/workspace scope and shown immediately;
obsolete or other-scope snapshots cannot overwrite it. Repo-wide conflicts show
complete wrapped winner/loser values and stamps; a user edit publishes a newer
version to choose the intended value. PgUp/PgDn expose long metadata;
NAV `e` shows full diagnostics.

Threads `w` withdraws eligible locally originated mail and `t` retries the selected
OUTBOX identity, each confirming the exact target. An uncertain `delivering`
claim is **UNCERTAIN** and is never automatically replayed, even across session
replacement. `a` (applied) / `n` (not-applied) first collects nonempty operator
evidence, then confirms the exact message/claim ID, old agent, generation and
current state. Cancel is the default; Tab or ←/→ selects and Enter executes.
Applied records **operator** confirmation of that old claim, not native journal
evidence, model understanding or handled work. Not-applied releases exactly the
old claim for later ordinary receiving; it does not submit a prompt. `Esc`
cancels. Never call internal `started` just because text was read or a hook
returned success. See the [UI reference](../../../docs/ui-reference.md).

### Receiving runtimes

The plugin provides the ledger, mailbox, UI and transport; the skill provides
instructions and the launcher. Neither alone supplies native receiving. Runtime
installation is a separate, consented, ownership-checked per-host opt-in.
Plugin loading never modifies OMP or Claude configuration, and runtime setup
does not change the new-repository `auto` default.

#### Native mailbox bundles

The compact OMP/Claude coordination prefix retains user priority, turn-boundary
work, view-only/no-ACK reads, explicit completion handling, useful replies and no
receipt/ACK loops. It directs replies to use `action` to enable work
(answers/decisions), `fyi` otherwise. Headers retain every message ID, sender,
kind, thread and hop. When the sender repo differs from the recipient repo,
`From` adds its short repo name (last path component) before the kind, for example
`From macbook/purple-deer (repo monitor_mobile) [action]`.
The random marker and exact content/hash support native journal evidence, not
proof of provider success, understanding or completed work.

Bounds remain 8 messages, 2,048 UTF-8 bytes per body, 8,192 bytes per bundle.
Pull missing/truncated context with `inbox read --message UUID`; do not reread
complete bodies solely because native mail arrived. Budgets include actual
prefix/header bytes. The review's fixed marker/prefix fixture is now 415 bytes
(formerly 360); one/three ASCII `OK` messages are 542/796 bytes (formerly 487/741),
for same-repo messages; the example's ` (repo monitor_mobile)` adds 22 UTF-8
bytes within the same bundle limit.

This source change does not alter the OMP extension or Claude hook; it does not
require receiver reinstall or session restart. Upgrade the plugin/skill on all
participating hosts together for the v8 wire support described above.

#### Runtime installation and health

On **each selected host**, inspect the read-only health summary first, then
install/check/remove only the client the user selects:

```bash
python3 scripts/herdr_agent_messenger.py runtime status
python3 scripts/herdr_agent_messenger.py runtime install omp
python3 scripts/herdr_agent_messenger.py runtime status omp
python3 scripts/herdr_agent_messenger.py runtime uninstall omp
python3 scripts/herdr_agent_messenger.py runtime install claude --destination /ABS/CHOSEN/CLAUDE_CONFIG
python3 scripts/herdr_agent_messenger.py runtime status claude --destination /ABS/CHOSEN/CLAUDE_CONFIG
python3 scripts/herdr_agent_messenger.py runtime uninstall claude --destination /ABS/CHOSEN/CLAUDE_CONFIG
```

With no client, `runtime status` prints one health-summary JSON object:

- `clients`: OMP/Claude installation records, each with `client`, `destination`,
  `installed`, `owner` (or `null`), and `error` (`null` or `{code,message}`).
  A foreign/modified runtime is reported in that client's error; it does not
  suppress the other client's result.
- `sessions`: active **local** agent counts, keyed by `omp`, `claude`, and
  `codex`. Each has `receiver_registered` (`extension_present=1`) and
  `receiver_absent` counts. Counts are `null` when ledger data is unavailable,
  not an invented zero. Codex's absence is normal: it pulls with the skill and
  has no receiving adapter; never report that as broken.
- `ledger`: `{available:bool,error:null|{code,message}}`. A missing/uninitialised
  ledger or one requiring an upgrade is reported without creating, migrating,
  refreshing or otherwise writing it; installation results remain available.
- `next_steps`: human-readable remedies for clients with active local sessions
  (missing runtimes, conflicts or unregistered receivers), plus unavailable
  ledger data. Unused clients remain in `clients` but have no install suggestion.
  Ask the user before `runtime install <client>`, then restart only the selected
  sessions.

`runtime status omp|claude` retains its existing client-specific output shape.
`install|uninstall` requires positional `omp|claude`; all three client-specific
operations accept `--destination DIR`. A no-client summary cannot use
`--destination`. OMP defaults to `~/.omp/agent/extensions`; Claude defaults
to `CLAUDE_CONFIG_DIR` when set, otherwise `~/.claude`. OMP owns only
`ham-mailbox.ts` and `ham-mailbox.install.json`, not Herdr's own extension.
Claude owns `hooks/ham-mailbox.py`, its manifest and exact `SessionStart` /
`UserPromptSubmit` entries merged into `settings.json`. Unrelated settings/hooks
survive install/uninstall. Ownership/checksum checks refuse foreign, modified
or symlinked targets rather than replacing them. Restart/reload only sessions
the user selects; neither installer silently enables all running sessions.
Installed files are not proof of activation or context admission. After the
selected sessions restart, confirm their receiver registration
(`extension_present=1`); otherwise report **installed, activation pending**.

**OMP:** only a supported root Herdr session registers. The adapter discovers
the current structured plugin root (or explicitly configured absolute
`HERDR_AGENT_MESSENGER_ROOT`) and calls Python asynchronously; it never opens
SQLite. Natural turn boundaries can receive both kinds, including off/manual
nonmembers. FYI enqueued while idle never wakes the recipient. Automatic action
or flagged turn-end FYI wake requires a
participating recipient role, matching recipient-repo snapshot/generation, TUI mode with supported empty-editor
evidence, configured input quiet time, Herdr `turn_state` of `idle` or `done`, an idle OMP runtime/no pending work, no continuation or
retry hold, and coalescing/hour/hop admission. Pane focus is not a wake gate.
Final conditions and agent-attributed
`sendMessage(...,{deliverAs:"nextTurn",triggerTurn:true})` occur in the same
synchronous tick, never steering an ongoing turn; unavailable editor/RPC/ACP
proof does not become a wake. Bell events and a five-minute code-only safety
check drive inspection. ACK requires exact marker/content hash in the same
session's public active-branch journal; later provider transforms or model
understanding are not asserted.

**Claude Code:** opt-in bounded `SessionStart` / `UserPromptSubmit` context
hooks only, with no Stop extension or idle wake. Outside Herdr, the adapter
returns before consuming input, discovering a plugin or touching the ledger.
Herdr environment signals activate it but do not establish identity: supported
root UUID/transcript, actual foreground process/ancestry and fresh generation
checks must prove the session. A cold unreported identity can be bound only
after those checks; a newer reported UUID is not overwritten. Unresolved
registration leaves queued mail; uncertain application stays held. Hook
stdout, `additionalContext` and exit 0 alone do not confirm delivery. Native
confirmation has two supported evidence paths:

- Exact same-session SDK system-context bundle plus fresh `api-request.tail`
  correlation, recorded as `claude:api-request:...`.
- Typed native hook raw/rendered exact claim bytes and hash, with a causal
  same-session native request witness, recorded as
  `claude:accepted-journal:SESSION#REQUEST;hook=CTXUUID;sha256=HASH`.

The second path proves **host-context application**, not exact provider-wire
bytes, provider attestation, model self-report or understanding. Both use
`confirmation_source=native`; operator resolutions remain `operator`. A closed
uncertain case is not retroactively acknowledged after an observer change.

**Codex:** install the current skill and pull at relevant work boundaries:
view-only `inbox read`, perform relevant work, then explicit `inbox handle`.
There is no idle wake or automatic native ACK adapter. Read-only subagents
sharing a terminal cannot manufacture delivered receipts.

Native bundles represent every claimed ID, at most 8 messages / 8 KiB total
with at most 2 KiB per body and per-ID instructions to read truncated text.
Receiving/admission evidence is distinct from handling or provider success.
A bounded actual Claude TUI 2.1.301 run observed production SessionStart context
and a current-generation worker's persisted native `via=hook` acceptance using
the typed journal path. In the same registered process, manual-worker and
auto-main idle windows of about 6.02 seconds each, with queued action/FYI mail
and an active worker, produced no extra primary turn or claim. This is not a
zero-provider-traffic claim: auxiliary quota-check/prompt-suggestion requests
are distinct. The separate mail-only nonce response was a functional check,
not ACK evidence. Herdr already knew this run's UUID; unreported-identity
bootstrap CAS has unit coverage, not actual-live acceptance from this run.
Actual OMP/physical SSH integration checkpoints are separate. Provider/session
failures do not mean successful processing; no blanket PASS or deployment is
claimed.

### Ledger management command flags

These use the same bundled launcher and accept `-h` / `--help`, with no
`--verbose`. Internal JSON services are adapter/transport interfaces, not an
agent settings or ACK escape hatch.

| Command | Arguments / behavior |
| --- | --- |
| `ledger setup` | Required `--name NAME`; create/check fixed canonical identity, never rename an existing store. |
| `ledger doctor` | No flags; local identity/schema/counts/notification diagnostics without contacting Herdr. |
| `ledger peer list` | No flags; canonical UUID, SSH alias, root and isolation overrides. |
| `ledger peer add` | Required `--ssh-alias ALIAS`; optional `--remote-root ABS_PATH`, `--remote-ledger-dir ABS_PATH`, `--remote-socket ABS_PATH`, `--canonical-name NAME`, `--uuid UUID`. Discover/verify protocol/root/identity before registration; register reverse direction separately. |
| `ledger status-export` | Optional `--repo ID`, `--wire`; fresh local status/roles. `--wire` requires authenticated JSON stdin. No remote AGENTS replication. |
| `ledger outbox` | Optional `--limit N`, default 100, range 1–1000; host-wide pending/sent/failed/cancelled transport jobs and errors, not repo sent-mail history. |
| `ledger retry` | Required `--item UUID`; retry that durable item, not a new message. |
| `ledger worker ensure\|run\|once\|status` | No extra flags. Ensure detached singleton, run foreground, execute one bounded pass, or inspect worker state. |
| `ledger reconcile`, `ledger event` | No flags; fresh complete serialized Herdr snapshot, not a partial closure inference. Event is one-shot. |
| `ledger retention` | No flags; expire final bodies after 90 days and status/wake logs after 30; preserve dedupe receipts/tombstones. |
| `ledger accept\|receipt\|setting\|withdraw` | No argv payload flags; authenticated protocol-1 JSON stdin as described above. |
| `ledger agent-session` | JSON stdin native session registration, not a guessed display/session revision. |
| `ledger delivery claim\|started\|release` | JSON stdin exact claim/agent/generation and actual application/release evidence. |
| `ledger runtime register\|inspect\|claim\|applied\|claude-hook` | JSON stdin receiver registration/admission/native journal verification or bounded Claude hook request. Adapter-owned evidence only. |

The open board stats the local bell each second and independently queries peers
every 30 seconds in cancellable bounded processes. OMP's five-minute safety
check and the per-host worker's FIFO transport/backoff/sweep/retention are
code-only; none is a model-polling loop. Native processing of an eligible action
is a separate turn and can spend tokens. Worker maintenance may remain pending
while active terminals need observation; it exits when no maintenance remains.

### Deployment order and current scope

Upgrade the protocol-capable HAM plugin on **each host first**, then install/
verify the matching skills and opt-in receiving runtimes for the chosen
sessions **before enabling participating repository policy**. Only afterward
may the user bind `prefix+m` to the new mailbox action or replace global recap
rules with `status set`. Plugin loading does not mutate global client/binding
configuration. Keep nonparticipating legacy functionality during the rollout.
These are deployment instructions, not a claim that this checkout was
installed, globally activated, merged, pushed or released.


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

Repository status uses distinct ledger subcommands:

```bash
python3 scripts/herdr_agent_messenger.py status set review --summary 'Checking mailbox contracts'
python3 scripts/herdr_agent_messenger.py status clear
python3 scripts/herdr_agent_messenger.py status list --repo
python3 scripts/herdr_agent_messenger.py status board
python3 scripts/herdr_agent_messenger.py status board --repo --host local
python3 scripts/herdr_agent_messenger.py status board --repo 'git:host/stable/repo' --host REGISTERED_SSH_ALIAS
```

| Subcommand | Arguments / result |
| --- | --- |
| `status set` | Required positional `phase` and `--summary TEXT`; no `--phase` flag. Reports the current agent's public phase/summary. |
| `status clear` | No extra arguments; clear current phase/summary. |
| `status list` | Optional `--repo [ID]`, `--verbose`: absent repo lists all local repository statuses, bare `--repo` selects caller's repo, explicit ID filters. Brief rows/counts by default; verbose restores full rows. |
| `status board` | Optional `--repo [ID]`, `--host local\|CANONICAL\|REGISTERED_SSH_ALIAS`, `--verbose`. No repo flag selects all repositories; bare `--repo` resolves caller's repository; explicit ID filters without fetching Herdr caller identity. No host flag selects local plus all registered peers; explicit host selects only that ledger. Returns `{repo_id,agents,hosts}`. |

These subcommands accept `-h` / `--help`; board/list also accept `--verbose` after the subcommand or on parent `status`. `status board`
has its own host selector; do not treat legacy `status --host HOST list` as
remote board syntax. Legacy `list` and stored local `status list` remain
available with narrower semantics.

#### Status board output and fields

Compact board/list rows have `address` (`canonical-host/alias`, terminal fallback),
`host`, `workspace` (label, ID fallback), `repo` (short repo name), and existing `turn_state`, `status`,
`role`, `unread`, `phase`, `summary`, `stale`, `extension_present`, `client`.
Row `repo_id` appears only in multi-repo results; optional devices/servers become
counts. Omitted old-peer resources remain unknown, not zero.
`--verbose` preserves full registry rows, receiver/placement/generation,
`status_updated_at`, `last_seen`, and complete device/server records.
Roles are computed on each agent's origin host for its own host-qualified
workspace; all-repository/all-host boards do not imply one global main.
`status_updated_at` is the actual agent status update timestamp, separate from
registry observation `last_seen`. Rows are stored snapshots, not proof of current
native activity. The board does not refresh the registry or mutate status,
last_seen, display, settings or claims; peer requests explicitly use
`refresh=false`. It performs no AGENTS/history replication.
Peers must run this read API version to honor `refresh=false`; an older peer
may ignore it and perform its legacy registry refresh.

Compact `hosts` entries retain `host`, `stale`, `error`, `unread`; optional
`resources` has `servers_unowned`, `dead_serve`, `stale_devices` counts and scan
`error`. Agents appear only once in top-level `agents`.
Verbose retains `{host,uuid,local,stale,error,unread,agents}` and full resources.
Python `status_board()`, TUI, remote/peer `status-export` retain the full shape:
compaction is only the CLI projection, not a transport change.
Peer failures produce a structured per-host `{code,message}` error,
`stale:true` and unread `"?"`, not a false closure or fabricated agents.
The board envelope exits with code 0 even if a peer fails: inspect each host's
`error`, `stale` and unread fields. An unknown/ambiguous `--host` instead returns
the top-level `peer_not_configured` error and exits with code 1.
The stateless CLI has no prior rows. A TUI using the optional previous-snapshot
service input may retain prior rows for the same registered host UUID, marked
stale with unread `"?"`; this does not create a durable remote cache.
Board/history reads expose no public settings mutation, user-origin compose,
direct send or operator-resolve/ACK command. Private UI services and native
runtime evidence endpoints are not agent policy bypasses.

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

Each item must have exactly one `address` / `route` and a nonblank string `message`. Duplicate resolved targets, including an address and route for the same agent, fail with `duplicate_route`. The participating coordination-policy preflight described above is all-target and precedes every dispatch; no valid peer is submitted if that gate fails. Where this policy does not apply, legacy per-item input failures retain independent results. Results retain input order. HAM dispatches supplied messages, not a semantic decomposition: tailor each assignment yourself.

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

Use a bounded wait matching the task instead of repeatedly polling request status
or the board. Wait expiry/unchanged output is not a reason to resend; inbox work
remains boundary-driven, not a polling loop.

### `request-cancel`

```bash
python3 scripts/herdr_agent_messenger.py request-cancel --request-id REQUEST_ID
```

Cancels your held request before submission without contacting its recipient or
SSH. It still requires `HERDR_PANE_ID` and verified local Herdr sender identity;
missing pane returns `sender_unavailable` (exit 1). This is not mailbox withdrawal
(use `mail withdraw --message UUID`) and adds no CLI push operation.

Compact result fields are `request_id`, `state`, `phase`, `cancelled`, `error_code`.
- Held `deferred`/`awaiting_decision`: `cancelled:true`, state `submission_failed`,
  phase/error `delivery_cancelled`, exit 0.
- Already submitted working/unknown: unchanged state, `cancelled:false`,
  `already_submitted`, exit 3; never replay.
- Other terminal or repeated cancellation: `cancelled:false`, `already_terminal`,
  exit 1.
- Non-owner: `cancelled:false`, `not_sender`, exit 1.
- Unknown ID: standard nested `request_not_found` error, exit 1.
Usage errors exit 2; `--request-id ID` is required (ID returned by request/send).

### `spawn`

```bash
python3 scripts/herdr_agent_messenger.py spawn \
  --kind omp --cwd /path/to/project --placement tab \
  --message 'Review the project and report concrete blockers through HAM mail.'
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
| `--message TEXT`, `--message-file PATH` | Optional, mutually exclusive initial prompt after readiness. File is caller-local UTF-8; body must be nonblank. Omitting both creates a session without a prompt. |
| `--timeout MS` | Interactive readiness timeout; default 90000, inclusive range 1–300000. Not a reply deadline. |
| `--verbose` | Full startup/placement/metadata result. |
| `-h`, `--help` | Print help. |
| `-- AGENT_ARGS...` | Optional positional `agent_args` forwarded to the interactive agent; no default arguments. Put all HAM flags before `--`. |

Creates a labeled interactive session. Optional initial messaging is a single persisted operation: target/body/readiness and `submitting` are committed before transport; observing/rechecking that operation never submits again. This initial submission is the only spawn-internal coordination-policy exception, not a general legacy bypass. `initial_prompt` includes operation/submission IDs, `acceptance` (`accepted|unknown|failed`) and evidence. An unfinished saved submission is reported unknown, not replayed. Accepted terminal transport is not native mailbox delivery or a completed response. Unknown/failed initial submission exits 1 with usable placement retained; unknown must not trigger a resend, replacement spawn or automatic cleanup. A second spawn invocation creates a different operation, not a safe retry. Without a message flag, startup is not a prompt or response. Metadata flags describe reporting/renaming success, not reply completion. A custom label outside HAM's palette has empty marker/color and no palette metadata. Inspect placement/startup failure cleanup before creating another session.

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

Where participation policy permits legacy prompt routing, agent CLI `request`, `send`, and `batch` respect recipient focus on the selected host, including remote Herdr. HAM popup sends and coordinator delegation initiated by the user are not focus-held. A deferred request is already accepted for eventual delivery: **do not resend, bypass HAM, or ask the user to unfocus**. Mailbox native wake instead requires Herdr `turn_state` of `idle` or `done` plus runtime idle/editor/quiet-time admission and ignores pane focus.

Self-send fails immediately with `recipient_is_sender`, before any focus check; the sender's focused pane never causes a self-addressed message to be deferred.

The default `--defer-timeout` is 180000 ms. Focus leaving before the deadline permits delivery; expiry while still focused changes to `awaiting_decision`. HAM notifies the sender host and, for remote recipients, the recipient host. The remote notification names the sender host, where **HAM: Held Messages** is available. Notification delivery is not proof that a user has read or decided.

In **HAM: Held Messages**, Up/Down selects, `p` pushes, `c` cancels, and `q`/Esc closes. Push can combine the message with unsent text already in that pane; the popup warns the user. Cancel produces terminal `submission_failed`, phase/error code `delivery_cancelled`, `submitted: false`. The agent must not automatically retry a user cancellation. A vanished/replaced recipient fails before held delivery with its resolver error or `recipient_changed`.

After deferred delivery or Push, the worker observes the submitted request about every 0.5 seconds until it is terminal or 30 minutes have passed since submission. Read the saved result with `request-status`; after the worker's observation limit, this command can still advance the request. The worker stays alive while any deferred, awaiting-decision, or watched request remains.

Focus is not a draft-text detector: a person who leaves a pane containing unsent text can still have that text combined with a later prompt. Never claim focus deferral guarantees empty input.

### Compact versus verbose payloads

`--verbose` is available on legacy prompt-routing commands, `status board|list` and `inbox read`. Compact responses omit bulky identities/routes and optional null/absent fields; verbose preserves full public diagnostics, never private fields. Do not rely on compact output for routes. All launcher/ledger CLI JSON uses whitespace-free separators and UTF-8 characters (`ensure_ascii=False`), including verbose; parse JSON rather than whitespace/escaped spelling.

| Operation | Compact fields | Additional/full `--verbose` fields |
| --- | --- | --- |
| `status board`, `status list` | Brief address/host/workspace/repo/role/turn/phase/summary/unread/stale/receiver rows; device/server counts; board host errors/resources counts, no duplicate host agent rows | Full original registry/status/resource rows, timestamps/identity/placement, `hosts[].agents` and full host metadata. See Status board output and fields. |
| `inbox read` | `{messages,...warnings}`; nonnull `message_id`, `thread_id`, `sender` (`origin_host/sender_label`), `kind`, `priority` if provided, `state`, `body`, `reply_to`, `hop`, truncation/omission flags; nonempty `hold_reason`, true `withdraw_requested` | Full original message/delivery fields including hashes, routing, claim/recipient identity and timestamps/nulls. Priority is not synthesized if absent. Python `Mailbox.read()` is unchanged. |
| `list` | `host`, `agents` with `address`, `status`, `workspace` | Full agent records with the fields listed below. |
| `status` | `address`, `status`; `stale: true` only if stale | `agent` full record, `route`, `route_refreshed`. |
| `spawn` | `address`, `marker`, `created`, `pane_id`, optional `tab_id`, `workspace_id`; metadata flags only when false; optional `initial_prompt` operation/submission/acceptance/evidence when requested | `label`, `color`, `host`, `kind`, `cwd`, `route`, `agent`, both metadata flags even on success, plus compact fields. `created` is `pane`, `tab`, or `workspace`. |
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
| Coordination: `use_mailbox` | Same-repository participating prompt traffic is refused before legacy submission. Use durable mail; no CLI bypass. Coordinated batch lookup/preflight failure aborts all submissions. |
| Mail/native: `sender_unresolved`, `stale_session`, `message_conflict`, `delivery_in_progress`, `invalid_setting` | Preserve immutable IDs and generation evidence, repair the actual identity/input/configuration problem; do not manufacture ACK or replay unknown claims. Settings are user-TUI only. Cross-repo sends are allowed. |
| Mail resolution: `agent_ambiguous`, `repo_ambiguous`, `main_ambiguous`, `main_unresolved` | Inspect candidates/workspaces in the error and current board; choose an exact repo/unique suffix or concrete agent address. Never pick an arbitrary main or spawn a replacement. |
| Native startup: `native_session_pending` | Herdr has not published the OMP session yet. Wait within the receiver's bounded retry window; never publish or replace native identity through HAM. Exhaustion is unavailable; a genuine reported-path conflict remains `stale_session`. |
| Initial spawn: `prompt_acceptance_unknown`, `spawn_state_unavailable` | Possible first-prompt acceptance; preserve operation/submission/placement and inspect. Never automatically repeat spawn or its first prompt. |
| Spawn input: `cwd_required`, `sender_pane_unavailable`, `label_unavailable` | Supply remote cwd/source pane or an available valid label, then retry startup. |
| Spawn execution: `placement_failed`, `agent_start_failed` | Inspect Herdr failure and cleanup result. Check already-created sessions/resources before retrying; unsuccessful cleanup may leave one behind. |
| Installation/internal: `cli_not_found`, `internal_error`, `dispatch_failed`, `invalid_sender_result`, `invalid_command` | Restore source-backed CLI installation or investigate reported failure. Unexpected error does not prove non-delivery; inspect state/recipient before retry. `invalid_command` is a defensive fallback; unknown commands normally exit 2. |

## Coordination safety

List requested hosts and verify labels; assign non-overlapping work; send worker-specific context unless explicitly asked to broadcast. Participating collaborators use mailbox threads and boundary-driven receipt/handling, not legacy requests or token-spending polling. Where policy permits legacy batch, observe each original request through settlement or clear terminal failure. Review actual workspace evidence, follow up on inconsistencies and synthesize missing hosts, uncertainty, unverified claims and risks. HAM does not perform semantic decomposition or verification.

Never modify SSH trust settings, bypass host-key verification, or send through an alias absent from configured SSH config. Prefer the bundled CLI over reconstructed remote commands. Never resend because a wait/read/status/storage operation failed.

## Client onboarding and installation

Install with **HAM Skill** in Herdr's command palette (or `Ctrl+G` in Agent Messenger). Installer targets are `codex`, `claude`, `omp`, or `all`.

### Installation checklist

For each selected host:
1. Install/enable the HAM plugin (ledger, mailbox, UI and transport).
2. Install/update the complete skill bundle (instructions/launcher, not a receiver).
3. Run read-only no-client `runtime status`.
4. Explain a missing receiver for a used OMP/Claude client and request consent.
   OMP supports eligible action wake; Claude receives only at turn boundaries.
5. Only with consent, run `runtime install <client>` or installer `--runtime`.
   The interactive default is **Not now**.
6. Report ownership conflicts; never replace foreign or modified files.
7. Identify sessions needing restart; restart only those the user selects.
8. Confirm `extension_present=1` after restart, or report **installed, activation pending**.

Codex has no adapter and pulls with the skill; its missing receiver is not broken.
Plugin loading never writes client configuration or changes the new-repo `auto`
default. Installation files alone do not prove active receiving.

From the plugin/source checkout root:

```bash
python3 agent_skill_installer.py install all
python3 agent_skill_installer.py install codex
python3 agent_skill_installer.py install claude
python3 agent_skill_installer.py install omp
python3 agent_skill_installer.py install omp --runtime omp
python3 agent_skill_installer.py install all --runtime all
```

`all` installs all three user-scope targets. The popup also offers project-scope installs when a project workspace is available. Canonical `SKILL.md`, this `reference.md`, scripts, and agent metadata belong together; do not copy only SKILL.md. The checkout's Claude skill is a pointer to the canonical `.agents` skill; read canonical SKILL.md fully, this reference only on demand. Installed client copies contain the complete canonical bundle.

After a successful interactive skill install, the installer shows OMP/Claude
runtime installation states and offers an explicit missing-runtime choice with
**Not now** selected by default. Before consent, it shows the destination and
the files it will write: OMP's extension and manifest, or Claude's hook,
manifest and merged `settings.json` entries described above. Consent uses the
same runtime installer and ownership checks; conflicts are shown verbatim,
never replaced or bypassed. Only the sessions the user chooses to restart
pick up the receiver; installing does not activate every running session.

Noninteractive `install <codex|claude|omp|all>` writes its final runtime summary
to **stderr**, preserving the existing stdout result. The summary names missing
runtimes and their exact install commands. `--runtime omp|claude|all` explicitly
consents to installing those runtimes, independently of the selected skill
target (`all` here means both receiving runtimes, not a Codex adapter).
Without that flag, no runtime is installed implicitly. On each selected host,
run the no-client `runtime status`, explain any missing runtime needed by a used
client and request consent, report ownership conflicts without replacing
foreign/modified files, and identify the sessions that need restarting.
After restart, inspect receiver registration (`extension_present=1`) or report
**installed, activation pending**. Never call native receiving ready based only
on plugin/skill installation or owned runtime files.

| Client | User scope | Project scope | Invocation |
| --- | --- | --- | --- |
| Codex | `~/.agents/skills/herdr-agent-messenger` | `<project>/.agents/skills/herdr-agent-messenger` | Type `$ham` and select **HAM — Herdr Agent Messenger**, or invoke `$herdr-agent-messenger`. |
| Claude Code | `~/.claude/skills/herdr-agent-messenger` | `<project>/.claude/skills/herdr-agent-messenger` | `/herdr-agent-messenger` or a matching natural-language request. |
| omp | `~/.omp/agent/skills/herdr-agent-messenger` | `<project>/.omp/skills/herdr-agent-messenger` | Ask naturally to use HAM or read `skill://herdr-agent-messenger`. |

An installed copy without the current reference is outdated, not a reason to bypass the CLI. Update through the installer. Unrelated skill directories and symlink conflicts are not overwritten. The bundled launcher needs access to the HAM plugin/source CLI; `cli_not_found` means repair that installation rather than invent a replacement transport.

For expanded client setup, see `docs/skill-guide.md` in the plugin/source checkout.
