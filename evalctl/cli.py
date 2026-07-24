from __future__ import annotations

import concurrent.futures
import difflib
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from . import __version__

from .static_contract import (
    BOOL,
    BUILTIN_SCORERS,
    CODE_REGISTRY,
    COMMAND_SPECS,
    CONTRACT_VERSION,
    DATA_SCHEMAS,
    DEFAULT_COMMAND_SCORER_TIMEOUT_SECONDS,
    DEFAULT_JOBS_LIST_LIMIT,
    DEFAULT_RESERVATION_TTL_SECONDS,
    DOCTOR_COMPONENTS,
    EXIT_CODES,
    GLOBAL_FLAG_SPECS,
    JOBS_BOOL_FLAGS,
    JOBS_FLAGS_WITH_VALUES,
    MAX_JOBS_LIST_LIMIT,
    OPTIONAL_COMPONENT_STATES,
    PLAN_BOOL_FLAGS,
    PLAN_FLAGS_WITH_VALUES,
    REPLAY_BOOL_FLAGS,
    REPLAY_FLAGS_WITH_VALUES,
    RUN_BOOL_FLAGS,
    RUN_FLAGS_WITH_VALUES,
    SAFE_ID_RE,
    SUBCOMMANDS,
    TOOL,
    VERB_NAMES,
    CommandSpec,
    EvalctlError,
    FlagSpec,
    ParsedArgs,
    capabilities_data as _static_capabilities_data,
    capabilities_entry_from_spec,
    envelope,
    help_text,
    now_iso,
    positive_int_spec,
    robot_docs,
    schema_data,
    schema_object,
    sha256_bytes,
    sha256_text,
    stable_json,
)
from .artifacts import (
    _atomic_write,
    apply_redaction,
    diff_manifests,
    display_path_name,
    load_cases,
    manifest,
    normalize_rel,
    read_json,
    render_text_diff,
    write_json,
)
from .inferctl import (
    capture_inferctl_preflight,
    inferctl_binary,
    inferctl_capabilities,
    inferctl_error_codes,
    inferctl_payload_data,
    inferctl_preflight_summary,
    inferctl_run_context,
    inferctl_verb_names,
    inferctl_warning_codes,
)
from .spoolctl import (
    probe_spoolctl,
    run_spoolctl_json,
    spoolctl_binary,
    spoolctl_flag_names,
    spoolctl_json,
    version_tuple,
)
from .processes import run_process
from .run_state import (
    ReservationHeartbeat,
    build_run_metadata,
    case_entry_from_artifacts,
    classify_run_dir as _classify_run_dir,
    clean_pending_case_dirs,
    clear_reservation,
    finalize_run as _finalize_run,
    is_terminal_marker,
    manifest_from_run_metadata,
    manifest_identity,
    parse_iso_timestamp,
    read_reservation,
    read_run_metadata,
    reservation_is_live,
    reservation_path,
    run_case_counts,
    run_identity,
    runs_root,
    runs_with_inferctl_state,
    runs_with_queue_state,
    split_completed_and_pending,
    status_counts,
    stored_queue_jobs,
    terminal_marker_count,
    timeout_seconds_for_run,
    write_reservation,
    write_run_metadata_once,
    write_terminal_marker,
)
from .scoring import (
    case_manifest_entry,
    normalize_command_verdict,
    run_command_scorer,
    run_scorer,
    score_case,
    score_summary,
    scorer_failure,
)


def finalize_run(run_dir: Path, metadata: dict[str, Any], case_entries: list[dict[str, Any]]) -> tuple[dict[str, Any], bool]:
    return _finalize_run(run_dir, metadata, case_entries, report_builder=report_data, markdown_renderer=markdown_report)


def classify_run_dir(run_dir: Path) -> dict[str, Any]:
    return _classify_run_dir(run_dir, report_builder=report_data)


def wants_json(argv: list[str]) -> bool:
    return "--json" in argv or not sys.stdout.isatty()


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


def nearest(value: str, choices: set[str] | frozenset[str]) -> str | None:
    matches = difflib.get_close_matches(value, sorted(choices), n=1, cutoff=0.6)
    return matches[0] if matches else None


def command_string(parts: list[str]) -> str:
    return " ".join(shlex.quote(part) for part in [TOOL, *parts])


def unknown_command_error(cmd: str, argv: list[str]) -> EvalctlError:
    suggestion = nearest(cmd, VERB_NAMES)
    ctx: dict[str, Any] = {"valid_values": sorted(VERB_NAMES)}
    if suggestion:
        ctx["did_you_mean"] = suggestion
        ctx["corrected_command"] = command_string([suggestion, *argv[1:]])
    return EvalctlError("E_UNKNOWN_COMMAND", f"unknown command '{cmd}'", "try: evalctl capabilities --json", 1, **ctx)


def unknown_subcommand_error(namespace: str, subcommand: str | None, argv: list[str]) -> EvalctlError:
    valid = SUBCOMMANDS[namespace]
    bad = subcommand or ""
    suggestion = nearest(bad, valid) if bad else None
    ctx: dict[str, Any] = {"valid_values": sorted(valid)}
    if suggestion:
        ctx["did_you_mean"] = suggestion
        remainder = argv[2:] if len(argv) > 2 else []
        ctx["corrected_command"] = command_string([namespace, suggestion, *remainder])
    return EvalctlError("E_UNKNOWN_SUBCOMMAND", f"unknown {namespace} subcommand '{bad}'", f"valid {namespace} subcommands: {', '.join(sorted(valid))}", 1, **ctx)


def unknown_flag_error(flag: str, argv: list[str], valid_flags: set[str]) -> EvalctlError:
    suggestion = nearest(flag, valid_flags)
    ctx: dict[str, Any] = {"valid_values": sorted(valid_flags)}
    if suggestion:
        ctx["did_you_mean"] = suggestion
        flag_specs = flag_specs_for_argv(argv, valid_flags)
        suggestion_spec = flag_specs.get(suggestion)
        if suggestion_spec is None or suggestion_spec.kind == "bool":
            corrected = [suggestion if item == flag else item for item in argv]
            ctx["corrected_command"] = command_string(corrected)
        else:
            ctx["requires_value"] = True
            bad_index = argv.index(flag) if flag in argv else -1
            next_value = argv[bad_index + 1] if bad_index >= 0 and bad_index + 1 < len(argv) else None
            registered = registered_flags_for_argv(argv, valid_flags)
            if next_value is not None and next_value != "--" and next_value not in registered and not (next_value.startswith("--") and not suggestion_spec.allow_dash_value):
                corrected = list(argv)
                corrected[bad_index] = suggestion
                ctx["corrected_command"] = command_string(corrected)
    return EvalctlError("E_UNKNOWN_FLAG", f"unknown flag '{flag}'", "check the command's supported flags", 1, **ctx)


def reject_unknown_flags(argv: list[str], stripped_args: list[str], valid_flags: set[str]) -> None:
    for token in stripped_args[1:]:
        if token.startswith("--"):
            raise unknown_flag_error(token, argv, valid_flags)


def spec_for_argv(argv: list[str]) -> CommandSpec | None:
    return COMMAND_SPECS.get(argv[0]) if argv else None


def flag_specs_for_argv(argv: list[str], valid_flags: set[str] | None = None) -> dict[str, FlagSpec]:
    spec = spec_for_argv(argv)
    flags: dict[str, FlagSpec] = dict(GLOBAL_FLAG_SPECS)
    if spec is not None:
        flags.update(spec.flags)
    if valid_flags is not None:
        flags = {flag: spec for flag, spec in flags.items() if flag in valid_flags}
        for flag in valid_flags:
            flags.setdefault(flag, BOOL)
    return flags


