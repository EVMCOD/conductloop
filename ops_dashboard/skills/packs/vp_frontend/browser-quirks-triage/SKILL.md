---
name: browser-quirks-triage
description: Use when a layout breaks in a specific browser, at a specific width, or on a specific device, to isolate the cause with a fixed reproduction matrix and a minimal fix.
---

# Browser Quirks Triage

A bug report like "it looks broken on my phone" is not a bug report. This
procedure turns it into one reproducible case, one named cause and one
minimal fix — in under 20 minutes.

## When to use

- A layout breaks on one browser, one device or one width only.
- Horizontal scroll appears on mobile.
- A modal, sticky header or dropdown renders in the wrong place.
- Text or a form is unusable at 320 px width.
- The bug appears only in production (build, minification or a stale asset).

## Step 1 — Pin the matrix (5 minutes)

Fill this in before touching code. A bug with a blank cell is not ready.

| Dimension | Values to test |
|---|---|
| Width | 320, 375, 768, 1024, 1440 |
| Browser | Safari (macOS + iOS), Chrome, Firefox, Edge |
| Zoom | 100%, 200%, 400% |
| Input | mouse, touch, keyboard |
| Theme | light, dark |
| State | loading, empty, error, long content |

Reproduce it on the narrowest width and the oldest browser you support.
If it does not reproduce there, it is not the bug — find the condition that
makes it appear.

## Step 2 — Isolate the cause (5 minutes)

Bisect by disabling one thing at a time, in this order:

1. **CSS reset or Tailwind preflight** — a reset that removes default margins
   or sets `box-sizing` differently changes every layout.
2. **Missing viewport meta** — without
   `<meta name="viewport" content="width=device-width, initial-scale=1">` a
   phone renders the desktop layout scaled down, and every width looks wrong.
3. **Intrinsic sizing** — a flex child with long text or an image without a
   fixed aspect ratio pushes the layout wider than the viewport. The usual
   culprits: `min-width: auto` on a flex item, `white-space: nowrap` on a
   label, a table with many columns.
4. **Overflow origin** — in devtools, set `overflow-x: hidden` on `body`: if
   the scrollbar disappears, something is wider than the viewport. Then find
   it by walking the DOM and checking `scrollWidth` against `clientWidth`.
5. **Stacking contexts** — `transform`, `filter`, `backdrop-filter` and
   `will-change` create a containing block; a `position: fixed` inside one
   stops being fixed relative to the viewport. This is the single most common
   cause of "the modal is offset on Safari".
6. **iOS specific** — 100vh includes the browser chrome (use `100dvh`);
   the keyboard shrinks the viewport (`visualViewport`); `position: fixed`
   elements jump when the keyboard opens; momentum scrolling needs
   `-webkit-overflow-scrolling: touch` only on old versions.
7. **Zoom** — a layout that breaks at 400% zoom usually relies on a fixed
   pixel width instead of a max-width.

## Step 3 — The fix (5 minutes)

- Prefer a layout fix over a browser-sniffing fix. `position: sticky` works
  in every supported browser; a user-agent check is maintenance debt.
- Use `min-width: 0` on a flex child that must shrink, `min-w-0` in Tailwind.
- Give images `max-width: 100%` and a defined `aspect-ratio`.
- Use `clamp()` for fluid type instead of media-query breakpoints per step.
- For sticky headers, set `scroll-margin-top` equal to the header height so
  anchor links are not hidden underneath it.
- Never use `user-scalable=no` or `maximum-scale=1`; it breaks zoom and
  fails accessibility checks.
- If the fix must be browser-specific, comment the exact bug and the
  version it applies to, and remove it within a release.

## What a good answer looks like

```
CASE: <url/path> at 320px, Safari iOS 17, dark mode, empty state
CAUSE: flex item <name> with min-width auto pushes container to 380px
FIX: min-w-0 on the flex child + truncate on the label
VERIFY: 320/375/768/1024/1440 x Safari/Chrome/Firefox, light+dark -> no h-scroll
EVIDENCE: before/after screenshot at 320px
```

## Traps

- Fixing the bug in the browser where it does not appear.
- "It works on my machine" without recording the matrix cell.
- Adding a global `overflow-x: hidden` to hide a horizontal scroll: it hides
  the symptom and clips sticky positioning and fixed elements.
- Testing only light mode; the dark theme is where contrast and border
  colours break.
- Forgetting that Safari and Firefox do not support every CSS feature, and
  that a missing feature degrades to a completely different layout rather
  than an error.
- Shipping a fix without re-testing the states (loading, empty, long
  content): those are where overflow hides.
