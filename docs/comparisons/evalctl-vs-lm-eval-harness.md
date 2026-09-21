---
title: evalctl and lm-evaluation-harness
description: When to use evalctl, a local command-line check that scores what your agent did to a workspace, and when to use lm-evaluation-harness, the standard framework for measuring a language model's capability on fixed academic benchmarks.
bucket: project
order: 17
draft: false
---

# evalctl and lm-evaluation-harness

These two tools are easy to keep apart once you name what each one measures.
lm-evaluation-harness measures how capable a language model is on fixed academic
benchmarks. evalctl measures what an agent did to a workspace. Different unit under
test, different job. This page says which one fits your question.

**Research baseline.** lm-evaluation-harness is EleutherAI's model-evaluation
framework; the repository is `EleutherAI/lm-evaluation-harness`, main at commit
[`d6de816`](https://github.com/EleutherAI/lm-evaluation-harness/tree/d6de81643928d653435c431bae19945d41d32520)
(checked 2026-09-20). Every claim about it below links to that source.

## Which one to use

Use **evalctl** to check what your agent did to your own workspace: the files it
wrote, the diffs it made, the commands it ran, the exit codes. It installs with
`pip`, needs no account, and the agent under test can run it itself. Each run leaves
one results file you can commit.

Use **lm-evaluation-harness** to measure a model's capability on standard benchmarks.
It is "a unified framework to test generative language models on a large number of
different evaluation tasks," with "over 60 standard academic benchmarks for LLMs" such
as MMLU, HellaSwag, and GSM8K
([README](https://github.com/EleutherAI/lm-evaluation-harness/tree/d6de81643928d653435c431bae19945d41d32520)).
It is the backend for Hugging Face's Open LLM Leaderboard.

They answer different questions. lm-evaluation-harness asks, "how good is this model
on known benchmarks?" evalctl asks, "did this change break what my agent does to a
workspace?" This page doesn't argue for one instead of the other.

## What each one is

evalctl is a single command-line tool. You run it, it runs your agent, and it grades
the result against the workspace the agent left behind. It runs in place, on your
machine, and it warns on every run that the runner isn't sandboxed.

lm-evaluation-harness is a framework for scoring models on fixed datasets. It runs a
model over standard tasks through request types like "generate_until, loglikelihood,
loglikelihood_rolling, and multiple_choice," using "publicly available prompts" so
results are reproducible and comparable between papers
([README](https://github.com/EleutherAI/lm-evaluation-harness/tree/d6de81643928d653435c431bae19945d41d32520)).
What it measures is a model's answers to standard questions — knowledge, reasoning,
multiple choice — not what an agent did to a system.

## Side by side

| Area | evalctl | lm-evaluation-harness |
| --- | --- | --- |
| What it measures | What an agent did to a workspace. | A model's answers on fixed academic benchmarks. |
| Unit under test | Files, diffs, commands, exit codes. | A model's response to a standard prompt. |
| The question | Did this change break what my agent does here? | How capable is this model on known tasks? |
| What you point it at | Your own repository and change. | A model, run over 60+ standard benchmark tasks. |
| Where the tasks come from | You define them, over your own work. | Fixed public datasets (MMLU, HellaSwag, GSM8K, …). |
| Setup to run | `pip install evalctl`. No account, no model key. | The harness plus a model to load and run. |
| Reused across models | No — it's your gate, not a ranking. | Yes — it's the backend of the Open LLM Leaderboard. |

Read this by column, not row by row. evalctl scores a workspace an agent changed.
lm-evaluation-harness scores a model's answers on standard tasks. The two never
measure the same thing.

## What lm-evaluation-harness does well

- **The standard for model capability.** With 60+ benchmarks and hundreds of
  subtasks, it is the common way to measure and compare model capability, "used in
  hundreds of papers"
  ([README](https://github.com/EleutherAI/lm-evaluation-harness/tree/d6de81643928d653435c431bae19945d41d32520)).
  evalctl says nothing about a model's MMLU score.
- **Reproducible, comparable results.** Publicly available prompts make results
  reproducible across papers and labs. That comparability is the point of a benchmark
  harness.
- **Leaderboard backing.** It is the engine behind Hugging Face's Open LLM
  Leaderboard, so its numbers are the ones the field cites. That is a different job
  than gating your own repo.

If your need is a model's capability score on known benchmarks, that is
lm-evaluation-harness's shape, not evalctl's.

## What evalctl does differently

- **It scores an agent's actions, not a model's answers.** evalctl grades the files,
  diffs, commands, and exit codes an agent produced. lm-evaluation-harness grades a
  model's response to a standard prompt. The unit under test is different.
- **Your own tasks, not a fixed dataset.** You don't run MMLU or GSM8K. You point
  evalctl at the change your agent made to your code and ask whether it still passes.
- **A gate, not a ranking.** evalctl answers whether today's change is good on your
  own work; it publishes no leaderboard and compares no models.
- **The agent runs it.** evalctl is built so the agent under test can call it, read the
  result, and continue. If it guesses a command wrong, the error tells it the right one
  to paste.
- **A results file another machine can re-check.** Hand the run directory to a second
  machine and it reproduces the report offline.

## What is the same (don't sell a false difference)

Both tools score against fixed expectations rather than by asking a judge model by
default — lm-evaluation-harness against dataset answers, evalctl against your checks.
So "deterministic versus judge-based" is **not** the difference here. The difference
is the unit under test: a model's answers on public benchmarks, versus what an agent
did to your workspace.

## Using them together

They rarely overlap, and that is the point. Use lm-evaluation-harness when you are
choosing or measuring a model and want its capability score on known benchmarks. Use
evalctl when you have an agent built on some model and want to gate what it does to
your repo. One measures the model; the other measures the agent's effect on your work.

## Don't say

- Don't call lm-evaluation-harness a tool for evaluating your own agent on your own
  repo. It measures a model's capability on fixed public benchmarks.
- Don't say lm-evaluation-harness uses an AI judge by default. It scores a model's
  answers against standard datasets.
- Don't call evalctl a model benchmark. It scores what an agent did to a workspace and
  ranks nothing.
- Don't say evalctl measures model capability. It has nothing to say about MMLU-style
  knowledge or reasoning scores.
- Don't imply the two compete. They measure different things — a model's answers versus
  an agent's actions.

## See also

- [All tool comparisons](/docs/comparisons/) — the full set, and which tool fits which question.

## Sources

- lm-evaluation-harness repository (60+ benchmarks, request types, public prompts,
  Open LLM Leaderboard backend), main at commit `d6de816`, 2026-09-20:
  [github.com/EleutherAI/lm-evaluation-harness](https://github.com/EleutherAI/lm-evaluation-harness/tree/d6de81643928d653435c431bae19945d41d32520)
- evalctl scope: [How evalctl compares](/docs/comparison/),
  [Security and sandboxing](/docs/security/)
