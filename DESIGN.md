# Codey UI Design Language

This document is the design baseline for Codey's workspace UI. Edit sources in
`frontend/`; `codey/web` contains the published build.
Keep one visual language across the application.

The 2026-10-20 revision adopts deepseek-harness as the main interaction reference
and Codex as an additional visual reference. Revised layout, branching, steering,
icons and inspection rules describe the approved redesign target, not a claim
that every target is already implemented. Track implementation and acceptance in
`docs/uiux_completion_spec_2026-10-20.zh-CN.md`. Existing factual execution,
authorization, recovery and accessibility requirements remain binding.

Product implementation and its verification scope are tracked separately in
`docs/uiux_implementation_2026-10-20.zh-CN.md`. The saved HTML prototype is a
comparison artifact; its fixture messages, terminal and plan are not product data.

Confirmed exceptions to reference matching: retain Codey's existing project/chat
navigation and Settings content. The sidebar has no Codey / Local workspace
brand heading; New chat, Add project and Search stay at the top, and Settings
is the only bottom action. Do not add redesign settings categories or a second
Add project entry. The composer keeps its original border when focused, without
an extra textarea outline or glow; retain visible keyboard focus on action buttons.

The entire existing navigation sidebar is outside this redesign's scope, including
its layout, entries, icons and interactions. Render projects and chats from the
user's actual state; do not seed extra demo projects, recent items, or comparison
tasks into it. This applies to the whole sidebar, not just its footer.

The quiet field focus rule applies to every input, textarea and select, including
inline renaming, search, file navigation and connection forms. Focus adds no
outline, outer ring, glow or second border; keep each field's resting border.

Message actions sit outside the message surface, in a row below it. User bubble
fill belongs only to the message body, never to its edit/copy row. Keep the body,
gap and actions in one continuous hover region so reaching a button does not hide it.

---

## 1. Design intent

Codey is a **local developer and research tool**, not a consumer chat app. The UI should feel like Cursor or a minimal IDE panel:

- **Monochrome first** — black, gray, white. Hierarchy comes from typography and spacing, not color blocks.
- **Quiet by default** — idle states show almost nothing. Activity is signaled with motion (spinner) or text, not banners.
- **No decoration** — no emoji, gradients for status or brand-blue/purple accents. The confirmed user-message surface uses one quiet panel fill; assistant answers remain on the canvas. Actions sit below the surface.
- **One restrained green family** — the provider availability dot, Research graph nodes and highlighted edges, native text selection, conversation navigation, and added diff lines share a muted green palette. Use role-specific brightness and opacity, not unrelated teal and gray-green accents. Other resting chrome stays monochrome.
- **English in the UI** — labels, buttons, placeholders, and system messages use English (`New chat`, `You`, `Codey`, `Allow`, …).
  User messages, model answers, and returned thinking text retain their original language; the English rule applies to fixed application copy.

When in doubt: remove color, remove chrome, remove copy.

Icon-only actions (project/chat more menus, new chat, topbar/sidebar controls,
message Copy, send/stop, document reload/copy/close and Shell Copy) brighten on
pointer hover without adding an individual button background. Keep full-row
hover and selected surfaces. Preserve an explicit keyboard-visible focus cue;
mouse focus alone must not introduce a focus frame.

---

## 2. Color tokens

Always use CSS variables from `:root`. Do not hard-code one-off hex values in components.

### Neutrals (structure)

| Token       | Value     | Use |
|------------|-----------|-----|
| `--bg`     | `#181818` | Main canvas |
| `--bg-2`   | `#1c1c1c` | Sidebar, composer shell |
| `--panel`  | `#202020` | Top bar, cards, menus |
| `--panel-2`| `#262626` | Nested surfaces, pressed states |
| `--hover`  | `#2a2a2a` | Row / button hover |
| `--active` | `#2f2f2f` | Selected list item |
| `--border` | `#2a2a2a` | Dividers, input borders |
| `--border-2`| `#232323`| Subtle separators |

### Text (4 levels)

| Token        | Value     | Use |
|-------------|-----------|-----|
| `--text`    | `#e6e6e6` | Primary body |
| `--text-dim`| `#a0a0a0` | Secondary body, tool output, readable explanations and enabled secondary actions |
| `--muted`   | `#6b6b6b` | Labels, group titles, placeholders |
| `--faint`   | `#4a4a4a` | Separators (`·`, `→`), disabled hints |

Necessary small explanations and enabled secondary actions remain readable:
composer context actions and thinking effort, Settings explanations, provider
probe explanations, and sidebar empty/search-empty states use `--text-dim`.
These ordinary small-text roles target at least 4.5:1 contrast against their
actual surface. Keep hierarchy through size, placement, spacing, and weight;
group labels, placeholders, separators, and disabled hints retain their
existing muted/faint roles. Do not globally brighten the palette.

### Semantic (minimal)

| Token       | Value     | Use |
|------------|-----------|-----|
| `--green-rgb` | `120, 165, 139` | Shared green channels for translucent treatments |
| `--green` | `rgb(var(--green-rgb))` (`#78a58b`) | Base green; use semantic aliases in components |
| `--green-bright` | `#acd1b9` | Brighter green for a navigation preview tick and diff `+` marks |
| `--green-selection` | `#3b5547` | Dark green for readable selected text |
| `--ok-dot` | `var(--green)` | **Provider online dot**, Research graph nodes and hovered connections |
| `--ok-dot-ring` | `rgba(var(--green-rgb), .14)` | Optional soft ring around the online dot |
| `--err-text`| `#d28a8a` | Error **text only**; diff deletion backgrounds and borders use separate tokens below |

The approved palette keeps a common green hue while separating filled selection,
small navigation markers, and diff scanning by brightness and opacity. Diff
backgrounds were increased from 7% to 20% for additions and 17% for deletions
because the former tints were difficult to distinguish on the dark canvas.
Do not use the base palette as permission for new colored controls or surfaces.

### Native text selection

| Token | Value | Use |
|-------|-------|-----|
| `--selection-bg` | `var(--green-selection)` (`#3b5547`) | Low-saturation dark green for native text selection only |

Selected text uses `--text` on `--selection-bg`, with at least 4.5:1 contrast.
Apply the same selection treatment to prose, nested code and links, drawer
details, and editable text in the composer and Settings. This transient
interaction tint replaces the browser's blue selection; do not reuse it for
selected rows, focus outlines, buttons, status, or permanent surfaces.

### Conversation navigation

| Token | Value | Use |
|-------|-------|-----|
| `--nav-current` | `var(--green)` (`#78a58b`) | Current reading position in the conversation's tick rail |
| `--nav-highlight` | `var(--green-bright)` (`#acd1b9`) | The one tick selected by pointer hover or keyboard focus |

These markers are mutually exclusive: while a tick is selected, the current
reading marker becomes an ordinary gray tick. Moving away restores the base
green reading marker. Animate width only, so color transitions cannot briefly
show both greens. Neither token belongs on status, buttons, or other surfaces.

### Allowed tint exception

Inside the **changes diff drawer**, added/removed lines use these semantic tokens:

| Token | Value | Use |
|-------|-------|-----|
| `--diff-add-bg` | `rgba(var(--green-rgb), .20)` | Added-line background |
| `--diff-add-mark` | `var(--green-bright)` | Added-line `+` mark |
| `--diff-add-edge` | `var(--green)` | Added-line left edge |
| `--diff-del-bg` | `rgba(210, 138, 138, .17)` | Removed-line background |
| `--diff-del-mark` | `#e4aaaa` | Removed-line `−` mark |
| `--diff-del-edge` | `#bb8282` | Removed-line left edge |

Keep added/removed code in `--text`, rather than tinting the entire line's text.
Use a 2px left edge and the existing signed marks so a change is not identified
by background color alone. Reserve the same transparent edge on context and
metadata lines to keep code and line numbers aligned. File-list stats stay gray.

