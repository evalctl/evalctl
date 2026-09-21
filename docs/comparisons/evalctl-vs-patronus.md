---
title: evalctl and Patronus
description: When to use evalctl, a local command-line check that scores what your agent did to a workspace, and when to use Patronus, a hosted platform whose SDK scores LLM outputs with managed and custom evaluators.
bucket: project
order: 12
draft: false
---

# evalctl and Patronus

Both tools evaluate AI work, and both let you score a run with a fixed check or an
AI grader. So people ask which one to pick. They sit at different layers. Patronus
is a platform you send outputs to, through an SDK that talks to a hosted service;
evalctl is a single command you run on your own machine. This page says which job
each one fits.

**Research baseline.** Patronus is a hosted evaluation platform. Its open Python
SDK, `patronus`, is published on PyPI at version
[`0.1.26`](https://pypi.org/project/patronus/0.1.26/) (MIT), sdist SHA-256
`26a17b93ba0df2f12c9f6c98ac60473eb260e647c46500ed489d4b60d676e442` (checked
2026-09-20). The SDK's source repository is not public, so the claims below are
pinned to that released package and to the open SDK documentation at
[patronus-ai.github.io/patronus-py](https://patronus-ai.github.io/patronus-py/).
The platform console and API sit behind an account at `app.patronus.ai`.

## Which one to use

Use **evalctl** to check what your agent did to your own workspace, fast, with
nothing to set up. It installs with `pip`, needs no account, no server, and no
model API key to run. The agent under test can run it itself. Each run leaves one
results file you can commit, and another machine can re-check it offline.

Use **Patronus** to score the quality of LLM and application outputs with managed
graders, track experiments over a dataset, and keep the results in a shared
platform. Its managed evaluators "run on Patronus infrastructure"
([Patronus evaluators](https://patronus-ai.github.io/patronus-py/evaluations/patronus-evaluators/)),
so you get graders such as factual accuracy, hallucination, and PII detection
without writing the logic yourself.

They fit together. evalctl is the quick local gate you run first, on your laptop
or in continuous integration. Patronus is the shared record you keep on top when
you want managed graders and a history of experiments. This page doesn't argue for
one instead of the other.

## What each one is

evalctl is a single command-line tool. You run it, it runs your agent, and it
grades the result against the workspace the agent left behind: the files it wrote,
the diffs it made, the commands it ran, the exit codes. It runs in place, on your
machine, and it warns on every run that the runner isn't sandboxed.

Patronus is a hosted platform with an SDK. You call `patronus.init()` with an API
key, then score outputs with evaluators. To get a key you "sign up at
https://app.patronus.ai" and create one
([initialization](https://patronus-ai.github.io/patronus-py/getting-started/initialization/));
by default the SDK talks to `api.patronus.ai` and exports traces to
`otel.patronus.ai`. It offers two kinds of evaluator: **managed** ones you call
with `RemoteEvaluator(...)`, which run on Patronus infrastructure
([Patronus evaluators](https://patronus-ai.github.io/patronus-py/evaluations/patronus-evaluators/)),
and **custom** ones you write yourself with the `@evaluator()` decorator or a
class, which run in your own process
([custom evaluators](https://patronus-ai.github.io/patronus-py/evaluations/evaluators/)).
Its experiments framework runs a set of evaluators over a dataset of task inputs
and outputs and records the scores
([experiments](https://patronus-ai.github.io/patronus-py/experiments/introduction/)).

## Side by side

| Area | evalctl | Patronus |
| --- | --- | --- |
| Built for | Checking what your agent did to your own workspace. | Scoring LLM and application output quality with managed and custom evaluators. |
| Who runs it | The agent under test, or one person, with no setup. | A developer or team, through the SDK against the hosted platform. |
| Unit under test | The workspace: files, diffs, commands, exit codes. | Application outputs — the task output for a given input. |
| Where results live | One portable file on your machine, ready to commit. | The Patronus platform, viewed at `app.patronus.ai`. |
| Setup to run | `pip install evalctl`. No account, no server, no model key. | `pip install patronus`, plus a Patronus API key for managed graders and tracking. |
| How it grades | Fixed checks over the workspace; an AI judge is optional and never the only check. | Managed graders on Patronus infrastructure, plus custom code evaluators you write. |
| Team history | None. Each run is a standalone file. | Kept over time: experiments and traces in the platform. |
| Cost model | Free tool; a run costs no model calls by default. | Managed graders and tracking run against the hosted account. |

Read this by column, not row by row. evalctl is the whole left column: small,
local, no account, pointed at your own repo, one file per run. Patronus is the
whole right column: an SDK that scores generated output against managed graders
and keeps the results in a hosted platform.

## What Patronus does well

- **Managed graders you don't have to build.** `RemoteEvaluator("judge", "factual-accuracy")`
  calls a grader that runs on Patronus infrastructure, and you can define your own
  criteria in the platform console
  ([Patronus evaluators](https://patronus-ai.github.io/patronus-py/evaluations/patronus-evaluators/)).
  evalctl ships no library of hosted graders; you write the checks.
- **Dataset experiments with tracking.** The experiments framework runs evaluators
  across a dataset and stores the scores for comparison
  ([experiments](https://patronus-ai.github.io/patronus-py/experiments/introduction/)).
  evalctl keeps no history; each run is one file.
- **Output-quality evaluation.** Patronus is built to judge generated text —
  accuracy, hallucination, PII, and criteria you define — across many examples.
  That is a different question than "what did the agent do to my repo," and it is
  Patronus's shape, not evalctl's.

If your need is managed graders, dataset experiments, and stored history for
output quality, that is Patronus's shape, not evalctl's.

## What evalctl does differently

- **No account, no server, no model key to run.** Install it and run it in a
  continuous integration job in seconds, with nothing to log into. evalctl scores
  what your agent already did, so a run costs no model calls by default. Patronus's
  managed graders and tracking run against a hosted account.
- **It scores the workspace, not the reply.** evalctl grades the files, diffs,
  commands, and exit codes an agent produced. Patronus centers on the output a
  model or application returned for an input. The unit under test is different.
- **Pointed at your own workspace.** You don't assemble a dataset of input and
  output pairs. You point evalctl at the change your agent made and ask whether it
  still passes. That is a local gate on your work, not a stored experiment.
- **The agent runs it.** evalctl is built so the agent under test can call it, read
  the result, and continue. If it guesses a command wrong, the error tells it the
  right one to paste.
- **A results file another machine can re-check.** Hand the run directory to a
  second machine and it reproduces the report offline, with no account to sign
  into. The file records what went in, the checks that ran, and what they found.

## What is the same (don't sell a false difference)

Both tools can grade with plain, repeatable checks, and both offer an AI grader as
one option. Patronus supports custom code evaluators through the `@evaluator()`
decorator alongside its managed graders
([custom evaluators](https://patronus-ai.github.io/patronus-py/evaluations/evaluators/)),
so "deterministic versus judge-based" is **not** the real difference between them.
The real differences are the unit under test, where the results live, and whether
you need an account and a hosted service at all. The SDK itself is open source
(MIT); the graders, console, and tracking are the hosted part.

## Using them together

Run evalctl as the quick local check while you work: score a change on a laptop or
in continuous integration, with no account and no model spend, and commit the
results file next to the code. When you want managed graders for output quality, or
a tracked history of experiments over a dataset, log those into Patronus on top.
The evalctl file records how one run was scored on your own repo — a different
artifact than a Patronus experiment stored in the platform.

## Don't say

- Don't say Patronus only uses LLM judges. It offers managed graders **and**
  custom code evaluators you write with the `@evaluator()` decorator.
- Don't call the Patronus SDK closed source. The `patronus` package is MIT; it is
  the platform — graders, console, tracking — that is hosted and needs an account.
- Don't say Patronus runs fully offline. Managed graders and tracking talk to
  `api.patronus.ai` with an API key.
- Don't say evalctl scores model outputs the way Patronus does. evalctl scores the
  workspace an agent changed — files, diffs, commands, exit codes.
- Don't claim evalctl needs an account or a server. It runs from the command line
  with nothing to log into.

## See also

- [All tool comparisons](/docs/comparisons/) — the full set, and which tool fits which question.

## Sources

- Patronus Python SDK on PyPI, version `0.1.26` (MIT), 2026-09-20:
  [pypi.org/project/patronus/0.1.26](https://pypi.org/project/patronus/0.1.26/)
- Patronus SDK documentation:
  [patronus-ai.github.io/patronus-py](https://patronus-ai.github.io/patronus-py/)
- Managed (remote) evaluators run on Patronus infrastructure:
  [patronus-ai.github.io/patronus-py/evaluations/patronus-evaluators](https://patronus-ai.github.io/patronus-py/evaluations/patronus-evaluators/)
- Custom code evaluators via the `@evaluator()` decorator:
  [patronus-ai.github.io/patronus-py/evaluations/evaluators](https://patronus-ai.github.io/patronus-py/evaluations/evaluators/)
- API key and account setup:
  [patronus-ai.github.io/patronus-py/getting-started/initialization](https://patronus-ai.github.io/patronus-py/getting-started/initialization/)
- Experiments framework (evaluators over a dataset):
  [patronus-ai.github.io/patronus-py/experiments/introduction](https://patronus-ai.github.io/patronus-py/experiments/introduction/)
- evalctl scope and sandboxing: [How evalctl compares](/docs/comparison/),
  [Security and sandboxing](/docs/security/)
