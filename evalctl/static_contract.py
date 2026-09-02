from __future__ import annotations

import hashlib
import json
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Literal, Mapping

from . import __version__
from .integration_contracts import MINIMUM_SPOOLCTL_CONTRACT, MINIMUM_SPOOLCTL_VERSION

# Dotted MAJOR.MINOR string, not an integer: the reference compatibility idiom
# splits this field on "." and an integer has no minor channel. MAJOR bumps on a
# breaking change; MINOR bumps on a purely additive one, so an agent can gate on
# `contract_version.split(".")` to tell additive contracts apart. See
# docs/agent-guide.md for the compatibility rule.
CONTRACT_VERSION = "1.0"
TOOL = "evalctl"
DEFAULT_COMMAND_SCORER_TIMEOUT_SECONDS = 30
DEFAULT_RESERVATION_TTL_SECONDS = 3600
DEFAULT_JOBS_LIST_LIMIT = 50
MAX_JOBS_LIST_LIMIT = 1000
DEFAULT_CASE_PAGE_LIMIT = 50
MAX_CASE_PAGE_LIMIT = 1000
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
    "E_CASE_INVALID": {"class": "user-input", "exit": 1, "where": ["validate", "run", "case"], "retryable": False, "surface": "envelope"},
    "E_UNKNOWN_COMMAND": {"class": "user-input", "exit": 1, "where": ["dispatch", "schema"], "retryable": False, "surface": "envelope"},
    "E_UNKNOWN_SUBCOMMAND": {"class": "user-input", "exit": 1, "where": ["dispatch"], "retryable": False, "surface": "envelope"},
    "E_UNKNOWN_FLAG": {"class": "user-input", "exit": 1, "where": ["run", "replay", "jobs", "plan", "doctor"], "retryable": False, "surface": "envelope"},
    "E_UNKNOWN_COMPONENT": {"class": "user-input", "exit": 1, "where": ["doctor"], "retryable": False, "surface": "envelope"},
    "E_SCHEMA_VIOLATION": {"class": "user-input", "exit": 1, "where": ["validate", "run"], "retryable": False, "surface": "envelope"},
    "E_SUITE_NOT_FOUND": {"class": "user-input", "exit": 1, "where": ["run", "report", "validate"], "retryable": False, "surface": "envelope"},
    "E_RUN_NOT_FOUND": {"class": "user-input", "exit": 1, "where": ["status", "report", "resume"], "retryable": False, "surface": "envelope"},
    "E_RUN_CORRUPT": {"class": "user-input", "exit": 1, "where": ["resume"], "retryable": False, "surface": "envelope"},
    "E_RUN_IN_FLIGHT": {"class": "transient", "exit": 4, "where": ["report"], "retryable": True, "surface": "envelope"},
    "E_INIT_UNWRITABLE": {"class": "tool-env", "exit": 3, "where": ["init"], "retryable": None, "surface": "envelope"},
    "E_RUNNER_FAILED": {"class": "tool-env", "exit": 3, "where": ["run"], "retryable": None, "surface": "runner_json"},
    "E_RUNNER_TIMEOUT": {"class": "tool-env", "exit": 3, "where": ["run"], "retryable": None, "surface": "runner_json"},
    "E_SPOOLCTL_UNAVAILABLE": {"class": "tool-env", "exit": 3, "where": ["run", "resume"], "retryable": False, "surface": "envelope"},
    "E_SPOOLCTL_INCOMPATIBLE": {"class": "tool-env", "exit": 3, "where": ["run", "resume"], "retryable": False, "surface": "envelope"},
    "E_INFERCTL_UNAVAILABLE": {"class": "tool-env", "exit": 3, "where": ["doctor"], "retryable": False, "surface": "envelope"},
    "E_INFERCTL_INCOMPATIBLE": {"class": "tool-env", "exit": 3, "where": ["doctor"], "retryable": False, "surface": "envelope"},
    "E_JOB_TRANSIENT": {"class": "transient", "exit": 4, "where": ["run", "resume"], "retryable": True, "surface": "envelope"},
    "E_SCORER_FAILED": {"class": "tool-env", "exit": 3, "where": ["run", "report"], "retryable": None, "surface": "envelope"},
    "E_SCORER_CASE_FAILED": {"class": "tool-env", "where": ["run", "replay"], "surface": "score_json"},
    "E_RUN_BUSY": {"class": "transient", "exit": 4, "where": ["run", "resume"], "retryable": True, "surface": "envelope"},
    "E_RUN_CONFLICT": {"class": "conflict", "exit": 5, "where": ["run", "init", "replay", "suite", "case", "scorer"], "retryable": False, "surface": "envelope"},
    "E_UNSANDBOXED_RUNNER_UNACK": {"class": "safety", "exit": 2, "where": ["run", "replay"], "retryable": False, "surface": "envelope"},
    "W_UNSANDBOXED_RUNNER": {"class": "warning", "where": ["run", "replay"], "surface": "envelope"},
    "W_RUNNER_UNRESOLVED": {"class": "warning", "where": ["validate"], "surface": "envelope"},
    "W_REPLAY_CASE_ABSENT": {"class": "warning", "where": ["replay"], "surface": "envelope"},
    "W_NOTHING_TO_REPLAY": {"class": "warning", "where": ["replay"], "surface": "envelope"},
    "W_TEXT_DIFF_APPROXIMATED": {"class": "warning", "where": ["run"], "surface": "envelope"},
    "W_OUTPUT_TRUNCATED": {"class": "warning", "where": ["run"], "surface": "envelope"},
    "W_PATH_UNREADABLE": {"class": "warning", "where": ["run"], "surface": "envelope"},
    "W_PARTIAL_RUN": {"class": "warning", "where": ["run", "report"], "surface": "envelope"},
    "W_RESERVATION_RECLAIMED": {"class": "warning", "where": ["run", "resume"], "surface": "envelope"},
    "W_RESUME_NOTHING_PENDING": {"class": "warning", "where": ["resume"], "surface": "envelope"},
    "W_INFERCTL_ABSENT": {"class": "warning", "where": ["run", "resume"], "surface": "envelope"},
    "W_INFERCTL_INCOMPATIBLE": {"class": "warning", "where": ["run", "resume"], "surface": "envelope"},
    "W_INFERCTL_CAPTURE_FAILED": {"class": "warning", "where": ["run", "resume"], "surface": "envelope"},
    "W_INFERCTL_PREFLIGHT_BLOCKED": {"class": "warning", "where": ["run", "resume"], "surface": "envelope"},
    "W_CASE_ADD_REJECTED": {"class": "warning", "where": ["case"], "surface": "envelope"},
}

