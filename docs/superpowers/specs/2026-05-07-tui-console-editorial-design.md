# tmuxx TUI — Console Editorial Redesign

Status: Draft for implementation
Date: 2026-05-07
Owner: Daniel Tang
Reference preview: `previews/console-editorial-tmuxx-ui.html`

## Goal

Replace the current visual treatment of the click-first cockpit (`TmuxTUI` in `tmuxx.py`) with a console-editorial aesthetic. The current implementation works functionally but reads as flat: only two contrast tiers (idle ≈ invisible, active ≈ near-white), no visible click affordance, no breathing room, and no visual anchor.

Functionality, layout responsibilities, and key bindings are unchanged. This is a presentation-layer rework.

## Non-goals

- No new features, no behavioral changes, no key rebinding.
- No new TUI library — Textual covers everything in this design.
- No color palette switching/theming work beyond setting the new defaults. Theme cycling stays as-is.
- No changes to `compose_window_grid`, modals, the agent CLI, MCP server, or any non-cockpit code path.

## Layout — chrome budget ≈ 14 rows

Top-to-bottom row plan (terminal cells; `1fr` = remaining):

| Row(s) | Height | Section           | Notes                                          |
| ------ | ------ | ----------------- | ---------------------------------------------- |
| 1      | 1      | sessions rail     | kicker `SESSIONS` + tags + primary/rename/kill |
| 2      | 1      | divider           | 1-cell `border-bottom: solid --border-dim`     |
| 3–7    | 5      | windows section   | bordered block: legend, action cluster, cards  |
| 8      | 1      | divider           |                                                |
| 9      | 1      | panes rail        | kicker `PANES @<id>` + chips + actions         |
| 10     | 1      | divider           |                                                |
| 11–12  | 2      | command dock      | meta line + button row                         |
| 13     | 1      | divider           |                                                |
| 14+    | 1fr    | preview           | header (1) + body (1fr)                        |
| last-1 | 1      | divider           |                                                |
| last   | 1      | breadcrumb footer | `tmuxx › convoke › @0 codex › %1` + live + ?   |

The breadcrumb sits at the bottom (per user preference). Top of screen begins with the sessions rail.

## Surface & color system — five tiers

Old palette has two effective tiers and snaps active states to near-white. New palette steps in even contrast increments with a dedicated accent tier for primary actions.

```css
:root {
  --bg: #0a0e0d; /* outermost */
  --panel: #0e1411; /* section bg */
  --surface: #15201b; /* idle button/chip */
  --raised: #1d2a23; /* hover/focus */
  --active: #243329; /* selected non-accent */

  --border-dim: #1f2c25; /* dividers */
  --border: #2c3a32; /* default border */
  --border-strong: #5a7263; /* hover/focus border */

  --text: #dce8df;
  --muted: #8da095;
  --faint: #5b6e64;

  --amber: #e0b148;
  --amber-soft: rgba(224, 177, 72, 0.16);
  --cyan: #77b9cd;
  --cyan-soft: rgba(119, 185, 205, 0.14);
  --green: #75c28e;
  --green-soft: rgba(117, 194, 142, 0.14);
  --red: #d97959;
  --red-soft: rgba(217, 121, 89, 0.14);
}
```

Declare these as Textual variables at the top of `TmuxTUI.CSS` using Textual's `$name: value;` syntax (e.g. `$amber: #e0b148;`) and reference them throughout the stylesheet. Avoid `rgba()` for accent-soft tiers — Textual prefers explicit hex blends; pre-compute against `--bg` (e.g. `$amber-soft: #1f1d12;`).

## Click-target states (the missing middle)

Every interactive cell follows this 5-state ladder:

| State    | bg             | text      | border        |
| -------- | -------------- | --------- | ------------- |
| Idle     | transparent    | `--muted` | none          |
| Hover    | `--raised`     | `--text`  | `--border`    |
| Active   | `--amber-soft` | `--amber` | `--amber`     |
| Primary  | `--amber-soft` | `--amber` | amber @ 45% α |
| Danger\* | `--red-soft`   | `--red`   | `--red`       |

\* danger style only applies on hover/focus to avoid drawing the eye to destructive buttons by default.

Three rules drive the feel:

