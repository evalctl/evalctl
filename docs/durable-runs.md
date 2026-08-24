---
title: Durable runs and resume
description: How run.json, per-case state markers, and TTL reservations make interrupted runs reconstructable.
bucket: concepts
order: 1
---

# Durable runs and resume

A run is designed to survive a killed process. evalctl writes durable metadata
before and during execution so an interrupted run can be reconstructed and
finished without redoing completed work.

## What gets written

- **`run.json`** — written once, before any case executes. It snapshots the suite
  and the run parameters.
- **`cases/<case_id>/state.json`** — a terminal marker written **only after** the
  case's report artifacts are complete.
- **`manifest.json`** — finalized from that durable state.

Because a case is only marked terminal once its artifacts are whole, a resume can
trust the marker: a case is either fully done or not counted.

## Resume

```bash
evalctl run --resume <run-id> --json
```

Resume reuses the original suite snapshot and run parameters from `run.json`,
skips terminal cases, deletes partial unfinished case directories, executes only
the remainder, and finalizes the **original** run id.

## Reservations

Concurrency safety is a liveness file, not a lock server. Each run holds a
`.reservation.json` with a TTL and a background heartbeat:

- A **live** reservation makes a competing `run` return `E_RUN_BUSY` (retryable,
  exit `4`).
- A **stale** reservation is reclaimed by an explicit `--resume`, which emits
  `W_RESERVATION_RECLAIMED`.

No daemon or lock server is required.

## Inspecting run state

```bash
evalctl jobs list --json
evalctl jobs get <run-id> --json
evalctl jobs prune --yes --json
```

`jobs` inspects completed, running, stale, and orphaned local run state and
prunes only with explicit confirmation (`--yes`/`--force`).

## Sidecars are operational, not canonical

`run.json`, `.reservation.json`, `.spoolctl.db`, `state.json`, and `job.json` are
operational state. **Reports and artifact replay do not require them** — scores
are recomputed from report artifacts, and `report_hash` stays based on the report
projection. `SOURCE_DATE_EPOCH` controls `created_ts` so manifest-parity tests are
deterministic. See [Artifact replay](/docs/replay/).
