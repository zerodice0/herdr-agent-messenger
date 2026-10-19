# Installation and migration

[한국어](ko/migration.md) · English · [README](../README.md)

## New installation

Install and enable HAM from the public GitHub repository:

```bash
herdr plugin install zerodice0/herdr-agent-messenger --yes
```

For local development, link a checkout instead:

```bash
herdr plugin link "$PWD" --enabled
```

Existing unnamed agents can be labeled with **Assign Agent Label**. Agents
detected after installation are labeled automatically.

## Workspace role migration (schema v7)

A writable ledger open upgrades older ledgers additively to schema v7.
During rollout, run `ledger reconcile` on each host before using read commands.
Read-only opening of a v6 ledger returns `schema_upgrade_required`, directing
the user to run `ledger reconcile`; it never migrates or otherwise mutates the
ledger on read.

Main and worker membership now belong to `(repo_id, canonical host, Herdr workspace_id)`, so
each worktree workspace can have its own main. Mode, home and wake thresholds
remain repository-wide; mail history and explicit named cross-workspace mail
remain repository-wide too.

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

Deploy schema v7 on **every host**, in this order:

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
