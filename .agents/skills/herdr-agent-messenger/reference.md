# HAM — on-demand reference

Read `SKILL.md` first. This reference covers the complete CLI, diagnostic payloads, recovery, and installation. Paths to `scripts/herdr_agent_messenger.py` are relative to this skill directory; use an absolute path when working elsewhere. Messaging runs from the coordinator's active Herdr agent pane (`HERDR_PANE_ID`).

## Command and flag reference

The legacy prompt-routing commands (`list`, recipient `status`, `send`, `batch`, `read`, `request`, `request-status`, `spawn`) accept `-h` / `--help` and `--verbose` (full diagnostic JSON; default off). Mailbox/ledger/runtime/device commands and `status set|clear|list|board` also accept `-h` / `--help` but have no verbose flag, including `mail threads` and `mail direct-list`. Numeric arguments are integers. Optional flags are unset unless a default is listed. Milliseconds are denoted `MS`.

### Recipient selectors

`status`, `send`, `read`, and `request` require **exactly one** of `--address`, `--label`, or `--route`:

| Flag | Meaning / default |
| --- | --- |
| `--address HOST/LABEL` | Exact `address` returned by `list` or `spawn`; carries its own host. |
| `--label LABEL` | Exact current Agent Labels name on `--host`. |
| `--route TOKEN` | Opaque verified GUI/CLI route; carries its own host. Pass unchanged, never decode or edit it. Supports unlabeled GUI selections. |
| `--host HOST` | Default `local`; otherwise a concrete configured SSH alias. An explicit non-default host must agree with `--address` or `conflicting_address` results. For routes use the token's host, not an override. |

Only literal `local` means local transport. Even a hostname matching this machine means SSH. Labels can repeat across hosts and do not guarantee the same occupant after restart. Missing/ambiguous hosts or labels must be reported, not guessed. Never substitute a pane ID or create a missing agent unless asked.

These selector rules describe legacy routing. Durable mail separately accepts
`local/LABEL`, registered canonical names or SSH aliases, and `main`, resolved
to the sender's workspace main at send time.
Before using legacy `send`, `request` or `batch`, inspect repository participation:
manual/auto same-repository traffic between participating agents is refused with
`use_mailbox`, with each endpoint's role evaluated in its own host-qualified
workspace. Use `mail send`, not another transport. All batch targets are
resolved and policy-checked before any dispatch; a refusal or discovery/policy
failure aborts the coordinated batch without partial submission. Off-mode,
nonparticipating and other-repository legacy traffic retains its previous
behavior. The policy prevents accidents; it is not an authorization boundary.

A verified route can refresh only when continuity checks identify one safe occupant. In verbose output, retain a new `route` when `route_refreshed: true`. Compact output intentionally omits routes; use addresses for normal agent workflows.

### Local mailbox

Mailbox messages are durable data, not terminal prompts. Same-repository local
and registered remote agents can exchange them without interrupting an ongoing turn:

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
| `mail send` | Required `--to local/LABEL`, `CANONICAL_HOST/LABEL`, registered `SSH_ALIAS/LABEL`, or symbolic `main`. `--kind fyi\|action`, default `fyi`; optional `--reply-to UUID`, `--subject TEXT` (1024 UTF-8 bytes); exactly one of `--body TEXT` / `--body-file PATH` (UTF-8, nonblank, at most 16 KiB). Replies keep the thread and increment hop; replying to addressed queued/delivered local mail also marks that parent handled atomically. Remote targets are selected from a fresh same-repository status export. |
| `inbox read` | Optional `--message UUID`, `--all`, `--limit N` (default 100, range 1–1000). Without `--all`, shows queued, uncertain delivering, and delivered mail. Reads never change delivery, logs, or bells, even from subagents. |
| `inbox handle` | Required repeatable `--message UUID`. The entire batch is validated before changing any row. A queued pull-only message becomes handled with no fabricated delivered timestamp. |
| `mail show` | Required `--thread UUID`; reads a known retained thread, including sent mail, without caller-recipient filtering. Returns retained bodies and delivery state from this ledger. |
| `mail threads` | Optional `--repo REPO_ID`, `--limit N` (default 100, 1–1000), `--cursor TOKEN`; repository-wide retained sent/received threads, including pending remote copies. Returns `{repo_id,threads,next_cursor}`. |
| `mail direct-list` | Same `--repo REPO_ID`, `--limit N`, `--cursor TOKEN`; retained `via=direct` user-input submissions, not `spawn_initial`. Returns `{repo_id,submissions,next_cursor}`. Read-only, not a direct-send command. |
| `mail withdraw` | Required `--message UUID`; originating terminal only. Local withdrawal races atomically with queued→delivering claim. Remote mail never attempted on the wire is cancelled locally; otherwise the saved original route carries a withdrawal request to the final recipient. Inspect `withdrawn` and `withdraw_requested`; already-claimed/delivered/handled or other non-withdrawn final mail is not withdrawn. Repeating successful withdrawal returns `withdrawn:true`, preserving the first final receipt/time. |
| `repo show` | Read-only repository identity, current settings, and local agents. No public setting mutation command. |

