# Installation and migration

[한국어](ko/migration.md) · English · [README](../README.md)

## Requirements

- Herdr 0.8.0 or newer on every participating host.
- Python 3.10 or newer on Herdr hosts.
- Python 3.11 or newer to run the optional `rollout_plugin.py` SSH deployment helper.

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
- **Claude hooks:** native receiving at turn boundaries, with no idle wake.
- **Codex:** skill-guided inbox pull, with no adapter required.

For new installs and upgrades, install/update the [client skill](skill-guide.md#install-with-the-plugin),
then run the read-only summary on **every selected host**:

```bash
python3 .agents/skills/herdr-agent-messenger/scripts/herdr_agent_messenger.py runtime status
# Only with user consent, for each used client (omp or claude):
python3 .agents/skills/herdr-agent-messenger/scripts/herdr_agent_messenger.py runtime install omp
```

Plugin and skill installation alone are not proof of native receiving readiness.
Missing runtimes require explicit consent: the interactive installer defaults to
**Not now**, and `agent_skill_installer.py install all --runtime omp|claude|all`
opts in explicitly. Never replace foreign or modified files. Tell the user which
sessions need restarting; restart only the sessions they select. After restart,
confirm `extension_present=1`, or report **installed, activation pending**.
Plugin loading never writes client configuration; the new-repository `auto`
default is unchanged. See the [receiving setup guide](skill-guide.md#receiving-dependency).

## Turn-end FYI (schema v10)

Schema v10 adds local `deliveries.fyi_turn_end INTEGER NOT NULL DEFAULT 0`.
Existing v9 deliveries stay conservatively unflagged: old records cannot prove
the recipient was working when they arrived.

Eligible action wakes an idle participating OMP promptly. Queued FYI becomes
wake-eligible 300 seconds after local recipient enqueue for batched idle wakes,
under the same safeguards. This delayed path adds no schema or settings:
existing queued FYI uses its local delivery timestamp without migration.

During local recipient enqueue, a locally reconciled `working` state with
`agents.last_seen` within 15 seconds sets the turn-end flag (normal local
reconciliation sweeps run every five seconds). Flagged FYI is delivered right
after that turn under existing wake gates, without waiting 300 seconds.
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
A valid explicit main may be anywhere in the group. Otherwise auto mode elects
from active group agents only when the anchor has an active agent: the sole
agent, or, with several, the unique active main-checkout agent. An open anchor
without an agent has **no auto main**, even with a sole linked agent. Manual
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
   `python3 .agents/skills/herdr-agent-messenger/scripts/herdr_agent_messenger.py ledger reconcile`.
   Writable reconciliation upgrades older storage to the current schema
   (v10, including v9 workspace metadata). Read-only commands never migrate.
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
their obsolete member/conditional-setting projections. The computed main is
derived on read rather than saved or published. A stale explicit designation
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
   `python3 .agents/skills/herdr-agent-messenger/scripts/herdr_agent_messenger.py ledger reconcile`
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
