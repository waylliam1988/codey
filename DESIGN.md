# Codey UI Design Language

This document is the design baseline for Codey’s control panel (`codey/web/index.html`).  
All future UI work should extend this system — not introduce a second visual language.

---

## 1. Design intent

Codey is a **local developer and research tool**, not a consumer chat app. The UI should feel like Cursor or a minimal IDE panel:

- **Monochrome first** — black, gray, white. Hierarchy comes from typography and spacing, not color blocks.
- **Quiet by default** — idle states show almost nothing. Activity is signaled with motion (spinner) or text, not banners.
- **No decoration** — no emoji, no gradients for status, no “chat bubble” aesthetics, no brand-blue/purple accents.
- **Restrained green exceptions** — the provider availability dot (`#4ec9b0`), transient Research graph hover, native text selection, and the conversation navigation's single reading/preview marker. Navigation uses subdued dark/gray green; other resting chrome stays monochrome.
- **English in the UI** — labels, buttons, placeholders, and system messages use English (`New chat`, `You`, `Codey`, `Allow`, …).
  User messages, model answers, and returned thinking text retain their original language; the English rule applies to fixed application copy.

When in doubt: remove color, remove chrome, remove copy.

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
| `--ok-dot` | `#4ec9b0` | **Provider online dot** (+ optional soft ring `rgba(78,201,176,.14)`), plus the Research graph hover accent below |
| `--err-text`| `#d28a8a` | Error **text only** — never error backgrounds or borders |

### Native text selection

| Token | Value | Use |
|-------|-------|-----|
| `--selection-bg` | `#365149` | Low-saturation dark green for native text selection only |

Selected text uses `--text` on `--selection-bg`, with at least 4.5:1 contrast.
Apply the same selection treatment to prose, nested code and links, drawer
details, and editable text in the composer and Settings. This transient
interaction tint replaces the browser's blue selection; do not reuse it for
selected rows, focus outlines, buttons, status, or permanent surfaces.

### Conversation navigation

| Token | Value | Use |
|-------|-------|-----|
| `--nav-current` | `#365149` | Current reading position in the conversation's tick rail |
| `--nav-highlight` | `#6f9183` | The one tick selected by pointer hover or keyboard focus |

These markers are mutually exclusive: while a tick is selected, the current
reading marker becomes an ordinary gray tick. Moving away restores the dark
green reading marker. Animate width only, so color transitions cannot briefly
show both greens. Neither token belongs on status, buttons, or other surfaces.

### Allowed tint exception

Inside the **changes diff drawer**, added/removed lines may use very weak backgrounds:

- Add: `rgba(78, 201, 176, .07)` — derived from `--ok-dot`
- Del: `rgba(210, 138, 138, .07)` — derived from `--err-text`

Do not reuse these diff tints elsewhere. Background tint for scanning belongs
only in diff; native text selection uses its separate interaction token above.

Inside the **Research graph canvas** (the Graph tab), the hovered
node and its directly connected edges may use `--ok-dot` as a transient hover
accent. The resting (non-hover) graph stays gray/white; no other component may
use `--ok-dot` for hover states.

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
┌──────────────┬──────────────────────────────────────┐
│   Sidebar    │  Topbar (title · status · ⋯)         │
│   260px      ├──────────────────────────────────────┤
│              │  Chat stream (max-width 760px)       │
│              ├──────────────────────────────────────┤
│              │  Composer (max-width 760px)          │
└──────────────┴──────────────────────────────────────┘
                                    │ Changes drawer │
                                    │ (fixed right)  │
