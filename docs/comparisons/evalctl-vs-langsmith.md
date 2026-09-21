---
title: evalctl and LangSmith
description: When to use evalctl, a local command-line check that scores what your agent did, and when to use LangSmith, a hosted platform for tracing, datasets, and running evaluations across a team.
bucket: project
order: 10
draft: false
---

# evalctl and LangSmith

Both tools evaluate AI work, and both let you score a run against fixed checks or
an AI judge. So people ask which one to pick. They sit at different layers.
LangSmith is a platform a team logs into; evalctl is a single command you run on
your own machine. This page says which job each one fits.

**Research baseline.** LangSmith is a hosted platform from LangChain; its docs are
at [docs.langchain.com/langsmith](https://docs.langchain.com/langsmith). The open
SDK is `langchain-ai/langsmith-sdk`, main at commit
[`55d31d2`](https://github.com/langchain-ai/langsmith-sdk/tree/55d31d21d8417d55b705941cb0205fc1f6487858)
(checked 2026-09-20). Every LangSmith claim below links to that source.

## Which one to use

Use **evalctl** to check what your agent did to your own workspace, fast, with
nothing to set up. It installs with `pip`, needs no account, no server, and no
model API key to run. The agent under test can run it itself. Each run leaves one
results file you can commit, and another machine can re-check it offline.

Use **LangSmith** to store datasets, track results over time, and share
evaluations across a team. It records traces from your deployed application,
keeps a history of experiments, and shows them in a dashboard many people can
open ([evaluation concepts](https://docs.langchain.com/langsmith/evaluation-concepts)).

They fit together. evalctl is the quick local gate you run first, on your laptop
or in continuous integration. LangSmith is the shared record you keep on top, so a
team can compare versions and watch production over time. This page doesn't argue
for one instead of the other.

## What each one is

evalctl is a single command-line tool. You run it, it runs your agent, and it
grades the result against the workspace the agent left behind: the files it wrote,
the diffs it made, the commands it ran, the exit codes. It runs in place, on your
machine, and it warns on every run that the runner isn't sandboxed.

LangSmith is a hosted platform. You send it traces and datasets through its SDK or
UI, and it stores them for a workspace of people. In LangSmith, "a dataset is a
collection of examples," each example "a test input, reference output pair," and
an experiment holds "outputs, evaluator scores, and execution traces"
([evaluation concepts](https://docs.langchain.com/langsmith/evaluation-concepts)).
It grades with four kinds of evaluator: human annotation, code-based rules,
LLM-as-judge, and pairwise comparison.

LangSmith can run in your own infrastructure, but only on the Enterprise plan,
against a Kubernetes cluster and a contract with LangChain
([self-hosted docs](https://docs.langchain.com/langsmith/self-hosted)). It is not
open source, and there is no free local mode.

## Side by side

| Area | evalctl | LangSmith |
| --- | --- | --- |
| Built for | Checking what your agent did to your own workspace. | Storing datasets, tracking experiments, and tracing across a team. |
| Who runs it | The agent under test, or one person, with no setup. | A team, through the SDK and a shared dashboard. |
| Unit under test | The workspace: files, diffs, commands, exit codes. | Application outputs against reference outputs in a dataset. |
| Where results live | One portable file on your machine, ready to commit. | The LangSmith workspace, viewed in its dashboard. |
| Setup to run | `pip install evalctl`. No account, no server, no model key. | An account and API key; a server if you self-host on Enterprise. |
| How it grades | Fixed checks over the workspace; an AI judge is optional and never the only check. | Human, code-based, LLM-as-judge, or pairwise evaluators. |
| Team history | None. Each run is a standalone file. | Kept over time: datasets, experiments, production traces. |
| Cost model | Free tool; a run costs no model calls by default. | Free and paid tiers; self-hosting is Enterprise-only. |

Read this by column, not row by row. evalctl is the whole left column: small,
local, no account, pointed at your own repo, one file per run. LangSmith is the
whole right column: a hosted service that keeps datasets, history, and traces for
a team.

## What LangSmith does well

- **Shared history over time.** An experiment is "a permanent record" of how a
  version scored on a dataset, so a team can compare versions and watch a metric
  move ([evaluation concepts](https://docs.langchain.com/langsmith/evaluation-concepts)).
  evalctl keeps no history; each run is one file.
- **Production tracing.** LangSmith records live runs from a deployed application
  and groups multi-turn conversations into threads
  ([evaluation concepts](https://docs.langchain.com/langsmith/evaluation-concepts)).
  evalctl scores a run you point it at; it does not watch production.
- **Datasets and a UI for a team.** Curated example sets, human annotation, and a
  dashboard many people can open are the platform's core. That is team workflow,
  and it is not evalctl's shape.

If your need is stored datasets, shared history, and production traces for a team,
that is LangSmith's shape, not evalctl's.

## What evalctl does differently

- **No account, no server, no model key to run.** Install it and run it in a
  continuous integration job in seconds, with nothing to log into. evalctl scores
  what your agent already did, so a run costs no model calls by default.
- **Pointed at your own workspace.** You don't curate a dataset of input and
  reference-output pairs. You point evalctl at the change your agent made and ask
  whether it still passes. That is a local gate on your work, not a stored
  experiment.
- **It scores the workspace, not the reply.** evalctl grades the files, diffs,
  commands, and exit codes an agent produced. LangSmith centers on application
  outputs compared to reference outputs. The unit under test is different.
- **The agent runs it.** evalctl is built so the agent under test can call it,
  read the result, and continue. If it guesses a command wrong, the error tells it
  the right one to paste.
- **A results file another machine can re-check.** Hand the run directory to a
  second machine and it reproduces the report offline, with no account to sign
  into. The file records what went in, the checks that ran, and what they found.

## What is the same (don't sell a false difference)

Both tools can grade with plain, repeatable checks, and both offer an AI judge as
one option. LangSmith lists "code-based" evaluators alongside LLM-as-judge ones
([evaluation concepts](https://docs.langchain.com/langsmith/evaluation-concepts)),
so "deterministic versus judge-based" is **not** the real difference between them.
The real differences are the unit under test, where the results live, and whether
you need an account and a server at all.

## Using them together

Run evalctl as the quick local check while you work: score a change on a laptop or
in continuous integration, with no account and no model spend, and commit the
results file next to the code. When you want shared datasets, a history of
experiments, or production traces across a team, log those into LangSmith on top.
The evalctl file records how one run was scored on your own repo — a different
artifact than a LangSmith experiment stored in a workspace.

## Don't say

- Don't call LangSmith open source or free to self-host. Self-hosting is
  Enterprise-only, on a Kubernetes cluster, under a contract.
- Don't say LangSmith only uses LLM judges. It offers human, code-based,
  LLM-as-judge, and pairwise evaluators.
- Don't call evalctl a replacement for LangSmith. evalctl keeps no shared history,
  no datasets, and no production tracing on purpose.
- Don't say evalctl scores model outputs the way LangSmith does. evalctl scores the
  workspace an agent changed — files, diffs, commands, exit codes.
- Don't claim evalctl needs an account or a server. It runs from the command line
  with nothing to log into.

## See also

- [All tool comparisons](/docs/comparisons/) — the full set, and which tool fits which question.

## Sources

- LangSmith evaluation concepts (datasets, experiments, evaluators, traces):
  [docs.langchain.com/langsmith/evaluation-concepts](https://docs.langchain.com/langsmith/evaluation-concepts)
- LangSmith self-hosted (Enterprise, Kubernetes):
  [docs.langchain.com/langsmith/self-hosted](https://docs.langchain.com/langsmith/self-hosted)
- LangSmith SDK, main at commit `55d31d2`, 2026-09-20:
  [github.com/langchain-ai/langsmith-sdk](https://github.com/langchain-ai/langsmith-sdk/tree/55d31d21d8417d55b705941cb0205fc1f6487858)
- evalctl scope and sandboxing: [How evalctl compares](/docs/comparison/),
  [Security and sandboxing](/docs/security/)
