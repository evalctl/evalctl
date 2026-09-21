---
title: evalctl and Braintrust
description: When to use evalctl, a local command-line check that scores what your agent did, and when to use Braintrust, a hosted platform for running evaluations, storing datasets, and comparing experiments across a team.
bucket: project
order: 11
draft: false
---

# evalctl and Braintrust

Both tools evaluate AI work, and both let you score a run with fixed checks or an
AI judge. So they look like alternatives. They sit at different layers. Braintrust
is a platform a team logs into; evalctl is a single command you run on your own
machine. This page says which job each one fits.

**Research baseline.** Braintrust is a hosted platform; its docs are at
[braintrust.dev/docs](https://www.braintrust.dev/docs/start/eval-sdk). The eval
SDK is `braintrustdata/braintrust-sdk-javascript`, main at commit
[`2c77318`](https://github.com/braintrustdata/braintrust-sdk-javascript/tree/2c77318f7f39b038315a8fd05c0ce25fe5714de7),
and its scorer library is
[`braintrustdata/autoevals`](https://github.com/braintrustdata/autoevals)
(checked 2026-09-20). Every Braintrust claim below links to that source.

## Which one to use

Use **evalctl** to check what your agent did to your own workspace, fast, with
nothing to set up. It installs with `pip`, needs no account, no server, and no
model API key to run. The agent under test can run it itself. Each run leaves one
results file you can commit, and another machine can re-check it offline.

Use **Braintrust** to run evaluations against curated datasets, store the results,
and compare experiments across a team. You write an `Eval()` with a dataset, a
task, and scoring functions; Braintrust records each run as an experiment and shows
it in a dashboard ([eval SDK](https://www.braintrust.dev/docs/start/eval-sdk)).

They fit together. evalctl is the quick local gate you run first, on your laptop or
in continuous integration. Braintrust is the shared record you keep on top, so a
team can compare versions and track scores over time. This page doesn't argue for
one instead of the other.

## What each one is

evalctl is a single command-line tool. You run it, it runs your agent, and it
grades the result against the workspace the agent left behind: the files it wrote,
the diffs it made, the commands it ran, the exit codes. It runs in place, on your
machine, and it warns on every run that the runner isn't sandboxed.

Braintrust is a hosted platform. You sign up, create API keys for Braintrust and
for your model provider, and write an evaluation from three parts: "Data — a
dataset of test cases with inputs and expected outputs," a "Task" to test, and
"Scores" that measure output quality
([eval SDK](https://www.braintrust.dev/docs/start/eval-sdk)). The `Eval()` function
creates "an experiment, a permanent record of how your task performed on the
dataset." Scores come from the `autoevals` library — an AI judge such as
`ExactMatch`, or a scorer you write yourself
([autoevals](https://github.com/braintrustdata/autoevals)). The SDK runs on your
machine, but the results are sent to Braintrust's cloud and viewed in its
dashboard.

## Side by side

| Area | evalctl | Braintrust |
| --- | --- | --- |
| Built for | Checking what your agent did to your own workspace. | Running dataset evaluations and comparing experiments across a team. |
| Who runs it | The agent under test, or one person, with no setup. | A team, through the SDK, with results in a shared dashboard. |
| Unit under test | The workspace: files, diffs, commands, exit codes. | Task outputs against expected outputs in a dataset. |
| Where results live | One portable file on your machine, ready to commit. | Braintrust's cloud, viewed in its dashboard. |
| Setup to run | `pip install evalctl`. No account, no server, no model key. | An account, a Braintrust API key, and a model provider key. |
| How it grades | Fixed checks over the workspace; an AI judge is optional and never the only check. | `autoevals` scorers (AI judge) or custom code scorers. |
| Team history | None. Each run is a standalone file. | Kept over time: datasets, experiments, comparisons. |
| Cost model | Free tool; a run costs no model calls by default. | Free and paid tiers; results stored in Braintrust's cloud. |

Read this by column, not row by row. evalctl is the whole left column: small,
local, no account, pointed at your own repo, one file per run. Braintrust is the
whole right column: a hosted service that runs dataset evaluations and stores
experiments for a team.

## What Braintrust does well

- **Experiments you can compare.** An `Eval()` run is "a permanent record of how
  your task performed on the dataset," and the platform makes it "easy to compare
  different versions of your prompts or models"
  ([eval SDK](https://www.braintrust.dev/docs/start/eval-sdk)). evalctl keeps no
  history; each run is one file.
- **A ready scorer library.** `autoevals` ships scorers — exact match, plus
  LLM-as-judge scorers — so you don't write them from scratch
  ([autoevals](https://github.com/braintrustdata/autoevals)).
- **Datasets, a playground, and a dashboard.** Curated test sets, a UI playground
  for testing without code, and a shared dashboard are the platform's core. That is
  team workflow, and it is not evalctl's shape.

If your need is stored datasets, comparable experiments, and a shared dashboard,
that is Braintrust's shape, not evalctl's.

## What evalctl does differently

- **No account, no server, no model key to run.** Install it and run it in a
  continuous integration job in seconds, with nothing to log into and no data sent
  to a cloud. evalctl scores what your agent already did, so a run costs no model
  calls by default.
- **Pointed at your own workspace.** You don't curate a dataset of input and
  expected-output pairs. You point evalctl at the change your agent made and ask
  whether it still passes. That is a local gate on your work, not a stored
  experiment.
- **It scores the workspace, not the output text.** evalctl grades the files,
  diffs, commands, and exit codes an agent produced. Braintrust centers on task
  outputs compared to expected outputs. The unit under test is different.
- **The agent runs it.** evalctl is built so the agent under test can call it, read
  the result, and continue. If it guesses a command wrong, the error tells it the
  right one to paste.
- **A results file that stays with your code.** evalctl writes one portable file you
  commit next to the change, and a second machine reproduces the report offline.
  Braintrust keeps the record in its cloud instead.

## What is the same (don't sell a false difference)

Both tools can grade with plain, repeatable checks, and both offer an AI judge as
one option. Braintrust's `autoevals` includes a code-based `ExactMatch` scorer and
lets you write your own scoring functions
([eval SDK](https://www.braintrust.dev/docs/start/eval-sdk)), so "deterministic
versus judge-based" is **not** the real difference between them. The real
differences are the unit under test, where the results live, and whether you need
an account and a cloud at all.

## Using them together

Run evalctl as the quick local check while you work: score a change on a laptop or
in continuous integration, with no account and no model spend, and commit the
results file next to the code. When you want curated datasets, comparable
experiments, or a shared dashboard across a team, log those into Braintrust on top.
The evalctl file records how one run was scored on your own repo — a different
artifact than a Braintrust experiment stored in the cloud.

## Don't say

- Don't say Braintrust only uses LLM judges. `autoevals` includes code-based
  scorers such as `ExactMatch`, and you can write your own.
- Don't call Braintrust fully local. The SDK runs on your machine, but results are
  sent to Braintrust's cloud and viewed in its dashboard.
- Don't call evalctl a replacement for Braintrust. evalctl keeps no shared history,
  no datasets, and no dashboard on purpose.
- Don't say evalctl scores task outputs the way Braintrust does. evalctl scores the
  workspace an agent changed — files, diffs, commands, exit codes.
- Don't claim evalctl needs an account or a model key. It runs from the command
  line with nothing to log into.

## See also

- [All tool comparisons](/docs/comparisons/) — the full set, and which tool fits which question.

## Sources

- Braintrust eval SDK (Eval, data/task/scores, experiments):
  [braintrust.dev/docs/start/eval-sdk](https://www.braintrust.dev/docs/start/eval-sdk)
- Braintrust eval SDK library, main at commit `2c77318`, 2026-09-20:
  [github.com/braintrustdata/braintrust-sdk-javascript](https://github.com/braintrustdata/braintrust-sdk-javascript/tree/2c77318f7f39b038315a8fd05c0ce25fe5714de7)
- Braintrust `autoevals` scorers (exact match, LLM-as-judge):
  [github.com/braintrustdata/autoevals](https://github.com/braintrustdata/autoevals)
- evalctl scope and sandboxing: [How evalctl compares](/docs/comparison/),
  [Security and sandboxing](/docs/security/)
