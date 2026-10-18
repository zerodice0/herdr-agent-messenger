# HAM mailbox and popup UI reference

[한국어](ko/ui-reference.md) · English · [README](../README.md)

## Open the mailbox

Three command-palette actions are available:

- **HAM: Mailbox (Right Split)** (`herdr.agent-messenger.mailbox-split`) opens a
  plugin-owned pane to the right of the opener.
- **HAM: Mailbox (Tab)** (`herdr.agent-messenger.mailbox-tab`) opens that view in a tab.
- **HAM: Mailbox (quick send)** (`herdr.agent-messenger.mailbox-popup`) opens a
  popup that closes after a successful send result. A failed send keeps the input open.

A shell pane can open any action; the opener does not need to contain an agent.
These actions do not change keybindings or install a runtime. Split/tab opening
reuses the workspace's live mailbox by pane/terminal IDs, not by its display label.
`q` closes only the owned mailbox pane or popup. Nonempty drafts require a
discard confirmation, with Cancel selected by default. At top level, `Esc`
closes a popup but does nothing in split/tab mode; in input or overlays it backs out.

### Responsive regions and navigation

The body contains **Agents**, **Threads**, and **Detail** regions, not tabs:

- Below 80 columns or 18 rows, only the focused region is visible.
- At 80–139 columns, Agents and Threads stack on the left and Detail appears on
  the right. If the lists would each have fewer than six rows, only the focused
  list appears on the left (Agents when Detail is focused).
- At 140 columns or more, all three regions appear side by side.
- Below 32 columns or 10 rows, a resize notice replaces the body. Drafts,
  selections, cursor and mode survive resizing; `Esc`/`q` retain their mode-specific behavior.

The header names the current mode (`NAV`, `INPUT`, `CONFIRM`, `SETTINGS`, `HELP`,
or `EVIDENCE`); the footer shows contextual keys. In **NAV**:

| Key | Action |
| --- | --- |
| `Tab` / `Shift+Tab`, `←` / `→`, `h` / `l` | Move focus between regions. |
| `↑` / `↓`, `j` / `k` | Move the list selection; arrows scroll when Detail is focused. |
| `PgUp` / `PgDn`, `Home` / `End` | Page or jump within the focused region. |
| `Enter` | Agents: open inline input; Threads: open a reply; Detail: no action. |
| `s` | Open Settings. |
| `?` | Open help, with contextual keys followed by the full keymap. |
| `e` | Show diagnostics (connection errors and pending operations) in Detail. |
| `r` | Refresh. |
| `q` | Quit, confirming before discarding nonempty drafts. |
| `Esc` | Close help/diagnostics/overlays; at top level, quit only in popup mode. |

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

Digits never navigate. Agents are grouped by host with stale/error headings.
Each row shows alias, turn state, unread count and native receiver readiness,
followed by one dim `role · phase · summary` line. A disconnected host keeps its
last snapshot marked stale and unread `?`; it is not treated as closed.
Detail automatically previews the most recently focused list's selection,
including full agent summaries and separate status-update/observation timestamps
in local time with relative age. **Detail is read-only: viewing never ACKs,
delivers, handles or resolves mail.** Threads includes message histories, direct
submissions and pending/failed OUTBOX entries; `delivering` is **UNCERTAIN**.

### Inline input and replies

Select an Agents row and press `Enter`. The full-width input box shows
`To host/alias · FYI`; UI mail is always `sender=user`, kind `fyi`, without a
subject. A missing native receiver produces a warning but still permits sending
to the mailbox. A stale recipient cannot be selected for input.
Threads `Enter` opens the same input as a reply to the thread's latest message.
The recipient must resolve to a live, non-stale agent; otherwise an error is shown.

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

Success closes input and shows a result tag on the agent row; in popup mode it
also closes the popup. Only the submitted, unchanged draft is cleared: edits
made while a send is pending are retained. Failure leaves the text open with an error.

### Settings overlay

Press `s` in NAV. `↑`/`↓` or `j`/`k` selects a row. Click a row to select it;
clicking the selected row acts like `Enter`. `Esc` closes the overlay and
discards unsaved inline changes.

- **Mode:** `Enter` opens a **PICK** list with a short explanation of each option.
  `↑`/`↓` selects; `Enter` or clicking an option saves immediately.
  On the mode row, `←`/`→` or `h`/`l` cycles an unsaved choice; `Enter` saves it.
- **Main:** `Enter` opens a **PICK** list of live, non-stale repository agents plus
  `(none)`. The current choice has a `●` marker. `Enter` or clicking a choice saves.
- **Members:** `Enter` opens a **CHECK** list. `Space` or clicking an entry toggles
  its checkbox; `Enter` saves the checked set. Existing unavailable members carry
  a `stale` tag and remain checked unless explicitly unticked.
- **Thresholds:** `←`/`→`, `-`/`+`, or clicking `◀`/`▶` adjusts the inline value.
  Steps are 1 for `idle_minutes`, 5 for `coalesce_seconds`, and 1 each for
  `wakes_per_hour` and `max_hop`; values stay within 0–10000. An `unsaved` marker
  remains until `Enter` saves. With no pending changes, `Enter` opens an optional
  digits-only input instead. `Esc` cancels an open picker, checklist, or numeric input.

The mouse wheel scrolls picker/checklist entries or overlay metadata.
The overlay shows `Target: host/alias` for the selected Agents row:
`m` sets that agent as main; `Space` toggles its manual membership.
These two shortcuts still require confirmation, with Cancel selected by default.
Home, `computed_main`, version/origin stamps, propagation pending and conflicts
are read-only. Settings uses the internal service; agent `repo show` remains read-only.

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

The TUI checks the local board bell once per second and queries peers every
30 seconds in bounded child processes outside the keyboard loop. Closing stops
only its own workers; these checks do not create model turns. Display-cell-aware
text helpers support Korean input, clipping and resizing. Nonempty `NO_COLOR`
disables colors without removing labels or other non-color state cues.

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
