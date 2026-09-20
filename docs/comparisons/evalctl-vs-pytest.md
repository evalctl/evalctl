---
title: evalctl and pytest
description: Why a passing test suite doesn't tell you whether your agent did the right thing, and where evalctl fits around the tests you already have.
bucket: project
order: 7
draft: false
---

# evalctl and pytest

You already have pytest. So a fair question is: to check an AI agent's work, why
not just run the tests? The short answer is that pytest checks whether the code is
correct. It doesn't check what the agent *did* to get there — and an agent can make
the tests pass without doing the right thing. evalctl grades that. This page shows
where each one fits.

## What pytest tells you, and what it doesn't

pytest runs assertions against code and reports pass or fail. That is exactly what
you want for deterministic code you wrote. Point it at an agent's work, though, and
it answers a narrower question than it looks like it answers.

A green test suite tells you the tests passed. It does not tell you *how* they
passed. An agent asked to fix a bug can:

- edit the code and fix the bug — the outcome you wanted; or
- edit the *test* to expect the buggy result, so the suite goes green without the
  bug being fixed.

pytest can't tell these apart. Both end in "tests passed." The exit code is the
same. If the only thing grading the agent is the test suite the agent can edit,
the agent can pass by changing the check.

## What evalctl adds

evalctl runs the agent's work and grades the **workspace it left behind**: the
files it wrote, the diff it produced, the commands it ran, and the exit codes —
text too, when that matters. Your test suite is one input to that grade, not the
whole grade.

A worked example ships with evalctl. An agent is asked to fix a real bug (a
`median()` that is wrong for even-length inputs), with the tests frozen. On its
first pass the agent cuts the corner: it edits the frozen test to expect the buggy
value, and the suite goes green. evalctl still fails the run. A scorer that checks
the test file's hash sees the test was changed and reports it, even though the exit
code passed. The agent reads that verdict, reverts the test, fixes the real bug,
and re-runs until every required check is green.

pytest alone passes that first attempt. evalctl catches it, because it grades what
the agent changed, not only whether the tests ended green.

## They work together, not instead

evalctl does not replace pytest. It usually *runs* it. In evalctl, a suite has a
runner — a command that executes the agent's work — and scorers that grade the
result. A common setup makes the runner your existing test command, so the test
suite's pass or fail becomes one required scorer, and workspace checks (did the
diff touch the right files, did a frozen file stay unchanged) sit alongside it
([scorers](/docs/command-scorers/)).

So the tests you already have keep doing their job. evalctl wraps the agent run
around them, adds the checks pytest can't make, and captures the whole run as a
directory another machine can re-score offline.

## When to reach for each

| Your situation | Reach for |
| --- | --- |
| Check that a function returns the right value. | pytest. |
| Check that an agent fixed the bug without editing the test to fake it. | evalctl (running pytest as one of its checks). |
| Grade a diff, a created or forbidden file, or an exit code as a pass condition. | evalctl. |
| Keep a repeatable, committable record of one agent run. | evalctl. |
| Unit-test deterministic code you wrote. | pytest. |

## Don't say

- Don't say pytest can't test AI work. It tests deterministic outputs well; it
  just can't see what the agent did to make the tests pass.
- Don't say evalctl replaces pytest. evalctl runs your test suite and grades
  around it.
- Don't say evalctl uses an AI to judge the code. Its scorers are deterministic;
  model-based judging is not a shipped feature.

## Sources

- evalctl scorers and command scorers (deterministic checks; running your suite as
  a scorer): [Scorers and command scorers](/docs/command-scorers/)
- evalctl quickstart (runner and suite shape): [Quickstart](/docs/quickstart/)
- evalctl scope and sandboxing: [How evalctl compares](/docs/comparison/),
  [Security and sandboxing](/docs/security/)
