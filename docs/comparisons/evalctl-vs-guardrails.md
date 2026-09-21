---
title: evalctl and Guardrails (Guardrails AI, NeMo Guardrails)
description: When to use evalctl, a local command-line check that runs your agent and scores what it did to a workspace, and when to use runtime guardrails like Guardrails AI or NeMo Guardrails, which validate an LLM's inputs and outputs live in the request path. Different jobs.
bucket: project
order: 21
draft: false
---

# evalctl and Guardrails (Guardrails AI, NeMo Guardrails)

These solve different problems. A guardrail sits in the request path and checks an
LLM's input or output **while your app is running**, to block or fix a bad
response before a user sees it. evalctl runs **after the fact**: it runs your agent
and scores what it did to a workspace, as a gate on a change. One is a live
safety net; the other is an offline check. This page keeps them apart.

**Research baseline.** Two common runtime guardrail toolkits:
- **Guardrails AI** — repository `guardrails-ai/guardrails`, main at commit
  [`06d0ff2`](https://github.com/guardrails-ai/guardrails/tree/06d0ff2c5f9bcb493d976b76f885e37e41ce845d),
  Apache-2.0.
- **NeMo Guardrails** — repository `NVIDIA-NeMo/Guardrails`, `develop` at commit
  [`54692ca`](https://github.com/NVIDIA-NeMo/Guardrails/tree/54692ca7fbb1d97855d8991e97dcaa6fd1a1ae16),
  release 0.24.1, Apache-2.0.

(Checked 2026-09-20. Every guardrail claim below links to one of these.)

## Which one to use

Use a **runtime guardrail** when you need to control an LLM's behavior live, in
your application. Guardrails AI "runs Input/Output Guards in your application that
detect, quantify and mitigate the presence of specific types of risks," combining
validators from its Hub into a single Guard
([repository](https://github.com/guardrails-ai/guardrails/tree/06d0ff2c5f9bcb493d976b76f885e37e41ce845d)).
NeMo Guardrails adds "programmable guardrails between the application code and the
LLM," steering the model away from unwanted topics and along predefined dialog
paths
([repository](https://github.com/NVIDIA-NeMo/Guardrails/tree/54692ca7fbb1d97855d8991e97dcaa6fd1a1ae16)).
Both act in the request path, at serving time.

Use **evalctl** to check what your agent did to a workspace, offline: the files it
wrote, the diffs it made, the commands it ran, the exit codes. It installs with
`pip`, needs no account, and leaves one results file you can commit and another
machine can re-check. It runs as a gate on a change, not as middleware in a live
request.

They answer different questions. A guardrail asks, "is this one response safe to
return right now?" evalctl asks, "did this change break what my agent does to my
repo?"

## What each one is

A runtime guardrail is middleware. It wraps the model call and inspects the input
before it reaches the model and the output before it reaches the user, then
allows, blocks, or rewrites. Guardrails AI does this with validators — regex,
competitor checks, toxic-language detection, and more — installed from its Hub.
NeMo Guardrails does it with "rails" that enforce topic limits, dialog flows, and
safe tool use. Both live inside the serving path of a running application.

evalctl is a single command-line tool that runs your agent and grades the result
against the workspace it left behind. It runs in place, on your machine, and warns
on every run that the runner isn't sandboxed. It is not in the request path of
your app; it is the check you run on a change before you ship it.

## Side by side

| Area | evalctl | Guardrails AI / NeMo Guardrails |
| --- | --- | --- |
| When it acts | Offline, after the run, as a gate on a change. | Live, in the request path, per model call. |
| What it inspects | The workspace an agent changed. | An LLM's input and output text. |
| What it does on a problem | Reports a failing check. | Blocks, mitigates, or rewrites the response. |
| The question | Did this change break what my agent does here? | Is this response safe to return right now? |
| Where it lives | Your terminal or continuous integration. | Inside your running application. |
| Setup to run | `pip install evalctl`. No account, no server. | Installed into your app's request handling. |

Read this by column. A guardrail is a live filter on responses. evalctl is an
offline gate on what an agent did. They never do the same job.

## What runtime guardrails do well

- **They act in real time.** A guardrail catches a bad input or output before the
  user sees it and can block or fix it live
  ([Guardrails AI](https://github.com/guardrails-ai/guardrails/tree/06d0ff2c5f9bcb493d976b76f885e37e41ce845d)).
  evalctl runs after the fact and reports; it does not intercept a live response.
- **They control conversation and topic.** NeMo Guardrails steers a model along
  predefined dialog paths and away from unwanted topics
  ([NeMo Guardrails](https://github.com/NVIDIA-NeMo/Guardrails/tree/54692ca7fbb1d97855d8991e97dcaa6fd1a1ae16)).
  evalctl says nothing about a live conversation.
- **A library of validators.** Guardrails AI's Hub ships reusable validators you
  compose into a Guard. evalctl scores a workspace, not response text against a
  validator set.

If your need is to control an LLM's inputs and outputs live in production, that is
a guardrail's job, not evalctl's.

## What evalctl does differently

- **It scores actions, not response text.** evalctl grades the files, diffs,
  commands, and exit codes an agent produced. A guardrail inspects an LLM's input
  or output string. The unit under test is different.
- **It runs offline, as a gate.** evalctl checks a change before you ship it, in
  a terminal or continuous integration. A guardrail runs on every live request.
- **A results file another machine can re-check.** Hand the run directory to a
  second machine and it reproduces the report offline.
- **The agent runs it.** evalctl is built so the agent under test can call it,
  read the result, and continue.

## What is the same (don't sell a false difference)

Both care about whether an AI system behaves, and both can use fixed, code-based
checks rather than a judge model. So "deterministic versus judge-based" is **not**
the difference. The difference is *when* and *what*: a guardrail filters a live
response in the request path; evalctl grades, offline, what an agent already did
to a workspace. A live safety net and an offline gate are different jobs.

## Using them together

They complement each other cleanly. Run guardrails in production to keep live
responses in bounds, and run evalctl in continuous integration to check that a
change to your agent still does the right thing to your repo. One protects the
user in the moment; the other protects the codebase before you ship. Neither
removes the need for the other.

## Don't say

- Don't call a runtime guardrail an offline eval tool. Guardrails AI and NeMo
  Guardrails act live, in the request path.
- Don't say guardrails score what an agent did to a workspace. They inspect an
  LLM's input and output text.
- Don't call evalctl a guardrail or say it blocks live responses. It runs offline
  and reports; it does not sit in the request path.
- Don't say the two compete. A live filter and an offline gate solve different
  problems.
- Don't imply evalctl controls conversations or topics. It grades a workspace an
  agent changed, not a live dialog.

## See also

- [All tool comparisons](/docs/comparisons/) — the full set, and which tool fits which question.
- [evalctl and LLM-as-judge](/docs/comparisons/evalctl-vs-llm-judge/) — when a fixed
  check beats asking another model, and when a judge still earns its place.

## Sources

- Guardrails AI (Input/Output Guards and validators that run in your application),
  `guardrails-ai/guardrails` main at commit `06d0ff2`, 2026-09-20:
  [github.com/guardrails-ai/guardrails](https://github.com/guardrails-ai/guardrails/tree/06d0ff2c5f9bcb493d976b76f885e37e41ce845d)
- NeMo Guardrails (programmable guardrails between application code and the LLM),
  `NVIDIA-NeMo/Guardrails` `develop` at commit `54692ca`, release 0.24.1,
  2026-09-20:
  [github.com/NVIDIA-NeMo/Guardrails](https://github.com/NVIDIA-NeMo/Guardrails/tree/54692ca7fbb1d97855d8991e97dcaa6fd1a1ae16)
- evalctl scope: [How evalctl compares](/docs/comparison/),
  [Security and sandboxing](/docs/security/)