ACK_UNSANDBOXED_ENV = "EVALCTL_ACKNOWLEDGE_UNSANDBOXED_RUNNER"
ACK_UNSANDBOXED_FLAG = "--acknowledge-unsandboxed-runner"
_ACK_TRUTHY = frozenset({"1", "true", "yes", "on"})


def env_ack_unsandboxed() -> bool:
    return os.environ.get(ACK_UNSANDBOXED_ENV, "").strip().lower() in _ACK_TRUTHY


DOCTOR_COMPONENTS = frozenset({"runtime", "suite_root", "runs_root", "reservations", "spoolctl", "inferctl", "runner_safety"})
OPTIONAL_COMPONENT_STATES = {"not_configured", "unknown"}


FlagKind = Literal["bool", "positive_int", "enum", "safe_id", "suite_path", "run_path", "json_text", "text"]


@dataclass(frozen=True)
class FlagSpec:
    kind: FlagKind
    choices: frozenset[str] = frozenset()
    default: object | None = None
    required: bool = False
    allow_dash_value: bool = False
    allow_empty: bool = False
    repeatable: bool = False
    minimum: int | None = None
    maximum: int | None = None


@dataclass(frozen=True)
class CommandSpec:
    name: str
    description: str
    flags: Mapping[str, FlagSpec] = field(default_factory=dict)
    args: tuple[str, ...] = ()
    subcommands: frozenset[str] = frozenset()
    mutates: bool = False
    json: bool = True
    exit_codes: tuple[int, ...] = (0, 1)
    mega_command: str | None = None
    handler: Callable[[list[str], bool, float], int] | None = None


@dataclass(frozen=True)
class ParsedArgs:
    positionals: tuple[str, ...]
    values: Mapping[str, Any]
    bools: frozenset[str]


BOOL = FlagSpec("bool")
GLOBAL_FLAG_SPECS = {
    "--json": BOOL,
    "--no-color": BOOL,
    "--help": BOOL,
    "--version": BOOL,
}
HELP_FLAGS = frozenset({"--help", "-h"})


def global_flag_specs_for(spec: CommandSpec) -> dict[str, FlagSpec]:
    """Global flags this verb actually accepts.

    Derived from CommandSpec.json, the same field that feeds the capabilities
    entry, so the accepted set and the advertised set cannot drift.
    """
    flags = dict(GLOBAL_FLAG_SPECS)
    if not spec.json:
        del flags["--json"]
    return flags


