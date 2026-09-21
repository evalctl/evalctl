---
title: Tools for evaluating AI agents, compared
description: How evalctl relates to prompt evaluators, agent-eval platforms, benchmarks, and observability tools — and when to reach for each.
bucket: project
order: 2
draft: false
---

# Tools for evaluating AI agents, compared

Pick a tool by the question you need answered. This page sorts the common
evaluation tools by that question, then points you to a detailed comparison for
each one.

evalctl answers one question: **did this change break what my agent does to a
workspace?** It runs the agent, looks at the files it wrote, the diffs it made,
the commands it ran, and the exit codes, and it scores that. It runs from the
command line, needs no Docker and no account, and writes one results file you
can commit and another machine can re-check.

Other tools answer different questions. Here is how the field divides.

## What are you trying to measure?

| Your question | Reach for | Why |
| --- | --- | --- |
| Did my agent do the right thing to the files and the system? | **evalctl** | Scores the workspace the agent left behind, not the text a model returned. |
| Is a single prompt or model reply good? | a prompt evaluator (promptfoo) | The thing under test is one prompt and its completion. |
| Do I need reproducible, container-isolated runs across many tasks at scale? | a container-based harness (Harbor) | Gives each run its own machine and runs thousands in parallel. |
| How does my agent score across a team's shared datasets and dashboards? | an agent-eval platform (LangSmith, Braintrust) | Built for teams, tracing, and stored history, usually with a server or an account. |
| How good is a model on a public benchmark? | a benchmark suite (SWE-bench, lm-evaluation-harness) | Measures a model or agent on someone else's fixed tasks, not yours. |
| What is my agent doing in production, right now? | an observability tool (Langfuse, Phoenix) | Records and displays live traces; evaluation is a secondary feature. |

Most teams use more than one. evalctl is the fast local check you run first and
underneath the heavier tools, not a replacement for them.

## The three layers

Evaluation tools stack into three layers. Confusing one layer for another is the
most common mistake, so it helps to name them.

1. **Record what the agent did.** Trajectory formats such as
   [Letta's Trajectory](/docs/comparisons/evalctl-vs-letta-trajectory/) and
   [Harbor's ATIF](/docs/comparisons/evalctl-vs-harbor-atif/) write down an
   agent's steps in a standard shape. They don't run or score anything.
2. **Run the agent and score the result.** This is where evalctl sits. It drives
   the run and grades the outcome. A container-based harness like Harbor also
   lives here, scoring runs inside isolated sandboxes at scale.
3. **Manage evaluation across a team.** Platforms such as LangSmith and Braintrust
   store datasets, track history, and support many people over time.

evalctl's results file belongs to the middle layer. It records how a run was
scored, which is a different thing than a recording of the steps (layer one) or a
shared history (layer three).

## Prompt evaluation and agent evaluation are not the same

A prompt evaluator grades the **text a model returns**. An agent evaluator grades
**what an agent did** — the files, the diffs, the commands, the exit codes. If
your agent only produces a chat reply, a prompt evaluator fits. If it edits code,
runs commands, or changes a workspace, the workspace is the thing worth checking,
and that is agent evaluation.

evalctl is an agent evaluator. It also handles the plain-text case, but that is
not where it is different.

## Detailed comparisons

- [evalctl and Harbor](/docs/comparisons/evalctl-vs-harbor/) — a local check
  pointed at your own repo, next to a container-based harness for benchmark runs
  at scale.
- [evalctl and DeepEval](/docs/comparisons/evalctl-vs-deepeval/) — scoring what an
  agent did to a workspace, next to scoring the quality of an LLM or RAG
  application's outputs.
- [evalctl and OpenAI Evals](/docs/comparisons/evalctl-vs-openai-evals/) — a local
  check on your repo, next to grading a model's answers against a dataset.
- [evalctl and Inspect](/docs/comparisons/evalctl-vs-inspect/) — a single CLI the
  agent drives, next to a full framework for running models and agents across
  datasets.
- [evalctl and promptfoo](/docs/comparison/) — scoring what an agent did, next to
  scoring the text a model returned.
- [evalctl and pytest](/docs/comparisons/evalctl-vs-pytest/) — why a passing test
  suite doesn't tell you what the agent did to make it pass.
- [evalctl and LLM-as-judge](/docs/comparisons/evalctl-vs-llm-judge/) — when a
  fixed check beats asking another model, and when a judge still earns its place.
- [evalctl and GitHub Actions](/docs/comparisons/evalctl-vs-github-actions/) — how
  to turn an agent run into a pass/fail check with per-case detail in CI.
- [evalctl and LangSmith](/docs/comparisons/evalctl-vs-langsmith/) — a local check
  that leaves a committable file, next to a hosted platform for datasets, team
  history, and production traces.
- [evalctl and Braintrust](/docs/comparisons/evalctl-vs-braintrust/) — a local
  check pointed at your own repo, next to a hosted platform for dataset
  evaluations and comparable experiments.
- [evalctl and Langfuse](/docs/comparisons/evalctl-vs-langfuse/) — a local check
  that leaves a committable file, next to an open-source observability platform
  that traces an application and evaluates it.
- [evalctl and Arize Phoenix](/docs/comparisons/evalctl-vs-phoenix/) — a single
  local command, next to an observability server that traces a run step by step
  and scores its spans.
- [evalctl and W&B Weave](/docs/comparisons/evalctl-vs-weave/) — scoring what an
  agent did to a workspace, next to logging dataset evaluations and traces to the
  Weights & Biases platform.
- [evalctl and TruLens](/docs/comparisons/evalctl-vs-trulens/) — scoring what an
  agent did to a workspace, next to scoring an application's generated text with
  local feedback functions.
- [evalctl and SWE-bench](/docs/comparisons/evalctl-vs-swe-bench/) — a reusable
  gate on your own repo, next to a fixed public benchmark that scores agents on
  curated GitHub issues with their own tests.
- [evalctl and lm-evaluation-harness](/docs/comparisons/evalctl-vs-lm-eval-harness/)
  — scoring what an agent did to a workspace, next to measuring a model's
  capability on fixed academic benchmarks.
- [evalctl and Harbor ATIF](/docs/comparisons/evalctl-vs-harbor-atif/) — running
  and scoring an agent, next to a JSON format that records an agent's run.
- [evalctl and Letta Trajectory](/docs/comparisons/evalctl-vs-letta-trajectory/) —
  running and scoring an agent, next to a token-efficient format that records a
  run for another agent to read.
- [evalctl and Guardrails](/docs/comparisons/evalctl-vs-guardrails/) — an offline
  gate on what an agent did, next to Guardrails AI and NeMo Guardrails validating
  an LLM's inputs and outputs live in the request path.

## How to choose in one line

Use evalctl when you want a fast, repeatable local check that scores what your
agent did to a workspace and leaves a results file you can commit — with no
Docker, no server, and no account. When you need shared datasets, team history,
container isolation, or production tracing, add a platform on top. evalctl is
built to run underneath one, not instead of it.