def registered_flags_for_argv(argv: list[str], valid_flags: set[str] | None = None) -> set[str]:
    spec = spec_for_argv(argv)
    flags = set(GLOBAL_FLAG_SPECS)
    if spec is not None:
        flags.update(spec.flags)
    if valid_flags is not None:
        flags.update(valid_flags)
    return flags


def command_parser_spec(argv: list[str], fallback: str) -> CommandSpec:
    if argv and argv[0] in COMMAND_SPECS:
        return COMMAND_SPECS[argv[0]]
    return COMMAND_SPECS[fallback]


def flag_message_name(flag: str) -> str:
    return flag


def missing_flag_value_error(flag: str) -> EvalctlError:
    return EvalctlError("E_CASE_INVALID", f"{flag_message_name(flag)} requires a value", f"provide a value for {flag}", 1, flag=flag)


def validate_flag_value(flag: str, value: str, spec: FlagSpec) -> Any:
    if value == "" and not spec.allow_empty:
        raise EvalctlError("E_CASE_INVALID", f"{flag} must not be empty", f"provide a value for {flag}", 1, flag=flag)
    if spec.kind == "text":
        return value
    if spec.kind == "suite_path":
        if value == "":
            raise EvalctlError("E_CASE_INVALID", f"{flag} must not be empty", f"provide {flag}", 1, flag=flag)
        return value
    if spec.kind == "run_path":
        if value == "":
            raise EvalctlError("E_CASE_INVALID", f"{flag} must not be empty", f"provide {flag}", 1, flag=flag)
        return value
    if spec.kind == "safe_id":
        if not is_safe_id(value):
            raise EvalctlError("E_CASE_INVALID", f"invalid {flag} value: {value!r}", "use letters, numbers, dot, underscore, or dash, and do not start with dash", 1, flag=flag)
        return value
    if spec.kind == "enum":
        if value not in spec.choices:
            raise EvalctlError("E_CASE_INVALID", f"{flag} must be one of {', '.join(sorted(spec.choices))} (got {value})", f"choose one of: {', '.join(sorted(spec.choices))}", 1, flag=flag, valid_values=sorted(spec.choices))
        return value
    if spec.kind == "positive_int":
        try:
            parsed = int(value)
        except ValueError:
            raise EvalctlError("E_CASE_INVALID", f"{flag} must be a positive integer (got {value})", f"provide {flag} as a positive integer", 1, flag=flag)
        minimum = spec.minimum if spec.minimum is not None else 1
        if parsed < minimum:
            raise EvalctlError("E_CASE_INVALID", f"{flag} must be at least {minimum} (got {parsed})", f"provide {flag} as a positive integer", 1, flag=flag)
        if spec.maximum is not None and parsed > spec.maximum:
            raise EvalctlError("E_CASE_INVALID", f"{flag} must be at most {spec.maximum} (got {parsed})", f"provide {flag} no larger than {spec.maximum}", 1, flag=flag)
        return parsed
    if spec.kind == "json_text":
        try:
            parsed_json = json.loads(value)
        except json.JSONDecodeError as exc:
            raise EvalctlError("E_CASE_INVALID", f"{flag} is invalid JSON: {exc.msg}", "provide a JSON object", 1, flag=flag)
        if not isinstance(parsed_json, dict):
            raise EvalctlError("E_CASE_INVALID", f"{flag} must be a JSON object", "provide a JSON object", 1, flag=flag)
        return parsed_json
    raise AssertionError(f"unsupported flag kind {spec.kind}")


def parse_command_args(argv: list[str], spec: CommandSpec) -> ParsedArgs:
    values: dict[str, Any] = {}
    bools: set[str] = set()
    positionals: list[str] = []
    flags = {**GLOBAL_FLAG_SPECS, **dict(spec.flags)}
    command_flags = set(flags)
    positional_mode = False
    i = 0
    while i < len(argv):
        token = argv[i]
        if positional_mode:
            positionals.append(token)
            i += 1
            continue
        if token == "--":
            positional_mode = True
            i += 1
            continue
        if token.startswith("--"):
            flag, attached_value = (token.split("=", 1) if "=" in token else (token, None))
            flag_spec = flags.get(flag)
            if flag_spec is None:
                raise unknown_flag_error(flag, argv, command_flags)
            if flag_spec.kind == "bool":
                if attached_value is not None:
                    raise EvalctlError("E_CASE_INVALID", f"{flag} does not take a value", f"use {flag} without a value", 1, flag=flag)
                bools.add(flag)
                i += 1
                continue
            if attached_value is None:
                if i + 1 >= len(argv):
                    raise missing_flag_value_error(flag)
                raw_value = argv[i + 1]
                if raw_value == "--" or raw_value in flags:
                    raise missing_flag_value_error(flag)
                if raw_value.startswith("--") and not flag_spec.allow_dash_value:
                    raise missing_flag_value_error(flag)
                step = 2
            else:
                raw_value = attached_value
                step = 1
            values[flag] = validate_flag_value(flag, raw_value, flag_spec)
            i += step
            continue
        positionals.append(token)
        i += 1
    for flag, flag_spec in spec.flags.items():
        if flag_spec.required and flag not in values and flag not in bools:
            raise EvalctlError("E_CASE_INVALID", f"{flag} is required", f"provide {flag}", 1, flag=flag)
    return ParsedArgs(tuple(positionals), values, frozenset(bools))


def parsed_value(parsed: ParsedArgs, flag: str, default: Any = None) -> Any:
    return parsed.values.get(flag, default)


def parsed_bool(parsed: ParsedArgs, flag: str) -> bool:
    return flag in parsed.bools


def print_envelope(data: Any, *, json_mode: bool, human: str | None = None, warnings: list[dict[str, Any]] | None = None,
                   commands: list[dict[str, Any]] | None = None, started: float | None = None,
                   meta_extra: dict[str, Any] | None = None) -> int:
    if json_mode:
        print(stable_json(envelope(data, warnings=warnings, commands=commands, started=started, meta_extra=meta_extra)))
    else:
        print(human if human is not None else json.dumps(data, indent=2, sort_keys=True))
    return 0


def print_error(err: EvalctlError, *, json_mode: bool, started: float | None = None) -> int:
    print(err.error["message"], file=sys.stderr)
    if err.error.get("corrected_command"):
        print(f"Did you mean: {err.error['corrected_command']}", file=sys.stderr)
    elif err.error.get("did_you_mean"):
        print(f"Did you mean: {err.error['did_you_mean']}", file=sys.stderr)
    if json_mode:
        print(stable_json(envelope(None, ok=False, errors=[err.error], started=started)))
    return err.exit_code


def capabilities_data() -> dict[str, Any]:
    return _static_capabilities_data(
        probe_spoolctl_func=probe_spoolctl,
        inferctl_capabilities_func=inferctl_capabilities,
        inferctl_verb_names_func=inferctl_verb_names,
    )


def command_help(argv: list[str], json_mode: bool, started: float) -> int:
    if len(argv) > 1:
        raise EvalctlError("E_CASE_INVALID", "help accepts no arguments", "try: evalctl --help", 1)
    print(help_text())
    return 0


def command_version(argv: list[str], json_mode: bool, started: float) -> int:
    if len(argv) > 1:
        raise EvalctlError("E_CASE_INVALID", "version accepts no arguments", "try: evalctl --version", 1)
    print(__version__)
    return 0


def command_capabilities(argv: list[str], json_mode: bool, started: float) -> int:
    parsed = parse_command_args(argv, COMMAND_SPECS["capabilities"])
    if len(parsed.positionals) != 1:
        raise EvalctlError("E_CASE_INVALID", "capabilities accepts only flags", "try: evalctl capabilities --json", 1)
    return print_envelope(capabilities_data(), json_mode=True, started=started)


