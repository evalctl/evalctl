---
title: evalctl and TruLens
description: When to use evalctl, a local command-line check that scores what your agent did, and when to use TruLens, a local open-source library that scores LLM application outputs with feedback functions.
bucket: project
order: 15
draft: false
---

# evalctl and TruLens

Both tools are open source, install with `pip`, and run on your own machine with no
account. So they are closer than most of the tools on this site. They still measure
different things. TruLens scores the quality of an LLM application's outputs with
feedback functions; evalctl scores what an agent did to a workspace. This page says
which job each one fits.

**Research baseline.** TruLens is an open-source evaluation library; its docs are at
[trulens.org](https://www.trulens.org/getting_started/). The repository is
`truera/trulens`, main at commit
[`33e4f66`](https://github.com/truera/trulens/tree/33e4f66126decd9db4161e6fc29b7e542106149a)
(checked 2026-09-20). Every TruLens claim below links to that source.

## Which one to use

Use **evalctl** to check what your agent did to your own workspace: the files it
wrote, the diffs it made, the commands it ran, the exit codes. It runs the agent,
scores the result against fixed checks, and leaves one results file you can commit.
The agent under test can run it itself, and it costs no model calls by default.

Use **TruLens** to measure the quality of an LLM application's outputs — a RAG
pipeline's answers, a chatbot's replies — with "feedback functions" for things like
groundedness, relevance, and the "Honest, Harmless, Helpful" dimensions
([getting started](https://www.trulens.org/getting_started/)). It instruments your
app, records each call, and shows scores in a local dashboard.

They fit together. evalctl gates what your agent did to the repo; TruLens measures
how good the text your app produced is. This page doesn't argue for one instead of
the other.

## What each one is

evalctl is a single command-line tool. You run it, it runs your agent, and it grades
the result against the workspace the agent left behind. It runs in place, on your
machine, and it warns on every run that the runner isn't sandboxed.

TruLens is a Python library you wrap around a running LLM application. You attach
feedback functions, run the app, and TruLens records each invocation and scores it.
Feedback functions can be LLM-as-judge or programmatic, drawn from stock metrics or
written yourself ([getting started](https://www.trulens.org/getting_started/)).
Results are stored in a local database — SQLite by default, or Postgres or Snowflake —
and shown in a dashboard with a leaderboard and trends. It runs entirely on your
machine, with no hosted account required.

## Side by side

| Area | evalctl | TruLens |
| --- | --- | --- |
| Built for | Checking what your agent did to your own workspace. | Measuring the quality of an LLM application's outputs. |
| Unit under test | The workspace: files, diffs, commands, exit codes. | An app's inputs, outputs, and intermediate steps (e.g. RAG). |
| How you use it | Point it at a change and run it as a gate. | Instrument a running app with feedback functions. |
| Where results live | One portable file on your machine, ready to commit. | A local database (SQLite/Postgres/Snowflake) and a dashboard. |
| Runs locally | Yes. No account, no server, no model key to run. | Yes. No account; a model key for LLM-judge feedback. |
| How it grades | Fixed checks over the workspace; an AI judge is optional and never the only check. | Feedback functions: LLM-as-judge or programmatic. |
| Typical target | A coding or workspace-changing agent. | A RAG pipeline, chatbot, or other text-generating app. |

Read this by column, not row by row. Both columns are local and open source. The
difference is the top two rows: evalctl scores a workspace an agent changed; TruLens
scores the text an application produced.

## What TruLens does well

- **App-output quality metrics.** TruLens ships feedback functions for groundedness,
  context relevance, answer relevance, and more — the metrics you want for a RAG
  pipeline or a chatbot ([getting started](https://www.trulens.org/getting_started/)).
  That is not what evalctl measures.
- **A dashboard over many runs.** Results land in a local database and a dashboard
  with a leaderboard and trends, so you can watch a metric move across versions.
  evalctl keeps no history; each run is one file.
- **Instrumentation of a running app.** TruLens records each invocation and its
  intermediate steps, close to observability of the app in motion. evalctl scores the
  outcome of a run, not the steps inside it.

If your need is measuring the quality of an application's generated text, that is
TruLens's shape, not evalctl's.

## What evalctl does differently

- **It scores the workspace, not the output text.** evalctl grades the files, diffs,
  commands, and exit codes an agent produced. TruLens centers on an application's
  outputs — the answer, the retrieval, the reply. The unit under test is different.
- **No model key to run.** evalctl scores what your agent already did, so a run costs
  no model calls by default. TruLens's LLM-judge feedback functions call a model, and
  need a key to do so.
- **Point-and-gate, not instrument.** You don't wrap your app in feedback functions.
  You point evalctl at the change your agent made and ask whether it still passes.
  That is a local gate, not an instrumented run.
- **The agent runs it.** evalctl is built so the agent under test can call it, read
  the result, and continue. If it guesses a command wrong, the error tells it the
  right one to paste.
- **One portable results file.** evalctl writes one file you commit next to the
  change, and a second machine reproduces the report offline. TruLens keeps its
  records in a database behind a dashboard.

## What is the same (don't sell a false difference)

Both tools are local, open source, and installable with `pip`, and both can grade
without a model — TruLens's feedback functions can be programmatic, not just
LLM-as-judge ([getting started](https://www.trulens.org/getting_started/)). So
"local versus hosted" and "deterministic versus judge-based" are **not** the real
differences here. The real difference is what each one measures: a workspace an agent
changed, versus the text an application produced.

## Using them together

Run evalctl as the gate on what your agent did to the repo: score a change on a
laptop or in continuous integration and commit the results file. When you also want
to measure how good the text your application generates is — its groundedness, its
relevance — reach for TruLens and its feedback functions. One checks the workspace;
the other checks the output. They answer different questions about the same system.

## Don't say

- Don't say TruLens needs an account or a hosted platform. It is open source and runs
  locally, storing results in a local database.
- Don't say TruLens only uses LLM judges. Its feedback functions can be programmatic
  as well as LLM-as-judge.
- Don't use the "no server" wedge against TruLens. Both tools run locally; the real
  difference is what they measure.
- Don't call evalctl a replacement for TruLens. evalctl does not measure the quality
  of generated text or a RAG pipeline's answers.
- Don't say evalctl scores app outputs the way TruLens does. evalctl scores the
  workspace an agent changed — files, diffs, commands, exit codes.

## See also

- [All tool comparisons](/docs/comparisons/) — the full set, and which tool fits which question.

## Sources

- TruLens getting started (feedback functions, local dashboard, stock metrics):
  [trulens.org/getting_started](https://www.trulens.org/getting_started/)
- TruLens repository, main at commit `33e4f66`, 2026-09-20:
  [github.com/truera/trulens](https://github.com/truera/trulens/tree/33e4f66126decd9db4161e6fc29b7e542106149a)
- evalctl scope and sandboxing: [How evalctl compares](/docs/comparison/),
  [Security and sandboxing](/docs/security/)