Do not reuse these diff tints elsewhere. Background tint for scanning belongs
only in diff; native text selection uses its separate interaction token above.

Inside the **Research graph canvas** (the Graph tab), all resting nodes use the
original `--ok-dot` green (`#78a58b`), as explicitly confirmed by the user.
Connections rest in gray; hovering a node also highlights its directly connected
edges in that green. Do not render the nodes white or gray at rest. No other
component may use this graph hover exception for accent styling.

### Forbidden

- Blue / purple accent colors (`#7c9cff`, `#4f7cff`, …)
- Green / yellow / red **status cards** or **colored buttons**
- Colored “success” or “warning” pill chips
- Emoji in UI copy or icons
- Glowing colored pulses on status indicators

---

## 3. Typography

```css
--font-sans: -apple-system, BlinkMacSystemFont, 'Segoe UI', 'Inter',
             'PingFang SC', 'Microsoft YaHei', sans-serif;
--font-mono: 'Cascadia Code', 'SF Mono', 'JetBrains Mono', Consolas, monospace;
```

| Role | Size | Weight | Font | Notes |
|------|------|--------|------|-------|
| Body | 13px | 400 | sans | `line-height: 1.6` globally |
| Conversation prose | 14px | 400 | sans | `line-height: 1.7`; grows naturally with content |
| Composer input | 13.5px | 400 | sans | Slightly larger for typing |
| Group label | 10.5px | 400 | sans | `uppercase`, `letter-spacing: 1px`, `--muted` |
| Message role label | 10.5px | 500 | sans | `YOU` / `CODEY`, uppercase |
| Status prefix | 10.5px | 500 | sans | `DONE`, `ERROR`, `PAUSED`, … uppercase |
| Tool line | 12px | 400 | mono | Aligned grid, not pills |
| Context hint | 11.5px | 400 | sans | Composer context row |
| Keyboard hint | 11px | 400 | mono | e.g. `Enter` |

**Weight rule:** default 400; emphasis 500. Avoid 600+ except rare cases (e.g. shell card title).

**Content rule:** assistant replies use **sans** for prose. Use **mono** only for tools, paths, commands, diff, and logs.

Enable on `body`:

```css
text-rendering: optimizeLegibility;
-webkit-font-smoothing: antialiased;
```

---

## 4. Layout

```
Sidebar         Conversation                    Right workspace
                Title / status                  Tabs / panel actions
                Messages and process            File / diff / review
                Composer                        Research evidence
```

- Conversation prose and composer share a centered reading-width token, initially
  760px. This is a default, not a universal hard limit: adapt to the available
  conversation column and keep reasonable padding when the right workspace opens.
  Code, tables, diff and inspection surfaces may use the available panel width.
- The right workspace owns file, diff, Research evidence and review tabs. It
  pushes the conversation on wide windows, supports resizing and fullscreen,
  and uses an explicit overlay/fullscreen presentation when space is insufficient.
  Each session owns its selected tabs, width and reading positions. Do not retain
  separate competing fixed drawers for these features in the redesigned layout.
- Sidebar: **260px by default**, adjustable from **220–420px** and collapsible to 0. Remember the preferred width in the local browser profile. On desktop, clamp the displayed width to leave at least 400px for the main area; shrinking the window does not overwrite the preference. No overlay on desktop unless viewport ≤ 720px; the narrow overlay uses the default width and disables resizing.
- Borders: 1px `--border`. Prefer spacing over heavy frames.
- Use shared radius tokens for controls, menus and cards. Match the designated
  component reference consistently; do not hard-code different radii in each feature.

---

## 5. Components

### 5.1 Sidebar actions

Pattern: **borderless hover rows** (not filled primary buttons).

```
+ New chat          Ctrl+N
+ Add project
```

- Default: transparent, `--text-dim`
- Hover: `--hover` background, `--text`
- Shortcut hint: `--muted`, mono, right-aligned

### 5.2 Group labels

```
PROJECTS
CHATS
```

Uppercase, muted, no background. Never use colored section headers.

### 5.3 Project / session lists

- **Project row:** name only; full path in `title` tooltip, not inline.
- **Active project:** 2px left bar in `--text`, not colored background.
- **Active session:** `--active` background.
- **Secondary actions:** revealed on hover or keyboard focus; visible on devices without hover. `⋯` opens a context menu. Do not show permanent `+` / `×` icon clusters.
- **Quiet hover copy:** omit native `title` tooltips that repeat `Choose folder` (without a project), `Research`, code/message Copy, `More`, and enabled `Refresh` / `Use in Project`. Icon-only More buttons retain `aria-label="More"`; the project More button uses an empty `title` to suppress its parent row's inherited path tooltip. Keep the full project path on the project name/context, source and truncated-path details, disabled reasons, and the conversation navigation preview. Copy success/failure retains its icon/text feedback and accessible name without a native tooltip; Research retains `aria-pressed`.
- `New chat`, `Add project`, and magnifier + `Search` share a 34px row height and 1px gaps. Search opens a same-height input in place and filters chat titles and project names locally. The input has a transparent background, no visible border or focus ring, and its text aligns with the Search label. Escape clears and closes search; an empty search also closes on blur. Search temporarily expands matching groups without changing saved project expansion.
- Project names match independently of their chats: a matching empty project remains visible with `No chats`. Search shows only groups with matches, or a single `No matches` state when nothing matches. Temporary expansion uses an expanded chevron and returns to the saved state when search closes.
- Sidebar empty states (`No projects`, `No chats`, `No matches`) use 12px `--text-dim`; group labels retain `--muted`.
- A nonempty search shows a small SVG clear icon at the input's right edge, using `--text-dim` and `--text` on hover, with no fill, border, or background. Replace the browser's native colored clear control. Its accessible name is `Clear search`; pointer click or keyboard activation clears the query and returns focus to the open input. Empty or closed search hides the icon.
- A monochrome gear + `Settings` row sits at the sidebar bottom, sharing the sidebar action alignment and 34px height. `Ctrl+,` opens the same settings dialog even when the sidebar is collapsed. Do not duplicate Settings in the topbar menu.
- The sidebar's existing right divider has a 6px transparent drag target, with a `col-resize` cursor and a single gray line on hover/focus. It is a keyboard-focusable vertical separator named `Resize sidebar`; Left/Right adjust by 10px, Home/End reach the available bounds, and double-click restores 260px. Pointer capture keeps dragging stable outside the divider. Escape, pointer cancellation, or window blur cancels an active drag. Disable width animation and text selection only during the drag; release immediately restores ordinary selection. Hiding the sidebar hides the separator.

Project rows reserve space for a more menu and a small SVG new-chat action. On
hover-capable devices these actions are hidden at rest, appearing on row hover
or keyboard-visible focus without shifting the project title. The new-chat
action opens a chat in that project and expands the project. Touch devices keep
both actions visible. Hover brightens the icon only, without an individual button background; retain the keyboard-visible focus outline. Use the existing monochrome row styling and the shared 16px square-and-pencil SVG for new-chat actions.

Expanded projects initially show at most five chats, retaining the active chat
in that preview. `Show more` reveals the remaining chats; `Show less` restores
the preview. This is local presentation state and never deletes chats. Search
shows all matching chats regardless of this preview limit.

Context menus (`.ctx-menu`):

- `--panel` background, `--border`, 8px radius, light shadow
- Items: borderless rows, hover `--hover`
- Destructive item: on hover, text becomes `--err-text` only (no red row background)

### 5.4 Top bar