def positive_int_spec(*, maximum: int | None = None) -> FlagSpec:
    return FlagSpec("positive_int", minimum=1, maximum=maximum)


COMMAND_SPECS: dict[str, CommandSpec] = {
    "capabilities": CommandSpec("capabilities", "Return the machine contract.", flags={"--json": BOOL}, exit_codes=(0,)),
    "schema": CommandSpec("schema", "Return output schemas.", args=("verb",), flags={"--json": BOOL}),
    "robot-docs": CommandSpec("robot-docs", "Return agent workflow guide.", args=("guide",), subcommands=frozenset({"guide"}), json=False),
    "init": CommandSpec("init", "Scaffold evals/ tree with sample code-review suite.", flags={"--json": BOOL, "--force": BOOL}, mutates=True, exit_codes=(0, 3, 5)),
    "validate": CommandSpec("validate", "Validate suite.json, cases.jsonl, fixtures, scorer refs, and runner config.", args=("suite",), flags={"--json": BOOL}),
    "doctor": CommandSpec("doctor", "Diagnose evalctl runtime, run state, and optional integrations.", flags={"--json": BOOL, "--component": FlagSpec("text"), "--fast": BOOL}, mega_command="DIAGNOSE"),
    "plan": CommandSpec(
        "plan",
        "Produce a side-effect-free execution plan.",
        args=("suite",),
        flags={
            "--json": BOOL,
            "--jobs": positive_int_spec(),
            "--timeout": positive_int_spec(),
            "--run-id": FlagSpec("safe_id"),
            "--resume": FlagSpec("safe_id"),
            "--queue": FlagSpec("enum", choices=frozenset({"spoolctl"})),
            "--slots": positive_int_spec(),
            "--inferctl-task": FlagSpec("safe_id"),
            "--limit": positive_int_spec(maximum=MAX_CASE_PAGE_LIMIT),
            "--cursor": FlagSpec("text"),
        },
        mega_command="PLAN",
    ),
    "run": CommandSpec(
        "run",
        "Run a suite and produce a portable, resumable run directory.",
        args=("suite",),
        flags={
            "--json": BOOL,
            "--jobs": positive_int_spec(),
            "--timeout": positive_int_spec(),
            "--run-id": FlagSpec("safe_id"),
            "--inferctl-task": FlagSpec("safe_id"),
            "--resume": FlagSpec("safe_id"),
            "--queue": FlagSpec("enum", choices=frozenset({"spoolctl"})),
            "--slots": positive_int_spec(),
            "--reservation-ttl": positive_int_spec(),
            "--fail-on-fail": BOOL,
            "--acknowledge-unsandboxed-runner": BOOL,
        },
        mutates=True,
        exit_codes=(0, 1, 2, 3, 4, 5, 6),
    ),
    "jobs": CommandSpec("jobs", "Inspect and prune local run/reservation/queue state.", args=("list", "get", "prune"), subcommands=frozenset({"list", "get", "prune"}), flags={"--json": BOOL, "--yes": BOOL, "--force": BOOL, "--limit": positive_int_spec(maximum=MAX_JOBS_LIST_LIMIT), "--cursor": FlagSpec("text")}, mutates=True),
    "replay": CommandSpec("replay", "Re-execute failed/errored cases from a source run into a linked partial run. Without --run-id the destination id is derived from the source run and the replayed case set, not the clock, so a retry is idempotent: it returns the existing run rather than spawning a second one or colliding.", args=("run-id",), flags={"--json": BOOL, "--failed": BOOL, "--run-dir": FlagSpec("run_path"), "--suite": FlagSpec("suite_path"), "--run-id": FlagSpec("safe_id"), "--force": BOOL, "--jobs": positive_int_spec(), "--timeout": positive_int_spec(), "--fail-on-fail": BOOL, "--acknowledge-unsandboxed-runner": BOOL}, mutates=True, exit_codes=(0, 1, 2, 3, 4, 5, 6)),
    "suite": CommandSpec("suite", "Author suites, including suite add.", args=("add", "name"), subcommands=frozenset({"add"}), flags={"--json": BOOL, "--runner-argv": FlagSpec("text"), "--runner-command": FlagSpec("text", allow_dash_value=True), "--shell": BOOL}, mutates=True, exit_codes=(0, 1, 5)),
    "case": CommandSpec("case", "Author cases, including case add. Pass --stdin to add many cases at once, one JSON object per line in cases.jsonl shape.", args=("add", "suite"), subcommands=frozenset({"add"}), flags={"--json": BOOL, "--stdin": BOOL, "--task": FlagSpec("text", allow_dash_value=True), "--workspace": FlagSpec("suite_path"), "--id": FlagSpec("safe_id"), "--diff": FlagSpec("suite_path"), "--expect-json": FlagSpec("json_text", allow_dash_value=True)}, mutates=True, exit_codes=(0, 1, 5)),
    "scorer": CommandSpec("scorer", "Author scorers, including built-in and command scorers.", args=("add", "suite"), subcommands=frozenset({"add"}), flags={"--json": BOOL, "--name": FlagSpec("enum", choices=frozenset(set(BUILTIN_SCORERS) | {"command"})), "--required": BOOL, "--advisory": BOOL, "--id": FlagSpec("safe_id"), "--argv": FlagSpec("text"), "--command": FlagSpec("text", allow_dash_value=True), "--shell": BOOL, "--timeout": positive_int_spec()}, mutates=True, exit_codes=(0, 1, 5)),
    "status": CommandSpec("status", "Diagnose run state.", args=("run-id",), flags={"--json": BOOL, "--run-dir": FlagSpec("run_path"), "--limit": positive_int_spec(maximum=MAX_CASE_PAGE_LIMIT), "--cursor": FlagSpec("text")}),
    "report": CommandSpec("report", "Generate markdown or JSON report from run artifacts.", args=("run-id",), flags={"--json": BOOL, "--format": FlagSpec("enum", choices=frozenset({"markdown", "json"}), default="markdown"), "--run-dir": FlagSpec("run_path"), "--limit": positive_int_spec(maximum=MAX_CASE_PAGE_LIMIT), "--cursor": FlagSpec("text")}, exit_codes=(0, 1, 3, 4)),
}

