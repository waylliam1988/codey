# Codey UI Design Language

This document is the design baseline for Codey’s control panel (`codey/web/index.html`).  
All future UI work should extend this system — not introduce a second visual language.

---

## 1. Design intent

Codey is a **local developer and research tool**, not a consumer chat app. The UI should feel like Cursor or a minimal IDE panel:

- **Monochrome first** — black, gray, white. Hierarchy comes from typography and spacing, not color blocks.
- **Quiet by default** — idle states show almost nothing. Activity is signaled with motion (spinner) or text, not banners.
- **No decoration** — no emoji, no gradients for status, no “chat bubble” aesthetics, no brand-blue/purple accents.
- **One persistent exception color** — the provider availability dot (`#4ec9b0`). Research graph hover may reuse the same token transiently inside the canvas; the rest of the chrome stays monochrome.
- **English in the UI** — labels, buttons, placeholders, and system messages use English (`New chat`, `You`, `Codey`, `Allow`, …).

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
| `--text-dim`| `#a0a0a0` | Secondary body, tool output |
| `--muted`   | `#6b6b6b` | Labels, group titles, placeholders |
| `--faint`   | `#4a4a4a` | Separators (`·`, `→`), disabled hints |

### Semantic (minimal)

| Token       | Value     | Use |
|------------|-----------|-----|
| `--ok-dot` | `#4ec9b0` | **Provider online dot** (+ optional soft ring `rgba(78,201,176,.14)`), plus the Research graph hover accent below |
| `--err-text`| `#d28a8a` | Error **text only** — never error backgrounds or borders |

### Allowed tint exception

Inside the **changes diff drawer**, added/removed lines may use very weak backgrounds:

- Add: `rgba(78, 201, 176, .07)` — derived from `--ok-dot`
- Del: `rgba(210, 138, 138, .07)` — derived from `--err-text`

Do not reuse these tints elsewhere. Diff is the only place where background tint helps scanning.

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
- Sidebar: **260px**, collapsible to 0. No overlay on desktop unless viewport ≤ 720px.
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
- **Secondary actions:** hidden until hover — `⋯` opens a context menu. Do not show permanent `+` / `×` icon clusters.

Context menus (`.ctx-menu`):

- `--panel` background, `--border`, 8px radius, light shadow
- Items: borderless rows, hover `--hover`
- Destructive item: on hover, text becomes `--err-text` only (no red row background)

### 5.4 Top bar

- Title breadcrumb: `project-name / chat-title` — project in `--muted`, slash in `--faint`, chat in `--text`.
- **Status area:**
  - Idle: empty (no dot, no “Connected” label)
  - Running / connecting: CSS **spinner** + short label (`Running`, `Connecting to Edge…`)
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

**Turn divider:**

```
Turn 2 ───────────────────────────────
```

Left label + 1px `--border` line extending right.

**Tool lines:** `.tool-line` grid — dot · kind (7ch) · path · → · result. Errors: result text `--err-text` only.

**Status rows** (done, pause, limit, error):

```
DONE · 2 files changed · checks passed · restore available        View diff
PAUSED · No progress for several turns          Continue
ERROR · Connection refused                      Retry
```

Structure: uppercase prefix + body + optional **text link** action (`link-btn`). No colored boxes.

**Changes summary** (inline, not a pill):

```
Changes   3 files                    View diff
  M  codey/web/index.html           +42 -18
```

File stats stay gray in the stream; weak tint only inside diff drawer.

**Assistant long replies:** render expanded by default. If the reply is long, show a quiet `Collapse` text action below it; clicking it folds the body and changes the action to `Expand`. Do not default to collapsed answers.

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
Choose folder · Research · DeepSeek          ← composer-context (11.5px, --muted)
┌─────────────────────────────────────────┐
│ Send a message to Codey…                │
└─────────────────────────────────────────┘
● DeepSeek ⌄              Enter     ■  ▶
```

- Box: `--bg-2`, 1px `--border`, radius 10px; focus border `--text-dim` (not blue).
- **Context row:** `Choose folder`, `Research`, and the current provider stay in one quiet line above the input.
- **Research token:** visible by default as text, not as a framed button. Hover changes text to `--text`; active Research uses brighter text only. No border, background, chip, underline, font-weight change, or accent color.
- **Provider picker:** borderless; status dot + label + chevron. Online state uses `--ok-dot`; offline state is the default solid gray `.dot`.
- **Send / Stop:** square **icon buttons** (`.icon-btn`), transparent until hover. No filled accent send button.
- `Enter` hint: visible on composer focus/hover only, `--faint`. `Enter` sends; `Shift+Enter` inserts a newline.

Provider is **session-level** — it lives in the composer, not duplicated as a primary control elsewhere.

Research is also **session-level**. It lives in the composer context row, never beside the model picker as a separate primary action. User-facing copy says `Research`; internal words like vault, knowledge, artifact, and index should not appear in the main chrome.

### 5.7 Changes drawer

- Fixed right panel, `--bg-2`, slides in from the right.
- Header actions: text buttons, no borders (`drawer-btn`).
- File list: mono paths, gray stats; expand row to show diff.
- Diff lines: see §2 tint exception.

### 5.8 Research drawer

- Same fixed right panel language as the changes drawer.
- Header actions are text buttons, no borders.
- Notes and sources are plain rows with title, type, short excerpt, and path or URL when useful.
- Restore is shown as a text action only when the backend has a valid restore snapshot for that run.
- Do not call this drawer `Knowledge` or `Vault` in the UI.

### 5.9 Icons

- SVG stroke icons, ~1.8px stroke, `currentColor`, no fill (except send/stop glyphs where needed).
- Monochrome only — icons inherit `--text-dim` → `--text` on hover.
- No emoji, no colored icon sets.

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

1. **Colors:** Only provider online dots and Research graph transient hover may use `--ok-dot`; `--err-text` for error strings; everything else neutral?
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

- **Zero-build asset modules:** the UI ships as `codey/web/index.html` (HTML skeleton + core state/SSE/composer/boot script) plus `codey/web/assets/`: `tokens.css` (`:root` design tokens), `app.css` (all other styles), and plain-script IIFE modules (`render.js`, `research_graph.js`, `research_drawer.js`, `changes_drawer.js`, `provider_ui.js`), each owning exactly one `window.Codey*` namespace. No npm, bundler, or ESM; scripts load synchronously in a fixed order and receive index state via `init(deps)`.
- **Do not fork the palette:** all color/spacing tokens stay in `tokens.css`; never redefine them per module or per page. `tests/test_ui_architecture.py` ratchets inline `<style>` to zero and only lets the inline `<script>` budget go down.
- **Dark mode only:** there is no light theme. New surfaces should assume dark gray backgrounds and light text.
- **Accessibility:** maintain keyboard focus on interactive rows; prefer visible hover states over permanent color coding. When adding color is unavoidable, pair with text labels (never color alone).

---

## 11. Future work (within this language)

These are compatible extensions — implement using the rules above:

- Tool-line collapse (`read × 5 files`)
- Inline rename instead of `prompt()`
- Toast notifications (gray panel, mono optional, no green/red toast backgrounds)
- Minimal Markdown in assistant messages (sans body; code blocks in mono)
- Provider health: offline = default solid gray `.dot`, online = `.dot.ok` — do not add a second green usage
- Top bar `Export markdown`, etc. — menu pattern same as `.ctx-menu`

---

## 12. Reference

Canonical implementation: `codey/web/index.html`  
Product context: root `README.md`

When this document and the code disagree, **update the code to match this document**, or amend this document in the same PR with a short rationale.
