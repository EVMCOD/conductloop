---
name: performance-budget
description: Use when a page is slower than the agreed bar, a bundle grew, or a release is about to ship, to measure Core Web Vitals and cut the biggest offender first against a written budget.
---

# Performance Budget

A budget is a number, a measurement method and a decision rule. If any of the
three is missing it is a wish, not a budget.

## The budget (defaults for a marketing site or a small SaaS dashboard)

| Metric | Budget | Blocked by |
|---|---|---|
| LCP (p75, field, mobile) | 2.5 s | 3.0 s |
| INP (p75, field) | 200 ms | 500 ms |
| CLS (p75, field) | 0.1 | 0.25 |
| TTFB (p75) | 600 ms | 800 ms |
| JS transferred, first load | 200 KB gzipped | 300 KB |
| Total transferred, first load | 1.0 MB | 1.6 MB |
| Images, first load | 250 KB | 400 KB |
| Web fonts | 2 families, 100 KB total | 4 families, 250 KB |
| Third-party scripts | 2 tags, 100 KB | 4 tags, 250 KB |
| Lighthouse mobile performance | 85 | 70 |

Field data (p75 from real users) beats lab data. Lab (Lighthouse) is a
screening tool: use it to catch a regression before release, and use field data
to decide whether users actually suffer.

## Measure before touching code (10 minutes)

1. **Reproduce on the slowest realistic device.** A mid-range Android from
   2022 or an iPhone 12 on throttled 4G. A developer's laptop is not a user.
2. **Field first.** Read LCP/INP/CLS p75 per URL from your analytics or the
   Web Vitals report. Note which URL is worst — it is almost never the home
   page, it is the page with the video, the table, or the third-party script.
3. **Lab second.** Run Lighthouse (mobile, not desktop) and read the
   diagnostics: what is the LCP element, what blocked the main thread, how
   much did each third party cost.
4. **Split the JS.** Build with a bundle analyzer. Record the top 10 modules by
   gzip size. One dependency is usually 30% of the bundle.
5. **Write the current numbers down** before you change anything. Without a
   baseline, "faster" is a feeling.

## Cut in this order (highest ratio of milliseconds removed to hours spent)

1. **Third-party scripts.** Every tag is measured: bytes, main-thread time,
   requests. If a script is not tied to a revenue or a support action, remove
   it. Load the survivors with `async` / `defer`, or after first paint.
2. **Images.** Serve modern formats, size them to the rendered box, use
   responsive `srcset`, set `width`/`height` to avoid layout shift, lazy-load
   everything below the fold, and never lazy-load the LCP image.
3. **Fonts.** Two families maximum, subset to the characters actually used,
   `font-display: swap` (or `optional` for non-critical), preload only the
   one font used above the fold. A 300 KB font family is the most common
   avoidable 300 KB.
4. **Render-blocking CSS.** Inline the critical CSS for the first screen, defer
   the rest. Split by route so a page never ships the styles of a page it does
   not show.
5. **JavaScript weight.** Route-level code splitting, dynamic import for
   heavy widgets (charts, editors, maps), `next/dynamic` for below-the-fold
   islands. A dashboard that ships its charting library on the login page is
   losing a fight it does not need to fight.
6. **Server and edge.** Cache the HTML where the content is stable, stream it
   where it is not, and check the TTFB before blaming the browser.

## Decision rules

- **Over budget but under the blocker:** ship with the top 2 cuts and a
  tracked ticket. Do not hold a release for a 200 ms regression.
- **Over the blocker:** the release is blocked until the LCP image or the
  heaviest third party is fixed. Those two account for most of the gap.
- **Budget reached:** stop. Extra performance work has no business value at
  that point; move the effort to a feature.
- Never trade correctness for speed: a spinner that hides a slow API is a
  design decision, not a performance fix.

## What a good answer looks like

A one-page memo, under 15 lines:

```
URL: <path>            p75 field: LCP 3.1s  INP 180ms  CLS 0.04
Lab (mobile throttled): LCP 4.2s  TBT 640ms  JS 412 KB gz
Top 3 costs: hero-image 180KB | analytics tag 120KB/300ms | chart lib 210KB
Cuts applied: <what> -> LCP 3.1s -> 2.4s
Verdict: ship | hold
```

Numbers with the URL and the device attached. No claim of improvement without
the before and after.

## Traps

- Measuring the home page while the slow page is `/pricing`.
- Comparing desktop lab numbers against mobile field numbers.
- Fixing CLS by removing the animation that users liked.
- Treating a Lighthouse 90 as "done": a perfect score on a marketing page can
  still have a 4 s field LCP on real hardware.
- Adding a performance library instead of deleting a dependency.
