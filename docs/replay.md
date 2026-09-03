---
title: Replay and portable artifacts
description: Re-run only the failed cases, and re-score a run directory copied anywhere.
bucket: concepts
order: 2
---

# Replay and portable artifacts

evalctl has two distinct "replay" ideas: **execution replay** (re-run the failed
cases) and **artifact replay** (re-score a copied run directory without
re-running anything).

## Execution replay

```bash
evalctl replay --failed <run-id> --acknowledge-unsandboxed-runner --json
```

Like `run`, `replay` re-executes local runner and scorer commands, so it
requires the invoker's acknowledgment (`--acknowledge-unsandboxed-runner` or
`EVALCTL_ACKNOWLEDGE_UNSANDBOXED_RUNNER=1`) or it refuses with exit `2`. Artifact
replay (below) only re-scores stored artifacts and needs no acknowledgment.

`replay --failed` selects failed/errored cases from the **recomputed report
projection** of the source run — not from `score.json` or the stored manifest
status. It re-runs only those cases against the current suite and writes a fresh
partial run whose manifest `replayed_from` names the source run.

- `--run-id <NEW>` names the **destination** run; it never resolves the source.
- Pass `--suite <suite-or-path>` when replaying a copied run, or when the suite
  can't be resolved from the manifest suite name.
- `W_NOTHING_TO_REPLAY` means there were no failed/errored cases;
  `W_REPLAY_CASE_ABSENT` means a selected case is missing from the current suite.

Typical loop: fix the runner or fixture, then replay only what failed instead of
re-running the whole suite.

## Artifact replay

The run directory is portable. Copy it anywhere and re-score it:

```bash
evalctl report --run-dir <copied-run-dir> --format json
```

evalctl recomputes scores from the report artifacts. Artifact replay does **not**
require `run.json`, `.reservation.json`, `.spoolctl.db`, `state.json`, or
`job.json` — only the report artifacts. This is what makes a run reviewable by
another agent or teammate who never had your shell history.

## Determinism

The plain synchronous `report_hash` is byte-stable across versions for the same
inputs (it stayed identical from v0.2 to v0.3). `SOURCE_DATE_EPOCH` controls the
run's `created_ts`, so manifest-shape parity is reproducible. Durability sidecars
are excluded from `report_hash` by design.