- Title breadcrumb: `project-name / chat-title` — project in `--muted`, slash in `--faint`, chat in `--text`.
- The chat title is a plain text button: click, or focus it with Tab and press Enter/Space, to rename in place. Only the chat name is editable; the project crumb and unused topbar space are not rename targets. Hover/focus uses a subtle gray background, without a permanent pencil icon. The topbar editor is transparent and borderless, with no outline, shadow, or native control frame; text stays aligned with the original title. It fits within the topbar and initially selects the full name; the caret and native text selection communicate editing. Sidebar inline rename retains its existing gray input style. Enter saves, Escape cancels, and leaving the editor saves; blank input keeps the original name, and names are limited to 80 characters. Respect IME composition. Commit updates the same session's title, sidebar, search, and durable state together; preserve drafts, message DOM, reading position, and ongoing work. Background rendering preserves the editor and caret, switching chats settles the original session's edit, and stale input events are ignored. Automatic naming must not replace active edits or confirmed custom titles. Remove `Rename chat` from the topbar menu; retain `Rename` in each sidebar chat menu for other chats.
- **Status area:**
  - Idle: empty (no dot, no “Connected” label)
  - Running / connecting: CSS **spinner** + short label (`Running`, `Connecting to Edge…`)
  - When another chat owns the run: `Running in <chat> · Open`. `Open` returns to the owning chat, where its Stop control remains available; other chats keep the composer Send slot available for queueing.
  - Error: small dot + `--err-text` label (`Disconnected`)
- No colored status chips in the top bar.

### 5.5 Chat stream — de-bubbled messages

Chat is the common workspace entry. With no chosen folder, tasks use a managed
session workspace; choosing a folder supplies the root and context. Keep the
existing composer, folder picker, process records, approval cards, diff and
restore surfaces. Do not add a computer-assistant mode or a second process UI.
Tool activity comes from execution events. Ordinary answers need no empty tool
block or extra model classification request.

Keep assistant prose in the reading flow and tool activity compact. User message
surfaces and alignment may follow the designated conversation reference; use
neutral surfaces and one consistent role hierarchy across the conversation.

```
YOU
User message text, left-aligned, no background.

CODEY
Assistant reply in sans, --text.

  · read    buggy.py              → 6 lines
  · write   snake.py              → 277 chars
```

**Role labels:** use `You` / `Codey` where needed to identify the author. Contiguous
process and answer content does not require a repeated uppercase label per fragment.

**Process disclosure:** one group per user request; continuation after approval uses the same group. Ordinary tools and optional thinking belong inside it. The final answer stays outside and expanded; errors and approval cards remain visible outside the disclosure.

```
▸ Worked · read 4 files · edited 2 files · ran 1 command

Assistant reply remains fully visible here.
```

Running copy is `Working · <current action>` or `Working · Waiting for reply`; terminal copy reflects the actual result (`Worked`, `Failed`, `Stopped`, `Paused`, `Waiting for approval`). Count completed, successful actions only, and changed files only for edits. Completed groups default to closed; explicit user disclosure choices survive updates. Step numbers are available inside the disclosure. Legacy records without run identifiers may retain their original Turn dividers.

`Thinking` appears only for nonempty reasoning text returned separately by the model. It defaults to closed, retains its source language, and has a Copy action. Never infer thinking from wait time, parse it out of answer text, or scrape model websites to manufacture this section. The same rendering applies to every connection.

**Tool lines:** compact text summaries without a resting container. Read shows the file and observed line range; search shows the query and retained match count (`+` means more results); edit shows the actual file and measured `+n −n`. Expand a tool to inspect its retained content or bounded edit diff. Counts and diffs come from the content actually written, never model claims. Missing before-content or a preview budget limit means `Changed · preview unavailable`, never fabricated statistics. Failed tools use a short actionable explanation; keep the complete retained error inside Details. Use `--err-text` only for the failure text.

**Status rows** (done, pause, limit, error):

```
DONE · 2 files changed · checks passed        View diff
PAUSED · No progress for several turns          Continue
ERROR · Connection refused                      Retry
```

Structure: uppercase prefix + body + optional **text action** (`link-btn`). No colored boxes.

Text actions have no underline in resting, hover or keyboard-focus states. Hover
brightens text to `--text`; keep the restrained global keyboard focus outline.
This shared rule also covers Settings actions such as `Select all`, `Clear` and
`Refresh models`. Actual hyperlinks inside answer prose retain their underline.

Each chat is an independent run lane: one active run is allowed per session, while different sessions may send and stream concurrently. Stop carries the selected session's `run_id`; it cannot cancel another chat. Project writer leases remain a separate resource guard for write tasks. Submission failures use confirmed causes only: `Another chat is running · Open` is reserved for a verified same-session admission conflict; `Select the local model again · Choose model` opens the existing model menu in the original chat; `Codey is temporarily busy · Retry` retries the original request. Unknown causes retain `Could not send the message · Retry`. Navigation and model selection do not send automatically. Failure actions belong to the original chat and preserve newer drafts in every chat.

At startup the composer restores the durable UI state, question state, and queued-input snapshot before enabling the textarea. Switching sessions remains available during that restore, but no draft can be submitted until the initial restore boundary completes; a late restore cannot overwrite an enabled user's new input.

Manual Retry belongs to the fixed identity of a user message. Each explicit retry has a new execution identity, recorded under that request, and updates one status position after its existing output. Keep the user message, tool records, reading position, and newer drafts intact. While retrying, replace error text and actions with a gray spinner and `Retrying · Waiting for reply`; another failure replaces that same row. Success hides the transient row and displays the answer normally, without a success banner. Only the latest user request can be retried, and admission or an active execution disables duplicate submission. Independent errors without a request identity must not guess which question to resend.

`Details` expands a quiet inline attempt history using the existing Run Details typography. Raw errors and `Local update paused` belong to the attempt history, not repeated chat messages. Model/owner recovery actions may sit beside Retry and never send automatically. Persist attempt facts and their recovery actions, but not the disclosure's open state or rendered details. Restore active attempts from authoritative local execution state; if an accepted attempt cannot be confirmed, replace its spinner with `Response was not confirmed` and an explicit Retry. Never resend automatically on reload or a transport failure. Late events from an earlier attempt cannot change the current status.

**Run Details** (inline receipt expansion):

`checks passed` reflects current required observations. Review approval does not
replace a failed, missing or stale required check. A repair receives current
verification and review before completion. Behavioral checks use the existing
task result and evidence paths; they add no permanent counter, confidence meter
or separate UI entry. A permission-blocked fix must not imply that a fix succeeded.

```
DONE · 2 files changed · checks passed        Details   View diff

RUN DETAILS
Work            Project writing
Model           DeepSeek
Actions         inspected 4 items, edited 2 files, ran 1 check
```

Run Details belongs under the task status row, not in a drawer, topbar menu, or
persistent sidebar. It is loaded only after the user clicks `Details`; expanded
content is not persisted into chat state. Styling follows the status-row
language: transparent background, no rounded container, no shadow, no color
accent, one subtle `--border-2` divider, group-label typography for the title,
12px sans rows, muted labels, and `--text-dim` values. Even warning rows remain
gray unless the status row itself is an error. Do not show internal terms such
as `RunTrace`, `PromptEnvelope`, `Policy Pipeline`, `Router`, `Ghost`,
`Hebbian`, `Directive`, or `Provider`.

One optional `Progress` row (0.5.1) explains where an interrupted run stopped,
sourced from the run's durable operation state: it appears only when the
operation state never reached terminal and the ledger has no `run_finished`
row. The copy is fixed English -- `Writing was interrupted`,
`Completion check was interrupted`, `Finishing was interrupted`, or
`Stopped during repair` -- with warning tone and no internal phase names
(`writer_running`, `RunOperationState`, ...). A finished run never shows the
row, even if a stale non-terminal snapshot exists on disk.

Optional `Recovery` rows (0.5.2 / 0.5.4) explain settled interrupted external effects
and recovered safe actions only after runtime recovery has written a settlement. Allowed copy:
`Read action was recovered`, `Lookup action was recovered`, `Read-only action can be retried`,
`Local write was interrupted and was not repeated`, `Provider response was not confirmed`,
and `Repair round was interrupted`. Do not show pending in-flight effects, effect ids,
provider ids, replay class, or runtime record names.


