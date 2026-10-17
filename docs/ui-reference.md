# HAM mailbox and popup UI reference

[한국어](ko/ui-reference.md) · English · [README](../README.md)

## Open the mailbox

**HAM: Mailbox (Right Split)** (`herdr.agent-messenger.mailbox-split`) opens a
plugin-owned pane to the right of the opener. **HAM: Mailbox (Tab)**
(`herdr.agent-messenger.mailbox-tab`) uses the same entrypoint in a tab. A shell
pane can open it; the opener does not need to contain an agent. These actions do
not change your keybindings or install a runtime.

HAM remembers the actual mailbox pane and terminal IDs per workspace. Opening
again focuses that live plugin pane, regardless of its label or another pane
being named `HAM`. The opener pane and terminal are passed separately to the
mailbox. Outside the editor, `q` closes only the plugin-owned mailbox pane;
`Esc` first leaves an editor or detail view.

### Four screens

- **1 Status:** local and configured peer agents, canonical host, role, turn,
  phase, unread count (`queued` or internally claimed), and extension presence.
  Missing extension support is shown explicitly, not as a delivery failure.
  A disconnected host retains its last snapshot marked stale, with unread shown
  as `?`; no offline host is inferred to have closed its agents. `Enter` inspects
  an agent. Select an agent and press `c` to choose its repository and compose.
- **2 Threads:** retained message bodies and delivery/hold states, withdrawal
  requests, direct submissions, and pending/failed OUTBOX entries. `Enter`
  inspects the selected entry; arrows scroll details. Viewing does not deliver
  or handle a message. `w` withdraws an eligible locally originated message;
  `t` explicitly retries the selected OUTBOX item with its existing identity.
- **3 Compose:** sender is always `user`. `Tab` switches subject/body; `Enter`
  inserts a body newline. `Ctrl+K` switches `fyi`/`action`. `Ctrl+D` switches
  mailbox/explicit **USER direct input**. `Ctrl+S` reviews the selected route;
  a second `Ctrl+S` confirms. Ordinary letters and digits, including `q`, are
  text while editing. `Esc` returns to the board.
- **4 Settings:** exactly seven rows are editable with `Enter`: `mode`
  (`off`/`manual`/`auto`), `main`, `members`, `idle_minutes`,
  `coalesce_seconds`, `wakes_per_hour`, and `max_hop`. Main is a
  canonical-host/terminal JSON target or `null`; members are an array of those
  targets. After choosing a board agent with `c`, `m` designates that agent as
  main and `Space` toggles its manual membership. Home (the canonical host where
  the mode was first enabled), `computed_main`, version/origin stamps,
  propagation pending and conflicts are intentionally read-only. These controls
  call the internal settings service; agent `repo show` stays read-only.

Only one settings save can be in flight. **Saving settings** marks the pending
operation; further save submissions or toggles are rejected, not queued. Wait for
the result, then retry the intended change. Text edits are retained if their save
is rejected. Navigation remains responsive, and `q` still closes outside text
editors while saving.
The authoritative result is shown immediately, and stale snapshots cannot replace
it.

`Page Up` / `Page Down` scroll long details and settings metadata, including
conflict records beyond the editable rows. Conflict metadata wraps without losing
values, winners, versions or origins, so the full records remain available.
Outside the composer, `?` opens the
full current error/status and peer-connection details when a short footer clips
them. Unsent draft edits made while a send is pending are retained.

### Honest acceptance and uncertainty

Mailbox composition follows normal native receiving rules. Direct input instead
submits the exact `Message from user (HAM):` header through the existing verified
terminal transport. It may interrupt the target, so it requires the explicit
route selection and confirmation. HAM durably records `via=direct` and
`submitting` before transport, then `accepted`, `unknown`, or `failed`; these are
not native `delivered`/`handled` receipts. Inspect direct results in Threads.
An interrupted `submitting` or `unknown` submission must be checked before
another explicit send; HAM does not replay it automatically.

An uncertain native claim remains held, including after a session replacement.
In its thread, `a` confirms applied or `n` confirms not-applied. Both open a
warning and require nonempty operator evidence before `Enter` confirms.
Applied refers to the old claimed agent/generation and is labeled a **manual
user confirmation**, not runtime-journal evidence or provider understanding.
Not-applied releases exactly that claim; it does not itself resend anything.
`Esc` cancels without changing the claim.

