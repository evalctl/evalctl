# Changelog

## Unreleased

- **BREAKING: `contract_version` is now a dotted `MAJOR.MINOR` string (`"1.0"`),
  not the integer `1`.** An integer had no minor channel: v0.2, v0.3, and v0.4
  were all additive yet all stuck at `1`, so an agent could not tell those
  contracts apart through the field that exists for exactly that purpose, and the
  reference compatibility idiom (which splits the field on `.`) raised against an
  integer. Decision: adopt the dotted string now, before 1.0, since changing the
  type is itself breaking and cheapest to pay while pre-1.0 (the two rejected
  options were a second additive-counter field, and documenting that additive
  change is simply not signalled). Going forward MAJOR bumps on a breaking
  change and MINOR bumps on a purely additive one. The field moves in both
  `data.contract_version` (`capabilities`) and `meta.contract_version` (every
  envelope); the `capabilities` schema now types it `string` with a
  `^\d+\.\d+$` pattern; capabilities, all schemas, every golden, and the docs
  move together. Consumers doing an integer comparison must switch to splitting
  on `.` and comparing integer parts. See docs/agent-guide.md for the rule an
  agent should read.
- **`doctor.operation_outcome` now matches the DIAGNOSE reference shape, and
  `doctor` reports a re-check cadence.** The second key was `health_kind`
  (`all-clear`/`attention-needed`), which no reference field named; it is now
  `exit_code_kind`. `kind` still carries the health outcome
  (`healthy`/`degraded`/`unhealthy`) while `exit_code_kind` reports the exit-code
  family the command returns (always `success`: `doctor` reports state, it never
  fails its own exit). A new `data.next_check_after_seconds` (`300` when healthy,
  `60` otherwise) tells an agent when to poll again.
- **`status` surfaces its recommended action as a paste-ready `command`.** Both
  the completed and in-flight branches carried `data.recommended_action` but left
  `commands[]` without it (completed carried only pagination links; in-flight
  carried none). The recommended command now appears in `commands[]` as well, so
  an agent can run it without reshaping the payload. `recommended_action` stays in
  `data` -- this is an addition, not a move.
- **`run`/`replay` meta now carries an inferctl requested-vs-actual pair when a
  capture was requested.** When `--inferctl-task` asked for preflight capture but
  inferctl was absent or incompatible, only a `W_INFERCTL_ABSENT`/
  `W_INFERCTL_INCOMPATIBLE` warning marked the silent degradation. `meta.inferctl`
  now reports `requested_mode`, `actual_mode`, `available`, and `degraded`, so the
  degradation is detectable from meta structure instead of warning text. The pair
  is omitted entirely when no capture was requested.
- **`case add --stdin` bulk-adds cases from piped `cases.jsonl`-shaped
  records.** Single-add takes one case per process; seeding a suite meant one
  fork per case. `--stdin` reads whole records (one JSON object per line, fields
  `id`/`task`/`workspace`/`diff`/`expect`), so a suite's `cases.jsonl` pipes
  straight into a fresh suite. The report is per-record --
  `data.added`/`skipped`/`rejected` with `counts` -- because a bulk add is not
  transactional across records: an exact-duplicate record is skipped, an
  id-conflict or a malformed/invalid record is rejected with a per-line reason,
  and every accepted record lands in one atomic append. Any rejection raises the
  new `W_CASE_ADD_REJECTED` warning and exits `1`; `--stdin` does not combine
  with the single-record flags (`--task`/`--workspace`/`--id`/`--diff`/
  `--expect-json`).
- **`plan` reports a live reservation as an external blocker instead of
  proposing work that would be refused.** With a live reservation held on the
  target run, `plan` (and `plan --resume`) marked the run blocked but proposed
  the `run`/`run --resume` command that `E_RUN_BUSY` would then reject, and the
  payload carried no branchable blocker. The payload now carries
  `data.blocked_by_external` -- populated only when a blocker exists -- naming
  the reservation, when it clears, and the `status <run-id>` command to inspect
  it; the affected cases are marked `blocked` and the recommended `commands`
  point at the blocker, not at the refused run. With no reservation held, the
  plan is unchanged.
