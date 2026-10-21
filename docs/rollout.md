# Safe SSH rollout

[한국어](ko/rollout.md) · English · [README](../README.md)

`rollout_plugin.py` installs, migrates, or updates HAM on explicitly selected
remote Herdr hosts. It accepts only full 40-character commit SHAs and concrete
SSH aliases authorized by the same SSH config and optional `ssh-hosts` allowlist
used by Agent Messenger. It never discovers or adds rollout targets itself.

## Preview

Review the exact commands without opening an SSH connection:

The preview includes conditional migration steps. `legacy-disable` runs before
the new plugin is enabled; legacy removal and its verification run after the new
plugin passes validation. Those steps execute only when preflight finds the
legacy installation.

```bash
python3 rollout_plugin.py \
  --host macbook-pro \
  --ref 0123456789abcdef0123456789abcdef01234567 \
  --dry-run \
  --format json
```

## Apply

`--confirm` authorizes installation or update and the Herdr server reload on the
listed hosts:

```bash
python3 rollout_plugin.py \
  --host macbook-pro \
  --host winmini \
  --ref 0123456789abcdef0123456789abcdef01234567 \
  --confirm \
  --format json
```

## Validation profiles

The default `smoke` profile checks:

- enabled state
- exact GitHub source and resolved commit
- manifest version
- `herdr config check`
- server config reload
- `agent-skill-guide`, `label-current`, and `message-agents`

Add `--profile full` to compare SHA-256 hashes for every file tracked by the
target commit and run the complete unittest suite. Full tests disable bytecode
writes.

Herdr currently enables a plugin during installation. The helper therefore
disables the new install immediately, validates it, and only then re-enables it
and reloads the server. This minimizes, but cannot remove, the short interval
between install and disable.

## Migration and rollback

Before a change, the helper records the current `herdr.agent-messenger` or
legacy `herdr.agent-labels` installation, including its exact GitHub commit and
enabled state. A migration removes the legacy ID only after the new ID passes
validation.

Any install, migration, or post-install validation failure triggers a
best-effort rollback to the recorded identity and state. If HAM was previously
absent, the failed installation is removed. Each host result reports rollback
success or failure.

Hosts are independent: a failure on one does not stop later targets. Exit codes
are:

- `0`: every host passed
- `1`: at least one host rollout or validation failed
- `2`: approval was missing or preflight input was invalid

The helper updates the Herdr plugin registration, not separately copied Codex or
Claude skill directories. After a rollout, open **HAM Skill** on that host and
update any project- or system-installed skill copy.

For schema v10, upgrade ledger code and CLI/copied skills together before
resuming enqueue: older CLI code misses the turn-end FYI flag. Existing v9
deliveries remain unflagged. The OMP extension is unchanged; no extension
restart is needed for this policy. Follow the [schema-v10 migration](migration.md#turn-end-fyi-schema-v10)
for process replacement and database backup/rollback.
