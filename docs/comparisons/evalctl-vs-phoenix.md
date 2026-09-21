---
title: evalctl and Arize Phoenix
description: When to use evalctl, a local command-line check that scores what your agent did, and when to use Arize Phoenix, an open-source observability platform that traces LLM applications and evaluates their spans.
bucket: project
order: 14
draft: false
---

# evalctl and Arize Phoenix

Both tools evaluate AI work, and both can grade a run with fixed checks or an AI
judge. So they look like alternatives. They sit at different layers. Phoenix is an
observability server you send traces to and open in a browser; evalctl is a single
command you run on your own machine. This page says which job each one fits.

**Research baseline.** Arize Phoenix is an open-source observability and evaluation
platform; its docs are at [arize.com/docs/phoenix](https://arize.com/docs/phoenix).
The repository is `Arize-ai/phoenix`, main at commit
[`767847c`](https://github.com/Arize-ai/phoenix/tree/767847c3386492f9783b623cf78e3eee47d0cea3)
(checked 2026-09-20). Every Phoenix claim below links to that source.

## Which one to use

Use **evalctl** to check what your agent did to your own workspace, fast, with
nothing to set up. It installs with `pip`, needs no server and no model API key to
run. The agent under test can run it itself. Each run leaves one results file you
can commit, and another machine can re-check it offline.

Use **Phoenix** to trace an LLM application step by step and score its spans. You
start Phoenix and "leave it running" at `http://localhost:6006`, send it traces,
and inspect model calls, retrieval, and tool use in its UI
([Phoenix docs](https://arize.com/docs/phoenix)). It is observability-first:
tracing is primary, and evaluation scores the traces and spans it captured.

They fit together. evalctl is the quick local gate you run first, on your laptop or
in continuous integration. Phoenix is the trace viewer you send runs to when you
need to see, step by step, what the application did. This page doesn't argue for one
instead of the other.

## What each one is

evalctl is a single command-line tool. You run it, it runs your agent, and it
grades the result against the workspace the agent left behind: the files it wrote,
the diffs it made, the commands it ran, the exit codes. It runs in place, on your
machine, and it warns on every run that the runner isn't sandboxed.

Phoenix is an observability platform. It is open source and self-hostable on
"Docker, Kubernetes, or your cloud of choice," and it also runs locally as a server
you leave running ([Phoenix docs](https://arize.com/docs/phoenix)). "Tracing lets
you see what happened during a single run of your AI application, step by step," and
its evaluations "score traces & spans with LLM-based evaluators, code-based checks,
or human labels." Arize also offers a managed version, Arize AX, built on the same
open standards.

## Side by side

| Area | evalctl | Phoenix |
| --- | --- | --- |
| Built for | Checking what your agent did to your own workspace. | Tracing an LLM application step by step and scoring its spans. |
| First job | Score a run and gate on it. | Capture traces; evaluation scores them. |
| Who runs it | The agent under test, or one person, with no setup. | A person or team, against a running Phoenix server. |
| Unit under test | The workspace: files, diffs, commands, exit codes. | Traces and spans of model calls, retrieval, and tool use. |
| Where results live | One portable file on your machine, ready to commit. | The Phoenix server, viewed in its UI at `localhost:6006`. |
| Setup to run | `pip install evalctl`. No server, no model key. | Start and leave a Phoenix server running; instrument the app to send traces. |
| How it grades | Fixed checks over the workspace; an AI judge is optional and never the only check. | LLM-based evaluators, code-based checks, or human labels. |
| Team history | None. Each run is a standalone file. | Kept in the server: traces, spans, datasets, experiments. |

Read this by column, not row by row. evalctl is the whole left column: small,
local, no server, pointed at your own repo, one file per run. Phoenix is the whole
right column: a running server that captures traces and scores their spans.

## What Phoenix does well

- **Step-by-step traces.** Phoenix shows "what happened during a single run of your
  AI application, step by step," capturing model calls, retrieval, and tool use
  ([Phoenix docs](https://arize.com/docs/phoenix)). evalctl scores the outcome of a
  run; it does not display the steps inside it.
- **Open source and self-hostable.** You can run Phoenix on your own infrastructure,
  or locally as a server, so your traces stay in your environment
  ([Phoenix docs](https://arize.com/docs/phoenix)). That is a real strength, and it
  is a server to run, not a single command.
- **Span-level scoring and integrations.** Phoenix scores traces and spans and plugs
  into evaluator libraries such as Ragas, DeepEval, and Cleanlab
  ([Phoenix docs](https://arize.com/docs/phoenix)). That is a different granularity
  than a single workspace gate.

If your need is step-by-step traces and span-level scoring for an application, that
is Phoenix's shape, not evalctl's.

## What evalctl does differently

- **No server to run, no model key to run.** Install it and run it in a continuous
  integration job in seconds, with nothing to keep running. evalctl scores what your
  agent already did, so a run costs no model calls by default. Phoenix wants a
  server up and the application instrumented to send it traces.
- **Pointed at your own workspace.** You don't instrument an application to emit
  spans. You point evalctl at the change your agent made and ask whether it still
  passes. That is a local gate on your work, not a trace stream.
- **It scores the workspace, not the spans.** evalctl grades the files, diffs,
  commands, and exit codes an agent produced. Phoenix centers on traces and spans of
  model calls. The unit under test is different.
- **The agent runs it.** evalctl is built so the agent under test can call it, read
  the result, and continue. If it guesses a command wrong, the error tells it the
  right one to paste.
- **A results file another machine can re-check.** Hand the run directory to a
  second machine and it reproduces the report offline, with no server to stand up.
  The file records what went in, the checks that ran, and what they found.

## What is the same (don't sell a false difference)

Both tools can grade with plain, repeatable checks, and both offer an AI judge as
one option. Phoenix scores spans with "code-based checks" alongside LLM-based
evaluators ([Phoenix docs](https://arize.com/docs/phoenix)), so "deterministic
versus judge-based" is **not** the real difference between them. The real
differences are what the tool is built around — traces and spans versus a workspace
gate — and whether you run a server at all.

## Using them together

Run evalctl as the quick local check while you work: score a change on a laptop or
in continuous integration, with no server and no model spend, and commit the results
file next to the code. When you want to see the steps inside a run or score its
spans, send the run to Phoenix on top. The evalctl file records how one run was
scored on your own repo — a different artifact than a Phoenix trace held in the
server.

## Don't say

- Don't say Phoenix can't run locally. It runs as a local server at
  `localhost:6006` and self-hosts on Docker or Kubernetes; the difference is that it
  is a server to keep running, not a single command.
- Don't say Phoenix only uses LLM judges. It scores spans with LLM-based evaluators,
  code-based checks, or human labels.
- Don't call Phoenix just a tracing tool with no evaluation. Span-level evaluation
  is a first-class part of it.
- Don't call evalctl a replacement for Phoenix. evalctl keeps no traces, shows no
  step-by-step spans, and has no UI on purpose.
- Don't say evalctl scores spans the way Phoenix does. evalctl scores the workspace
  an agent changed — files, diffs, commands, exit codes.

## See also

- [All tool comparisons](/docs/comparisons/) — the full set, and which tool fits which question.

## Sources

- Arize Phoenix docs (tracing, span evaluation, self-hosting, local server):
  [arize.com/docs/phoenix](https://arize.com/docs/phoenix)
- Phoenix repository, main at commit `767847c`, 2026-09-20:
  [github.com/Arize-ai/phoenix](https://github.com/Arize-ai/phoenix/tree/767847c3386492f9783b623cf78e3eee47d0cea3)
- evalctl scope and sandboxing: [How evalctl compares](/docs/comparison/),
  [Security and sandboxing](/docs/security/)
