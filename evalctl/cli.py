from __future__ import annotations

import concurrent.futures
import fnmatch
import hashlib
import json
import math
import os
import re
import shlex
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from . import __version__

CONTRACT_VERSION = 1
TOOL = "evalctl"
DEFAULT_COMMAND_SCORER_TIMEOUT_SECONDS = 30
DEFAULT_RESERVATION_TTL_SECONDS = 3600
SAFE_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")
BUILTIN_SCORERS = ("contains", "regex", "exact", "json-schema", "numeric-threshold", "file-exists", "exit-code", "workspace-diff")

EXIT_CODES = {
    0: {"meaning": "success", "retryable": None},
    1: {"meaning": "user-input error", "retryable": False},
    2: {"meaning": "safety block", "retryable": False},
    3: {"meaning": "tool-environment error", "retryable": None},
    4: {"meaning": "transient failure", "retryable": True},
    5: {"meaning": "conflict", "retryable": False},
    6: {"meaning": "eval failure", "retryable": False},
}

CODE_REGISTRY = {
    "E_CASE_INVALID": {"class": "user-input", "exit": 1, "where": ["validate", "run"], "retryable": False, "surface": "envelope"},
    "E_SCHEMA_VIOLATION": {"class": "user-input", "exit": 1, "where": ["validate", "run"], "retryable": False, "surface": "envelope"},
    "E_SUITE_NOT_FOUND": {"class": "user-input", "exit": 1, "where": ["run", "report", "validate"], "retryable": False, "surface": "envelope"},
    "E_RUN_NOT_FOUND": {"class": "user-input", "exit": 1, "where": ["status", "report", "resume"], "retryable": False, "surface": "envelope"},
    "E_RUN_CORRUPT": {"class": "user-input", "exit": 1, "where": ["resume"], "retryable": False, "surface": "envelope"},
    "E_RUNNER_FAILED": {"class": "tool-env", "exit": 3, "where": ["run"], "retryable": None, "surface": "runner_json"},
    "E_RUNNER_TIMEOUT": {"class": "tool-env", "exit": 3, "where": ["run"], "retryable": None, "surface": "runner_json"},
    "E_SPOOLCTL_UNAVAILABLE": {"class": "tool-env", "exit": 3, "where": ["run", "resume"], "retryable": False, "surface": "envelope"},
    "E_SPOOLCTL_INCOMPATIBLE": {"class": "tool-env", "exit": 3, "where": ["run", "resume"], "retryable": False, "surface": "envelope"},
    "E_JOB_TRANSIENT": {"class": "transient", "exit": 4, "where": ["run", "resume"], "retryable": True, "surface": "envelope"},
    "E_SCORER_FAILED": {"class": "tool-env", "exit": 3, "where": ["run", "report"], "retryable": None, "surface": "envelope"},
    "E_SCORER_CASE_FAILED": {"class": "tool-env", "where": ["run", "replay"], "surface": "score_json"},
    "E_RUN_BUSY": {"class": "transient", "exit": 4, "where": ["run", "resume"], "retryable": True, "surface": "envelope"},
    "E_RUN_CONFLICT": {"class": "conflict", "exit": 5, "where": ["run", "init", "replay", "suite", "case", "scorer"], "retryable": False, "surface": "envelope"},
    "W_UNSANDBOXED_RUNNER": {"class": "warning", "where": ["run", "replay"], "surface": "envelope"},
    "W_REPLAY_CASE_ABSENT": {"class": "warning", "where": ["replay"], "surface": "envelope"},
    "W_NOTHING_TO_REPLAY": {"class": "warning", "where": ["replay"], "surface": "envelope"},
    "W_TEXT_DIFF_APPROXIMATED": {"class": "warning", "where": ["run"], "surface": "envelope"},
    "W_OUTPUT_TRUNCATED": {"class": "warning", "where": ["run"], "surface": "envelope"},
    "W_PATH_UNREADABLE": {"class": "warning", "where": ["run"], "surface": "envelope"},
    "W_PARTIAL_RUN": {"class": "warning", "where": ["run", "report"], "surface": "envelope"},
    "W_RESERVATION_RECLAIMED": {"class": "warning", "where": ["run", "resume"], "surface": "envelope"},
    "W_RESUME_NOTHING_PENDING": {"class": "warning", "where": ["resume"], "surface": "envelope"},
}


class EvalctlError(Exception):
    def __init__(self, code: str, message: str, hint: str, exit_code: int = 1, **ctx: Any) -> None:
        super().__init__(message)
        self.error = {"code": code, "message": message, "hint": hint, "exit_code": exit_code, **ctx}
        self.exit_code = exit_code


