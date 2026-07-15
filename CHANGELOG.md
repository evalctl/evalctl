# Changelog

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