def command_schema(argv: list[str], json_mode: bool, started: float) -> int:
    parsed = parse_command_args(argv, COMMAND_SPECS["schema"])
    args = list(parsed.positionals)
    if len(args) > 2:
        raise EvalctlError("E_CASE_INVALID", "schema accepts at most one verb", "try: evalctl schema run --json", 1)
    return print_envelope(schema_data(args[1] if len(args) > 1 else None), json_mode=True, started=started)


def command_robot_docs(argv: list[str], json_mode: bool, started: float) -> int:
    parsed = parse_command_args(argv, COMMAND_SPECS["robot-docs"])
    args = list(parsed.positionals)
    if len(args) > 1 and args[1] == "guide":
        if len(args) != 2:
            raise EvalctlError("E_CASE_INVALID", "robot-docs guide accepts no extra arguments", "try: evalctl robot-docs guide", 1)
        print(robot_docs(), end="")
        return 0
    raise unknown_subcommand_error("robot-docs", args[1] if len(args) > 1 else None, argv)


def resolve_suite(suite: str | None) -> Path:
    suite = suite or "code-review"
    direct = Path(suite)
    if direct.exists():
        return direct
    candidate = Path("evals") / "suites" / suite
    if candidate.exists():
        return candidate
    raise EvalctlError("E_SUITE_NOT_FOUND", f"suite not found: {suite}", "try: evalctl init && evalctl validate code-review --json", 1)


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


def parse_jobs(argv: list[str]) -> int:
    parsed = parse_command_args(argv, command_parser_spec(argv, "run"))
    return int(parsed_value(parsed, "--jobs", min(os.cpu_count() or 1, 4)))


def parse_positive_int_flag(argv: list[str], flag: str, default: int) -> int:
    parsed = parse_command_args(argv, command_parser_spec(argv, "run"))
    return int(parsed_value(parsed, flag, default))


def parse_jobs_list_limit(argv: list[str]) -> int:
    parsed = parse_command_args(argv, COMMAND_SPECS["jobs"])
    return int(parsed_value(parsed, "--limit", DEFAULT_JOBS_LIST_LIMIT))


def render_runner_arg(arg: str, env: dict[str, str]) -> str:
    for key, value in env.items():
        arg = arg.replace(f"${key}", value)
    return arg


def is_safe_id(value: str) -> bool:
    return bool(value) and not value.startswith("-") and value not in {".", ".."} and ".." not in value and bool(SAFE_ID_RE.fullmatch(value))


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


def dedupe_warnings(warnings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for warning in warnings:
        key = stable_json(warning)
        if key in seen:
            continue
        seen.add(key)
        out.append(warning)
    return out


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
    provenance_path = case_dir / "inferctl-provenance.json"
    provenance = {"inferctl": read_json(provenance_path)} if provenance_path.exists() else None
    return case_manifest_entry(case, "error", scores, provenance=provenance), warnings


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
    timeout = int(timeout_override if timeout_override is not None else runner.get("timeout_seconds") or 300)
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
    if runner.get("shell", False):
        cmd: str | list[str] = render_runner_arg(runner["command"], eval_env)
        shell = True
    else:
        cmd = [render_runner_arg(str(a), eval_env) for a in runner["argv"]]
        shell = False
    return run_process(
        cmd,
        shell=shell,
        cwd=prepared["cwd"],
        env=prepared["env"],
        timeout=prepared["timeout"],
        stdin_text=case["task"] if runner.get("stdin") == "task" else None,
    ).as_dict()


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
    provenance_path = case_dir / "inferctl-provenance.json"
    provenance = {"inferctl": read_json(provenance_path)} if provenance_path.exists() else None
    return case_manifest_entry(case, status, scores, provenance=provenance), warnings


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
    data = spoolctl_json(args, timeout=3)
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
                                   jobs: int, timeout_override: int | None, slots: int,
                                   inferctl_context: dict[str, Any] | None = None) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    db_path = run_dir / ".spoolctl.db"
    clean_pending_case_dirs(run_dir, pending_cases)
    prepared_by_case: dict[str, dict[str, Any]] = {}
    job_ids: list[str] = []
    warnings: list[dict[str, Any]] = []
    for case in sorted(pending_cases, key=lambda c: c["id"]):
        prepared = prepare_case_workspace(suite_dir, suite, case, run_dir, timeout_override)
        if inferctl_context is not None:
            _, capture_warnings = capture_inferctl_preflight(prepared, inferctl_context)
            warnings.extend(capture_warnings)
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
    entries_by_id = dict(completed_entries)
    for case in sorted(pending_cases, key=lambda c: c["id"]):
        prepared = prepared_by_case[case["id"]]
        job_doc = read_json(prepared["case_dir"] / "job.json")
        detail = spoolctl_json(["show", "--db", str(db_path), "--json", job_doc["job_id"]], timeout=3)
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


def run_case(suite_dir: Path, suite: dict[str, Any], case: dict[str, Any], run_dir: Path, timeout_override: int | None,
             inferctl_context: dict[str, Any] | None = None) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    prepared = prepare_case_workspace(suite_dir, suite, case, run_dir, timeout_override)
    all_warnings = list(prepared["warnings"])
    if inferctl_context is not None:
        _, warnings = capture_inferctl_preflight(prepared, inferctl_context)
        all_warnings.extend(warnings)
    runner_result = execute_runner_in_process(prepared)
    output_text, runner_json, warnings = normalize_runner_artifacts(prepared, runner_result)
    all_warnings.extend(warnings)
    entry, warnings = capture_workspace_after_and_score(prepared, output_text, runner_json)
    all_warnings.extend(warnings)
    write_terminal_marker(prepared["case_dir"], case["id"], entry["status"])
    return entry, all_warnings


def command_init(argv: list[str], json_mode: bool, started: float) -> int:
    parsed = parse_command_args(argv, COMMAND_SPECS["init"])
    if len(parsed.positionals) != 1:
        raise EvalctlError("E_CASE_INVALID", "init accepts only flags", "try: evalctl init --json", 1)
    data = init_project(force=parsed_bool(parsed, "--force"))
    return print_envelope(data, json_mode=json_mode, human=f"Created {data['created']} with suite {data['suite']}", started=started)


def command_validate(argv: list[str], json_mode: bool, started: float) -> int:
    parsed = parse_command_args(argv, COMMAND_SPECS["validate"])
    if len(parsed.positionals) > 2:
        raise EvalctlError("E_CASE_INVALID", "validate accepts at most one suite positional", "try: evalctl validate code-review --json", 1)
    suite_arg = parsed.positionals[1] if len(parsed.positionals) > 1 else "code-review"
    data = validate_suite(resolve_suite(suite_arg))
    return print_envelope(data, json_mode=json_mode, human=f"{data['suite']}: {data['case_count']} cases valid", started=started)


def runner_from_authoring_flags(argv: list[str], *, prefix: str = "--runner", parsed: ParsedArgs | None = None) -> dict[str, Any]:
    parsed = parsed or parse_command_args(argv, COMMAND_SPECS["suite"])
    argv_value = parsed_value(parsed, f"{prefix}-argv")
    command_value = parsed_value(parsed, f"{prefix}-command")
    shell = parsed_bool(parsed, "--shell")
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
    parsed = parse_command_args(argv, COMMAND_SPECS["suite"])
    args = list(parsed.positionals)
    if len(args) != 3 or args[1] != "add":
        raise EvalctlError("E_CASE_INVALID", "suite command requires: suite add <name>", "try: evalctl suite add demo --runner-argv \"python3 $EVALCTL_WORKSPACE/r.py\" --json", 1)
    data = suite_add_data(args[2], runner_from_authoring_flags(argv, parsed=parsed))
    return print_envelope(data, json_mode=json_mode, human=f"{'Created' if data['created'] else 'Exists'} suite {data['suite']}", started=started)