**Changes summary** (inline, not a pill):

```
Changes   3 files                    View diff
  M  codey/web/index.html           +42 -18
```

File stats stay gray in the stream; addition/deletion tints stay inside the diff drawer.

**Assistant long replies:** keep the full answer expanded without Collapse/Expand actions. Preserve text selection and whole-message copying; process history uses its own disclosure and does not fold the answer.

**Reading and copying:** prose, code, paths, commands, diff, and error details support native pointer selection and copying. Message/code Copy actions complement selection. Following is explicit reading state: upward wheel/touch movement, reading keys, scrollbar dragging, text selection and question navigation stop following, even 40px from the bottom. Composer keys, horizontal code scrolling and content geometry changes do not imply reading intent. Resume only when the reader actually reaches the bottom (2px rounding tolerance), releases the scrollbar there, or activates `Back to latest`. Streaming and deferred selection updates respect this state. Preserve the visible reading anchor during updates, process disclosure changes and chat switching.

`Back to latest` remains a centered 30px circular down-arrow button above the composer. Use a single subtle gray border, neutral background and gray stroke icon; retain its accessible name and the existing gray keyboard focus outline, without a native `title` tooltip or hover text label. Hide it while following or actually at the latest output. Metadata refresh preserves unchanged message DOM and explicit disclosure state. Hidden chat DOM is cached for at most six recent chats; older chats retain their reading offset. Do not add unread counts or another scrolling control.

Native selected text uses the subdued `--selection-bg` with light `--text`,
including selections inside text inputs and textareas. Preserve ordinary
pointer/keyboard selection, the exact copied text, draft values, and carets;
selection styling does not add an input outline or shadow.

**Conversation navigation:** show a quiet vertical tick rail in the left
reading margin when the conversation has at least eight user questions,
scrolls vertically, and its available reading area is at least 920px wide and
360px high. Use available space, not fullscreen state; count overlapping
inspection drawers and hide while Settings is open. Keep the original 760px
content width and composer position. Each tick represents one user question
and its following reply; tool calls and stream updates do not add ticks.

At rest, only the current reading position uses `--nav-current`. Pointer
hover or keyboard focus selects one tick with `--nav-highlight`, suppresses
the base green marker, and stretches nearby gray ticks into a gentle wave.
After 220ms, show the question and a plain-text reply excerpt in a dark gray
tooltip, kept inside the available reading area. Do not add a visible numbered
`Question N` label above the excerpt; accessible tick names may retain position.
Hover never scrolls; click,
Enter, or Space jumps to the user question without sending or changing drafts,
carets, native text selections, or disclosure state. Scrolling dismisses the
preview. ArrowUp/ArrowDown and Home/End browse questions through a single Tab
stop; Escape dismisses and restores prior keyboard focus. Ticks have names
containing the question, and the reading position uses `aria-current="location"`.

For long histories, keep at most 48 ticks inside the reading area, centering
the visible window on the reading/selected question; keyboard navigation can
reach every question. Preserve tick focus across tool/text updates, rebuild
for the active chat only, and cancel stale previews on chat switches, hiding,
drawer/Settings opening, or window blur. Respect reduced motion by disabling
the width transition. Fixed UI labels stay English; question/reply excerpts
retain their original language and are inserted as text, never HTML.

Whole-message Copy icons under user messages, model answers and status messages are hidden at rest on devices with hover. Reveal only the relevant message's icon when its continuous region (body, intervening space, and button) is hovered or contains keyboard-visible focus. Preserve the button's layout space, but disable pointer interaction while hidden. Mouse focus alone must not keep an icon visible after leaving; Tab reveals the button with a gray keyboard focus background; pointer hover brightens only the icon. Keep icons visible on devices without hover. During copying, keep the icon visible and prevent duplicate activation without removing keyboard focus. After completion, retain gray success feedback for 1.2 seconds, or retryable failure feedback for 4 seconds, even after pointer exit. Code-block Copy reveals on its code region. Thinking is one ordered disclosure without a repeated Copy control on every fragment. Respect reduced-motion preferences.

Right-clicking selected chat/drawer text or an editable text selection opens a small `Copy` menu in the existing `.ctx-menu` language: dark `--panel`, gray text, 8px radius, one subtle border, and no accent. The menu item has no individual border or focus outline. Pointer opening shows plain text; hover or keyboard navigation uses the existing gray row background for focus. Copy uses the exact selected text, preserving the draft and selection; whole-message and code Copy buttons keep their existing behavior. Shift+F10 / the context-menu key opens the same selection menu. Escape returns focus and selection to the source; outside click, scrolling, window resizing, and chat switching dismiss it. Keep the menu within the viewport. A successful copy briefly shows gray `Copied`; failure shows gray `Could not copy` and leaves Copy available to retry. Do not offer a stale text selection for an unselected input or expose password selections. Desktop copying uses this menu without enabling developer/debug menus in the host.

The same menu offers `Quote in reply` only for a selection inside one current assistant answer's body or code. Append ordinary editable Markdown quotation to the existing draft, retaining selected indentation, blank lines and text; place the caret after the quote with space to ask a question. Keep native Undo/Redo and focus the composer without scrolling the conversation. Do not send, attach the whole answer, or create hidden context. User messages, Thinking, tools, drawers and editable inputs retain Copy only; hidden actions are excluded from keyboard navigation. Chat switching/deletion invalidates the captured answer. Actual scrolling dismisses the menu; a queued scroll notification without movement does not.

Copy success uses a temporary gray checkmark or `Copied`, with an accessible name; no success accent. External Markdown links allow HTTP(S) only. Workspace links go through the session-bound document service. Tables scroll horizontally when necessary, and fenced code preserves whitespace with a quiet language label. Model output is escaped, never rendered as arbitrary HTML.

**Shell approval:**

```
Approval required
Runs in E:\project
$ pytest -q
[ Deny ]  [ Allow ]
```

Plain `--panel` card. `Allow` is `text-btn primary` (weight 500), not colored. Safety through copy and layout (`Deny` left, `Allow` right).

**Command results:**

At rest, show one borderless 26px disclosure row: a small terminal SVG, neutral
`Ran` text with an ellipsized command, and a chevron. Successful and failed
commands share the same quiet row treatment. Only an expanded result has a
rounded `--panel-2` Shell panel with a subtle border. Wrap the command, bound
scrollable output, and place the actual execution status at the lower right;
only failure text uses `--err-text`. Command and output have independent Copy
icons, revealed on panel hover or keyboard-visible focus (always available on
touch). Preserve exact retained text when copying and disclose runner
truncation explicitly. Do not conflate UI folding with discarded output.

**Welcome (empty chat):**

```
Codey
Send a message to start.
```

No marketing copy, no emoji.

### 5.6 Composer

2026-10-20 user preference: retain Codey's existing composer appearance,
including the project/Research context row above the quiet input box, the
borderless model picker, its green availability dot and muted ring, and the
unfilled send/stop controls. Add steering, queue and context actions within that
language. The redesign does not replace this composer with a generic chat pill
or remove the connection dot. The dot continues to indicate actual availability.
Do not add a permanent attachment plus button. Images enter through clipboard
paste and files through drag and drop; show removable attachments only after
they are added. Preserve the existing send SVG path
M3 11 21 3l-7 18-3-8-8-2Z rather than substituting a different send icon.
The context label is the selected project/folder name, not a fixed Codey brand
label. With no selected project show Choose folder; with a selected project
show its real name and expose the full path. Research is the existing explicit
research toggle, not a second project or model selector.

