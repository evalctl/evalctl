# Changelog

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
