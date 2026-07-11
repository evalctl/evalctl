# evalctl

**Local-first evals for agents, not just prompts.**

evalctl treats eval cases as files, runners as shell commands, and results as
durable artifacts. It scores what agents actually do — files written, diffs
produced, commands run — on your own machine, with no gateway, dashboard, or
SaaS account.

Execution can be delegated to [spoolctl](https://github.com/Ozhiaki/spoolctl)
for crash-safe async runs; model selection can be delegated to `inferctl` for
local-inference-stack awareness. Neither is required.

## How it differs from promptfoo

promptfoo is the incumbent local eval CLI, and it is prompt/chat-shaped.
evalctl is agent-shaped.

| Axis | promptfoo | evalctl |
| --- | --- | --- |
| Unit under test | prompt → completion | agent run → files, diffs, command logs, artifacts |
| Scoring surface | text of a response | resulting workspace: git diff, expected/forbidden file changes, exit codes, plus text |
| Execution | in-process, synchronous | optional spoolctl: bounded concurrency, retries, dead-letter, resume failed across a crash |
| Model context | provider API keys | optional inferctl: local backend route + preflight recorded per case |

## Status

Pre-code. This repo currently holds the positioning
([evalctl-positioning.md](evalctl-positioning.md)). v0.1 targets one code-review
eval suite, end to end, with spoolctl and inferctl wired in — scoring agent
behavior on real diffs, not prompt regression.

## Home

- Site: evalctl.dev
- Org: https://github.com/evalctl
