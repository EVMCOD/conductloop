---
name: "code-review-rigor"
description: "Use when reviewing a diff, pull request or generated change before it ships, to find the defects that matter and skip the ones that do not."
---

# Code Review Rigor

A review that finds the things that break, and stays quiet about the rest.
Ends with a verdict: APPROVE, APPROVE WITH NITS, or REQUEST CHANGES, plus the
list of blocking findings.

## When to use

- Before merging any change that touches money, auth, data, or migrations.
- Before a release, on the whole diff since the last release.
- When an AI-generated change needs a human check before it lands.
- When a change is blamed for an incident and nobody is sure why.

Not for: reviewing your own first draft in the same session you wrote it (read
it fresh tomorrow instead), or style preferences.

## Step 1 - Read for the contract first, not the diff

Before reading any line, answer in one sentence: what is this change supposed
to guarantee? Write it at the top of the review, and judge every finding
against it. A change with no stated contract cannot be reviewed; ask for it.

Then read in this order, not top to bottom: the public surface (what a caller
can now do that they could not before), the data (what is written, deleted,
migrated), the failure paths (what happens when the network, the disk, or a
third-party API fails), and only then the implementation. Reading top to
bottom finds typos; this order finds outages.

## Step 2 - The blocking checklist

Each item is blocking if the answer is bad. Do the list before reading the
code closely: a change that fails one of these is not worth polishing.

- Money: does any float touch a currency amount? Floats lose pennies, so
  amounts must be integers of minor units or Decimal.
- Auth: is every new entry point behind the same check as its neighbours? A
  new endpoint without an auth check is a blocking finding.
- Data loss: can a write be half-done? Is there a transaction, or is the write
  idempotent?
- Migrations: is every migration reversible, and does the code work with the
  old schema in a rolling deploy?
- Secrets: are credentials read from the environment, never committed, and
  never logged?
- Input: is every external input validated at the boundary, and is a failed
  validation a 4xx and not a 500?
- Concurrency: can two writers touch the same row, and if so, what happens?
- Blast radius: does a failure affect one user, one product, or every product
  the founder runs?

## Step 3 - Separate blocking from noise

A finding is blocking only if it can cause one of these: data loss or
corruption; a wrong number charged, paid or reported; a security hole or a
credential leak; a crash or hang in a path users hit; a migration that cannot
be undone; or a silent wrong answer, where the code runs and is wrong.

Everything else is a nit or a suggestion. Mark it as such, and cap nits at
three: a review with 15 nits gets ignored, and the blocking finding inside it
gets missed. Never block on naming, formatting, file layout, "this could be
more idiomatic", or a preference the codebase does not enforce.

## Step 4 - Write the findings so they can be acted on

Each finding gets the file and line, one sentence on what breaks, and the
concrete fix:

> `path/file.py:42` - [blocking] The retry loop re-sends the charge without
> an idempotency key, so a timeout after the provider accepted it charges
> the customer twice. Fix: generate a key per charge attempt and reuse it
> on retry.

Rules for phrasing: say what breaks, not what is ugly ("this is hard to read"
is not a finding); give the fix when you know it, and say what you need to
know when you do not; put the blocking finding before the suggestion; and say
"blocking" explicitly, because if the author cannot tell what blocks the
merge, they will merge it.

## Step 5 - Verify what you claim

Before sending a blocking finding, try to break the claim:

- Trace the failure path by hand, or write the 5-line test that reproduces it.
  If you cannot, it is a question, not a blocker.
- Check whether the codebase already handles the case elsewhere (a middleware,
  a DB constraint). A blocker it already prevents is a false positive.
- Check the test suite: if it covers this path, it is usually fine.

Say "I could not reproduce this, but here is the sequence that worries me"
when that is the case. It is more useful than a confident blocker.

## What a good answer looks like

A review with the contract of the change in one sentence at the top; the
verdict on the first line (APPROVE, APPROVE WITH NITS, or REQUEST CHANGES);
blocking findings ordered by severity, each with file:line, what breaks and
the fix; a "worth knowing" section for the nits, max three; and a note on what
you could not verify and why.

Time box: 30 minutes for a normal diff, 90 for a risky one. Beyond that the
change is too big to review properly: split it first, or ship it behind a
flag.

## Common traps

- Reading top to bottom and finding only typos.
- Blocking on style, which trains the author to ignore the review.
- Reporting 15 findings of which 3 are real.
- Asserting a bug without tracing it, and being wrong in public.
- Skipping the money and auth paths because the diff "looks small".
- Approving with a vague "looks good", which teaches the author nothing.