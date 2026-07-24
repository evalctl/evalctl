from __future__ import annotations

import difflib
import json
import os
import shlex
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import __version__

from .static_contract import (
    BOOL,
    COMMAND_SPECS,
    DEFAULT_JOBS_LIST_LIMIT,
    DEFAULT_RESERVATION_TTL_SECONDS,
    DOCTOR_COMPONENTS,
    GLOBAL_FLAG_SPECS,
    SUBCOMMANDS,
    TOOL,
    VERB_NAMES,
    CommandSpec,
    EvalctlError,
    FlagSpec,
    ParsedArgs,
    capabilities_data as _static_capabilities_data,
    envelope,
    help_text,
    robot_docs,
    schema_data,
    stable_json,
)
from .artifacts import (
    load_cases,
    read_json,
)
from .inferctl import (
    inferctl_binary,
    inferctl_capabilities,
    inferctl_run_context,
    inferctl_verb_names,
)
from .spoolctl import (
    probe_spoolctl,
)
from .run_state import (
    ReservationHeartbeat,
    build_run_metadata,
    classify_run_dir as _classify_run_dir,
    clear_reservation,
    finalize_run as _finalize_run,
    manifest_identity,
    read_reservation,
    read_run_metadata,
    reservation_is_live,
    run_identity,
    runs_root,
    split_completed_and_pending,
    timeout_seconds_for_run,
    write_run_metadata_once,
)
from .runner import (
    execute_pending_cases,
    execute_spoolctl_pending_cases,
)
from .suite import (
    case_add_data,
    init_project,
    is_safe_id,
    resolve_suite,
    scorer_add_data,
    suite_add_data,
    validate_suite,
)
from .doctor import doctor_data
from .reports import markdown_report, report_data


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


def parse_jobs(argv: list[str]) -> int:
    parsed = parse_command_args(argv, command_parser_spec(argv, "run"))
    return int(parsed_value(parsed, "--jobs", min(os.cpu_count() or 1, 4)))


def parse_positive_int_flag(argv: list[str], flag: str, default: int) -> int:
    parsed = parse_command_args(argv, command_parser_spec(argv, "run"))
    return int(parsed_value(parsed, flag, default))


def parse_jobs_list_limit(argv: list[str]) -> int:
    parsed = parse_command_args(argv, COMMAND_SPECS["jobs"])
    return int(parsed_value(parsed, "--limit", DEFAULT_JOBS_LIST_LIMIT))


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


def command_suite_add(argv: list[str], json_mode: bool, started: float) -> int:
    parsed = parse_command_args(argv, COMMAND_SPECS["suite"])
    args = list(parsed.positionals)
    if len(args) != 3 or args[1] != "add":
        raise EvalctlError("E_CASE_INVALID", "suite command requires: suite add <name>", "try: evalctl suite add demo --runner-argv \"python3 $EVALCTL_WORKSPACE/r.py\" --json", 1)
    data = suite_add_data(args[2], runner_from_authoring_flags(argv, parsed=parsed))
    return print_envelope(data, json_mode=json_mode, human=f"{'Created' if data['created'] else 'Exists'} suite {data['suite']}", started=started)


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


def command_scorer_add(argv: list[str], json_mode: bool, started: float) -> int:
    parsed = parse_command_args(argv, COMMAND_SPECS["scorer"])
    args = list(parsed.positionals)
    if len(args) != 3 or args[1] != "add":
        raise EvalctlError("E_CASE_INVALID", "scorer command requires: scorer add <suite>", "try: evalctl scorer add demo --name exact --required --json", 1)
    data = scorer_add_data(args[2], command_scorer_config(argv, parsed))
    label = data["id"] or data["scorer"]
    return print_envelope(data, json_mode=json_mode, human=f"{'Added' if data['created'] else 'Exists'} scorer {label}", started=started)


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
