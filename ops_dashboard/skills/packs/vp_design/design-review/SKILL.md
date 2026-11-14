---
name: design-review
description: Use when a screen, modal or flow is ready for review before it ships, to produce a ship / fix / block verdict with a numbered defect list instead of opinions.
---

# Design Review

A 20-minute structured pass over one screen or flow. Output is a verdict
(`ship`, `fix`, `block`) and a numbered defect list — nothing else.

## When to use

- A screen is "done" and nobody has looked at it with fresh eyes.
- Before a release that changes a user-facing surface.
- When two parts of the same product look like they came from different products.

Do not use it for: marketing copy, a token audit across many screens (that is
design-system consistency), or a code-level WCAG pass (that is the accessibility
check).

## Before you start (5 min)

1. Get the artifact: a live URL, a screenshot at real width, or a recording of
   the flow. Never review a description of a design.
2. Get the state list. A screen is not one screen: it has loading, empty,
   error, success, partial, permission-denied and max-content states. Ask for
   the states you were not given — missing states are the most common defect.
3. Get the target: platform, the narrowest width that must work, and the one
   action the screen exists to produce.
4. Write the primary action down before you look. Everything after that
   answers one question: does the hierarchy point at it?

## The review, in this order

Do not reorder. Early checks decide whether later checks are worth doing.

1. **Hierarchy.** Squint at the screen: what do you see first? It must be the
   primary action or the answer to the user's question. One primary action per
   view. If two elements compete for the first read, one of them is not
   primary.
2. **States.** All seven states rendered. An empty state that only says "No
   data" is a defect; an error state with no next step is a blocker.
3. **Spacing rhythm.** One base unit: 4 px. Allowed steps 4 / 8 / 12 / 16 / 24 /
   32 / 48. A single off-grid value is a nit; three or more on one screen is a
   defect (the grid is being ignored, not broken).
4. **Type scale.** At most 4 sizes on one view. Body text at least 16 px on
   touch, 14 px on dense desktop. Line height 1.4–1.6 for body, 1.1–1.3 for
   headings. Two weights plus one display weight, no more.
5. **Color.** One accent color per surface. Status colors (success, warning,
   danger) mean state and are never decoration. Text contrast at least 4.5:1,
   borders and icons at least 3:1 (the accessibility check owns the
   measurement). Color should carry less than half of the visible area.
6. **Consistency.** Every control is an existing component, not a new one. A
   new visual pattern needs a reason and a name. The same idea rendered twice
   with different spacing is the defect, not the second instance.
7. **Interaction states.** Hover, focus-visible, active, disabled, loading.
   Focus must be visible on every interactive element.
8. **Content.** Real words, real lengths. No lorem ipsum, no "Item 1". The
   longest realistic string must not break the layout. Numbers, dates and
   currency are localized. Empty and error copy says what to do next.
9. **Motion.** Durations: 120 ms micro, 200 ms transition, 300 ms panel.
   Nothing above 400 ms. Motion never blocks the next action, and the flow
   respects reduced-motion.
10. **Edges.** Longest realistic label, longest number, zero records, one
    record, a thousand records, a failed request, a slow request (skeleton
    shown), signed-out user, expired session.

## Verdict rules

**Blockers — cannot ship:**

- A reachable state is missing (loading, error, empty).
- Text contrast below 4.5:1, or an invisible focus indicator.
- The primary action is not the most prominent element.
- The flow dead-ends: an error with no retry, a save with no confirmation.
- The layout breaks at the required width, or content is clipped.

**Fix before the next release — not blockers, not "later":**

- Inconsistent spacing on a repeated element.
- A new component that duplicates an existing one.
- Missing hover / focus / active states.

**Nits — list them, do not block:**

- Optical alignment, sub-pixel corners, tone of voice, decorative detail.

Rule of thumb: 3 blockers means redesign, not patch. 1–2 blockers means patch and
re-review that screen only.

## Output

```
VERDICT: ship | fix | block
PRIMARY ACTION: <the one action>
BLOCKERS (n): <defect> | <where> | <fix>
FIX BEFORE NEXT RELEASE (n): <defect> | <fix>
NITS (n): <defect>
```

## Traps

- Reviewing on a 27-inch monitor at 200% zoom. Review at 1x, at the narrowest
  supported width, and in dark mode if the product has one.
- Confusing "clean" with "boring": one accent and one type scale is not a
  defect.
- Rewriting copy. Note it, do not fix it here.
- Approving because the founder likes it. The verdict is about the user.
- Spending 40 minutes. If the review is not finished in 20, cut to the
  blockers and schedule a second pass.