```

- Main + chat + composer content: **max-width 760px**, centered.
- Sidebar: **260px by default**, adjustable from **220–420px** and collapsible to 0. Remember the preferred width in the local browser profile. On desktop, clamp the displayed width to leave at least 400px for the main area; shrinking the window does not overwrite the preference. No overlay on desktop unless viewport ≤ 720px; the narrow overlay uses the default width and disables resizing.
- Borders: 1px `--border`. Prefer spacing over heavy frames.
- Border radius: **6–10px** for controls; **8px** for cards/menus. Avoid large “pill” radii except where already established (provider menu items).

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
- A monochrome gear + `Settings` row sits at the sidebar bottom, sharing the sidebar action alignment and 34px height. The topbar more menu and `Ctrl+,` open the same settings dialog when the sidebar is collapsed.
- The sidebar's existing right divider has a 6px transparent drag target, with a `col-resize` cursor and a single gray line on hover/focus. It is a keyboard-focusable vertical separator named `Resize sidebar`; Left/Right adjust by 10px, Home/End reach the available bounds, and double-click restores 260px. Pointer capture keeps dragging stable outside the divider. Escape, pointer cancellation, or window blur cancels an active drag. Disable width animation and text selection only during the drag; release immediately restores ordinary selection. Hiding the sidebar hides the separator.

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
  - When another chat owns the run: `Running in <chat> · Open`. Stop names the same chat; the single-run limit still applies.
  - Error: small dot + `--err-text` label (`Disconnected`)
- No colored status chips in the top bar.

### 5.5 Chat stream — de-bubbled messages

Do **not** use chat bubbles, alignment by role, or role-colored backgrounds.

```
YOU
User message text, left-aligned, no background.

CODEY
Assistant reply in sans, --text.

  · read    buggy.py              → 6 lines
  · write   snake.py              → 277 chars
```

**Role label:** `YOU` / `CODEY` above each block.

**Process disclosure:** one group per user request; continuation after approval uses the same group. Ordinary tools and optional thinking belong inside it. The final answer stays outside and expanded; errors and approval cards remain visible outside the disclosure.

```
▸ Worked · read 4 files · edited 2 files · ran 1 command

