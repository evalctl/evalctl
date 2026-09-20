---
title: evalctl and OpenAI Evals
description: When to use evalctl to check what your agent did to a workspace, and when to use OpenAI Evals to grade a model's answers against a dataset.
bucket: project
order: 5
draft: true
---

# evalctl and OpenAI Evals

Both tools evaluate AI systems from the command line, so they get compared. They
test different things. OpenAI Evals grades a model's **answer** against reference
answers in a dataset. evalctl grades what an agent **did** to a workspace. This
page says which one fits your question.

**Research baseline.** OpenAI Evals `openai/evals`, main at commit
[`8eac7a7`](https://github.com/openai/evals/tree/8eac7a7de5215c907fbddc30efdaf316913eccdd)
(dated 2026-04-14, checked 2026-09-20), MIT licensed. Every OpenAI Evals claim
below links to that source.

## Which one to use

Use **evalctl** to check what your agent did to your own workspace: the files it
wrote, the diffs it made, the commands it ran, the exit codes. It installs with
`pip`, needs no model API key to run, and the agent under test can run it itself.
Each run leaves one results file you can commit.

Use **OpenAI Evals** to measure how well a model answers a dataset of prompts.
You pick or write an eval, point it at a model, and it scores the model's
completions against reference answers or a grading rubric. It ships a registry of
ready-made evals, many of them standard academic benchmarks.

They test different units. OpenAI Evals asks "is this model's answer right?"
evalctl asks "did this agent change the workspace correctly?"

## What each one is

evalctl is a single command-line tool. You run it, it runs your agent, and it
grades the result against the workspace the agent left behind. It runs in place
and warns on every run that the runner isn't sandboxed.

OpenAI Evals is "a framework for evaluating large language models (LLMs) or
systems built using LLMs"
([README](https://github.com/openai/evals/tree/8eac7a7de5215c907fbddc30efdaf316913eccdd)).
You define an eval — usually a YAML file plus a dataset — and run it against a
model. The thing scored is the model's completion `a` against a reference list of
correct answers `B`
([eval templates](https://github.com/openai/evals/blob/8eac7a7de5215c907fbddc30efdaf316913eccdd/docs/eval-templates.md)).
Running an eval needs an `OPENAI_API_KEY`.

One fact worth knowing before you invest: the open-source repo is quiet — its last
commit is dated 2026-04-14 — and its README now points you to configure and run
evals in the hosted OpenAI Dashboard instead
([README](https://github.com/openai/evals/tree/8eac7a7de5215c907fbddc30efdaf316913eccdd)).
The open framework still runs, but active development has moved to the platform.

## Side by side

| Area | evalctl | OpenAI Evals (`8eac7a7`) |
| --- | --- | --- |
| What it scores | The workspace the agent left: files, diffs, commands, exit codes, and text. | A model's completion against reference answers in a dataset. |
| Unit under test | One agent run against your repo. | One model, many prompts, from a dataset. |
| How it grades | Fixed checks over the workspace; an AI judge is optional. | Deterministic templates (`Match`, `Includes`, `FuzzyMatch`, `JsonMatch`) or model-graded (`ModelBasedClassify`). |
| Model API key to run | Not needed by default. | Needed; runs prompts against a model. |
| Shape | One command-line tool. | A framework plus a registry of datasets (stored with Git-LFS). |
| Authoring | Point it at your repo. | Write a YAML eval and supply a dataset, or reuse a registry eval. |
| Project status | Active. | Open repo quiet since 2026-04-14; development moved to the OpenAI Dashboard. |

## What OpenAI Evals does well

- **A registry of ready-made benchmarks.** Many standard academic evals are
  already written, so you can measure a model without building the dataset
  yourself ([eval templates](https://github.com/openai/evals/blob/8eac7a7de5215c907fbddc30efdaf316913eccdd/docs/eval-templates.md)).
  evalctl ships no such registry.
- **Grading open-ended answers.** Its model-graded template
  (`ModelBasedClassify`) uses a model to judge a free-form answer against a
  rubric — useful when there is no single correct string
  ([eval templates](https://github.com/openai/evals/blob/8eac7a7de5215c907fbddc30efdaf316913eccdd/docs/eval-templates.md)).
- **Comparing models on the same task.** Its shape — one dataset, many model runs —
  is built to ask which model answers a benchmark better. evalctl is not.

If your question is how well a model answers a set of prompts, that is OpenAI
Evals' shape, not evalctl's.

## What evalctl does differently

- **It grades the workspace, not a completion.** evalctl checks the files, diffs,
  commands, and exit codes your agent produced. OpenAI Evals grades the text a
  model returned against reference answers. Even its tool-using "completion
  function" path still scores the completion, not the files the agent changed.
- **No model API key to run.** evalctl scores what your agent already did, so a
  run costs no model calls by default. OpenAI Evals runs prompts against a model
  and needs an `OPENAI_API_KEY`.
- **No dataset to author.** You point evalctl at the change your agent just made
  and ask whether it still passes. OpenAI Evals expects a dataset of prompts and
  reference answers, stored in its registry.
- **A results file another machine can re-check.** Hand the run directory to a
  second machine and it reproduces the report offline.

## What is the same (don't sell a false difference)

OpenAI Evals is not judge-only. Its basic templates —
[`Match`, `Includes`, `FuzzyMatch`, `JsonMatch`](https://github.com/openai/evals/blob/8eac7a7de5215c907fbddc30efdaf316913eccdd/docs/eval-templates.md) —
are plain deterministic string and JSON checks with no model in the loop. So
"deterministic versus model-graded" is not the real difference between the two
tools. The real difference is the unit under test: a model's answer to a dataset,
versus an agent's effect on a workspace.

## Using them together

Use OpenAI Evals to measure a model on a benchmark: pick an eval, run it, compare
models. Use evalctl to check that the agent you built on top of a model did the
right thing to your repo. One asks whether the model answers well; the other asks
whether the agent acted correctly.

## Don't say

- Don't say OpenAI Evals can only use a model to grade. Its basic templates are
  deterministic string and JSON matches.
- Don't say the open-source repo is actively developed. Its last commit is dated
  2026-04-14, and OpenAI now steers users to the hosted Dashboard.
- Don't say evalctl grades model answers or runs benchmarks. It scores what an
  agent did to a workspace.
- Don't call evalctl a replacement for OpenAI Evals. They test different units.

## Sources

- OpenAI Evals repository, main at commit `8eac7a7`, dated 2026-04-14, checked
  2026-09-20:
  [github.com/openai/evals](https://github.com/openai/evals/tree/8eac7a7de5215c907fbddc30efdaf316913eccdd)
- OpenAI Evals templates (basic deterministic templates, model-graded template,
  completion `a` vs answers `B`):
  [docs/eval-templates.md](https://github.com/openai/evals/blob/8eac7a7de5215c907fbddc30efdaf316913eccdd/docs/eval-templates.md)
- evalctl scope and sandboxing: [How evalctl compares](/docs/comparison/),
  [Security and sandboxing](/docs/security/)
