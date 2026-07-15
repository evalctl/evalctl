# evalctl

**Local-first evals for agents, not just prompts.**

evalctl treats eval cases as files, runners as shell commands, and results as
durable artifacts. It scores what agents actually do — files written, diffs
produced, commands run — on your own machine, with no gateway, dashboard, or
SaaS account.

v0.1 runs synchronously in-process and writes self-contained run directories.
The contract leaves room for future [spoolctl](https://github.com/Ozhiaki/spoolctl)
and [inferctl](https://inferctl.dev) integrations, but neither is required or
implemented in v0.1.

## About

evalctl is an agent-first evaluation harness for local workflows. It runs eval
cases as filesystem fixtures, invokes agents through ordinary runner commands,
and grades the resulting workspace with deterministic scorers. The core artifact
is a portable run directory that another agent can inspect, report on, and
re-score without access to the original shell history.

Keywords: agent evals, LLM evaluation, AI agents, local-first, CLI, code review,
workspace diff, deterministic scoring, artifact replay, eval harness.

## How it differs from promptfoo

[promptfoo](https://www.promptfoo.dev/) is the incumbent local eval CLI, and it
is prompt/chat-shaped.
evalctl is agent-shaped.

| Axis | promptfoo | evalctl |
| --- | --- | --- |
| Unit under test | prompt → completion | agent run → files, diffs, command logs, artifacts |
| Scoring surface | text of a response | resulting workspace: git diff, expected/forbidden file changes, exit codes, plus text |
| Execution | in-process, synchronous | v0.1 synchronous; spoolctl async is deferred |
| Model context | provider API keys | [inferctl](https://inferctl.dev) route/preflight provenance is deferred |

## Status

Python implementation in progress. v0.1 targets one synchronous code-review eval
suite end to end: scaffold, validate, run, status, report, deterministic local
scorers, and artifact replay from a copied run directory.

## Home

- Site: [evalctl.dev](https://evalctl.dev)
- Org: [github.com/evalctl](https://github.com/evalctl)