def case_add_data(suite_name: str, task: str, workspace_raw: str, *, case_id: str | None = None,
                  diff_raw: str | None = None, expect_raw: str | dict[str, Any] | None = None) -> dict[str, Any]:
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
    if expect_raw is not None:
        if isinstance(expect_raw, dict):
            expect = expect_raw
        else:
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
    parsed = parse_command_args(argv, COMMAND_SPECS["case"])
    args = list(parsed.positionals)
    if len(args) != 3 or args[1] != "add":
        raise EvalctlError("E_CASE_INVALID", "case command requires: case add <suite>", "try: evalctl case add demo --task \"do X\" --workspace fixtures/x --json", 1)
    task = parsed_value(parsed, "--task")
    workspace = parsed_value(parsed, "--workspace")
    if task is None:
        raise EvalctlError("E_CASE_INVALID", "case add requires --task", "provide --task text", 1)
    if workspace is None:
        raise EvalctlError("E_CASE_INVALID", "case add requires --workspace", "provide --workspace fixtures/name", 1)
    data = case_add_data(args[2], task, workspace, case_id=parsed_value(parsed, "--id"), diff_raw=parsed_value(parsed, "--diff"), expect_raw=parsed_value(parsed, "--expect-json"))
    return print_envelope(data, json_mode=json_mode, human=f"{'Added' if data['created'] else 'Exists'} case {data['id']}", started=started)


def command_scorer_config(argv: list[str], parsed: ParsedArgs | None = None) -> dict[str, Any]:
    parsed = parsed or parse_command_args(argv, COMMAND_SPECS["scorer"])
    name = parsed_value(parsed, "--name")
    if name is None:
        raise EvalctlError("E_CASE_INVALID", "scorer add requires --name", "provide --name exact or --name command", 1)
    if parsed_bool(parsed, "--required") and parsed_bool(parsed, "--advisory"):
        raise EvalctlError("E_CASE_INVALID", "--required and --advisory are mutually exclusive", "choose one scorer requirement mode", 1)
    required = not parsed_bool(parsed, "--advisory")
    config: dict[str, Any] = {"name": name, "required": required}
    if name != "command":
        if any(flag in parsed.values for flag in ("--argv", "--command", "--timeout")) or parsed_bool(parsed, "--shell"):
            raise EvalctlError("E_CASE_INVALID", "built-in scorers do not accept command runner flags", "use --name command for external scorers", 1)
        if "--id" in parsed.values:
            config["id"] = parsed_value(parsed, "--id")
        return config
    scorer_id = parsed_value(parsed, "--id")
    if scorer_id is None or not is_safe_id(scorer_id):
        raise EvalctlError("E_CASE_INVALID", "command scorer requires a path-safe --id", "use letters, numbers, dot, underscore, or dash", 1)
    argv_value = parsed_value(parsed, "--argv")
    command_value = parsed_value(parsed, "--command")
    if bool(argv_value) == bool(command_value):
        raise EvalctlError("E_CASE_INVALID", "--argv and --command are mutually exclusive for command scorers", "provide exactly one command form", 1)
    shell = parsed_bool(parsed, "--shell")
    if argv_value and shell:
        raise EvalctlError("E_CASE_INVALID", "--shell requires --command, not --argv", "drop --shell or use --command", 1)
    if command_value and not shell:
        raise EvalctlError("E_CASE_INVALID", "--command requires --shell", "use --argv for shell:false scorers", 1)
    config["id"] = scorer_id
    config["shell"] = shell
    if argv_value:
        argv_parts = shlex.split(argv_value)
        if not argv_parts:
            raise EvalctlError("E_CASE_INVALID", "--argv must not be empty", "provide at least one argv token", 1)
        config["argv"] = argv_parts
    else:
        config["command"] = command_value
    timeout = parsed_value(parsed, "--timeout")
    if timeout is not None:
        config["timeout_seconds"] = timeout
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
    parsed = parse_command_args(argv, COMMAND_SPECS["scorer"])
    args = list(parsed.positionals)
    if len(args) != 3 or args[1] != "add":
        raise EvalctlError("E_CASE_INVALID", "scorer command requires: scorer add <suite>", "try: evalctl scorer add demo --name exact --required --json", 1)
    data = scorer_add_data(args[2], command_scorer_config(argv, parsed))
    label = data["id"] or data["scorer"]
    return print_envelope(data, json_mode=json_mode, human=f"{'Added' if data['created'] else 'Exists'} scorer {label}", started=started)


def execute_pending_cases(suite_dir: Path, suite: dict[str, Any], all_cases: list[dict[str, Any]], pending_cases: list[dict[str, Any]],
                          completed_entries: dict[str, dict[str, Any]], run_dir: Path, jobs: int,
                          timeout_override: int | None,
                          inferctl_context: dict[str, Any] | None = None) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    all_warnings: list[dict[str, Any]] = []
    case_results: dict[str, tuple[dict[str, Any], list[dict[str, Any]]]] = {}
    clean_pending_case_dirs(run_dir, pending_cases)
    with concurrent.futures.ThreadPoolExecutor(max_workers=jobs) as executor:
        future_to_case = {executor.submit(run_case, suite_dir, suite, case, run_dir, timeout_override, inferctl_context): case for case in sorted(pending_cases, key=lambda c: c["id"])}
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
                  queue_backend: str | None = None, slots: int | None = None,
                  inferctl_task: str | None = None) -> tuple[dict[str, Any], list[dict[str, Any]], bool]:
    run_dir.mkdir(parents=True)
    shutil.copytree(suite_dir, run_dir / "suite-snapshot")
    queue = {"backend": "spoolctl", "db": ".spoolctl.db", "jobs": {}} if queue_backend == "spoolctl" else None
    all_warnings = [{"code": "W_UNSANDBOXED_RUNNER", "message": "runner commands execute arbitrary local code; evalctl is not a sandbox"}]
    inferctl_context, inferctl_warnings = inferctl_run_context(inferctl_task)
    all_warnings.extend(inferctl_warnings)
    metadata = build_run_metadata(suite, suite_dir, cases, run_id, slots or jobs, timeout_override, replayed_from, queue=queue, mode="queued" if queue_backend == "spoolctl" else "synchronous",
                                  provenance={"inferctl": inferctl_context} if inferctl_context.get("requested") else None)
    write_run_metadata_once(run_dir, metadata)
    with ReservationHeartbeat(run_dir, run_id, reservation_ttl):
        if queue_backend == "spoolctl":
            case_entries, run_warnings = execute_spoolctl_pending_cases(suite_dir, suite, cases, cases, {}, run_dir, run_id, jobs, timeout_override, slots or jobs, inferctl_context)
        else:
            case_entries, run_warnings = execute_pending_cases(suite_dir, suite, cases, cases, {}, run_dir, jobs, timeout_override, inferctl_context)
        all_warnings.extend(run_warnings)
        if any(c["status"] == "error" for c in case_entries):
            all_warnings.append({"code": "W_PARTIAL_RUN", "message": "some cases errored; report remains generable"})
        data, run_ok = finalize_run(run_dir, metadata, case_entries)
        clear_reservation(run_dir)
    return data, dedupe_warnings(all_warnings), run_ok


def plan_case_entry(case: dict[str, Any], action_name: str, reason: str, suite: dict[str, Any], *, inferctl_task: str | None = None) -> dict[str, Any]:
    return {
        "id": case["id"],
        "action": action_name,
        "reason": reason,
        "runner": suite.get("runner", {}),
        "workspace": {"source": case.get("workspace")},
        "scorers": suite.get("scorers", []),
        "provenance": {"inferctl": {"requested": inferctl_task is not None, "task": inferctl_task, "available": inferctl_binary() is not None}},
    }


def build_tracks(case_items: list[dict[str, Any]], jobs: int) -> list[dict[str, Any]]:
    track_count = max(1, min(jobs, max(len([item for item in case_items if item["action"] == "run"]), 1)))
    tracks = [{"id": f"slot-{idx + 1}", "items": []} for idx in range(track_count)]
    run_index = 0
    for item in case_items:
        if item["action"] == "run":
            tracks[run_index % track_count]["items"].append({"id": item["id"], "action": item["action"]})
            run_index += 1
    return tracks