VERB_NAMES = frozenset(COMMAND_SPECS)
SUBCOMMANDS = {name: spec.subcommands for name, spec in COMMAND_SPECS.items() if spec.subcommands}
RUN_FLAGS_WITH_VALUES = {flag for flag, spec in COMMAND_SPECS["run"].flags.items() if spec.kind != "bool"}
RUN_BOOL_FLAGS = {flag for flag, spec in COMMAND_SPECS["run"].flags.items() if spec.kind == "bool"}
REPLAY_FLAGS_WITH_VALUES = {flag for flag, spec in COMMAND_SPECS["replay"].flags.items() if spec.kind != "bool"}
REPLAY_BOOL_FLAGS = {flag for flag, spec in COMMAND_SPECS["replay"].flags.items() if spec.kind == "bool"}
JOBS_FLAGS_WITH_VALUES = {flag for flag, spec in COMMAND_SPECS["jobs"].flags.items() if spec.kind != "bool"}
JOBS_BOOL_FLAGS = {flag for flag, spec in COMMAND_SPECS["jobs"].flags.items() if spec.kind == "bool"}
DOCTOR_FLAGS_WITH_VALUES = {flag for flag, spec in COMMAND_SPECS["doctor"].flags.items() if spec.kind != "bool"}
DOCTOR_BOOL_FLAGS = {flag for flag, spec in COMMAND_SPECS["doctor"].flags.items() if spec.kind == "bool"}
PLAN_FLAGS_WITH_VALUES = {flag for flag, spec in COMMAND_SPECS["plan"].flags.items() if spec.kind != "bool"}
PLAN_BOOL_FLAGS = {flag for flag, spec in COMMAND_SPECS["plan"].flags.items() if spec.kind == "bool"}


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
             started: float | None = None, meta_extra: dict[str, Any] | None = None) -> dict[str, Any]:
    started = started or time.time()
    payload = data if ok else None
    meta: dict[str, Any] = {
        "request_id": "req_" + uuid.uuid4().hex[:20],
        "ts_iso": now_iso(),
        "data_hash": sha256_text(stable_json(payload)) if ok else None,
        "contract_version": CONTRACT_VERSION,
        "elapsed_ms": int((time.time() - started) * 1000),
    }
    if meta_extra:
        meta.update(meta_extra)
    return {
        "ok": ok,
        "tool_version": __version__,
        "data": payload,
        "meta": meta,
        "warnings": warnings or [],
        "commands": commands or [],
        "errors": errors or [],
    }