- **`status` and `report` now see a run while it is still in flight.** Both verbs
  keyed on `manifest.json`, which is written only at finalize, so mid-run they
  returned `E_RUN_NOT_FOUND` (exit `1`) for a run that `jobs get` reported as
  `running`. Now `status <run-id>` returns exit `0` with the live `state` and
  per-case `progress` (`case_count`/`terminal`/`pending`), and `report <run-id>`
  returns the new `E_RUN_IN_FLIGHT` (class `transient`, exit `4`, retryable)
  naming the state and pointing at `status`, instead of pretending the run does
  not exist. `E_RUN_NOT_FOUND` is now reserved for ids with no run directory, so
  `jobs get`, `status`, and `report` never disagree about run existence.
  `contract_version` remains `1`; report projection and report hashes are
  unchanged.
- **`plan`, `status`, and `report` now bound their per-case collections.** These
  three verbs returned the full case array in one envelope -- roughly 660 bytes
  per case for `plan` -- so a large suite could emit a quarter-megabyte payload
  with no truncation notice and reject the bounding flag. They now accept
  `--limit` (default `50`, max `1000`) and `--cursor`, matching `jobs list`, and
  report `meta.pagination` and `meta.truncated` with the omitted count and a
  paste-ready next-page command whenever the collection is clipped. An unbounded
  call on a small suite is byte-identical to before: no pagination meta is
  emitted unless the output is clipped or a bounding flag is passed. A cursor the
  tool never issued -- on any of the four paginated verbs, `jobs list` included
  -- is now `E_CASE_INVALID` (exit `1`) naming the offending value, instead of a
  silent empty page indistinguishable from the end of the collection. For
  `report`, only `cases` is paged; `failures` and `report_hash` stay computed
  over the full set, so the hash remains the integrity anchor across pages.

- **`validate` now checks the two things it claimed to check but did not.** A
  suite whose scorer name was not a known scorer, or whose runner executable did
  not resolve, still returned `valid: true` -- so an agent following the
  prescribed validate-then-run workflow got a green validate and an errored run.
  An unknown scorer name is now `E_CASE_INVALID` (exit `1`) naming the valid set
  (`valid_values`) with a near-match `did_you_mean`. An unresolvable runner
  executable is the new `W_RUNNER_UNRESOLVED` warning rather than an error,
  because it may still resolve in the run environment; an argv token carrying an
  env placeholder (`$VAR`) resolves only at run time and is left alone.
- **`suite add --runner-argv` rejects a JSON array.** The flag takes a
  shell-style argv string, but a JSON array (`'["python3","runner.py"]'`) was
  silently coerced into a single literal argv token that validated and then
  errored at run time. It is now `E_CASE_INVALID` (exit `1`) naming the shape
  the flag wants.
- **Not-found and unknown-verb errors now name the valid set.** A missing suite
  (`E_SUITE_NOT_FOUND`), a missing run (`E_RUN_NOT_FOUND`), or an unknown
  `schema` verb reported only the bad value. They now carry `valid_values` -- the
  suites on disk, the runs on disk, the schema verbs the tool holds -- with a
  near-match `did_you_mean` where one exists; `schema <bad-verb>` also carries a
  paste-ready `corrected_command`. A suggestion is offered only within one
  vocabulary, never across unrelated flags. `schema <bad-verb>` is now
  `E_UNKNOWN_COMMAND` (the code its dispatch sibling already used) instead of a
  generic parse error.
- **`init` on an unwritable directory is a declared tool-environment error.**
  Scaffolding into a read-only directory surfaced as an undeclared internal error.
  It is now `E_INIT_UNWRITABLE` (class `tool-env`, exit `3`) naming the OS reason
  and pointing at the write-permission fix.
- **`schema` now pins every verb and publishes its output vocabularies.** Only
  three of the fourteen JSON verbs had a schema golden, so `doctor`, `status`,
  `report`, `replay` and the rest could drift silently; and `definitions` was
  exported empty, so no output vocabulary was contract. Every JSON verb now has a
  pinned schema golden, and a test fails if a verb ships without one. The
  `definitions` block now carries `case_status` (`error`/`fail`/`pass`),
  `run_state` (`completed`/`orphaned`/`running`/`stale`) and `plan_action`
  (`blocked`/`run`/`skip_terminal`) as enums, referenced by `$ref` from every
  schema that carries them -- so an agent branching on case status reads the full
  set from the contract instead of discovering it by observation. A test pins the
  published `case_status` enum to exactly the set a real run produces. The per-job
  `state` inside `queue_jobs` is deliberately left open: it is a spoolctl
  vocabulary evalctl passes through and does not own. `contract_version` remains
  `1`; report projection and report hashes are unchanged.