def command_plan(argv: list[str], json_mode: bool, started: float) -> int:
    parsed = parse_command_args(argv, COMMAND_SPECS["plan"])
    args = list(parsed.positionals)
    if len(args) > 2:
        raise EvalctlError("E_CASE_INVALID", "plan accepts at most one suite positional", "try: evalctl plan code-review --json", 1)
    resume_id = parsed_value(parsed, "--resume")
    run_id_arg = parsed_value(parsed, "--run-id")
    queue_backend = parsed_value(parsed, "--queue")
    slots_raw = parsed_value(parsed, "--slots")
    if slots_raw is not None and queue_backend is None:
        raise EvalctlError("E_CASE_INVALID", "--slots requires --queue spoolctl", "try: evalctl plan code-review --queue spoolctl --slots 4 --json", 1)
    inferctl_task = parsed_value(parsed, "--inferctl-task")
    jobs = int(parsed_value(parsed, "--jobs", min(os.cpu_count() or 1, 4)))
    slots = int(slots_raw) if queue_backend == "spoolctl" and slots_raw is not None else jobs if queue_backend == "spoolctl" else None
    timeout_override = parsed_value(parsed, "--timeout")
    warnings: list[dict[str, Any]] = []
    commands: list[dict[str, str]] = []

    completed_entries: dict[str, dict[str, Any]] = {}
    if resume_id is not None:
        run_dir = Path("evals") / "runs" / resume_id
        if not run_dir.exists():
            raise EvalctlError("E_RUN_NOT_FOUND", f"run not found: {resume_id}", "resume an existing incomplete run id", 1)
        metadata = read_run_metadata(run_dir)
        suite_dir = run_dir / "suite-snapshot"
        suite = read_json(suite_dir / "suite.json")
        cases = load_cases(suite_dir / suite.get("cases", "cases.jsonl"))
        completed_entries, pending_cases = split_completed_and_pending(run_dir, cases)
        run_id = resume_id
        run_dir_value: str | None = str(run_dir)
        run_mode = "resume" if pending_cases else "existing_completed"
        jobs = int(metadata["execution"]["jobs"])
        execution_mode = "queued" if metadata["execution"].get("mode") == "queued" else "synchronous"
        timeout_seconds = int(metadata["execution"]["timeout_seconds"])
        case_items = [
            plan_case_entry(case, "skip_terminal" if case["id"] in completed_entries else "run", "terminal case already completed" if case["id"] in completed_entries else "pending case", suite, inferctl_task=inferctl_task)
            for case in cases
        ]
        suite_name = suite.get("name", suite_dir.name)
        commands.append({"command": f"evalctl run --resume {shlex.quote(resume_id)} --json", "rationale": "Resume pending cases."})
    else:
        if len(args) != 2:
            raise EvalctlError("E_SUITE_NOT_FOUND", "plan requires a suite name", "try: evalctl plan code-review --json", 1)
        suite_dir = resolve_suite(args[1])
        validate_suite(suite_dir)
        suite = read_json(suite_dir / "suite.json")
        cases = load_cases(suite_dir / suite.get("cases", "cases.jsonl"))
        run_id = run_id_arg
        run_dir = Path("evals") / "runs" / run_id if run_id else None
        run_dir_value = str(run_dir) if run_dir is not None else None
        run_mode = "fresh"
        if run_dir is not None and run_dir.exists():
            if (run_dir / "manifest.json").exists():
                manifest_doc = read_json(run_dir / "manifest.json")
                if run_identity(suite, suite_dir, cases) == manifest_identity(manifest_doc):
                    run_mode = "existing_completed"
                    commands.append({"command": f"evalctl report {shlex.quote(run_id)} --format json", "rationale": "Inspect existing completed run."})
                else:
                    run_mode = "blocked"
                    warnings.append({"code": "W_PLAN_BLOCKED", "message": "run id is already completed for a different suite or case set"})
                    commands.append({"command": "evalctl doctor --json", "rationale": "Inspect run conflict state."})
            else:
                reservation = read_reservation(run_dir)
                run_mode = "blocked"
                if reservation and reservation_is_live(reservation):
                    warnings.append({"code": "W_PLAN_BLOCKED", "message": "run reservation is live"})
                else:
                    warnings.append({"code": "W_PLAN_BLOCKED", "message": "run is incomplete and may be resumable"})
                    commands.append({"command": f"evalctl run --resume {shlex.quote(run_id)} --json", "rationale": "Resume incomplete run."})
        execution_mode = "queued" if queue_backend == "spoolctl" else "synchronous"
        timeout_seconds = timeout_seconds_for_run(suite, timeout_override)
        case_items = [plan_case_entry(case, "blocked" if run_mode == "blocked" else "run", "plan is blocked" if run_mode == "blocked" else "pending case", suite, inferctl_task=inferctl_task) for case in cases]
        suite_name = suite.get("name", suite_dir.name)
        if run_mode == "fresh":
            run_cmd = ["evalctl", "run", suite_name]
            if run_id:
                run_cmd.extend(["--run-id", run_id])
            if jobs:
                run_cmd.extend(["--jobs", str(jobs)])
            if timeout_override is not None:
                run_cmd.extend(["--timeout", str(timeout_override)])
            if queue_backend:
                run_cmd.extend(["--queue", queue_backend])
            if slots is not None:
                run_cmd.extend(["--slots", str(slots)])
            if inferctl_task:
                run_cmd.extend(["--inferctl-task", inferctl_task])
            run_cmd.append("--json")
            commands.append({"command": " ".join(shlex.quote(part) for part in run_cmd), "rationale": "Execute this plan."})

    if queue_backend == "spoolctl":
        try:
            probe_spoolctl(timeout=3)
        except EvalctlError as exc:
            warnings.append({"code": exc.error["code"], "message": exc.error["message"], "requested_queue": "spoolctl"})
            if run_mode == "fresh":
                run_mode = "blocked"
                for item in case_items:
                    item["action"] = "blocked"
                    item["reason"] = "spoolctl is unavailable or incompatible"
                commands.append({"command": "evalctl doctor --component spoolctl --json", "rationale": "Diagnose spoolctl availability."})

    will_run = len([item for item in case_items if item["action"] == "run"])
    will_skip = len([item for item in case_items if item["action"] == "skip_terminal"])
    blocked = len([item for item in case_items if item["action"] == "blocked"])
    tracks = build_tracks(case_items, slots or jobs)
    data = {
        "suite": {"name": suite_name, "suite_dir": str(suite_dir), "case_count": len(cases)},
        "run": {
            "run_id": run_id,
            "run_id_strategy": "explicit" if run_id else "generated_at_run_time",
            "run_dir": run_dir_value,
            "mode": run_mode,
        },
        "execution": {"mode": execution_mode, "jobs": jobs, "slots": slots, "timeout_seconds": timeout_seconds},
        "dependency_graph": {"kind": "independent_cases", "edges": []},
        "plan": {"summary": {"total_items": len(case_items), "will_run": will_run, "will_skip": will_skip, "blocked": blocked, "parallel_tracks": len(tracks)}, "tracks": tracks},
        "cases": case_items,
        "warnings": warnings,
    }
    return print_envelope(data, json_mode=json_mode, human=f"plan {suite_name}: {will_run} run, {will_skip} skip, {blocked} blocked", warnings=warnings, commands=commands, started=started)


