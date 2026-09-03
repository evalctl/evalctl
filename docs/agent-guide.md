---
title: Driving evalctl from an agent
description: The agent-first workflow — capabilities, JSON envelopes, exit-code branching, and artifact handoff.
bucket: guides
order: 4
---

# Driving evalctl from an agent

Every mutating verb speaks a `--json` envelope, exit codes are a stable
branching contract, and the run directory is a portable artifact another agent
can inspect without your shell history. The tool ships its own handbook —
`evalctl robot-docs guide` — and this page mirrors it.

## Discover the contract first

```bash
evalctl capabilities --json     # verbs, flags, exit codes, error registry
evalctl schema run --json       # output schema for a verb
```

Don't hard-code the surface. Read `capabilities` for the installed version and
branch on what it reports.

### Reading `contract_version`

`meta.contract_version` (also `data.contract_version` in `capabilities`) is a
dotted `MAJOR.MINOR` string, e.g. `"1.0"`. Split it on `.`:

- **MAJOR** bumps on a breaking change. If the MAJOR you built against differs
  from the one you observe, stop and re-read the contract.
- **MINOR** bumps on a purely additive change (a new verb, flag, field, or error
  code). A higher MINOR than you built against is safe: existing fields keep
  their meaning; new ones may appear. Ignore fields you don't recognize.

This is why the field is a string, not an integer: an integer has no minor
channel, so additive contracts could not be told apart. Build the comparison as
two integer parts, not a float — `"1.10"` is newer than `"1.2"`.

## The workflow

1. Scaffold `evals/suites/code-review/` with `evalctl init`, **or** author a new
   suite: `suite add`, create fixtures, `case add`, `scorer add`.
2. `evalctl validate <suite> --json` **before** executing local code.
3. `evalctl run <suite> --acknowledge-unsandboxed-runner --json`. The runner is
   arbitrary local code; evalctl is not a sandbox. `run`/`replay` refuse with
   exit `2` (`E_UNSANDBOXED_RUNNER_UNACK`) until the invoker acknowledges, via
   that flag or `EVALCTL_ACKNOWLEDGE_UNSANDBOXED_RUNNER=1`.
4. If a run is interrupted, `evalctl run --resume <run-id> --json`. Resume reuses
   `run.json`, terminal `state.json` markers, and the original suite snapshot; it
   skips terminal cases and re-runs only unfinished ones.
5. `jobs list|get|prune --json` to inspect completed, running, stale, and
   orphaned local state. Reservations are TTL files with a background heartbeat —
   no daemon or lock server.
6. `status` for run state and the recommended next command.
7. `report --format json` for a deterministic envelope, or `--format markdown`
   for a human report.
8. Copy the run directory anywhere and `report --run-dir <path> --format json`;
   evalctl recomputes scores from artifacts and needs no durability sidecars.
9. After fixing a failed runner or fixture, `evalctl replay --failed <run-id>
   --json` re-executes only the failed/errored cases.

## Exit-code branching

Branch on the process exit code:

`0` success · `1` input error · `2` safety block · `3` tool-environment error ·
`4` retryable transient · `5` conflict · `6` eval failure (from
`run --fail-on-fail`).

Only `4` is retryable. See [Error and exit codes](/docs/errors/) for the full
mapping and which codes surface where.

## Reason codes vs. envelope errors

A runner timeout, runner spawn failure, or command-scorer failure is **reportable
case data**, not a command failure. By default `run`/`replay` still exit `0`,
emit `W_PARTIAL_RUN`, and record the per-case reason under
`runner.json.error_code` or the scorer verdict — they do **not** appear in
`errors[]`. Pass `--fail-on-fail` to turn a failed case into exit `6`; the
envelope stays `ok: true`, so branch on `data.fail_on_fail_triggered` rather than
on `ok`.

## Why the artifact matters

The core deliverable is a portable run directory: another agent can inspect it,
report on it, and re-score it with no access to the original shell. Scores are
recomputed from report artifacts, so a copied run is fully reportable — that is
what makes evalctl results durable rather than ephemeral console output.
