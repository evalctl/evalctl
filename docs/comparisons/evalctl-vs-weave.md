---
title: evalctl and W&B Weave
description: When to use evalctl, a local command-line check that scores what your agent did, and when to use Weights & Biases Weave, which logs LLM traces and evaluations to the W&B platform for a team.
bucket: project
order: 15
draft: false
---

# evalctl and W&B Weave

Both tools evaluate AI work, and both can grade a run with fixed checks or an AI
judge. So they look like alternatives. They sit at different layers. Weave logs
traces and evaluations to the Weights & Biases platform; evalctl is a single command
you run on your own machine. This page says which job each one fits.

**Research baseline.** Weave is the LLM tracing and evaluation product from Weights
& Biases; its docs are at
[docs.wandb.ai/weave](https://docs.wandb.ai/weave/guides/core-types/evaluations).
The repository is `wandb/weave`, main at commit
[`e49e576`](https://github.com/wandb/weave/tree/e49e576bde9602d19d505279949803c1b7b34f79)
(checked 2026-09-20). Every Weave claim below links to that source.

## Which one to use

Use **evalctl** to check what your agent did to your own workspace, fast, with
nothing to set up. It installs with `pip`, needs no account, no server, and no model
API key to run. The agent under test can run it itself. Each run leaves one results
file you can commit, and another machine can re-check it offline.

Use **Weave** to run evaluations against curated datasets and keep the results in
the Weights & Biases platform. You call `weave.init(...)`, define an `Evaluation`
over a `Dataset` and one or more scoring functions, and inspect the run "in the Weave
UI" ([evaluations](https://docs.wandb.ai/weave/guides/core-types/evaluations)).

They fit together. evalctl is the quick local gate you run first, on your laptop or
in continuous integration. Weave is the logged record you keep on top, so a team can
compare runs and track scores over time. This page doesn't argue for one instead of
the other.

## What each one is

evalctl is a single command-line tool. You run it, it runs your agent, and it grades
the result against the workspace the agent left behind: the files it wrote, the diffs
it made, the commands it ran, the exit codes. It runs in place, on your machine, and
it warns on every run that the runner isn't sandboxed.

Weave is a tracing and evaluation product tied to the Weights & Biases platform. Its
workflow centers on "the `Evaluation` object, which defines a `Dataset` or list of
dictionaries for test examples, one or more scoring functions," run against a `Model`
whose "each call to `.evaluate()` triggers an evaluation run"
([evaluations](https://docs.wandb.ai/weave/guides/core-types/evaluations)). Scoring
functions can be plain code, and Weave also provides a standardized `LLMJudge` class.
You start with `weave.init(...)` and view results in the Weave UI; no offline-only
mode is documented.

## Side by side

| Area | evalctl | W&B Weave |
| --- | --- | --- |
| Built for | Checking what your agent did to your own workspace. | Running dataset evaluations and logging traces to the W&B platform. |
| Who runs it | The agent under test, or one person, with no setup. | A team, through the SDK, with results in the Weave UI. |
| Unit under test | The workspace: files, diffs, commands, exit codes. | Model or function outputs against examples in a Dataset. |
| Where results live | One portable file on your machine, ready to commit. | The Weights & Biases platform, viewed in the Weave UI. |
| Setup to run | `pip install evalctl`. No account, no server, no model key. | `weave.init(...)` with a W&B account; a model key for judge scorers. |
| How it grades | Fixed checks over the workspace; an AI judge is optional and never the only check. | Code scoring functions or an `LLMJudge` class. |
| Team history | None. Each run is a standalone file. | Kept in the platform: evaluations, traces, comparisons. |

Read this by column, not row by row. evalctl is the whole left column: small, local,
no account, pointed at your own repo, one file per run. Weave is the whole right
column: a logged service that keeps evaluations and traces for a team.

## What Weave does well

- **Reusable, comparable evaluations.** An `Evaluation` pairs a dataset with scoring
  functions into "a reusable evaluation configuration" whose runs you compare in the
  UI ([evaluations](https://docs.wandb.ai/weave/guides/core-types/evaluations)).
  evalctl keeps no history; each run is one file.
- **Traces alongside scores.** Weave logs the traces of a run next to its scores, so
  a team can inspect what happened. evalctl scores the outcome; it does not log a
  trace stream.
- **Part of a broader platform.** Weave sits in the Weights & Biases ecosystem that
  many teams already use for experiment tracking. That is team workflow, and it is
  not evalctl's shape.

If your need is logged, comparable evaluations and traces inside the W&B platform,
that is Weave's shape, not evalctl's.

## What evalctl does differently

- **No account, no server, no model key to run.** Install it and run it in a
  continuous integration job in seconds, with nothing to log into. evalctl scores
  what your agent already did, so a run costs no model calls by default. Weave
  expects `weave.init(...)` and a W&B account to log to.
- **Pointed at your own workspace.** You don't curate a dataset of examples and a
  model function. You point evalctl at the change your agent made and ask whether it
  still passes. That is a local gate on your work, not a logged evaluation.
- **It scores the workspace, not the model output.** evalctl grades the files, diffs,
  commands, and exit codes an agent produced. Weave centers on model or function
  outputs against dataset examples. The unit under test is different.
- **The agent runs it.** evalctl is built so the agent under test can call it, read
  the result, and continue. If it guesses a command wrong, the error tells it the
  right one to paste.
- **A results file that stays with your code.** evalctl writes one portable file you
  commit next to the change, and a second machine reproduces the report offline.
  Weave keeps the record in the W&B platform instead.

## What is the same (don't sell a false difference)

Both tools can grade with plain, repeatable checks, and both offer an AI judge as one
option. Weave's scoring functions can be plain code, with `LLMJudge` as one provided
class ([evaluations](https://docs.wandb.ai/weave/guides/core-types/evaluations)), so
"deterministic versus judge-based" is **not** the real difference between them. The
real differences are the unit under test, where the results live, and whether you
need an account and a platform at all.

## Using them together

Run evalctl as the quick local check while you work: score a change on a laptop or in
continuous integration, with no account and no model spend, and commit the results
file next to the code. When you want logged, comparable evaluations and traces across
a team, send those to Weave on top. The evalctl file records how one run was scored on
your own repo — a different artifact than a Weave evaluation held in the W&B platform.

## Don't say

- Don't say Weave only uses LLM judges. Its scoring functions can be plain code, and
  `LLMJudge` is one provided class.
- Don't claim Weave runs fully offline. It logs to the Weave UI and its docs describe
  no offline-only mode; treat it as tied to the W&B platform.
- Don't call evalctl a replacement for Weave. evalctl keeps no logged history, no
  traces, and no shared UI on purpose.
- Don't say evalctl scores model outputs the way Weave does. evalctl scores the
  workspace an agent changed — files, diffs, commands, exit codes.
- Don't claim evalctl needs an account or a model key. It runs from the command line
  with nothing to log into.

## See also

- [All tool comparisons](/docs/comparisons/) — the full set, and which tool fits which question.

## Sources

- Weave evaluations (Evaluation, Dataset, Model, scoring functions, LLMJudge, UI):
  [docs.wandb.ai/weave/guides/core-types/evaluations](https://docs.wandb.ai/weave/guides/core-types/evaluations)
- Weave repository, main at commit `e49e576`, 2026-09-20:
  [github.com/wandb/weave](https://github.com/wandb/weave/tree/e49e576bde9602d19d505279949803c1b7b34f79)
- evalctl scope and sandboxing: [How evalctl compares](/docs/comparison/),
  [Security and sandboxing](/docs/security/)