def command_run(argv: list[str], json_mode: bool, started: float) -> int:
    parsed = parse_command_args(argv, COMMAND_SPECS["run"])
    resume_id = parsed_value(parsed, "--resume")
    if resume_id is not None:
        return command_run_resume(argv, resume_id, json_mode, started, parsed=parsed)
    args = list(parsed.positionals)
    if len(args) < 2:
        raise EvalctlError("E_SUITE_NOT_FOUND", "run requires a suite name", "try: evalctl run code-review --json", 1)
    queue_backend = parsed_value(parsed, "--queue")
    slots_raw = parsed_value(parsed, "--slots")
    if slots_raw is not None and queue_backend is None:
        raise EvalctlError("E_CASE_INVALID", "--slots requires --queue spoolctl", "try: evalctl run code-review --queue spoolctl --slots 4 --json", 1)
    jobs = int(parsed_value(parsed, "--jobs", min(os.cpu_count() or 1, 4)))
    slots = int(slots_raw) if queue_backend == "spoolctl" and slots_raw is not None else jobs if queue_backend == "spoolctl" else None
    reservation_ttl = int(parsed_value(parsed, "--reservation-ttl", DEFAULT_RESERVATION_TTL_SECONDS))
    timeout_override = parsed_value(parsed, "--timeout")
    run_id_value = parsed_value(parsed, "--run-id")
    inferctl_task = parsed_value(parsed, "--inferctl-task")
    if queue_backend == "spoolctl":
        probe_spoolctl()
    suite_dir = resolve_suite(args[1])
    validate_suite(suite_dir)
    suite = read_json(suite_dir / "suite.json")
    cases = load_cases(suite_dir / suite.get("cases", "cases.jsonl"))
    unsandboxed_warning = {"code": "W_UNSANDBOXED_RUNNER", "message": "runner commands execute arbitrary local code; evalctl is not a sandbox"}
    if not suite.get("acknowledged_unsandboxed_runner") and sys.stderr.isatty():
        print(unsandboxed_warning["message"], file=sys.stderr)
    run_id = run_id_value if run_id_value is not None else datetime.now(timezone.utc).strftime("%Y-%m-%dT%H%M%SZ-") + suite.get("name", suite_dir.name)
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
    data, all_warnings, run_ok = execute_cases(suite_dir, suite, cases, run_dir, run_id, jobs, timeout_override, None, reservation_ttl, queue_backend, slots, inferctl_task)
    commands = [{"command": f"evalctl report {run_id} --format json", "rationale": "regenerate deterministic JSON report"}]
    print_envelope(data, json_mode=json_mode, human=f"Run {run_id}: {'pass' if run_ok else 'fail'}", warnings=all_warnings, commands=commands, started=started)
    return 6 if parsed_bool(parsed, "--fail-on-fail") and not run_ok else 0


def command_run_resume(argv: list[str], run_id: str, json_mode: bool, started: float, *, parsed: ParsedArgs | None = None) -> int:
    parsed = parsed or parse_command_args(argv, COMMAND_SPECS["run"])
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
    reservation_ttl = int(parsed_value(parsed, "--reservation-ttl", DEFAULT_RESERVATION_TTL_SECONDS))
    jobs = int(metadata["execution"]["jobs"])
    timeout_override = int(metadata["execution"]["timeout_seconds"])
    queued = metadata["execution"].get("mode") == "queued" and metadata.get("queue", {}).get("backend") == "spoolctl"
    inferctl_context = metadata.get("provenance", {}).get("inferctl") if isinstance(metadata.get("provenance"), dict) else None
    if queued:
        probe_spoolctl()
    with ReservationHeartbeat(run_dir, run_id, reservation_ttl):
        if pending_cases:
            if queued:
                case_entries, run_warnings = execute_spoolctl_pending_cases(suite_dir, suite, cases, pending_cases, completed_entries, run_dir, run_id, jobs, timeout_override, jobs, inferctl_context)
            else:
                case_entries, run_warnings = execute_pending_cases(suite_dir, suite, cases, pending_cases, completed_entries, run_dir, jobs, timeout_override, inferctl_context)
            warnings.extend(run_warnings)
        else:
            case_entries = [completed_entries[case["id"]] for case in sorted(cases, key=lambda c: c["id"])]
            warnings.append(nothing_pending_warning)
        if any(c["status"] == "error" for c in case_entries):
            warnings.append({"code": "W_PARTIAL_RUN", "message": "some cases errored; report remains generable"})
        data, run_ok = finalize_run(run_dir, metadata, case_entries)
        clear_reservation(run_dir)
    commands = [{"command": f"evalctl report {run_id} --format json", "rationale": "regenerate deterministic JSON report"}]
    print_envelope(data, json_mode=json_mode, human=f"Resume {run_id}: {'pass' if run_ok else 'fail'}", warnings=dedupe_warnings(warnings), commands=commands, started=started)
    return 6 if parsed_bool(parsed, "--fail-on-fail") and not run_ok else 0


def action(command: str, rationale: str, *, is_destructive: bool = False, alternatives: list[Any] | None = None) -> dict[str, Any]:
    return {"command": command, "rationale": rationale, "is_destructive": is_destructive, "alternatives": alternatives or []}


def component(state: str, details: str, **extra: Any) -> dict[str, Any]:
    data = {"state": state, "details": details}
    data.update({k: v for k, v in extra.items() if v is not None})
    return data


def probe_runtime() -> dict[str, Any]:
    ok = sys.version_info >= (3, 11)
    return component(
        "healthy" if ok else "degraded",
        f"Python {sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}; evalctl {__version__}",
        observed={"python": sys.version.split()[0], "evalctl_version": __version__, "cwd_readable": os.access(Path.cwd(), os.R_OK)},
        recommended_action=None if ok else action("python3.11 -m evalctl doctor --json", "Use Python 3.11 or newer."),
    )


def probe_suite_root() -> dict[str, Any]:
    evals = Path("evals")
    default_suite = evals / "suites" / "code-review"
    if not evals.exists():
        return component("degraded", "evals/ is not initialized", recommended_action=action("evalctl init --json", "Initialize the default evalctl suite."))
    if not default_suite.exists():
        return component("degraded", "default code-review suite is absent", recommended_action=action("evalctl init --json", "Create or restore the default suite."))
    try:
        validate_suite(default_suite)
    except EvalctlError as exc:
        return component("unhealthy", "default suite is invalid", errors=[exc.error], recommended_action=action("evalctl validate code-review --json", "Inspect suite validation errors."))
    return component("healthy", "default suite is present and valid", observed={"suite": "code-review"})


def probe_runs_root() -> dict[str, Any]:
    root = runs_root()
    if not root.exists():
        return component("not_configured", "no runs directory exists yet", observed={"runs_root": str(root)})
    readable = os.access(root, os.R_OK)
    writable = os.access(root, os.W_OK)
    if not (readable and writable):
        return component("unhealthy", "runs directory is not readable and writable", observed={"readable": readable, "writable": writable})
    return component("healthy", "runs directory is readable and writable", observed={"runs_root": str(root)})


def probe_reservations(run_dirs: list[Path]) -> dict[str, Any]:
    classified = [classify_run_dir(path) for path in run_dirs]
    stale = [item["run_id"] for item in classified if item["state"] == "stale"]
    live = [item["run_id"] for item in classified if item["state"] == "running"]
    if stale:
        recommended = action("evalctl jobs prune --json", "Remove stale reservations and orphaned run directories.")
        if len(stale) == 1:
            recommended["alternatives"] = [f"evalctl run --resume {stale[0]} --json"]
        return component("degraded", "stale reservations are present", observed={"stale": stale, "live": live}, recommended_action=recommended)
    return component("healthy", "no stale reservations found", observed={"live": live})


