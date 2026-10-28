# ConductLoop — design system & UI plan

*2026-10-23. This document is the contract for the visual work. It replaces
the "each page has its own CSS" situation with one shared system, and it
sequences the rebuild of a UI that today is 30 pages and 15.106 lines of HTML.*

---

## 1. What the UI is today

| Surface | Size | Notes |
|---|---|---|
| `ops_dashboard/web/index.html` | **6.749 lines** | The whole cockpit: shell, graph canvas, file browser, markdown renderer, search, alerts. Inline `<style>` + inline JS. |
| `ops_dashboard/web/*.html` | 30 pages | `chat`, `sessions`, `learning`, `connectors`, `team`, `brain`, `voice`, `approvals`, `activity`, … |
| `ops_dashboard/web/*.css` | 4 files | `chat.css`, `sessions.css`, `learning.css`, `connectors.css` — each with its own tokens, duplicated from scratch. |

**The problem is not the colours, it is the absence of a system.** Every page
redefines `:root`, two accents compete (deep blue `#1e40af` and violet
`#7c3aed`), and there is no type scale, no spacing scale, no elevation scale
and no motion vocabulary. The same card, button and pill look different in
Chat than in Sessions.

**The 6.749-line `index.html` is the real blocker**: every visual change to
the main screen is an edit inside a file where the CSS, the JS and the
markup are interleaved.

## 2. The system

`ops_dashboard/web/theme.css` is the single source of truth. It is linked by
every page and served by the app (`GET /theme.css`).

### Direction: "instrument panel"

ConductLoop is a cockpit for a 7-business holding. It is opened at 08:00 for
a briefing and left open all day. So:

- **One accent, and it means "you can act on this"** — violet `--cl-accent`.
  Nothing decorative is violet.
- **Data colours are reserved for state** — blue = information, green = ok,
  amber = warning, red = failure. A green dot is always a status, never a
  decoration.
- **Density over decoration** — 13px UI type, a 4px space scale, hairline
  borders instead of heavy shadows, and real elevation only where something
  floats above the page.
- **Light by default, dark by choice** — the app already has a `body.dark`
  toggle; the system keeps that behaviour so no page is left half-migrated.

### Tokens

| Group | Names | Scale |
|---|---|---|
| Surfaces | `--cl-bg`, `--cl-bg-soft`, `--cl-bg-2`, `--cl-bg-3`, `--cl-bg-hover`, `--cl-bg-active` | 6 steps |
| Ink | `--cl-ink`, `--cl-ink-soft`, `--cl-ink-muted`, `--cl-ink-faint` | 4 steps |
| Lines | `--cl-border`, `--cl-border-2`, `--cl-border-strong` | 3 steps |
| Accent | `--cl-accent`, `--cl-accent-hover`, `--cl-accent-soft`, `--cl-accent-ink`, `--cl-focus` | 5 |
| State | `--cl-info`, `--cl-ok`, `--cl-warn`, `--cl-danger` (+ `-soft` variants) | 4 × 2 |
| Type | `--cl-text-2xs … --cl-text-3xl` | 11 / 12 / 13 / 14 / 16 / 20 / 28 / 36 |
| Space | `--cl-space-1 … --cl-space-16` | 4 / 8 / 12 / 16 / 20 / 24 / 32 / 40 / 48 / 64 |
| Radius | `--cl-radius-xs … --cl-radius-full` | 4 / 6 / 10 / 14 / 20 / 999 |
| Elevation | `--cl-shadow-1 … --cl-shadow-3` | 3 levels |
| Motion | `--cl-dur-fast/base/slow`, `--cl-ease`, `--cl-ease-out` | 120 / 180 / 260 ms |

### Components

`.cl-card` · `.cl-btn` (+ `--primary`, `--ghost`, `--danger`) · `.cl-input` ·
`.cl-textarea` · `.cl-select` · `.cl-pill` (+ `--ok`, `--warn`, `--danger`,
`--info`, `--accent`) · `.cl-label` · `.cl-kbd` · `.cl-empty` ·
`.cl-skeleton` · `.cl-stat` · `.cl-table` · `.cl-toast` · `.cl-divider` ·
`.cl-shell` · `.cl-rail` · `.cl-topbar` · `.cl-statusbar`

### Migration bridge

The old token names are kept as **aliases** of the new ones, so a page
adopts the new palette by changing six lines instead of rewriting its CSS:

```css
:root {
  --bg: var(--cl-bg);
  --ink: var(--cl-ink);
  --blue: var(--cl-info);
  /* … */
}
```

`index.html` already does this (2026-10-23). Every other page still defines
its own `:root`; converting it is mechanical and is the first task of each
page in the plan below.

## 3. The plan, in order

### Phase A — Foundations *(done, 2026-10-23)*
- [x] `theme.css` with tokens + components, served at `/theme.css`
- [x] `index.html` aliased onto the system (one palette, one dark mode)
- [x] Route registration in `routes_meta.py`

### Phase B — Break the monolith
- [ ] Move `index.html`'s inline `<style>` into `web/app.css` (tokens alias the
      system, so no visual change)
- [ ] Split its inline JS into `web/app/*.js`: `shell.js`, `graph.js`,
      `browser.js`, `markdown.js`, `search.js`, `alerts.js`
- [ ] Extract the shell (rail, topbar, status bar) into `web/shell.html`
      included by every page, so Chat/Sessions/Learning stop re-implementing it

### Phase C — Components
- [ ] Replace every hand-rolled card/button/pill/table with the `.cl-*`
      components, page by page, starting with `index.html` and `chat.html`
- [ ] One `toast`, one `modal`, one `empty state`, one `skeleton` pattern

### Phase D — The screens that matter
1. **Today (8am)** — the briefing: what changed, what is broken, what needs a
   decision. This is the screen that earns the product its daily habit.
2. **The cockpit** — the 15 businesses as one canvas, with their real state
   (URLs up/down, mentions, phase) instead of a static list.
3. **The chat with the org** — the CEO directing the agents, with the agent's
   tools, memory and pending approvals visible.
4. **Media** — the new image/video/speech surface, showing assets, jobs and
   their state (queued → running → ready).

### Phase E — Motion & polish
- [ ] 120/180/260ms vocabulary applied to every transition
- [ ] `prefers-reduced-motion` respected everywhere
- [ ] Focus rings, keyboard paths, empty states, loading skeletons

## 4. Rules for the work

1. **One system.** No new page brings its own `:root`.
2. **Tokens, not literals.** `#7c3aed` in a component is a review comment.
3. **Data colours mean state.** If it is not ok/warn/error/info, it is not
   green/amber/red/blue.
4. **Density is a feature.** A cockpit that shows 8 projects when it can show
   15 is a worse cockpit.
5. **No dark-mode migration by halves.** A page is either aliased onto the
   system or it keeps its old palette until it is done.

## 5. Open questions for the product

- The **rail** (vertical, rotated labels: SEARCH / DAY / TEAM / BOARDROOM /
  BUSINESS) is unusual and costs horizontal space. It should become a normal
  icon rail with tooltips, and the five "views" need to be reconciled with the
  15-item sidebar that lives inside the same screen.
- The **canvas** puts every project at a fixed grid position; it is not really
  a graph. Decide: a real force graph, or an honest list/board.
- The **status bar** carries global counters (docs, projects, URLs, provider).
  It is useful and should stay, but it should read as a status bar and not as
  a second header.
