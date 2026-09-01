---
title: The spoolctl queue
description: Optionally delegate runner execution to spoolctl — and what evalctl still owns.
bucket: concepts
order: 4
---

# The spoolctl queue

By default `run` executes cases synchronously and needs no external service. The
queue path is opt-in: it delegates **only runner execution** to
[spoolctl](https://github.com/Ozhiaki/spoolctl), and evalctl keeps ownership of
everything else.

## Enabling it

```bash
evalctl run demo --queue spoolctl --slots 4 --acknowledge-unsandboxed-runner --json
```

The queue path still executes local runner code, so the invoker's acknowledgment
(`--acknowledge-unsandboxed-runner` or `EVALCTL_ACKNOWLEDGE_UNSANDBOXED_RUNNER=1`)
is required here too, or the run refuses with exit `2`.

Requires spoolctl `>= 0.4.11` (queue contract v2). If spoolctl is **absent or
incompatible**, a queued
run fails explicitly (`E_SPOOLCTL_UNAVAILABLE` / `E_SPOOLCTL_INCOMPATIBLE`, exit
`3`). Non-queued runs never touch spoolctl.

## What each side owns

Even under `--queue spoolctl`, evalctl still:

- prepares per-case workspaces,
- normalizes stdout and stderr,
- captures workspace diffs,
- scores cases,
- writes the terminal markers.

spoolctl only runs the runner command. This keeps the run directory and its
scoring evalctl-owned and reproducible regardless of how the runner was executed.

## The queue database

The queue DB is **per-run**, at `.spoolctl.db`. In this release, evalctl starts a
single ephemeral `spoolctl work --drain` worker per queued run, with at-most-once
execution by default.

Because the DB is per-run, an externally managed cross-machine worker fleet would
require a shared filesystem — general hosted-worker fleets are **not** part of
this release. The `.spoolctl.db` is a durability sidecar: it is operational state
and is not required for reports or [artifact replay](/docs/replay/).

## When to use it

Reach for the queue when you want bounded, worker-driven runner execution.
Otherwise the synchronous path — optionally with `--jobs N` for bounded
in-process parallelism — is complete on its own.
