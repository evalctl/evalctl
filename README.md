# evalctl

[![PyPI](https://img.shields.io/pypi/v/evalctl.svg)](https://pypi.org/project/evalctl/)
[![CI](https://github.com/evalctl/evalctl/actions/workflows/ci.yml/badge.svg)](https://github.com/evalctl/evalctl/actions/workflows/ci.yml)
[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)

**Local-first evals for agents, not just prompts.**

evalctl treats eval cases as files, runners as shell commands, and results as
durable artifacts. It scores what agents actually do — files written, diffs
produced, commands run — on your own machine, with no gateway, dashboard, or
SaaS account.

v0.4 writes durable run metadata, supports crash resume, adds local run-state
inspection, diagnoses runtime health, produces side-effect-free execution plans,
and can optionally delegate runner execution to
[spoolctl](https://github.com/Ozhiaki/spoolctl). The standalone synchronous path
remains complete and requires no external service. [inferctl](https://inferctl.dev)
preflight provenance can be captured before runner execution without changing
report scoring.

## About

evalctl is an agent-first evaluation harness for local workflows. It runs eval
cases as filesystem fixtures, invokes agents through ordinary runner commands,
and grades the resulting workspace with deterministic scorers. The core artifact
is a portable run directory that another agent can inspect, report on, and
re-score without access to the original shell history.

## How it differs from promptfoo

[promptfoo](https://www.promptfoo.dev/) is the incumbent local eval CLI, and it
is prompt/chat-shaped.
evalctl is agent-shaped.

| Axis | promptfoo | evalctl |
| --- | --- | --- |
| Unit under test | prompt → completion | agent run → files, diffs, command logs, artifacts |
| Scoring surface | text of a response | resulting workspace: git diff, expected/forbidden file changes, exit codes, plus text |
| Execution | in-process, synchronous | v0.4 synchronous by default; `plan` previews actions, `doctor` diagnoses state, `run --resume` resumes crashed runs; optional `--queue spoolctl` delegates runner execution |
| Model context | provider API keys | Optional [inferctl](https://inferctl.dev) preflight provenance through `run --inferctl-task`; route capture remains deferred |

## Status

Python pre-release. The standalone synchronous path is complete and needs no
external service. v0.4 provides:

- **Authoring** — scaffold (`init`), `validate`, and CLI verbs to build suites.
- **Running** — bounded-parallel run execution, side-effect-free `plan`, crash
  resume, and `doctor` runtime diagnostics.
- **Results** — durable run metadata, `status`, `report`, deterministic local
  scorers, command scorers, and bounded local jobs inspection.
- **Replay** — execution replay for failed cases, and artifact replay from a
  copied run directory.
- **Contract** — truthful warnings and errors, real schema output, and a
  machine-readable `capabilities` surface.
- **Optional integrations** — spoolctl queueing and inferctl preflight
  provenance.

`contract_version` is `1.1`.

## Install

Requires Python 3.11+ and has no runtime dependencies (standard library only).

```bash
pip install evalctl
```

To use the optional spoolctl queue, install it alongside:

```bash
pip install evalctl "spoolctl>=0.4.11"
```

For unreleased changes, install from the default branch:

```bash
pip install "git+https://github.com/evalctl/evalctl.git"
```

For development, clone and install editable:

```bash
git clone https://github.com/evalctl/evalctl.git
cd evalctl
pip install -e .
```

## Quickstart

Scaffold a project, author a suite, run it, and read the report:

```bash
export EVALCTL_ACKNOWLEDGE_UNSANDBOXED_RUNNER=1  # or pass --acknowledge-unsandboxed-runner per run
evalctl init --json
evalctl suite add demo --runner-argv "python3 $EVALCTL_WORKSPACE/r.py" --json
evalctl case add demo --task "do X" --workspace fixtures/x --expect-json '{"exact":"ok"}' --json
evalctl scorer add demo --name exact --required --json
evalctl run demo --json
evalctl doctor --json
evalctl plan demo --json
evalctl run demo --inferctl-task code --json
evalctl run --resume <run-id> --json
evalctl jobs list --limit 50 --json
evalctl run demo --queue spoolctl --slots 4 --json
evalctl replay --failed <run-id> --json
evalctl report <run-id> --format json
```

## Durable Runs

Every run writes `run.json` before executing cases and writes
`cases/<case_id>/state.json` only after the case artifacts needed for reports are
complete. `manifest.json` is finalized from that durable state. If a process is
killed mid-run, `evalctl run --resume <run-id> --json` reuses the original
suite snapshot and run parameters, skips terminal cases, deletes partial
unfinished case directories, and executes only the remainder.

Reservations are liveness-only `.reservation.json` files with a TTL and
background heartbeat. A live reservation returns `E_RUN_BUSY`; a stale
reservation is reclaimed by explicit `--resume`. `jobs list|get|prune` inspects
completed, running, stale, and orphaned local run state and safely prunes only
with explicit confirmation.

`jobs list` is bounded by default. Use `--limit` and `--cursor` to page through
large run directories; the JSON envelope includes pagination metadata and a
paste-ready next-page command when more rows are available.

Durability sidecars are operational state. Reports and artifact replay do not
require `run.json`, `.reservation.json`, `.spoolctl.db`, `state.json`, or
`job.json`; `report_hash` stays based on the report projection. `SOURCE_DATE_EPOCH`
controls `created_ts` for deterministic manifest parity.

## Doctor And Plan

`evalctl doctor --json` reports runtime, suite root, runs root, reservations,
spoolctl, inferctl, and runner-safety state without failing just because a
component is degraded. Use `--component NAME` to scope diagnostics and `--fast`
for PATH-only optional integration checks.

`evalctl plan <suite> --json` resolves the case set without creating run
directories, enqueueing jobs, executing runners, scoring, or writing inferctl
artifacts. The plan includes run-id strategy, execution mode, independent-case
dependency metadata, parallel tracks, per-case actions, warnings, and
paste-ready follow-up commands. `--resume`, `--queue spoolctl`, `--slots`, and
`--inferctl-task` mirror the run surface for planning.

## Optional Spoolctl Queue

`evalctl run <suite> --queue spoolctl --json` delegates only runner execution to
spoolctl (`>= 0.4.11`, speaking contract `>= 2`). A spoolctl reporting a newer
contract is accepted. Evalctl still prepares workspaces, normalizes stdout and
stderr, captures workspace diffs, scores cases, and writes terminal markers. If
spoolctl is absent or incompatible, queued runs fail explicitly; non-queued runs
do not need spoolctl.

The queue database is per-run at `.spoolctl.db`; v0.4 starts one ephemeral
`spoolctl work --drain` worker per queued run. General externally managed worker
fleets are not part of this release.

## Inferctl Preflight Provenance

`evalctl run <suite> --inferctl-task TASK --json` probes inferctl once per run
and, when compatible preflight support is available, writes per-case
`inferctl-preflight.json` and `inferctl-provenance.json` before runner execution.
Queued spoolctl runs capture the same artifacts before enqueue. Absence,
incompatibility, parse failures, timeouts, and policy/readiness blocks are
warnings; the runner still executes and scoring proceeds.

The v0.4 capture mode is `preflight` only. `inferctl route` is not called, and
report projection is unchanged, so `report_hash` remains comparable to an
equivalent run without inferctl.

## Authoring

`suite add`, `case add`, and `scorer add` let agents build a suite without
hand-editing `suite.json` or `cases.jsonl`. Authoring verbs are idempotent on
retry: adding the same canonical object returns `created:false`; reusing the
same key with different content returns `E_RUN_CONFLICT`.

`case add` only writes paths under the suite tree. Absolute paths and `..`
segments are rejected so generated suite files remain portable.

## Replay

`replay --failed` selects failed/errored cases from the source run's recomputed
report projection, not from `score.json` or stored manifest status. It re-runs
only those cases against the current suite and writes a fresh partial run whose
manifest `replayed_from` names the source run.

`replay --run-id` names the destination run. It never resolves the source. Pass
`--suite <suite-or-path>` when replaying a copied run or when the current suite
cannot be resolved by manifest suite name.

## Command Scorers

`scorer add <suite> --name command --id judge --argv "python3 scorer.py"` adds an
external scorer. The scorer receives `EVALCTL_CASE_FILE`,
`EVALCTL_OUTPUT_FILE`, and `EVALCTL_WORKSPACE`, and emits one JSON verdict.

Command-scorer verdicts are captured once under
`cases/<case_id>/scorers/<id>.json`. Reports and artifact replay read that
artifact and do not re-execute the scorer binary. Command scorers run arbitrary
local code and are covered by the same unsandboxed-runner warning as runners.

## License

Apache License 2.0. See [LICENSE](LICENSE).

## Home

- Site: [evalctl.dev](https://evalctl.dev)
- Repository: [github.com/evalctl/evalctl](https://github.com/evalctl/evalctl)

---

<sub>Keywords: agent evals, LLM evaluation, AI agents, local-first, CLI, code
review, workspace diff, deterministic scoring, artifact replay, eval harness.</sub>