**Empty home (2026-10-20 target):** keep the conversation area blank: no welcome
heading, introductory sentence, starter icon, or suggested action buttons. Place
the original composer slightly below the middle of the available chat area.
On the first actual send, move that same composer smoothly to its normal bottom
position (about 360ms), retaining input focus and the submitted message. Respect
reduced motion by changing position without travel. New chat returns to the blank
layout. Do not insert fixture messages into a newly started conversation. This
behavior is implemented in the saved interaction prototype; production integration
remains part of the UI completion work.

```
Choose folder · Research                     ← enabled context actions (11.5px, --text-dim)
┌─────────────────────────────────────────┐
│ Send a message to Codey…                │
│                                         │
│ ● Gemma4 12B ⌄ Low ⌄       Enter  ↗   │
└─────────────────────────────────────────┘
```

- Composer wrapper: separate from chat with spacing, without a full-width top divider. Keep the topbar's existing subtle bottom divider.
- Box: `--bg-2`, 1px `--border`, radius 10px; keep the same quiet border while typing. Text input and the bottom model/action row share this frame so their task scope reads as one control.
- **Context row:** `Choose folder` and `Research` stay on the left of the quiet line above the input. Local-save failure/progress occupies the right side of that same line, on the same text baseline; it never adds a row or moves/resizes the input frame. Enabled actions use `--text-dim`; the separator stays `--faint`. Disabled inactive actions retain `--muted` and 0.55 opacity; active Research retains `--text`, including when disabled at 0.55 opacity. Long folder names truncate while retaining their complete accessible name; Research and save Retry remain visible. The main grid has one shrinkable content column; at viewport ≤ 720px, the overlay sidebar leaves a single full-width body column.
- **Research token:** visible by default as text, not as a framed button. Hover changes text to `--text`; active Research uses brighter text only. No border, background, chip, underline, font-weight change, or accent color.
- **Provider picker:** borderless; status dot + label + chevron. This is the only visible provider/model selector in the composer. Online state uses `--ok-dot`; offline state is the default solid gray `.dot`.
- **Send / Stop:** 34px square **icon buttons** (`.icon-btn`) with 16px icons, transparent until hover; enabled Send uses `--text` and the original paper-plane outline. Send and Stop occupy one fixed position. No filled accent send button.
- **Model menu:** directly show the short catalog without a search input. Opening focuses the current model. Arrow keys navigate, Enter selects, Escape closes and returns focus. Do not invent unsupported model/effort controls. Menus have a viewport-bounded scroll area.
- API connections show their actual model names; Local can supply an optional display name and the complete model ID in Settings. The menu contains only models explicitly selected in Settings from enabled sources; never invent a website's selected model version. Optional integrations contribute source and catalog data to the same controls, without vendor-specific UI branches, accents, cards or Settings panels. Discovery never selects a new model. Catalog availability does not promise successful generation. The model menu contains only models; connection configuration belongs in Settings.
- A separate borderless effort selector sits beside the model name: `Gemma4 12B ⌄ Low ⌄`. The selector disappears for unsupported connections and website models. Accessible copy names `Thinking effort`; visible labels are `Off / Mini / Low / Med / High / XHigh / Max`, filtered by verified capabilities. The two borderless selectors sit 2px apart, without a separator. The model menu uses a stable 240px width independent of the selected name or effort label, shrinking to the available viewport width. Selected checks retain right-side padding. The effort menu uses a 160px width. Both menus are bounded by the viewport. Long model labels use ellipsis and retain their complete accessible name. Model and effort triggers and menu rows have no native hover tooltips. Each selector has its own menu, keyboard focus, and dismissal. Opening one closes the other.
- The enabled effort trigger uses `--text-dim`, becoming `--text` on hover or while its menu is open; disabled effort retains `--muted` and 0.55 opacity. A provider probe explanation uses 11.5px `--text-dim` in the existing model menu.
- Thinking controls require verified connection/model metadata. Active KoboldCpp Jinja and a matching `enable_thinking` template support Off/High; verified versions 1.112.2–1.117.x additionally support minimal/low/medium budgets. High uses the native unrestricted thinking budget within the total output limit. Mini/Med map to minimal/medium. Show the concrete initial level rather than Default or On. Every non-Off choice explicitly enables thinking; do not fabricate distinct Max behavior. Returned reasoning and configurable thinking are independent capabilities.
- API model and thinking choices belong to the chat, persist with it, and are captured at admission for the next request. Background/continued requests use that chat's choices, even while another chat is visible. A new chat on the same connection inherits the active chat's selected model and thinking choices, with its own independent effort map. With no matching active choice, it uses the connection default. Connection address, credentials, API protocol, context limit, tool calling, and default model live in Settings. Pending command continuation and cold recovery retain the admitted selection. A changed connection cannot silently carry a stale chat selection or credential to another endpoint. Deleted connections preserve readable old chats and their model identity; sending requires an explicit new choice.
- `Enter` hint: immediately before Send/Stop with a 6px gap, visible on composer focus/hover only, `--faint`. `Enter` sends; `Shift+Enter` inserts a newline.
- **Drafts and submission:** exact text, UTF-16 caret/selection offsets and direction belong to `session.draft`, persisted with the existing local UI state across chat switches and restarts. Question answers use `QuestionDraftStore` in the same browser origin and validate question revision/options before restore; `Clear draft` only removes that question's draft. A different tab exposes a conflict with `Keep current` / `Use other draft`. Cross-port and cross-profile recovery is not promised. Drafts do not enter model context or chat excerpts until explicitly sent.
- A draft revision advances only on text changes and never rewinds when cleared. Admission shows `Sending…`, prevents duplicate requests, and only consumes the accepted text/revision snapshot; later edits, including editing back to the same text, remain intact. Choosing a folder changes context without sending. Save failure preserves editing and shows one quiet `Not saved · Retry` in the context row's right-hand status slot; assistive text explains `Could not save local changes`, since the save scope also includes chats and projects. Retry saves current local content without reloading it or sending a task. Only an already-visible failure changes to `Saving…`; Retry remains in place with `aria-disabled` and ignores repeat activation while saving. Normal background saves stay silent. Hide the status only after successful confirmation with no queued or coalesced edits waiting to save. Return focus to the input only if the disappearing status still owns focus; background responses never take focus from another control. A version conflict pauses automatic writes, including the exit beacon, until explicit Retry; the local cache still retains the draft. No extra row, toast, underline, separate draft API, store or save timer.
- **Pending approval:** the command card owns `Approval required` and its Allow / Deny controls. Do not duplicate this with a composer notification or another approval action. Enter never approves a command globally. The command card remains the single approval surface.

Provider is **session-level** — it lives in the bottom provider picker, not duplicated in the context row or elsewhere.

Research is also **session-level**. It lives in the composer context row, never beside the model picker as a separate primary action. User-facing copy says `Research`; internal words like vault, knowledge, artifact, and index should not appear in the main chrome.

### 5.7 Changes inspection

Redesign target: use a Changes tab in the shared right workspace. Preserve the
existing factual diff and restore behavior below while replacing drawer-specific chrome.

- Fixed right panel, `--bg-2`, slides in from the right.
- Header actions: text buttons, no borders (`drawer-btn`).
- File list: mono paths, gray stats; expand row to show diff.
- Identify the bound project and `Working tree` or `Snapshot` scope. Change-summary file links open and expand the exact file. Switching chats/projects closes old inspection surfaces; late loads/restore responses cannot overwrite a new scope.
- The drawer alone owns its project, displayed data and operation state. First load shows Loading; same-project refresh keeps the old diff readable and shows Updating. Reuse file nodes by path and preserve disclosures, focus and the visible diff-line anchor. Reopening the same project does not take focus again; an explicit file target is consumed once and does not override focus moved during loading.
- Failed refresh retains the displayed results with `Could not update · Showing previous changes · Retry`. Network, HTTP, business and JSON errors never become No changes. Previous results remain copyable, but Restore stays disabled until a successful refresh. Switching projects clears old content and copy data. Restore retains backend locking/hash checks; a successful restore followed by failed refresh reports both facts, while an unknown restore outcome says `Could not confirm restore`.
- Diff lines: use the §2 diff background, signed-mark and edge tokens; keep code text light and context lines neutral.