def help_text() -> str:
    return f"""evalctl  Local-first evals for agents.

USAGE: evalctl <command> [flags]

COMMANDS:
  capabilities --json              Machine contract
  schema <verb> --json             Output schema for a verb
  robot-docs guide                 Agent workflow handbook
  init [--force]                   Scaffold evals/ with code-review suite
  validate [suite] [--json]        Validate suite files
  doctor [--component NAME] [--fast] [--json]
  plan <suite> [--jobs N] [--timeout S] [--run-id ID] [--resume ID] [--queue spoolctl] [--slots N] [--inferctl-task TASK] [--json]
  run <suite> [--jobs N] [--timeout S] [--run-id ID] [--inferctl-task TASK] [--resume ID] [--queue spoolctl] [--slots N] [--reservation-ttl S] [--fail-on-fail] [--acknowledge-unsandboxed-runner] [--json]
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
  --help, -h       Show help for evalctl or for any verb; never mutates

EXIT CODES: 0 ok; 1 input; 2 safety; 3 environment; 4 transient; 5 conflict; 6 eval failed.
{agent_automation_footer()}"""



def flag_usage(flag: str, spec: FlagSpec) -> str:
    if spec.kind == "bool":
        return flag
    if spec.kind == "enum":
        return f"{flag} {'|'.join(sorted(spec.choices))}"
    if spec.kind == "positive_int":
        if spec.maximum is not None:
            return f"{flag} N            (1-{spec.maximum})"
        return f"{flag} N"
    if spec.kind in {"suite_path", "run_path"}:
        return f"{flag} PATH"
    if spec.kind == "safe_id":
        return f"{flag} ID"
    if spec.kind == "json_text":
        return f"{flag} JSON"
    return f"{flag} TEXT"


def agent_automation_footer(verb: str | None = None) -> str:
    schema_line = f"evalctl schema {verb} --json" if verb in DATA_SCHEMAS else "evalctl schema <verb> --json"
    return ("AGENT/AUTOMATION:\n"
            "  Machine contract: evalctl capabilities --json\n"
            "  Workflow guide:   evalctl robot-docs guide\n"
            f"  Schemas:          {schema_line}\n")


def verb_help_text(spec: CommandSpec) -> str:
    positionals = "".join(f" <{arg}>" for arg in spec.args)
    lines = [
        f"{TOOL} {spec.name}  {spec.description}",
        "",
        f"USAGE: {TOOL} {spec.name}{positionals} [flags]",
        "",
    ]
    if spec.subcommands:
        lines.append("SUBCOMMANDS:")
        lines.append(f"  {', '.join(sorted(spec.subcommands))}")
        lines.append("")
    flags = {**global_flag_specs_for(spec), **dict(spec.flags)}
    lines.append("FLAGS:")
    lines.extend(f"  {flag_usage(flag, flags[flag])}" for flag in sorted(flags))
    lines.append("")
    lines.append(f"MUTATES: {'yes' if spec.mutates else 'no'}")
    lines.append(f"JSON ENVELOPE: {'yes' if spec.json else 'no'}")
    lines.append("EXIT CODES: " + "; ".join(f"{code} {EXIT_CODES[code]['meaning']}" for code in spec.exit_codes))
    if spec.mega_command is not None:
        lines.append(f"MEGA-COMMAND: {spec.mega_command}")
    return "\n".join(lines) + "\n" + agent_automation_footer(spec.name)


def capabilities_entry_from_spec(spec: CommandSpec) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "description": spec.description,
        "json": spec.json,
        "mutates": spec.mutates,
        "exit_codes": list(spec.exit_codes),
    }
    if spec.args:
        entry["args"] = list(spec.args)
    if spec.flags:
        entry["flags"] = list(spec.flags)
    if spec.mega_command is not None:
        entry["mega_command"] = spec.mega_command
    return entry


