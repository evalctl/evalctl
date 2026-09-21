---
title: evalctl and SWE-bench
description: When to use evalctl, a local command-line check that scores what your agent did to your own repo, and when to use SWE-bench, a fixed public benchmark that measures how many curated GitHub issues an agent can resolve.
bucket: project
order: 17
draft: false
---

# evalctl and SWE-bench

Both tools run a coding agent and then check the code it produced. So they get
compared. One is a benchmark; the other is a tool. SWE-bench measures how many
curated public issues an agent can resolve; evalctl checks what your agent did to
your own repo. This page says which job each one fits.

**Research baseline.** SWE-bench is a benchmark and evaluation harness; its docs are
at [swebench.com](https://www.swebench.com/SWE-bench/). The repository is
`SWE-bench/SWE-bench`, main at commit
[`02e7a74`](https://github.com/SWE-bench/SWE-bench/tree/02e7a74ffd0b707aab73d203fe87bdc7c76afc8e)
(checked 2026-09-20). Every SWE-bench claim below links to that source.

## Which one to use

Use **evalctl** to check what your agent did to your own workspace, fast, with
nothing to set up. It installs with `pip`, needs no Docker and no account, and the
agent under test can run it itself. You point it at the change your agent made and
ask whether it still passes. Each run leaves one results file you can commit.

Use **SWE-bench** to measure how an agent does on a fixed, public set of real
software issues. Given "a codebase and an issue, a language model is tasked with
generating a patch that resolves the described problem"
([SWE-bench](https://www.swebench.com/SWE-bench/)). The harness applies the patch and
runs the repo's real tests to decide whether the issue was resolved, and the
leaderboard ranks approaches against each other.

They answer different questions. SWE-bench asks, "how good is this agent on a shared
benchmark?" evalctl asks, "did this change break what my agent does to my repo?"
This page doesn't argue for one instead of the other.

## What each one is

evalctl is a single command-line tool. You run it, it runs your agent, and it grades
the result against the workspace the agent left behind: the files it wrote, the diffs
it made, the commands it ran, the exit codes. It runs in place, on your machine, and
it warns on every run that the runner isn't sandboxed.

SWE-bench is a benchmark: a fixed dataset of "2,294 instances sourced from 12 Python
repositories," plus a Docker-based harness to score them
([README](https://github.com/SWE-bench/SWE-bench/tree/02e7a74ffd0b707aab73d203fe87bdc7c76afc8e)).
A model produces a patch; the harness applies it and runs the repo's own tests,
using two test sets — FAIL_TO_PASS and PASS_TO_PASS — to decide whether the fix
resolves the issue without breaking what already worked. It ships variants such as
SWE-bench Lite, Verified (500 human-confirmed instances), and Multimodal, and the
harness wants "an x86_64 machine with at least 120GB of free storage, 16GB of RAM,
and 8 CPU cores."

## Side by side

| Area | evalctl | SWE-bench |
| --- | --- | --- |
| What it is | A reusable tool to check your own agent on your own repo. | A fixed public benchmark plus a Docker scoring harness. |
| The question | Did this change break what my agent does here? | How many curated issues can this agent resolve? |
| What you point it at | Your own repository and change. | 2,294 curated issues from 12 Python repos (or a variant). |
| Who writes the checks | You do, over your own workspace. | The benchmark: each issue's own FAIL_TO_PASS / PASS_TO_PASS tests. |
| Setup to run | `pip install evalctl`. No Docker, no account, no model key. | Docker; ~120GB storage, 16GB RAM, 8 cores for the harness. |
| Isolation | Runs in place; warns it isn't sandboxed. | A container per instance. |
| What a run leaves | One portable results file you commit. | A resolved / not-resolved score per instance, for a leaderboard. |
| Reused across models | No — it's your gate, not a ranking. | Yes — it's built to compare models and agents. |

Read this by column, not row by row. evalctl is the whole left column: your own repo,
your own checks, a local gate. SWE-bench is the whole right column: someone else's
curated issues, their hidden tests, a shared benchmark.

## What SWE-bench does well

- **A shared, real-world benchmark.** SWE-bench measures agents on real GitHub issues
  with the repositories' own tests, so results compare across approaches on the same
  ground ([SWE-bench](https://www.swebench.com/SWE-bench/)). evalctl is not a
  benchmark and does not rank models.
- **Resolution scored by real tests.** A patch counts only if it makes the failing
  tests pass and keeps the passing ones passing
  ([README](https://github.com/SWE-bench/SWE-bench/tree/02e7a74ffd0b707aab73d203fe87bdc7c76afc8e)).
  That hidden-test rigor is the point of a benchmark.
- **Container isolation at scale.** A Docker container per instance gives reproducible
  runs across thousands of tasks. evalctl doesn't match that isolation, and says so on
  every run.

If your need is a standard score for how an agent does on curated public issues, that
is SWE-bench's shape, not evalctl's.

## What evalctl does differently

- **Pointed at your own repo, not a fixed dataset.** You don't solve someone else's
  2,294 issues. You point evalctl at the change your agent made to your code and ask
  whether it still passes. That is a day-to-day gate, not a benchmark score.
- **Your checks, not the benchmark's hidden tests.** evalctl scores the workspace
  against checks you define — files, diffs, commands, exit codes — for work that has no
  curated FAIL_TO_PASS set waiting for it.
- **No Docker, no 120GB, no model key to run.** Install it and run it in a continuous
  integration job in seconds. evalctl scores what your agent already did, so a run
  costs no model calls by default.
- **The agent runs it.** evalctl is built so the agent under test can call it, read the
  result, and continue. If it guesses a command wrong, the error tells it the right one
  to paste.
- **A results file another machine can re-check.** Hand the run directory to a second
  machine and it reproduces the report offline, with no benchmark image to rebuild.

## What is the same (don't sell a false difference)

Both tools score by running real checks, not by asking a model. SWE-bench decides
resolution with the repo's own unit tests
([README](https://github.com/SWE-bench/SWE-bench/tree/02e7a74ffd0b707aab73d203fe87bdc7c76afc8e)),
and evalctl scores with fixed checks by default. So "deterministic versus judge-based"
is **not** the difference between them. The real differences are whose tasks you run —
a fixed public dataset or your own repo — and whether the tool is a benchmark that
ranks models or a gate you run on your own work.

## Using them together

Use SWE-bench when you want to know how your agent ranks on a shared, public benchmark
of real issues. Use evalctl as the everyday gate on your own repo: score a change on a
laptop or in continuous integration, with no Docker and no model spend, and commit the
results file. One tells you how your agent compares to the field; the other tells you
whether today's change is good on your own code.

## Don't say

- Don't call SWE-bench a reusable tool for your own repo. It is a fixed benchmark of
  curated public issues with their own tests.
- Don't say SWE-bench uses an AI judge. It scores resolution with the repositories'
  real unit tests (FAIL_TO_PASS / PASS_TO_PASS).
- Don't call evalctl a benchmark or say it ranks models. It is a gate you run on your
  own work; it publishes no leaderboard.
- Don't say evalctl sandboxes runs like SWE-bench. SWE-bench runs a container per
  instance; evalctl warns on every run that it isn't sandboxed.
- Don't say evalctl solves SWE-bench tasks. It scores what your agent did to your own
  repo, not a curated public dataset.

## See also

- [All tool comparisons](/docs/comparisons/) — the full set, and which tool fits which question.

## Sources

- SWE-bench overview (task definition, variants, harness):
  [swebench.com/SWE-bench](https://www.swebench.com/SWE-bench/)
- SWE-bench repository (2,294 instances, FAIL_TO_PASS/PASS_TO_PASS, Docker harness,
  resource requirements), main at commit `02e7a74`, 2026-09-20:
  [github.com/SWE-bench/SWE-bench](https://github.com/SWE-bench/SWE-bench/tree/02e7a74ffd0b707aab73d203fe87bdc7c76afc8e)
- evalctl scope and sandboxing: [How evalctl compares](/docs/comparison/),
  [Security and sandboxing](/docs/security/)