`mail threads` and `mail direct-list` default to the verified caller's repository.
Explicit `--repo REPO_ID` avoids fetching Herdr caller identity. Both enumerate
all participants' records for that repo on this local ledger, not only the
caller's inbox and not replicated all-host history. They never claim, acknowledge,
handle, refresh the registry, or change status/last_seen/display state.

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
`sender_unresolved`, `stale_session`, `native_session_pending`, `recipient_absent`, `repo_mismatch`,
`message_conflict`, `delivery_in_progress`, `body_too_large`, `invalid_limit`,
`invalid_repo`, `invalid_cursor` (including history tampering/scope mismatch).
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
present) and a wake claim must retain a hop-eligible `action`, otherwise it
returns `no_action` without claiming FYI. `applied` ACKs only the exact session,
marker and content hash after the extension finds that custom message in the
public active-branch session history; that is host journal acceptance, not proof
of later provider transforms or model understanding.

Runtime installation is a separate, explicit per-host opt-in; plugin loading
and skill copying do not install adapters. See [receiving runtimes](#receiving-runtimes)
for both OMP and Claude ownership rules, commands and acceptance limits.

#### Mailbox terminology

- **Mailbox / inbox:** durable mail for a recipient's stable terminal slot.
  `inbox read` filters this ledger host, caller terminal and current repository:
  default `queued`, `delivering`, `delivered`; `--all` includes all retained
  states for that same recipient, and `--message UUID` can read a final message.
  Viewing never acknowledges application or work.
- **Thread:** the root message UUID is the thread UUID. `--reply-to` retains the
  parent thread/subject and increments its hop. `mail show --thread UUID` reads
  a known thread; `mail threads` discovers repo-wide retained threads.
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
- **Unread:** the board counts `queued` + `delivering`, not an actual-view flag;
  delivered-but-unhandled mail is excluded.

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
| `accept` | `{message:IMMUTABLE_MESSAGE,resolved:{recipient_host,recipient_terminal_id,recipient_address}}` → current receipt. New sends always carry a concrete terminal target matching the immutable target, including sends addressed to `main`. Persisted legacy symbolic messages use sender-scope compatibility handling or fail visibly; they never select another workspace's main. |
| `receipt` | Current compact public receipt, including `message_id`, `origin_host`, `payload_hash`, resolved recipient fields, `state`, `authoritative_host`, times and `withdraw_requested` → retained nonregressing receipt. Only the saved downstream route can advance the origin/relay copy. |
| `withdraw` | Original receipt identifying `message_id`, `origin_host`, `payload_hash` and target → withdrawn/current receipt. Retained legacy relay routes may include `relay_home`. Unknown IDs create a permanent hash-bound tombstone before late acceptance. |
| `setting` | `{repo_id,field,value,version,origin_host,changed_by,changed_at}` → `{applied,...repo_settings}` for repo-wide `home`, `mode`, `thresholds`. Protocol-1 legacy `main`, `members`, `computed_main` payloads (including an old conditional `expected_main`) succeed as no-ops. Workspace settings never use this endpoint for replication. |
| `status-export --wire` | Optional `{repo_id,refresh,resources}` → `{host:{canonical_name,uuid},agents:[...],resources?:{...}}` with roles and optional resource fields. Default refresh gets a fresh local snapshot; `refresh:false` reads stored rows without registry/status/last_seen/display/claim mutation. Only `resources:true` opts into server discovery and adds agent `servers` plus host `resources`; `status board` sends both flags. Missing/false `resources` avoids scans for send/policy/recipient lookup and leaves those fields unknown. Plain `status-export [--repo ID]` remains a fresh local export without server discovery. Remote rows are never inserted into local AGENTS. |

The Python settings service is
`RepoSettings(store).show(repo_id,host,workspace_id)` and
`mutate(repo_id,field,value,*,host=None,workspace_id=None)`, reserved for the user
Settings UI, with **no agent mutation CLI**. `main` and `members` mutations
require an explicit local host/workspace scope and reject inactive or
out-of-scope targets. `main` is `null` or `{host,terminal_id}`; `members` is a
unique list of those targets. `mode` (`off|manual|auto`) and `thresholds`
(`idle_minutes:3,coalesce_seconds:30,wakes_per_hour:6,max_hop:10` defaults)
remain repo-wide, as does read-only `home`.

`fields(connection,repo_id)` contains only repo-wide fields;
`workspace_fields(connection,repo_id,host,workspace_id)` reads the local scope.
`main(connection,repo_id,host,workspace_id)` selects that scope's main;
`role(agent,connection=None)` is the shared role calculation for runtime,
status export, sidebar and borders. Replicated fields independently win by
`(version,origin_host)`; same-version different values record the deterministic
canonical-name winner and conflict. Different fields' concurrent edits survive.
Workspace main/member settings stay on their owning host and never enter peer
publication or repo-wide conflict/member projections. Peers receive each
agent's authoritative role through origin-host status export.

First enabling a repository creates a version-1 home proposal; **offline peers
do not prevent local use**. Observed concurrent home proposals converge by
canonical-name tie order. Home is not the authority for workspace roles.
Main election is derived on read, not stored or published as `computed_main`.
A valid explicit main must be active in the exact `(repo_id, host, workspace_id)`
scope. A target that becomes inactive or moves out is ignored without rewriting
the setting. In auto mode, the sole active agent becomes main; with several,
exactly one active main-checkout agent becomes main. Otherwise there is none.
A single agent in a worktree workspace therefore gets its own main role.

`--to main` resolves on the sender's host at send time against the sender's
current scope and stores the concrete terminal target in the immutable message.
Missing main produces `main_unresolved` naming the workspace; there is no
fallback to a repo-global main, another workspace, or an offline home route.
Receipts and withdrawals follow the saved target. Explicit named mail still
crosses workspaces within a repository. An already-stored unresolved symbolic
message uses its recorded sender agent ID, origin host and repo to find the
sender's local workspace. If that scope cannot be found or has no main, it fails
visibly with `main_unresolved`, never by silently routing to another scope.

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
See [migration guidance](../../../docs/migration.md#workspace-role-migration-schema-v7).

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
| `manual` | Active explicit user-designated terminal in the same repo/host/workspace, otherwise none | Explicit in-scope members except main. Nonmembers can receive at natural boundaries but do not wake automatically. |
| `auto` | Active in-scope explicit designation first; otherwise the sole active agent; with several, exactly one active main-checkout agent in the scope | All other agents in that scope, even when no main can be selected. Worktree workspaces elect independently; identical workspace IDs on different hosts do not collide. |

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

`status board` agent rows and status-export payload agent rows add independently
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
stale claims. Agents shows right-aligned PHN/EMU/SRV columns for phone, emulator,
and server counts at 70 inner columns or wider, before the flexible summary.
Zeros show a dim `·`; missing fields independently show `?`. Host group labels
retain the agent count and show nonzero or unknown dead/unowned/stale-device
findings, truncating findings first. Detail has unchanged Devices and Servers
sections; known-empty lists show none and resource errors remain visible.
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

Responsive **Agents**, **Threads**, **Detail** regions replace exclusive screens:
one focused region below 80 columns or 18 rows, stacked lists plus Detail at
80–139 columns, three columns from 140. Below 32 columns or 10 rows, resize
instructions appear without losing mode, focus, selections, text or cursor.
NAV keys: Tab/Shift+Tab, ←/→ or h/l changes focus; ↑/↓ or j/k selects rows;
PgUp/PgDn/Home/End pages or jumps. Press `Space` on an Agents or Threads item,
including DIRECT/OUTBOX, to open a centered read-only Detail viewer (~90% screen,
maximum 100×40). Arrows, PgUp/PgDn, Home/End and wheel scroll; `t` toggles
technical information. It opens at the latest message and follows new arrivals
only while at that position. If the item disappears on refresh, it closes with
a status message. Esc, q, Space, or outside click closes; inside click and Enter
are inert. Viewing never ACKs. Detail Enter otherwise does nothing. Digits never
navigate; `s` opens Settings, `?` help, `e` diagnostics and `r` refresh.

Agents groups rows under host headers, showing stale/errors, alias, turn,
unread, native receiver readiness and one dim role/phase/summary line.
Disconnected peers retain stale snapshots with unread `?`, not closed agents.
Detail automatically previews the last-focused list selection, including full
agent fields and separate status-update/observation timestamps with local time
and relative age. Threads includes messages, direct submissions and OUTBOX.
**Viewing is read-only and never ACKs, delivers, handles or resolves mail.**

Agents Enter opens inline FYI input; Threads Enter opens a reply to its latest
message, resolving the recipient by host/terminal to a live, non-stale agent.
Stale/unresolvable recipients are blocked; absent native receivers warn but
allow mailbox sending. Detail Enter does nothing. UI mail has `sender=user`,
kind `fyi`, no subject. Enter sends immediately without confirmation; blank
bodies fail without sending. Shift+Enter inserts a newline using the mailbox's
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

↑/↓ selects a row and Enter edits its value; all mutations require confirmation.
The overlay's `Target: host/alias` is the selected Agents row: `m` designates it
main and Space toggles manual membership, only if the active target belongs to
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

On **each selected host**, install/check/remove only the chosen client:

```bash
python3 scripts/herdr_agent_messenger.py runtime install omp
python3 scripts/herdr_agent_messenger.py runtime status omp
python3 scripts/herdr_agent_messenger.py runtime uninstall omp
python3 scripts/herdr_agent_messenger.py runtime install claude --destination /ABS/CHOSEN/CLAUDE_CONFIG
python3 scripts/herdr_agent_messenger.py runtime status claude --destination /ABS/CHOSEN/CLAUDE_CONFIG
python3 scripts/herdr_agent_messenger.py runtime uninstall claude --destination /ABS/CHOSEN/CLAUDE_CONFIG
```

`install|status|uninstall` takes positional `omp|claude` and optional
`--destination DIR`. OMP defaults to `~/.omp/agent/extensions`; Claude defaults
to `CLAUDE_CONFIG_DIR` when set, otherwise `~/.claude`. OMP owns only
`ham-mailbox.ts` and `ham-mailbox.install.json`, not Herdr's own extension.
Claude owns `hooks/ham-mailbox.py`, its manifest and exact `SessionStart` /
`UserPromptSubmit` entries merged into `settings.json`. Unrelated settings/hooks
survive install/uninstall. Ownership/checksum checks refuse foreign, modified
or symlinked targets rather than replacing them. Restart/reload only sessions
the user selects; neither installer silently enables all running sessions.
Runtime status reports owned installation, not proof of context admission.

**OMP:** only a supported root Herdr session registers. The adapter discovers
the current structured plugin root (or explicitly configured absolute
`HERDR_AGENT_MESSENGER_ROOT`) and calls Python asynchronously; it never opens
SQLite. Natural turn boundaries can receive both kinds, including off/manual
nonmembers. FYI alone does not start a turn. Automatic action wake requires a
participating role, same repo/generation, TUI mode with supported empty-editor
evidence, configured input quiet time, idle/no pending work, no continuation or
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
| `status list` | Optional `--repo [ID]`: absent lists all local repository statuses, bare `--repo` selects the caller's repo, explicit ID filters it. |
| `status board` | Optional `--repo [ID]`, `--host local\|CANONICAL\|REGISTERED_SSH_ALIAS`. No repo flag selects all repositories; bare `--repo` resolves the caller's repository; explicit ID filters without fetching Herdr caller identity. No host flag selects local plus all registered peers; explicit host selects only that ledger. Returns `{repo_id,agents,hosts}`. |

These subcommands accept `-h` / `--help` and have no `--verbose`. `status board`
has its own host selector; do not treat legacy `status --host HOST list` as
remote board syntax. Legacy `list` and stored local `status list` remain
available with narrower semantics.

Board agent rows preserve the registry fields and include role, turn_state,
phase, summary, unread, extension_present, client, workspace_id, pane_id,
generation, is_main_checkout, status_updated_at, last_seen and stale.
Roles are computed on each agent's origin host for its own host-qualified
workspace; all-repository/all-host boards do not imply one global main.
`status_updated_at` is the actual agent status update timestamp, separate from
registry observation `last_seen`. Rows are stored snapshots, not proof of current
native activity. The board does not refresh the registry or mutate status,
last_seen, display, settings or claims; peer requests explicitly use
`refresh=false`. It performs no AGENTS/history replication.
Peers must run this read API version to honor `refresh=false`; an older peer
may ignore it and perform its legacy registry refresh.

Each `hosts` entry contains `{host,uuid,local,stale,error,unread,agents}`.
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

Where participation policy permits legacy prompt routing, agent CLI `request`, `send`, and `batch` respect recipient focus on the selected host, including remote Herdr. HAM popup sends and coordinator delegation initiated by the user are not focus-held. A deferred request is already accepted for eventual delivery: **do not resend, bypass HAM, or ask the user to unfocus**. Mailbox native wake instead uses idle/editor/quiet-time admission and ignores pane focus.

Self-send fails immediately with `recipient_is_sender`, before any focus check; the sender's focused pane never causes a self-addressed message to be deferred.

The default `--defer-timeout` is 180000 ms. Focus leaving before the deadline permits delivery; expiry while still focused changes to `awaiting_decision`. HAM notifies the sender host and, for remote recipients, the recipient host. The remote notification names the sender host, where **HAM: Held Messages** is available. Notification delivery is not proof that a user has read or decided.

In **HAM: Held Messages**, Up/Down selects, `p` pushes, `c` cancels, and `q`/Esc closes. Push can combine the message with unsent text already in that pane; the popup warns the user. Cancel produces terminal `submission_failed`, phase/error code `delivery_cancelled`, `submitted: false`. The agent must not automatically retry a user cancellation. A vanished/replaced recipient fails before held delivery with its resolver error or `recipient_changed`.

After deferred delivery or Push, the worker observes the submitted request about every 0.5 seconds until it is terminal or 30 minutes have passed since submission. Read the saved result with `request-status`; after the worker's observation limit, this command can still advance the request. The worker stays alive while any deferred, awaiting-decision, or watched request remains.

Focus is not a draft-text detector: a person who leaves a pane containing unsent text can still have that text combined with a later prompt. Never claim focus deferral guarantees empty input.

### Compact versus verbose payloads

`--verbose` is available on the legacy prompt-routing commands, not mailbox/ledger/runtime or status subcommands. Compact responses omit bulky identities/routes and optional null/absent fields. Verbose preserves the full public diagnostic result; private fields are never printed. Do not rely on compact output to provide a route token.

| Operation | Compact fields | Additional/full `--verbose` fields |
| --- | --- | --- |
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
| Mail/native: `sender_unresolved`, `stale_session`, `repo_mismatch`, `message_conflict`, `delivery_in_progress`, `invalid_setting` | Preserve immutable IDs and generation evidence, repair the actual identity/input/configuration problem; do not manufacture ACK or replay unknown claims. Settings are user-TUI only. |
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
