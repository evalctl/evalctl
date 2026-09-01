---
title: Error and exit codes
description: The exit-code contract, the error/warning registry, and which surface each code appears on.
bucket: guides
order: 5
---

# Error and exit codes

evalctl's exit codes and error-code registry are a stable contract. The
authoritative copy for the installed version is under
`evalctl capabilities --json` (`exit_codes` and `error_codes`); this page is the
reference.

## Exit codes

| Code | Meaning | Retryable |
| --- | --- | --- |
| `0` | success | — |
| `1` | user-input error | no |
| `2` | safety block (`run`/`replay` runner not acknowledged) | no |
| `3` | tool-environment error | — |
| `4` | transient failure | **yes** |
| `5` | conflict | no |
| `6` | eval failure (from `run --fail-on-fail`) | no |

Only exit `4` is retryable. Exit `3` (`retryable: null`) means "depends" — inspect
the reason code rather than blindly retrying.

## Surfaces

Every code appears on exactly one surface:

- **`envelope`** — the code lands in `errors[]` or `warnings[]` and predicts the
  command's process-exit class.
- **`runner_json`** — a per-case reason under `runner.json.error_code` (runner
  spawn or timeout). Not a command failure by itself.
- **`score_json`** — a per-case scorer verdict reason, e.g. `E_SCORER_CASE_FAILED`
  in `cases/<id>/scorers/<scorer_id>.json` or `score.json`.

A runner timeout, spawn failure, or command-scorer failure is reportable case
data: `run`/`replay` exit `0` by default (or `6` with `--fail-on-fail`), emit
`W_PARTIAL_RUN`, and keep the per-case reason code **out** of `errors[]`.

Exit `6` keeps the envelope `ok: true` (the harness succeeded; the eval did not).
Branch on `data.fail_on_fail_triggered`, not on `ok` — it is `true` only when
`--fail-on-fail` was passed and at least one case did not pass, and evalctl also
prints a one-line `eval failure:` summary to stderr.

## Error codes

| Code | Class | Exit | Surface |
| --- | --- | --- | --- |
| `E_CASE_INVALID` | user-input | 1 | envelope |
| `E_SCHEMA_VIOLATION` | user-input | 1 | envelope |
| `E_SUITE_NOT_FOUND` | user-input | 1 | envelope |
| `E_RUN_NOT_FOUND` | user-input | 1 | envelope |
| `E_RUN_CORRUPT` | user-input | 1 | envelope |
| `E_UNKNOWN_COMMAND` | user-input | 1 | envelope |
| `E_UNKNOWN_SUBCOMMAND` | user-input | 1 | envelope |
| `E_UNKNOWN_FLAG` | user-input | 1 | envelope |
| `E_UNKNOWN_COMPONENT` | user-input | 1 | envelope |
| `E_UNSANDBOXED_RUNNER_UNACK` | safety | 2 | envelope |
| `E_INIT_UNWRITABLE` | tool-env | 3 | envelope |
| `E_RUNNER_FAILED` | tool-env | 3 | runner_json |
| `E_RUNNER_TIMEOUT` | tool-env | 3 | runner_json |
| `E_SPOOLCTL_UNAVAILABLE` | tool-env | 3 | envelope |
| `E_SPOOLCTL_INCOMPATIBLE` | tool-env | 3 | envelope |
| `E_INFERCTL_UNAVAILABLE` | tool-env | 3 | envelope |
| `E_INFERCTL_INCOMPATIBLE` | tool-env | 3 | envelope |
| `E_SCORER_FAILED` | tool-env | 3 | envelope |
| `E_SCORER_CASE_FAILED` | tool-env | — | score_json |
| `E_JOB_TRANSIENT` | transient | 4 | envelope |
| `E_RUN_BUSY` | transient | 4 | envelope |
| `E_RUN_IN_FLIGHT` | transient | 4 | envelope |
| `E_RUN_CONFLICT` | conflict | 5 | envelope |

## Warnings

Warnings never fail a command; they annotate the envelope.

| Code | Where |
| --- | --- |
| `W_UNSANDBOXED_RUNNER` | present on every `run`/`replay` envelope |
| `W_RUNNER_UNRESOLVED` | `validate` could not resolve the runner executable now |
| `W_PARTIAL_RUN` | some cases did not complete cleanly |
| `W_REPLAY_CASE_ABSENT` | a selected case is missing from the current suite |
| `W_NOTHING_TO_REPLAY` | no failed/errored cases to replay |
| `W_RESUME_NOTHING_PENDING` | resume found no unfinished cases |
| `W_RESERVATION_RECLAIMED` | a stale reservation was reclaimed |
| `W_TEXT_DIFF_APPROXIMATED` | a text diff was approximated |
| `W_OUTPUT_TRUNCATED` | runner output exceeded the byte cap |
| `W_PATH_UNREADABLE` | a workspace path was skipped (e.g. non-UTF-8) |
| `W_INFERCTL_ABSENT` | `--inferctl-task` requested, but inferctl is not installed |
| `W_INFERCTL_INCOMPATIBLE` | installed inferctl failed the version/contract check |
| `W_INFERCTL_PREFLIGHT_BLOCKED` | inferctl preflight refused the run |
| `W_INFERCTL_CAPTURE_FAILED` | inferctl route/provenance capture failed |

The `E_INFERCTL_*` and `W_INFERCTL_*` codes only arise around the **planned**
inferctl integration, requested with `run`/`plan --inferctl-task`. inferctl is
not yet available (`capabilities --json` reports it as `planned`), so today the
request degrades to `W_INFERCTL_ABSENT` rather than capturing route provenance.

## Common cases

- **`E_UNSANDBOXED_RUNNER_UNACK`** — `run`/`replay` refuse (exit `2`) because the
  invoker has not acknowledged that runner and scorer commands execute as local
  code. Inspect the suite, then pass `--acknowledge-unsandboxed-runner` or set
  `EVALCTL_ACKNOWLEDGE_UNSANDBOXED_RUNNER=1`. The acknowledgment must come from
  the invoker; a suite-file field cannot satisfy it.
- **`E_RUN_BUSY`** — a live reservation holds the run. Wait, or take it over with
  an explicit `run --resume`. Retryable.
- **`E_RUN_IN_FLIGHT`** — `report` was asked for a run that exists but has not
  finalized, so there is no deterministic report yet. Retryable once the run
  finishes; use `status <run-id>` to watch live progress in the meantime. `status`
  itself never returns this — it reports the live state directly. `E_RUN_NOT_FOUND`
  is reserved for run ids with no run directory at all, so `jobs get`, `status`,
  and `report` never disagree about whether a run exists.
- **`E_RUN_CONFLICT`** — you reused a key/run-id with different content, or an
  authoring add clashed. Not retryable; change the identity or content.
- **`E_SPOOLCTL_UNAVAILABLE` / `E_SPOOLCTL_INCOMPATIBLE`** — only ever raised when
  `--queue spoolctl` is requested. Non-queued runs never need spoolctl.
