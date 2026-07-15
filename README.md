# evalctl

**Local-first evals for agents, not just prompts.**

evalctl treats eval cases as files, runners as shell commands, and results as
durable artifacts. It scores what agents actually do — files written, diffs
produced, commands run — on your own machine, with no gateway, dashboard, or
SaaS account.

v0.1 runs synchronously in-process and writes self-contained run directories.
The contract leaves room for future [spoolctl](https://github.com/Ozhiaki/spoolctl)
and `inferctl` integrations, but neither is required or implemented in v0.1.

## How it differs from promptfoo

promptfoo is the incumbent local eval CLI, and it is prompt/chat-shaped.
evalctl is agent-shaped.

| Axis | promptfoo | evalctl |
| --- | --- | --- |
| Unit under test | prompt → completion | agent run → files, diffs, command logs, artifacts |
| Scoring surface | text of a response | resulting workspace: git diff, expected/forbidden file changes, exit codes, plus text |
| Execution | in-process, synchronous | v0.1 synchronous; spoolctl async is deferred |
| Model context | provider API keys | inferctl route/preflight provenance is deferred |

## Status

Python implementation in progress. v0.1 targets one synchronous code-review eval
suite end to end: scaffold, validate, run, status, report, deterministic local
scorers, and artifact replay from a copied run directory.

## Home

- Site: evalctl.dev
- Org: https://github.com/evalctl