1. **No more snap-to-white.** Active stops at amber.
2. **Borders mean clickable.** Idle has no border (reads like a label); border appears on hover and persists when active.
3. **Status indicators carry color, surrounding text stays calm.** A row never fully recolors based on status — only its glyph does.

## Status glyph convention

| Glyph | Meaning           | Color                              | Animation                               |
| ----- | ----------------- | ---------------------------------- | --------------------------------------- |
| `●`   | running           | `--cyan`                           | opacity pulse 1.6s                      |
| `◉`   | waiting for input | `--amber`                          | blink 1.1s                              |
| `○`   | idle              | `--faint`                          | none                                    |
| `▸`   | attached / active | `--text` / `--amber` (when active) | caret blink 850ms when on selected card |
| `⠋…⠏` | agent thinking    | `--cyan`                           | spinner (10 frames @ 100ms)             |

The spinner is the only new convention. It applies when a pane is BOTH `status=running` AND classified as an agent (existing classifier already exposes this). At the session/window level, the spinner appears if any contained pane is in agent-thinking state; otherwise the worst-status glyph for the contents wins (existing rollup logic).

## Section-by-section detail

### Sessions rail (1 row)

```
SESSIONS   • convoke   tmuxx   agents ⠋   release ◉    +New Session  Rename  Kill
```

- Kicker `SESSIONS` in `--faint`, uppercase, letter-spacing 0.12em.
- Each session is a `tag` (idle → hover → active states from the ladder).
- Right-aligned action cluster: primary `+New Session`, neutral `Rename`, danger `Kill`.
- Active session uses amber tier, not white.

### Windows section (5 rows, bordered — the visual anchor)

```
┌─ WINDOWS · @0 ──────────────────────  +New Window  Rename  Attach  Kill ─┐
│                                                                            │
│   ▸ 0 codex ⠋        1 server ●        2 qa ◉        3 notes ○            │
│     3 panes · agent  1 pane · vite     2 panes · qa  1 pane · idle 14m    │
│                                                                            │
└────────────────────────────────────────────────────────────────────────────┘
```

- The only bordered block in the layout. Border = `--border`.
- Inset legend at top-left and inset action cluster at top-right (overlap the border with `--bg`).
- Cards are 2 lines: title line (window number + name + status glyph) and subtitle line (`<n> panes · <human status>`).
- Active card: amber-soft bg, amber text, leading caret `▸` that blinks; sweep meter underline running left→right (2.4s).
- Cards use a 1-cell internal divider via grid `gap` so they read as discrete tiles.

### Panes rail (1 row)

```
PANES @0   • %1 codex ⠋   %2 zsh ○   %3 tests ◉   %4 build ●     SendKeys  SplitH  SplitV  Attach  Kill
```

- Same `tag` ladder as sessions.
- Kicker includes the current window id so the rail is self-describing.

### Command dock (2 rows)

Row 11 (meta):

```
COMMAND   targeting · pane %1 codex in @0
```

Row 12 (buttons):

```
+Window  Rename Window  Send Keys  Split H  Split V  Attach Pane     Refresh  Search  Theme  Copy
```

- Meta line tells the user _what the next command will hit_ — closes the gap where today you click a button and don't know what it'll affect.
- Left cluster = mutating actions (window/pane). Right cluster = utility actions.
- Primary `+Window` always amber; rest neutral; nothing destructive lives here (Kill stays in the parent rail).

### Preview (1fr)

```
PREVIEW · WINDOW   @0 codex   190×61 · 2 panes · attached         click a pane chip to scope · ⌘C copy
═══ amber sweep underline (animated 3.6s) ═══════════════════════════════════
~/GitHub/tmuxx main $ python3 -m unittest …
```

- Header replaces today's blank top of preview pane: mode label (`PREVIEW · WINDOW` / `PREVIEW · PANE` / `PREVIEW · SESSION`), target id, dimensions, pane count, attach state, then a hint on the right.
- Animated 1px amber sweep below the header — the only continuous "this is live" cue in the preview area.
- Body unchanged from current `PanePreview` rendering.

### Breadcrumb footer (1 row)

```
tmuxx › convoke › @0 codex › %1                                live · 0.2s   ?
```

