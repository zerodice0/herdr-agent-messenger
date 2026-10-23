# Installation and migration

[한국어](ko/migration.md) · English · [README](../README.md)

## Requirements

- Herdr 0.8.0 or newer on every participating host.
- Python 3.10 or newer on Herdr hosts.
- Python 3.11 or newer to run the optional `python3 -m ham rollout` SSH deployment helper.

## Python package layout

Product Python code lives under `ham/`: `agents/`, `ledger/`, `ui/`, `ops/`,
and `integrations/{omp,claude}/`. `ham/paths.py` defines `PLUGIN_ROOT`; the root
contains both `ham/__init__.py` and `herdr-plugin.toml`. Herdr manifest actions,
receivers, and SSH execute with plugin-root cwd:
`cd /ABS/PLUGIN_ROOT && python3 -m ham <dispatch> ...`.
Dispatch names are `cli`, `labels`, `delivery`, `mailbox`, `messenger`,
`skill-installer`, `worker`, `display`, and `rollout`; ledger commands use
`python3 -m ham cli ledger ...`. For caller-relative `spawn`, `--message-file`,
`--body-file`, or `--destination`, prefer the absolute copied/bundled
`scripts/herdr_agent_messenger.py` launcher: it discovers the plugin root and
preserves caller context. It starts `python3 -m ham --cwd <CALLER_DIRECTORY> cli ...`
with process cwd at the discovered plugin root, so the pinned package loads before
the dispatcher restores caller cwd. Direct equivalent:
`cd /ABS/PLUGIN_ROOT && python3 -m ham --cwd /ABS/CALLER cli ...`.
Without `--cwd`, module execution at the plugin root makes relative arguments
root-relative. Do not use `PYTHONPATH` to launch from an arbitrary cwd.

This is a **flag-day cutover across all five hosts together**, not a rolling
upgrade. Protocol **1** and the ledger schema remain unchanged, but mixed old/new
peer executable paths fail. Stop cross-host traffic and old HAM workers/TUIs,
upgrade every host, refresh copied skills, and, with operator consent, reinstall
`runtime install omp` / `runtime install claude` on every host where that runtime
is enabled. Restart OMP sessions loading the receiver, Claude sessions loading
the hook, and HAM workers/TUIs before resuming traffic. Retry failed outbox items
with `python3 -m ham cli ledger retry --item OUTBOX_ITEM_UUID` after every host
has upgraded; do not send replacement messages. The older schema-only notes
below do not waive this package cutover's reinstall/restart requirements.

## New installation

Install and enable HAM from the public GitHub repository:

```bash
herdr plugin install zerodice0/herdr-agent-messenger --yes
```

For local development, link a checkout instead:

```bash
herdr plugin link "$PWD" --enabled
```

The plugin's display name is **HAM** and its ID is `herdr.agent-messenger`.
Existing unnamed agents can be labeled with **Assign Agent Label**. Unnamed
agents detected after installation receive `color-animal` labels such as
`blue-otter`, with matching color markers. Manually named agents are left unchanged.

### Receiving dependency

- **Plugin:** ledger, mailbox, UI, and transport.
- **Skill:** agent instructions and CLI launcher, not a receiver.
- **OMP adapter:** native receiving and eligible action/FYI wake.
- **Claude hooks:** native receiving at turn boundaries; the ledger worker adds an idle wake.
- **Codex:** no adapter is required; the ledger worker wakes idle sessions and the skill pulls at work boundaries.

