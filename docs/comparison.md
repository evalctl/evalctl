---
title: How evalctl compares
description: evalctl vs. promptfoo — agent-shaped evals vs. prompt-shaped evals.
bucket: project
order: 1
---

# How evalctl compares

[promptfoo](https://www.promptfoo.dev/) is the incumbent local eval CLI, and it
is **prompt/chat-shaped**: the unit under test is a prompt and its completion.
evalctl is **agent-shaped**: the unit under test is an agent run and the
workspace it leaves behind.

| Axis | promptfoo | evalctl |
| --- | --- | --- |
| Unit under test | prompt → completion | agent run → files, diffs, command logs, artifacts |
| Scoring surface | text of a response | resulting workspace: git diff, expected/forbidden file changes, exit codes, plus text |
| Execution | in-process, synchronous | synchronous by default; `plan` previews actions, `doctor` diagnoses state, `run --resume` resumes crashed runs; optional `--queue spoolctl` delegates runner execution |
| Model context | provider API keys | inferctl route/preflight provenance is deferred |

## The core difference

promptfoo grades the *text* a model returns. evalctl grades what an agent *did*:
the files it wrote, the diff it produced, the commands it ran, the exit codes. If
your unit under test is a coding agent or a tool-using agent rather than a single
completion, the workspace is the thing worth scoring.

## Local-first, no service

Both are local CLIs. evalctl has **no gateway, dashboard, or SaaS account**, no
runtime dependencies beyond Python 3.11+, and its core deliverable is a portable
run directory another agent can re-score offline.

## Not in this release

Compare tooling, [inferctl](https://inferctl.dev) route capture, externally
managed shared worker fleets, and LLM-as-judge scoring are roadmap items, not
shipped commands. See the [changelog](/docs/changelog/).
