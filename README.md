<p align="center">
  <img src="assets/ham-logo.png" alt="HAM — Herdr Agent Messenger mascot logo" width="360">
</p>

# HAM — Herdr Agent Messenger

[한국어](README.ko.md) · English

HAM is an agent-labeling and multi-agent messaging plugin for Herdr. It gives
unnamed agents memorable `color-animal` names, starts new labeled sessions, and
routes prompts from a keyboard-first popup, Codex, Claude Code, or omp.

## What HAM provides

- Automatic labels such as `blue-otter` with matching color markers
- Local and SSH agent discovery using concrete aliases from `~/.ssh/config`
- Coordinator delegation and direct multi-recipient delivery
- A bundled `herdr-agent-messenger` skill for Codex, Claude Code, and omp, split into short `SKILL.md` and on-demand `reference.md`
- Labeled interactive-session creation with `spawn`, plus safe request, wait, read, batch, and rollout helpers
- Focus-based holds for agent messages, with a 3-minute default timeout and explicit Push/Cancel in **HAM: Held Messages**
- Compact CLI output by default, with `--verbose` diagnostics and `request-status --wait` follow-up

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

## Use

Open **Send Prompt to Agents** from a Herdr agent pane to select recipients and
choose coordinator delegation or direct delivery.

Codex users can type `$ham` and select **HAM — Herdr Agent Messenger**. Claude
Code users can invoke `/herdr-agent-messenger` or make a matching natural-language
request. omp users can ask naturally to use HAM or read
`skill://herdr-agent-messenger` after installing the omp skill target.

```text
$ham Ask local/yellow-falcon what language label dxp-ui uses. Wait for the
reply, then align this work and its defaults with that answer.
```

Use `local/LABEL` for this machine, not its hostname, and a configured SSH alias
for remote recipients. A bare label can collide across hosts or be mistaken for
a client's own subagent.

To start a new agent and message it, ask HAM explicitly to create the session.
HAM uses `spawn`, then `request --address` with the returned address; it does not
silently create a session when an existing label is missing. From a checkout:

```bash
python3 .agents/skills/herdr-agent-messenger/scripts/herdr_agent_messenger.py spawn \
  --kind omp --cwd "$PWD" --placement tab
# Copy the returned address, then:
python3 .agents/skills/herdr-agent-messenger/scripts/herdr_agent_messenger.py request \
  --address RETURNED_HOST/RETURNED_LABEL --message 'Review this project.'
```

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
overlap was lost. See the [skill guide](docs/skill-guide.md) for flags and errors,
including `invalid_defer_timeout` for a nonpositive or invalid hold timeout.

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

The checkout also provides a non-interrupting local mailbox. From a registered
Herdr agent pane, use `mail send --to local/LABEL --body TEXT`, then let the
recipient use `inbox read` and `inbox handle --message UUID`. A read never marks
mail delivered or handled, including reads from task agents sharing the pane.
`mail show --thread UUID` shows the thread; `mail withdraw --message UUID`
withdraws only mail that has not already been claimed.

Mailbox commands also accept `--to CANONICAL_HOST/LABEL` (or the registered SSH
alias) and `--to main`, resolved once by the repository's home host. Run
`ledger setup --name HOST`, register each direction with `ledger peer add
--ssh-alias ALIAS`, and inspect `ledger doctor` / `ledger outbox` for transport
state. The detached flock worker retries transient link failures with the same
message ID; permanent failures are visible and explicitly retriable without
blocking unrelated mail. These commands do not submit a terminal prompt or wake
a model. Existing `send`/`request`, Held Messages, and their behavior are unchanged. See the
[mailbox command reference](.agents/skills/herdr-agent-messenger/reference.md#local-mailbox)
for flags, state/recovery rules, storage isolation, and identity configuration.

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