For new installs and upgrades, install/update the [client skill](skill-guide.md#install-with-the-plugin),
then run the read-only summary on **every selected host**, with cwd set to its plugin root:

```bash
python3 -m ham cli runtime status
# Only with user consent, for each used client (omp or claude):
python3 -m ham cli runtime install omp
```

Plugin and skill installation alone are not proof of native receiving readiness.
Missing runtimes require explicit consent: the interactive installer defaults to
**Not now**, and `python3 -m ham skill-installer install all --runtime omp|claude|all`
opts in explicitly. Never replace foreign or modified files. Tell the user which
sessions need restarting; restart only the sessions they select. After restart,
confirm `extension_present=1`, or report **installed, activation pending**.
Plugin loading never writes client configuration; the new-repository `auto`
default is unchanged. See the [receiving setup guide](skill-guide.md#receiving-dependency).

### OMP receiver bell targeting

OMP receivers watch their own terminal bell rather than the Mailbox TUI's
`_board` bell. Ledger mutations must ring every local terminal whose mailbox,
turn state, session identity, or effective role/settings changed. Registry
heartbeat freshness, display/phase changes, and unrelated board activity do not
wake receivers. `_board` still refreshes the Mailbox TUI, and the receiver's
five-minute registration/safety sweep remains enabled.

Upgrade the ledger writers and reinstall the OMP receiver together before
restarting the selected sessions: an old writer may rely on `_board` alone for
settings or operator claim-resolution notifications. No ledger schema change is
required. Plugin-root discovery still runs per receiver operation so installation
changes are not hidden by a cached checkout; eliminating unrelated wake probes
also eliminates their associated discovery subprocesses.

## Sticky auto main (schema v12)

Schema v12 additively creates local-only `auto_main_incumbents`, keyed by
`(repo_id, host, anchor_workspace_id)`, with `terminal_id` and `recorded_at`.
Writable opens accept both v10 and v11, preserving user settings and mail data;
v10 also receives the v11 additions. Migration itself does not elect a main.
Back up the ledger, upgrade code and copied skills, run `ledger reconcile`,
and restart the host's HAM processes together. Read-only opens never migrate;
old code cannot open v12 and downgrade requires the pre-upgrade backup.

Auto precedence is active explicit pin, valid incumbent, then the existing
unique-candidate rule. The incumbent must still be active in the same scope
and physically in its anchor workspace. Registry reconciliation records a
unique candidate only without an active pin or valid incumbent, and removes
invalid incumbents in the same transaction. Reads never record elections.
Incumbency is terminal-keyed: a native session restart in the same terminal
keeps it. A later main-checkout sibling does not demote the incumbent; when
the incumbent leaves, the next unique candidate takes over. Two candidates
in the first snapshot still produce no main. Linked siblings cannot steal it.

This is election memory, not a user setting or explicit pin. Manual/off modes
ignore it; clearing an explicit pin allows incumbent/computed auto election.
No `workspace_settings` rows are rewritten. `repo show` adds read-only
`main_source` (`pinned`, `auto`, or null); agent status exports and protocol 1
are unchanged. Incumbents are never replicated; peers consume origin-host roles.

## Sender dismissal (schema v11)

Schema v11 additively creates local `mail_dismissals` and nullable
`messages.sender_context`; v10 is explicitly accepted as an upgrade source.
Existing identities, settings, bodies, delivery states, hashes and receipts are
not rewritten. The audit has no message-body foreign key and keeps
sender/group identity plus failure snapshots, so body retention cannot erase
the first dismissal or force a body to be restored on an authorized retry.
Dismissal does not reset `final_at`, delivery `updated_at`, or retention deadlines.

New agent sends snapshot verified sender identity and anchor scope locally,
outside immutable payload hashes and peer envelopes. A closed linked workspace
and deleted sender agent cannot revoke the original anchor main's authority.
Existing v10 messages receive null context, not an invented historical group.
For those messages, a main override requires surviving recorded sender identity
and workspace metadata proving the group; missing linked-workspace/anchor
evidence fails closed. The original terminal may still dismiss with verified
sender-repo ownership evidence. No arbitrary same-repo main is inferred.

Before an approved deployment, back up the host database and stop old HAM
processes sharing it. Upgrade host code and copied skills together, run
`python3 -m ham cli ledger reconcile` for writable migration, then restart the
host's HAM workers, TUIs and other processes loading HAM code. Read-only opens
never migrate. Old code cannot open v11; downgrade requires restoring the
pre-upgrade database, not changing its version marker. This feature does not
itself require an OMP/Claude runtime reinstall or change their contracts.
Deployment and cleanup of real mail still require explicit user authorization.

This is **origin-ledger attention resolution**, not replicated recipient
handling. Protocol 1, delivery states, outbox kinds and remote receipts remain
unchanged. No new receipt, native ACK, wake or handled timestamp is generated.
Recipient-host history stays unchanged. See
[`mail dismiss`](../.agents/skills/herdr-agent-messenger/reference.md#sender-dismissal).

## Turn-end FYI (schema v10)

Schema v10 adds local `deliveries.fyi_turn_end INTEGER NOT NULL DEFAULT 0`.
Existing v9 deliveries stay conservatively unflagged: old records cannot prove
the recipient was working when they arrived.

Eligible action wakes an idle participating OMP promptly. Queued FYI becomes
wake-eligible 600 seconds after local recipient enqueue for batched idle wakes,
under the same safeguards. This is a minimum, not a scheduled wake: natural
events or the five-minute safety check normally yield about ten to fifteen minutes.
This delayed path adds no schema or settings: existing queued FYI uses its
local delivery timestamp without migration.

Claude Code and Codex idle sessions are now woken by the ledger worker, with no
schema, protocol or integration change and no new runtime to install (Claude still
needs its registered hook). Both use the same gates as above, with idle time measured
from the durable status log. See [Idle wake for Claude Code and Codex](skill-guide.md#idle-wake-for-claude-code-and-codex).

The default thresholds are now `coalesce_seconds` 60 and `wakes_per_hour` 4
(`idle_minutes` 3 and `max_hop` 10 are unchanged). They apply only to repos with
no saved `thresholds`; repos that already saved thresholds keep their saved
values, and no setting or schema changes are needed.

During local recipient enqueue, a locally reconciled `working` state with
`agents.last_seen` within 15 seconds sets the turn-end flag (normal local
reconciliation sweeps run every five seconds). Flagged FYI is delivered right
after that turn under existing wake gates, without waiting 600 seconds.
Idle, unknown or stale enqueue state does not set the flag but still permits
the delayed path once eligible. Remote wire payloads do not carry this flag;
the final recipient's ledger decides locally.

Back up the ledger and stop old HAM workers/Mailbox processes. Upgrade host
ledger code and CLI/copied skills together, then run upgraded `ledger reconcile`
and start replacement HAM processes. Older enqueue code can otherwise miss the
flag, and old code cannot open v10 storage. Read-only opens do not migrate. Downgrade requires restoring
the pre-upgrade database. OMP extension and Claude hooks are unchanged; this
policy requires no extension restart or runtime reinstall.

Each new TUI compose starts as `action`; Ctrl+G toggles `action` / `fyi` and
the selection resets when a new compose opens. CLI `--kind` still defaults to
`fyi`.


## Worktree role grouping (schema v9)

This is the current role contract; the schema-v7 section below is historical.
Roles, main/member selection, Settings mutations and `--to main` share
`(repo_id, canonical host, anchor_workspace_id)`. Mode, home and wake thresholds
remain repository-wide.

The additive `workspaces` table stores `host`, `workspace_id`, `repo_key`,
`is_linked_worktree` and `label` for open workspaces from the Herdr snapshot,
including workspaces with no agents. For the same repo and host, a linked
workspace anchors to a deterministically selected open nonlinked workspace
with matching Herdr `worktree.repo_key`. Without that metadata or an open
anchor, it retains its own workspace scope. There is no git-common-directory
fallback: ordinary workspaces must not be conflated.

Existing `workspace_settings` rows are not migrated, copied or deleted.
While linked workspaces are grouped, only the anchor's explicit main/member
settings apply; linked settings remain unchanged and apply again if ungrouped.
A valid explicit main may be anywhere in the group. Otherwise auto mode keeps
a valid anchor incumbent (schema v12), then elects from active group agents
only when the anchor has an active agent: the sole agent, or, with several,
the unique active main-checkout agent. An open anchor without an agent has
**no auto main**, even with a sole linked agent. Manual
workers are the anchor's selected group members except main; auto workers are
all other group agents. Opening/closing an anchor changes the group and refreshes
affected roles. `repo show` returns `workspace_id` as the resolved anchor and
`requested_workspace_id` as the original workspace.

`--to main` resolves the sender's group and saves a concrete target; it does not
choose another group or home. `[HOST/]main@REPO` counts distinct role groups,
not each linked workspace: zero mains gives `main_unresolved`, several groups
with mains give `main_ambiguous`. Named `HOST/LABEL` addressing is unchanged.

### Storage upgrade and mixed-version rollout

1. Back up each host's ledger database before upgrading. Stop its old HAM
   plugin/worker/Mailbox processes and receiving sessions that load HAM code;
   deploy the same upgraded code and refresh copied skills together on that host.
   Do not let old and new processes share the upgraded ledger.
2. From the updated HAM checkout on each host, run
   `python3 -m ham cli ledger reconcile`.
   Writable reconciliation upgrades older storage to the current schema
   (v12, including v9 workspace metadata). Read-only commands never migrate.
3. Restart the host's HAM processes with the upgraded code. The OMP extension
   and Claude hook contracts are unchanged; no new adapter/hook configuration
   is required. Resume group-dependent routing after the participating hosts
   have upgraded and reconciled.

Protocol remains **1**. Optional additive role-scope metadata in status exports
does not require peer support: old peers can display the origin host's computed
roles. They still compute old semantics for their own local agents until
upgraded. Legacy role-setting payloads remain successful no-ops; only repo-wide
settings replicate. Schema-v8 mail compatibility requirements still apply.
Wire compatibility does **not** imply shared-database compatibility: old code
cannot open a schema-v9 database. A downgrade requires restoring the backed-up
pre-upgrade database, not editing a schema-version marker.

## Workspace role migration (schema v7)
> Historical v6→v7 migration record. Its independent-workspace role semantics
> are superseded by [schema-v9 worktree grouping](#worktree-role-grouping-schema-v9).
> Use the v9 rollout above for current deployments.


At the time of schema v7, a writable ledger open upgraded older ledgers to v7.
During rollout, run `ledger reconcile` on each host before using read commands.
Read-only opening of a v6 ledger returns `schema_upgrade_required`, directing
the user to run `ledger reconcile`; it never migrates or otherwise mutates the
ledger on read.

Schema v7 assigned main and worker membership to
`(repo_id, canonical host, Herdr workspace_id)`, allowing independent worktree
mains. Schema v9 supersedes that election boundary with role groups. Mode,
home, wake thresholds and mail history remained repository-wide.

On each host, migration maps legacy repo-level `main` and `members` targets
matching active local agents into those agents' current scopes, grouping
members by scope. Targets on other hosts are handled by their own host's ledger
copy. Inactive or unknown local targets cannot be mapped safely and are dropped;
`ledger_meta.workspace_settings_migration_dropped` records JSON entries
`{repo_id,field,target}`. Re-select any needed active replacements in Settings.
The upgrade removes repo-level `main`, `members` and `computed_main` rows and
their obsolete member/conditional-setting projections. The unique-candidate
fallback is derived on read; schema v12 separately remembers incumbents during
registry reconciliation, never as `computed_main` settings. A stale explicit designation
remains stored but is ignored while its target is inactive or outside the scope.

Workspace role settings stay on the owning host and are never replicated.
Peers learn each agent's role from that agent's origin-host status export.
Protocol remains 1: incoming old-peer `main`, `members` and `computed_main`
setting payloads return success without applying changes, preventing permanent
old-outbox failures. Home/mode/threshold replication is unchanged. Older
versions still compute their own old roles; upgrade each host to obtain
workspace-scoped roles there.

`--to main` now resolves on the sender's host when sending and saves a concrete
terminal target for the sender's current workspace main. If there is no main,
it fails visibly instead of waiting for a repo home or choosing another
workspace. Legacy unresolved symbolic messages, including remote-origin sends
and home relays, fail with `legacy_symbolic_main` and an instruction to upgrade
every host. Upgrade every host before using `--to main` across hosts.
Mailbox Settings labels repo-wide Mode/Thresholds separately from the
opener's workspace Main/Members. Update copied skills along with the host
code so agents use this routing and role contract.

The historical schema-v7 deployment sequence was:

1. Update the HAM code and copied skills on every host; replace old Mailbox
   panes and restart receiving runtimes/workers with the upgraded code.
2. On **every host**, run the upgraded skill's command:
   `python3 -m ham cli ledger reconcile`
   from the HAM checkout. This explicit writable step upgrades that host's
   ledger before read-only status or peer exports are used.
3. Only after every host is upgraded and reconciled, resume cross-host
   `--to main` use. Explicit `HOST/LABEL` addressing is unchanged.

## Migrate from 0.7.x

HAM 0.8.0 changes the plugin ID from `herdr.agent-labels` to
`herdr.agent-messenger`. The repository and install source are now
`zerodice0/herdr-agent-messenger`, renamed from `herdr-agent-labels`. The old
GitHub URL redirects, so existing GitHub installs keep updating. The repository
rename does not replace the legacy plugin-ID migration below.

For an existing GitHub installation, disable the legacy ID, install the new
version, verify it, and only then remove the legacy registration:

```bash
herdr plugin disable herdr.agent-labels
herdr plugin install zerodice0/herdr-agent-messenger --yes
herdr plugin list --plugin herdr.agent-messenger --json
herdr plugin action invoke agent-skill-guide --plugin herdr.agent-messenger
herdr plugin uninstall herdr.agent-labels
```

For a local development link, update the checkout and reload Herdr. Local links
are path-backed, so Herdr rereads the 0.8.0 manifest and adopts the new ID
without an unlink/relink cycle:

```bash
git pull --ff-only
herdr config check
herdr server reload-config
herdr plugin list --plugin herdr.agent-messenger --json
herdr plugin action invoke agent-skill-guide --plugin herdr.agent-messenger
```

## Compatibility behavior

During migration, HAM preserves these fallbacks:

- The legacy `ssh-hosts` allowlist is read when the new config path has no file.
- Legacy discovery cache and request-state directories are used when new state
  does not exist.
- Old `HERDR_AGENT_LABELS_*` variables remain fallbacks; new configuration
  should use `HERDR_AGENT_MESSENGER_*`.
- The 0.8.0 skill wrapper checks `herdr.agent-messenger` first and then the
  legacy plugin ID.

Open **HAM Skill** and update any copied project or system skills before
removing the legacy plugin. An unchanged 0.7.x skill only knows the old ID.

## Verify the final state

```bash
herdr config check
herdr server reload-config
herdr plugin list --plugin herdr.agent-messenger --json
herdr plugin action list --plugin herdr.agent-messenger
```

The final action list should contain `agent-skill-guide`, `label-current`, and
`message-agents`, with only `herdr.agent-messenger` enabled.