Assistant reply remains fully visible here.
```

Running copy is `Working · <current action>` or `Working · Waiting for reply`; terminal copy reflects the actual result (`Worked`, `Failed`, `Stopped`, `Paused`, `Waiting for approval`). Count completed, successful actions only, and changed files only for edits. Completed groups default to closed; explicit user disclosure choices survive updates. Step numbers are available inside the disclosure. Legacy records without run identifiers may retain their original Turn dividers.

`Thinking` appears only for nonempty reasoning text returned separately by the model. It defaults to closed, retains its source language, and has a Copy action. Never infer thinking from wait time, parse it out of answer text, or scrape model websites to manufacture this section. The same rendering applies to every connection.

**Tool lines:** `.tool-line` grid — dot · kind (7ch) · path · → · result. Errors: result text `--err-text` only.

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

Submission failures use confirmed causes only: `Another chat is running · Open` navigates to the verified run owner; `Select the local model again · Choose model` opens the existing model menu in the original chat; `Codey is temporarily busy · Retry` retries the original request. Unknown causes retain `Could not send the message · Retry`. Navigation and model selection do not send automatically. Failure actions belong to the original chat and preserve newer drafts in every chat.

Manual Retry belongs to the fixed identity of a user message. Each explicit retry has a new execution identity, recorded under that request, and updates one status position after its existing output. Keep the user message, tool records, reading position, and newer drafts intact. While retrying, replace error text and actions with a gray spinner and `Retrying · Waiting for reply`; another failure replaces that same row. Success hides the transient row and displays the answer normally, without a success banner. Only the latest user request can be retried, and admission or an active execution disables duplicate submission. Independent errors without a request identity must not guess which question to resend.

`Details` expands a quiet inline attempt history using the existing Run Details typography. Raw errors and `Local update paused` belong to the attempt history, not repeated chat messages. Model/owner recovery actions may sit beside Retry and never send automatically. Persist attempt facts and their recovery actions, but not the disclosure's open state or rendered details. Restore active attempts from authoritative local execution state; if an accepted attempt cannot be confirmed, replace its spinner with `Response was not confirmed` and an explicit Retry. Never resend automatically on reload or a transport failure. Late events from an earlier attempt cannot change the current status.

**Run Details** (inline receipt expansion):

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

File stats stay gray in the stream; weak tint only inside diff drawer.

**Assistant long replies:** render expanded by default. If the reply is long, show a quiet `Collapse` text action below it; clicking it folds the body and changes the action to `Expand`. Do not default to collapsed answers.

**Reading and copying:** prose, code, paths, commands, diff, and error details support native pointer selection and copying. Message/code Copy actions complement selection. New output follows only while the reader is near the end; otherwise a quiet `Back to latest` action is available as a centered 30px circular down-arrow button above the composer. Use a single subtle gray border, neutral background and gray stroke icon; retain the accessible name `Back to latest` and the existing gray keyboard focus outline, without a native `title` tooltip or hover text label. Hide the button near the latest output. Chat switching restores reading position. Metadata refresh preserves unchanged message DOM and explicit disclosure state. Hidden chat DOM is cached for at most six recent chats; older chats retain their reading offset.

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
the dark green marker, and stretches nearby gray ticks into a gentle wave.
After 220ms, show the question and a plain-text reply excerpt in a dark gray
tooltip, kept inside the available reading area. Hover never scrolls; click,
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

Whole-message Copy icons under user messages and model answers are hidden at rest on devices with hover. Reveal only the relevant message's icon when its continuous region (body, intervening space, and button) is hovered or contains keyboard-visible focus. Preserve the button's layout space, but disable pointer interaction while hidden. Mouse focus alone must not keep an icon visible after leaving; Tab reveals the button with the existing gray focus background. Keep icons visible on devices without hover. During copying, keep the icon visible and prevent duplicate activation without removing keyboard focus. After completion, retain gray success feedback for 1.2 seconds, or retryable failure feedback for 4 seconds, even after pointer exit. Code-block and thinking Copy controls retain their existing visibility rules. Respect reduced-motion preferences.

Right-clicking selected chat/drawer text or an editable text selection opens a small `Copy` menu in the existing `.ctx-menu` language: dark `--panel`, gray text, 8px radius, one subtle border, and no accent. The menu item has no individual border or focus outline. Pointer opening shows plain text; hover or keyboard navigation uses the existing gray row background for focus. Copy uses the exact selected text, preserving the draft and selection; whole-message and code Copy buttons keep their existing behavior. Shift+F10 / the context-menu key opens the same selection menu. Escape returns focus and selection to the source; outside click, scrolling, window resizing, and chat switching dismiss it. Keep the menu within the viewport. A successful copy briefly shows gray `Copied`; failure shows gray `Could not copy` and leaves Copy available to retry. Do not offer a stale text selection for an unselected input or expose password selections. Desktop copying uses this menu without enabling developer/debug menus in the host.

Copy success uses a temporary gray checkmark or `Copied`, with an accessible name; no success accent. Markdown links allow HTTP(S) only, tables scroll horizontally when necessary, and fenced code preserves whitespace with a quiet language label. Model output is escaped, never rendered as arbitrary HTML.

**Shell approval:**

```
Approval required
Runs in E:\project
$ pytest -q
[ Deny ]  [ Allow ]
```

Plain `--panel` card. `Allow` is `text-btn primary` (weight 500), not colored. Safety through copy and layout (`Deny` left, `Allow` right).

**Welcome (empty chat):**

```
Codey
Send a message to start.
```

No marketing copy, no emoji.

### 5.6 Composer

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
- **Context row:** only `Choose folder` and `Research` stay in the quiet line above the input. Enabled actions use `--text-dim`; the separator stays `--faint`. Disabled inactive actions retain `--muted` and 0.55 opacity; active Research retains `--text`, including when disabled at 0.55 opacity.
- **Research token:** visible by default as text, not as a framed button. Hover changes text to `--text`; active Research uses brighter text only. No border, background, chip, underline, font-weight change, or accent color.
- **Provider picker:** borderless; status dot + label + chevron. This is the only visible provider/model selector in the composer. Online state uses `--ok-dot`; offline state is the default solid gray `.dot`.
- **Send / Stop:** 34px square **icon buttons** (`.icon-btn`) with 16px icons, transparent until hover; enabled Send uses `--text` and the original paper-plane outline. Send and Stop occupy one fixed position. No filled accent send button.
- **Model menu:** directly show the short catalog without a search input. Opening focuses the current model. Arrow keys navigate, Enter selects, Escape closes and returns focus. Do not invent unsupported model/effort controls. Menus have a viewport-bounded scroll area.
- API connections show their actual model names; Local can supply an optional display name and the complete model ID in Settings. The menu contains only models explicitly selected in Settings from enabled sources; never invent a website's selected model version. Optional integrations contribute source and catalog data to the same controls, without vendor-specific UI branches, accents, cards or Settings panels. Discovery never selects a new model. Catalog availability does not promise successful generation. The model menu contains only models; connection configuration belongs in Settings.
- A separate borderless effort selector sits beside the model name: `Gemma4 12B ⌄ Low ⌄`. The selector disappears for unsupported connections and website models. Accessible copy names `Thinking effort`; visible labels are `Off / Mini / Low / Med / High / XHigh / Max`, filtered by verified capabilities. The two borderless selectors sit 2px apart, without a separator. The model menu's right edge aligns with the effort selector's chevron and adjusts to the current labels; without an effort selector it uses a 160px width. The effort menu uses a 160px width. Both menus are bounded by the viewport. Long model labels use ellipsis and retain their complete accessible name. Model and effort triggers and menu rows have no native hover tooltips. Each selector has its own menu, keyboard focus, and dismissal. Opening one closes the other.
- The enabled effort trigger uses `--text-dim`, becoming `--text` on hover or while its menu is open; disabled effort retains `--muted` and 0.55 opacity. A provider probe explanation uses 11.5px `--text-dim` in the existing model menu.
- Thinking controls require verified connection/model metadata. Active KoboldCpp Jinja and a matching `enable_thinking` template support Off/High; verified versions 1.112.2–1.117.x additionally support minimal/low/medium budgets. High uses the native unrestricted thinking budget within the total output limit. Mini/Med map to minimal/medium. Show the concrete initial level rather than Default or On. Every non-Off choice explicitly enables thinking; do not fabricate distinct Max behavior. Returned reasoning and configurable thinking are independent capabilities.
- API model and thinking choices belong to the chat, persist with it, and are captured at admission for the next request. Background/continued requests use that chat's choices, even while another chat is visible. A new chat on the same connection inherits the active chat's selected model and thinking choices, with its own independent effort map. With no matching active choice, it uses the connection default. Connection address, credentials, API protocol, context limit, tool calling, and default model live in Settings. Pending command continuation and cold recovery retain the admitted selection. A changed connection cannot silently carry a stale chat selection or credential to another endpoint. Deleted connections preserve readable old chats and their model identity; sending requires an explicit new choice.
- `Enter` hint: immediately before Send/Stop with a 6px gap, visible on composer focus/hover only, `--faint`. `Enter` sends; `Shift+Enter` inserts a newline.
- **Drafts and submission:** text and caret/selection belong to a chat, retained in memory across chat switches and removed when that chat is deleted. Choosing a folder changes context without sending. Admission shows `Sending…`, prevents duplicate requests, and only consumes the accepted draft snapshot; failures preserve editing and Retry never overwrites a later draft.
- **Pending approval:** one neutral `Approval required · Review command` entry appears above the frame, pointing to the original command card and its chat. Enter never approves a command globally. The command card remains the single approval surface.

Provider is **session-level** — it lives in the bottom provider picker, not duplicated in the context row or elsewhere.

Research is also **session-level**. It lives in the composer context row, never beside the model picker as a separate primary action. User-facing copy says `Research`; internal words like vault, knowledge, artifact, and index should not appear in the main chrome.

### 5.7 Changes drawer

- Fixed right panel, `--bg-2`, slides in from the right.
- Header actions: text buttons, no borders (`drawer-btn`).
- File list: mono paths, gray stats; expand row to show diff.
- Identify the bound project and `Working tree` or `Snapshot` scope. Change-summary file links open and expand the exact file. Switching chats/projects closes old inspection surfaces; late loads/restore responses cannot overwrite a new scope.
- Diff lines: see §2 tint exception.

### 5.8 Research drawer

- Same fixed right panel language as the changes drawer.
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

- SVG stroke icons, ~1.8px stroke, `currentColor`, no fill (except send/stop glyphs where needed).
- Monochrome only — icons inherit `--text-dim` → `--text` on hover.
- No emoji, no colored icon sets.

### 5.11 Settings

- Use one viewport-bounded dark dialog, about 560px wide, with a `Settings` heading, quiet close icon, and labeled connection fields. Native dialog modality keeps keyboard focus inside; Escape/backdrop dismissal returns focus to the trigger. No second right workbench drawer.
- Models use one shared source row: disclosure, source name, selected count, and a monochrome master toggle. Websites, optional API integrations, and Local use the same component and keyboard semantics. Expanding shows checkboxes with actual model names, `Select all`, and `Clear`. Long catalogs add `Find models`; the daily composer menu stays short and has no search.
- Source toggles preserve the selected subset. Turning a source off excludes it from the picker, automatic discovery, failover, repair, review, and advisor selection. Re-enabling restores only previous choices. Directory refresh updates names and capabilities without selecting new arrivals or forgetting absent selected IDs. `Refresh models` is explicit; cache display requires no connector request.
- Zero selected means the source is off in both the dialog and stored preferences. Its master toggle stays disabled until an explicit model selection or `Select all` supplies a subset and enables the source. Clearing the final unused model turns it off immediately; protected in-use models remain selected.
- `Save changes` commits model preferences as one revision-checked snapshot; `Cancel`, Escape, and backdrop dismissal discard staged changes. A stale reply cannot overwrite a newer saved scope. An in-use model (including approval-paused work and reviewers) cannot be disabled; unused models remain editable. All sources off is valid: one quiet composer hint links to Settings, Send is disabled, and drafts and existing chat/model identities remain intact. Removed integrations display their saved model names with `Unavailable`; no automatic substitution.
- Starting Codey, opening the picker, and enabling Websites never launch model websites. Sending opens only the target website as needed and reuses its existing login. User-owned tabs stay open. API connection discovery and model inference are separate operations.
- Local model names come from actual model metadata/IDs or the user's optional display name. `Connection` expands inside its source, with `Address`, `Default model`, and `API key`. A saved key stays masked and is never returned to the browser. `Connect` becomes `Save connection` for an existing connection; this explicit connection edit is separate from model preference Save. Rejected saves retain the form and show one error line.
- Connection and Advanced explanations use 11.5px `--text-dim`. Loading failures and validation errors retain `--err-text`; section labels, optional metadata, and placeholders retain `--muted`.
- Initial connection loading disables the form while leaving dismissal available. Failed loading replaces `Loading connection…` with `Could not load connection` and an adjacent gray `Retry` text action. Retry reloads inside the same dialog, prevents duplicate loads, and ignores replies from closed dialogs. Successful keyboard retry moves focus to `Address` if the user has not moved focus elsewhere. Save failures retain edits and use the existing error line; they do not offer a load Retry that could replace those edits.
- `Advanced` defaults closed and contains optional `Display name`, `Context limit`, `API protocol`, and `Tool calling`. Context presets live in one dark listbox menu with exact token counts and `Custom…`; a custom input appears only when selected. The limit must match the model server's loaded context, not imply that Codey enlarges it.
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
| `New chat` | `新建纯聊天` |
| `You` | `你` |
| `Send a message to Codey…` | `给 Codey 发消息…` |
| `View diff` | `查看改动` (in UI chrome) |
| `Allow` / `Deny` | Colored `允许并继续` buttons |

README and docs for end users may stay in Chinese; **the web UI stays English** unless this document is explicitly revised.

---

## 8. Adding new features — checklist

Before shipping any UI change, verify:

1. **Colors:** Only provider online dots and Research graph transient hover may use `--ok-dot`; `--err-text` for error strings; `--selection-bg` only for native selected text; navigation's mutually exclusive `--nav-current`/`--nav-highlight` only on its single position/preview tick; all other chrome neutral?
2. **Hierarchy:** Can this be done with label size, weight, or spacing instead of a new color?
3. **Chat area:** Still de-bubbled? No new bubble variants?
4. **Actions:** Secondary/destructive actions behind `⋯` or text links, not permanent colored buttons?
5. **Status:** Spinner or text — not a new chip color?
6. **Icons:** Stroke SVG, `currentColor`?
7. **Copy:** English, no emoji?
8. **Mono vs sans:** Code/paths in mono; prose in sans?
9. **Max width:** Content still centered at 760px?
10. **Tokens:** New values added to `:root` — not scattered hex in rules?

---

## 9. Anti-patterns (do not reintroduce)

These existed in earlier iterations and were intentionally removed:

- Blue/purple primary buttons (`Send`, provider accent, active session `#1f2840`)
- User/assistant colored bubbles and right-aligned user messages
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