### 5.8 Research inspection

Redesign target: use the shared right workspace for evidence and report navigation.
Research remains an explicit user action; existing evidence and handoff rules apply.

- Same fixed right panel language as the changes drawer.
- Retain the existing Evidence / Sources / Graph / Notes tabs. Do not replace
  Research with only a brief and a source list. Graph retains Codey's existing
  Canvas renderer and node/edge semantics, Depth 1/2/3, Reset, node dragging,
  panning, wheel zoom, selection details and Notes/source navigation. Keep its
  resting green nodes, gray connections and green hover associations; the project/chat sidebar
  is not redesigned to accommodate the graph.
- Identify the bound chat; switching chats closes the drawer, and late note responses do not render into a different chat's drawer.
- Header actions are text buttons, no borders.
- Notes and sources are plain rows with title, type, bounded Markdown preview, source chips, and path or URL when useful.
- Notes text uses Research note card/body styles, not diff/code block styling. Source chips are derived from saved provenance (`note.sources`, citation map, opened sources), not from arbitrary body text.
- Restore is shown as a text action only when the backend has a valid restore snapshot for that run.
- Do not call this drawer `Knowledge` or `Vault` in the UI.

### 5.9 Local context drawer

- Entry lives only in the topbar more menu as `Local context`.
- Same fixed right panel language as Changes and Research.
- Changes, Research, and Local context are mutually exclusive; opening one closes the others.
- The drawer is a quiet audit surface, not a workspace, personality panel, or sidebar section.
- User-facing copy stays neutral: `Local context`, `Recent focus`, `Pending review`, `Active preferences`, `Follow-ups`, `Health`.
- Do not expose internal terms such as Ghost, Memory, Affinity, Hebbian, Directive, Work Queue, or Router in the UI.
- The drawer binds to the session/project scope it loaded; switching chat/project closes it.

### 5.10 Icons

- Use one local icon registry and semantic icon names. New components consume
  shared SVGs; consolidate inline paths, CSS masks and text glyph substitutes
  when touching their controls. Assets ship inside Codey, never from another
  application's installation or a runtime CDN.
- Use a 24×24 viewBox, rounded caps/joins, and a shared 1.5-unit stroke as the
  current baseline. Render at 16px by default; 14px secondary and 20px prominent
  icons use explicit shared size roles. Adjust the family together if reference
  comparison requires a different weight, never individual paths ad hoc.
- Stroke uses `currentColor`, without fill except semantic send/stop or other
  explicitly registered filled glyphs. Close uses SVG rather than text `×`;
  functional tree/disclosure arrows use the shared chevron.
- Monochrome only — icons inherit `--text-dim` → `--text` on hover.
- No emoji, no colored icon sets.
- The button owns its accessible name; decorative SVGs are aria-hidden and
  non-focusable. A small glyph does not shrink the hit target: use at least a
  28px desktop target and a 44px touch target where touch interaction is offered.
  Keep keyboard-visible focus and show secondary actions on non-hover devices.
- Copy/check/error, running/stopped and expanded/collapsed transitions keep a
  stable box. Critical actions remain discoverable; quiet styling is not a reason
  to hide focus, errors or the only route to an operation.

### 5.11 Settings

- Use one viewport-bounded dark dialog, about 560px wide, with a `Settings` heading, quiet close icon, and labeled connection fields. Native dialog modality keeps keyboard focus inside; Escape/backdrop dismissal returns focus to the trigger. No second right workbench drawer.
- Models use one shared source row: disclosure, source name, selected count, and a monochrome master toggle. Websites, optional API integrations, and Local use the same component and keyboard semantics. Expanding shows checkboxes with actual model names. In every source toolbar, `Select all` and `Clear` stay together on the left with a 14px gap; only the optional `Refresh models` action aligns to the right. Removing refresh never changes the selection actions' grouping. Long catalogs add `Find models`; the daily composer menu stays short and has no search.
- Source toggles preserve the selected subset. Turning a source off excludes it from the picker, automatic discovery, failover, repair, review, and advisor selection. Re-enabling restores only previous choices. Directory refresh updates names and capabilities without selecting new arrivals or forgetting absent selected IDs. `Refresh models` is explicit and appears only for sources with directory discovery capability; cache display requires no connector request. Websites currently lists registered website connectors rather than a discoverable model directory, so it has no refresh action. Shared rendering and layout follow source capabilities without vendor-specific branches.
- Zero selected means the source is off in both the dialog and stored preferences. Its master toggle stays disabled until an explicit model selection or `Select all` supplies a subset and enables the source. Clearing the final unused model turns it off immediately; protected in-use models remain selected.
- `Save changes` commits model preferences as one revision-checked snapshot; `Cancel`, Escape, and backdrop dismissal discard staged changes. A stale reply cannot overwrite a newer saved scope. An in-use model (including approval-paused work and reviewers) cannot be disabled; unused models remain editable. All sources off is valid: one quiet composer hint links to Settings, Send is disabled, and drafts and existing chat/model identities remain intact. Removed integrations display their saved model names with `Unavailable`; no automatic substitution.
- `Save changes` is disabled until source enablement or the selected model-ID sets differ from the loaded baseline; reverting changes disables it again. Catalog names/order, search and disclosures do not count as edits. Refresh updates only that source's model rows, preserving all staged choices, search, open groups and focus. Newly discovered models remain unchecked, absent selected IDs remain selected, and removing the focused row returns focus to its source summary. Late responses from closed dialogs cannot modify or unlock a new dialog.
- Starting Codey, opening the picker, and enabling Websites never launch model websites. Sending opens only the target website as needed and reuses its existing login. User-owned tabs stay open. API connection discovery and model inference are separate operations.
- Local model names come from actual model metadata/IDs or the user's optional display name. `Connection` expands inside its source, with `Address`, `Default model`, and `API key`. A saved key stays masked and is never returned to the browser. `Connect` becomes `Save connection` for an existing connection; this explicit connection edit is separate from model preference Save. Rejected saves retain the form and show one error line.
- An unchanged connected form disables `Save connection`; editing enables it and reverting disables it. An unconnected form with an address can still Connect. A blank API key retains the saved key; a newly entered key counts as an edit. Collapsing/reopening Connection does not reload over staged fields. Saving connection fields never saves model preferences, and Save changes never saves connection fields.
- Connection and Advanced explanations use 11.5px `--text-dim`. Loading failures and validation errors retain `--err-text`; section labels, optional metadata, and placeholders retain `--muted`.
- Initial connection loading disables the form while leaving dismissal available. Failed loading replaces `Loading connection…` with `Could not load connection` and an adjacent gray `Retry` text action. Retry reloads inside the same dialog, prevents duplicate loads, and ignores replies from closed dialogs. Successful keyboard retry moves focus to `Address` if the user has not moved focus elsewhere. Save failures retain edits and use the existing error line; they do not offer a load Retry that could replace those edits.
- `Advanced` defaults closed and contains optional `Display name`, `Context limit`, `API protocol`, and `Tool calling`. Context presets live in one dark listbox menu with exact token counts and `Custom…`; a custom input appears only when selected. The limit must match the model server's loaded context, not imply that Codey enlarges it.
- API budgets come from the admitted model and connection configuration. A Local preset is configuration, not a detected model capacity; confirmed KoboldCpp uses the smaller configured/running window. Missing catalog limits never imply a fabricated 32K model window. Output reservation and safety margin must fit that same window.
- Token information stays in the existing Run details rows: `API usage` is accumulated service-reported usage; `Request context` describes the `Last prepared request`. Never add them or label the latter as live/next-request occupancy. Prefix estimates with `~`, and label known partial totals with the number of requests with incomplete usage. Unknown is not zero; cache/reasoning details are included in their respective totals. No persistent token panel, extra progress chrome, or vendor-specific styling.
- Context maintenance adds no chat cards, progress banners, token countdown or focus/scroll changes. Natural work state is model input, not visible conversation. Existing Run details includes service-reported summary usage in totals; its last request context remains the last conversation request, never a background summary. Real preparation/generation failures remain explicit; quiet maintenance must not invent completion or hide failed execution.
- Quiet maintenance retains the newest complete tool exchange and a readable route to older evidence. Work state preserves the latest user correction and treats historical questions as data; it must not answer them or invent verification. A failed or outdated candidate cannot replace current work.
- API protocol offers `Chat Completions / Responses`, chosen explicitly without request-failure switching. Tool calling separately offers `Automatic / Native / Compatibility`; Compatibility uses text-based tool requests over the chosen API protocol. Shell approval remains in force. Inputs, select triggers, and listbox options use existing tokens, 34px heights, and 6px radii; no browser-default white buttons. Text inputs retain their single quiet border during pointer and keyboard focus, without an extra outline or focus shadow; the caret and text selection communicate editing. Select triggers and other buttons retain restrained gray keyboard focus outlines.

