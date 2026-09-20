---
title: evalctl and Inspect
description: When to use evalctl, a single CLI the agent drives to check one change, and when to use Inspect, a full evaluation framework for running models and agents across datasets.
bucket: project
order: 6
draft: false
---

# evalctl and Inspect

Both tools evaluate AI systems, both run from the command line, and both can
score with plain checks or a model judge. So they overlap more than most pairs on
this site. The difference is size and shape: Inspect is a full evaluation
**framework**; evalctl is a single **CLI** the agent drives to check one change.
This page says which one fits your job.

**Research baseline.** Inspect `UKGovernmentBEIS/inspect_ai`, version 0.3.266,
main at commit [`ec4dfc6`](https://github.com/UKGovernmentBEIS/inspect_ai/tree/ec4dfc6953784dc45b79de3147530c89868c6e26)
(dated 2026-09-19, checked 2026-09-20), MIT licensed, from the UK AI Security
Institute. Every Inspect claim below links to that source.

## Which one to use

Use **evalctl** to check what your agent did to your own workspace, fast, with
nothing to author. It installs with `pip`, needs no framework, no dataset, no
model API key to run, and no container. The agent under test can run it itself.
Each run leaves one results file you can commit.

Use **Inspect** to build and run evaluations across a dataset of tasks: measure a
model or an agent on many samples, score each one, and view the results. Inspect
gives you a Python framework — datasets, solvers, scorers — plus over 200
pre-built evaluations and optional Docker sandboxing for tool-using tasks.

Inspect is the bigger, more general tool. evalctl is the small local gate you run
underneath one. This page doesn't argue for one instead of the other.

## What each one is

evalctl is a single command-line tool. You run it, it runs your agent, and it
grades the result against the workspace the agent left behind: the files it wrote,
the diffs it made, the commands it ran, the exit codes. It runs in place and warns
on every run that the runner isn't sandboxed.

Inspect is "a framework for large language model evaluations"
([README](https://github.com/UKGovernmentBEIS/inspect_ai/tree/ec4dfc6953784dc45b79de3147530c89868c6e26)).
You write an evaluation in Python as a task: a dataset of samples, a solver that
produces answers (a prompt, a chain, or a tool-using agent), and a scorer that
grades them. It includes "prompt engineering, tool usage, multi-turn dialog, and
model graded evaluations," and it can run each task inside a Docker sandbox
([README](https://github.com/UKGovernmentBEIS/inspect_ai/tree/ec4dfc6953784dc45b79de3147530c89868c6e26)).

## Side by side

| Area | evalctl | Inspect (0.3.266) |
| --- | --- | --- |
| Shape | One command-line tool. | A Python framework you write tasks in. |
| Unit under test | One agent run against your repo. | A dataset of samples, run against a model or agent. |
| Scale | One run, locally, as a gate. | Many samples per task, many tasks; built for capability and safety evals. |
| Isolation | Runs in place; warns it isn't sandboxed. | Optional Docker sandbox per task, plus local and remote sandboxes. |
| How it grades | Fixed checks over the workspace; an AI judge is optional. | Deterministic scorers (`match`, `includes`, `pattern`, `exact`) or model-graded (`model_graded_qa`, `model_graded_fact`). |
| Model API key to run | Not needed by default. | Needed; a task runs a model. |
| Ready-made evals | None; you point it at your repo. | Over 200 pre-built evaluations. |
| Who runs it | The agent under test, or one person. | A researcher or engineer writing and running tasks. |

## What Inspect does well

- **A full framework for eval research.** Datasets, solvers, and scorers give you
  a structured way to write and run evaluations, extendable by other Python
  packages ([README](https://github.com/UKGovernmentBEIS/inspect_ai/tree/ec4dfc6953784dc45b79de3147530c89868c6e26)).
  evalctl has no framework and doesn't try to.
- **Scale across a dataset.** Inspect is built to run a model or agent over many
  samples and many tasks — the shape of a capability or safety evaluation. evalctl
  scores one run at a time.
- **Sandboxed tool use.** Inspect can run a tool-using agent inside a Docker
  sandbox and score the result, isolation evalctl gives up on purpose.
- **A large library of ready-made evals.** Over 200 pre-built evaluations run on
  any model out of the box.

If your need is a framework for running models or agents across datasets, with
sandboxing and a library of benchmarks, that is Inspect's shape, not evalctl's.

## What evalctl does differently

- **A single CLI, not a framework.** You don't write a Python task, a dataset, or
  a scorer to use evalctl. You point it at the change your agent made and ask
  whether it still passes. Inspect asks you to build an eval; evalctl is the eval.
- **The agent runs it.** evalctl is built so the agent under test can call it,
  read the result, and continue. If it guesses a command wrong, the error tells it
  the right one to paste.
- **No model key, no container to run.** evalctl scores what your agent already
  did, so a run needs no model API key and no sandbox setup. An Inspect task runs
  a model and, for tool use, a Docker sandbox.
- **A local gate on your repo, not a dataset.** evalctl checks the one change in
  front of you. Inspect measures a model or agent across a dataset of samples. Same
  idea of scoring behavior, a different activity.
- **A results file another machine can re-check.** Hand the run directory to a
  second machine and it reproduces the report offline.

## What is the same (don't sell a false difference)

Two contrasts that sound tempting are false here:

- **Not "deterministic versus judge."** Inspect ships first-class deterministic
  scorers — `match`, `includes`, `pattern`, `exact` — alongside its model-graded
  ones. Both tools grade deterministically by default and treat the judge as
  optional.
- **Not "we score state, they score text."** An Inspect scorer can run commands
  inside the task's sandbox and check the environment, not only the model's text.

The real difference is size and shape: a framework you build evals in, versus a
single CLI the agent drives to check one change.

## Using them together

Run evalctl as the fast local check while you work: score the change your agent
just made, with no framework, no dataset, and no model spend. When you need to
measure a model or agent across a dataset — a capability eval, a safety eval, a
benchmark — that is Inspect's job. evalctl is the gate you run first and
underneath a framework like Inspect, not a replacement for it.

## Don't say

- Don't say Inspect only grades text. Its scorers can run commands in a sandbox
  and check the environment.
- Don't say Inspect leans on a model judge. It ships deterministic scorers
  (`match`, `includes`, `pattern`, `exact`) as first-class options.
- Don't say Inspect can't sandbox or can't do agents. It has a Docker sandbox and
  a tool-using agent model.
- Don't call evalctl more capable than Inspect. Inspect is the larger, more
  general framework; evalctl is a small local gate.
- Don't call evalctl a replacement for Inspect. They work at different sizes.

## See also

- [All tool comparisons](/docs/comparisons/) — the full set, and which tool fits which question.

## Sources

- Inspect repository, version 0.3.266, main at commit `ec4dfc6`, dated
  2026-09-19, checked 2026-09-20:
  [github.com/UKGovernmentBEIS/inspect_ai](https://github.com/UKGovernmentBEIS/inspect_ai/tree/ec4dfc6953784dc45b79de3147530c89868c6e26)
- Inspect scorers (deterministic and model-graded), from the package's scorer
  module in that commit.
- Inspect sandbox providers (Docker, local, remote), from the package's sandbox
  module in that commit.
- evalctl scope and sandboxing: [How evalctl compares](/docs/comparison/),
  [Security and sandboxing](/docs/security/)
