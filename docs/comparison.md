---
title: How evalctl compares
description: When to use evalctl to check what your agent did to a workspace, and when to use promptfoo to test the text a model returns.
bucket: project
order: 1
---

# How evalctl compares

evalctl and promptfoo are both local eval tools you run from the command line, so
they get compared. They test different units. promptfoo tests the **text a model
returns** for a prompt. evalctl tests what an agent **did** to a workspace. This
page says which one fits your question.

**Research baseline.** promptfoo `promptfoo/promptfoo`, version 0.123.1, main at
commit [`d536238`](https://github.com/promptfoo/promptfoo/tree/d53623875190336fa85c383698693a06f928ad7b)
(dated 2026-09-20, checked 2026-09-20), MIT licensed. Every promptfoo claim below
links to that source.

## Which one to use

Use **evalctl** to check what your agent did to your own workspace: the files it
wrote, the diffs it made, the commands it ran, the exit codes. It installs with
`pip`, needs no model API key to run, and the agent under test can run it itself.
Each run leaves one results file you can commit.

Use **promptfoo** to test and compare the answers a model gives. You write a config
of prompts, run them across models and providers, and grade the completions with
assertions or a model rubric. promptfoo also does red teaming — adversarial and
safety testing of an LLM app — which evalctl does not.

They test different units. promptfoo asks "is this model's answer good?" evalctl
asks "did this agent change the workspace correctly?"

## What each one is

evalctl is a single command-line tool. You run it, it runs your agent, and it
grades the result against the workspace the agent left behind. It runs in place,
needs nothing beyond Python 3.11+, and warns on every run that the runner isn't
sandboxed.

promptfoo is an "LLM eval & red teaming" toolkit
([README](https://github.com/promptfoo/promptfoo/tree/d53623875190336fa85c383698693a06f928ad7b)).
You describe prompts and test cases in a config file, run them against one or more
model providers, and score the responses. It is a Node tool (Node.js >=22.22.0),
also installable through Homebrew and a pip wrapper, and most providers need an API
key. As of 2026 promptfoo is part of OpenAI and remains open source under MIT
([README](https://github.com/promptfoo/promptfoo/tree/d53623875190336fa85c383698693a06f928ad7b)).

## Side by side

| Area | evalctl | promptfoo (0.123.1) |
| --- | --- | --- |
| Unit under test | One agent run against your repo. | A prompt and the model's completion, across providers. |
| Scoring surface | The workspace: files, diffs, commands, exit codes, and text. | The text of a response. |
| How it grades | Fixed checks over the workspace; an AI judge is a roadmap item, not shipped. | Deterministic assertions or a model-graded rubric. |
| Model API key to run | Not needed by default. | Needed; it runs prompts against a provider. |
| Also does | Nothing else; it is one small tool. | Red teaming and adversarial testing of LLM apps. |
| Runtime | Python 3.11+. | Node.js >=22.22.0 (also Homebrew, pip wrapper). |
| Who runs it | The agent under test, or one person. | A person authoring prompt tests. |
| What a run leaves | One portable results file another machine can re-check. | A results view and stored eval records. |

## What promptfoo does well

- **Comparing models on the same prompts.** Its shape — one config, many providers
  — is built to ask which model answers your prompts better
  ([README](https://github.com/promptfoo/promptfoo/tree/d53623875190336fa85c383698693a06f928ad7b)).
  evalctl is not.
- **Grading response text.** Deterministic assertions and model-graded rubrics
  score the content of a completion, which is the right tool when the thing you
  ship is a prompt and its answer.
- **Red teaming.** promptfoo tests an LLM app for adversarial and safety failures,
  a whole capability evalctl doesn't have.

If your unit under test is a prompt and its answer, that is promptfoo's shape, not
evalctl's.

## What evalctl does differently

- **It grades the workspace, not the response.** evalctl checks the files, diffs,
  commands, and exit codes your agent produced. promptfoo grades the text a model
  returned. If your agent edits code or changes a workspace, that result is what
  evalctl scores.
- **No model API key to run.** evalctl scores what your agent already did, so a run
  costs no model calls by default. promptfoo runs prompts against a provider and
  needs a key.
- **The agent runs it.** evalctl is built so the agent under test can call it, read
  the result, and continue. promptfoo is authored and run by a person.
- **A results file another machine can re-check.** Hand the run directory to a
  second machine and it reproduces the report offline.

## What is the same (don't sell a false difference)

Both are local CLIs with no required server or account, and both can grade
deterministically: promptfoo ships plain assertions alongside its model-graded
rubric. So "local versus hosted" and "deterministic versus judge" are not the real
difference. The real difference is the unit under test — a model's response, versus
an agent's effect on a workspace.

## Using them together

Use promptfoo to test the prompts and responses in your app: compare providers,
assert on the text, red-team the behavior. Use evalctl to check that the agent you
built on top did the right thing to your repo. One grades what the model said; the
other grades what the agent did.

## Not in this release

Some evalctl capabilities named elsewhere are roadmap items, not shipped commands:
compare tooling, [inferctl](https://inferctl.dev) route capture, externally managed
shared worker fleets, and LLM-as-judge scoring. See the [changelog](/docs/changelog/).

## Don't say

- Don't say promptfoo can only grade with a model. It ships deterministic
  assertions too.
- Don't say promptfoo is hosted or needs an account. It is a local, open-source
  CLI.
- Don't say evalctl grades response text or compares models. It scores what an
  agent did to a workspace.
- Don't say evalctl does red teaming. It doesn't; that is promptfoo's ground.
- Don't call evalctl a replacement for promptfoo. They test different units.

## Sources

- promptfoo repository, version 0.123.1, main at commit `d536238`, dated
  2026-09-20, checked 2026-09-20:
  [github.com/promptfoo/promptfoo](https://github.com/promptfoo/promptfoo/tree/d53623875190336fa85c383698693a06f928ad7b)
- evalctl scorers (deterministic checks; judge on the roadmap):
  [Scorers and command scorers](/docs/command-scorers/)
- evalctl security and sandboxing: [Security and sandboxing](/docs/security/)
- evalctl roadmap items: [Changelog](/docs/changelog/)