- **Zero-build asset modules:** the UI ships as `codey/web/index.html` (HTML skeleton + core state/SSE/composer/boot script) plus `codey/web/assets/`: `tokens.css` (`:root` design tokens), `app.css` (all other styles), and plain-script IIFE modules (`render.js`, `research_graph.js`, `research_drawer.js`, `research_runs.js`, `changes_drawer.js`, `local_context_drawer.js`, `run_details.js`, `provider_ui.js`, `settings.js`, `ui_state.js`, `sse.js`, `composer.js`, `conversation_nav.js`, `conversation_ui.js`), each owning exactly one `window.Codey*` namespace. No npm, bundler, or ESM; scripts load synchronously in a fixed order and receive index state via `init(deps)`.
- **Do not fork the palette:** all color/spacing tokens stay in `tokens.css`; never redefine them per module or per page. `tests/test_ui_architecture.py` ratchets inline `<style>` to zero and only lets the inline `<script>` budget go down.
- **Dark mode only:** there is no light theme. New surfaces should assume dark gray backgrounds and light text.
- **Accessibility:** interactive rows and disclosures use native buttons and gray focus-visible outlines. Hidden sidebar/drawers are inert. Menus return focus on Escape; drawer keyboard focus stays within the open inspection surface and returns to its trigger on close. Respect reduced-motion preferences. When adding color is unavoidable, pair with text labels (never color alone).
- **Desktop text selection:** create the pywebview window with `text_select=True`; its default suppresses native page selection. Do not counter the host setting with global CSS overrides.
- **Asset freshness:** versioned asset URLs include the current CSS/JS content revision, so source updates also invalidate existing desktop caches without requiring a package version bump.

---

## 11. Future work (within this language)

These are compatible extensions — implement using the rules above:

- Toast notifications (gray panel, mono optional, no green/red toast backgrounds)
- Provider health: offline = default solid gray `.dot`, online = `.dot.ok` — do not add a second green usage
- Top bar `Export markdown`, etc. — menu pattern same as `.ctx-menu`

---

## 12. Reference

Canonical implementation: `codey/web/index.html`  
Product context: root `README.md`

When this document and the code disagree, **update the code to match this document**, or amend this document in the same PR with a short rationale.
