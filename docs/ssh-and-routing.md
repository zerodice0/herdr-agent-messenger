# SSH discovery and routing

[한국어](ko/ssh-and-routing.md) · English · [README](../README.md)

## Discovery

HAM shows local agents immediately. When concrete `Host` aliases exist in
`~/.ssh/config`, remote discovery queries them asynchronously. Only hosts that
return a valid Herdr snapshot within five seconds are included. Results are
cached briefly so reopening the popup does not contact every host again.

Recursive SSH `Include` files are supported. Probes use non-interactive
authentication and disable SSH forwarding, agent forwarding, X11 forwarding,
and local commands. Hosts without required trust, authentication, connectivity,
or a running Herdr server are skipped.

Herdr lookup is shared by remote commands and the Python ledger entrypoints
(including mail acceptance, status export, and the worker). An executable
`HERDR_BIN_PATH` takes precedence; a server-reported ` (deleted)` suffix is
removed before checking it. Otherwise HAM tries `herdr` on `PATH`, then
`~/.local/bin/herdr`, `/opt/homebrew/bin/herdr`, `/usr/local/bin/herdr`, and
`~/.local/share/mise/shims/herdr`, in that order. These checks do not require a
login shell or edits to the remote shell startup files.

## Restrict authorized hosts

By default, HAM probes every concrete alias for backward compatibility. To limit
discovery, create `ssh-hosts` in the plugin config directory with one alias per
line:

```text
macbook-pro
winmini
```

Only aliases also present in SSH config are used. An empty file means local-only;
a missing file retains all-alias discovery. The normal path is:

```text
~/.config/herdr/plugins/config/herdr.agent-messenger/ssh-hosts
```

Set `HERDR_AGENT_MESSENGER_SSH_HOSTS_FILE` to override it. The legacy
`HERDR_AGENT_LABELS_SSH_HOSTS_FILE` and former plugin config directory remain
migration fallbacks. Set `HERDR_AGENT_MESSENGER_SSH_CONFIG` to use a different
SSH config; `HERDR_AGENT_LABELS_SSH_CONFIG` is its legacy fallback.

## Tailscale aliases

Tailscale needs no HAM-specific format. Point a stable alias at a MagicDNS name
or Tailscale IP:

```sshconfig
Host winmini
  HostName winmini.example-tailnet.ts.net
  User your-remote-user
```

HAM displays and authorizes only the explicit alias. It never treats every
device in the tailnet as authorized automatically.

## Delivery modes

After discovery, choose one of two modes:

- **Delegate through coordinator** is the default. The focused agent receives
  the complete request and verified worker routes, decomposes the task, sends a
  tailored message to each worker, waits for responses, and synthesizes the
  result. Workers see only the context the coordinator sends them.
- **Send directly** copies the complete original message to every selected
  recipient immediately.

The plugin does not semantically split work itself. Coordinator delegation can
use the embedded router path and route tokens even when the coordinator has no
globally installed HAM skill.

These modes have different privacy boundaries: delegate mode exposes the full
request only to the coordinator, while direct mode exposes it to every selected
agent, including agents reached over SSH.

## Route safety

Every operation resolves the current host and Agent Labels occupant again. HAM
does not silently reuse a stale pane occupant.

For CLI use, copy the exact `address` from `list` or `spawn` into
`--address HOST/LABEL` on `status`, `send`, `read`, or `request`, or into an `address` field
in a batch item. An item accepts exactly one of `address` and `route`; duplicate
resolved occupants are rejected even when represented differently. Only literal
`local` means local transport; every other host must be a configured concrete
SSH alias. A non-default `--host` must agree with `--address`.

When asked to start a new session, use HAM `spawn`, then message its returned
address. Raw `herdr agent start <name>` bypasses HAM label selection and metadata
reporting. Never create a replacement merely because an existing label is
missing. Remote spawning requires `--cwd` on that host and, for a split,
`--from-pane` on that host.

V2 route tokens contain the host, an occupant fingerprint, and hashed continuity
fields rather than raw session IDs or working directories. A stale registered
label can refresh only when pane, workspace, and agent kind still match uniquely.
Unlabeled or display-only routes require stricter continuity. Ambiguous or
changed occupants remain `route_expired`. V1 tokens remain readable for exact
matches.

`status`, `send`, `read`, and request payloads expose `route_refreshed` and a
current `route`; `spawn` also returns the new occupant's `route`. Use that token
for later GUI-style operations. A coordinator prompt's supplied absolute CLI
path and opaque route tokens work without a globally installed skill and can
select unlabeled agents. Do not decode, modify, or replace tokens with guessed
commands. If `route_refreshed: true`, retain the new token.

## Stored metadata

Remote prompts include the sender's local hostname and agent label. The private
`0600` discovery cache stores remote labels, pane/session metadata, status,
workspace paths, and host aliases under `HERDR_PLUGIN_STATE_DIR`.
