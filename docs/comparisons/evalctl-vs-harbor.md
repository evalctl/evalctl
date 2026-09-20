---
title: evalctl and Harbor
description: When to use evalctl, a local command-line check that scores what your agent did, and when to use Harbor, a container-based harness for running agent benchmarks at scale.
bucket: project
order: 3
draft: false
---

# evalctl and Harbor

Both tools install with `pip`, both evaluate what an agent did rather than the
text a model returned, and both score with plain checks by default. So they look
alike. They are built for different jobs. This page says which job each one fits.

**Research baseline.** Harbor `harbor-framework/harbor`, release v0.23.0, main at
commit [`71c77fd`](https://github.com/harbor-framework/harbor/tree/71c77fdd119df12eb6ab56e5bc0f29bf62fad338)
(checked 2026-09-20). Every Harbor claim below links to that source.

## Which one to use

Use **evalctl** to check what your agent did to your own workspace, fast, with
nothing to set up. It installs with `pip`, needs no Docker, no server, no account,
and no model API key to run. The agent under test can run it itself. Each run
leaves one results file you can commit and another machine can re-check.

Use **Harbor** to run agents against reproducible benchmark tasks inside isolated
containers, at scale. Harbor gives each run its own clean machine, and it can run
thousands of runs in parallel across cloud sandboxes. It is the harness behind
benchmarks like Terminal-Bench.

They fit together. evalctl is the quick local check you run first; Harbor is the
container-isolated, large-scale harness you reach for when you need reproducible
benchmark runs. This page doesn't argue for one instead of the other.

## What each one is

evalctl is a single command-line tool. You run it, it runs your agent, and it
grades the result against the workspace the agent left behind: the files it wrote,
the diffs it made, the commands it ran, the exit codes. It runs in place, on your
machine, and it warns on every run that the runner isn't sandboxed.

Harbor is a framework "from the creators of Terminal-Bench for evaluating and
optimizing agents and language models"
([repository](https://github.com/harbor-framework/harbor/tree/71c77fdd119df12eb6ab56e5bc0f29bf62fad338)).
It runs "any agent with any model on any task in any sandbox in parallel"
([docs](https://docs.harborframework.com/)). Each task is a self-contained
directory with its own container, and Harbor isolates every run so trials never
share state.

LangChain is not the maker of Harbor. LangChain integrates with it — you can run a
LangGraph agent inside Harbor and view results in LangSmith — but Harbor is a
separate project ([LangChain's write-up](https://www.langchain.com/blog/unified-stack-for-evaluating-agents)).

## Side by side

| Area | evalctl | Harbor (v0.23.0) |
| --- | --- | --- |
| Built for | Checking what your agent did to your own workspace. | Running agents against reproducible benchmark tasks at scale. |
| Who runs it | The agent under test, or one person, with no setup. | A person running a task suite or benchmark. |
| Isolation | Runs in place; warns on every run that the runner isn't sandboxed. | A container per run: local Docker, or a cloud sandbox for parallel scale. |
| Scale | One run, locally, as a gate. | Hundreds or thousands of runs in parallel across cloud sandboxes. |
| How it grades | Fixed checks over the workspace; an AI judge is optional and never the only check. | A `test.sh` script runs after the agent and writes a score; an AI judge is optional. |
| What you point it at | Your own repository and agent. | A self-contained task directory (instructions, Dockerfile, solution, tests). |
| Setup to run | `pip install evalctl`. No container, no account, no model key. | `pip install harbor`, plus Docker or a cloud sandbox, plus a model API key. |
| What a run leaves | One portable results file another machine can re-check offline. | A trial record you view in Harbor's viewer or LangSmith. |

Read this by column, not row by row. evalctl is the whole left column: small,
local, no container, pointed at your own repo. Harbor is the whole right column: a
container per run, built to scale across many tasks and machines.

## What Harbor does well

- **A clean machine for every run.** Each trial runs in its own container, so
  trials never share state
  ([tasks: environment](https://docs.harborframework.com/core-concepts/tasks/environment.md)).
  evalctl doesn't match that isolation, and says so on every run.
- **Scale.** Harbor runs many trials in parallel across cloud sandboxes such as
  Daytona and Modal, so you can run a whole benchmark at once instead of one run
  at a time ([docs](https://docs.harborframework.com/)).
- **Reusable benchmark tasks.** A Harbor task is a self-contained directory —
  instructions, a Dockerfile, a solution, and tests — that anyone can rerun
  ([create a task](https://docs.harborframework.com/tutorials/create-a-task.md)).
  It plugs into standard benchmarks like Terminal-Bench, SWE-Bench, and Aider
  Polyglot.

If your need is reproducible, container-isolated benchmark runs at scale, that is
Harbor's shape, not evalctl's.

## What evalctl does differently

- **No container, no account, no model key to run.** Install it and run it in a
  continuous integration job in seconds. Harbor needs Docker or a cloud sandbox,
  and its built-in agent needs a model API key. evalctl scores what your agent
  already did, so a run costs no model calls by default.
- **Pointed at your own repository.** You don't author a self-contained benchmark
  task to use evalctl. You point it at the change your agent made and ask whether
  it still passes. That is a local gate on your work, not a benchmark you publish.
- **The agent runs it.** evalctl is built so the agent under test can call it,
  read the result, and continue. If it guesses a command wrong, the error tells it
  the right one to paste.
- **A results file another machine can re-check.** Hand the run directory to a
  second machine and it reproduces the report, with no container image to rebuild.
  The file records what went in, the checks that ran, and what they found.

## What is the same (don't sell a false difference)

Both grade with plain, repeatable checks by default. Harbor's verifier is a
`test.sh` script that runs after the agent and writes a reward — usually `1` or
`0` — and "task authors can implement `test.sh` to use whatever verification
approach they like," including pure pytest with no model involved
([verifier](https://docs.harborframework.com/core-concepts/tasks/verifier.md)). An
AI judge is one optional method in both tools, not Harbor's required path. So
"deterministic versus judge-based" is **not** a real difference between them. The
real differences are isolation, scale, and what you point the tool at.

## Using them together

Run evalctl as the quick local check while you work: score a change on a laptop or
in continuous integration, with no container and no model spend. When you need
reproducible, isolated runs across many tasks or machines, that is Harbor's job.
The results file evalctl leaves records how one run was scored on your own repo — a
different artifact than a Harbor trial in a sandboxed benchmark task.

## Don't say

- Don't call Harbor a LangChain product. LangChain integrates with Harbor; it
  didn't build it.
- Don't say Harbor leans on an AI judge. Its default verifier is a `test.sh`
  script, and the judge is optional.
- Don't call evalctl a replacement for Harbor. evalctl gives up container
  isolation and large-scale parallel runs on purpose.
- Don't say evalctl sandboxes runs like Harbor. It warns on every run that the
  runner isn't sandboxed.
- Don't claim evalctl runs your agent for you the way Harbor's built-in agent
  does. evalctl scores an agent run; it doesn't need a model key to do so.

## See also

- [All tool comparisons](/docs/comparisons/) — the full set, and which tool fits which question.

## Sources

- Harbor repository, main at commit `71c77fd`, 2026-09-20:
  [github.com/harbor-framework/harbor](https://github.com/harbor-framework/harbor/tree/71c77fdd119df12eb6ab56e5bc0f29bf62fad338)
- Harbor docs (tagline, sandboxes): [docs.harborframework.com](https://docs.harborframework.com/)
- Harbor verifier (`test.sh`, reward, optional judge):
  [core-concepts/tasks/verifier](https://docs.harborframework.com/core-concepts/tasks/verifier.md)
- Harbor task authoring:
  [tutorials/create-a-task](https://docs.harborframework.com/tutorials/create-a-task.md)
- LangChain's Harbor integration:
  [langchain.com/blog/unified-stack-for-evaluating-agents](https://www.langchain.com/blog/unified-stack-for-evaluating-agents)
- evalctl scope and sandboxing: [How evalctl compares](/docs/comparison/),
  [Security and sandboxing](/docs/security/)
