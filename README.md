<p align="center">
  <img src="assets/ham-logo.png" alt="HAM — Herdr Agent Messenger mascot logo" width="360">
</p>

# HAM — Herdr Agent Messenger

[한국어](README.ko.md) · English

HAM is an agent-labeling and multi-agent messaging plugin for Herdr. It gives
unnamed agents memorable `color-animal` names, starts new labeled sessions with
an optional atomic first prompt, and provides a durable same-repository mailbox
for coordination across local and SSH hosts.

## What HAM provides

- Automatic labels such as `blue-otter` with matching color markers
- Local and SSH agent discovery using concrete aliases from `~/.ssh/config`
- Durable same-repository threads, explicit handling, and `off` / `manual` / `auto` participation modes
- A keyboard-first mailbox/status TUI with inline FYI quick-send, replies, and confirmed direct input
- A bundled `herdr-agent-messenger` skill for Codex, Claude Code, and omp, split into short `SKILL.md` and on-demand `reference.md`
- Labeled interactive-session creation with `spawn --message`, plus standalone and permitted legacy request, wait, read, batch, and rollout helpers
- Separately opt-in omp and Claude receiving runtimes; Codex uses explicit inbox reads at work boundaries
- Focus-based holds for permitted legacy agent prompts, with a 3-minute default timeout and explicit Push/Cancel in **HAM: Held Messages**
- Compact agent-facing CLI output by default (`status board|list`, `inbox read`, legacy routing), `--verbose` diagnostics, whitespace-free UTF-8 JSON, and bounded `request-status --wait` instead of polling

Manually named agents are left unchanged. Agent addresses are always
host-qualified, for example `local/blue-otter` or
`macbook-pro/purple-koala`.

## Requirements

- Herdr 0.8.0 or newer
- Python 3.10 or newer on Herdr hosts; Python 3.11 or newer to run `rollout_plugin.py`

## Install

```bash
herdr plugin install zerodice0/herdr-agent-messenger --yes
```

The repository is now `herdr-agent-messenger`, renamed from `herdr-agent-labels`.
The old GitHub URL redirects, so existing GitHub installs keep updating.
The plugin ID remains `herdr.agent-messenger` and its display name is **HAM**.

For local development:

```bash
herdr plugin link "$PWD" --enabled
```

Upgrading from 0.7.x requires migrating the former `herdr.agent-labels` ID.
Follow the [migration guide](docs/migration.md) before removing the old plugin.

### Receiving dependency

- **Plugin:** ledger, mailbox, UI, and transport.
- **Skill:** agent instructions and CLI launcher, not a receiver.
- **OMP adapter:** native receiving and eligible automatic action wake.
- **Claude hooks:** native receiving at turn boundaries, with no idle wake.
- **Codex:** skill-guided inbox pull; no adapter is needed.