def capabilities_data(*, probe_spoolctl_func: Callable[..., dict[str, Any]] | None = None,
                      inferctl_capabilities_func: Callable[..., dict[str, Any]] | None = None,
                      inferctl_verb_names_func: Callable[[dict[str, Any]], set[str]] | None = None) -> dict[str, Any]:
    try:
        if probe_spoolctl_func is None:
            raise EvalctlError("E_SPOOLCTL_UNAVAILABLE", "spoolctl probe unavailable", "run from evalctl.cli for live integration status", 3)
        spool = probe_spoolctl_func()
        spoolctl_status = {"available": True, "planned": False, "minimum_version": MINIMUM_SPOOLCTL_VERSION,
                           "minimum_contract": MINIMUM_SPOOLCTL_CONTRACT, "version": spool.get("version") or spool.get("tool_version"),
                           "contract_version": spool.get("contract_version")}
    except EvalctlError:
        spoolctl_status = {"available": False, "planned": False, "minimum_version": MINIMUM_SPOOLCTL_VERSION, "minimum_contract": MINIMUM_SPOOLCTL_CONTRACT}
    try:
        if inferctl_capabilities_func is None or inferctl_verb_names_func is None:
            raise EvalctlError("E_INFERCTL_UNAVAILABLE", "inferctl probe unavailable", "run from evalctl.cli for live integration status", 3)
        inferctl = inferctl_capabilities_func(timeout=3)
        inferctl_verbs = inferctl_verb_names_func(inferctl)
        inferctl_status = {
            "available": "preflight" in inferctl_verbs,
            "planned": True,
            "preflight": "preflight" in inferctl_verbs,
            "route": "route" in inferctl_verbs,
            "contract_version": inferctl.get("contract_version"),
        }
    except EvalctlError:
        inferctl_status = {"available": False, "planned": True}
    verbs = {name: capabilities_entry_from_spec(spec) for name, spec in COMMAND_SPECS.items()}
    return {
        "tool_name": TOOL,
        "contract_version": CONTRACT_VERSION,
        "features": ["universal_envelope", "deterministic_output", "artifact_replay", "workspace_diff", "authoring", "execution_replay", "command_scorer", "durable_runs", "resumable", "run_state_jobs", "queue_spoolctl", "bounded_jobs_list", "bounded_case_collections", "doctor", "plan", "inferctl_preflight_provenance"],
        "verbs": verbs,
        "global_flags": {"--json": "structured envelope; accepted only where the verb's json field is true",
                         "--help": "per-verb help, exit 0, no side effects", "--version": "version", "--no-color": "suppress ANSI"},
        "exit_codes": {str(k): v for k, v in EXIT_CODES.items()},
        "error_codes": CODE_REGISTRY,
        "env_vars": {
            "EVALCTL_CASE_FILE": "materialized case JSON passed to runner",
            "EVALCTL_WORKSPACE": "fresh per-case workspace",
            "EVALCTL_OUTPUT_FILE": "runner response destination",
            "EVALCTL_TASK_FILE": "task text file",
            "EVALCTL_DIFF_FILE": "review diff file when present",
            "SOURCE_DATE_EPOCH": "controls deterministic timestamps, including run created_ts",
            "EVALCTL_ACKNOWLEDGE_UNSANDBOXED_RUNNER": "set truthy (1/true/yes/on) to let run/replay execute suite runner and scorer commands; without it (and without --acknowledge-unsandboxed-runner) run/replay refuse with exit 2",
        },
        "integrations": {
            "spoolctl": spoolctl_status,
            "inferctl": inferctl_status,
        },
        "schemas_uri": "evalctl schema <verb> --json",
        "robot_docs_uri": "evalctl robot-docs guide",
    }


def schema_object(required: list[str], properties: dict[str, Any], *, additional: bool = True) -> dict[str, Any]:
    return {"type": "object", "required": required, "properties": properties, "additionalProperties": additional}


def ref(name: str) -> dict[str, Any]:
    return {"$ref": f"#/definitions/{name}"}


# Output vocabularies published as enums in the schema `definitions` block and
# referenced from every schema that carries them. Each enum is exactly the set a
# real run can produce; a test pins each against live output so the two cannot
# drift. Kept sorted so the schema payload is byte-stable.
#   case_status -- the per-case result and the keys of status_counts.
#   run_state   -- how classify_run_dir labels a run directory.
#   plan_action -- what plan proposes per case.
# The per-job `state` inside queue_jobs is deliberately NOT enumerated: it is a
# spoolctl vocabulary that evalctl passes through and does not own.
DEFINITIONS: dict[str, Any] = {
    "case_status": {"type": "string", "enum": ["error", "fail", "pass"]},
    "run_state": {"type": "string", "enum": ["completed", "orphaned", "running", "stale"]},
    "plan_action": {"type": "string", "enum": ["blocked", "run", "skip_terminal"]},
}

STATUS_COUNTS_SCHEMA = {"type": "object", "additionalProperties": {"type": "integer", "minimum": 0}, "propertyNames": ref("case_status")}

CASE_RESULT_SCHEMA = schema_object(["id", "status", "ok"], {"id": {"type": "string"}, "status": ref("case_status"), "ok": {"type": "boolean"}})

FAILURE_SCHEMA = schema_object(["id", "status", "scores"], {"id": {"type": "string"}, "status": ref("case_status"), "scores": {"type": "array", "items": {"type": "object"}}})

PLAN_CASE_SCHEMA = schema_object(["id", "action", "reason"], {"id": {"type": "string"}, "action": ref("plan_action"), "reason": {"type": "string"}})

RUN_SUMMARY_SCHEMA = schema_object(
    ["ok", "case_count", "status_counts"],
    {
        "ok": {"type": "boolean"},
        "case_count": {"type": "integer", "minimum": 0},
        "status_counts": STATUS_COUNTS_SCHEMA,
    },
)

