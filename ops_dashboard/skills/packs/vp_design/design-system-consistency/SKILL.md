---
name: design-system-consistency
description: Use when the same idea appears twice with different styling, when a new component is added, or before merging two products, to keep one token set and one visual language.
---

# Design System Consistency

The job: one visual language across many small products, without a platform
team. The output is a token decision, a drift list, and a rule that keeps the
next screen from forking the system.

## When to use

- The same idea is implemented twice with different spacing, color or type.
- A new component is being added to a product that already has a token file.
- Two products are being merged, or one product is being cloned from another.
- A visual change is being made in a product that has no documented tokens.

## The model: three layers, one direction

| Layer | Example | Who may use it |
|---|---|---|
| Primitive | `blue-500`, `space-4`, `font-size-14` | nobody but the token file |
| Semantic | `surface-raised`, `text-muted`, `border-focus`, `danger-bg` | components only |
| Component | `button-primary-bg` | one component only |

Rule: a component may only reference semantic tokens. A component that names a
primitive (`blue-500`) has leaked the palette and will break the moment the
brand color changes. A component token may only be created when the value is
not expressible as a semantic token plus a variant.

## Adding a value: the 5-step rule

1. **Prove reuse.** The value must appear in 3 or more places, or be required
   by the brand. One-off values stay local to the component.
2. **Add the primitive** with the raw value.
3. **Add the semantic alias** with the intent ("border for a destructive
   action"), not the appearance.
4. **Replace the usages** and delete the inline values.
5. **Record it** in the token file with one line of intent, so the next person
   knows when it may and may not be used.

Cost of a token: one definition, one alias, one line of documentation, and every
usage you replace. A fork costs all of that again on every future change.
Never fork a token per product without a written reason.

## Drift audit (30 minutes)

1. Search the component sources for raw hex colors, excluding the token file.
   Every hit is a leak; each one becomes a candidate semantic token.
2. Search for pixel values that are not multiples of 4. Each is either a grid
   break or a deliberate exception — decide which and record the exceptions.
3. Search for repeated font-family and font-size declarations outside the type
   scale.
4. Count distinct border radii and box shadows in use. More than 2 radii means
   there is no radius scale.
5. Check theme parity: every semantic token must have a dark-mode value. A
   hardcoded color breaks exactly one theme, always the one nobody tests.

Rank the findings by number of occurrences and fix the top three. Do not
attempt a full migration inside a review; a drift list with counts is the
artifact.

## Brand rules that override tokens

- Logo: never stretched, never recolored, never retyped. Clearspace on all
  sides equal to the height of the logomark's cap height.
- One primary brand color, used for one thing (the primary action). If it
  appears on every button it stops meaning anything.
- One type family, 3–4 weights. A second family only with a written reason.
- Imagery: one style, decided once (photographic or illustrative, not both).
- Voice: three adjectives the product is, three words it never uses. Write
  them in the token file so copy can be checked against them.

## Versioning

- The token set has a version. Components pin to it.
- A renamed or removed token is a breaking change: deprecate for one minor
  version, then remove, and migrate every usage in the same change.
- Adding a token is a minor change and needs no migration.

## Output

```
TOKENS: <count> semantic, <count> primitive | dark parity: complete | gaps
DRIFT: <n> raw colors, <n> off-grid values, <n> duplicate type rules
TOP 3 FIXES: <what> | <files affected> | <effort>
DECISIONS: <new semantic tokens added> | <values kept local and why>
```

## Traps

- Building a token file before having three products' worth of screens. Start
  with the primitives; the semantics come from real usage.
- Creating a semantic token for a single exception. Exceptions are where a
  system goes to die.
- Refactoring tokens and re-skinning in the same change. One change, one
  reason, one revert.
- Assuming consistency means one palette across every product. The brand can
  differ per product; the *structure* (layers, naming, spacing scale) must not.
- Letting a component define a local constant that later needs to be shared.
  Move it to the token file at the second use, not the tenth.
