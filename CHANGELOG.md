# Changelog

## Unreleased

Spoolctl compatibility fix. `contract_version` remains `1`; command names,
envelope shape, artifact layout, and report projection remain compatible with
v0.4.

- Queued spoolctl runs now require `spoolctl >= 0.4.11` speaking contract
  `>= 2`. The previous minimum, `0.4.1`, was never published to PyPI, and the
  compatibility gate pinned spoolctl's contract to exactly `1`. spoolctl moved
  to contract `2` in its 0.4.5, so `evalctl run <suite> --queue spoolctl`
  failed with `E_SPOOLCTL_INCOMPATIBLE` against every installable spoolctl.
- A spoolctl reporting a contract newer than `2` is now accepted rather than
  rejected. The comparison is a numeric floor with no upper bound, so a future
  spoolctl release does not become an evalctl outage.
- The compatibility gate reports three distinguishable causes instead of one
  message. Version mismatches carry `observed_version` and `minimum_version`,
  contract mismatches carry `observed_contract` and `minimum_contract`, and
  missing `spoolctl add` flags carry `missing_flags`. `E_SPOOLCTL_INCOMPATIBLE`
  and exit `3` are unchanged.
- Prerelease spoolctl versions at the floor are rejected. `0.4.11rc1` no longer
  passes as `0.4.11`; `0.4.12rc1`, `0.5.0a1`, and `0.4.11+local` are accepted.
- `capabilities` reports `minimum_contract` alongside `minimum_version`, and
  the observed `contract_version` when spoolctl is available.
- `evalctl doctor` recommends installing or upgrading spoolctl instead of
  re-running `evalctl doctor`, and names a version that the command it prints
  can install. Doctor exit codes are unchanged.

## 0.4.1 - 2026-07-24

CLI grammar hardening and refactor-safety patch. `contract_version` remains
`1`; command names, envelope shape, artifact layout, report projection, and
optional integration behavior remain compatible with v0.4.

- Added dev-only subprocess-aware coverage controls and normalized checked-in
  goldens for help, capabilities, representative schemas, robot docs, and
  malformed-input error envelopes.
- Added a typed internal command/flag registry and centralized parser for
  booleans, positive integers, enums, safe IDs, paths, JSON text, and free
  text while preserving the public CLI grammar.
- Reclassified malformed documented inputs as user-input errors. Invalid
  integer values, zero/negative positive-integer flags, missing values,
  empty-string values, and registered flags supplied where values are required
  now return structured `E_CASE_INVALID` envelopes with exit 1 instead of
  raw tracebacks, internal/environment failures, or silent fallbacks.
- Unknown flags are rejected consistently across all commands and subcommands
  before positional interpretation. This changes typo cases such as
  `init --forse --json` from silent success to `E_UNKNOWN_FLAG` exit 1.
- Unknown-flag suggestions now account for flag arity. Value-taking suggestions
  no longer emit syntactically invalid corrected commands unless a value can be
  preserved safely.
- Tightened safe IDs so leading-dash values such as `--json` cannot be accepted
  as run, resume, case, scorer, or inferctl task IDs.
- Treated `--format` as a report-only flag and made pre-dispatch JSON-mode
  detection non-raising; malformed `--format` inputs now produce enveloped
  errors rather than raw tracebacks.

## 0.4.0 - 2026-07-23

Planning, diagnostics, CLI recovery, bounded job listing, and inferctl
provenance minor release. `contract_version` remains `1`; the changes are
additive, and report projection remains unchanged for comparable runs.

- Added bounded `jobs list` output with `--limit`, `--cursor`, `total_count`,
  pagination metadata, truncated metadata, and paste-ready next-page commands.
- Added structured did-you-mean recovery for unknown top-level commands,
  namespace subcommands, and checked flag typos, including `did_you_mean`,
  `corrected_command`, and `valid_values` fields.
- Added `doctor` diagnostics for runtime, suite root, runs root, reservations,
  spoolctl, inferctl, and runner-safety state.
- Added side-effect-free `plan` output for fresh runs, explicit run ids, resume
  planning, spoolctl queue planning, concurrency tracks, and inferctl task
  intent.
- Added `run --inferctl-task TASK` best-effort inferctl preflight provenance.
  Compatible runs write per-case `inferctl-preflight.json` and
  `inferctl-provenance.json`; absent, incompatible, blocked, and failed capture
  states are warnings and do not prevent runner execution or scoring.
- Updated README, robot docs, schemas, capabilities, help text, warning codes,
  and regression tests for the v0.4 surfaces.

## 0.3.0 - 2026-07-16

Durability and resume minor release. `contract_version` remains `1`; the
plain synchronous report hash stays byte-identical to v0.2 and manifest-shape
parity is preserved with `created_ts` controlled by `SOURCE_DATE_EPOCH`.
Capabilities and schema hashes were re-pinned for additive flags, schemas, and
error-code registry entries.

- Added durable `run.json` metadata and per-case terminal `state.json` markers
  so interrupted runs can be reconstructed.
- Added TTL-based `.reservation.json` liveness with background heartbeat and
  stale-reservation reclaim through `run --resume`.
- Added `run --resume <run-id>` to skip terminal cases, re-run unfinished cases,
  and finalize the original run id from snapshotted state.
- Added `jobs list|get|prune` for local run/reservation/queue inspection and
  guarded cleanup.
- Refactored case execution into prepare, execute, normalize, score, and marker
  phases.
- Added optional `run --queue spoolctl` for `spoolctl >= 0.4.1`, using one
  ephemeral drain worker, per-run `.spoolctl.db`, at-most-once execution by
  default, and evalctl-owned artifact reconstruction/scoring.
- Updated README, robot docs, schemas, capabilities, help text, and regression
  tests for the v0.3 surfaces.

## 0.2.0 - 2026-07-15

Authoring and execution-replay minor release. `contract_version` remains `1`;
the universal envelope is unchanged. Capabilities and schema hashes were
re-pinned for additive verbs, schemas, and error-code registry entries.

- Added CLI authoring verbs: `suite add`, `case add`, and `scorer add`.
- Added `replay --failed` to re-execute failed/errored cases into a fresh
  partial run linked by `manifest.replayed_from`.
- Added command-scorer protocol with captured per-case verdict artifacts that
  report/artifact replay read without re-executing scorer binaries.
- Added per-case command-scorer failure code `E_SCORER_CASE_FAILED` with
  `surface:"score_json"`.
- Added schemas, capabilities, help, robot docs, and regression coverage for the
  new v0.2 surfaces.

## 0.1.1 - 2026-07-15

Contract-hardening patch. `contract_version` remains `1`.

- Added real bounded `--jobs` execution with deterministic evalctl-owned run surfaces.
- Made `W_UNSANDBOXED_RUNNER` present in every `run` envelope, including completed-run reuse.
- Replaced generic `schema <verb>` stubs with real per-verb data payload schemas.
- Killed runner process groups on timeout so child and grandchild processes do not survive.
- Wrote JSON artifacts with same-directory temp files and `os.replace` for atomic visibility.
- Rejected conflicting completed `--run-id` reuse when suite or case identity changes.
- Capped `EVALCTL_OUTPUT_FILE` raw bytes and set `runner.json.output_truncated` truthfully.
- Skipped non-UTF-8 workspace paths with `W_PATH_UNREADABLE` instead of crashing serialization.
- Added replay and scorer regression coverage for corrupted `score.json`, exact, regex, JSON, numeric threshold, and non-required advisory scorers.