DATA_SCHEMAS = {
    "capabilities": schema_object(
        ["tool_name", "contract_version", "features", "verbs", "global_flags", "exit_codes", "error_codes", "env_vars", "integrations", "schemas_uri", "robot_docs_uri"],
        {
            "tool_name": {"type": "string"},
            "contract_version": {"type": "string", "pattern": r"^\d+\.\d+$"},
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
    "doctor": schema_object(
        ["operation_outcome", "components", "recommended_action", "fallbacks_active", "next_check_after_seconds"],
        {
            "operation_outcome": schema_object(["kind", "exit_code_kind"], {"kind": {"type": "string"}, "exit_code_kind": {"type": "string"}}),
            "components": {"type": "object", "additionalProperties": {"type": "object"}},
            "recommended_action": schema_object(["command", "rationale", "is_destructive", "alternatives"], {"command": {"type": "string"}, "rationale": {"type": "string"}, "is_destructive": {"type": "boolean"}, "alternatives": {"type": "array"}}),
            "fallbacks_active": {"type": "array", "items": {"type": "object"}},
            "next_check_after_seconds": {"type": "integer", "minimum": 0},
        },
    ),
    "plan": schema_object(
        ["suite", "run", "execution", "dependency_graph", "plan", "cases", "warnings"],
        {
            "suite": {"type": "object"},
            "run": {"type": "object"},
            "execution": {"type": "object"},
            "dependency_graph": {"type": "object"},
            "plan": {"type": "object"},
            "cases": {"type": "array", "items": PLAN_CASE_SCHEMA},
            "warnings": {"type": "array", "items": {"type": "object"}},
            "blocked_by_external": schema_object(["kind", "run_id", "reason", "recommended_command"], {"kind": {"type": "string"}, "run_id": {"type": "string"}, "reason": {"type": "string"}, "clears_when": {"type": "string"}, "recommended_command": {"type": "string"}}),
        },
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
            "total_count": {"type": "integer", "minimum": 0},
            "run_id": {"type": "string"},
            "run_dir": {"type": "string"},
            "state": ref("run_state"),
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
        ["suite"],
        {
            "suite": {"type": "string"},
            # Single add mode: one record.
            "id": {"type": "string"},
            "created": {"type": "boolean"},
            "case": {"type": "object"},
            # Bulk (--stdin) mode: per-record outcomes. `added`/`skipped`/
            # `rejected` are disjoint; every input line lands in exactly one.
            "added": {"type": "array", "items": {"type": "object"}},
            "skipped": {"type": "array", "items": {"type": "object"}},
            "rejected": {"type": "array", "items": schema_object(["line", "reason", "message"], {"line": {"type": "integer"}, "id": {"type": ["string", "null"]}, "reason": {"type": "string"}, "message": {"type": "string"}})},
            "counts": {"type": "object", "additionalProperties": {"type": "integer", "minimum": 0}},
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
        ["run_id", "run_dir", "state", "recommended_action"],
        {
            "run_id": {"type": "string"},
            "run_dir": {"type": "string"},
            "state": ref("run_state"),
            "progress": schema_object(["case_count", "terminal", "pending"], {"case_count": {"type": "integer", "minimum": 0}, "terminal": {"type": "integer", "minimum": 0}, "pending": {"type": "integer", "minimum": 0}}),
            "run": RUN_SUMMARY_SCHEMA,
            "cases": {"type": "array", "items": CASE_RESULT_SCHEMA},
            "reservation": {"type": "object"},
            "queue_jobs": {"type": "array", "items": {"type": "object"}},
            "recommended_action": schema_object(["command", "rationale", "alternatives"], {"command": {"type": "string"}, "rationale": {"type": "string"}, "alternatives": {"type": "array", "items": {"type": "string"}}}),
        },
    ),
    "report": schema_object(
        ["run", "failures", "cases", "run_id", "report_hash"],
        {
            "run": schema_object(["ok", "suite", "case_count", "status_counts"], {"ok": {"type": "boolean"}, "suite": {"type": "string"}, "case_count": {"type": "integer", "minimum": 0}, "status_counts": STATUS_COUNTS_SCHEMA}),
            "failures": {"type": "array", "items": FAILURE_SCHEMA},
            "cases": {"type": "array", "items": CASE_RESULT_SCHEMA},
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
    # definitions are always the full set: a single-verb request must still carry
    # the definitions its $ref pointers resolve against.
    return {"envelope_schema": envelope_schema, "schemas": schemas, "definitions": DEFINITIONS}


def robot_docs() -> str:
    return """# evalctl - Agent Workflow Guide

## Quick reference

Capabilities: `evalctl capabilities --json`
Schemas: `evalctl schema run --json`
Initialize: `evalctl init --json`
Validate: `evalctl validate code-review --json`
Diagnose: `evalctl doctor --json`
Plan: `evalctl plan code-review --json`
Run: `evalctl run code-review --acknowledge-unsandboxed-runner --json`
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
4. Run `evalctl doctor --json` when local run state, optional integrations, or
   runtime health are unclear. Doctor exits 0 when it successfully diagnoses
   degraded components; malformed diagnostic input exits 1.
5. Run `evalctl plan <suite> --json` to inspect the resolved case set, run/skip
   actions, concurrency tracks, optional integration posture, and paste-ready
   follow-up commands without creating a run directory or executing runners.
6. Run `evalctl run <suite> --acknowledge-unsandboxed-runner --json`. run and
   replay execute the suite's runner and scorer commands as local code and are not
   sandboxed, so they refuse with `E_UNSANDBOXED_RUNNER_UNACK` (exit 2) unless the
   invoker acknowledges: pass `--acknowledge-unsandboxed-runner`, or set
   `EVALCTL_ACKNOWLEDGE_UNSANDBOXED_RUNNER=1` once for automation. The
   acknowledgment is the caller's, never the suite file's; inspect an untrusted
   suite before acknowledging it. Use `--inferctl-task TASK` when you want
   best-effort inferctl preflight provenance captured before each runner executes.
   Absence, incompatibility, preflight blocks, and capture failures are warnings;
   runner execution and report scoring still proceed. evalctl v0.4 captures
   preflight only and does not call `inferctl route`.
7. If a run is interrupted, use `evalctl run --resume <run-id> --json`. Resume uses
   `run.json`, terminal `cases/<id>/state.json` markers, and the original suite snapshot;
   it skips terminal cases and re-runs only unfinished cases.
8. Use `jobs list --limit 50 --json` to inspect completed, running, stale, and
   orphaned local run state. List output is bounded by default and returns
   `meta.pagination.next_cursor` plus a next-page command when more runs exist.
   Use `jobs get <run-id> --json` and `jobs prune --json` for single-run inspection
   and cleanup. Reservations are TTL files with a background heartbeat; no daemon or
   lock server is required.
9. Optionally use `evalctl run <suite> --queue spoolctl --json` to delegate runner
   execution to spoolctl. Spoolctl is optional and must be >= 0.4.11; absent or incompatible
   spoolctl is a hard error only when `--queue spoolctl` is requested. The queue DB is
   per-run `.spoolctl.db`, so externally managed cross-machine workers require a shared
   filesystem and are not a general hosted-worker mode.
10. Use `status` for run state and recommended next command.
11. Use `report --format json` for a deterministic report envelope or `--format markdown` for a human report.
12. Copy a completed run directory anywhere and run `report --run-dir <path> --format json`;
   evalctl recomputes scores from report artifacts and does not require durability sidecars.
13. After fixing a failed runner/fixture, run `evalctl replay --failed <run-id> --json`
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
Exit `2` is emitted only by `E_UNSANDBOXED_RUNNER_UNACK`: `run`/`replay` refuse to
execute an unacknowledged suite before any runner or scorer runs. Exit `6` keeps
the envelope `ok:true` (the harness succeeded) with `data.run.ok:false` and
`data.fail_on_fail_triggered:true`; branch on those fields, not on `ok`, and a
one-line `eval failure:` summary is also written to stderr.

## Error-code surfaces

Codes with `surface:"envelope"` appear in `errors[]` or `warnings[]` and predict
the command's process-exit class. Codes with `surface:"runner_json"` appear as
per-case `runner.json.error_code` reason codes. Codes with `surface:"score_json"`
appear as per-case scorer verdict reason codes, for example
`E_SCORER_CASE_FAILED` in `cases/<id>/scorers/<scorer_id>.json` or `score.json`.
A runner timeout, runner spawn failure, or command-scorer failure is reportable
case data: `run`/`replay` exits 0 by default, exits 6 with `--fail-on-fail`, emits
`W_PARTIAL_RUN`, and does not put the per-case reason code in `errors[]`.
Unknown command, subcommand, and checked flag typos use `E_UNKNOWN_COMMAND`,
`E_UNKNOWN_SUBCOMMAND`, or `E_UNKNOWN_FLAG`; JSON errors include
`did_you_mean`, `corrected_command`, and `valid_values` when evalctl can safely
construct a correction.

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
