# HAM mailbox and overlay UI reference

[한국어](ko/ui-reference.md) · English · [README](../README.md)

## Open the mailbox

Three command-palette actions are available:

- **HAM: Mailbox (Right Split)** (`herdr.agent-messenger.mailbox-split`) opens a
  plugin-owned pane to the right of the opener.
- **HAM: Mailbox (Tab)** (`herdr.agent-messenger.mailbox-tab`) opens that view in a tab.
- **HAM: Mailbox (quick send)** (`herdr.agent-messenger.mailbox-popup`) opens an
  overlay that closes after a successful send result. A failed send keeps the input open.

A shell pane can open any action; the opener does not need to contain an agent.
These actions do not change keybindings or install a runtime. The right-split
action (when bound to `prefix+m`) focuses the workspace's registered live mailbox
only if it is in the opener's tab; otherwise it opens a new mailbox to the right
and registers it instead. The previous mailbox closes only when its visible first
line contains `NAV ·`; drafts, confirmations, pickers, and unreadable panes stay open.
The tab action still focuses the workspace's registered live mailbox in any tab.
Reuse checks pane/terminal IDs, not display labels.
`q` closes only the owned mailbox pane or overlay. Nonempty drafts require a
discard confirmation, with Cancel selected by default. At top level, `Esc`
closes the quick-send overlay but does nothing in split/tab mode; in input or dialogs it backs out.

