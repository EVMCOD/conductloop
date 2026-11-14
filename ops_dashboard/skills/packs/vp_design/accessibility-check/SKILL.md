---
name: accessibility-check
description: Use when a UI is about to ship or after an accessibility bug report, to run the WCAG 2.2 AA automated and manual checks and produce a per-criterion pass/fail list.
---

# Accessibility Check

A 30-minute check that produces a criterion-by-criterion pass/fail list with
the fix for every failure. Target: WCAG 2.2 Level AA.

## When to use

- Before shipping any user-facing surface (web or mobile).
- After an accessibility complaint, a support ticket, or an audit finding.
- Once per product per quarter as a regression net.

Scope note: this checks the interface, not the content strategy. WCAG covers
"can a user operate it", not "is it a good experience".

## Rules that are numbers, not opinions

| Check | Threshold | Criterion |
|---|---|---|
| Body text contrast | >= 4.5:1 | 1.4.3 |
| Large text (>= 24 px, or >= 18.7 px bold) | >= 3:1 | 1.4.3 |
| Borders, icons, form outlines, focus rings | >= 3:1 against adjacent color | 1.4.11 |
| Touch target size | >= 24x24 CSS px, 44x44 recommended | 2.5.8 |
| Keyboard reachability | every action reachable and operable without a pointer | 2.1.1 |
| Focus indicator | visible, >= 3:1 contrast, never removed | 2.4.7 / 2.4.11 |
| Focus order | follows the visual order, no traps | 2.4.3 |
| Reflow at 320 CSS px | no two-dimensional scrolling | 1.4.10 |
| Text zoom to 200% | no loss of content or function | 1.4.4 |
| Accessible name | every control exposes a name, role and value | 4.1.2 |
| Errors | identified in text, not colour only, and announced | 3.3.1 / 4.1.3 |
| Motion | anything moving > 5 s can be paused | 2.2.2 |
| Target size | spacing so adjacent targets do not collide | 2.5.8 |

Contrast is computed, not eyeballed: relative luminance of each color, then
`(L1 + 0.05) / (L2 + 0.05)`. When the computed value is close to the threshold,
round against yourself — 4.49 fails.

## What to automate, what to do by hand

Automate (catches roughly a third of real issues):

- axe DevTools or Lighthouse accessibility run over the main routes.
- Contrast, accessible names, roles, form labels, heading order, duplicate ids.
- HTML validity and duplicate-id checks.

By hand (the part tools cannot do):

- Tab through the whole flow and follow the focus ring. Highest-signal single
  test in this checklist.
- Screen-reader pass on one real flow (VoiceOver on Safari, TalkBack on
  Android): landmarks, heading order, what gets announced on an error, what
  happens on a dynamic update.
- Reflow at 320 CSS px, then 200% zoom.
- Touch targets on a real phone.
- Focus not obscured by sticky headers, cookie banners, or floating chat
  widgets (a WCAG 2.2 addition, and the most common real failure on
  marketing pages).
- Errors and confirmations announced to assistive tech.
- Reduced-motion preference honoured.

## Procedure (30 min)

1. **Scope.** List the routes that carry real traffic (from analytics, not
   instinct). Review the top 3 plus the checkout/signup flow.
2. **Automated pass.** Run axe on each route. Record the count of violations
   by impact, not by total.
3. **Keyboard pass.** Unplug the mouse. Complete the primary task on each
   route. Record every point where you are lost.
4. **Screen-reader pass.** One flow, one reader, spoken aloud as you go. Record
   every "it did not tell me what happened".
5. **Contrast pass.** Compute the ratios for the text/background pairs actually
   used, in both light and dark themes.
6. **Touch pass.** On a real phone, measure every tap target under 24 px and
   every pair of targets closer than 8 px.
7. **Classify.** Every finding gets: criterion, impact (blocker / serious /
   minor), effort to fix, and the one-line fix.

## Verdict rules

- **Blocker** — a keyboard user cannot complete the primary task, or text is
  below 4.5:1, or a control has no accessible name. Ship-blocking.
- **Serious** — a reachable but confusing state (focus order wrong, error not
  announced, target under 24 px). Fix in the current sprint.
- **Minor** — cosmetic (a redundant ARIA role, a heading level skip that does
  not affect navigation). Queue it.

Rule: an automated pass with zero violations is not a pass. It means the
automated third found nothing.

## Output

```
SCOPE: <routes reviewed>
AUTOMATED: <n> violations (<n> critical, <n> serious)
KEYBOARD: <pass/fail> — <where it broke>
SCREEN READER: <pass/fail> — <what was not announced>
VERDICT: pass | fix | block
FINDINGS (n): <criterion> | <where> | <observed vs required> | <fix> | <effort>
```

## Traps

- Testing only with a mouse, only on desktop, only in light mode.
- Believing a green tool report. Tool coverage is roughly a third.
- Fixing contrast by changing the whole theme instead of the one token.
- Adding ARIA to fix semantics that should be fixed in the markup (a `div`
  with a button role is still not a button).
- Treating an accessible name as a label: a name is what a screen reader
  announces; a visible label is what a sighted user reads. Both must exist
  when the control is visible.