---

## 6. Motion

Keep motion subtle and functional:

| Element | Animation |
|---------|-----------|
| Sidebar collapse | width 0.18s ease |
| Drawer | transform 0.18s ease |
| Spinner | rotate 0.8s linear |
| Hover backgrounds | 0.12s |

Avoid: colored pulses, bounce, large parallax, decorative transitions.

---

## 7. Copy & tone

- **UI chrome:** English, short, sentence case for sentences; Title Case for menu items where already established (`New chat`, `Add project`).
- **System messages:** factual (`Running`, `Approval required`, `Reached turn limit`).
- **User-facing errors:** one line when possible; prefix `Error` in status rows.
- **No emoji**, no exclamation-heavy marketing, no “嗨/欢迎使用”.

Examples:

| Prefer | Avoid |
|--------|-------|
| `New chat` | `新建聊天` |
| `You` | `你` |
| `Send a message to Codey…` | `给 Codey 发消息…` |
| `View diff` | `查看改动` (in UI chrome) |
| `Allow` / `Deny` | Colored `允许并继续` buttons |

README and docs for end users may stay in Chinese; **the web UI stays English** unless this document is explicitly revised.

---

## 8. Adding new features — checklist

Before shipping any UI change, verify:

1. **Colors:** Green roles share the §2 palette through semantic aliases: `--ok-dot` for provider dots, resting Research graph nodes and hovered connections, `--selection-bg` for native selected text, mutually exclusive `--nav-current`/`--nav-highlight` for navigation, and `--diff-add-*` inside diff only. Deletions use `--diff-del-*`; error strings use `--err-text`. All other chrome stays neutral. Are selected text and diff code/marks readable, and do signed diff marks and navigation width still communicate state without color?
2. **Hierarchy:** Can this be done with label size, weight, or spacing instead of a new color?
3. **Chat area:** Does role hierarchy and message presentation match its designated reference?
4. **Actions:** Secondary/destructive actions behind `⋯` or text links, not permanent colored buttons?
5. **Status:** Spinner or text — not a new chip color?
6. **Icons:** Stroke SVG, `currentColor`?
7. **Copy:** English, no emoji?
8. **Mono vs sans:** Code/paths in mono; prose in sans?
9. **Width:** Does prose share the reading-width token, while inspection content remains usable at the available width?
10. **Tokens:** New values added to `:root` — not scattered hex in rules?

---

## 9. Anti-patterns (do not reintroduce)

These existed in earlier iterations and were intentionally removed:

- Blue/purple primary buttons (`Send`, provider accent, active session `#1f2840`)
- Uncoordinated message variants or decorative role-colored surfaces
- Colored tool pills (`write` tag in accent blue)
- Green/yellow done/shell/limit cards with gradients
- `task-strip` status chips with per-state hues
- Pulsing blue connection dot
- Inline full project paths in the sidebar
- Permanent row action buttons (`+`, `×`, `✎`) always visible
- `alert()` / native dialogs for routine UX (acceptable temporarily; replace with inline/toast when touching that flow)
- Emoji in labels or status

---

## 10. Implementation notes

- **Frontend source and shipped assets:** edit TypeScript modules in `frontend/src`, the HTML skeleton in `frontend/index.html`, and CSS in `frontend/public/assets`. Vite builds the frontend; `python -m tools.publish_frontend` publishes the validated build to `codey/web`. Production modules share the internal runtime registry; browser tests explicitly opt into its test bridge. Do not edit generated bundles or add production globals.
- **Do not fork the palette:** all color/spacing tokens stay in `frontend/public/assets/tokens.css`; components consume semantic aliases from that file. `app.css` owns component styles. Rebuild and publish after CSS changes so shipped assets and their freshness metadata match the source. `tests/test_ui_architecture.py` ratchets inline `<style>` to zero and only lets the inline `<script>` budget go down.
- **Theme:** the current application implements dark mode. New surfaces use semantic
  tokens rather than assuming hard-coded dark colors. System/light/dark selection
  is a redesign feature requiring its own implementation and contrast acceptance.
- **Accessibility:** interactive rows and disclosures use native buttons and gray focus-visible outlines. Hidden sidebar/drawers are inert. Menus return focus on Escape; drawer keyboard focus stays within the open inspection surface and returns to its trigger on close. Respect reduced-motion preferences. When adding color is unavoidable, pair with text labels (never color alone).
- **Desktop text selection:** create the pywebview window with `text_select=True`; its default suppresses native page selection. Do not counter the host setting with global CSS overrides.
- **Asset freshness:** versioned asset URLs include the current CSS/JS content revision, so source updates also invalidate existing desktop caches without requiring a package version bump.

---

## 11. Future work (within this language)

These are compatible extensions — implement using the rules above:

- Toast notifications (gray panel, mono optional, no green/red toast backgrounds)
- Provider health: offline = default solid gray `.dot`, online = `.dot.ok` using the shared green token; other status chrome stays neutral
- Top bar `Export markdown`, etc. — menu pattern same as `.ctx-menu`

---

## 12. Reference

Canonical editable implementation: `frontend/index.html`, `frontend/src`, and `frontend/public/assets`.
Published implementation: `codey/web`.
Product context: root `README.md`

When this document and the code disagree, **update the code to match this document**, or amend this document in the same PR with a short rationale.

Shell results preserve explicit process status and signed integer exits across restart. Missing historical status displays `Result status unknown`; a zero exit alone does not invent a missing status. Command preview truncation and its complete-command digest remain visible after restore.


### Process disclosure and local maintenance diagnostics

Keep interface SVGs in Codey's own assets and ship them with the frontend;
never reference another application's installation directory. Command, Worked
and Thinking disclosures share quiet typography and the same trailing chevron.
Progress spinners remain animated at a slower rate with reduced motion enabled
so a pending request remains distinguishable from an idle or stalled interface.

Each user request has one dynamic progress location: the process summary.
Show `Sending…`, `Working · Waiting for reply`, or the active tool operation
there, with one spinner while work is active. Create this summary before the
first tool or reasoning event. Never append a second Working row beneath the
process. On approval, replace the disclosure with static `Waiting for approval`
text: no chevron, click target, or spinner. Keep the approval card visible outside
the hidden process history. Restore the process disclosure when work resumes. On task
completion, stop the spinner and use `Worked`. Keep failed-request Details and
Retry controls available in their existing recovery row. A tool failure alone
does not terminate the request's progress.