The open TUI stats the local board bell once per second. Peer status queries
run independently every 30 seconds in bounded child processes, never inside the
keyboard read loop. Closing stops only its own workers. These are code-only
checks and do not create model turns. Korean input, terminal-cell clipping,
resize and compact-screen instructions use the same text helpers as the legacy
messenger.

### Role, phase and border ownership

HAM displays roles before the alias, for example `🟧 [메인] orange-rabbit`, so
the alias remains addressable. A shared projection reports alias, color,
display name and phase (`recap`) under the HAM metadata source. Device tokens
such as `phone` and `emu` and other sources are not overwritten.

When status/roles are invoked, summary supplies `[메인] summary` or
`[워커] summary` on the border; off-mode status has no role prefix. HAM updates
only a blank border, a verified legacy border, or its own last recorded border
value. A differing user rename is preserved. The old `user:pane-recap` token is
cleared only when the new status/role feature is invoked; this UI does not
deploy or remove global recap rules.

## Legacy direct-prompt popup

The existing **Send Prompt to Agents** and **Held Messages** actions remain
available. The sections below describe that older popup, not mailbox delivery.

## Open the messenger

Run **Send Prompt to Agents** from a pane containing an agent. The focused agent
becomes the fixed sender. If no agent is running in the focused pane, HAM shows
an error notification instead of opening the popup.

Local agents appear immediately, sorted by Herdr workspace label. Unicode labels
are preserved and worktree-backed workspaces use a `WT:` prefix. Remote agents
are grouped under their SSH host alias. Within each host, agents are grouped
again under their workspace. Workspace identity uses Herdr's `workspace_id`, so
different workspaces with the same visible label are kept separate and display
their IDs for disambiguation.

## Recipient rows

Interaction and lifecycle indicators are separate:

- `›` keyboard cursor
- `[x]` selected recipient
- `●` working
- `○` idle
- `✓` done
- `!` blocked
- `~` stale

Color is only a redundant cue, so state remains readable in monochrome. Workspace
labels appear once in group headings instead of repeating on every agent row.
Each agent row prioritizes the Agent Labels name, Herdr pane ID, and lifecycle
state. The full session ID and workspace fields remain searchable without taking
the main row width. Host headings always show their agent count as `(n)`;
workspace headings use the same suffix only when they contain multiple agents.

Long groups keep both the host and workspace headings visible while scrolling.
When a compact viewport has room for only one sticky heading, the more specific
workspace heading takes priority. The recipient heading shows the visible range
when needed. Unavailable remote hosts are summarized; open the details view to
see each alias and its latest error.

## Message editor and layout

The popup grows up to 120 columns by 32 rows and shrinks to fit compact
terminals. Discovered recipients receive most of the available space while at
least four soft-wrapped message rows remain visible when possible. Long messages
follow the text cursor and display a viewport scrollbar.

Shortcut hints wrap instead of clipping on narrow terminals. The popup inherits
Herdr's terminal palette. Lifecycle colors remain reserved for working, done,
blocked, and warning states.

The UI follows `LC_ALL`, `LC_MESSAGES`, or `LANG` and supports English, Japanese,
and Korean. Unsupported locales fall back to English.

Vertical cursor movement follows the same wrapping as the message display.
Long labels and status text are clipped by terminal cell width so Korean and
Japanese text cannot spill into the next row.

Cancelling a send preserves confirmed deliveries. Recipients that already
received the prompt, or whose acceptance is uncertain after an interrupted
connection, are deselected. The popup identifies uncertain delivery so recipients
can be checked before another send. Pending and definitely failed recipients
remain selected in direct mode.

## Keyboard controls

- `Up` / `Down`: move through choices, recipients, or message lines
- `Enter`: confirm the highlighted discovery choice
- `D` / `L`: choose remote discovery or local-only
- `C` / `D`: choose coordinator delegation or direct delivery
- `Space`: toggle a recipient
- `Ctrl+A`: select all filtered recipients
- `Ctrl+D`: clear the selection
- `Tab`: switch between recipients and the message editor
- `Ctrl+O`: reopen the delivery mode screen
- `Ctrl+R`: refresh local and authorized remote agents
- `Ctrl+U`: show unavailable remote hosts and their latest errors
- `Ctrl+G`: open the HAM skill installer and guide
- `Ctrl+S`: send the prompt
- `Esc`: go back, cancel pending discovery or sends, then close the popup
