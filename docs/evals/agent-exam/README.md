# The agent exam

Two real tasks, on this repository, with the model the person configured. The
exam answers one question: **does the runner do the work?**

On 16-nov-2026 the built-in session runner (`ops_dashboard/sessions/runner.py`)
had 60 turns of a real task, 25 reads, 23 searches, 7 runs (4 refused) and 2
edits (1 failed). It burned **657,426 input tokens for 2,206 output tokens and
changed nothing**. The same model (MiniMax-M3) solved the same task in 15
minutes inside Claude Code. The model was fine; the runner was not.

Measuring first, changing second. This directory holds the exam:

```
docs/evals/agent-exam/
  README.md
  A-url-alerts-readme/     task.md, base, hidden_tests/
  B-session-cap/           task.md, base, hidden_tests/
```

* `task.md` — the prompt the agent gets, word for word.
* `base` — the commit the task starts from.
* `hidden_tests/` — the tests that decide whether the task was done. They are
  never given to the agent; the exam applies them to its worktree afterwards.

## Running it

```bash
scripts/agent_exam.py --runner builtin --runner claude
scripts/agent_exam.py --runner builtin --task A --out /tmp/exam.json
scripts/agent_exam.py --dry-run          # the plumbing, with a scripted model
```

For every run it:

1. clones the repository into a temporary folder, **without remotes**, at the
   base commit of the task;
2. opens a code session on that clone, with a **temporary database** — the real
   `ops.db` is never opened — and with the session's own provider (MiniMax-M3
   through the Coding Plan by default);
3. runs the task with the runner under test;
4. applies the hidden tests to the agent's worktree and runs them.

and prints the table:

| runner | task | finished | hidden tests | turns | tokens in | tokens out | min |
|---|---|---|---|---|---|---|---|
| builtin | A | yes | yes | 42 | 118,204 | 9,881 | 11.3 |

**It spends the owner's Coding Plan quota** (a flat rate: no money per token,
but the plan's limits are real). It is a bench, not a test: it never runs in
CI, and it is never run by `scripts/verify.sh`.

## The bar

With MiniMax-M3, the built-in runner finishes A and B with the hidden tests in
green, in **fewer than 80 turns** and with **fewer than 150k input tokens**
(counted without the cache) per task, **twice in a row**.

The `claude` runner is the reference to measure against, not the goal: it is
the safety net until the built-in one is good enough, and the yardstick for
what "good enough" means.