Shell approvals use one rounded neutral card outside the process disclosure.
Approval cards, expanded command panels and the composer share the single
`--panel-radius: 10px` token. Keep the approval title at 13px, weight 500, so it
fits the surrounding interface rather than reading as a large heading.
Its header is `Run this command?`; its body shows `Shell`, the command, and the
working directory. Omit the Reason section and generic risk-description copy.
Keep truncated-command notices and the full-command digest when applicable.
Place `Deny` and the contrasting primary `Allow once` button at the bottom
right. Do not offer broader approval scopes without matching backend semantics.
While submitting, lock both choices and show the spinner and `Submitting…`
inside the primary button. An unconfirmed decision keeps both choices locked
and offers `Check status`; never treat a lost response as permission to submit
another decision or execute the command again.

Process history defaults to one collapsed summary per user request, including
while tools are running. Show live activity in the summary; preserve a user's
explicit expansion across updates and chat changes. Keep one Thinking entry
with ordered reasoning sections rather than a repeated row per turn. Omit the
step-number footer. Tool failures and approval controls stay outside the folded
history so required actions remain available.

Local memory/maintenance warnings do not mean the command or task is paused.
Keep them in request Details or the process diagnostic body, deduplicated and
collapsed by default. Preserve diagnostic references; do not append a repeated
`Local update paused` line after each command. Previously saved instances of
that informational message use the same diagnostic presentation. Actual task
pause, denial, failure and Stop states retain their normal controls.

### Conversation content and continuity (2026-10-16)

The middle conversation column follows one user request through work, questions,
artifacts and the final answer. Keep one author label where the process and answer
are contiguous, one live Working indicator, and a server-measured `Worked for Ns`
summary when elapsed time is available. Do not estimate historical durations.
An expanded individual tool result retains its state when the chat is rebuilt.

**Markdown and files.** Use one Markdown parser for answers, Research and document
preview: emphasis, strike-through, task lists, rules, tables, quotations and fenced
code share the monochrome typography. Repair the common model-generated space
before closing bold delimiters, without altering literal code. Keep pipes inside
table code spans in their original cell. HTML remains inert; executable URL
schemes and embedded SVG never become active content. A malformed percent escape
or one failed image must not prevent the rest of the answer from rendering.

An inline filename is a candidate until the server confirms a file in this chat's
workspace. Do not turn `json.dumps`, private symbols or globs into file buttons.
Explicit local links, Windows paths, line numbers and abbreviated unique file
references use the existing scoped document entry. Missing and ambiguous paths
retain explicit server outcomes. Confirming an image does not decode it as text:
read raster bytes through the same scoped service (2 MiB limit; PNG/JPEG/GIF/WebP),
display a thumbnail, and support pointer/keyboard enlargement and Escape. Image
failure offers `Reload image` and local Details, never a model-task retry.

**Deliverables.** Ordinary file mentions remain inline. An explicitly named
deliverables list may become compact file rows with a filename, type, and Open.
Research has one report entry using the same document pane; `View research evidence`
and `Use in Project` are secondary menu actions. Keep evidence access available
when no report was saved. Never add a second generic Open beside the report action.

**True streaming.** Native API text deltas carry run identity, stream identity and
monotonically ordered snapshot sequences. Local native API selections request
streaming; existing non-native text protocols do not expose their control JSON.
Only actual received text is shown progressively. Completed Markdown blocks keep
their existing DOM; only the changed tail is replaced. Selection defers a content
update until the selection is released. Final settlement updates the same answer,
including a final answer longer than the bounded live prefix; history never plays
a typing animation. Interrupted text stays readable with a quiet partial-response
notice. Rendering failures offer `Render again` beside source/details and cannot
execute a tool, regenerate a model response or erase the remaining conversation.

**Pending input.** The existing Send path queues the current draft for the next
turn. The redesign adds a separate steering action for the current task; acceptance
and consumption must follow the backend's actual generation boundary. Keep queued,
accepted and consumed states distinct and retain a session-scoped Stop. Before sending, persist the chat and a unique delivery
identity. The server saves a version-1 SQLite receipt before admission and before
dispatch; the browser retains only delivery identities awaiting confirmation.
Queued inputs show `Queued · next turn`, `Return to draft` and `Remove`. Return to
draft first removes the queued item with its current revision; it preserves newer
draft text. Stop durably pauses queued work. Resume is explicit. A lost response,
an interrupted dispatch, or an admitted run with no final receipt after restart
remains visible for `Check status`; never automatically resubmit it. Completed
receipts do not consume pending capacity and retain their deduplication identities.
Queue recovery owns its status actions, without a second request-level Retry.

**User questions.** The native `ask_user` control suspends work after acknowledging
its exact original tool call. Display one compact question form outside folded
process history: question, two to six radio choices, optional descriptions and
free text, Dismiss and Confirm. Use the existing 13px title and 10px radius; no
Reason section and no collapsible waiting header. Show `Waiting for your answer`
without a live spinner or a duplicate Working line. Ordinary Send stays disabled
until the question is settled. Lock controls during submission; unconfirmed answers
offer a status check. Accepted answers are saved before entering the input queue,
recover after restart, and resume the original model, connection revision, intent,
native history and authorization. A choice never grants shell permission. Resumed
commands still require their own approval. Updating another question must preserve
the focused form and composition state. Answered cards become a quiet answer row.

**Editing and long histories.** Hover/focus on a user message reveals Copy and
`Edit message`. Keep the current Codey inline editor with `Cancel` and `Send`:
Escape cancels and Ctrl/Cmd+Enter sends. Editing does not open a modal or require
a branch name, and retains the composer's separate draft. Its current ordinary
send semantics remain unchanged. Historical-context branching is a separate
entry-aware backend contract, not an extra naming dialog on Edit. Copy, branch,
regenerate and quote actions all follow the message-local hover/focus disclosure
rule above, including pointer-exit hiding and non-hover availability.
Conversation branching does not restore workspace files or authorize
repeating an executed command. Initially mount the
latest 100 saved messages, extending backward to a complete user-request boundary.
`Load earlier messages` prepends another window while retaining existing nodes and
the reading anchor; `Search this conversation` queries the server archive and can
locate messages outside the mounted tail. The UI snapshot remains bounded to 500
messages, while the independent transcript archive retains messages imported from
the incoming state and uses epoch-safe cursor pagination. Wheel and
reading keys inside a scrollable command, code block or table belong to that
region until its directional edge, then may reach the outer conversation.

All UI assets ship inside Codey. Runtime records, queue receipts, question records
and serialized UI state retain version 1. SQLite connections belong to individual
transactions and close deterministically; closing a live queued run retains its
owning application resources until its terminal receipt is written. No runtime
dependency on a Codex installation, compatibility migration, blind retry, fake
streaming timer or full-answer Collapse is introduced.

### 2026-10-16 boundary clarifications

- A queued input rejected explicitly by admission (`input_queue_full`) or by a
  later identity query (`input_not_found`) is a recoverable local `Not sent`
  record. `Return to draft` removes that local record before restoring the text.
  Transport loss and other ambiguous responses remain `Delivery unconfirmed`
  and require `Check status`; they are never treated as safe to resend.
- A Research report card resolves through the `runId` on its own message. A
  message without a run identity may use the only stored Research run for that
  session, preserving older single-run records; it never selects the latest run
  when multiple histories exist.
- Markdown code spans are parsed before table-pipe and emphasis repair. Ordinary
  prose pipes and `**` inside code remain byte-for-byte literal after rendering.
- Every confirmed local image is scheduled through the existing four-worker
  pool. The UI no longer silently truncates image jobs at twenty; each failed
  image keeps its own reload/details path.
- Command output observation uses a bounded execution snapshot with separate
  stdout/stderr capture and an execution identity. The current browser reads
  snapshots on demand by polling while a command is running; it does not claim
  a dedicated output SSE stream. Read or drain failures preserve the observed
  execution as `unknown` and expose reload/status actions rather than rerunning
  the command.