Plugin and skill installation alone do not make native receiving ready. Install
the [client skill](#install-the-client-skill), then run the read-only health
summary on **each selected host**, from the plugin/checkout root:

```bash
python3 .agents/skills/herdr-agent-messenger/scripts/herdr_agent_messenger.py runtime status
# Only after user consent, for a client actually used on this host:
python3 .agents/skills/herdr-agent-messenger/scripts/herdr_agent_messenger.py runtime install omp
```

Use `claude` instead of `omp` for Claude hooks. The skill installer offers
missing runtimes with **Not now** selected by default; noninteractive
`install all --runtime omp|claude|all` is explicit consent, never an implicit
dependency install. Runtime ownership conflicts must not be overwritten.
Restart only the sessions the user selects, then confirm `extension_present=1`;
otherwise report **installed, activation pending**. Installation is not proof
of activation. Plugin loading never changes client configuration, and the
`auto` default for new repositories is unchanged. See the [skill guide](docs/skill-guide.md#receiving-dependency).

## Use

Use the [durable mailbox](#durable-mailbox) for participating agents coordinating
within the same repository. The [HAM TUI](docs/ui-reference.md) offers responsive
Agents, Threads and read-only Detail regions, plus a Settings overlay (`s`).
Open **HAM: Mailbox (Right Split)**, **HAM: Mailbox (Tab)**, or **HAM: Mailbox
(quick send)**; the popup closes after a successful send. In NAV, Tab/Shift+Tab,
arrows or h/l move between regions; arrows or j/k select rows. On an Agents row,
Enter opens inline FYI input and Enter sends immediately; on Threads, Enter replies.
Shift+Enter adds a newline via the mailbox's kitty keyboard protocol request
(through herdr in kitty-compatible terminals). Use Alt+Enter on Moshi/mosh;
Ctrl+J is not a newline key. `HAM_MAILBOX_NO_KITTY=1` disables the request.
Esc keeps the draft; Ctrl+D selects direct input, which requires confirmation
and may interrupt the recipient. It is not agent-to-agent mail or native delivery.
`?` opens help in NAV, F1 does so while typing, and `e` shows diagnostics.
Settings `m`/Space changes main/membership for the selected agent with confirmation;
Threads `w`/`t`/`a`/`n` also require confirmation (and evidence for `a`/`n`).
Agents are grouped by host; stale peers show unread `?`. Detail shows the full
summary and separate status-update/observation times. Viewing never ACKs mail.

The legacy **Send Prompt to Agents** popup still supports coordinator delegation
and direct multi-recipient delivery where participation policy permits it.

Codex users can type `$ham` and select **HAM — Herdr Agent Messenger**. Claude
Code users can invoke `/herdr-agent-messenger` or make a matching natural-language
request. omp users can ask naturally to use HAM or read
`skill://herdr-agent-messenger` after installing the omp skill target.

```text
$ham Ask local/yellow-falcon what language label dxp-ui uses. Wait for the
reply, then align this work and its defaults with that answer.
```

Always use a host-qualified address. Legacy discovery and prompt-routing commands
use `local/LABEL` for this machine and a configured SSH alias for a remote host.
Mailbox commands also accept registered canonical host names. A bare label can
collide across hosts or be mistaken for a client's own subagent.

### New sessions

Ask HAM explicitly to create a session. `spawn --message` creates the labeled
session and durably records a single initial-prompt operation, submitting only
after interactive readiness. Do not split this into `spawn` followed by a
legacy `request`. From a checkout:

```bash
python3 .agents/skills/herdr-agent-messenger/scripts/herdr_agent_messenger.py spawn \
  --kind omp --cwd "$PWD" --placement tab --message 'Review this project.'
```

Use `--message-file PATH` instead for a caller-local UTF-8 file; put HAM flags
before `--` when forwarding agent arguments. Omit both message flags to create
a session without a first prompt. Missing existing labels never cause HAM to
silently create replacement sessions. An `unknown` initial-prompt acceptance
may already have submitted: preserve its operation ID and pane, and do not
resend or spawn a replacement. Terminal acceptance is not native mailbox
delivery or evidence of successful model processing.

### Permitted legacy prompts

In `manual` or `auto`, legacy `send`, `request`, and `batch` refuse participating
same-repository sender/recipient pairs with `use_mailbox`. Batch preflights every
target: a refusal or lookup failure causes zero submissions. This prevents
accidental bypasses; it is not a security boundary. Nonparticipating traffic
retains the legacy workflow, including the following hold and observation rules.

CLI messages to a focused recipient are held; do not resend. After the default
3-minute hold deadline, choose Push or Cancel in **HAM: Held Messages** on the
sender host. After deferred delivery or Push, the worker observes the request
until it is terminal or 30 minutes after submission. Read the saved result with
`request-status`; it can still advance observation after that limit.
Self-send fails immediately with `recipient_is_sender`, even if the sender pane
is focused, and is never held.

`request-status --wait MS` stops on a state, phase or `error_code` change, a
terminal state, or the deadline, not response-text growth. Compact
`response.truncated` is true only when reply text itself was cut by output
bounds or a failed read, not merely when older history was omitted or baseline
overlap was lost. See the [command reference](.agents/skills/herdr-agent-messenger/reference.md)
for exhaustive flags and errors, including `invalid_defer_timeout` for a
nonpositive or invalid hold timeout.

### Install the client skill

Install or update the bundled skill from **HAM Skill** in Herdr's command
palette, or run:

```bash
herdr plugin action invoke agent-skill-guide --plugin herdr.agent-messenger
```

The installer supports `codex`, `claude`, `omp`, and `all`; for example, from the
plugin/checkout root, `python3 agent_skill_installer.py install all` installs all
user-scope targets. The omp destination is
`~/.omp/agent/skills/herdr-agent-messenger`, or
`<project>/.omp/skills/herdr-agent-messenger` for a project-scope popup install.

HAM can also be used with Herdr without enabling the plugin. See the
[skill guide](docs/skill-guide.md#use-ham-without-the-plugin) for the
source-backed standalone setup and its limitations.

## Durable mailbox

Participating agents use durable same-repository mail instead of terminal prompt
injection. From a registered Herdr agent pane:

```bash
python3 .agents/skills/herdr-agent-messenger/scripts/herdr_agent_messenger.py mail send \
  --to local/blue-owl --kind action --subject 'Review' --body 'Review this project.'
# At the recipient's work boundary:
python3 .agents/skills/herdr-agent-messenger/scripts/herdr_agent_messenger.py inbox read
# After doing the relevant work:
python3 .agents/skills/herdr-agent-messenger/scripts/herdr_agent_messenger.py inbox handle \
  --message MESSAGE_UUID
```

`fyi` is informational; `action` can qualify for an omp idle wake. FYI alone does
not start a turn. Reads are view-only, including reads by subagents sharing a
pane. Explicit handling can mark queued mail handled without inventing a
delivered timestamp. Replies to mail addressed to the current recipient
atomically handle that parent. Use `mail show --thread UUID` to inspect the
thread and `mail withdraw --message UUID` to request withdrawal; already-claimed
mail cannot simply be recalled.

### Read repository history and the status board

Use the bundled CLI prefix above with these read-only commands:

```text
mail threads [--repo REPO_ID] [--limit N] [--cursor TOKEN]
mail direct-list [--repo REPO_ID] [--limit N] [--cursor TOKEN]
status board [--repo [ID]] [--host local|CANONICAL|REGISTERED_SSH_ALIAS] [--verbose]
```

History defaults to the verified caller's repository; explicit `--repo REPO_ID`
avoids a Herdr caller-identity lookup. Both lists cover all participants' retained
records in that repository on this ledger, including sent/received threads and
pending remote copies, not replicated all-host history. Direct history is
read-only user-direct submission evidence, not permission to send direct input.
`--limit` defaults to 100 (1–1000). Results contain `repo_id`,
`threads` or `submissions`, and `next_cursor` (`null` at the end). Pass the opaque,
HMAC-signed cursor unchanged; it is bound to the ledger, query and repository.
Ordering is `updated_at DESC`, UUID ascending, using keyset pagination, not OFFSET.
Pages are live, not a frozen snapshot: updates can move records before the
boundary; restart from the first page to see newer updates. Tampered or
wrong-scope tokens fail with `invalid_cursor`.

The board defaults to all local repositories and all registered peers. Bare
`--repo` selects the caller's repository; `--repo ID` filters explicitly.
`--host` selects only the local ledger or one registered canonical host/SSH alias.
The result is `{repo_id,agents,hosts}`: brief address/workspace/role/turn/phase/
summary/unread/receiver rows and device/server counts, with no duplicate
`hosts[].agents`. Hosts retain errors/stale/unread and resource counts/errors.
`status board --verbose` restores full registry/status/resource fields, including
`status_updated_at` (status update) and `last_seen` (registry observation).
Python/TUI/peer exports keep the full shape. Reads never refresh registry,
status/display/claims, or peers (`refresh=false`).
Peer failures stay in `hosts` as structured errors with `stale:true`, unread `?`;
a stateless CLI does not invent missing agent rows. A TUI may retain verified
prior rows as stale. `list` and local `status list` remain narrower reads.
Peers must support this read API: older versions may ignore `refresh=false`
and perform their legacy registry refresh. A partial peer failure still exits
with code 0; inspect each host's `error`, `stale` and unread fields. An unknown or
ambiguous `--host` returns `peer_not_configured` with exit code 1.

All three commands accept `-h` / `--help` and return one JSON object.
Only the board accepts `--verbose`; `status list` and `inbox read` also offer
compact defaults with `--verbose` full diagnostics. Inbox retains IDs, sender,
body, state and reply context; reads never ACK. CLI JSON is whitespace-free
UTF-8 even in verbose mode. Domain errors use `{"error":{"code":"CODE","message":"TEXT"}}`
(exit 1); usage errors exit 2. Reads never acknowledge mail or expose public
settings, user-compose/direct-send, or operator ACK bypasses. See the
[reference terminology](.agents/skills/herdr-agent-messenger/reference.md#mailbox-terminology)
for inbox, Threads, transport outbox, message states and unread counts.

### Device claims and server presence

Use the same bundled CLI prefix:

```text
device claim phone|emu DEVICE [--label TEXT] [--force]
device release [phone|emu|all] [DEVICE]
device list
```

Claim a device before using it and release it when finished. `release` defaults
to `all` and releases only the caller's own claims; kind/device arguments narrow
that selection. Emulator numbers normalize to `emulator-N`. Claims belong to
the verified host/terminal/session generation, not an inherited pane ID.
Reclaiming your own device updates its label. Another live holder causes
`device_claimed`; use `--force` **only on explicit user instruction**.
Inactive or replaced-generation holders are stale and may be replaced with
`replaced_stale:true`. A failed Herdr refresh is an error, not evidence that a
holder disappeared. Attachment reported by adb is separate from holder liveness.

`device list` and resource views on `status board` are read-only: no registry
refresh, stale-claim cleanup or token writes. Verbose board/peer JSON has independent
`devices` (held nonstale claims) and `servers` lists; host entries gain
`resources:{servers_unowned:[],dead_serve:[],stale_devices:[],error:null}`.
Compact CLI views project these lists as counts; full rows stay available with `--verbose`.
Servers are discovered from current-user listeners and Tailscale Serve routes,
not claimed. Missing `devices`, `servers` or `resources` from an older peer
means unknown (`?`), independently, not an empty list.

HAM projects exact sidebar token values `phone=📱 <labels space-joined>` and
`emu=🤖 <labels space-joined>` and clears each key when no claims remain.
`status set` preserves this projection. No legacy device JSON is imported by
the product. `heavy_slot.py` remains a separate, unchanged script; HAM does not
read or display its slots. See the [skill guide](docs/skill-guide.md#device-claims-and-resource-presence)
for JSON fields and the [UI reference](docs/ui-reference.md#device-and-server-presence)
for resource displays.

### Participation and roles

Choose participation in the TUI Settings view, not through an agent mutation
command:

Repositories first seen by HAM from this version default to `auto`: all same-repo
agents participate, eligible queued action mail may auto-start an OMP turn, and
same-repo legacy `send`/`request`/`batch` must use mail instead. Already-known
repositories keep their settings; without a stored mode they remain `off` until
changed. Host-local (`local:`) repositories follow the same default without
propagating settings. Read-only commands never initialize participation.

Mode and wake thresholds apply repository-wide. Main and worker membership are
scoped by `(repo_id, canonical host, Herdr workspace_id)`: each worktree workspace
gets its own main, not the main checkout workspace's coordinator. Workspace
settings stay on their owning host and are never replicated; peers receive each
agent's authoritative role through status export.

| Mode | Roles and automatic behavior |
| --- | --- |
| `off` | No roles, automatic wakes, or participating legacy rejection. Explicit inbox reads and receiving at natural user-turn boundaries remain possible. |
| `manual` | An active explicit main in the workspace coordinates its selected worker members, excluding the main. Nonmembers can receive at natural boundaries but do not wake automatically. |
| `auto` | An active explicit main in the workspace wins. Otherwise its single active agent becomes main; with several active agents, exactly one in the repository's main checkout becomes main. Otherwise there is no main. All other agents in that scope are workers, even when no main can be selected. |

Sidebar roles and HAM-owned pane borders use each agent's current host-qualified
workspace scope. Borders show `[메인] alias · summary` or `[워커] alias · summary`.
Missing roles or summaries are omitted; the alias stays before the summary so
Herdr can truncate long summaries without discarding the identity first.
`status clear` removes phase/summary but keeps the alias and any active role.
Manual user borders are preserved. Agents keeps the addressable alias last in
`display_agent`; phase appears separately in HAM's `$recap` token.

The home is the canonical host proposed at first registration (or when an
already-known repository first enables participation); it is read-only in Settings.
Concurrent proposals converge by canonical host name. A canonical host name plus UUID is distinct from an
SSH alias or hostname. Agents use stable terminal slots with session generations;
pane moves do not change the slot. Uncertain claims from an old generation are
never automatically replayed. The TUI's evidence-based operator resolution is
an audit decision, not native delivery proof or completion of the work.

### Remote transport and receiving runtimes

Mailbox targets may be `local/LABEL`, `CANONICAL_HOST/LABEL`, a registered
`SSH_ALIAS/LABEL`, or `main`. `--to main` resolves on the sender's host at send
time to the sender's current workspace main and stores a concrete terminal
target. A scope without a main fails visibly; it never falls back to a different
workspace or the repository home. Explicit named mail can still cross workspaces
within the same repository. Initialize each host with `ledger setup --name HOST`,
then register each direction with
`ledger peer add --ssh-alias ALIAS`. `ledger doctor` and `ledger outbox` expose
transport state. A detached, code-only flock worker retries transient failures
with the same message IDs; permanent failures remain visible for explicit retry.
It does not run model polling or replicate databases.

Ledger schema v7 migrates active local legacy main/member targets into their
current workspace scopes and removes obsolete repo-level role settings.
Protocol 1 remains unchanged: legacy `main`, `members` and `computed_main`
setting payloads succeed as no-ops. See [migration details](docs/migration.md#workspace-role-migration-schema-v7).

Skill installation does **not implicitly** install a receiving runtime. Opt in
through the installer's explicit runtime choice/flag or separately
on each host with `runtime install omp` or `runtime install claude`; inspect or
remove it with the corresponding `runtime status` / `runtime uninstall` command.
Restart only the selected sessions. The installers own only HAM's extension or
hook entries and preserve unrelated Herdr/client configuration.

During OMP startup, `native_session_pending` means Herdr has not yet published
its native session path. HAM waits within its bounded registration retries
without changing that identity. Successful registration clears HAM's waiting or
unavailable status; exhausted retries remain unavailable. A genuinely conflicting
reported path is still rejected as `stale_session`.

- **omp:** The root Herdr session can receive at a natural turn boundary.
  Automatic action wakes additionally require an eligible role, supported empty
  editor, quiet input, idle session, and the configured coalescing/rate limits.
  Focus alone does not hold mail. ACK means the exact bundle was observed in the
  same session's active-branch journal, not provider acceptance or understanding.
- **Claude Code:** Bounded `SessionStart` and `UserPromptSubmit` hooks receive
  mail; there is no Stop/idle wake or code-worker-created Claude turn. Hook
  output alone is not delivery. Native ACK supports two evidence paths: an exact
  bundle in the SDK system/request blob plus a fresh request trace
  (`claude:api-request:`), or typed native hook raw/rendered records matching the
  exact claim bytes/hash plus a native causal request witness
  (`claude:accepted-journal:`). The latter proves host-context application, not
  exact provider-wire bytes, provider attestation, model self-report, or
  understanding. Both use `confirmation_source=native`; operator resolutions
  remain `operator`.
- **Codex:** Pull with `inbox read` at work boundaries, do the relevant work,
  then explicitly `inbox handle`. There is no automatic receiving adapter or wake.

Delivery is not handling. Direct user input separately records terminal
acceptance, never native mailbox delivery; its header is
`Message from user (HAM):`.

A bounded live check on Claude Code 2.1.301 observed production SessionStart
context and the current-generation worker's persisted `delivered` receipt with
`via=hook`, native confirmation, and `claude:accepted-journal:` evidence.
The same registered process added no primary turn or claim during separate
6.02-second manual-worker and auto-main idle windows, with the worker active and
action/FYI mail queued. Auxiliary `quota_check` / `prompt_suggestion` requests are
separate; this is not a claim of zero provider traffic. Herdr already knew the
session UUID: cold-unreported binding CAS is unit-proved, not live-proved.
These bounded observations do not establish whole-runtime or provider success.

See the [mailbox command reference](.agents/skills/herdr-agent-messenger/reference.md#local-mailbox)
for exhaustive flags, identity, storage, receipt/recovery semantics, and runtime
installation; see the [UI reference](docs/ui-reference.md) for Settings, quick-send,
replies, and uncertain-claim recovery.

## Deployment order

Upgrade the protocol-capable plugin on **each host first**. Make the matching
skill and receiving runtime ready before enabling participating policy, so
legacy coordination is not refused before recipients can receive mail. Codex
uses its skill-driven pull workflow instead of a runtime adapter. Only afterward
should the user choose any global binding and migrate status/recap conventions.
No global binding is changed automatically. This is deployment guidance, not a
claim that hosts or sessions have already been upgraded. See the
[rollout guide](docs/rollout.md) for the detailed procedure.

## Documentation

| Guide | English | 한국어 |
| --- | --- | --- |
| Skill installation and prompting | [Skill guide](docs/skill-guide.md) | [스킬 가이드](docs/ko/skill-guide.md) |
| Installation and 0.7.x migration | [Migration](docs/migration.md) | [마이그레이션](docs/ko/migration.md) |
| SSH discovery and routing contracts | [SSH and routing](docs/ssh-and-routing.md) | [SSH 및 라우팅](docs/ko/ssh-and-routing.md) |
| Popup behavior and keyboard controls | [UI reference](docs/ui-reference.md) | [UI 참고서](docs/ko/ui-reference.md) |
| Safe remote deployment | [Rollout](docs/rollout.md) | [원격 배포](docs/ko/rollout.md) |

## Test

```bash
python3 -m unittest -v
```

Each Python test module imports `ham_test_support` before product modules. Its
test-only tripwire rejects attempts to bootstrap the runner's default ledger
(including HOME, XDG state, an inherited ledger override, and the account's real
home), before any directory or schema is created. Even a caught tripwire fails
the responsible test. Fixtures must supply a private ledger directory or private
HOME/XDG environment to every code path they exercise; an explicit private
`LedgerStore` alone does not isolate incidental stores opened by other helpers.
The guard is not imported by production commands.
