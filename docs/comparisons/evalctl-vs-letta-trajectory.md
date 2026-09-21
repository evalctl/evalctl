---
title: evalctl and Letta Trajectory
description: When to use evalctl, a local command-line check that runs your agent and scores what it did to a workspace, and when to use Letta's Trajectory, a token-efficient format for recording an agent's run so another agent can read it. Different layers, not rivals.
bucket: project
order: 19
draft: false
---

# evalctl and Letta Trajectory

These two do not compete. Trajectory is a format for **recording** what an agent
did, packed small so another agent can read it back. evalctl **runs** an agent and
**scores** the workspace it left behind. One writes the run down; the other grades
an outcome. This page names the two layers so you don't reach for one expecting
the other.

**Research baseline.** Trajectory is the `@letta-ai/trajectory` package; the
repository is `letta-ai/trajectory`, main at commit
[`21ae92d`](https://github.com/letta-ai/trajectory/tree/21ae92d10579499b183d93929c85da0b91233167),
Apache-2.0 (checked 2026-09-20). Claims below link to that source and to Letta's
[announcement](https://www.letta.com/blog/trajectory).

## Which layer you need

Reach for **Trajectory** when you need to record an agent's run in a compact,
agent-readable shape. It "formats trajectories across harnesses in a consistent,
agent-friendly format" ([Letta](https://www.letta.com/blog/trajectory)), keeping
only the detail an agent needs to understand what happened and dropping harness
bookkeeping. On sampled sessions it reaches about a 5.6x token reduction versus
native Claude Code sessions. It is built to be consumed by an agent — for memory
formation, review, or search — not by a scorer.

Reach for **evalctl** when you need to run your agent and grade what it did to a
workspace: the files it wrote, the diffs it made, the commands it ran, the exit
codes. It installs with `pip`, needs no account, and leaves one results file you
can commit and another machine can re-check.

They answer different questions. Trajectory asks, "how do I record this run so an
agent can read it cheaply?" evalctl asks, "did this change break what my agent
does here?"

## What each one is

Trajectory is a data format, not a tool that runs or grades anything. It
normalizes sessions from different harnesses into one list of records —
assistant messages, user messages, reasoning, tool calls, tool results — sized
for token-efficient consumption. Letta draws the line itself: full-fidelity
formats "are intended for full-fidelity replay and benchmarking," while
Trajectory instead prioritizes token efficiency for agent consumption
([Letta](https://www.letta.com/blog/trajectory)). It does not run an agent and it
does not score one.

evalctl is a single command-line tool. You run it, it runs your agent, and it
grades the result against the workspace the agent left behind. It runs in place,
on your machine, and warns on every run that the runner isn't sandboxed. Its
results file records **how a run was scored**, not a compressed transcript of the
steps.

## Side by side

| Area | evalctl | Letta Trajectory |
| --- | --- | --- |
| What it is | A tool that runs an agent and scores the result. | A format for recording an agent's run, compactly. |
| Layer | Run and score (layer two). | Record what happened (layer one). |
| Does it run an agent? | Yes — it drives the run. | No — it is a file format. |
| Does it score anything? | Yes — against your checks. | No — it stores data for an agent to read. |
| Optimized for | A committable, re-checkable scoring record. | Token-efficient consumption by another agent. |
| The question | Did this change break what my agent does here? | How do I record this run so an agent reads it cheaply? |

Read this by column. Trajectory is a way to write a run down small. evalctl is a
way to run a check and grade it. Neither replaces the other.

## What Letta Trajectory does well

- **Token-efficient records.** It keeps only what an agent needs to understand a
  session and drops per-line envelopes, duplicated payloads, and UI event
  streams, reaching about a 5.6x token cut on sampled sessions
  ([Letta](https://www.letta.com/blog/trajectory)). evalctl does not produce
  agent-readable transcripts.
- **One shape across harnesses.** It converts sessions from different harnesses
  into a single format, so a consuming agent reads one thing
  ([repository](https://github.com/letta-ai/trajectory/tree/21ae92d10579499b183d93929c85da0b91233167)).
- **Built for agent memory and search.** The format is designed to feed memory
  formation and retrieval, not to gate a build. That is a different job than
  evalctl's.

If your need is a compact, agent-readable record of what happened, that is
Trajectory's shape, not evalctl's.

## What evalctl does differently

- **It runs and scores; a format does neither.** evalctl drives the agent and
  grades the files, diffs, commands, and exit codes it produced. Trajectory
  records a run; it does not decide whether the run was good.
- **Pointed at your own repo.** You point evalctl at the change your agent made
  and ask whether it still passes — a day-to-day gate, not a transcript format.
- **A results file another machine can re-check.** The run directory reproduces
  the report offline. That scoring record is a different artifact than a
  token-efficient transcript.
- **The agent runs it.** evalctl is built so the agent under test can call it,
  read the result, and continue.

## What is the same (don't sell a false difference)

Both are agent-native and both concern what an agent did, so it is tempting to
call them competitors. They are not. The difference is not "deterministic versus
judge-based" — Trajectory scores nothing at all. The difference is the layer:
Trajectory **records** a run for an agent to read; evalctl **runs and grades** one.
A scoring record and a token-efficient transcript are different artifacts.

## Using them together

They stack. An agent can read a Trajectory of a past run to form memory or decide
what to do next, and evalctl can gate what that agent then does to your repo. One
compresses what happened for the next agent; the other decides whether today's
change is good. Recording a run and grading a run are complementary, not a race.

## Don't say

- Don't call Trajectory an evaluation framework. Letta says it is a format for
  agent consumption, not full-fidelity replay or benchmarking.
- Don't say Trajectory scores or judges a run. It stores records; it grades
  nothing.
- Don't call evalctl a trajectory format. It runs an agent and records how the
  run was scored, not a compact transcript.
- Don't say evalctl and Trajectory compete. They sit at different layers —
  recording a run versus running and grading one.
- Don't say evalctl emits Trajectory. Its results file is scoring provenance, not
  an agent-readable transcript.

## See also

- [All tool comparisons](/docs/comparisons/) — the full set, and which tool fits which question.
- [evalctl and Harbor ATIF](/docs/comparisons/evalctl-vs-harbor-atif/) — the other
  trajectory format that records a run rather than scoring it.

## Sources

- Letta Trajectory (Apache-2.0 format for recording agent runs, token-efficient
  for agent consumption, ~5.6x token reduction, explicitly not replay/benchmark),
  `letta-ai/trajectory` main at commit `21ae92d`, 2026-09-20:
  [github.com/letta-ai/trajectory](https://github.com/letta-ai/trajectory/tree/21ae92d10579499b183d93929c85da0b91233167),
  [announcement](https://www.letta.com/blog/trajectory)
- evalctl scope: [How evalctl compares](/docs/comparison/),
  [Security and sandboxing](/docs/security/)
