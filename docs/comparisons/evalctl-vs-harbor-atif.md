---
title: evalctl and Harbor ATIF
description: When to use evalctl, a local command-line check that runs your agent and scores what it did to a workspace, and when to use Harbor's Agent Trajectory Interchange Format (ATIF), a JSON format for recording an agent's steps. Different layers, not rivals.
bucket: project
order: 19
draft: false
---

# evalctl and Harbor ATIF

These two do not compete. ATIF is a format for **recording** what an agent did,
step by step. evalctl **runs** an agent and **scores** the workspace it left
behind. One writes down the trajectory; the other grades an outcome. This page
names the two layers so you don't reach for one expecting the other.

**Research baseline.** ATIF is the Agent Trajectory Interchange Format, specified
in the Harbor repository `harbor-framework/harbor`, main at commit
[`71c77fd`](https://github.com/harbor-framework/harbor/tree/71c77fdd119df12eb6ab56e5bc0f29bf62fad338),
in
[`rfcs/0001-trajectory-format.md`](https://github.com/harbor-framework/harbor/blob/71c77fdd119df12eb6ab56e5bc0f29bf62fad338/rfcs/0001-trajectory-format.md)
(RFC v1.8, checked 2026-09-20). Every ATIF claim below links to that source.

## Which layer you need

Reach for **ATIF** when you need a standard way to write down an agent's run: the
messages, the reasoning, the tool calls, the observations. The RFC defines it as
"a standardized, JSON-based specification for logging the complete interaction
history of autonomous LLM agents," meant to make that data "immediately usable
across debugging, visualization, Supervised Fine-Tuning (SFT), and Reinforcement
Learning (RL) pipelines"
([RFC](https://github.com/harbor-framework/harbor/blob/71c77fdd119df12eb6ab56e5bc0f29bf62fad338/rfcs/0001-trajectory-format.md)).

Reach for **evalctl** when you need to run your agent and grade what it did to a
workspace: the files it wrote, the diffs it made, the commands it ran, the exit
codes. It installs with `pip`, needs no account, and leaves one results file you
can commit and another machine can re-check.

They answer different questions. ATIF asks, "how do I record this run in a
standard shape?" evalctl asks, "did this change break what my agent does here?"

## What each one is

ATIF is a data format, not a tool that runs or grades anything. It says how to
lay out a trajectory as JSON so that different producers and consumers agree on
the shape. It "will serve as the standardized data logging methodology for the
Harbor project"
([RFC](https://github.com/harbor-framework/harbor/blob/71c77fdd119df12eb6ab56e5bc0f29bf62fad338/rfcs/0001-trajectory-format.md)).
It carries per-step token metrics, structured tool payloads, and full replayable
detail. It does not execute an agent and it does not score one.

evalctl is a single command-line tool. You run it, it runs your agent, and it
grades the result against the workspace the agent left behind. It runs in place,
on your machine, and it warns on every run that the runner isn't sandboxed. Its
results file records **how a run was scored** — which checks passed, which
failed — not a blow-by-blow of every step the agent took.

## Side by side

| Area | evalctl | Harbor ATIF |
| --- | --- | --- |
| What it is | A tool that runs an agent and scores the result. | A JSON format for recording an agent's run. |
| Layer | Run and score (layer two). | Record what happened (layer one). |
| Does it run an agent? | Yes — it drives the run. | No — it is a file format. |
| Does it score anything? | Yes — against your checks. | No — it stores data; grading is a separate step. |
| What it produces | A results file: how the run was scored. | A trajectory: the full step-by-step record. |
| The question | Did this change break what my agent does here? | How do I log this run in a standard shape? |

Read this by column. ATIF is a way to write a run down. evalctl is a way to run
a check and grade it. Neither is a substitute for the other.

## What ATIF does well

- **A standard shape for trajectories.** ATIF unifies conversational logs,
  explicit action sequences, and replayable structures into one JSON spec, so
  data written by one producer is readable by many consumers
  ([RFC](https://github.com/harbor-framework/harbor/blob/71c77fdd119df12eb6ab56e5bc0f29bf62fad338/rfcs/0001-trajectory-format.md)).
  evalctl does not define an interchange format for full trajectories.
- **Full-fidelity detail for training.** It keeps per-step token metrics,
  structured tool payloads, and untruncated outputs — the detail SFT and RL
  pipelines need. evalctl's results file is a scoring record, not a training
  corpus.
- **Producer-agnostic.** Different agents and harnesses can emit ATIF, so
  downstream tools read one format. That interoperability is the point of a
  format.

If your need is a standard record of what an agent did, that is ATIF's shape, not
evalctl's.

## What evalctl does differently

- **It runs and scores; a format does neither.** evalctl drives the agent and
  grades the files, diffs, commands, and exit codes it produced. ATIF stores a
  run; it does not decide whether the run was good.
- **Pointed at your own repo.** You point evalctl at the change your agent made
  and ask whether it still passes. That is a day-to-day gate, not a logging spec.
- **A results file another machine can re-check.** Hand the run directory to a
  second machine and it reproduces the report offline. That file records the
  scoring, which is a different artifact than a trajectory of the steps.
- **The agent runs it.** evalctl is built so the agent under test can call it,
  read the result, and continue.

## What is the same (don't sell a false difference)

Both touch JSON and both care about what an agent did, so it is tempting to call
them competitors. They are not. The difference is not "deterministic versus
judge-based" — ATIF scores nothing at all. The difference is the layer: ATIF
**records** a run; evalctl **runs and grades** one. A results file that says how a
run was scored is a different artifact than a trajectory that records every step.

## Using them together

They stack. An agent run can be recorded as an ATIF trajectory for replay,
visualization, or training, and the same class of run can be gated by evalctl in
continuous integration. One preserves what happened; the other decides whether
today's change is good on your own code. Recording a run and grading a run are
complementary jobs, not a race.

## Don't say

- Don't call ATIF an evaluation tool. It is a logging format; it runs nothing and
  scores nothing.
- Don't say ATIF uses an AI judge. It stores trajectory data; grading is a
  separate concern outside the format.
- Don't call evalctl a trajectory format. It runs an agent and records how the
  run was scored, not a step-by-step interchange record.
- Don't say evalctl and ATIF compete. They sit at different layers — recording a
  run versus running and grading one.
- Don't say evalctl emits ATIF. Its results file is scoring provenance, not a
  full ATIF trajectory.

## See also

- [All tool comparisons](/docs/comparisons/) — the full set, and which tool fits which question.
- [evalctl and Harbor](/docs/comparisons/evalctl-vs-harbor/) — the local check next
  to Harbor's container-based benchmark harness.

## Sources

- Harbor ATIF specification (RFC v1.8: JSON trajectory format for logging agent
  interaction histories, for debugging/visualization/SFT/RL), `harbor-framework/harbor`
  main at commit `71c77fd`, 2026-09-20:
  [rfcs/0001-trajectory-format.md](https://github.com/harbor-framework/harbor/blob/71c77fdd119df12eb6ab56e5bc0f29bf62fad338/rfcs/0001-trajectory-format.md)
- evalctl scope: [How evalctl compares](/docs/comparison/),
  [Security and sandboxing](/docs/security/)
