---
title: evalctl and DeepEval
description: When to use evalctl to check what your agent did to a workspace, and when to use DeepEval to score the quality of an LLM or RAG application's outputs.
bucket: project
order: 4
draft: true
---

# evalctl and DeepEval

Both tools evaluate AI systems, and both run locally, so they get compared. They
score different things. DeepEval scores the quality of what a model or agent
**said**. evalctl scores what an agent **did** to a workspace. This page says
which one fits your question.

**Research baseline.** DeepEval `confident-ai/deepeval`, release
[`python-v4.2.0`](https://github.com/confident-ai/deepeval/releases/tag/python-v4.2.0),
main at commit [`23901cf`](https://github.com/confident-ai/deepeval/tree/23901cfc5fe4f61c3ca745f67748e00699d0484f)
(checked 2026-09-20). Every DeepEval claim below links to that source.

## Which one to use

Use **evalctl** to check what your agent did to your own workspace: the files it
wrote, the diffs it made, the commands it ran, the exit codes. Its checks are
deterministic and give the same result every time, it needs no model API key to
run, and each run leaves one results file you can commit.

Use **DeepEval** to score the quality of an LLM or RAG application's outputs:
whether an answer is relevant, faithful to its sources, free of hallucination, or
safe. It ships 50+ ready-made metrics, most of them powered by an LLM judge, and
you write evaluations as pytest-style tests in Python.

They cover different halves of the same pipeline. For a coding agent, DeepEval can
grade the reasoning and the answer; evalctl grades the code that resulted. This
page doesn't argue for one instead of the other.

## What each one is

evalctl is a single command-line tool. You run it, it runs your agent, and it
grades the result against the workspace the agent left behind. It runs in place
and warns on every run that the runner isn't sandboxed. The agent under test can
run it itself.

DeepEval is "an open-source LLM evaluation framework for LLM applications"
([docs](https://deepeval.com/docs/introduction)). You import it and write test
cases in Python, in the shape of pytest tests. A test case carries fields like
`input`, `actual_output`, `expected_output`, `retrieval_context`, and
`tools_called`, and a metric scores those fields
([metrics](https://deepeval.com/docs/metrics-introduction)). For agents, DeepEval
traces "the complete chain of plans, model calls, tools, handoffs, and
intermediate steps." It reads that trace — not a file system
([docs](https://deepeval.com/docs/introduction)).

## Side by side

| Area | evalctl | DeepEval (v4.2.0) |
| --- | --- | --- |
| What it scores | The workspace the agent left: files, diffs, commands, exit codes, and text. | The model or agent's outputs and trace: answers, retrieval context, tool calls, plans, handoffs. |
| Sees the file system | Yes — that is the point. | No. It reads outputs and traces, "rather than directly monitoring file systems or workspaces." |
| How it grades | Deterministic checks by default; an AI judge is optional and never the only check. | 50+ ready-made metrics; "almost all" use an LLM judge (G-Eval, DAG, QAG). |
| Model API key to run | Not needed by default. | Needed by default; most metrics call an LLM judge. |
| Shape | One command-line tool. | A Python framework you import and write tests with. |
| Who runs it | The agent under test, or one person. | A developer writing pytest-style test cases. |
| Best at | Did the agent change the workspace correctly? | Is the generated answer relevant, faithful, and safe? |
| Local and open | Yes; runs offline, leaves a portable results file. | Yes, local-first and open source; pairs with the Confident AI cloud for dashboards. |

## What DeepEval does well

- **Scoring answer quality.** DeepEval's strength is judging the *content* of a
  response — relevancy, faithfulness to sources, hallucination, bias, toxicity —
  with research-backed metrics you don't have to write
  ([metrics](https://deepeval.com/docs/metrics-introduction)). evalctl does none
  of this.
- **RAG evaluation.** Metrics for retrieval quality (faithfulness, contextual
  recall and precision) make DeepEval a strong fit for RAG pipelines, a case
  evalctl doesn't cover.
- **Agent trajectories.** DeepEval can trace and score an agent's chain of plans,
  model calls, tools, and handoffs as one trajectory
  ([docs](https://deepeval.com/docs/introduction)). If you want to grade *how* the
  agent reasoned, that is DeepEval's ground.

If your question is whether a generated answer is good, that is DeepEval's shape,
not evalctl's.

## What evalctl does differently

- **It grades the workspace, not the trace.** evalctl checks the files, diffs,
  commands, and exit codes the agent produced. DeepEval reads the agent's outputs
  and trace and, by its own docs, does not monitor the file system. If your agent
  edits code or changes a workspace, that result is what evalctl scores.
- **The same result every time, no model key.** evalctl's default checks are
  deterministic and cost no model calls. DeepEval's metrics are mostly LLM judges,
  so a run needs a model API key and can vary from run to run. (DeepEval supports
  deterministic metrics too — see the caveat below — but the ready-made ones lean
  on a judge.)
- **A command-line tool the agent runs.** You don't import a library or write
  Python test cases to use evalctl. The agent under test calls the command, reads
  the result, and continues.
- **A results file another machine can re-check.** Hand the run directory to a
  second machine and it reproduces the report offline.

## One honest caveat

DeepEval is not judge-only. You can build deterministic metrics with the `scorer`
module (ROUGE, BLEU, BLEURT), and its `DAGMetric` "can be fully deterministic"
([metrics](https://deepeval.com/docs/metrics-introduction)). The real difference
isn't "judge versus deterministic" in the absolute — it's the **default path**
(DeepEval's ready-made metrics call an LLM judge and need a key; evalctl's default
scorers don't) and, more fundamentally, **what gets scored** (a model's outputs
and trace versus a workspace's files and diffs).

## Using them together

Point DeepEval at the answer and the reasoning: is the response relevant,
faithful, and safe, and did the agent's trajectory make sense? Point evalctl at
the result: did the agent write the right files, produce the right diff, and exit
clean? For a coding or tool-using agent, the two cover different halves — the
quality of what was said, and the correctness of what was done.

## Don't say

- Don't say DeepEval can only use an LLM judge. It supports deterministic metrics
  (ROUGE, BLEU, a deterministic DAG); its *default* metrics call a judge.
- Don't say DeepEval needs the Confident AI cloud. It is local-first and open
  source; the cloud is optional.
- Don't say evalctl grades answer quality, faithfulness, or RAG retrieval. It
  doesn't; that is DeepEval's domain.
- Don't call evalctl a replacement for DeepEval. They score different things — a
  workspace versus a model's outputs and trace.

## Sources

- DeepEval repository, main at commit `23901cf`, 2026-09-20:
  [github.com/confident-ai/deepeval](https://github.com/confident-ai/deepeval/tree/23901cfc5fe4f61c3ca745f67748e00699d0484f)
- DeepEval introduction (framework, pytest-style, agent traces, local-first):
  [deepeval.com/docs/introduction](https://deepeval.com/docs/introduction)
- DeepEval metrics (LLM-as-a-judge default, deterministic options, test-case
  fields): [deepeval.com/docs/metrics-introduction](https://deepeval.com/docs/metrics-introduction)
- evalctl scope and sandboxing: [How evalctl compares](/docs/comparison/),
  [Security and sandboxing](/docs/security/)
