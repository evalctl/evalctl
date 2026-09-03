---
title: Command surface
description: Every evalctl verb, what it does, and the flags that matter.
bucket: guides
order: 3
---

# Command surface

`evalctl <command> [flags]`. The machine-readable contract for the installed
version is always `evalctl capabilities --json`; this page is the human summary.

## Contract and docs

| Command | Purpose |
| --- | --- |
| `capabilities --json` | Machine contract: verbs, flags, exit codes, error registry |
| `schema <verb> --json` | Output schema for a verb |
| `robot-docs guide` | Agent workflow handbook |

## Authoring

| Command | Purpose |
| --- | --- |
| `init [--force]` | Scaffold `evals/` with a sample code-review suite (`--force` replaces an existing sample suite and reports the files it deletes) |
| `suite add <name>` | Add a suite (`--runner-argv ARGV` or `--runner-command CMD --shell`) |
| `case add <suite>` | Add a case (`--task`, `--workspace`, `--id`, `--diff`, `--expect-json`), or bulk-add with `--stdin` |
| `scorer add <suite>` | Add a scorer (`--name`, `--required`/`--advisory`, command scorers via `--argv`) |
| `validate [suite]` | Validate suite files, fixtures, scorer refs, runner config |

## Running

| Command | Purpose |
| --- | --- |
| `plan <suite>` | Produce a side-effect-free execution plan; never mutates |
| `run <suite>` | Run a suite into a portable, resumable run directory |
| `run --resume <run-id>` | Resume an interrupted run; re-run only unfinished cases |
| `replay --failed <run-id>` | Re-execute failed/errored cases into a linked partial run |

Key `run` flags: `--jobs N` (bounded parallelism), `--timeout S`, `--run-id ID`,
`--queue spoolctl --slots N`, `--reservation-ttl S`, and `--fail-on-fail` (exit
`6` with `data.fail_on_fail_triggered: true` when any case fails, instead of
`0`). `plan` accepts the same execution flags as `run` and prints the plan
without touching disk.

Because they execute the suite's runner and scorer commands as local code, `run`
and `replay` require the invoker to acknowledge that evalctl is not a sandbox:
pass `--acknowledge-unsandboxed-runner` or set
`EVALCTL_ACKNOWLEDGE_UNSANDBOXED_RUNNER=1`. Without it they refuse with
`E_UNSANDBOXED_RUNNER_UNACK` and exit `2` before any command runs. `plan` never
executes runners, so it needs no acknowledgment.

`run --inferctl-task TASK` captures inferctl preflight provenance for each case
before its runner starts. `plan --inferctl-task TASK` records the requested
provenance in its side-effect-free plan. If inferctl is absent or incompatible,
the run continues and records a degradation warning.

## Inspecting

| Command | Purpose |
| --- | --- |
| `doctor [--component NAME]` | Diagnose runtime, run state, and optional integrations (`--fast` skips slow probes) |
| `jobs list\|get\|prune` | Inspect and prune local run/reservation/queue state (`--yes`, `--force`) |
| `status <run-id>` | Diagnose run state and get the recommended next command |
| `report <run-id>` | Generate a report (`--format markdown\|json`) |

Any command that resolves a run accepts `--run-dir PATH` in place of a run id,
which is what makes a copied run directory reportable anywhere. See
[Artifact replay](/docs/replay/).

## Global flags

`--json` (structured envelope), `--no-color` (suppress ANSI), `--version`,
`--help`/`-h`.

## Runner environment

Runners and command scorers receive these variables:

| Variable | Meaning |
| --- | --- |
| `EVALCTL_CASE_FILE` | Materialized case JSON passed to the runner |
| `EVALCTL_WORKSPACE` | Fresh per-case workspace |
| `EVALCTL_OUTPUT_FILE` | Runner response destination |
| `EVALCTL_TASK_FILE` | Task text file |
| `EVALCTL_DIFF_FILE` | Review diff file when present |
| `SOURCE_DATE_EPOCH` | Controls deterministic timestamps, including run `created_ts` |
