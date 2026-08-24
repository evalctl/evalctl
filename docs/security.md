---
title: Security posture
description: evalctl runs arbitrary local code and is not a sandbox. What that means, and the guarantees it does make.
bucket: project
order: 3
---

# Security posture

Be direct about the threat model: **evalctl is not a sandbox.** Runners and
command scorers are arbitrary local commands that evalctl executes on your
machine with your privileges. Treat a suite the way you'd treat any script you're
about to run.

## The unsandboxed-runner warning

Every `run` and `replay` envelope carries `W_UNSANDBOXED_RUNNER` — including
completed-run reuse. It is not situational; it is a standing reminder that the
runner (and any command scorer) is unsandboxed local code. If you need
isolation, provide it around evalctl (containers, VMs, restricted users);
evalctl won't provide it for you.

## What evalctl does guard

- **Portable suite paths.** `case add` only writes paths under the suite tree.
  Absolute paths and `..` segments are rejected so generated suite files stay
  portable and can't escape the suite directory.
- **Process-group cleanup.** Runner process groups are killed on timeout, so
  child and grandchild processes don't survive a `E_RUNNER_TIMEOUT`.
- **Atomic artifact writes.** JSON artifacts are written to a same-directory temp
  file and moved into place with `os.replace`, so readers never see a
  half-written file. (This is atomicity, not full crash-durability — v0.1.1 does
  not fsync.)
- **Output caps.** `EVALCTL_OUTPUT_FILE` raw bytes are capped, with a truthful
  `runner.json.output_truncated` flag and a `W_OUTPUT_TRUNCATED` warning.
- **Robust serialization.** Non-UTF-8 workspace paths are skipped with
  `W_PATH_UNREADABLE` rather than crashing the run.

## No network, no account

evalctl has no gateway, dashboard, or SaaS account and no runtime dependencies
beyond the Python standard library. It does not phone home. The optional
[spoolctl queue](/docs/spoolctl-queue/) is local, and the deferred
[inferctl](https://inferctl.dev) integration is not wired in.

## Reporting

Security issues belong on the
[evalctl repository](https://github.com/evalctl/evalctl). Do not include secrets
in eval fixtures or task files — the run directory is designed to be **copied and
shared**, and anything in a fixture travels with it.