- **`replay` is now idempotent independently of the wall clock.** The default
  destination id embedded a second-resolution timestamp, so a retry behaved
  differently depending on which second it landed in: two `replay` calls in the
  same second collided on one id and the second returned `E_RUN_CONFLICT`, while
  two calls across a second boundary produced two separate runs. The default id
  is now derived from the source run and the exact replayed case set, not the
  clock, so a retry always lands on the same id and returns the existing run --
  matching how `run --run-id` treats a completed run. Idempotency keys on run
  identity, not the id string: a genuinely different case set on the same id is
  still `E_RUN_CONFLICT` unless `--force` is passed, and `--force` always
  rebuilds. `SOURCE_DATE_EPOCH` is still honored for run timestamps; it no longer
  affects the replay id at all. Report hashes are unchanged.

## 0.4.4 - 2026-09-01

Two exit codes that the contract advertised but that no code path exercised are
now wired to real behavior. `contract_version` remains `1`; command names,
envelope shape, error codes, exit codes, case statuses, and report projection are
unchanged, and report hashes are unaffected.

- **Exit `2` (safety block) is now reachable: `run`/`replay` refuse to execute an
  unacknowledged unsandboxed runner.** Runner and scorer commands are local code
  that evalctl executes with the caller's privileges. `run`, `run --resume`, and
  `replay` now refuse with `E_UNSANDBOXED_RUNNER_UNACK` (class `safety`, exit `2`)
  before running any command, unless the **invoker** acknowledges via the new
  `--acknowledge-unsandboxed-runner` flag or `EVALCTL_ACKNOWLEDGE_UNSANDBOXED_RUNNER=1`.
  The acknowledgment deliberately lives with the invoker, not in the suite file:
  a suite-file field is controlled by whoever authored the suite, so it could not
  defend against an untrusted one. The former scaffold field
  `acknowledged_unsandboxed_runner` is removed, and the standing
  `W_UNSANDBOXED_RUNNER` warning still rides every `run`/`replay` envelope. This
  is a behavior change: existing callers of `run`/`replay` must now acknowledge.
- **Exit `6` (`run --fail-on-fail`) is machine-branchable.** The envelope still
  reports `ok: true` (the harness succeeded even though the eval did not), and now
  carries `data.fail_on_fail_triggered` -- `true` only when `--fail-on-fail` was
  passed and at least one case did not pass. evalctl also prints a one-line
  `eval failure:` summary to stderr. Branch on the field, not on `ok`.

## 0.4.3 - 2026-07-29

`--help` now works per verb and no longer runs the verb. Queued runs read
spoolctl's own `failure_reason` enum instead of inferring outcomes from two
proxies, and record real durations. `contract_version` remains `1`; command
names, envelope shape, error codes, exit codes, case statuses, and report
projection are unchanged, and report hashes are byte-identical to v0.4.2.

- **`evalctl <verb> --help` ran the verb.** `--help` and `--json` were
  registered as global flags, so they passed the unknown-flag check on every
  verb and reached handlers that never acted on them; only the top-level
  dispatcher acted on `--help`. `evalctl init --help` scaffolded a tree and
  `evalctl run <suite> --help` executed the suite -- the read-only probe an
  agent reaches for first was, on two verbs, a mutation. `--help` and `-h` are
  now handled per verb, before the handler, and print that verb's own help at
  exit 0 with no side effects. Detection follows the flag grammar: `--help` as
  the value of a dash-tolerant flag is still a value, and after `--` it is still
  a positional.
- **`robot-docs guide --json` returned markdown at exit 0.** `capabilities`
  correctly declares `"json": false` for `robot-docs`, but the flag was accepted
  and ignored. It is now rejected with `E_UNKNOWN_FLAG` at exit 1. The reject
  list is derived from the same `CommandSpec.json` field that feeds
  `capabilities`, so the accepted set cannot drift from the advertised one. The
  error carries no `did_you_mean`: the nearest flag to `--json` is `--version`,
  which is valid syntax with unrelated semantics.

- **Queued cases recorded `duration_ms: 0`.** The queued path read
  `attempts[].duration_ms`, a field real spoolctl has never emitted, so the
  value was always `0` on every queued case. Durations are now derived from the
  attempt's `started_at` and `finished_at`, clamping to `0` only when a
  timestamp is missing or unusable. This is the release's one change to
  recorded run data.
  `duration_ms` appears in `cases/<id>/runner.json` and in no scored output, so
  reports and report hashes are unaffected.
