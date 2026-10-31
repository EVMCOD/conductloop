---
name: component-contract-review
description: Use when a shared React or TypeScript component is about to be merged or promoted to the design system, to check its props API, states, keyboard behaviour and required tests.
---

# Component Contract Review

A component is an API. Review the API before reviewing the pixels: a
beautiful component with a leaky props contract costs more than a plain one.

## When to use

- A new component is proposed for reuse across two or more screens.
- A shared component is being changed (props added, markup changed).
- Before promoting a component from one product into the shared library.

Skip it for a one-off view that will never be reused: review that as a screen.

## 1. The props API (the contract)

- **Names say intent, not appearance.** `variant="danger"` beats
  `isRed`; `size="sm"` beats `small`. Appearance-named props freeze the design.
- **Boolean props are a smell.** Three booleans (`isPrimary`, `isSmall`,
  `isFlat`) mean the component should take `variant` and `size` enums.
  Two booleans that are genuinely independent are fine.
- **Typed unions over `any`.** Discriminated unions on `variant` give the
  editor autocomplete and make illegal combinations unrepresentable.
- **Spread the rest props.** A component that does not forward `...rest` will
  be forked the first time someone needs a `data-testid`.
- **No required prop without a default story.** If a prop is required, there
  must be a documented example of a valid value.
- **Callback names match intent:** `onChange` for the value, `onSubmit` for
  the form, `onDismiss` for closing. If the component fires `onChange` on
  mount, it is a bug, not a quirk.
- **Controlled and uncontrolled must be explicit.** Pick one. If both, name
  them (`value` / `defaultValue`) and never mix them in one instance.

## 2. State and data

- **The four states must exist**: loading, empty, error, success. A component
  that only renders the success case is half a component. If it fetches, it
  renders all four, including a partial-failure state.
- **Loading should not be a layout jump.** Reserve the height of the content
  (skeleton matching the final box) or the CLS score will pay for it.
- **Errors are recoverable.** An error state offers a retry; it does not
  dead-end.
- **Empty states distinguish "no data yet" from "no data ever".** A brand-new
  account and a filtered-to-zero result are different screens.
- **No derived state stored.** If a value can be computed during render, do
  not keep it in `useState` with a `useEffect` that syncs it.
- **Effects are for subscriptions, not derivation.** A `useEffect` that sets
  state from props is a bug waiting for an infinite loop.
- **Key prop on lists is the item id.** Never the array index on a list that
  can reorder.

## 3. Keyboard and screen reader

- Every interactive element is a real `button` or `a`. A `div` with
  `onClick` is not a button: it is not focusable, not activatable with Enter
  or Space, and not announced.
- Visible focus: never remove the outline without replacing it with something
  at least as visible.
- Focus order matches the visual order.
- Escape closes overlays and returns focus to the trigger.
- Overlays trap focus while open and restore it on close.
- Icon-only controls carry an accessible name.
- State is announced: `aria-expanded`, `aria-selected`, `aria-pressed` or the
  native equivalent.
- Touch targets: 24x24 CSS px minimum, 44x44 for primary actions.

## 4. Tests that must exist before merge

1. **Interaction test** (React Testing Library): the primary action fires the
   callback with the expected payload. Use user events, not `fireEvent`.
2. **State test**: at least the empty state renders the empty message.
3. **Keyboard test**: Tab reaches the control, Enter or Space activates it.
4. **Accessibility assertion**: an automated axe pass on the rendered states
   of the component.
5. **Type test**: `tsc --noEmit` passes with the component used the wrong way
   on purpose (only if the project runs type tests; otherwise a type-level
   snapshot).
6. **Visual snapshot** — optional and often noise. Use it only for genuinely
   visual components (a chart, a badge) and update it deliberately.

## What a good answer looks like

```
COMPONENT: <name>   PATHS: <n>
Props API: <ok | list of changes>
States: loading ok | empty MISSING | error ok
Keyboard: <ok | issue>
Tests present: interaction | empty-state | keyboard | a11y
Verdict: approve | approve-with-changes | block
```

## Traps

- Reviewing the happy path only; the empty state is where components die.
- Accepting `any` because "we will type it later".
- A boolean-prop explosion that nobody notices until the eleventh variant.
- Treating a snapshot test as behaviour coverage.
- Forgetting that a component used in a table cell needs different padding
  than the one used in a modal.
- Copy-pasting an existing component instead of extending it: three forks of
  the same button are a design-system problem, not a component problem.