def now_iso() -> str:
    source_date_epoch = os.environ.get("SOURCE_DATE_EPOCH")
    if source_date_epoch:
        try:
            ts = int(source_date_epoch)
        except ValueError:
            raise EvalctlError("E_CASE_INVALID", f"SOURCE_DATE_EPOCH must be an integer Unix timestamp (got {source_date_epoch})", "unset SOURCE_DATE_EPOCH or set it to seconds since epoch", 1)
        return datetime.fromtimestamp(ts, timezone.utc).isoformat().replace("+00:00", "Z")
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def stable_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_text(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def sha256_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def envelope(data: Any, *, ok: bool = True, warnings: list[dict[str, Any]] | None = None,
             commands: list[dict[str, Any]] | None = None, errors: list[dict[str, Any]] | None = None,
             started: float | None = None) -> dict[str, Any]:
    started = started or time.time()
    payload = data if ok else None
    meta: dict[str, Any] = {
        "request_id": "req_" + uuid.uuid4().hex[:20],
        "ts_iso": now_iso(),
        "data_hash": sha256_text(stable_json(payload)) if ok else None,
        "contract_version": CONTRACT_VERSION,
        "elapsed_ms": int((time.time() - started) * 1000),
    }
    return {
        "ok": ok,
        "tool_version": __version__,
        "data": payload,
        "meta": meta,
        "warnings": warnings or [],
        "commands": commands or [],
        "errors": errors or [],
    }


def wants_json(argv: list[str]) -> bool:
    return "--json" in argv or "--format" in argv and value_after(argv, "--format") == "json" or not sys.stdout.isatty()


def value_after(argv: list[str], flag: str, default: str | None = None) -> str | None:
    if flag not in argv:
        return default
    idx = argv.index(flag)
    if idx + 1 >= len(argv):
        raise EvalctlError("E_CASE_INVALID", f"{flag} requires a value", f"try: {TOOL} --help", 1)
    return argv[idx + 1]


def has_flag(argv: list[str], flag: str) -> bool:
    return flag in argv


def strip_flags(argv: list[str], flags_with_values: set[str], bool_flags: set[str]) -> list[str]:
    out: list[str] = []
    i = 0
    while i < len(argv):
        item = argv[i]
        if item in bool_flags:
            i += 1
        elif item in flags_with_values:
            i += 2
        else:
            out.append(item)
            i += 1
    return out


def print_envelope(data: Any, *, json_mode: bool, human: str | None = None, warnings: list[dict[str, Any]] | None = None,
                   commands: list[dict[str, Any]] | None = None, started: float | None = None) -> int:
    if json_mode:
        print(stable_json(envelope(data, warnings=warnings, commands=commands, started=started)))
    else:
        print(human if human is not None else json.dumps(data, indent=2, sort_keys=True))
    return 0


def print_error(err: EvalctlError, *, json_mode: bool, started: float | None = None) -> int:
    print(err.error["message"], file=sys.stderr)
    if json_mode:
        print(stable_json(envelope(None, ok=False, errors=[err.error], started=started)))
    return err.exit_code


def help_text() -> str:
    return f"""evalctl  Local-first evals for agents.

USAGE: evalctl <command> [flags]

COMMANDS:
  capabilities --json              Machine contract
  schema <verb> --json             Output schema for a verb
  robot-docs guide                 Agent workflow handbook
  init [--force]                   Scaffold evals/ with code-review suite
  validate [suite] [--json]        Validate suite files
  run <suite> [--jobs N] [--timeout S] [--run-id ID] [--resume ID] [--queue spoolctl] [--slots N] [--reservation-ttl S] [--fail-on-fail] [--json]
  jobs list|get|prune [--yes] [--json]
  replay --failed <run-id|--run-dir PATH> [--suite S] [--run-id NEW] [--force] [--json]
  suite add <name> [--runner-argv ARGV|--runner-command CMD --shell] [--json]
  case add <suite> --task TEXT --workspace PATH [--id ID] [--diff PATH] [--expect-json JSON] [--json]
  scorer add <suite> --name NAME [--required|--advisory] [--id ID] [--argv ARGV|--command CMD --shell] [--json]
  status <run-id|--run-dir PATH> [--json]
  report <run-id|--run-dir PATH> [--format markdown|json] [--json]

GLOBAL FLAGS:
  --json           Structured envelope output
  --no-color       Suppress ANSI
  --version        Print version
  --help, -h       Show help

EXIT CODES: 0 ok; 1 input; 2 safety; 3 environment; 4 transient; 5 conflict; 6 eval failed.
AGENT/AUTOMATION:
  Machine contract: evalctl capabilities --json
  Workflow guide:   evalctl robot-docs guide
  Schemas:          evalctl schema <verb> --json
"""


def capabilities_data() -> dict[str, Any]:
    try:
        spool = probe_spoolctl()
        spoolctl_status = {"available": True, "planned": False, "minimum_version": "0.4.1", "version": spool.get("version") or spool.get("tool_version")}
    except EvalctlError:
        spoolctl_status = {"available": False, "planned": False, "minimum_version": "0.4.1"}
    verbs = {
        "capabilities": {"description": "Return the machine contract.", "json": True, "mutates": False, "flags": ["--json"], "exit_codes": [0]},
        "schema": {"description": "Return output schemas.", "json": True, "mutates": False, "args": ["verb"], "flags": ["--json"], "exit_codes": [0, 1]},
        "robot-docs": {"description": "Return agent workflow guide.", "json": False, "mutates": False, "args": ["guide"], "exit_codes": [0, 1]},
        "init": {"description": "Scaffold evals/ tree with sample code-review suite.", "json": True, "mutates": True, "flags": ["--json", "--force"], "exit_codes": [0, 5]},
        "validate": {"description": "Validate suite.json, cases.jsonl, fixtures, scorer refs, and runner config.", "json": True, "mutates": False, "args": ["suite"], "flags": ["--json"], "exit_codes": [0, 1]},
        "run": {"description": "Run a suite and produce a portable, resumable run directory.", "json": True, "mutates": True, "args": ["suite"], "flags": ["--json", "--jobs", "--timeout", "--run-id", "--resume", "--queue", "--slots", "--reservation-ttl", "--fail-on-fail"], "exit_codes": [0, 1, 3, 4, 5, 6]},
        "jobs": {"description": "Inspect and prune local run/reservation/queue state.", "json": True, "mutates": True, "args": ["list", "get", "prune"], "flags": ["--json", "--yes", "--force"], "exit_codes": [0, 1]},
        "replay": {"description": "Re-execute failed/errored cases from a source run into a linked partial run.", "json": True, "mutates": True, "args": ["run-id"], "flags": ["--json", "--failed", "--run-dir", "--suite", "--run-id", "--force", "--jobs", "--timeout", "--fail-on-fail"], "exit_codes": [0, 1, 3, 4, 5, 6]},
        "suite": {"description": "Author suites, including suite add.", "json": True, "mutates": True, "args": ["add", "name"], "flags": ["--json", "--runner-argv", "--runner-command", "--shell"], "exit_codes": [0, 1, 5]},
        "case": {"description": "Author cases, including case add.", "json": True, "mutates": True, "args": ["add", "suite"], "flags": ["--json", "--task", "--workspace", "--id", "--diff", "--expect-json"], "exit_codes": [0, 1, 5]},
        "scorer": {"description": "Author scorers, including built-in and command scorers.", "json": True, "mutates": True, "args": ["add", "suite"], "flags": ["--json", "--name", "--required", "--advisory", "--id", "--argv", "--command", "--shell", "--timeout"], "exit_codes": [0, 1, 5]},
        "status": {"description": "Diagnose run state.", "json": True, "mutates": False, "args": ["run-id"], "flags": ["--json", "--run-dir"], "exit_codes": [0, 1]},
        "report": {"description": "Generate markdown or JSON report from run artifacts.", "json": True, "mutates": False, "args": ["run-id"], "flags": ["--json", "--format", "--run-dir"], "exit_codes": [0, 1, 3]},
    }
    return {
        "tool_name": TOOL,
        "contract_version": CONTRACT_VERSION,
        "features": ["universal_envelope", "deterministic_output", "artifact_replay", "workspace_diff", "authoring", "execution_replay", "command_scorer", "durable_runs", "resumable", "run_state_jobs", "queue_spoolctl"],
        "verbs": verbs,
        "global_flags": {"--json": "structured envelope", "--help": "help", "--version": "version", "--no-color": "suppress ANSI"},
        "exit_codes": {str(k): v for k, v in EXIT_CODES.items()},
        "error_codes": CODE_REGISTRY,
        "env_vars": {
            "EVALCTL_CASE_FILE": "materialized case JSON passed to runner",
            "EVALCTL_WORKSPACE": "fresh per-case workspace",
            "EVALCTL_OUTPUT_FILE": "runner response destination",
            "EVALCTL_TASK_FILE": "task text file",
            "EVALCTL_DIFF_FILE": "review diff file when present",
            "SOURCE_DATE_EPOCH": "controls deterministic timestamps, including run created_ts",
        },
        "integrations": {
            "spoolctl": spoolctl_status,
            "inferctl": {"available": False, "planned": True},
        },
        "schemas_uri": "evalctl schema <verb> --json",
        "robot_docs_uri": "evalctl robot-docs guide",
    }


def schema_object(required: list[str], properties: dict[str, Any], *, additional: bool = True) -> dict[str, Any]:
    return {"type": "object", "required": required, "properties": properties, "additionalProperties": additional}


RUN_SUMMARY_SCHEMA = schema_object(
    ["ok", "case_count", "status_counts"],
    {
        "ok": {"type": "boolean"},
        "case_count": {"type": "integer", "minimum": 0},
        "status_counts": {"type": "object", "additionalProperties": {"type": "integer", "minimum": 0}},
    },
)

DATA_SCHEMAS = {
    "capabilities": schema_object(
        ["tool_name", "contract_version", "features", "verbs", "global_flags", "exit_codes", "error_codes", "env_vars", "integrations", "schemas_uri", "robot_docs_uri"],
        {
            "tool_name": {"type": "string"},
            "contract_version": {"type": "integer"},
            "features": {"type": "array", "items": {"type": "string"}},
            "verbs": {"type": "object", "additionalProperties": {"type": "object"}},
            "global_flags": {"type": "object", "additionalProperties": {"type": "string"}},
            "exit_codes": {"type": "object", "additionalProperties": {"type": "object"}},
            "error_codes": {"type": "object", "additionalProperties": schema_object(["class", "where", "surface"], {"class": {"type": "string"}, "exit": {"type": "integer"}, "where": {"type": "array", "items": {"type": "string"}}, "retryable": {"type": ["boolean", "null"]}, "surface": {"type": "string"}})},
            "env_vars": {"type": "object", "additionalProperties": {"type": "string"}},
            "integrations": {"type": "object", "additionalProperties": {"type": "object"}},
            "schemas_uri": {"type": "string"},
            "robot_docs_uri": {"type": "string"},
        },
    ),
    "schema": schema_object(
        ["envelope_schema", "schemas", "definitions"],
        {
            "envelope_schema": {"type": "object"},
            "schemas": {"type": "object", "additionalProperties": {"type": "object"}},
            "definitions": {"type": "object"},
        },
    ),
    "init": schema_object(
        ["created", "suite", "files"],
        {"created": {"type": "string"}, "suite": {"type": "string"}, "files": {"type": "array", "items": {"type": "string"}}},
    ),
    "validate": schema_object(
        ["suite", "case_count", "valid"],
        {"suite": {"type": "string"}, "case_count": {"type": "integer", "minimum": 0}, "valid": {"type": "boolean"}},
    ),
    "run": schema_object(
        ["run_id", "run_dir", "run", "report_hash"],
        {"run_id": {"type": "string"}, "run_dir": {"type": "string"}, "run": RUN_SUMMARY_SCHEMA, "report_hash": {"type": "string"}, "existing": {"type": "boolean"}, "queue": {"type": "object"}},
    ),
    "jobs": schema_object(
        [],
        {
            "runs": {"type": "array", "items": {"type": "object"}},
            "count": {"type": "integer", "minimum": 0},
            "run_id": {"type": "string"},
            "run_dir": {"type": "string"},
            "state": {"type": "string"},
            "reservation": {"type": "object"},
            "cases": {"type": "object"},
            "queue_jobs": {"type": "array", "items": {"type": "object"}},
            "confirmed": {"type": "boolean"},
            "candidates": {"type": "object"},
            "removed": {"type": "object"},
            "refused": {"type": "array", "items": {"type": "object"}},
        },
    ),
    "replay": schema_object(
        ["replayed_from", "cases_replayed"],
        {
            "replayed_from": {"type": "string"},
            "cases_replayed": {"type": "integer", "minimum": 0},
            "run_id": {"type": "string"},
            "run_dir": {"type": "string"},
            "run": RUN_SUMMARY_SCHEMA,
            "report_hash": {"type": "string"},
        },
    ),
    "suite": schema_object(
        ["suite", "suite_dir", "created", "files"],
        {
            "suite": {"type": "string"},
            "suite_dir": {"type": "string"},
            "created": {"type": "boolean"},
            "files": {"type": "array", "items": {"type": "string"}},
        },
    ),
    "case": schema_object(
        ["suite", "id", "created", "case"],
        {
            "suite": {"type": "string"},
            "id": {"type": "string"},
            "created": {"type": "boolean"},
            "case": {"type": "object"},
        },
    ),
    "scorer": schema_object(
        ["suite", "scorer", "created"],
        {
            "suite": {"type": "string"},
            "scorer": {"type": "string"},
            "id": {"type": ["string", "null"]},
            "created": {"type": "boolean"},
        },
    ),
    "status": schema_object(
        ["run_id", "run_dir", "run", "cases", "recommended_action"],
        {
            "run_id": {"type": "string"},
            "run_dir": {"type": "string"},
            "run": RUN_SUMMARY_SCHEMA,
            "cases": {"type": "array", "items": {"type": "object"}},
            "recommended_action": schema_object(["command", "rationale", "alternatives"], {"command": {"type": "string"}, "rationale": {"type": "string"}, "alternatives": {"type": "array", "items": {"type": "string"}}}),
        },
    ),
    "report": schema_object(
        ["run", "failures", "cases", "run_id", "report_hash"],
        {
            "run": schema_object(["ok", "suite", "case_count", "status_counts"], {"ok": {"type": "boolean"}, "suite": {"type": "string"}, "case_count": {"type": "integer", "minimum": 0}, "status_counts": {"type": "object", "additionalProperties": {"type": "integer", "minimum": 0}}}),
            "failures": {"type": "array", "items": {"type": "object"}},
            "cases": {"type": "array", "items": {"type": "object"}},
            "run_id": {"type": "string"},
            "report_hash": {"type": "string"},
        },
    ),
}


def schema_data(verb: str | None = None) -> dict[str, Any]:
    envelope_schema = {
        "type": "object",
        "required": ["ok", "tool_version", "data", "meta", "warnings", "commands", "errors"],
        "properties": {
            "ok": {"type": "boolean"},
            "tool_version": {"type": "string"},
            "data": {},
            "meta": {"type": "object"},
            "warnings": {"type": "array"},
            "commands": {"type": "array"},
            "errors": {"type": "array"},
        },
    }
    schemas = DATA_SCHEMAS
    if verb:
        if verb not in schemas:
            raise EvalctlError("E_CASE_INVALID", f"unknown schema verb '{verb}'", "try: evalctl capabilities --json", 1)
        schemas = {verb: schemas[verb]}
    return {"envelope_schema": envelope_schema, "schemas": schemas, "definitions": {}}


def robot_docs() -> str:
    return """# evalctl - Agent Workflow Guide

## Quick reference

Capabilities: `evalctl capabilities --json`
Schemas: `evalctl schema run --json`
Initialize: `evalctl init --json`
Validate: `evalctl validate code-review --json`
Run: `evalctl run code-review --json`
Inspect: `evalctl status <run-id> --json`
Report: `evalctl report <run-id> --format json`
Artifact replay: `evalctl report --run-dir <copied-run-dir> --format json`

## v0.3 workflow

1. Scaffold `evals/suites/code-review/` with `evalctl init`.
2. Or author a new suite without hand-editing JSON:
   `evalctl suite add demo --runner-argv "python3 $EVALCTL_WORKSPACE/r.py" --json`;
   create fixtures; `evalctl case add demo --task "..." --workspace fixtures/x --json`;
   `evalctl scorer add demo --name exact --required --json`.
3. Run `evalctl validate <suite> --json` before executing local code.
4. Run `evalctl run <suite> --json`. The runner is arbitrary local code; evalctl is not a sandbox.
5. If a run is interrupted, use `evalctl run --resume <run-id> --json`. Resume uses
   `run.json`, terminal `cases/<id>/state.json` markers, and the original suite snapshot;
   it skips terminal cases and re-runs only unfinished cases.
6. Use `jobs list|get|prune --json` to inspect completed, running, stale, and orphaned
   local run state. Reservations are TTL files with a background heartbeat; no daemon or
   lock server is required.
7. Optionally use `evalctl run <suite> --queue spoolctl --json` to delegate runner
   execution to spoolctl. Spoolctl is optional and must be >= 0.4.1; absent or incompatible
   spoolctl is a hard error only when `--queue spoolctl` is requested. The queue DB is
   per-run `.spoolctl.db`, so externally managed cross-machine workers require a shared
   filesystem and are not a general hosted-worker mode.
8. Use `status` for run state and recommended next command.
9. Use `report --format json` for a deterministic report envelope or `--format markdown` for a human report.
10. Copy a completed run directory anywhere and run `report --run-dir <path> --format json`;
   evalctl recomputes scores from report artifacts and does not require durability sidecars.
11. After fixing a failed runner/fixture, run `evalctl replay --failed <run-id> --json`
   to re-execute only failed/errored cases into a fresh partial run. `replay --run-id`
   names the destination run, not the source.

## Command scorers

`evalctl scorer add <suite> --name command --id judge1 --argv "python3 scorer.py"`
adds an external scorer. A command scorer receives `EVALCTL_CASE_FILE`,
`EVALCTL_OUTPUT_FILE`, and `EVALCTL_WORKSPACE`, emits one JSON verdict, and is executed
once at run time. Its normalized verdict is stored under
`cases/<case_id>/scorers/<id>.json`; reports and artifact replay read that artifact and
never re-execute the scorer binary. Command scorers execute arbitrary local code and are
covered by the same unsandboxed warning as runners.

## Exit-code branching

`0` success, `1` input error, `2` safety block, `3` tool environment error, `4` retryable transient, `5` conflict, `6` eval failure from `run --fail-on-fail`.

## Error-code surfaces

Codes with `surface:"envelope"` appear in `errors[]` or `warnings[]` and predict
the command's process-exit class. Codes with `surface:"runner_json"` appear as
per-case `runner.json.error_code` reason codes. Codes with `surface:"score_json"`
appear as per-case scorer verdict reason codes, for example
`E_SCORER_CASE_FAILED` in `cases/<id>/scorers/<scorer_id>.json` or `score.json`.
A runner timeout, runner spawn failure, or command-scorer failure is reportable
case data: `run`/`replay` exits 0 by default, exits 6 with `--fail-on-fail`, emits
`W_PARTIAL_RUN`, and does not put the per-case reason code in `errors[]`.

## Durable runs

Fresh runs write `run.json` once before execution and terminal
`cases/<case_id>/state.json` markers after each case's report artifacts are complete.
`manifest.json` is finalized from that durable state. `SOURCE_DATE_EPOCH` controls
`created_ts`, which makes manifest parity tests deterministic. Durability sidecars are
operational state and are not part of `report_hash`.

## Artifact writes

JSON artifacts are written by creating a temporary file in the target directory
and replacing the final path with `os.replace`. This guarantees atomic visibility:
readers never see a half-written final JSON file. It is not a full crash-durable
guarantee; v0.1.1 does not fsync files or directories.

## Deferred

Compare, inferctl route capture, externally managed shared worker fleets, and
LLM-as-judge scoring are roadmap items, not v0.3 commands.
"""


def resolve_suite(suite: str | None) -> Path:
    suite = suite or "code-review"
    direct = Path(suite)
    if direct.exists():
        return direct
    candidate = Path("evals") / "suites" / suite
    if candidate.exists():
        return candidate
    raise EvalctlError("E_SUITE_NOT_FOUND", f"suite not found: {suite}", "try: evalctl init && evalctl validate code-review --json", 1)


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        raise EvalctlError("E_SUITE_NOT_FOUND", f"missing file: {path}", f"create {path} or run evalctl init", 1)
    except json.JSONDecodeError as exc:
        raise EvalctlError("E_SCHEMA_VIOLATION", f"invalid JSON in {path}: {exc.msg}", f"fix {path}:{exc.lineno}", 1)


def load_cases(cases_path: Path) -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    seen: set[str] = set()
    try:
        lines = cases_path.read_text().splitlines()
    except FileNotFoundError:
        raise EvalctlError("E_SUITE_NOT_FOUND", f"missing file: {cases_path}", f"create {cases_path}", 1)
    for line_no, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            case = json.loads(line)
        except json.JSONDecodeError as exc:
            raise EvalctlError("E_CASE_INVALID", f"invalid JSONL at {cases_path}:{line_no}: {exc.msg}", f"fix line {line_no}", 1)
        case_id = case.get("id") or sha256_text(stable_json(case))[7:19]
        if case_id in seen:
            raise EvalctlError("E_CASE_INVALID", f"duplicate case id: {case_id}", "choose unique case ids", 1)
        seen.add(case_id)
        case["id"] = case_id
        cases.append(case)
    return cases


def validate_suite(suite_dir: Path) -> dict[str, Any]:
    suite = read_json(suite_dir / "suite.json")
    runner = suite.get("runner") or {}
    shell = bool(runner.get("shell", False))
    has_argv = isinstance(runner.get("argv"), list) and bool(runner.get("argv"))
    has_cmd = isinstance(runner.get("command"), str) and bool(runner.get("command"))
    if shell and not has_cmd or not shell and not has_argv:
        raise EvalctlError("E_SCHEMA_VIOLATION", "runner must define argv for shell:false or command for shell:true", f"fix {suite_dir/'suite.json'} runner", 1)
    if has_argv and has_cmd:
        raise EvalctlError("E_SCHEMA_VIOLATION", "runner must not define both argv and command", f"fix {suite_dir/'suite.json'} runner", 1)
    cases = load_cases(suite_dir / suite.get("cases", "cases.jsonl"))
    for case in cases:
        for key in ("task", "workspace"):
            if key not in case:
                raise EvalctlError("E_CASE_INVALID", f"case {case.get('id')} missing {key}", f"add {key} to case", 1)
        workspace = suite_dir / case["workspace"]
        if not workspace.exists():
            raise EvalctlError("E_CASE_INVALID", f"case {case['id']} workspace missing: {case['workspace']}", "fix workspace path", 1)
        if case.get("diff") and not (suite_dir / case["diff"]).exists():
            raise EvalctlError("E_CASE_INVALID", f"case {case['id']} diff missing: {case['diff']}", "fix diff path", 1)
    return {"suite": suite.get("name", suite_dir.name), "case_count": len(cases), "valid": True}


def init_project(force: bool = False) -> dict[str, Any]:
    root = Path("evals")
    if root.exists() and not force:
        raise EvalctlError("E_RUN_CONFLICT", "evals/ already exists; refusing to overwrite", "try: evalctl validate code-review --json, or evalctl init --force to replace sample files", 5)
    suite = root / "suites" / "code-review"
    if force and suite.exists():
        shutil.rmtree(suite)
    (suite / "fixtures" / "cr-pass").mkdir(parents=True, exist_ok=True)
    (suite / "fixtures" / "cr-fail").mkdir(parents=True, exist_ok=True)
    (root / "runs").mkdir(parents=True, exist_ok=True)
    suite_json = {
        "name": "code-review",
        "cases": "cases.jsonl",
        "acknowledged_unsandboxed_runner": True,
        "runner": {
            "argv": ["python3", "$EVALCTL_WORKSPACE/runner.py"],
            "shell": False,
            "command": None,
            "cwd": None,
            "timeout_seconds": 30,
            "max_output_bytes": 1048576,
            "env_allowlist": ["PATH", "HOME"],
            "redact_env_values": [],
            "redact_patterns": ["(?i)secret[=:]\\s*\\S+", "(?i)token[=:]\\s*\\S+"],
        },
        "scorers": [
            {"name": "contains", "required": True},
            {"name": "exit-code", "required": True},
            {"name": "file-exists", "required": True},
            {"name": "workspace-diff", "required": True},
        ],
    }
    cases = [
        {"id": "cr-pass", "task": "Review this diff and report the null dereference.", "workspace": "fixtures/cr-pass", "diff": "fixtures/cr-pass/change.diff", "expect": {"text_contains": ["null dereference", "src/app.py:7"], "files_exist": ["review.md"], "files_changed": ["review.md"], "files_forbidden": ["src/secrets.py"], "exit_code": 0}},
        {"id": "cr-fail", "task": "Review this diff and avoid touching secrets.", "workspace": "fixtures/cr-fail", "diff": "fixtures/cr-fail/change.diff", "expect": {"text_contains": ["bounds check"], "files_exist": ["review.md"], "files_changed": ["review.md"], "files_forbidden": ["src/secrets.py"], "exit_code": 0}},
    ]
    (suite / "suite.json").write_text(json.dumps(suite_json, indent=2, sort_keys=True) + "\n")
    (suite / "cases.jsonl").write_text("\n".join(stable_json(c) for c in cases) + "\n")
    for case_name, body, diff, runner in [
        ("cr-pass", "value = payload.get('name')\nprint(value.upper())\n", "--- a/src/app.py\n+++ b/src/app.py\n@@ -4,4 +4,4 @@\n-print(value)\n+print(value.upper())\n", "from pathlib import Path\nimport os\nPath(os.environ['EVALCTL_OUTPUT_FILE']).write_text('Found null dereference at src/app.py:7\\n')\nPath('review.md').write_text('Found null dereference at src/app.py:7\\n')\n"),
        ("cr-fail", "items = []\nprint(items[3])\n", "--- a/src/app.py\n+++ b/src/app.py\n@@ -1,2 +1,2 @@\n-print(items[0])\n+print(items[3])\n", "from pathlib import Path\nimport os\nPath(os.environ['EVALCTL_OUTPUT_FILE']).write_text('Looks fine\\n')\nPath('src').mkdir(exist_ok=True)\nPath('src/secrets.py').write_text('token=redacted\\n')\n"),
    ]:
        case_dir = suite / "fixtures" / case_name
        (case_dir / "src").mkdir(exist_ok=True)
        (case_dir / "src" / "app.py").write_text(body)
        (case_dir / "change.diff").write_text(diff)
        (case_dir / "runner.py").write_text(runner)
    return {"created": str(root), "suite": "code-review", "files": ["evals/suites/code-review/suite.json", "evals/suites/code-review/cases.jsonl"]}


def normalize_rel(path: Path, root: Path) -> str:
    rel = path.relative_to(root)
    parts = rel.parts
    posix = PurePosixPath(*parts).as_posix()
    if posix in ("", ".") or ".." in parts:
        raise ValueError("invalid relative path")
    posix.encode("utf-8", "strict")
    return posix


def display_path_name(path: Path) -> str:
    return os.fsencode(path.name).decode("utf-8", "replace")


def manifest(root: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    warnings: list[dict[str, Any]] = []
    entries: list[dict[str, Any]] = []
    for path in sorted(root.rglob("*"), key=os.fsencode):
        try:
            rel = normalize_rel(path, root)
            st = path.lstat()
            if stat.S_ISDIR(st.st_mode):
                entries.append({"path": rel, "kind": "directory"})
            elif stat.S_ISLNK(st.st_mode):
                target = os.readlink(path)
                entries.append({"path": rel, "kind": "symlink", "target": target, "sha256": sha256_bytes(target.encode("utf-8")), "broken": not path.exists()})
            elif stat.S_ISREG(st.st_mode):
                entries.append({"path": rel, "kind": "file", "sha256": sha256_bytes(path.read_bytes()), "size": st.st_size})
            else:
                subtype = "fifo" if stat.S_ISFIFO(st.st_mode) else "socket" if stat.S_ISSOCK(st.st_mode) else "device" if stat.S_ISCHR(st.st_mode) or stat.S_ISBLK(st.st_mode) else "unknown"
                entries.append({"path": rel, "kind": "other", "subtype": subtype})
        except Exception as exc:
            warnings.append({"code": "W_PATH_UNREADABLE", "message": f"could not read path {display_path_name(path)}: {exc}"})
    return {"root": ".", "entries": entries}, warnings


def diff_manifests(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    b = {e["path"]: e for e in before["entries"]}
    a = {e["path"]: e for e in after["entries"]}
    changed = []
    for path in sorted(set(b) | set(a)):
        if path not in b:
            changed.append({"path": path, "status": "added", "kind": a[path]["kind"], "sha256_before": None, "sha256_after": a[path].get("sha256")})
        elif path not in a:
            changed.append({"path": path, "status": "deleted", "kind": b[path]["kind"], "sha256_before": b[path].get("sha256"), "sha256_after": None})
        elif b[path].get("sha256") != a[path].get("sha256") or b[path].get("kind") != a[path].get("kind"):
            changed.append({"path": path, "status": "modified", "kind": a[path]["kind"], "sha256_before": b[path].get("sha256"), "sha256_after": a[path].get("sha256")})
    return {"changed_paths": changed}


def apply_redaction(text: str, patterns: list[str], values: list[str]) -> tuple[str, bool]:
    changed = False
    for value in values:
        if value and value in text:
            text = text.replace(value, "[REDACTED]")
            changed = True
    for pattern in patterns:
        text2 = re.sub(pattern, "[REDACTED]", text)
        changed = changed or text2 != text
        text = text2
    return text, changed


def _atomic_write(path: Path, text: str, *, _writer: Any | None = None) -> None:
    writer = _writer or (lambda tmp_path, value: tmp_path.write_text(value))
    tmp_name = None
    with tempfile.NamedTemporaryFile("w", dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", delete=False) as tmp:
        tmp_name = tmp.name
    tmp_path = Path(tmp_name)
    try:
        writer(tmp_path, text)
        os.replace(tmp_path, path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()


def write_json(path: Path, data: Any) -> None:
    _atomic_write(path, json.dumps(data, indent=2, sort_keys=True) + "\n")


def parse_jobs(argv: list[str]) -> int:
    raw = value_after(argv, "--jobs")
    if raw is None:
        return min(os.cpu_count() or 1, 4)
    try:
        jobs = int(raw)
    except ValueError:
        raise EvalctlError("E_CASE_INVALID", f"--jobs must be a positive integer (got {raw})", "try: evalctl run code-review --jobs 4 --json", 1)
    if jobs < 1:
        raise EvalctlError("E_CASE_INVALID", f"--jobs must be at least 1 (got {jobs})", "try: evalctl run code-review --jobs 1 --json", 1)
    return jobs


def parse_positive_int_flag(argv: list[str], flag: str, default: int) -> int:
    raw = value_after(argv, flag)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise EvalctlError("E_CASE_INVALID", f"{flag} must be a positive integer (got {raw})", f"provide {flag} as a positive integer", 1)
    if value < 1:
        raise EvalctlError("E_CASE_INVALID", f"{flag} must be at least 1 (got {value})", f"provide {flag} as a positive integer", 1)
    return value


def version_tuple(value: str) -> tuple[int, ...]:
    parts = []
    for item in value.split("."):
        match = re.match(r"(\d+)", item)
        parts.append(int(match.group(1)) if match else 0)
    return tuple(parts)


def spoolctl_binary() -> str:
    path = shutil.which("spoolctl")
    if not path:
        raise EvalctlError("E_SPOOLCTL_UNAVAILABLE", "spoolctl is not available on PATH", "install spoolctl >= 0.4.1 or drop --queue spoolctl", 3)
    return path


def spoolctl_json(args: list[str], *, allow_exit_codes: set[int] | None = None) -> dict[str, Any]:
    allow_exit_codes = allow_exit_codes or {0}
    try:
        result = subprocess.run([spoolctl_binary(), *args], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except OSError as exc:
        raise EvalctlError("E_SPOOLCTL_UNAVAILABLE", f"could not run spoolctl: {exc}", "install spoolctl >= 0.4.1 or drop --queue spoolctl", 3)
    if result.returncode == 4:
        raise EvalctlError("E_JOB_TRANSIENT", "spoolctl reported a transient job-system failure", "retry the queued run or resume it later", 4)
    if result.returncode not in allow_exit_codes:
        raise EvalctlError("E_SPOOLCTL_INCOMPATIBLE", f"spoolctl command failed: {result.stderr.strip() or result.stdout.strip()}", "upgrade spoolctl to >= 0.4.1 or drop --queue spoolctl", 3)
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise EvalctlError("E_SPOOLCTL_INCOMPATIBLE", f"spoolctl returned invalid JSON: {exc.msg}", "upgrade spoolctl to >= 0.4.1 or drop --queue spoolctl", 3)
    if isinstance(payload, dict) and "ok" in payload:
        if not payload.get("ok") and result.returncode != 6:
            raise EvalctlError("E_SPOOLCTL_INCOMPATIBLE", "spoolctl returned an error envelope", "inspect spoolctl output or drop --queue spoolctl", 3)
        data = payload.get("data")
        return data if isinstance(data, dict) else {"value": data}
    if isinstance(payload, dict):
        return payload
    raise EvalctlError("E_SPOOLCTL_INCOMPATIBLE", "spoolctl JSON output must be an object", "upgrade spoolctl to >= 0.4.1 or drop --queue spoolctl", 3)


def probe_spoolctl() -> dict[str, Any]:
    data = spoolctl_json(["capabilities", "--json"])
    version = str(data.get("version") or data.get("tool_version") or "")
    contract = str(data.get("contract_version") or "")
    verbs = data.get("verbs", {})
    add_flags = set()
    if isinstance(verbs, dict):
        add_info = verbs.get("add", {})
        if isinstance(add_info, dict):
            add_flags = set(add_info.get("flags", []))
    if version_tuple(version) < (0, 4, 1) or contract != "1" or not {"--cwd", "--env", "--max-crashes"} <= add_flags:
        raise EvalctlError("E_SPOOLCTL_INCOMPATIBLE", "spoolctl is missing required evalctl queue capabilities", "upgrade spoolctl to >= 0.4.1", 3)
    return data


def render_runner_arg(arg: str, env: dict[str, str]) -> str:
    for key, value in env.items():
        arg = arg.replace(f"${key}", value)
    return arg


def is_safe_id(value: str) -> bool:
    return bool(value) and value not in {".", ".."} and ".." not in value and bool(SAFE_ID_RE.fullmatch(value))


def validate_suite_name(name: str) -> None:
    if not is_safe_id(name) or "/" in name or "\\" in name:
        raise EvalctlError("E_CASE_INVALID", f"invalid suite name: {name}", "use a simple name with letters, numbers, dot, underscore, or dash", 1)


def normalize_suite_rel(raw: str, *, field: str) -> str:
    if not raw:
        raise EvalctlError("E_CASE_INVALID", f"{field} must not be empty", f"provide {field}", 1)
    if "\\" in raw:
        raise EvalctlError("E_CASE_INVALID", f"{field} must use POSIX-style relative paths", "use forward slashes and stay under the suite directory", 1)
    path = PurePosixPath(raw)
    if path.is_absolute() or any(part == ".." for part in path.parts):
        raise EvalctlError("E_CASE_INVALID", f"{field} must stay under the suite directory", "use a relative path without ..", 1)
    normalized = path.as_posix()
    if normalized in {"", "."}:
        raise EvalctlError("E_CASE_INVALID", f"{field} must name a path", f"provide {field}", 1)
    return normalized


def decode_subprocess_output(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return value.decode("utf-8", "replace")


def score_summary(score: dict[str, Any]) -> dict[str, Any]:
    out = {"scorer": score["scorer"], "ok": score["ok"], "score": score["score"]}
    if "id" in score:
        out["id"] = score["id"]
    return out


def case_manifest_entry(case: dict[str, Any], status: str, scores: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "id": case["id"],
        "input_hash": sha256_text(stable_json(case)),
        "status": status,
        "scores": [score_summary(s) for s in scores],
        "artifacts": {
            "input": f"cases/{case['id']}/input.json",
            "output": f"cases/{case['id']}/output.txt",
            "runner": f"cases/{case['id']}/runner.json",
            "workspace_before": f"cases/{case['id']}/workspace-before.json",
            "workspace_after": f"cases/{case['id']}/workspace-after.json",
            "diff_manifest": f"cases/{case['id']}/workspace-diff.json",
            "diff": f"cases/{case['id']}/workspace.diff",
            "score": f"cases/{case['id']}/score.json",
        },
    }


TERMINAL_CASE_STATUSES = {"pass", "fail", "error", "canceled"}


def write_terminal_marker(case_dir: Path, case_id: str, status: str) -> None:
    write_json(case_dir / "state.json", {"id": case_id, "status": status, "completed_ts": now_iso()})


def is_terminal_marker(path: Path) -> bool:
    try:
        marker = read_json(path)
    except Exception:
        return False
    return marker.get("status") in TERMINAL_CASE_STATUSES


def terminal_marker_count(run_dir: Path) -> int:
    cases_dir = run_dir / "cases"
    if not cases_dir.exists():
        return 0
    return sum(1 for marker in cases_dir.glob("*/state.json") if is_terminal_marker(marker))


def case_entry_from_artifacts(run_dir: Path, case_id: str) -> dict[str, Any]:
    case_dir = run_dir / "cases" / case_id
    case = read_json(case_dir / "input.json")
    score_doc = read_json(case_dir / "score.json")
    return case_manifest_entry(case, score_doc["status"], score_doc["scores"])


def reservation_path(run_dir: Path) -> Path:
    return run_dir / ".reservation.json"


def parse_iso_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def read_reservation(run_dir: Path) -> dict[str, Any] | None:
    path = reservation_path(run_dir)
    if not path.exists():
        return None
    try:
        data = read_json(path)
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def reservation_is_live(record: dict[str, Any], *, now: datetime | None = None) -> bool:
    try:
        ttl_seconds = int(record["ttl_seconds"])
        heartbeat_ts = parse_iso_timestamp(str(record["heartbeat_ts"]))
    except Exception:
        return False
    now = now or datetime.now(timezone.utc)
    return (now - heartbeat_ts).total_seconds() < ttl_seconds


def write_reservation(run_dir: Path, run_id: str, ttl_seconds: int) -> None:
    write_json(reservation_path(run_dir), {
        "run_id": run_id,
        "pid": os.getpid(),
        "host": socket.gethostname(),
        "started_ts": now_iso(),
        "heartbeat_ts": now_iso(),
        "ttl_seconds": ttl_seconds,
    })


def clear_reservation(run_dir: Path) -> None:
    try:
        reservation_path(run_dir).unlink()
    except FileNotFoundError:
        pass


class ReservationHeartbeat:
    def __init__(self, run_dir: Path, run_id: str, ttl_seconds: int) -> None:
        self.run_dir = run_dir
        self.run_id = run_id
        self.ttl_seconds = ttl_seconds
        self.stop = threading.Event()
        self.thread: threading.Thread | None = None

    def __enter__(self) -> "ReservationHeartbeat":
        write_reservation(self.run_dir, self.run_id, self.ttl_seconds)
        interval = max(0.1, min(5.0, self.ttl_seconds / 4))
        self.thread = threading.Thread(target=self._run, args=(interval,), daemon=True)
        self.thread.start()
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.stop.set()
        if self.thread:
            self.thread.join(timeout=1)

    def _run(self, interval: float) -> None:
        while not self.stop.wait(interval):
            try:
                record = read_reservation(self.run_dir) or {}
                record.update({
                    "run_id": self.run_id,
                    "pid": os.getpid(),
                    "host": socket.gethostname(),
                    "heartbeat_ts": now_iso(),
                    "ttl_seconds": self.ttl_seconds,
                })
                record.setdefault("started_ts", now_iso())
                write_json(reservation_path(self.run_dir), record)
            except Exception:
                pass


def split_completed_and_pending(run_dir: Path, cases: list[dict[str, Any]]) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    completed: dict[str, dict[str, Any]] = {}
    pending: list[dict[str, Any]] = []
    for case in sorted(cases, key=lambda c: c["id"]):
        marker_path = run_dir / "cases" / case["id"] / "state.json"
        if is_terminal_marker(marker_path):
            try:
                completed[case["id"]] = case_entry_from_artifacts(run_dir, case["id"])
                continue
            except Exception:
                pass
        pending.append(case)
    return completed, pending


def clean_pending_case_dirs(run_dir: Path, pending_cases: list[dict[str, Any]]) -> None:
    for case in pending_cases:
        shutil.rmtree(run_dir / "cases" / case["id"], ignore_errors=True)


def synthesize_case_error(suite_dir: Path, suite: dict[str, Any], case: dict[str, Any], run_dir: Path, exc: BaseException) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    warnings: list[dict[str, Any]] = []
    case_dir = run_dir / "cases" / case["id"]
    case_dir.mkdir(parents=True, exist_ok=True)
    workspace = case_dir / "workspace"
    if not workspace.exists():
        shutil.copytree(suite_dir / case["workspace"], workspace)
    input_json = case_dir / "input.json"
    task_txt = case_dir / "task.txt"
    output_file = case_dir / "output.txt"
    write_json(input_json, case)
    task_txt.write_text(case["task"])
    if case.get("diff"):
        diff_src = suite_dir / case["diff"]
        diff_file = case_dir / "input.diff"
        if not diff_file.exists():
            shutil.copyfile(diff_src, diff_file)
    before, mw = manifest(workspace)
    warnings.extend(mw)
    after, mw = manifest(workspace)
    warnings.extend(mw)
    diff = diff_manifests(before, after)
    output_file.write_text("")
    (case_dir / "runner.stdout.txt").write_text("")
    (case_dir / "runner.stderr.txt").write_text(str(exc))
    runner_json = {"exit_code": None, "signal": None, "timed_out": False, "spawn_failed": True, "error_code": "E_RUNNER_FAILED", "duration_ms": 0,
                   "stdout_truncated": False, "stderr_truncated": False, "output_truncated": False,
                   "stdout_redacted": False, "stderr_redacted": False, "output_redacted": False}
    write_json(case_dir / "runner.json", runner_json)
    write_json(case_dir / "workspace-before.json", before)
    write_json(case_dir / "workspace-after.json", after)
    write_json(case_dir / "workspace-diff.json", diff)
    (case_dir / "workspace.diff").write_text(render_text_diff(diff))
    scores = score_case(case, "", runner_json, after, diff, suite.get("scorers", []), case_dir=case_dir, execute=False, suite=suite)
    score_doc = {"case_id": case["id"], "status": "error", "ok": False, "scores": scores}
    write_json(case_dir / "score.json", score_doc)
    write_terminal_marker(case_dir, case["id"], "error")
    return case_manifest_entry(case, "error", scores), warnings


def prepare_case_workspace(suite_dir: Path, suite: dict[str, Any], case: dict[str, Any], run_dir: Path,
                           timeout_override: int | None) -> dict[str, Any]:
    warnings: list[dict[str, Any]] = []
    case_dir = run_dir / "cases" / case["id"]
    case_dir.mkdir(parents=True, exist_ok=True)
    workspace = case_dir / "workspace"
    shutil.copytree(suite_dir / case["workspace"], workspace)
    input_json = case_dir / "input.json"
    task_txt = case_dir / "task.txt"
    output_file = case_dir / "output.txt"
    write_json(input_json, case)
    task_txt.write_text(case["task"])
    diff_src = suite_dir / case["diff"] if case.get("diff") else None
    diff_file = case_dir / "input.diff"
    if diff_src:
        shutil.copyfile(diff_src, diff_file)
    before, mw = manifest(workspace)
    warnings.extend(mw)
    write_json(case_dir / "workspace-before.json", before)
    runner = suite["runner"]
    timeout = int(timeout_override or runner.get("timeout_seconds") or 300)
    max_bytes = int(runner.get("max_output_bytes") or 5 * 1024 * 1024)
    env = {k: os.environ[k] for k in runner.get("env_allowlist", []) if k in os.environ}
    eval_env = {
        "EVALCTL_CASE_FILE": str(input_json.resolve()),
        "EVALCTL_WORKSPACE": str(workspace.resolve()),
        "EVALCTL_OUTPUT_FILE": str(output_file.resolve()),
        "EVALCTL_TASK_FILE": str(task_txt.resolve()),
        "EVALCTL_DIFF_FILE": str(diff_file.resolve() if diff_src else ""),
    }
    env.update(eval_env)
    env_values = [os.environ.get(k, "") for k in runner.get("redact_env_values", [])]
    patterns = runner.get("redact_patterns", [])
    cwd = Path(render_runner_arg(runner.get("cwd") or str(workspace), eval_env))
    return {
        "suite": suite,
        "case": case,
        "case_dir": case_dir,
        "workspace": workspace,
        "output_file": output_file,
        "task_txt": task_txt,
        "diff_file": diff_file,
        "diff_src": diff_src,
        "before": before,
        "runner": runner,
        "timeout": timeout,
        "max_bytes": max_bytes,
        "env": env,
        "eval_env": eval_env,
        "env_values": env_values,
        "patterns": patterns,
        "cwd": cwd,
        "warnings": warnings,
    }


def execute_runner_in_process(prepared: dict[str, Any]) -> dict[str, Any]:
    runner = prepared["runner"]
    case = prepared["case"]
    eval_env = prepared["eval_env"]
    started = time.time()
    timed_out = False
    spawn_failed = False
    exit_code: int | None = None
    signal_value: int | None = None
    stdout = ""
    stderr = ""
    proc: subprocess.Popen[str] | None = None
    try:
        stdin = subprocess.PIPE if runner.get("stdin") == "task" else None
        input_text = case["task"] if runner.get("stdin") == "task" else None
        if runner.get("shell", False):
            cmd: str | list[str] = render_runner_arg(runner["command"], eval_env)
            proc = subprocess.Popen(cmd, shell=True, cwd=prepared["cwd"], env=prepared["env"], text=True, stdin=stdin,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        else:
            argv = [render_runner_arg(str(a), eval_env) for a in runner["argv"]]
            proc = subprocess.Popen(argv, shell=False, cwd=prepared["cwd"], env=prepared["env"], text=True, stdin=stdin,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        stdout, stderr = proc.communicate(input=input_text, timeout=prepared["timeout"])
        exit_code = proc.returncode
        if proc.returncode < 0:
            signal_value = -proc.returncode
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        exit_code = None
        if proc is not None:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except ProcessLookupError:
                pass
            drained_stdout, drained_stderr = proc.communicate()
        else:
            drained_stdout, drained_stderr = "", ""
        stdout = decode_subprocess_output(exc.stdout) + decode_subprocess_output(drained_stdout)
        stderr = decode_subprocess_output(exc.stderr) + decode_subprocess_output(drained_stderr)
    except OSError as exc:
        spawn_failed = True
        exit_code = None
        stderr = str(exc)
    duration_ms = int((time.time() - started) * 1000)
    return {
        "stdout": stdout,
        "stderr": stderr,
        "timed_out": timed_out,
        "spawn_failed": spawn_failed,
        "exit_code": exit_code,
        "signal": signal_value,
        "duration_ms": duration_ms,
    }


def normalize_runner_artifacts(prepared: dict[str, Any], runner_result: dict[str, Any]) -> tuple[str, dict[str, Any], list[dict[str, Any]]]:
    warnings: list[dict[str, Any]] = []
    case_dir = prepared["case_dir"]
    output_file = prepared["output_file"]
    max_bytes = prepared["max_bytes"]
    stdout = runner_result["stdout"]
    stderr = runner_result["stderr"]
    trunc_stdout = len(stdout.encode()) > max_bytes
    trunc_stderr = len(stderr.encode()) > max_bytes
    stdout = stdout.encode()[:max_bytes].decode("utf-8", "replace")
    stderr = stderr.encode()[:max_bytes].decode("utf-8", "replace")
    stdout, red_stdout = apply_redaction(stdout, prepared["patterns"], prepared["env_values"])
    stderr, red_stderr = apply_redaction(stderr, prepared["patterns"], prepared["env_values"])
    output_truncated = False
    if output_file.exists():
        output_bytes = output_file.read_bytes()
        output_truncated = len(output_bytes) > max_bytes
        output_text = output_bytes[:max_bytes].decode("utf-8", "replace")
    else:
        output_text = stdout
    output_text, red_output = apply_redaction(output_text, prepared["patterns"], prepared["env_values"])
    output_file.write_text(output_text)
    (case_dir / "runner.stdout.txt").write_text(stdout)
    (case_dir / "runner.stderr.txt").write_text(stderr)
    error_code = "E_RUNNER_TIMEOUT" if runner_result["timed_out"] else "E_RUNNER_FAILED" if runner_result["spawn_failed"] else None
    runner_json = {"exit_code": runner_result["exit_code"], "signal": runner_result["signal"], "timed_out": runner_result["timed_out"], "spawn_failed": runner_result["spawn_failed"], "error_code": error_code, "duration_ms": runner_result["duration_ms"],
                   "stdout_truncated": trunc_stdout, "stderr_truncated": trunc_stderr, "output_truncated": output_truncated,
                   "stdout_redacted": red_stdout, "stderr_redacted": red_stderr, "output_redacted": red_output}
    if trunc_stdout or trunc_stderr or output_truncated:
        warnings.append({"code": "W_OUTPUT_TRUNCATED", "message": "runner output exceeded max_output_bytes"})
    write_json(case_dir / "runner.json", runner_json)
    return output_text, runner_json, warnings


def capture_workspace_after_and_score(prepared: dict[str, Any], output_text: str, runner_json: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    warnings: list[dict[str, Any]] = []
    case = prepared["case"]
    suite = prepared["suite"]
    case_dir = prepared["case_dir"]
    after, mw = manifest(prepared["workspace"])
    warnings.extend(mw)
    diff = diff_manifests(prepared["before"], after)
    write_json(case_dir / "workspace-after.json", after)
    write_json(case_dir / "workspace-diff.json", diff)
    (case_dir / "workspace.diff").write_text(render_text_diff(diff))
    status = "error" if runner_json["timed_out"] or runner_json["spawn_failed"] else "pass"
    scores = score_case(case, output_text, runner_json, after, diff, suite.get("scorers", []), case_dir=case_dir, execute=True, suite=suite, eval_env=prepared["eval_env"])
    if any(s.get("error") for s in scores):
        status = "error"
    elif status != "error" and not all(s["ok"] for s in scores if s.get("required", True)):
        status = "fail"
    score_doc = {"case_id": case["id"], "status": status, "ok": status == "pass", "scores": scores}
    write_json(case_dir / "score.json", score_doc)
    return case_manifest_entry(case, status, scores), warnings


def spoolctl_runner_command(prepared: dict[str, Any]) -> list[str]:
    runner = prepared["runner"]
    eval_env = prepared["eval_env"]
    if runner.get("shell", False):
        command = render_runner_arg(runner["command"], eval_env)
        if runner.get("stdin") == "task":
            command = f"exec {command} < \"$EVALCTL_TASK_FILE\""
        return ["sh", "-c", command]
    argv = [render_runner_arg(str(a), eval_env) for a in runner["argv"]]
    if runner.get("stdin") != "task":
        return argv
    wrapper = (
        "import os, subprocess, sys\n"
        "with open(os.environ['EVALCTL_TASK_FILE']) as stdin:\n"
        "    raise SystemExit(subprocess.call(sys.argv[1:], stdin=stdin))\n"
    )
    return [sys.executable, "-c", wrapper, *argv]


def spoolctl_add_case(db_path: Path, run_id: str, prepared: dict[str, Any]) -> str:
    case_id = prepared["case"]["id"]
    args = [
        "add", "--db", str(db_path), "--json", "--queue", "evalctl",
        "--key", f"{run_id}:{case_id}",
        "--tag", f"evalctl_run={run_id}",
        "--tag", f"evalctl_case={case_id}",
        "--cwd", str(prepared["cwd"]),
        "--timeout", str(prepared["timeout"]),
        "--max-retries", "0",
        "--max-crashes", "0",
    ]
    for key, value in sorted(prepared["eval_env"].items()):
        args.extend(["--env", f"{key}={value}"])
    args.append("--")
    args.extend(spoolctl_runner_command(prepared))
    data = spoolctl_json(args)
    job_id = str(data.get("job_id") or data.get("id") or "")
    if not job_id:
        raise EvalctlError("E_SPOOLCTL_INCOMPATIBLE", "spoolctl add did not return data.job_id", "upgrade spoolctl to >= 0.4.1", 3)
    write_json(prepared["case_dir"] / "job.json", {"job_id": job_id, "state": data.get("state", "queued")})
    return job_id


def latest_terminal_attempt(job_detail: dict[str, Any]) -> dict[str, Any]:
    attempts = job_detail.get("attempts")
    if not isinstance(attempts, list) or not attempts:
        raise EvalctlError("E_SPOOLCTL_INCOMPATIBLE", "spoolctl show did not include attempts", "upgrade spoolctl to >= 0.4.1", 3)
    return attempts[-1]


def runner_result_from_spoolctl_attempt(attempt: dict[str, Any], max_bytes: int) -> dict[str, Any]:
    stdout_path = attempt.get("stdout_path")
    stderr_path = attempt.get("stderr_path")
    stdout = Path(stdout_path).read_text(errors="replace") if stdout_path else ""
    stderr = Path(stderr_path).read_text(errors="replace") if stderr_path else str(attempt.get("error") or "")
    state = attempt.get("state")
    exit_code = attempt.get("exit_code")
    timed_out = state == "timed_out" and exit_code is None
    spawn_failed = exit_code is None and (state in {"failed", "abandoned", "canceled"} or str(attempt.get("error") or "").startswith("spawn failed:"))
    return {
        "stdout": stdout,
        "stderr": stderr,
        "timed_out": timed_out,
        "spawn_failed": spawn_failed,
        "exit_code": exit_code,
        "signal": None,
        "duration_ms": int(attempt.get("duration_ms") or 0),
    }


def execute_spoolctl_pending_cases(suite_dir: Path, suite: dict[str, Any], all_cases: list[dict[str, Any]], pending_cases: list[dict[str, Any]],
                                   completed_entries: dict[str, dict[str, Any]], run_dir: Path, run_id: str,
                                   jobs: int, timeout_override: int | None, slots: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    db_path = run_dir / ".spoolctl.db"
    clean_pending_case_dirs(run_dir, pending_cases)
    prepared_by_case: dict[str, dict[str, Any]] = {}
    job_ids: list[str] = []
    for case in sorted(pending_cases, key=lambda c: c["id"]):
        prepared = prepare_case_workspace(suite_dir, suite, case, run_dir, timeout_override)
        prepared_by_case[case["id"]] = prepared
        job_ids.append(spoolctl_add_case(db_path, run_id, prepared))
    worker = subprocess.Popen([spoolctl_binary(), "work", "--db", str(db_path), "--queue", "evalctl", "--slots", str(slots), "--drain"],
                              text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    wait_data = spoolctl_json(["wait", "--db", str(db_path), "--json", *job_ids], allow_exit_codes={0, 6})
    worker_stdout, worker_stderr = worker.communicate(timeout=60)
    if worker.returncode == 4:
        raise EvalctlError("E_JOB_TRANSIENT", "spoolctl worker reported a transient failure", "retry the queued run or resume it later", 4)
    if worker.returncode not in {0, None}:
        raise EvalctlError("E_SPOOLCTL_INCOMPATIBLE", f"spoolctl worker failed: {worker_stderr or worker_stdout}", "inspect spoolctl worker output", 3)
    warnings: list[dict[str, Any]] = []
    entries_by_id = dict(completed_entries)
    for case in sorted(pending_cases, key=lambda c: c["id"]):
        prepared = prepared_by_case[case["id"]]
        job_doc = read_json(prepared["case_dir"] / "job.json")
        detail = spoolctl_json(["show", "--db", str(db_path), "--json", job_doc["job_id"]])
        attempt = latest_terminal_attempt(detail)
        runner_result = runner_result_from_spoolctl_attempt(attempt, prepared["max_bytes"])
        output_text, runner_json, normalize_warnings = normalize_runner_artifacts(prepared, runner_result)
        warnings.extend(prepared["warnings"])
        warnings.extend(normalize_warnings)
        entry, score_warnings = capture_workspace_after_and_score(prepared, output_text, runner_json)
        warnings.extend(score_warnings)
        write_terminal_marker(prepared["case_dir"], case["id"], entry["status"])
        write_json(prepared["case_dir"] / "job.json", {"job_id": job_doc["job_id"], "state": detail.get("state") or attempt.get("state")})
        entries_by_id[case["id"]] = entry
    case_entries = [entries_by_id[case["id"]] for case in sorted(all_cases, key=lambda c: c["id"])]
    return case_entries, warnings


def run_case(suite_dir: Path, suite: dict[str, Any], case: dict[str, Any], run_dir: Path, timeout_override: int | None) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    prepared = prepare_case_workspace(suite_dir, suite, case, run_dir, timeout_override)
    all_warnings = list(prepared["warnings"])
    runner_result = execute_runner_in_process(prepared)
    output_text, runner_json, warnings = normalize_runner_artifacts(prepared, runner_result)
    all_warnings.extend(warnings)
    entry, warnings = capture_workspace_after_and_score(prepared, output_text, runner_json)
    all_warnings.extend(warnings)
    write_terminal_marker(prepared["case_dir"], case["id"], entry["status"])
    return entry, all_warnings


def render_text_diff(diff: dict[str, Any]) -> str:
    return "\n".join(f"{p['status']}\t{p['path']}" for p in diff["changed_paths"]) + ("\n" if diff["changed_paths"] else "")


def score_case(case: dict[str, Any], output_text: str, runner_json: dict[str, Any], after: dict[str, Any], diff: dict[str, Any],
               scorers: list[dict[str, Any]], *, case_dir: Path | None = None, execute: bool = False,
               suite: dict[str, Any] | None = None, eval_env: dict[str, str] | None = None) -> list[dict[str, Any]]:
    expect = case.get("expect", {})
    configured = scorers or [{"name": n, "required": True} for n in BUILTIN_SCORERS]
    out: list[dict[str, Any]] = []
    for scorer in configured:
        name = scorer["name"]
        required = bool(scorer.get("required", True))
        try:
            result = run_scorer(scorer, expect, output_text, runner_json, after, diff, required=required,
                                case_dir=case_dir, execute=execute, suite=suite or {}, eval_env=eval_env or {})
            if result is None:
                continue
            result["required"] = required
            out.append(result)
        except Exception as exc:
            result = {"scorer": name, "ok": False, "score": 0.0, "label": "error", "required": required, "findings": [{"why": str(exc)}], "error": True}
            if scorer.get("id"):
                result["id"] = scorer["id"]
            if name == "command":
                result["error_code"] = "E_SCORER_CASE_FAILED"
            out.append(result)
    return out


def scorer_failure(scorer: dict[str, Any], required: bool, why: str) -> dict[str, Any]:
    result = {
        "scorer": scorer.get("name", "command"),
        "ok": False,
        "score": 0.0,
        "label": "error",
        "required": required,
        "findings": [{"why": why}],
        "error": True,
        "error_code": "E_SCORER_CASE_FAILED",
    }
    if scorer.get("id"):
        result["id"] = scorer["id"]
    return result


def normalize_command_verdict(raw: Any, scorer: dict[str, Any], required: bool) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return scorer_failure(scorer, required, "command scorer stdout must be one JSON object")
    if not isinstance(raw.get("ok"), bool):
        return scorer_failure(scorer, required, "command scorer verdict requires boolean ok")
    score = raw.get("score")
    if not isinstance(score, (int, float)) or isinstance(score, bool) or not math.isfinite(float(score)) or not 0.0 <= float(score) <= 1.0:
        return scorer_failure(scorer, required, "command scorer verdict requires finite numeric score in 0.0..1.0")
    label = raw.get("label")
    if label is None or label == "":
        label = "pass" if raw["ok"] else "fail"
    if not isinstance(label, str):
        return scorer_failure(scorer, required, "command scorer verdict label must be a string")
    findings_raw = raw.get("findings")
    if not isinstance(findings_raw, list):
        return scorer_failure(scorer, required, "command scorer verdict requires findings list")
    findings: list[dict[str, Any]] = []
    for item in findings_raw:
        if isinstance(item, str):
            findings.append({"why": item})
        elif isinstance(item, dict):
            findings.append(item)
        else:
            return scorer_failure(scorer, required, "command scorer findings must be objects or strings")
    result = {
        "scorer": "command",
        "id": scorer["id"],
        "ok": raw["ok"],
        "score": float(score),
        "label": label,
        "required": required,
        "findings": findings,
    }
    if bool(raw.get("error")):
        result.update({"ok": False, "score": 0.0, "label": "error", "error": True, "error_code": "E_SCORER_CASE_FAILED"})
    return result


def run_command_scorer(scorer: dict[str, Any], required: bool, case_dir: Path | None, execute: bool,
                       suite: dict[str, Any], eval_env: dict[str, str]) -> dict[str, Any]:
    scorer_id = str(scorer.get("id", ""))
    if not is_safe_id(scorer_id):
        return scorer_failure(scorer, required, "command scorer id must be path-safe")
    if case_dir is None:
        return scorer_failure(scorer, required, "command scorer requires case_dir")
    scorers_dir = case_dir / "scorers"
    verdict_path = scorers_dir / f"{scorer_id}.json"
    if not execute:
        verdict = read_json(verdict_path)
        if not isinstance(verdict, dict):
            return scorer_failure(scorer, required, "command scorer verdict artifact must be an object")
        return verdict
    scorers_dir.mkdir(parents=True, exist_ok=True)
    timeout = int(scorer.get("timeout_seconds") or DEFAULT_COMMAND_SCORER_TIMEOUT_SECONDS)
    runner = suite.get("runner", {})
    max_bytes = int(scorer.get("max_output_bytes") or runner.get("max_output_bytes") or 5 * 1024 * 1024)
    env = {k: os.environ[k] for k in runner.get("env_allowlist", []) if k in os.environ}
    env.update(eval_env)
    env_values = [os.environ.get(k, "") for k in runner.get("redact_env_values", [])]
    patterns = runner.get("redact_patterns", [])
    stdout = ""
    stderr = ""
    proc: subprocess.Popen[str] | None = None
    try:
        if scorer.get("shell", False):
            command = scorer.get("command")
            if not isinstance(command, str) or not command:
                result = scorer_failure(scorer, required, "command scorer shell mode requires command")
                write_json(verdict_path, result)
                return result
            cmd: str | list[str] = render_runner_arg(command, eval_env)
            proc = subprocess.Popen(cmd, shell=True, cwd=case_dir / "workspace", env=env, text=True,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        else:
            argv_raw = scorer.get("argv")
            if not isinstance(argv_raw, list) or not argv_raw:
                result = scorer_failure(scorer, required, "command scorer requires argv when shell:false")
                write_json(verdict_path, result)
                return result
            cmd = [render_runner_arg(str(a), eval_env) for a in argv_raw]
            proc = subprocess.Popen(cmd, shell=False, cwd=case_dir / "workspace", env=env, text=True,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        stdout, stderr = proc.communicate(timeout=timeout)
        if proc.returncode != 0:
            result = scorer_failure(scorer, required, f"command scorer exited {proc.returncode}")
        else:
            try:
                raw = json.loads(stdout.strip())
            except json.JSONDecodeError as exc:
                result = scorer_failure(scorer, required, f"invalid command scorer JSON: {exc.msg}")
            else:
                result = normalize_command_verdict(raw, scorer, required)
    except subprocess.TimeoutExpired as exc:
        if proc is not None:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except ProcessLookupError:
                pass
            drained_stdout, drained_stderr = proc.communicate()
            stdout = decode_subprocess_output(exc.stdout) + decode_subprocess_output(drained_stdout)
            stderr = decode_subprocess_output(exc.stderr) + decode_subprocess_output(drained_stderr)
        result = scorer_failure(scorer, required, f"command scorer timed out after {timeout}s")
    except OSError as exc:
        result = scorer_failure(scorer, required, f"command scorer spawn failed: {exc}")
    stdout = stdout.encode()[:max_bytes].decode("utf-8", "replace")
    stderr = stderr.encode()[:max_bytes].decode("utf-8", "replace")
    stdout, _ = apply_redaction(stdout, patterns, env_values)
    stderr, _ = apply_redaction(stderr, patterns, env_values)
    stdout = stdout.encode()[:max_bytes].decode("utf-8", "replace")
    stderr = stderr.encode()[:max_bytes].decode("utf-8", "replace")
    (scorers_dir / f"{scorer_id}.stdout.txt").write_text(stdout)
    (scorers_dir / f"{scorer_id}.stderr.txt").write_text(stderr)
    write_json(verdict_path, result)
    return result


def run_scorer(scorer: dict[str, Any], expect: dict[str, Any], output_text: str, runner_json: dict[str, Any],
               after: dict[str, Any], diff: dict[str, Any], *, required: bool, case_dir: Path | None,
               execute: bool, suite: dict[str, Any], eval_env: dict[str, str]) -> dict[str, Any] | None:
    name = scorer["name"]
    findings: list[dict[str, Any]] = []
    if name == "command":
        return run_command_scorer(scorer, required, case_dir, execute, suite, eval_env)
    elif name == "exact":
        if "exact" not in expect:
            return None
        ok = output_text == expect["exact"]
    elif name == "contains":
        values = expect.get("text_contains", [])
        if not values:
            return None
        missing = [v for v in values if v not in output_text]
        findings = [{"why": "missing text", "value": v} for v in missing]
        ok = not missing
    elif name == "regex":
        values = expect.get("text_regex", [])
        if not values:
            return None
        missing = [v for v in values if not re.search(v, output_text, re.MULTILINE)]
        findings = [{"why": "regex did not match", "value": v} for v in missing]
        ok = not missing
    elif name == "json-schema":
        if "json_schema" not in expect:
            return None
        try:
            json.loads(output_text)
            ok = True
        except json.JSONDecodeError as exc:
            ok = False
            findings = [{"why": f"output is not JSON: {exc.msg}"}]
    elif name == "numeric-threshold":
        threshold = expect.get("numeric_threshold")
        if not threshold:
            return None
        data = json.loads(output_text)
        value = data
        for part in threshold["path"].split("."):
            value = value[part]
        if "gte" in threshold:
            ok = value >= threshold["gte"]
        elif "lte" in threshold:
            ok = value <= threshold["lte"]
        else:
            ok = True
    elif name == "file-exists":
        values = expect.get("files_exist", [])
        if not values:
            return None
        paths = {e["path"] for e in after["entries"]}
        missing = [v for v in values if v not in paths]
        findings = [{"path": v, "why": "expected file missing"} for v in missing]
        ok = not missing
    elif name == "exit-code":
        if "exit_code" not in expect:
            return None
        ok = runner_json.get("exit_code") == expect["exit_code"]
        if not ok:
            findings = [{"why": "exit code mismatch", "expected": expect["exit_code"], "actual": runner_json.get("exit_code")}]
    elif name == "workspace-diff":
        changed = [p["path"] for p in diff["changed_paths"]]
        missing_changed = [p for p in expect.get("files_changed", []) if not any(fnmatch.fnmatch(c, p) for c in changed)]
        forbidden = [p for p in expect.get("files_forbidden", []) for c in changed if fnmatch.fnmatch(c, p)]
        findings = [{"path": p, "why": "expected change missing"} for p in missing_changed] + [{"path": p, "why": "forbidden path modified"} for p in forbidden]
        ok = not findings
    else:
        raise ValueError(f"unknown scorer {name}")
    return {"scorer": name, "ok": ok, "score": 1.0 if ok else 0.0, "label": "pass" if ok else "fail", "findings": findings}


def command_init(argv: list[str], json_mode: bool, started: float) -> int:
    data = init_project(force=has_flag(argv, "--force"))
    return print_envelope(data, json_mode=json_mode, human=f"Created {data['created']} with suite {data['suite']}", started=started)


def command_validate(argv: list[str], json_mode: bool, started: float) -> int:
    args = strip_flags(argv, set(), {"--json", "--no-color"})
    suite_arg = args[1] if len(args) > 1 else "code-review"
    data = validate_suite(resolve_suite(suite_arg))
    return print_envelope(data, json_mode=json_mode, human=f"{data['suite']}: {data['case_count']} cases valid", started=started)


def runner_from_authoring_flags(argv: list[str], *, prefix: str = "--runner") -> dict[str, Any]:
    argv_value = value_after(argv, f"{prefix}-argv")
    command_value = value_after(argv, f"{prefix}-command")
    shell = has_flag(argv, "--shell")
    if bool(argv_value) == bool(command_value):
        raise EvalctlError("E_CASE_INVALID", f"{prefix}-argv and {prefix}-command are mutually exclusive", f"try: {TOOL} suite add demo {prefix}-argv \"python3 $EVALCTL_WORKSPACE/r.py\"", 1)
    if argv_value and shell:
        raise EvalctlError("E_CASE_INVALID", "--shell requires --runner-command, not --runner-argv", "drop --shell or use --runner-command", 1)
    if command_value and not shell:
        raise EvalctlError("E_CASE_INVALID", "--runner-command requires --shell", "use --runner-argv for shell:false runners", 1)
    runner = {
        "argv": shlex.split(argv_value) if argv_value else None,
        "shell": shell,
        "command": command_value if command_value else None,
        "cwd": None,
        "timeout_seconds": 30,
        "max_output_bytes": 1048576,
        "env_allowlist": ["PATH", "HOME"],
        "redact_env_values": [],
        "redact_patterns": [],
    }
    if argv_value and not runner["argv"]:
        raise EvalctlError("E_CASE_INVALID", "--runner-argv must not be empty", "provide at least one argv token", 1)
    return runner


def suite_add_data(name: str, runner: dict[str, Any], *, _validator: Any = validate_suite) -> dict[str, Any]:
    validate_suite_name(name)
    root = Path("evals") / "suites"
    dest = root / name
    suite_json = {
        "name": name,
        "cases": "cases.jsonl",
        "acknowledged_unsandboxed_runner": True,
        "runner": runner,
        "scorers": [],
    }
    suite_text = json.dumps(suite_json, indent=2, sort_keys=True) + "\n"
    if dest.exists():
        existing_suite = dest / "suite.json"
        if existing_suite.exists() and existing_suite.read_text() == suite_text:
            return {"suite": name, "suite_dir": str(dest), "created": False, "files": [str(dest / "suite.json"), str(dest / "cases.jsonl")]}
        raise EvalctlError("E_RUN_CONFLICT", f"suite already exists with different content: {name}", "choose a new suite name or edit the existing suite explicitly", 5)
    root.mkdir(parents=True, exist_ok=True)
    tmp_path = Path(tempfile.mkdtemp(prefix=f".{name}.", dir=root))
    try:
        (tmp_path / "fixtures").mkdir()
        (tmp_path / "suite.json").write_text(suite_text)
        (tmp_path / "cases.jsonl").write_text("")
        _validator(tmp_path)
        os.replace(tmp_path, dest)
    except Exception:
        shutil.rmtree(tmp_path, ignore_errors=True)
        raise
    return {"suite": name, "suite_dir": str(dest), "created": True, "files": [str(dest / "suite.json"), str(dest / "cases.jsonl"), str(dest / "fixtures")]}


def command_suite_add(argv: list[str], json_mode: bool, started: float) -> int:
    args = strip_flags(argv, {"--runner-argv", "--runner-command"}, {"--json", "--no-color", "--shell"})
    if len(args) != 3 or args[1] != "add":
        raise EvalctlError("E_CASE_INVALID", "suite command requires: suite add <name>", "try: evalctl suite add demo --runner-argv \"python3 $EVALCTL_WORKSPACE/r.py\" --json", 1)
    data = suite_add_data(args[2], runner_from_authoring_flags(argv))
    return print_envelope(data, json_mode=json_mode, human=f"{'Created' if data['created'] else 'Exists'} suite {data['suite']}", started=started)


def case_add_data(suite_name: str, task: str, workspace_raw: str, *, case_id: str | None = None,
                  diff_raw: str | None = None, expect_raw: str | None = None) -> dict[str, Any]:
    suite_dir = resolve_suite(suite_name)
    suite = read_json(suite_dir / "suite.json")
    workspace = normalize_suite_rel(workspace_raw, field="--workspace")
    if not (suite_dir / workspace).exists():
        raise EvalctlError("E_CASE_INVALID", f"workspace missing: {workspace}", "create the fixture before adding the case", 1)
    case: dict[str, Any] = {"task": task, "workspace": workspace}
    if diff_raw:
        diff = normalize_suite_rel(diff_raw, field="--diff")
        if not (suite_dir / diff).exists():
            raise EvalctlError("E_CASE_INVALID", f"diff missing: {diff}", "create the diff file before adding the case", 1)
        case["diff"] = diff
    if expect_raw:
        try:
            expect = json.loads(expect_raw)
        except json.JSONDecodeError as exc:
            raise EvalctlError("E_CASE_INVALID", f"--expect-json is invalid JSON: {exc.msg}", "provide a JSON object such as '{\"exact\":\"ok\"}'", 1)
        if not isinstance(expect, dict):
            raise EvalctlError("E_CASE_INVALID", "--expect-json must be a JSON object", "provide scorer expectations as an object", 1)
        case["expect"] = expect
    final_id = case_id or sha256_text(stable_json(case))[7:19]
    if not is_safe_id(final_id):
        raise EvalctlError("E_CASE_INVALID", f"invalid case id: {final_id}", "use a path-safe id", 1)
    case["id"] = final_id
    cases_path = suite_dir / suite.get("cases", "cases.jsonl")
    existing_cases = load_cases(cases_path)
    for existing in existing_cases:
        if existing["id"] == final_id:
            if stable_json(existing) == stable_json(case):
                return {"suite": suite.get("name", suite_dir.name), "id": final_id, "created": False, "case": case}
            raise EvalctlError("E_RUN_CONFLICT", f"case id already exists with different content: {final_id}", "choose a new --id or edit cases.jsonl explicitly", 5)
    old_text = cases_path.read_text()
    new_text = old_text
    if new_text and not new_text.endswith("\n"):
        new_text += "\n"
    new_text += stable_json(case) + "\n"
    _atomic_write(cases_path, new_text)
    try:
        validate_suite(suite_dir)
    except Exception:
        _atomic_write(cases_path, old_text)
        raise
    return {"suite": suite.get("name", suite_dir.name), "id": final_id, "created": True, "case": case}


def command_case_add(argv: list[str], json_mode: bool, started: float) -> int:
    args = strip_flags(argv, {"--task", "--workspace", "--id", "--diff", "--expect-json"}, {"--json", "--no-color"})
    if len(args) != 3 or args[1] != "add":
        raise EvalctlError("E_CASE_INVALID", "case command requires: case add <suite>", "try: evalctl case add demo --task \"do X\" --workspace fixtures/x --json", 1)
    task = value_after(argv, "--task")
    workspace = value_after(argv, "--workspace")
    if not task:
        raise EvalctlError("E_CASE_INVALID", "case add requires --task", "provide --task text", 1)
    if not workspace:
        raise EvalctlError("E_CASE_INVALID", "case add requires --workspace", "provide --workspace fixtures/name", 1)
    data = case_add_data(args[2], task, workspace, case_id=value_after(argv, "--id"), diff_raw=value_after(argv, "--diff"), expect_raw=value_after(argv, "--expect-json"))
    return print_envelope(data, json_mode=json_mode, human=f"{'Added' if data['created'] else 'Exists'} case {data['id']}", started=started)


def command_scorer_config(argv: list[str]) -> dict[str, Any]:
    name = value_after(argv, "--name")
    if not name:
        raise EvalctlError("E_CASE_INVALID", "scorer add requires --name", "provide --name exact or --name command", 1)
    if name not in set(BUILTIN_SCORERS) | {"command"}:
        raise EvalctlError("E_CASE_INVALID", f"unknown scorer name: {name}", f"known scorers: {', '.join(BUILTIN_SCORERS)}, command", 1)
    if has_flag(argv, "--required") and has_flag(argv, "--advisory"):
        raise EvalctlError("E_CASE_INVALID", "--required and --advisory are mutually exclusive", "choose one scorer requirement mode", 1)
    required = not has_flag(argv, "--advisory")
    config: dict[str, Any] = {"name": name, "required": required}
    if name != "command":
        if any(value_after(argv, flag) for flag in ("--argv", "--command", "--timeout")) or has_flag(argv, "--shell"):
            raise EvalctlError("E_CASE_INVALID", "built-in scorers do not accept command runner flags", "use --name command for external scorers", 1)
        if value_after(argv, "--id"):
            config["id"] = value_after(argv, "--id")
        return config
    scorer_id = value_after(argv, "--id")
    if not scorer_id or not is_safe_id(scorer_id):
        raise EvalctlError("E_CASE_INVALID", "command scorer requires a path-safe --id", "use letters, numbers, dot, underscore, or dash", 1)
    argv_value = value_after(argv, "--argv")
    command_value = value_after(argv, "--command")
    if bool(argv_value) == bool(command_value):
        raise EvalctlError("E_CASE_INVALID", "--argv and --command are mutually exclusive for command scorers", "provide exactly one command form", 1)
    shell = has_flag(argv, "--shell")
    if argv_value and shell:
        raise EvalctlError("E_CASE_INVALID", "--shell requires --command, not --argv", "drop --shell or use --command", 1)
    if command_value and not shell:
        raise EvalctlError("E_CASE_INVALID", "--command requires --shell", "use --argv for shell:false scorers", 1)
    config["id"] = scorer_id
    config["shell"] = shell
    if argv_value:
        parsed = shlex.split(argv_value)
        if not parsed:
            raise EvalctlError("E_CASE_INVALID", "--argv must not be empty", "provide at least one argv token", 1)
        config["argv"] = parsed
    else:
        config["command"] = command_value
    timeout = value_after(argv, "--timeout")
    if timeout is not None:
        try:
            parsed_timeout = int(timeout)
        except ValueError:
            raise EvalctlError("E_CASE_INVALID", f"--timeout must be an integer (got {timeout})", "provide seconds as a positive integer", 1)
        if parsed_timeout < 1:
            raise EvalctlError("E_CASE_INVALID", f"--timeout must be at least 1 (got {parsed_timeout})", "provide a positive timeout", 1)
        config["timeout_seconds"] = parsed_timeout
    return config


def scorer_key(config: dict[str, Any]) -> tuple[str, str]:
    if config["name"] == "command":
        return ("command", str(config.get("id", "")))
    return ("builtin", config["name"])


def scorer_add_data(suite_name: str, config: dict[str, Any]) -> dict[str, Any]:
    suite_dir = resolve_suite(suite_name)
    suite_path = suite_dir / "suite.json"
    suite = read_json(suite_path)
    old_text = suite_path.read_text()
    scorers = list(suite.get("scorers", []))
    new_key = scorer_key(config)
    for existing in scorers:
        if scorer_key(existing) == new_key:
            if stable_json(existing) == stable_json(config):
                return {"suite": suite.get("name", suite_dir.name), "scorer": config["name"], "id": config.get("id"), "created": False}
            raise EvalctlError("E_RUN_CONFLICT", f"scorer already exists with different config: {new_key[1]}", "choose a new id/name or edit suite.json explicitly", 5)
    suite["scorers"] = scorers + [config]
    write_json(suite_path, suite)
    try:
        validate_suite(suite_dir)
    except Exception:
        _atomic_write(suite_path, old_text)
        raise
    return {"suite": suite.get("name", suite_dir.name), "scorer": config["name"], "id": config.get("id"), "created": True}


def command_scorer_add(argv: list[str], json_mode: bool, started: float) -> int:
    args = strip_flags(argv, {"--name", "--id", "--argv", "--command", "--timeout"}, {"--json", "--no-color", "--required", "--advisory", "--shell"})
    if len(args) != 3 or args[1] != "add":
        raise EvalctlError("E_CASE_INVALID", "scorer command requires: scorer add <suite>", "try: evalctl scorer add demo --name exact --required --json", 1)
    data = scorer_add_data(args[2], command_scorer_config(argv))
    label = data["id"] or data["scorer"]
    return print_envelope(data, json_mode=json_mode, human=f"{'Added' if data['created'] else 'Exists'} scorer {label}", started=started)


def timeout_seconds_for_run(suite: dict[str, Any], timeout_override: int | None) -> int:
    return int(timeout_override or suite["runner"].get("timeout_seconds", 300))


def build_run_metadata(suite: dict[str, Any], suite_dir: Path, cases: list[dict[str, Any]], run_id: str,
                       jobs: int, timeout_override: int | None, replayed_from: str | None,
                       queue: dict[str, Any] | None = None, mode: str = "synchronous") -> dict[str, Any]:
    return {
        "schema_version": 1,
        "run_id": run_id,
        "created_ts": now_iso(),
        "suite_identity": run_identity(suite, suite_dir, cases),
        "execution": {"mode": mode, "jobs": jobs, "timeout_seconds": timeout_seconds_for_run(suite, timeout_override)},
        "replayed_from": replayed_from,
        "queue": queue,
    }


def write_run_metadata_once(run_dir: Path, metadata: dict[str, Any]) -> None:
    path = run_dir / "run.json"
    if path.exists():
        raise EvalctlError("E_RUN_BUSY", f"run metadata already exists for {metadata['run_id']}", "use evalctl run --resume to continue an incomplete run", 4)
    write_json(path, metadata)


def manifest_from_run_metadata(metadata: dict[str, Any], case_entries: list[dict[str, Any]]) -> dict[str, Any]:
    identity = metadata["suite_identity"]
    manifest_doc: dict[str, Any] = {
        "schema_version": 1,
        "run_id": metadata["run_id"],
        "suite": {"name": identity["suite_name"], "hash": identity["suite_hash"], "case_count": len(identity["cases"])},
        "created_ts": metadata["created_ts"],
        "execution": metadata["execution"],
        "replayed_from": metadata.get("replayed_from"),
        "cases": case_entries,
    }
    if metadata.get("queue") is not None:
        manifest_doc["queue"] = metadata["queue"]
    return manifest_doc


def read_run_metadata(run_dir: Path) -> dict[str, Any]:
    path = run_dir / "run.json"
    if not path.exists():
        raise EvalctlError("E_RUN_NOT_FOUND", f"run metadata not found: {path}", "resume requires a run created with durable metadata", 1)
    try:
        metadata = json.loads(path.read_text())
        if not isinstance(metadata, dict):
            raise ValueError("run.json must be an object")
        for key in ("schema_version", "run_id", "created_ts", "suite_identity", "execution"):
            if key not in metadata:
                raise ValueError(f"run.json missing {key}")
        identity = metadata["suite_identity"]
        if not isinstance(identity, dict) or not isinstance(identity.get("cases"), list):
            raise ValueError("run.json suite_identity is malformed")
        execution = metadata["execution"]
        if not isinstance(execution, dict) or not {"mode", "jobs", "timeout_seconds"} <= set(execution):
            raise ValueError("run.json execution is malformed")
        return metadata
    except EvalctlError:
        raise
    except Exception as exc:
        raise EvalctlError("E_RUN_CORRUPT", f"run metadata is corrupt for {run_dir}: {exc}", "inspect or remove run.json, then retry with a valid run", 1)


def finalize_run(run_dir: Path, metadata: dict[str, Any], case_entries: list[dict[str, Any]]) -> tuple[dict[str, Any], bool]:
    manifest_doc = manifest_from_run_metadata(metadata, case_entries)
    write_json(run_dir / "manifest.json", manifest_doc)
    report = report_data(run_dir)
    (run_dir / "report.md").write_text(markdown_report(report))
    run_ok = all(c["status"] == "pass" for c in case_entries)
    data = {
        "run_id": metadata["run_id"],
        "run_dir": str(run_dir),
        "run": {"ok": run_ok, "case_count": len(case_entries), "status_counts": status_counts(case_entries)},
        "report_hash": report["report_hash"],
    }
    if metadata.get("replayed_from") is not None:
        data["replayed_from"] = metadata["replayed_from"]
        data["cases_replayed"] = len(case_entries)
    return data, run_ok


def execute_pending_cases(suite_dir: Path, suite: dict[str, Any], all_cases: list[dict[str, Any]], pending_cases: list[dict[str, Any]],
                          completed_entries: dict[str, dict[str, Any]], run_dir: Path, jobs: int,
                          timeout_override: int | None) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    all_warnings: list[dict[str, Any]] = []
    case_results: dict[str, tuple[dict[str, Any], list[dict[str, Any]]]] = {}
    clean_pending_case_dirs(run_dir, pending_cases)
    with concurrent.futures.ThreadPoolExecutor(max_workers=jobs) as executor:
        future_to_case = {executor.submit(run_case, suite_dir, suite, case, run_dir, timeout_override): case for case in sorted(pending_cases, key=lambda c: c["id"])}
        for future in concurrent.futures.as_completed(future_to_case):
            case = future_to_case[future]
            try:
                entry, warnings = future.result()
            except Exception as exc:
                try:
                    entry, warnings = synthesize_case_error(suite_dir, suite, case, run_dir, exc)
                except Exception as synth_exc:
                    if not (run_dir / "manifest.json").exists() and terminal_marker_count(run_dir) == 0:
                        shutil.rmtree(run_dir, ignore_errors=True)
                    raise EvalctlError("E_SCORER_FAILED", f"case {case['id']} failed before replayable artifacts could be written: {synth_exc}", "retry evalctl run with a fresh --run-id", 3)
            case_results[case["id"]] = (entry, warnings)
    case_entries = []
    for case in sorted(all_cases, key=lambda c: c["id"]):
        if case["id"] in completed_entries:
            entry = completed_entries[case["id"]]
            warnings: list[dict[str, Any]] = []
        else:
            entry, warnings = case_results[case["id"]]
            all_warnings.extend(warnings)
        case_entries.append(entry)
    return case_entries, all_warnings


def execute_cases(suite_dir: Path, suite: dict[str, Any], cases: list[dict[str, Any]], run_dir: Path, run_id: str,
                  jobs: int, timeout_override: int | None, replayed_from: str | None,
                  reservation_ttl: int = DEFAULT_RESERVATION_TTL_SECONDS,
                  queue_backend: str | None = None, slots: int | None = None) -> tuple[dict[str, Any], list[dict[str, Any]], bool]:
    run_dir.mkdir(parents=True)
    shutil.copytree(suite_dir, run_dir / "suite-snapshot")
    queue = {"backend": "spoolctl", "db": ".spoolctl.db", "jobs": {}} if queue_backend == "spoolctl" else None
    metadata = build_run_metadata(suite, suite_dir, cases, run_id, slots or jobs, timeout_override, replayed_from, queue=queue, mode="queued" if queue_backend == "spoolctl" else "synchronous")
    write_run_metadata_once(run_dir, metadata)
    all_warnings = [{"code": "W_UNSANDBOXED_RUNNER", "message": "runner commands execute arbitrary local code; evalctl is not a sandbox"}]
    with ReservationHeartbeat(run_dir, run_id, reservation_ttl):
        if queue_backend == "spoolctl":
            case_entries, run_warnings = execute_spoolctl_pending_cases(suite_dir, suite, cases, cases, {}, run_dir, run_id, jobs, timeout_override, slots or jobs)
        else:
            case_entries, run_warnings = execute_pending_cases(suite_dir, suite, cases, cases, {}, run_dir, jobs, timeout_override)
        all_warnings.extend(run_warnings)
        if any(c["status"] == "error" for c in case_entries):
            all_warnings.append({"code": "W_PARTIAL_RUN", "message": "some cases errored; report remains generable"})
        data, run_ok = finalize_run(run_dir, metadata, case_entries)
        clear_reservation(run_dir)
    return data, all_warnings, run_ok


def command_run(argv: list[str], json_mode: bool, started: float) -> int:
    resume_id = value_after(argv, "--resume")
    if resume_id is not None:
        return command_run_resume(argv, resume_id, json_mode, started)
    args = strip_flags(argv, {"--jobs", "--timeout", "--run-id", "--reservation-ttl", "--queue", "--slots"}, {"--json", "--no-color", "--fail-on-fail"})
    if len(args) < 2:
        raise EvalctlError("E_SUITE_NOT_FOUND", "run requires a suite name", "try: evalctl run code-review --json", 1)
    queue_backend = value_after(argv, "--queue")
    slots_raw = value_after(argv, "--slots")
    if slots_raw is not None and queue_backend is None:
        raise EvalctlError("E_CASE_INVALID", "--slots requires --queue spoolctl", "try: evalctl run code-review --queue spoolctl --slots 4 --json", 1)
    if queue_backend is not None and queue_backend != "spoolctl":
        raise EvalctlError("E_CASE_INVALID", f"unsupported queue backend: {queue_backend}", "supported value: --queue spoolctl", 1)
    if queue_backend == "spoolctl":
        probe_spoolctl()
    suite_dir = resolve_suite(args[1])
    validate_suite(suite_dir)
    suite = read_json(suite_dir / "suite.json")
    cases = load_cases(suite_dir / suite.get("cases", "cases.jsonl"))
    jobs = parse_jobs(argv)
    slots = parse_positive_int_flag(argv, "--slots", jobs) if queue_backend == "spoolctl" else None
    reservation_ttl = parse_positive_int_flag(argv, "--reservation-ttl", DEFAULT_RESERVATION_TTL_SECONDS)
    unsandboxed_warning = {"code": "W_UNSANDBOXED_RUNNER", "message": "runner commands execute arbitrary local code; evalctl is not a sandbox"}
    if not suite.get("acknowledged_unsandboxed_runner") and sys.stderr.isatty():
        print(unsandboxed_warning["message"], file=sys.stderr)
    run_id = value_after(argv, "--run-id") or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H%M%SZ-") + suite.get("name", suite_dir.name)
    run_dir = Path("evals") / "runs" / run_id
    if run_dir.exists():
        manifest_path = run_dir / "manifest.json"
        if manifest_path.exists():
            manifest_doc = read_json(manifest_path)
            if run_identity(suite, suite_dir, cases) != manifest_identity(manifest_doc):
                existing_count = manifest_doc["suite"]["case_count"]
                existing_suite = manifest_doc["suite"]["name"]
                raise EvalctlError("E_RUN_CONFLICT", f"run-id `{run_id}` already completed for suite `{existing_suite}`/{existing_count} cases; refusing to reuse for a different suite/case set", "use a fresh --run-id", 5)
            data = report_data(run_dir)
            run_summary = {"ok": data["run"]["ok"], "case_count": data["run"]["case_count"], "status_counts": data["run"]["status_counts"]}
            existing = {"run_id": run_id, "run_dir": str(run_dir), "existing": True, "run": run_summary, "report_hash": data["report_hash"]}
            return print_envelope(existing, json_mode=json_mode, warnings=[unsandboxed_warning], started=started)
        reservation = read_reservation(run_dir)
        if reservation and reservation_is_live(reservation):
            raise EvalctlError("E_RUN_BUSY", f"run reservation is live for {run_id}", "wait and retry evalctl run with a new --run-id", 4)
        raise EvalctlError("E_RUN_BUSY", f"run {run_id} is incomplete and may be resumable", f"retry with: evalctl run --resume {run_id} --json", 4)
    timeout_override = int(value_after(argv, "--timeout")) if value_after(argv, "--timeout") else None
    data, all_warnings, run_ok = execute_cases(suite_dir, suite, cases, run_dir, run_id, jobs, timeout_override, None, reservation_ttl, queue_backend, slots)
    commands = [{"command": f"evalctl report {run_id} --format json", "rationale": "regenerate deterministic JSON report"}]
    print_envelope(data, json_mode=json_mode, human=f"Run {run_id}: {'pass' if run_ok else 'fail'}", warnings=all_warnings, commands=commands, started=started)
    return 6 if has_flag(argv, "--fail-on-fail") and not run_ok else 0


def command_run_resume(argv: list[str], run_id: str, json_mode: bool, started: float) -> int:
    if not is_safe_id(run_id) or "/" in run_id or "\\" in run_id:
        raise EvalctlError("E_CASE_INVALID", f"invalid run id: {run_id}", "use a simple run id with letters, numbers, dot, underscore, or dash", 1)
    run_dir = Path("evals") / "runs" / run_id
    if not run_dir.exists():
        raise EvalctlError("E_RUN_NOT_FOUND", f"run not found: {run_id}", "resume an existing incomplete run id", 1)
    nothing_pending_warning = {"code": "W_RESUME_NOTHING_PENDING", "message": "run has no pending cases to resume"}
    if (run_dir / "manifest.json").exists():
        data = report_data(run_dir)
        run_summary = {"ok": data["run"]["ok"], "case_count": data["run"]["case_count"], "status_counts": data["run"]["status_counts"]}
        existing = {"run_id": run_id, "run_dir": str(run_dir), "existing": True, "run": run_summary, "report_hash": data["report_hash"]}
        return print_envelope(existing, json_mode=json_mode, warnings=[nothing_pending_warning], started=started)
    suite_dir = run_dir / "suite-snapshot"
    if not suite_dir.exists():
        raise EvalctlError("E_RUN_NOT_FOUND", f"suite snapshot not found for {run_id}", "resume requires the original suite-snapshot directory", 1)
    metadata = read_run_metadata(run_dir)
    suite = read_json(suite_dir / "suite.json")
    cases = load_cases(suite_dir / suite.get("cases", "cases.jsonl"))
    if stable_json(metadata["suite_identity"]) != stable_json(run_identity(suite, suite_dir, cases)):
        raise EvalctlError("E_RUN_CONFLICT", f"run metadata does not match the suite snapshot for {run_id}", "inspect run.json and suite-snapshot before retrying", 5)
    reservation = read_reservation(run_dir)
    warnings: list[dict[str, Any]] = []
    if reservation and reservation_is_live(reservation):
        raise EvalctlError("E_RUN_BUSY", f"run reservation is live for {run_id}", "wait for the run to finish or retry after its reservation TTL", 4)
    if reservation is not None:
        warnings.append({"code": "W_RESERVATION_RECLAIMED", "message": "reclaimed stale run reservation"})
    completed_entries, pending_cases = split_completed_and_pending(run_dir, cases)
    reservation_ttl = parse_positive_int_flag(argv, "--reservation-ttl", DEFAULT_RESERVATION_TTL_SECONDS)
    jobs = int(metadata["execution"]["jobs"])
    timeout_override = int(metadata["execution"]["timeout_seconds"])
    queued = metadata["execution"].get("mode") == "queued" and metadata.get("queue", {}).get("backend") == "spoolctl"
    if queued:
        probe_spoolctl()
    with ReservationHeartbeat(run_dir, run_id, reservation_ttl):
        if pending_cases:
            if queued:
                case_entries, run_warnings = execute_spoolctl_pending_cases(suite_dir, suite, cases, pending_cases, completed_entries, run_dir, run_id, jobs, timeout_override, jobs)
            else:
                case_entries, run_warnings = execute_pending_cases(suite_dir, suite, cases, pending_cases, completed_entries, run_dir, jobs, timeout_override)
            warnings.extend(run_warnings)
        else:
            case_entries = [completed_entries[case["id"]] for case in sorted(cases, key=lambda c: c["id"])]
            warnings.append(nothing_pending_warning)
        if any(c["status"] == "error" for c in case_entries):
            warnings.append({"code": "W_PARTIAL_RUN", "message": "some cases errored; report remains generable"})
        data, run_ok = finalize_run(run_dir, metadata, case_entries)
        clear_reservation(run_dir)
    commands = [{"command": f"evalctl report {run_id} --format json", "rationale": "regenerate deterministic JSON report"}]
    print_envelope(data, json_mode=json_mode, human=f"Resume {run_id}: {'pass' if run_ok else 'fail'}", warnings=warnings, commands=commands, started=started)
    return 6 if has_flag(argv, "--fail-on-fail") and not run_ok else 0


def runs_root() -> Path:
    return Path("evals") / "runs"


def stored_queue_jobs(run_dir: Path) -> list[dict[str, Any]]:
    jobs: list[dict[str, Any]] = []
    for path in sorted((run_dir / "cases").glob("*/job.json")):
        try:
            data = read_json(path)
        except Exception:
            continue
        case_id = path.parent.name
        job_id = data.get("job_id") if isinstance(data, dict) else None
        if job_id is not None:
            jobs.append({"case_id": case_id, "job_id": job_id, "state": data.get("state"), "spoolctl_available": shutil.which("spoolctl") is not None})
    return jobs


def run_case_counts(run_dir: Path) -> dict[str, int]:
    case_count = 0
    suite_dir = run_dir / "suite-snapshot"
    if suite_dir.exists():
        try:
            suite = read_json(suite_dir / "suite.json")
            case_count = len(load_cases(suite_dir / suite.get("cases", "cases.jsonl")))
        except Exception:
            case_count = 0
    marker_count = terminal_marker_count(run_dir)
    if case_count == 0:
        case_count = len(list((run_dir / "cases").glob("*")))
    return {"case_count": case_count, "terminal": marker_count, "pending": max(case_count - marker_count, 0)}


def classify_run_dir(run_dir: Path) -> dict[str, Any]:
    manifest_path = run_dir / "manifest.json"
    reservation = read_reservation(run_dir)
    reservation_live = bool(reservation and reservation_is_live(reservation))
    if manifest_path.exists():
        state = "completed"
    elif reservation_live:
        state = "running"
    elif reservation is not None:
        state = "stale"
    else:
        state = "orphaned"
    data: dict[str, Any] = {
        "run_id": run_dir.name,
        "run_dir": str(run_dir),
        "state": state,
        "reservation": {"present": reservation is not None, "live": reservation_live},
        "cases": run_case_counts(run_dir),
        "queue_jobs": stored_queue_jobs(run_dir),
    }
    if manifest_path.exists():
        try:
            report = report_data(run_dir)
            data["run"] = report["run"]
            data["report_hash"] = report["report_hash"]
        except Exception:
            pass
    return data


def command_jobs(argv: list[str], json_mode: bool, started: float) -> int:
    args = strip_flags(argv, set(), {"--json", "--no-color", "--yes", "--force"})
    if len(args) < 2:
        raise EvalctlError("E_CASE_INVALID", "jobs requires list, get, or prune", "try: evalctl jobs list --json", 1)
    subcommand = args[1]
    root = runs_root()
    run_dirs = sorted([p for p in root.iterdir() if p.is_dir()], key=lambda p: p.name) if root.exists() else []
    if subcommand == "list":
        runs = [classify_run_dir(path) for path in run_dirs]
        return print_envelope({"runs": runs, "count": len(runs)}, json_mode=json_mode, human="\n".join(f"{r['run_id']}\t{r['state']}" for r in runs), started=started)
    if subcommand == "get":
        if len(args) != 3:
            raise EvalctlError("E_CASE_INVALID", "jobs get requires a run id", "try: evalctl jobs get <run-id> --json", 1)
        run_dir = root / args[2]
        if not run_dir.exists():
            raise EvalctlError("E_RUN_NOT_FOUND", f"run not found: {args[2]}", "try: evalctl jobs list --json", 1)
        data = classify_run_dir(run_dir)
        return print_envelope(data, json_mode=json_mode, human=f"{data['run_id']}: {data['state']}", started=started)
    if subcommand == "prune":
        confirmed = has_flag(argv, "--yes") or has_flag(argv, "--force")
        stale = [classify_run_dir(path) for path in run_dirs if classify_run_dir(path)["state"] == "stale"]
        orphaned = [classify_run_dir(path) for path in run_dirs if classify_run_dir(path)["state"] == "orphaned"]
        refused = [classify_run_dir(path) for path in run_dirs if classify_run_dir(path)["state"] in {"completed", "running"}]
        removed_reservations: list[str] = []
        removed_runs: list[str] = []
        if confirmed:
            for item in stale:
                clear_reservation(Path(item["run_dir"]))
                removed_reservations.append(item["run_id"])
            for item in orphaned:
                shutil.rmtree(item["run_dir"], ignore_errors=True)
                removed_runs.append(item["run_id"])
        data = {
            "confirmed": confirmed,
            "candidates": {"stale_reservations": [item["run_id"] for item in stale], "orphaned_runs": [item["run_id"] for item in orphaned]},
            "removed": {"reservations": removed_reservations, "runs": removed_runs},
            "refused": [{"run_id": item["run_id"], "state": item["state"]} for item in refused],
        }
        return print_envelope(data, json_mode=json_mode, human=json.dumps(data, indent=2, sort_keys=True), started=started)
    raise EvalctlError("E_CASE_INVALID", f"unknown jobs subcommand '{subcommand}'", "try: evalctl jobs list --json", 1)


def parse_replay_source(argv: list[str]) -> Path:
    if not has_flag(argv, "--failed"):
        raise EvalctlError("E_CASE_INVALID", "replay requires --failed in v0.2", "try: evalctl replay --failed <run-id> --json", 1)
    run_dir = value_after(argv, "--run-dir")
    args = strip_flags(argv, {"--run-dir", "--run-id", "--suite", "--jobs", "--timeout"}, {"--json", "--no-color", "--failed", "--force", "--fail-on-fail"})
    positional = args[1:] if args and args[0] == "replay" else args
    if run_dir and positional:
        raise EvalctlError("E_CASE_INVALID", "replay source must be either a run id or --run-dir, not both", "try: evalctl replay --failed <run-id> --json", 1)
    if not run_dir and len(positional) != 1:
        raise EvalctlError("E_CASE_INVALID", "replay requires a source run id or --run-dir", "try: evalctl replay --failed <run-id> --json", 1)
    path = Path(run_dir) if run_dir else Path("evals") / "runs" / positional[0]
    if not (path / "manifest.json").exists():
        raise EvalctlError("E_RUN_NOT_FOUND", f"run not found: {path}", "try: evalctl run code-review --json", 1)
    return path


def command_replay(argv: list[str], json_mode: bool, started: float) -> int:
    source_run = parse_replay_source(argv)
    source_manifest = read_json(source_run / "manifest.json")
    source_report = report_data(source_run)
    failed_ids = [case["id"] for case in source_report["cases"] if case["status"] != "pass"]
    if not failed_ids:
        warnings = [{"code": "W_NOTHING_TO_REPLAY", "message": "source run has no failed or errored cases"}]
        data = {"replayed_from": source_manifest["run_id"], "cases_replayed": 0}
        return print_envelope(data, json_mode=json_mode, human=f"{source_manifest['run_id']}: nothing to replay", warnings=warnings, started=started)

    suite_arg = value_after(argv, "--suite") or source_manifest["suite"]["name"]
    try:
        suite_dir = resolve_suite(suite_arg)
    except EvalctlError as exc:
        if exc.error["code"] == "E_SUITE_NOT_FOUND":
            raise EvalctlError("E_SUITE_NOT_FOUND", exc.error["message"], "pass the current suite with --suite <suite-or-path>", 1)
        raise
    validate_suite(suite_dir)
    suite = read_json(suite_dir / "suite.json")
    current_cases = load_cases(suite_dir / suite.get("cases", "cases.jsonl"))
    current_by_id = {case["id"]: case for case in current_cases}
    warnings: list[dict[str, Any]] = []
    target_cases = []
    for case_id in failed_ids:
        case = current_by_id.get(case_id)
        if case is None:
            warnings.append({"code": "W_REPLAY_CASE_ABSENT", "message": f"case {case_id} is absent from the current suite"})
        else:
            target_cases.append(case)
    if not target_cases:
        warnings.append({"code": "W_NOTHING_TO_REPLAY", "message": "no selected failed cases exist in the current suite"})
        data = {"replayed_from": source_manifest["run_id"], "cases_replayed": 0}
        return print_envelope(data, json_mode=json_mode, human=f"{source_manifest['run_id']}: nothing to replay", warnings=warnings, started=started)

    jobs = parse_jobs(argv)
    timeout_override = int(value_after(argv, "--timeout")) if value_after(argv, "--timeout") else None
    run_id = value_after(argv, "--run-id") or f"{source_manifest['run_id']}-replay-{datetime.now(timezone.utc).strftime('%Y-%m-%dT%H%M%SZ')}"
    run_dir = Path("evals") / "runs" / run_id
    if run_dir.resolve() == source_run.resolve():
        raise EvalctlError("E_RUN_CONFLICT", "replay destination must not be the source run", "use a fresh --run-id", 5)
    if run_dir.exists():
        if (run_dir / "manifest.json").exists():
            if not has_flag(argv, "--force"):
                raise EvalctlError("E_RUN_CONFLICT", f"run-id `{run_id}` already completed; refusing to overwrite without --force", "use a fresh --run-id or pass --force", 5)
            shutil.rmtree(run_dir)
        else:
            raise EvalctlError("E_RUN_BUSY", f"run reservation exists for {run_id}", "wait and retry evalctl replay with a new --run-id", 4)
    if not suite.get("acknowledged_unsandboxed_runner") and sys.stderr.isatty():
        print("runner commands execute arbitrary local code; evalctl is not a sandbox", file=sys.stderr)
    data, run_warnings, run_ok = execute_cases(suite_dir, suite, target_cases, run_dir, run_id, jobs, timeout_override, source_manifest["run_id"])
    all_warnings = run_warnings + warnings
    commands = [{"command": f"evalctl report {run_id} --format json", "rationale": "inspect replay report"}]
    print_envelope(data, json_mode=json_mode, human=f"Replay {run_id}: {'pass' if run_ok else 'fail'}", warnings=all_warnings, commands=commands, started=started)
    return 6 if has_flag(argv, "--fail-on-fail") and not run_ok else 0


def status_counts(cases: list[dict[str, Any]]) -> dict[str, int]:
    counts = {"pass": 0, "fail": 0, "error": 0}
    for case in cases:
        counts[case["status"]] = counts.get(case["status"], 0) + 1
    return counts


def run_identity(suite: dict[str, Any], suite_dir: Path, cases: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "suite_name": suite.get("name", suite_dir.name),
        "suite_hash": sha256_text(stable_json(suite)),
        "cases": sorted((case["id"], sha256_text(stable_json(case))) for case in cases),
    }


def manifest_identity(manifest_doc: dict[str, Any]) -> dict[str, Any]:
    return {
        "suite_name": manifest_doc["suite"]["name"],
        "suite_hash": manifest_doc["suite"]["hash"],
        "cases": sorted((case["id"], case["input_hash"]) for case in manifest_doc["cases"]),
    }


def resolve_run(argv: list[str]) -> Path:
    run_dir = value_after(argv, "--run-dir")
    if run_dir:
        path = Path(run_dir)
    else:
        args = strip_flags(argv, {"--run-dir", "--format"}, {"--json", "--no-color"})
        if len(args) < 2:
            raise EvalctlError("E_RUN_NOT_FOUND", "run id or --run-dir is required", "try: evalctl status <run-id> --json", 1)
        path = Path("evals") / "runs" / args[1]
    if not (path / "manifest.json").exists():
        raise EvalctlError("E_RUN_NOT_FOUND", f"run not found: {path}", "try: evalctl run code-review --json", 1)
    return path


def recompute_case_score(run_dir: Path, case_entry: dict[str, Any], suite: dict[str, Any]) -> dict[str, Any]:
    case_dir = run_dir / "cases" / case_entry["id"]
    case = read_json(case_dir / "input.json")
    output = (case_dir / "output.txt").read_text(errors="replace")
    runner_json = read_json(case_dir / "runner.json")
    before = read_json(case_dir / "workspace-before.json")
    after = read_json(case_dir / "workspace-after.json")
    diff = diff_manifests(before, after)
    scores = score_case(case, output, runner_json, after, diff, suite.get("scorers", []), case_dir=case_dir, execute=False, suite=suite)
    status = "error" if runner_json.get("timed_out") or runner_json.get("spawn_failed") or any(s.get("error") for s in scores) else "pass"
    if status != "error" and not all(s["ok"] for s in scores if s.get("required", True)):
        status = "fail"
    return {"case_id": case["id"], "status": status, "ok": status == "pass", "scores": scores}


def report_data(run_dir: Path) -> dict[str, Any]:
    manifest_doc = read_json(run_dir / "manifest.json")
    suite = read_json(run_dir / "suite-snapshot" / "suite.json")
    cases = []
    for entry in sorted(manifest_doc["cases"], key=lambda c: c["id"]):
        score = recompute_case_score(run_dir, entry, suite)
        aggregate = sum(s["score"] for s in score["scores"]) / max(len(score["scores"]), 1)
        cases.append({"id": score["case_id"], "status": score["status"], "ok": score["ok"], "aggregate_score": aggregate, "scores": score["scores"], "artifacts": entry["artifacts"]})
    failures = [c for c in cases if c["status"] != "pass"]
    failures.sort(key=lambda c: (0 if c["status"] == "error" else 1, c["aggregate_score"], c["id"]))
    def report_score(score: dict[str, Any]) -> dict[str, Any]:
        out = {"scorer": score["scorer"], "ok": score["ok"], "label": score["label"], "findings": score["findings"]}
        if "id" in score:
            out["id"] = score["id"]
        return out

    normalized = {"run": {"ok": not failures, "suite": manifest_doc["suite"]["name"], "case_count": len(cases), "status_counts": status_counts(cases)}, "failures": [{"id": f["id"], "status": f["status"], "scores": [report_score(s) for s in f["scores"]]} for f in failures], "cases": [{"id": c["id"], "status": c["status"], "ok": c["ok"]} for c in cases]}
    return {**normalized, "run_id": manifest_doc["run_id"], "report_hash": sha256_text(stable_json(normalized))}


def markdown_report(data: dict[str, Any]) -> str:
    lines = [f"# evalctl report: {data['run_id']}", "", f"Status: {'pass' if data['run']['ok'] else 'fail'}", f"Report hash: `{data['report_hash']}`", "", "## Failures"]
    if not data["failures"]:
        lines.append("None.")
    for failure in data["failures"]:
        lines.append(f"- `{failure['id']}` {failure['status']}")
        for score in failure["scores"]:
            if not score["ok"]:
                lines.append(f"  - {score['scorer']}: {score['label']} {score['findings']}")
    return "\n".join(lines) + "\n"


def command_status(argv: list[str], json_mode: bool, started: float) -> int:
    run_dir = resolve_run(argv)
    manifest_doc = read_json(run_dir / "manifest.json")
    report = report_data(run_dir)
    data = {"run_id": manifest_doc["run_id"], "run_dir": str(run_dir), "run": report["run"], "cases": report["cases"], "recommended_action": {"command": f"evalctl report --run-dir {run_dir} --format json", "rationale": "inspect deterministic report and ranked failures", "alternatives": []}}
    return print_envelope(data, json_mode=json_mode, human=f"{manifest_doc['run_id']}: {'pass' if report['run']['ok'] else 'fail'}", started=started)


def command_report(argv: list[str], json_mode: bool, started: float) -> int:
    run_dir = resolve_run(argv)
    data = report_data(run_dir)
    fmt = value_after(argv, "--format", "json" if json_mode else "markdown")
    if fmt == "markdown" and not has_flag(argv, "--json"):
        print(markdown_report(data), end="")
        return 0
    if fmt not in {"json", "markdown"}:
        raise EvalctlError("E_CASE_INVALID", f"--format must be markdown or json (got {fmt})", "try: evalctl report <run-id> --format json", 1)
    commands = [{"command": f"evalctl report --run-dir {run_dir} --format json", "rationale": "regenerate this report"}]
    return print_envelope(data, json_mode=True, commands=commands, started=started)


def main(argv: list[str] | None = None) -> int:
    started = time.time()
    argv = list(sys.argv[1:] if argv is None else argv)
    json_mode = wants_json(argv)
    try:
        if not argv or argv[0] in {"--help", "-h"}:
            print(help_text())
            return 0
        if argv[0] == "--version":
            print(__version__)
            return 0
        cmd = argv[0]
        if cmd == "capabilities":
            return print_envelope(capabilities_data(), json_mode=True, started=started)
        if cmd == "schema":
            args = strip_flags(argv, set(), {"--json", "--no-color"})
            return print_envelope(schema_data(args[1] if len(args) > 1 else None), json_mode=True, started=started)
        if cmd == "robot-docs" and len(argv) > 1 and argv[1] == "guide":
            print(robot_docs(), end="")
            return 0
        if cmd == "init":
            return command_init(argv, json_mode, started)
        if cmd == "validate":
            return command_validate(argv, json_mode, started)
        if cmd == "suite" and len(argv) > 1 and argv[1] == "add":
            return command_suite_add(argv, json_mode, started)
        if cmd == "case" and len(argv) > 1 and argv[1] == "add":
            return command_case_add(argv, json_mode, started)
        if cmd == "scorer" and len(argv) > 1 and argv[1] == "add":
            return command_scorer_add(argv, json_mode, started)
        if cmd == "run":
            return command_run(argv, json_mode, started)
        if cmd == "jobs":
            return command_jobs(argv, json_mode, started)
        if cmd == "replay":
            return command_replay(argv, json_mode, started)
        if cmd == "status":
            return command_status(argv, json_mode, started)
        if cmd == "report":
            return command_report(argv, json_mode, started)
        raise EvalctlError("E_CASE_INVALID", f"unknown command '{cmd}'", "try: evalctl capabilities --json", 1)
    except EvalctlError as exc:
        return print_error(exc, json_mode=json_mode, started=started)
    except KeyboardInterrupt:
        return print_error(EvalctlError("E_RUNNER_FAILED", "interrupted", "retry the command", 3), json_mode=json_mode, started=started)
    except Exception as exc:
        return print_error(EvalctlError("E_RUNNER_FAILED", f"internal error: {exc}", "run with --json and inspect errors[0]", 3), json_mode=json_mode, started=started)


if __name__ == "__main__":
    raise SystemExit(main())