def probe_spoolctl_component(run_dirs: list[Path], *, fast: bool) -> dict[str, Any]:
    queued = [path.name for path in runs_with_queue_state(run_dirs)]
    binary = shutil.which("spoolctl")
    if fast:
        state = "unknown" if binary else ("degraded" if queued else "not_configured")
        return component(state, "fast mode used PATH-only spoolctl check", observed={"binary": binary, "queued_runs": queued})
    if binary is None:
        state = "degraded" if queued else "not_configured"
        recommended = action("install spoolctl >= 0.4.1 or run without --queue spoolctl", "Queued state exists but spoolctl is absent.") if queued else None
        return component(state, "spoolctl is not available on PATH", observed={"queued_runs": queued}, recommended_action=recommended)
    try:
        data = probe_spoolctl(timeout=3)
        return component("healthy", "spoolctl is compatible", observed={"version": data.get("version") or data.get("tool_version"), "queued_runs": queued})
    except EvalctlError as exc:
        state = "unhealthy" if queued else "degraded"
        return component(state, "spoolctl is present but not compatible or responsive", observed={"queued_runs": queued}, errors=[exc.error], recommended_action=action("evalctl doctor --component spoolctl --fast --json", "Use fast diagnostics or inspect spoolctl separately."))


def probe_inferctl_component(run_dirs: list[Path], *, fast: bool) -> dict[str, Any]:
    provenance_runs = [path.name for path in runs_with_inferctl_state(run_dirs)]
    binary = inferctl_binary()
    if fast:
        return component("unknown" if binary else "not_configured", "fast mode used PATH-only inferctl check", observed={"binary": binary, "provenance_runs": provenance_runs})
    if binary is None:
        return component("not_configured", "inferctl is not available on PATH", observed={"provenance_runs": provenance_runs})
    try:
        data = inferctl_capabilities(timeout=3)
        verbs = inferctl_verb_names(data)
        if "preflight" not in verbs:
            return component("degraded", "inferctl is present but lacks preflight support", observed={"contract_version": data.get("contract_version"), "verbs": sorted(verbs)}, recommended_action=action("inferctl capabilities --json", "Inspect inferctl capabilities."))
        return component("healthy", "inferctl preflight support is available", observed={"contract_version": data.get("contract_version"), "verbs": sorted(verbs), "route_available": "route" in verbs})
    except EvalctlError as exc:
        return component("degraded", "inferctl is present but not compatible or responsive", errors=[exc.error], observed={"provenance_runs": provenance_runs}, recommended_action=action("evalctl doctor --component inferctl --fast --json", "Use fast diagnostics or inspect inferctl separately."))


def probe_runner_safety() -> dict[str, Any]:
    return component("healthy", "runner and scorer commands execute as local code; evalctl is not a sandbox", observed={"sandboxed": False}, warnings=[{"code": "W_UNSANDBOXED_RUNNER", "message": "inspect suites before running untrusted runner or scorer commands"}])


def safe_component_probe(name: str, probe: Any) -> dict[str, Any]:
    try:
        return probe()
    except EvalctlError as exc:
        return component("unhealthy", f"{name} probe failed", errors=[exc.error])
    except (OSError, json.JSONDecodeError, subprocess.TimeoutExpired, PermissionError) as exc:
        return component("unhealthy", f"{name} probe failed", errors=[{"code": "E_RUNNER_FAILED", "message": str(exc)}])
    except Exception as exc:
        return component("unhealthy", f"{name} probe failed", errors=[{"code": "E_RUNNER_FAILED", "message": str(exc)}])


def doctor_data(component_name: str | None, *, fast: bool) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    root = runs_root()
    run_dirs = sorted([p for p in root.iterdir() if p.is_dir()], key=lambda p: p.name) if root.exists() else []
    probes = {
        "runtime": lambda: probe_runtime(),
        "suite_root": lambda: probe_suite_root(),
        "runs_root": lambda: probe_runs_root(),
        "reservations": lambda: probe_reservations(run_dirs),
        "spoolctl": lambda: probe_spoolctl_component(run_dirs, fast=fast),
        "inferctl": lambda: probe_inferctl_component(run_dirs, fast=fast),
        "runner_safety": lambda: probe_runner_safety(),
    }
    selected = [component_name] if component_name else sorted(DOCTOR_COMPONENTS)
    components = {name: safe_component_probe(name, probes[name]) for name in selected}
    states = [item["state"] for item in components.values()]
    if "unhealthy" in states:
        outcome = "unhealthy"
    elif "degraded" in states:
        outcome = "degraded"
    else:
        outcome = "healthy"
    recommended = None
    for item in components.values():
        rec = item.get("recommended_action")
        if rec and not rec.get("is_destructive"):
            recommended = rec
            break
    commands = []
    if recommended:
        commands.append({"command": recommended["command"], "rationale": recommended["rationale"]})
    if component_name is None:
        commands.append({"command": "evalctl jobs list --limit 50 --json", "rationale": "Inspect local run state."})
    data = {
        "operation_outcome": {"kind": outcome, "health_kind": "all-clear" if outcome == "healthy" else "attention-needed"},
        "components": components,
        "recommended_action": recommended or action("evalctl jobs list --limit 50 --json", "Inspect local run state."),
        "fallbacks_active": [],
    }
    return data, commands


def command_doctor(argv: list[str], json_mode: bool, started: float) -> int:
    parsed = parse_command_args(argv, COMMAND_SPECS["doctor"])
    args = list(parsed.positionals)
    component_name = parsed_value(parsed, "--component")
    if component_name is not None and component_name not in DOCTOR_COMPONENTS:
        raise EvalctlError("E_UNKNOWN_COMPONENT", f"unknown doctor component '{component_name}'", "choose one of the valid component names", 1, valid_values=sorted(DOCTOR_COMPONENTS))
    if len(args) != 1:
        raise EvalctlError("E_CASE_INVALID", "doctor accepts only flags", "try: evalctl doctor --json", 1)
    data, commands = doctor_data(component_name, fast=parsed_bool(parsed, "--fast"))
    return print_envelope(data, json_mode=json_mode, human=f"doctor: {data['operation_outcome']['kind']}", commands=commands, started=started)


def command_jobs(argv: list[str], json_mode: bool, started: float) -> int:
    parsed = parse_command_args(argv, COMMAND_SPECS["jobs"])
    args = list(parsed.positionals)
    if len(args) < 2:
        raise EvalctlError("E_CASE_INVALID", "jobs requires list, get, or prune", "try: evalctl jobs list --json", 1)
    subcommand = args[1]
    if subcommand in {"get", "prune"} and ("--limit" in argv or "--cursor" in argv):
        raise EvalctlError("E_CASE_INVALID", f"jobs {subcommand} does not accept list pagination flags", "try: evalctl jobs list --limit 50 --json", 1, corrected_command="evalctl jobs list --limit 50 --json")
    root = runs_root()
    run_dirs = sorted([p for p in root.iterdir() if p.is_dir()], key=lambda p: p.name) if root.exists() else []
    if subcommand == "list":
        limit = int(parsed_value(parsed, "--limit", DEFAULT_JOBS_LIST_LIMIT))
        cursor = parsed_value(parsed, "--cursor")
        if cursor is None:
            page_dirs = run_dirs
        else:
            page_dirs = [path for path in run_dirs if path.name > cursor]
        page = page_dirs[:limit]
        runs = [classify_run_dir(path) for path in page]
        has_more = len(page_dirs) > limit
        next_cursor = runs[-1]["run_id"] if has_more and runs else None
        data = {"runs": runs, "count": len(runs), "total_count": len(run_dirs)}
        meta_extra = {
            "pagination": {"limit": limit, "cursor": cursor, "next_cursor": next_cursor, "has_more": has_more},
            "truncated": {"by_limit": has_more, "omitted": max(len(page_dirs) - len(page), 0)},
        }
        commands = []
        if has_more and next_cursor is not None:
            commands.append({"command": f"evalctl jobs list --limit {limit} --cursor {shlex.quote(next_cursor)} --json", "rationale": "Fetch the next page of runs."})
        human = "\n".join(f"{r['run_id']}\t{r['state']}" for r in runs)
        if has_more and sys.stdout.isatty() and next_cursor is not None:
            human = (human + "\n" if human else "") + f"Next page: evalctl jobs list --limit {limit} --cursor {shlex.quote(next_cursor)}"
        return print_envelope(data, json_mode=json_mode, human=human, commands=commands, started=started, meta_extra=meta_extra)
    if subcommand == "get":
        if len(args) != 3:
            raise EvalctlError("E_CASE_INVALID", "jobs get requires a run id", "try: evalctl jobs get <run-id> --json", 1)
        run_dir = root / args[2]
        if not run_dir.exists():
            raise EvalctlError("E_RUN_NOT_FOUND", f"run not found: {args[2]}", "try: evalctl jobs list --json", 1)
        data = classify_run_dir(run_dir)
        return print_envelope(data, json_mode=json_mode, human=f"{data['run_id']}: {data['state']}", started=started)
    if subcommand == "prune":
        confirmed = parsed_bool(parsed, "--yes") or parsed_bool(parsed, "--force")
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
    raise unknown_subcommand_error("jobs", subcommand, argv)