A separate pane action, **HAM: Deliver queued FYI now** (`herdr.agent-messenger.fyi-now`),
does not open the mailbox; see [Deliver FYI now](#deliver-fyi-now).

### Herdr overlay placement

Quick send, **Send Prompt to Agents**, **Held Messages**, and **HAM Skill** use
the official `overlay` placement. Herdr temporarily zooms the overlay pane and
restores the previous focus and zoom when it closes. The persistent mailbox's
right-split/tab placement and reuse behavior are unchanged. The public
`mailbox-popup` action and pane entrypoint IDs remain stable for existing bindings.
Unlike the old centered, bounded popup, the overlay fills the active tab's entire
content area across its split panes, while the sidebar and tab bar remain visible.

HAM sends one `plugin pane open --placement overlay --focus` command without a
`pane layout --current` probe or `--width`/`--height` flags. No manifest pane
sizes are set: overlays use Herdr's available pane area, not a floating rectangle
with HAM-selected dimensions. This matches the public CLI and avoids unsupported
size configuration. See the official [Panes documentation](https://herdr.dev/docs/plugins/#panes),
the [v0.9.5 overlay implementation](https://github.com/herdrdev/herdr/blob/v0.9.5/src/app/api/plugins/panes.rs),
and [manifest size validation](https://github.com/herdrdev/herdr/blob/v0.9.5/src/app/api/plugins/manifest.rs),
which permits width/height only for `popup`. The minimum stays **Herdr 0.8.0**:
the [v0.8.0 plugin schema](https://github.com/herdrdev/herdr/blob/v0.8.0/src/api/schema/plugins.rs)
already defines `Overlay`.

Before opening an overlay, HAM captures the original messenger sender, installer
project root, or mailbox repository and opener pane/terminal identity as applicable.
The overlay's own pane ID never replaces that context. Held Messages remains
host-wide and does not require an agent in the opener.

HAM centers a top-anchored content area inside that overlay, without drawing
another outer frame. Quick send is capped at 120 columns, Send Prompt at 104,
Held at 96, and Skill at 80. Below 80 columns, the side gutters shrink to one
cell. These are content limits, not requested Herdr pane sizes; split and tab
mailboxes retain their existing geometry.

Each overlay begins with `HAM · <Surface>` and muted task context on one line,
followed by a blank row. Quick send puts INPUT/NAV at the right edge; Held puts
the visible-list position there. Context is clipped by terminal-cell width,
including wide characters. Split and tab mailbox titles are unchanged.

The four surfaces share semantic colors and `[Key] Action` hints. Hints stay
together and use at most two rows. They retain their declared navigation/action
order, with close last; priority only determines which hints are omitted when
space is short, preserving close/help and essential editing actions before
secondary shortcuts. Selection always uses `›` and bold text. On 256-color
terminals it adds a dark-grey full-row band with the default foreground; other
terminals use reverse video. `NO_COLOR`, including an empty value, disables all
color-pair initialization and keeps the marker, bold and reverse fallback.

Quick send gives INPUT at least eight content rows when space permits and caps
Detail at nine outer rows. In INPUT, the compose frame and title are accented;
in NAV, `›` marks the active region title. Existing region frames and the Send
Prompt editor's left rule remain visible. Selection markers sit beside their
turn glyph or recipient checkbox, with a separating space. The Recipients
section label stays accent-bold, not selected. Held places its selected preview,
dates and push warning immediately after the
list. Below its title, the empty state shows only `No held messages.` and
`[Esc] Close`. Skill puts each status name two cells after the longest client
name and highlights only the selected name/status row; long paths appear on a
separate detail line.

### Responsive regions and navigation

The body contains **Agents**, **Threads**, and **Detail**. Detail is always below
the lists when there is room; it is never a third column on the right.

- **Stacked, side by side:** with at least 80 columns and 18 available body rows,
  the lists share the top row only if each half can fit the complete Agents
  column model: TURN, ALIAS, MAIL, RX, ROLE, PHN, EMU, SRV and at least 16
  cells of PHASE · SUMMARY. The threshold is calculated from current alias/role
  widths, column gaps and borders, not a fixed terminal width (for example,
  158 columns with a 15-cell alias and the four-cell ROLE minimum).
- **Tabbed:** below that split threshold, the top list uses the full width.
  Its border shows `Agents │ Threads`; the active tab is accent-coloured and
  bold, and the inactive tab is dim. Click a label to select its list.
  Detail stays full width below. Long aliases/roles give up spare width before
  the full-width Agents table drops resource columns.
- Detail's outer height is `round(available_body * 0.4)`, with at least nine
  outer rows (six content rows, a notice and borders) and at least eight outer
  rows left for the list, including its border/tab bar. Detail scrolls fully.
- Available body height excludes the header, footer, any status line and INPUT.
  INPUT stays full width below Detail; opening or growing it can force the
  single-region fallback.
- **Single:** below 80 columns or 18 available body rows, the focused region
  fills the body. The list tab bar remains on its top border. The unchanged
  focus cycle `Agents → Threads → Detail → Agents` reaches Detail by replacing
  the list; its border retains the tabs plus a Detail label. Clicking a tab
  returns to its list. INPUT remains a separate full-width panel below.
- `Tab`/`Shift+Tab`, `←`/`→` and `h`/`l` follow that focus cycle in every layout,
  switching the active tab with list focus. Detail keeps the last-focused
  list's tab active. Each list preserves its own selection and scroll.
- **Tiny:** below 32 columns or 10 terminal rows, a resize notice replaces the
  body without losing drafts, selections, cursor or mode.

```text
Tabbed (split Agents columns would not fit)
+-- Agents | Threads -------------------------------+
| Active list                                       | remaining rows, min 8
+---------------------------------------------------+
| Detail                                            | about 40%, min 9 rows
+---------------------------------------------------+
| INPUT (when open; excluded from body height)       |
+---------------------------------------------------+
```

For example, without INPUT or status, 200×50 has 48 usable body rows: the
side-by-side lists use 29 outer rows and Detail uses 19. At 110×35, the active
tab uses 20 outer rows and Detail uses 13. Borders count toward these heights.

Agent Detail is a single-column, sectioned view: **Work** (turn, phase, bold
summary), **Identity & role**, **Devices & servers**, **Mail**, then dim
**Receiver/technical**. Device entries show the icon and human-readable label
(for example, `📱 SM-A245N`) before technical IDs. Mail contains counts and the
Threads/Space reading hint, not message bodies. Thread Detail retains its
message history and optional technical information.

The header names the current mode (`NAV`, `INPUT`, `CONFIRM`, `SETTINGS`, `HELP`,
or `EVIDENCE`); the footer shows contextual keys. In **NAV**:

In **NAV**, press `Space` on an Agents or Threads item (including DIRECT and
OUTBOX entries) to open a centered, read-only Detail viewer. The viewer is about
90% of the screen, capped at 100×40 cells. `↑`/`↓`, `PgUp`/`PgDn`, `Home`/`End`
and the wheel scroll; `t` toggles technical information as in Detail. It anchors
at the latest message and follows new arrivals only while still at the latest
position. If the item disappears on refresh, it closes with a status message.
`Esc`, `q`, `Space`, or a click outside closes it; clicks inside and `Enter` do
nothing. Viewing never ACKs. Space remains text in INPUT and retains its existing
CONFIRM/Settings picker bindings.
| Key | Action |
| --- | --- |
| `Space` | Open the selected Agents or Threads item in the read-only viewer. |
| `PgUp` / `PgDn`, `Home` / `End` | Page or jump within the focused region. Detail pages use the actual visible content rows, with one row of overlap; Home/End jump to its first/last page. |
| `Enter` | Agents: open inline input; Threads: open a reply; Detail: no action. |
| `f` | Agents only: deliver the selected local agent's queued ordinary FYI now (no prompt typed, no confirmation); see [Deliver FYI now](#deliver-fyi-now). Remote and inactive rows show a reason without changing anything. |
| `m` | Agents only: confirm pinning the selected live local role-group agent as main; if already explicitly pinned, confirm clearing the pin. Remote, inactive and other-group rows show a reason without changing settings. |
| `t` | Detail: expand/collapse technical information for a native thread, DIRECT submission or OUTBOX entry; return to the latest native message (the top for single items). Threads: retain the existing OUTBOX retry confirmation. Not a toggle in agent details or diagnostics; in INPUT, `t` is text. |
| `s` | Open Settings. |
| `?` | Open help, with contextual keys followed by the full keymap. |
| `e` | Show diagnostics (connection errors and pending operations) in Detail. |
| `r` | Refresh. |
| `q` | Quit, confirming before discarding nonempty drafts. |
| `Esc` | Close help/diagnostics/dialogs; at top level, quit only the quick-send overlay. |

### Mouse

- Click a region to focus it, or click an Agents or Threads row to select it.
  Clicking the already selected row acts like `Enter`: Agents opens input;
  Threads opens a reply.
  Press and release act once even when the terminal delivers both together;
  queued mouse reports are drained before processing the next click or wheel.
- The wheel scrolls the region under the pointer without moving focus.
- Click inside input text to move the cursor.
- Dialog buttons are clickable; clicks outside a dialog are ignored.
  In Settings, click a row to select it; clicking the selected row acts like `Enter`.
  Clicking anywhere in help closes it.
- Mouse handling is enabled by default. While it is on, `Shift`+drag usually
  selects terminal text. Set `HAM_MAILBOX_NO_MOUSE=1` to disable mouse handling.

Digits never navigate. Agents uses **borderless, aligned one-line rows**, not
a second metadata line per agent. Columns have **two-space gaps**, with no
vertical separators or horizontal rules inside the panel. Header labels are
**cyan and bold, without underlines**; without color they are bold only.
Each host section starts with a **bold host label and agent count**, such as
`▾ winmini · 1 agent` or `▾ winmini · 2 agents`, followed by its column header
and role-scope groups. The opener's repository remains first: the local host
comes first, with the opener's anchor group first, then other groups by anchor
workspace ID; remote hosts follow alphabetically. Each group shows an indented
`anchor label · N` line for its total agent count. Linked workspaces appear
under it as `workspace label (worktree) · N` sub-lines; agents sort by alias,
then terminal ID. The actual opener workspace alone gets **`· current`**,
on its worktree sub-line when linked. Local labels come from Herdr snapshot
workspace labels (or explicit `workspace_label` data), falling back to IDs.
Older peer data without optional role-scope metadata keeps workspace grouping;
missing IDs display `No workspace` and sort last. Current workspace lines are
cyan/bold (bold without color); other group/workspace lines are dim.
Empty hosts have no column header. Host sections have no blank separator rows.
Below them, **Other repositories** lists local and registered-peer agents grouped
by host (local first), then repository short name (the last path component).
Repository names truncate to the available width; each indented repository line
shows its agent count. One all-repository status query per peer supplies both sections.
Host, column-header, workspace and repository lines are inert to mouse selection
and skipped by keyboard selection. Scrolling keeps the selected agent visible and
retains its host/header, plus its workspace or repository line when four rows fit.
Selecting any repository's agent opens input without changing the opener's
repository: user mail keeps that repository as sender context.
DIRECT submissions also retain the opener's repository for history, with the
other-repository recipient preserved in their target snapshot. In an unscoped
all-repositories view, sending uses the selected target's repository as the
origin fallback without changing the view.
Findings follow only when nonzero (or unknown), with stale/error information
when applicable; findings truncate before the count.

TURN starts with the selection marker and state glyph; its heading aligns
with the state word. ALIAS, ROLE and SUMMARY are left-aligned. MAIL, RX, PHN,
EMU and SRV headings and values are centered; unknown counts show `?`.
Known-zero counts show `·`, dimmed only in unselected rows. Nonzero MAIL
counts are bold in unselected rows. The selected row has a **common light
background and high-contrast foreground across its full width**, including
gaps and the bold turn glyph/word. Unselected turn states retain semantic
colors. Without color, selection uses full-width reverse video instead.

Columns show **state glyph + word**, **alias**, **MAIL** (unread count),
**RX** (native receiver flag: `✓` ready, `⚠` absent, `?` unknown), **ROLE**,
resource counts and a final flexible **phase · summary** column. TURN, ALIAS
and MAIL are required. Add RX first, then SUMMARY when at least 16 content
cells remain, then ROLE, then PHN/EMU/SRV together. Extra columns preserve
16 SUMMARY cells, or at least eight ALIAS cells when SUMMARY is absent.
ALIAS prefers the longest visible alias, clamped to 8–18 cells; ROLE uses the
longest role clamped to 4–8. Without SUMMARY, ALIAS absorbs leftover width.
At 30 inner cells TURN shows only marker + glyph and has a blank heading;
at 38 it shows the complete state word. At 76 SUMMARY remains visible, while
all columns fit at 100 and above. Headings are never cut mid-word. SUMMARY
grows only after ALIAS has its preferred width, and truncates only at its end.
Full summaries remain in Detail. Send-result labels stay bounded inside ALIAS
or SUMMARY and may truncate. MAIL is `?` for a stale host.
A disconnected host keeps its last snapshot marked stale; it is not treated as closed.

Threads retains its **full-width bold column header with underlined words**
and **full-width selected-row background** (reverse video without color).
Unselected state, kind and transport-operation labels use semantic foreground
colors; titles and participants retain their normal foreground. On the pale
selected-row background, labels instead use the high-contrast selection
foreground and bold, preserving every glyph and word. Each entry keeps two
lines and the same full-width mouse targets.

Threads includes conversations in which the opener's repository is an origin,
sender or recipient. Both the thread list and message history add `(repo NAME)`
to senders from another repository; NAME is its short name, capped at 24 cells.

Threads is **one list and one scrollport**, with counted `Active (N)` and
`Handled (N)` group rules. Active/needs-attention entries come first; a blank
visual row separates the lower Handled section. Empty groups add neither a
heading nor padding. Each group preserves its existing updated/recent order.
Handled rows are dimmer unless selected; selection always restores full contrast.
Headers and blank rows cannot be selected or activated. Arrows, paging and
the wheel move between entries across the divider, and clicks target the exact
entry even if a refresh has reordered the list.

Only a nonempty native thread whose **every retained message is `handled` or has
a sender dismissal audit** belongs in Handled. Dismissal resolves sender-side
attention, not delivery or recipient work: the recorded state stays
`undeliverable`. A newer handled or dismissed message cannot hide earlier queued,
delivered-but-unhandled, uncertain or undismissed failure mail. A new reply
returns the thread to Active without changing its identity or losing selection.
Withdrawn mail remains Active: withdrawal is cancellation, not handling.
Direct submissions and displayed pending/failed OUTBOX operations always remain
Active. Transport success, acceptance, claim IDs and operator evidence alone
never establish resolution; recorded `delivering` claims remain uncertain.
Agent Detail counts use the same rule and identify `handled/dismissed` totals
when dismissal audits are present.

| Threads cue | Meaning / color |
| --- | --- |
| `. pending`, `. queued` | Waiting, yellow |
| `> submitting` | Direct submission in progress, yellow |
| `? UNCERTAIN delivering` | Unresolved native claim, yellow; not a delivered receipt |
| `+ delivered`, `+ DIRECT accepted` | Recorded native delivery / separate direct acceptance, green |
| `= handled` | Recorded handled state, cyan |
| `x dismissed` | Audited sender resolution, dim; **not delivered or recipient handled** |
| `! failed`, `! undeliverable` | Failure, red |
| `- withdrawn` | Withdrawn, dim |
| `? DIRECT unknown` | Uncertain direct acceptance, yellow |
| `i fyi`, `! action` | Mail kind, cyan / yellow |
| `> direct` | User terminal-input evidence, cyan; **not native mail** |
| `> message`, `< receipt`, `# setting`, `- withdraw` | OUTBOX transport operation, cyan; not an FYI/action mail kind |

These compact cue glyphs are single-cell ASCII and always retain a text label.
States appear only from recorded row data, never from viewing or selecting a row.
If any message in a thread has an unresolved `delivering` claim, the thread keeps
its uncertainty warning even when a later message has another state. OUTBOX
retains its prefix, target and error. Its operation label does not infer the
payload's mail kind. Detail repeats state/kind/transport cues and preserves
operator-vs-native evidence; direct acceptance never means native delivered or
handled. The `?` help overlay includes this legend.

Dismissed message Detail always shows actor alias/terminal, dismissal time and
reason, plus the preserved delivery state and former failure reason; technical
mode is not required. Mixed handled+dismissed threads retain a dismissed list cue.
Its old failure is historical, not a current HOLD cue; genuinely held,
undismissed messages retain their current HOLD indicator.
This is a local origin-ledger resolution, not a recipient receipt or replicated
handling event. Use `mail dismiss --message UUID --reason TEXT` from an authorized
sender/main CLI context; the TUI adds no dismissal shortcut or mutation.

State colors are redundant cues: **working is yellow**, **idle neutral**,
**done green**, and **blocked red**; **unknown/stale states are dim**.
Glyphs and words remain visible (`● working`, `○ idle`, `✓ done`, `! blocked`,
`? unknown`, `~ stale`). The presence of `NO_COLOR` (even an empty value) disables
color but preserves glyphs, words, bold, dim and reverse-video selection.

Detail and other plain-text panels leave one column of inner padding on the left.

Detail automatically previews the most recently focused list's selection,
including full agent summaries and separate status-update/observation timestamps
in local time with relative age. **Detail is read-only: viewing never ACKs,
delivers, handles or resolves mail.** Threads includes message histories, direct
submissions and pending/failed OUTBOX entries; `delivering` is **UNCERTAIN**.

To read sent or received mail, press `Tab` to focus **Threads**, select a thread,
then press `Space` to open its messages in the viewer. Detail remains available
for inline reading. Selecting an agent shows its status and resources, not message
bodies. After Unread, Agent Detail has a **Mail** section with counts of loaded
active/handled threads involving that agent and the dim hint
`Read: Tab → Threads, select a thread, Space opens it`.
If none are loaded, it says `No threads with this agent yet`.
Devices and Servers remain available below.

Message Detail starts with a bounded **message i/N, local timestamp/age and
recorded sender → recipient** header, then a compact kind/state summary and the
body in normal brightness. Native histories keep their chronological order and
numbering (oldest → newest), but a newly selected thread opens at its **latest
message header**. Short latest messages use the clamped final page. Use PgUp or
Home in Detail to read earlier messages; the Threads title previews the latest
message's subject or first body line.
Long participant labels are shortened only in these reading headers. On narrow
panels, participants and direction get their own header row rather than being
displaced by the message number/timestamp row; that metadata row may be shortened.
Full canonical values remain in technical information. Kind/state, acceptance
and transport warnings wrap to preserve their complete meaning instead of being
cut off. Subjects and UUIDs do not take space before the first body line.
Native and DIRECT bodies preserve their original paragraphs, real newlines and
literal `\n` text, wrapping only to the panel width. OUTBOX remains a transport
operation, not a conversational reply or native ACK: only a message operation
with a body in its nested message payload exposes a body.

Technical information is collapsed initially. In NAV with Detail focused, `t`
reveals the full recorded source, canonical identifiers/targets, claim and old
agent/generation, routing, confirmation source, evidence and errors in dim text.
The compact state still shows unresolved `delivering` as UNCERTAIN, operator
confirmation as **NOT native ACK**, and DIRECT acceptance as **not native
delivery/handled**, even while technical information is hidden.
Both toggling directions return to the latest native message header. Selecting a
different item starts collapsed at that header; single-message threads, DIRECT
and OUTBOX start at the top as before. Refreshing the same item preserves its
toggle and reading offset, clamping when content or wrapping changes. A new
message moves the viewport to its header only if you were still at the previous
latest header's position (or its clamped final page); scrolling elsewhere keeps
your reading position.

The existing read-only notice also shows the current visible line range/total,
`↑more`/`↓more` when content remains above/below, and the technical toggle state.
Narrow panels shorten the notice but keep the no-ACK warning, position and
toggle hint; no extra content row is reserved. Counts include wrapped headers,
body paragraphs and any expanded technical lines. PgUp/PgDn move by the actual
content viewport minus one row (minimum one), so adjacent pages overlap by one
row. Arrow keys, Home/End, the wheel, resize and toggling use the same bounds.

### Inline input and replies

Select an Agents row and press `Enter`. The full-width input box below Detail
(or below the focused region in single mode) shows the recipient and selected
mail kind, initially `ACTION`. UI mail is `sender=user`, without a subject.
Input space is reserved before choosing the body layout, so opening or growing input can switch
from stacked to single mode. A missing native receiver produces a warning but
still permits sending to the mailbox. A stale recipient cannot be selected for input.
Threads `Enter` opens the same input as a reply to the thread's latest message.
The recipient must resolve to a live, non-stale agent; otherwise an error is shown.
Every new compose, including replies and reopened drafts, resets to `action`.
`Ctrl+G` toggles `action` / `fyi` for that compose, and the selected kind is sent
explicitly; the CLI default remains `fyi`. Use action when the recipient must
act, answer, or continue; eligible action wakes promptly. FYI is batched for
an idle participating OMP wake after about ten to fifteen minutes. The minimum is
600 seconds from local recipient enqueue; natural events or five-minute safety
checks determine the actual wake, subject to existing gates. Already queued mail
needs no migration. FYI arriving
during a turn is delivered right after that turn when local enqueue observes
fresh `working` state. Existing wake safeguards apply to both paths. Action
wakes can include younger FYI; FYI alone needs a reply only if it changes work.

- `Enter` sends mailbox text immediately, without a review or confirmation step.
  A blank body is rejected without sending.
- `Shift+Enter` inserts a newline. The mailbox requests the **kitty keyboard
  protocol**, which works through herdr in kitty-compatible terminals.
  **`Alt+Enter` is the fallback** for Moshi/mosh, which cannot negotiate that
  protocol. `Ctrl+J` is **not** a newline key. Set `HAM_MAILBOX_NO_KITTY=1` to
  disable the protocol request.
- Printable characters, including digits, `q`, `?`, `:`, and `/`, are text.
  Arrows, `Home`/`End`, `Backspace` and `Delete` edit the text; `↑`/`↓` move between lines.
- `Ctrl+D` toggles **direct terminal input**. Enter then opens a confirmation
  with Cancel selected; `Tab` or `←`/`→` selects a button and `Enter` executes it.
- `Esc` closes input but keeps its text and cursor per recipient and reply.
  `Ctrl+R` refreshes without leaving input; `F1` opens help while typing and
  returns to the same input afterwards.

Success closes input and shows a result tag on the agent row; quick-send mode
also closes the overlay. A mailbox enqueue posts a best-effort, nonblocking
Herdr notification, `Queued · recipient · ACTION|FYI`; it does not claim delivery.
Notification failure cannot turn a successful enqueue into an error. Only the
submitted, unchanged draft is cleared: edits made while a send is pending are
retained. Failure leaves the text open with an error.

### Main shortcut in Agents

In NAV, select a live agent on this host in the opener's repository/workspace role
group and press `m`. The confirmation names the target and role group; Cancel is
selected initially. Confirm pins that terminal as main through the same Settings
service and live scope/target validation as the Settings overlay. Press `m` on
the explicitly pinned row to clear the pin. The clear request carries the pinned
terminal; if another Mailbox changed the pin before saving, the job fails with
`main_pin_changed` and leaves the newer pin untouched. Clearing a pin does not turn off
roles: in `auto`, the incumbent or a unique candidate may become main again.
An automatically elected main is not an explicit pin, so `m` pins it rather than
clearing it. The Settings picker and Settings `m` shortcut keep their existing
behavior. No mutation CLI is available.

### Deliver FYI now

Queued ordinary FYI normally waits for the FYI delay (about ten to fifteen minutes).
Two triggers let you skip that wait without composing mail or typing into the agent,
so the request itself spends no model tokens. The only model work is the agent's
own turn that reads the delivered bundle.

- **Mailbox:** in NAV, select a local agent in Agents and press `f`. The result appears in
  the status line and as a short tag on the row.
- **Pane action:** **HAM: Deliver queued FYI now** (`herdr.agent-messenger.fyi-now`, context
  `pane`) acts on the focused pane and reports through a Herdr notification. Bind it in
  `~/.config/herdr/config.toml`. `prefix+f` collides with no Herdr 0.9.5 default; the
  second entry is the hangul-layout variant, matching other HAM bindings:

  ```toml
  [[keys.command]]
  key = "prefix+f"
  type = "plugin_action"
  command = "herdr.agent-messenger.fyi-now"
  description = "HAM: deliver queued FYI now"

  [[keys.command]]
  key = "prefix+ㄹ"
  type = "plugin_action"
  command = "herdr.agent-messenger.fyi-now"
  description = "HAM: deliver queued FYI now (hangul)"
  ```

  Check your own config for an existing `prefix+f` before adding it. Both triggers are local
  only: for an agent on another host, use the Mailbox on that host.

The request marks that agent's queued, unheld, ordinary FYI deliveries wake-eligible by
setting the local `deliveries.fyi_turn_end` flag, whose meaning is now "deliver without the
FYI delay: turn end or user request". It appends a `fyi_now` row to the status log, then
rings the agent's local bell so the receiver inspects at once. Action mail, held mail,
handled mail, and other agents are untouched. No schema, protocol, or runtime change is
involved, and the OMP extension needs no restart.

Every wake gate still applies, because the receiver (for Claude and Codex, the ledger
worker) enforces them: the agent must take part (mode and role), its turn must be idle
or done, the hourly wake limit must have room, the hop limit holds, and the quiet period
must have passed. For OMP the prompt must be empty and quiet for the idle threshold; for
Claude and Codex the agent must have been continuously idle for it, since the worker cannot
see typed prompts. For these two, the request also starts the worker if needed (unless
autostart is off) and first checks that the session can be woken; a
`worker_start_failed` note means the worker could not be started. A
working agent receives the FYI right after its turn. One wake carries at most 8 messages
and 8 KiB; any remainder follows in later wakes. The status line names exactly what applies:

| Outcome | Message (shape) |
| --- | --- |
| `requested` | OMP: `N FYI eligible now, wake requested; delivers once prompt empty and quiet M min`. Claude/Codex: `... delivers once idle for M min` |
| `after_turn` | `agent is working: N FYI delivered right after its turn` |
| `hourly_limit` | `N FYI marked, wake limit reached (4/4 this hour); next wake possible HH:MM` |
| `nothing_queued` / `only_held` | no deliverable FYI, with the queued action or held count |
| `not_participating` | mode off, or manual non-member: HAM never wakes it; FYI wait for its next turn |
| `no_receiver` | OMP extension not registered; Claude without a registered hook; Claude/Codex that cannot be woken right now (`Claude cannot be woken right now (REASON: detail)`, nothing flagged); or another client (`inbox read` only) |
| `not_agent` | the focused pane is not a HAM agent (pane action only) |
| remote agent | local only: use the Mailbox on that agent's host |

### Settings overlay

Press `s` in NAV. `↑`/`↓` or `j`/`k` selects a row. Click a row to select it;
clicking the selected row acts like `Enter`. `Esc` closes the overlay and
discards unsaved inline changes.

Mode and Thresholds are repository-wide. Main and Members show the resolved
anchor of the Mailbox opener's role group (`Main (workspace ANCHOR @ HOST) · role group`
/ `Members (workspace ANCHOR @ HOST) · role group`). The original workspace is
resolved from `HERDR_HAM_OPENER_PANE_ID` on the canonical local host; the role
scope is `(repo_id, host, anchor_workspace_id)`, not the selected agent's workspace.
The refresh reuses its live Herdr pane snapshot if the opener is not yet in the
ledger, falling back to the Mailbox's own workspace if the opener has closed.
If no workspace is available, only Main/Members are disabled; Mode and Thresholds
still load and save for the selected repository.
The all-repositories view keeps its existing repository selection; without one
repository, settings stays disabled with an explanation. Role settings remain
host-local. Changes save at the anchor; grouped linked-workspace settings stay
unchanged and ignored until ungrouped. `repo show` distinguishes anchor
`workspace_id` from original `requested_workspace_id`.

- **Mode (repo-wide):** `Enter` opens a **PICK** list with a short explanation of each option.
  `↑`/`↓` selects; `Enter` or clicking an option saves immediately.
  On the mode row, `←`/`→` or `h`/`l` cycles an unsaved choice; `Enter` saves it.
- **Main (workspace ANCHOR @ HOST) · role group:** `Enter` opens a **PICK** list
  of active agents across the same repo/host role group, including linked
  workspaces, plus `(none)`. The current choice has `●`; `Enter` or clicking saves.
- **Members (workspace ANCHOR @ HOST) · role group:** `Enter` opens a **CHECK**
  list for the whole group. `Space` or clicking toggles an entry; `Enter` saves
  the checked set. Inactive or out-of-group targets cannot be newly assigned;
  the service rejects them. A configured agent outside the group has no role there.
- **Thresholds (repo-wide):** `←`/`→`, `-`/`+`, or clicking `◀`/`▶` adjusts the inline value.
  Steps are 1 for `idle_minutes`, 5 for `coalesce_seconds`, and 1 each for
  `wakes_per_hour` and `max_hop`; values stay within 0–10000. An `unsaved` marker
  remains until `Enter` saves. With no pending changes, `Enter` opens an optional
  digits-only input instead. `Esc` cancels an open picker, checklist, or numeric input.

The mouse wheel scrolls picker/checklist entries or overlay metadata.
The overlay shows `Target: repository REPO_ID · workspace ANCHOR @ HOST`
for the resolved settings scope and identifies the opener workspace separately;
main/member changes save at the anchor, not at the selected agent's workspace.
For the selected Agents row,
`m` sets that agent as main; `Space` toggles its manual membership, only for
active targets in the opener's settings scope. These two shortcuts still
require confirmation, with Cancel selected by default.
Home, main source (`pinned`, `auto` or `none`), effective main (the resolved main,
whether pinned or automatic), fallback candidate (the computed unique candidate, used only when there is no pin or
incumbent), version/origin stamps, propagation pending and conflicts
are read-only. Only repo-wide settings propagate. Requests, snapshots and save
results are associated with the full repo/host/workspace scope, so a result from
another scope cannot overwrite the current settings. Settings uses the internal
service; agent `repo show` remains read-only and reports the caller's scope.

Only one settings save can be in flight. The affected row shows **Saving settings**,
then its save result or error. Further saves are rejected, not queued, and rejected
edits remain available. Navigation stays responsive. The authoritative result
appears immediately and stale snapshots cannot overwrite it. `PgUp`/`PgDn` scroll
long metadata and complete wrapped conflict records; `e` in NAV shows full diagnostics.

### Honest acceptance and uncertainty

Mailbox sends follow normal native receiving rules. Direct input instead uses
the exact `Message from user (HAM):` header and verified terminal transport.
It may interrupt the target, hence its explicit route selection and confirmation.
HAM records `via=direct` and `submitting`, then `accepted`, `unknown`, or `failed`;
these are not native `delivered`/`handled` receipts. Inspect results in Threads.
Check interrupted `submitting` or `unknown` results before another explicit send;
HAM does not replay them automatically.

In Threads, `w` withdraws eligible locally originated mail and `t` retries the
selected OUTBOX item with its existing identity. Both require confirmation of
the exact target. An uncertain native claim remains held even after session
replacement. `a` confirms applied; `n` confirms not-applied. Both first require
nonempty operator evidence, then a confirmation showing the exact message/claim
ID, old agent, generation and current state. Cancel is selected by default;
`Tab` or `←`/`→` chooses a button, `Enter` executes it, and `Esc` cancels.
Applied is a **manual user confirmation** of the old claim, not native
runtime-journal evidence, model understanding or handled work. Not-applied
releases exactly that claim and does not itself resend anything.

The keyboard loop retains its 100 ms input timeout, but paints are event-driven:
keys (including resize), started or completed jobs, and expired send-result tags
request a redraw. A clean, unchanged screen is not repainted on each input timeout.
Displayed relative ages request the next paint only when their displayed unit
changes: each second below one minute, each minute below one hour, each hour below
one day, and each day thereafter. Age-only refreshes are coalesced to whole
wall-clock seconds, so fractional timestamps do not cause multiple paints per
second; an age label can lag its boundary by up to one second. Missing timestamps
and relative-age labels outside the visible Detail viewport schedule no age
refresh. Input and new-mail updates do not wait for age refreshes.

The TUI still checks the local board bell once per second; an unchanged bell does
not request another local snapshot. Local snapshots reuse a persistent child
process rather than starting a new process for every changed bell. Each snapshot
reads fresh ledger, mail, agent-status, settings and device-claim rows. Only raw
host-resource scans and Herdr pane metadata are cached, for 30 seconds within that
Mailbox instance; an explicit refresh bypasses those caches.
A fresh ledger change to an agent's last HAM label refreshes Herdr metadata early,
without rescanning host resources, so cached HAM labels do not look user-edited.
If a snapshot is already in flight, explicit refresh queues one cache-bypassing
follow-up instead of cancelling that snapshot or restarting its worker.

Peers are still queried every 30 seconds in bounded child processes outside the
keyboard loop. Closing stops only the Mailbox's own workers, including its local
snapshot process; these checks do not create model turns. Display-cell-aware text
helpers support Korean input, clipping and resizing. The presence of `NO_COLOR`
disables colors without removing labels or other non-color state cues.

### Device and server presence

The Agents **PHN**, **EMU**, and **SRV** columns show phone, emulator, and server
counts, respectively, when space permits after RX, SUMMARY and ROLE.
Each uses three centered content cells with two-space gaps and ASCII headings
regardless of color settings; there are no resource emoji. Known-zero counts
show `·`, dimmed in unselected rows; nonzero counts have normal brightness.
Missing device data shows `?` in PHN
and EMU; missing server data independently shows `?` in SRV.
Host group labels show findings such as `dead 1 · unowned 1 · stale-dev 1`
for dead Serve routes, unowned served backends, and stale devices. Zero
findings are omitted. An unavailable field shows `?`; a discovery error adds
`!`. At narrow widths findings truncate first, retaining the agent count.

Agent Detail has **Devices** and **Servers** sections. Devices show held
nonstale claims; Servers show port/process/PID, bind address, cwd, inherited
flag, start timestamp/age and URLs. `inherited` means the listener predates this
agent session. Missing data shows `Devices: ?` or `Servers: ?`; known-empty
data shows `: none`. Old peers may omit `devices`, `servers` or host `resources`
independently: absence is unknown, not zero.

These are read-only views of agent JSON `devices` and `servers` plus host
`resources:{servers_unowned:[],dead_serve:[],stale_devices:[],error:null}`.
Server discovery uses current-user listeners and Tailscale Serve; it excludes
plugin, adb and agent-runtime listeners. Viewing never refreshes the registry,
prunes device claims, changes tokens or acknowledges mail.

Device mutations use the bundled CLI, not table actions:

```text
device claim phone|emu DEVICE [--label TEXT] [--force]
device release [phone|emu|all] [DEVICE]
device list
```

`release` defaults to all of the caller's own claims; kind/device arguments
narrow it. Claims use verified host/terminal/generation identity. Inactive or
old-generation holders are stale, independent of adb attachment, and may be
replaced; a different live holder blocks claiming unless the user explicitly
instructs `--force`. Failed Herdr refresh does not imply a stale holder.
`device list` is read-only. See the [skill guide](skill-guide.md#device-claims-and-resource-presence)
for command results and device details. Product code imports no legacy device
JSON. `heavy_slot.py` remains separate and unchanged; its slots are not displayed.

### Role, phase and border ownership

Main/workers use `(repository, canonical host, anchor_workspace_id)`. Linked
workspaces join a deterministic open nonlinked workspace with matching Herdr
`worktree.repo_key` on the same host. Missing metadata or no anchor keeps an
independent scope; there is no git-common-directory fallback. Mode/home/thresholds
remain repo-wide. Valid anchor explicit main/member settings apply to the whole
group; linked settings remain stored unchanged and return when ungrouped.
In auto, an active explicit main wins. Otherwise an active anchor agent is
required: the sole group agent wins, or, with several, the unique main-checkout
agent. An open anchor without agents has no auto main even with a sole linked
agent. Other auto agents remain workers. Manual roles use the active explicit
main and selected group members. All displays use origin-host computed roles.
See [schema-v9 storage and rollout](migration.md#worktree-role-grouping-schema-v9)
before sharing a ledger with upgraded code: old processes cannot open v9 storage.

HAM displays roles before the alias, for example `🟧 [메인] orange-rabbit`, so
the alias remains addressable. A shared projection reports alias, color,
display name, phase (`recap`) and ledger-backed device tokens under metadata
source `herdr.agent-messenger`. Exact values are `phone=📱 <labels space-joined>`
and `emu=🤖 <labels space-joined>`; each token key is cleared when no claims remain.
Status projection preserves these claims, and force replacement reprojects the
previous live holder too. Herdr uses a flat per-pane token map: metadata sources
do not isolate keys. HAM leaves unrelated token keys untouched.

When status/roles are invoked, summary supplies `[메인] summary` or
`[워커] summary` on the border; off-mode status has no role prefix. HAM updates
only a blank border, a verified legacy border, or its own last recorded border
value. A differing user rename is preserved. The old `user:pane-recap` token is
cleared only when the new status/role feature is invoked; this UI does not
deploy or remove global recap rules.

## Legacy direct-prompt overlay

The existing **Send Prompt to Agents** and **Held Messages** actions remain
available. The sections below describe that older prompt UI, not mailbox delivery.

## Open the messenger

Run **Send Prompt to Agents** from a pane containing an agent. The focused agent
becomes the fixed sender. If no agent is running in the focused pane, HAM shows
an error notification instead of opening the overlay.

Local agents appear immediately, sorted by Herdr workspace label. Unicode labels
are preserved and worktree-backed workspaces use a `WT:` prefix. Remote agents
are grouped under their SSH host alias. Within each host, agents are grouped
again under their workspace. Workspace identity uses the canonical host plus
Herdr's `workspace_id`, never a bare ID across hosts. Different workspaces with
the same visible label stay separate and display their IDs for disambiguation.

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

The overlay uses Herdr's available pane area; HAM does not request a fixed size.
Its centered content area is capped at 104 columns, with recipient pane/status
information aligned inside that area. The editor wraps at no more than 88 cells.
Discovered recipients receive most of the available height while at least four
soft-wrapped message rows remain visible when possible. Long messages follow the
text cursor and display a viewport scrollbar.

Discovery and route hints follow their short choice blocks instead of sitting
at the screen bottom. Reopening the route choice from compose shows the selected
count and that the draft is kept. Normal coordinator/recommended information is
neutral; discovery completion is successful, not a warning. Shared shortcut hints
wrap as whole actions to at most two rows.

The UI follows `LC_ALL`, `LC_MESSAGES`, or `LANG` and supports English, Japanese,
and Korean. Unsupported locales fall back to English.

Vertical cursor movement follows the same wrapping as the message display.
Long labels and status text are clipped by terminal cell width so Korean and
Japanese text cannot spill into the next row.

Cancelling a send preserves confirmed deliveries. Recipients that already
received the prompt, or whose acceptance is uncertain after an interrupted
connection, are deselected. The overlay identifies uncertain delivery so recipients
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
- `Esc`: go back, cancel pending discovery or sends, then close the overlay