- Left: pinned `tmuxx` label + path through current selection. Each segment is clickable and jumps focus to that scope.
- Right: live indicator (refresh interval) + help shortcut.

## Animation set

Default-on (cheap, calm):

- `●` pulse — opacity 0.55 ↔ 1.0 over 1.6s.
- `◉` blink — `text-style: blink` (Textual native) or 1.1s opacity step.
- `▸` caret blink on active window card — 850ms steps(2).
- Hover background transition — 120ms ease.
- Live pip glow — 2s ease-in-out.

Reserved for _active agent_:

- Spinner glyph — 10-frame braille rotation at 100ms via `set_interval(0.1, ...)` rotating a `Static`'s text.
- Sweep meter under active window card — implemented as a 1-row `Static` whose contents are a 1-character amber block at a position updated each tick (`set_interval(0.08, ...)`); CSS gradients are not available in Textual.
- Preview header sweep underline — same approach: a 1-row `Static` placed beneath the preview header with the highlight cell offset stepped each tick.

Animations are driven by Textual timers and consume negligible CPU. If load becomes a concern under SSH/tmux, gate the spinner/sweep timers on `len([p for p in panes if p.status == "agent_thinking"]) > 0` so they only run when needed.

## Mapping to current Textual code

Existing widget tree from `TmuxTUI.compose` (`tmuxx.py:1418`) is preserved. Only changes:

| Current id              | Change                                                                                                                                                                                                                                       |
| ----------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `#top-bar`              | Removed. Brand moves to footer breadcrumb.                                                                                                                                                                                                   |
| `#brand`                | Removed (replaced by breadcrumb leading `tmuxx`).                                                                                                                                                                                            |
| `#session-rail`         | Becomes the new top row. Same id, restyled.                                                                                                                                                                                                  |
| `#focus-panel`          | Renamed to `#windows-section`. Gets `border: solid $border;` and `border-title-align: left;` so the section title (`WINDOWS · @0`) is inset on the top border via Textual's native `border_title` API.                                       |
| `#focus-head`           | Action cluster (`+New Window`, `Rename`, `Attach`, `Kill`) sits as the first row INSIDE the bordered container, docked top, right-aligned. (Textual borders don't accept interactive widgets; only `border_title` / `border_subtitle` text.) |
| `#focus-panes`          | Renamed `#panes-rail`. Same content, restyled.                                                                                                                                                                                               |
| `#command-zone`         | Two-row layout: `#command-meta` (existing) + `#command-dock` (existing). New target line added in `#command-context`.                                                                                                                        |
| `#preview-panel`        | Gains a new `#preview-head` Static row above `self._preview`.                                                                                                                                                                                |
| (new) `#breadcrumb-bar` | New bottom row replacing the brand-only top bar.                                                                                                                                                                                             |

`ClickCell` keeps its API; only its CSS classes change. The `command-cell`, `nav-cell`, `nav-cell.window-card`, `nav-cell.pane-chip` rules in `TmuxTUI.CSS` are rewritten against the new tier system. Existing `data-action` wiring is untouched.

`PanePreview.set_*_content` methods are unchanged; the new preview header is rendered by a sibling `Static` updated in the same callbacks.

## Acceptance criteria

1. Cockpit launches and renders with the new layout on a 120×40 terminal without overflow or clipping.
2. Active session, window, and pane each visibly distinct from idle peers without using near-white.
3. Hovering a `ClickCell` produces a visible background + border change within 200ms (one Textual transition cycle).
4. Primary actions (`+New …`) read as amber-tier in both idle and hover.
5. Breadcrumb correctly reflects current selection and updates on selection change.
6. Spinner glyph appears on agent-thinking panes/windows/sessions and stops within 1 refresh interval after the agent completes.
7. All existing keyboard shortcuts (`q`, `?`, etc.) behave as before.
8. `python3 -m unittest test_tmux_agent_unit test_tmux_core_unit -v` passes (no regressions in non-UI logic).
9. Terminal height of 24 rows still shows at least 6 rows of preview body (degraded but usable).

## Out of scope (explicit)

- Theme picker UI changes.
- New status types or classification logic.
- Changes to `compose_window_grid` border rendering.
- Tree sidebar (already removed in prior commit; not coming back).
- Brand mark / logo work beyond the existing wordmark.