def parse_replay_source(argv: list[str]) -> Path:
    parsed = parse_command_args(argv, COMMAND_SPECS["replay"])
    args = list(parsed.positionals)
    if not parsed_bool(parsed, "--failed"):
        raise EvalctlError("E_CASE_INVALID", "replay requires --failed in v0.2", "try: evalctl replay --failed <run-id> --json", 1)
    run_dir = parsed_value(parsed, "--run-dir")
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
    parsed = parse_command_args(argv, COMMAND_SPECS["replay"])
    source_run = parse_replay_source(argv)
    source_manifest = read_json(source_run / "manifest.json")
    source_report = report_data(source_run)
    failed_ids = [case["id"] for case in source_report["cases"] if case["status"] != "pass"]
    if not failed_ids:
        warnings = [{"code": "W_NOTHING_TO_REPLAY", "message": "source run has no failed or errored cases"}]
        data = {"replayed_from": source_manifest["run_id"], "cases_replayed": 0}
        return print_envelope(data, json_mode=json_mode, human=f"{source_manifest['run_id']}: nothing to replay", warnings=warnings, started=started)

    suite_arg = parsed_value(parsed, "--suite", source_manifest["suite"]["name"])
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

    jobs = int(parsed_value(parsed, "--jobs", min(os.cpu_count() or 1, 4)))
    timeout_override = parsed_value(parsed, "--timeout")
    run_id = parsed_value(parsed, "--run-id", f"{source_manifest['run_id']}-replay-{datetime.now(timezone.utc).strftime('%Y-%m-%dT%H%M%SZ')}")
    run_dir = Path("evals") / "runs" / run_id
    if run_dir.resolve() == source_run.resolve():
        raise EvalctlError("E_RUN_CONFLICT", "replay destination must not be the source run", "use a fresh --run-id", 5)
    if run_dir.exists():
        if (run_dir / "manifest.json").exists():
            if not parsed_bool(parsed, "--force"):
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
    return 6 if parsed_bool(parsed, "--fail-on-fail") and not run_ok else 0


def resolve_run(argv: list[str]) -> Path:
    spec = COMMAND_SPECS["report"] if argv and argv[0] == "report" else COMMAND_SPECS["status"]
    parsed = parse_command_args(argv, spec)
    run_dir = parsed_value(parsed, "--run-dir")
    if run_dir is not None:
        path = Path(run_dir)
    else:
        args = list(parsed.positionals)
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
    parsed = parse_command_args(argv, COMMAND_SPECS["status"])
    if len(parsed.positionals) > 2:
        raise EvalctlError("E_CASE_INVALID", "status accepts at most one run positional", "try: evalctl status <run-id> --json", 1)
    run_dir = resolve_run(argv)
    manifest_doc = read_json(run_dir / "manifest.json")
    report = report_data(run_dir)
    data = {"run_id": manifest_doc["run_id"], "run_dir": str(run_dir), "run": report["run"], "cases": report["cases"], "recommended_action": {"command": f"evalctl report --run-dir {run_dir} --format json", "rationale": "inspect deterministic report and ranked failures", "alternatives": []}}
    return print_envelope(data, json_mode=json_mode, human=f"{manifest_doc['run_id']}: {'pass' if report['run']['ok'] else 'fail'}", started=started)


def command_report(argv: list[str], json_mode: bool, started: float) -> int:
    parsed = parse_command_args(argv, COMMAND_SPECS["report"])
    if len(parsed.positionals) > 2:
        raise EvalctlError("E_CASE_INVALID", "report accepts at most one run positional", "try: evalctl report <run-id> --format json", 1)
    run_dir = resolve_run(argv)
    data = report_data(run_dir)
    fmt = parsed_value(parsed, "--format", "json" if json_mode else "markdown")
    if fmt == "markdown" and not has_flag(argv, "--json"):
        print(markdown_report(data), end="")
        return 0
    if fmt not in {"json", "markdown"}:
        raise EvalctlError("E_CASE_INVALID", f"--format must be markdown or json (got {fmt})", "try: evalctl report <run-id> --format json", 1)
    commands = [{"command": f"evalctl report --run-dir {run_dir} --format json", "rationale": "regenerate this report"}]
    return print_envelope(data, json_mode=True, commands=commands, started=started)


def report_format_json_requested(argv: list[str]) -> bool:
    if not argv or argv[0] != "report":
        return False
    for index, token in enumerate(argv):
        if token == "--format" and index + 1 < len(argv):
            return argv[index + 1] == "json"
        if token == "--format=json":
            return True
    return False


def main(argv: list[str] | None = None) -> int:
    started = time.time()
    argv = list(sys.argv[1:] if argv is None else argv)
    json_mode = wants_json(argv)
    try:
        if not argv or argv[0] in {"--help", "-h"}:
            return command_help(argv or ["--help"], json_mode, started)
        if argv[0] == "--version":
            return command_version(argv, json_mode, started)
        cmd = argv[0]
        if cmd == "capabilities":
            return command_capabilities(argv, json_mode, started)
        if cmd == "schema":
            return command_schema(argv, json_mode, started)
        if cmd == "robot-docs":
            return command_robot_docs(argv, json_mode, started)
        if cmd == "init":
            return command_init(argv, json_mode, started)
        if cmd == "validate":
            return command_validate(argv, json_mode, started)
        if cmd == "doctor":
            return command_doctor(argv, json_mode, started)
        if cmd == "plan":
            return command_plan(argv, json_mode, started)
        if cmd == "suite":
            if len(argv) > 1 and argv[1] == "add":
                return command_suite_add(argv, json_mode, started)
            raise unknown_subcommand_error("suite", argv[1] if len(argv) > 1 else None, argv)
        if cmd == "case":
            if len(argv) > 1 and argv[1] == "add":
                return command_case_add(argv, json_mode, started)
            raise unknown_subcommand_error("case", argv[1] if len(argv) > 1 else None, argv)
        if cmd == "scorer":
            if len(argv) > 1 and argv[1] == "add":
                return command_scorer_add(argv, json_mode, started)
            raise unknown_subcommand_error("scorer", argv[1] if len(argv) > 1 else None, argv)
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
        raise unknown_command_error(cmd, argv)
    except EvalctlError as exc:
        return print_error(exc, json_mode=json_mode or report_format_json_requested(argv), started=started)
    except KeyboardInterrupt:
        return print_error(EvalctlError("E_RUNNER_FAILED", "interrupted", "retry the command", 3), json_mode=json_mode or report_format_json_requested(argv), started=started)
    except Exception as exc:
        return print_error(EvalctlError("E_RUNNER_FAILED", f"internal error: {exc}", "run with --json and inspect errors[0]", 3), json_mode=json_mode or report_format_json_requested(argv), started=started)


if __name__ == "__main__":
    raise SystemExit(main())