- Queued outcome classification keys on `(failure_reason, exit_code)`. spoolctl
  has emitted a machine-readable `attempts[].failure_reason` since its 0.4.2,
  and evalctl's contract `>= 2` floor guarantees it is present. The attempt's
  `state` field and a prefix match on spoolctl's human-readable error text are
  no longer consulted. Both were contracts evalctl does not own: spoolctl could
  have reworded either in any release without evalctl failing loudly. For every
  outcome evalctl can reach, the new mapping agrees with the old one -- this is
  a robustness change, not a correction of past results.
- **A canceled queued job was reported as a spoolctl incompatibility.** A job
  canceled before a worker picked it up reports an empty `attempts` list, and
  the queued path raised `E_SPOOLCTL_INCOMPATIBLE` with exit `3`, telling the
  operator to upgrade spoolctl for what is an ordinary queue outcome. Such a
  job now records `E_RUNNER_FAILED` on the case and the run completes. Only a
  payload with no `attempts` key, or a non-list there, is still treated as a
  spoolctl evalctl cannot speak to.
- **Per-case `job.json` records the queue's state for the job, not the last
  attempt's.** The value moves from `succeeded` to `done`, and from `failed` or
  `timed_out` to `dead`; a job canceled before it ran now records `canceled`
  instead of nothing. evalctl read `state` off the top level of `spoolctl
  show`, where real spoolctl does not put it -- the job is nested under `job` --
  so the read always failed and fell through to the attempt's state, a
  different vocabulary. `job.json` is queue provenance, and the job state is
  spoolctl's own verdict; the execution detail is in `runner.json` and is
  unchanged. `job.json` is not part of the report projection, so report hashes
  are unaffected.
- A `failure_reason` evalctl does not recognize now maps to `E_RUNNER_FAILED`
  rather than being an error, so a future spoolctl adding an enum member does
  not become an evalctl outage. `E_SPOOLCTL_INCOMPATIBLE` is never raised for
  an unrecognized reason, and the required spoolctl version is unchanged.
- The fake spoolctl test fixture now synthesizes real spoolctl's attempt record
  field-for-field. It previously emitted `duration_ms` and omitted
  `failure_reason` -- the inverse of the real tool -- which is why the duration
  bug survived two releases undetected. Fake and real binary are asserted
  against one shared key set, and the real-spoolctl CI job asserts a queued
  0.5s case records a real elapsed duration. The fixture's `show` envelope was
  flat where the real one nests the job, which is what hid the dead job-state
  read above; the envelope's top-level key set is now part of the same
  two-sided check.

## 0.4.2 - 2026-07-28

Spoolctl compatibility fix, real-spoolctl CI coverage, and an internal module
split. `contract_version` remains `1`; command names, envelope shape, artifact
layout, and report projection remain compatible with v0.4.

This release also carries everything listed under `0.4.1` below, which was
never tagged or published.

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
- Queued-run semantics are now covered against a real spoolctl binary, not only
  the fake fixture. A `queue-integration` CI job installs pinned
  `spoolctl==0.4.11` and runs `tests/test_real_spoolctl.py`, which checks report
  hash parity between queued and in-process runs, timeout and spawn-failure
  mapping, the `stdin:"task"` wrapper, resume, and queue provenance. Setting
  `EVALCTL_REQUIRE_REAL_SPOOLCTL=1` turns a missing or below-floor binary into a
  hard failure, so the job cannot pass by skipping its own tests. Ordinary local
  test discovery still passes with spoolctl absent.
- The fake spoolctl fixture writes its database through a same-directory
  temporary file and `os.replace`, serializes the full load-mutate-save under a
  lock, and assigns job ids from a persisted counter. This fixes intermittent
  Python 3.13 CI failures caused by a reader observing a partially written
  fixture database.
- `evalctl/cli.py` was split into focused modules covering static contracts,
  process execution, artifacts, scoring, optional integrations, run state,
  runner orchestration, suite reports, doctor, and command handlers. No public
  behavior changed: the CLI grammar, envelopes, and goldens are identical.

## 0.4.1 - 2026-07-24

Never tagged or published; these changes first ship in `0.4.2` above.

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
