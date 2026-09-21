---
title: evalctl and Langfuse
description: When to use evalctl, a local command-line check that scores what your agent did, and when to use Langfuse, an open-source observability platform that traces LLM applications and evaluates them across a team.
bucket: project
order: 13
draft: false
---

# evalctl and Langfuse

Both tools evaluate AI work, and both can grade a run with fixed checks or an AI
judge. So they look like alternatives. They sit at different layers. Langfuse is a
platform you host and a team logs into; evalctl is a single command you run on your
own machine. This page says which job each one fits.

**Research baseline.** Langfuse is an open-source observability and evaluation
platform; its docs are at [langfuse.com/docs](https://langfuse.com/docs/evaluation/overview).
The repository is `langfuse/langfuse`, main at commit
[`816e69d`](https://github.com/langfuse/langfuse/tree/816e69d4e21fdab9c32aa3872e1583aa03050297)
(checked 2026-09-20). Every Langfuse claim below links to that source.

## Which one to use

Use **evalctl** to check what your agent did to your own workspace, fast, with
nothing to set up. It installs with `pip`, needs no server, no database, and no
model API key to run. The agent under test can run it itself. Each run leaves one
results file you can commit, and another machine can re-check it offline.

Use **Langfuse** to trace an LLM application, watch it in production, and evaluate
it across a team. It records "live incoming traces," stores them, and scores them
with model-based, code-based, or human evaluators
([evaluation overview](https://langfuse.com/docs/evaluation/overview)). It is
observability-first: tracing is the foundation, and evaluation runs on top of it.

They fit together. evalctl is the quick local gate you run first, on your laptop or
in continuous integration. Langfuse is the traced record you keep on top, so a team
can watch production and track scores over time. This page doesn't argue for one
instead of the other.

## What each one is

evalctl is a single command-line tool. You run it, it runs your agent, and it
grades the result against the workspace the agent left behind: the files it wrote,
the diffs it made, the commands it ran, the exit codes. It runs in place, on your
machine, and it warns on every run that the runner isn't sandboxed.

Langfuse is a platform you run as a service. It is open source and can be
self-hosted with Docker, or used as Langfuse Cloud
([self-hosting](https://langfuse.com/self-hosting)). Self-hosting it means running
its two application containers against four stores: Postgres for transactional
data, ClickHouse for traces and scores, Redis for queues and cache, and S3-style
object storage for events. It traces an application's model calls, retrieval, and
tool use, then evaluates traces or datasets with model-based (LLM-as-judge),
code-based, or human evaluators
([evaluation overview](https://langfuse.com/docs/evaluation/overview)).

## Side by side

| Area | evalctl | Langfuse |
| --- | --- | --- |
| Built for | Checking what your agent did to your own workspace. | Tracing an LLM application and evaluating it across a team. |
| First job | Score a run and gate on it. | Record traces; evaluation runs on top. |
| Who runs it | The agent under test, or one person, with no setup. | A team, against a hosted or self-hosted server. |
| Unit under test | The workspace: files, diffs, commands, exit codes. | Traces and spans of model calls, plus dataset runs. |
| Where results live | One portable file on your machine, ready to commit. | The Langfuse server's databases, viewed in its UI. |
| Setup to run | `pip install evalctl`. No server, no database, no model key. | Langfuse Cloud, or self-host: Postgres, ClickHouse, Redis, object storage. |
| How it grades | Fixed checks over the workspace; an AI judge is optional and never the only check. | Model-based, code-based, or human evaluators. |
| Team history | None. Each run is a standalone file. | Kept over time: traces, scores, datasets, dashboards. |

Read this by column, not row by row. evalctl is the whole left column: small,
local, no server, pointed at your own repo, one file per run. Langfuse is the whole
right column: a traced, hosted service that keeps history for a team.

## What Langfuse does well

- **Production tracing.** Langfuse records live traces of an application's model
  calls, retrieval, and tool use, and evaluation runs against those traces
  ([evaluation overview](https://langfuse.com/docs/evaluation/overview)). evalctl
  scores a run you point it at; it does not watch production.
- **Open source and self-hostable.** You can run the whole platform on your own
  infrastructure with Docker, so your traces stay in your environment
  ([self-hosting](https://langfuse.com/self-hosting)). That is a real strength, and
  it is a platform to operate, not a single command.
- **Datasets, dashboards, and a team UI.** Stored datasets, annotation queues, and
  a shared dashboard are the platform's core. That is team workflow, and it is not
  evalctl's shape.

If your need is production tracing, stored history, and a shared UI for a team,
that is Langfuse's shape, not evalctl's.

## What evalctl does differently

- **No server, no database, no model key to run.** Install it and run it in a
  continuous integration job in seconds, with nothing to operate. evalctl scores
  what your agent already did, so a run costs no model calls by default. Self-hosted
  Langfuse is a service with four backing stores to run.
- **Pointed at your own workspace.** You don't instrument an application to emit
  traces. You point evalctl at the change your agent made and ask whether it still
  passes. That is a local gate on your work, not a stream of traces.
- **It scores the workspace, not the trace.** evalctl grades the files, diffs,
  commands, and exit codes an agent produced. Langfuse centers on traces and spans
  of model calls. The unit under test is different.
- **The agent runs it.** evalctl is built so the agent under test can call it, read
  the result, and continue. If it guesses a command wrong, the error tells it the
  right one to paste.
- **A results file another machine can re-check.** Hand the run directory to a
  second machine and it reproduces the report offline, with no server to stand up.
  The file records what went in, the checks that ran, and what they found.

## What is the same (don't sell a false difference)

Both tools can grade with plain, repeatable checks, and both offer an AI judge as
one option. Langfuse lists "Code Evaluators" for deterministic checks alongside
model-based ones ([evaluation overview](https://langfuse.com/docs/evaluation/overview)),
so "deterministic versus judge-based" is **not** the real difference between them.
The real differences are what the tool is built around — traces versus a workspace
gate — and whether you run a server at all.

## Using them together

Run evalctl as the quick local check while you work: score a change on a laptop or
in continuous integration, with no server and no model spend, and commit the results
file next to the code. When you want production traces, stored history, or a shared
dashboard across a team, send those to Langfuse on top. The evalctl file records how
one run was scored on your own repo — a different artifact than a Langfuse trace held
in the platform's databases.

## Don't say

- Don't say Langfuse can't run locally. It is open source and self-hostable with
  Docker; the difference is that it is a server with backing databases, not a single
  command.
- Don't say Langfuse only uses LLM judges. It offers code-based, model-based, and
  human evaluators.
- Don't call Langfuse just a tracing tool with no evaluation. Evaluation is a
  first-class part of the platform.
- Don't call evalctl a replacement for Langfuse. evalctl keeps no traces, no stored
  history, and no dashboard on purpose.
- Don't say evalctl scores traces the way Langfuse does. evalctl scores the
  workspace an agent changed — files, diffs, commands, exit codes.

## See also

- [All tool comparisons](/docs/comparisons/) — the full set, and which tool fits which question.

## Sources

- Langfuse evaluation overview (traces, datasets, evaluator types):
  [langfuse.com/docs/evaluation/overview](https://langfuse.com/docs/evaluation/overview)
- Langfuse self-hosting (open source, Docker, required infrastructure):
  [langfuse.com/self-hosting](https://langfuse.com/self-hosting)
- Langfuse repository, main at commit `816e69d`, 2026-09-20:
  [github.com/langfuse/langfuse](https://github.com/langfuse/langfuse/tree/816e69d4e21fdab9c32aa3872e1583aa03050297)
- evalctl scope and sandboxing: [How evalctl compares](/docs/comparison/),
  [Security and sandboxing](/docs/security/)
