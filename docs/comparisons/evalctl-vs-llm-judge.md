---
title: evalctl and LLM-as-judge
description: When a deterministic check beats asking another model, when a judge still earns its place, and why evalctl grades with fixed checks first.
bucket: project
order: 8
draft: false
---

# evalctl and LLM-as-judge

A common way to grade an AI agent is to ask another model: "did this agent do a
good job?" That is LLM-as-judge. It is quick to set up and it fits questions that
have no single right answer. It is also the wrong tool for a question that has one.
This page shows where each approach fits and why evalctl grades with fixed checks
first.

## What a judge is good at, and where it slips

An LLM judge reads an output and scores it. For genuinely subjective questions —
is this answer clear, is this summary faithful, is this reply polite — that is
often the only practical way to grade at scale. Keep the judge for those.

Three things happen when you use a judge for a question that actually has a
verifiable answer:

- **It varies.** The same work can score differently on two runs. A grade you can't
  reproduce is a weak gate.
- **It costs.** Every graded case is a model call, in money and time.
- **It can be talked out of it.** The agent's own output steers the judge. If the
  agent claims it fixed the bug, a judge reading that claim may agree.

For "did the agent do the right thing to the workspace," none of that is
necessary. The answer is a fact you can check.

## What a fixed check gives you instead

Some questions have one correct answer, and you can check it directly:

- Did the test suite exit clean? Read the exit code.
- Did the diff touch the files it was supposed to, and none it wasn't? Read the
  diff.
- Did a file that was supposed to stay frozen stay frozen? Compare its hash.

A deterministic check answers these the same way every time, at no model cost, and
it can't be argued with. evalctl grades with these checks first: its built-in
scorers cover exact match, regular expressions, JSON comparison, and numeric
thresholds, and a command scorer runs your own code to produce a verdict
([scorers](/docs/command-scorers/)).

Here is the case that shows the difference. An agent is asked to fix a bug, with
the tests frozen. It cuts the corner and edits the test to expect the buggy value,
then reports success. A judge reading "I fixed it" and a green test run might pass
it. A fixed check that compares the test file's hash sees the test changed and
fails the run — no matter what the agent said. The verifiable question got a
verifiable answer.

## Deterministic first is not "no judge ever"

evalctl grades deterministically today. Model-based judging is not a shipped
feature — a roadmap item, not a built-in scorer
([scorers](/docs/command-scorers/)). If you need a model judgment now, you write a
command scorer: your own binary that calls a model and emits a verdict, which
evalctl records like any other scorer.

The rule isn't "never use a judge." It's:

- Grade the verifiable parts with fixed checks — files, diffs, exit codes.
- Reach for a judge only for the parts that are genuinely subjective.
- Never let a judge be the only gate on a pass.

A judge that grades open-ended quality on top of fixed checks is a fine design. A
judge that is the *sole* reason a run passed is a gate an agent can move.

## When to reach for each

| Your question | Reach for |
| --- | --- |
| Did the agent change the right files and pass the tests without gaming them? | A deterministic check (evalctl). |
| Did the exit code, diff, or a frozen file come out as required? | A deterministic check (evalctl). |
| Is this generated answer clear, well-written, or polite? | An LLM judge. |
| Is a free-form summary faithful to its source? | An LLM judge, alongside fixed checks. |

## Don't say

- Don't say LLM judges are useless. They grade subjective quality that fixed
  checks can't reach.
- Don't say evalctl ships an LLM judge. It doesn't; model-based judging is a
  command scorer you write, and a built-in is a roadmap item.
- Don't say a judge should never be used. Say it should never be the only gate.
- Don't say a deterministic check can grade open-ended quality. It grades
  verifiable facts.

## Sources

- evalctl scorers and command scorers (deterministic built-ins; judge as a
  command scorer; LLM-as-judge on the roadmap):
  [Scorers and command scorers](/docs/command-scorers/)
- evalctl scope and sandboxing: [How evalctl compares](/docs/comparison/),
  [Security and sandboxing](/docs/security/)
