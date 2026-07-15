from __future__ import annotations

import concurrent.futures
import fnmatch
import hashlib
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from . import __version__

CONTRACT_VERSION = 1
TOOL = "evalctl"

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
    "E_RUN_NOT_FOUND": {"class": "user-input", "exit": 1, "where": ["status", "report"], "retryable": False, "surface": "envelope"},
    "E_RUNNER_FAILED": {"class": "tool-env", "exit": 3, "where": ["run"], "retryable": None, "surface": "runner_json"},
    "E_RUNNER_TIMEOUT": {"class": "tool-env", "exit": 3, "where": ["run"], "retryable": None, "surface": "runner_json"},
    "E_SCORER_FAILED": {"class": "tool-env", "exit": 3, "where": ["run", "report"], "retryable": None, "surface": "envelope"},
    "E_RUN_BUSY": {"class": "transient", "exit": 4, "where": ["run"], "retryable": True, "surface": "envelope"},
    "E_RUN_CONFLICT": {"class": "conflict", "exit": 5, "where": ["run", "init"], "retryable": False, "surface": "envelope"},
    "W_UNSANDBOXED_RUNNER": {"class": "warning", "where": ["run"], "surface": "envelope"},
    "W_TEXT_DIFF_APPROXIMATED": {"class": "warning", "where": ["run"], "surface": "envelope"},
    "W_OUTPUT_TRUNCATED": {"class": "warning", "where": ["run"], "surface": "envelope"},
    "W_PATH_UNREADABLE": {"class": "warning", "where": ["run"], "surface": "envelope"},
    "W_PARTIAL_RUN": {"class": "warning", "where": ["run", "report"], "surface": "envelope"},
}


class EvalctlError(Exception):
    def __init__(self, code: str, message: str, hint: str, exit_code: int = 1, **ctx: Any) -> None:
        super().__init__(message)
        self.error = {"code": code, "message": message, "hint": hint, "exit_code": exit_code, **ctx}
        self.exit_code = exit_code


def now_iso() -> str:
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
  run <suite> [--jobs N] [--timeout S] [--run-id ID] [--fail-on-fail] [--json]
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
    verbs = {
        "capabilities": {"description": "Return the machine contract.", "json": True, "mutates": False, "flags": ["--json"], "exit_codes": [0]},
        "schema": {"description": "Return output schemas.", "json": True, "mutates": False, "args": ["verb"], "flags": ["--json"], "exit_codes": [0, 1]},
        "robot-docs": {"description": "Return agent workflow guide.", "json": False, "mutates": False, "args": ["guide"], "exit_codes": [0, 1]},
        "init": {"description": "Scaffold evals/ tree with sample code-review suite.", "json": True, "mutates": True, "flags": ["--json", "--force"], "exit_codes": [0, 5]},
        "validate": {"description": "Validate suite.json, cases.jsonl, fixtures, scorer refs, and runner config.", "json": True, "mutates": False, "args": ["suite"], "flags": ["--json"], "exit_codes": [0, 1]},
        "run": {"description": "Run a suite synchronously and produce a portable run directory.", "json": True, "mutates": True, "args": ["suite"], "flags": ["--json", "--jobs", "--timeout", "--run-id", "--fail-on-fail"], "exit_codes": [0, 1, 3, 4, 5, 6]},
        "status": {"description": "Diagnose run state.", "json": True, "mutates": False, "args": ["run-id"], "flags": ["--json", "--run-dir"], "exit_codes": [0, 1]},
        "report": {"description": "Generate markdown or JSON report from run artifacts.", "json": True, "mutates": False, "args": ["run-id"], "flags": ["--json", "--format", "--run-dir"], "exit_codes": [0, 1, 3]},
    }
    return {
        "tool_name": TOOL,
        "contract_version": CONTRACT_VERSION,
        "features": ["universal_envelope", "deterministic_output", "artifact_replay", "workspace_diff"],
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
            "SOURCE_DATE_EPOCH": "used for deterministic timestamps where applicable",
        },
        "integrations": {
            "spoolctl": {"available": False, "planned": True},
            "inferctl": {"available": False, "planned": True},
        },
        "schemas_uri": "evalctl schema <verb> --json",
        "robot_docs_uri": "evalctl robot-docs guide",
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
    schemas = {name: {"type": "object", "additionalProperties": True} for name in capabilities_data()["verbs"]}
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

## v0.1 workflow

1. Scaffold `evals/suites/code-review/` with `evalctl init`.
2. Edit `suite.json`, `cases.jsonl`, and fixtures by hand.
3. Run `evalctl validate <suite> --json` before executing local code.
4. Run `evalctl run <suite> --json`. The runner is arbitrary local code; evalctl is not a sandbox.
5. Use `status` for run state and recommended next command.
6. Use `report --format json` for a deterministic report envelope or `--format markdown` for a human report.
7. Copy a run directory anywhere and run `report --run-dir <path> --format json`; v0.1 recomputes deterministic scorers from artifacts and does not invoke the runner.

## Exit-code branching

`0` success, `1` input error, `2` safety block, `3` tool environment error, `4` retryable transient, `5` conflict, `6` eval failure from `run --fail-on-fail`.

## Error-code surfaces

Codes with `surface:"envelope"` appear in `errors[]` or `warnings[]` and predict
the command's process-exit class. Codes with `surface:"runner_json"` appear as
per-case `runner.json.error_code` reason codes. A runner timeout or spawn failure
is reportable case data: `run` exits 0 by default, exits 6 with `--fail-on-fail`,
emits `W_PARTIAL_RUN`, and does not put the runner reason code in `errors[]`.

## Artifact writes

JSON artifacts are written by creating a temporary file in the target directory
and replacing the final path with `os.replace`. This guarantees atomic visibility:
readers never see a half-written final JSON file. It is not a full crash-durable
guarantee; v0.1.1 does not fsync files or directories.

## Deferred

Async queueing, crash resume, execution replay, compare, command scorers, mutating suite/case/scorer add verbs, inferctl route capture, and LLM-as-judge scoring are roadmap items, not v0.1 commands.
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
    return posix


def manifest(root: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    warnings: list[dict[str, Any]] = []
    entries: list[dict[str, Any]] = []
    for path in sorted(root.rglob("*"), key=lambda p: normalize_rel(p, root)):
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
            warnings.append({"code": "W_PATH_UNREADABLE", "message": f"could not read path {path.name}: {exc}"})
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


def render_runner_arg(arg: str, env: dict[str, str]) -> str:
    for key, value in env.items():
        arg = arg.replace(f"${key}", value)
    return arg


def decode_subprocess_output(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return value.decode("utf-8", "replace")


def case_manifest_entry(case: dict[str, Any], status: str, scores: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "id": case["id"],
        "input_hash": sha256_text(stable_json(case)),
        "status": status,
        "scores": [{"scorer": s["scorer"], "ok": s["ok"], "score": s["score"]} for s in scores],
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
    scores = score_case(case, "", runner_json, after, diff, suite.get("scorers", []))
    score_doc = {"case_id": case["id"], "status": "error", "ok": False, "scores": scores}
    write_json(case_dir / "score.json", score_doc)
    return case_manifest_entry(case, "error", scores), warnings


def run_case(suite_dir: Path, suite: dict[str, Any], case: dict[str, Any], run_dir: Path, timeout_override: int | None) -> tuple[dict[str, Any], list[dict[str, Any]]]:
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
    started = time.time()
    timed_out = False
    spawn_failed = False
    exit_code: int | None = None
    signal_value: int | None = None
    stdout = ""
    stderr = ""
    try:
        stdin = subprocess.PIPE if runner.get("stdin") == "task" else None
        input_text = case["task"] if runner.get("stdin") == "task" else None
        if runner.get("shell", False):
            cmd: str | list[str] = render_runner_arg(runner["command"], eval_env)
            proc = subprocess.Popen(cmd, shell=True, cwd=cwd, env=env, text=True, stdin=stdin,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        else:
            argv = [render_runner_arg(str(a), eval_env) for a in runner["argv"]]
            proc = subprocess.Popen(argv, shell=False, cwd=cwd, env=env, text=True, stdin=stdin,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        stdout, stderr = proc.communicate(input=input_text, timeout=timeout)
        exit_code = proc.returncode
        if proc.returncode < 0:
            signal_value = -proc.returncode
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        exit_code = None
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except ProcessLookupError:
            pass
        drained_stdout, drained_stderr = proc.communicate()
        stdout = decode_subprocess_output(exc.stdout) + decode_subprocess_output(drained_stdout)
        stderr = decode_subprocess_output(exc.stderr) + decode_subprocess_output(drained_stderr)
    except OSError as exc:
        spawn_failed = True
        exit_code = None
        stderr = str(exc)
    duration_ms = int((time.time() - started) * 1000)
    trunc_stdout = len(stdout.encode()) > max_bytes
    trunc_stderr = len(stderr.encode()) > max_bytes
    stdout = stdout.encode()[:max_bytes].decode("utf-8", "replace")
    stderr = stderr.encode()[:max_bytes].decode("utf-8", "replace")
    stdout, red_stdout = apply_redaction(stdout, patterns, env_values)
    stderr, red_stderr = apply_redaction(stderr, patterns, env_values)
    output_truncated = False
    if output_file.exists():
        output_bytes = output_file.read_bytes()
        output_truncated = len(output_bytes) > max_bytes
        output_text = output_bytes[:max_bytes].decode("utf-8", "replace")
    else:
        output_text = stdout
    output_text, red_output = apply_redaction(output_text, patterns, env_values)
    output_file.write_text(output_text)
    (case_dir / "runner.stdout.txt").write_text(stdout)
    (case_dir / "runner.stderr.txt").write_text(stderr)
    error_code = "E_RUNNER_TIMEOUT" if timed_out else "E_RUNNER_FAILED" if spawn_failed else None
    runner_json = {"exit_code": exit_code, "signal": signal_value, "timed_out": timed_out, "spawn_failed": spawn_failed, "error_code": error_code, "duration_ms": duration_ms,
                   "stdout_truncated": trunc_stdout, "stderr_truncated": trunc_stderr, "output_truncated": output_truncated,
                   "stdout_redacted": red_stdout, "stderr_redacted": red_stderr, "output_redacted": red_output}
    if trunc_stdout or trunc_stderr or output_truncated:
        warnings.append({"code": "W_OUTPUT_TRUNCATED", "message": "runner output exceeded max_output_bytes"})
    write_json(case_dir / "runner.json", runner_json)
    after, mw = manifest(workspace)
    warnings.extend(mw)
    diff = diff_manifests(before, after)
    write_json(case_dir / "workspace-after.json", after)
    write_json(case_dir / "workspace-diff.json", diff)
    (case_dir / "workspace.diff").write_text(render_text_diff(diff))
    status = "error" if timed_out or spawn_failed else "pass"
    scores = score_case(case, output_text, runner_json, after, diff, suite.get("scorers", []))
    if any(s.get("error") for s in scores):
        status = "error"
    elif status != "error" and not all(s["ok"] for s in scores if s.get("required", True)):
        status = "fail"
    score_doc = {"case_id": case["id"], "status": status, "ok": status == "pass", "scores": scores}
    write_json(case_dir / "score.json", score_doc)
    return case_manifest_entry(case, status, scores), warnings


def render_text_diff(diff: dict[str, Any]) -> str:
    return "\n".join(f"{p['status']}\t{p['path']}" for p in diff["changed_paths"]) + ("\n" if diff["changed_paths"] else "")


def score_case(case: dict[str, Any], output_text: str, runner_json: dict[str, Any], after: dict[str, Any], diff: dict[str, Any], scorers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    expect = case.get("expect", {})
    configured = scorers or [{"name": n, "required": True} for n in ("contains", "regex", "exact", "json-schema", "numeric-threshold", "file-exists", "exit-code", "workspace-diff")]
    out: list[dict[str, Any]] = []
    for scorer in configured:
        name = scorer["name"]
        required = bool(scorer.get("required", True))
        try:
            result = run_scorer(name, expect, output_text, runner_json, after, diff)
            if result is None:
                continue
            result["required"] = required
            out.append(result)
        except Exception as exc:
            out.append({"scorer": name, "ok": False, "score": 0.0, "label": "error", "required": required, "findings": [{"why": str(exc)}], "error": True})
    return out


def run_scorer(name: str, expect: dict[str, Any], output_text: str, runner_json: dict[str, Any], after: dict[str, Any], diff: dict[str, Any]) -> dict[str, Any] | None:
    findings: list[dict[str, Any]] = []
    if name == "exact":
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


def command_run(argv: list[str], json_mode: bool, started: float) -> int:
    args = strip_flags(argv, {"--jobs", "--timeout", "--run-id"}, {"--json", "--no-color", "--fail-on-fail"})
    if len(args) < 2:
        raise EvalctlError("E_SUITE_NOT_FOUND", "run requires a suite name", "try: evalctl run code-review --json", 1)
    suite_dir = resolve_suite(args[1])
    validate_suite(suite_dir)
    suite = read_json(suite_dir / "suite.json")
    cases = load_cases(suite_dir / suite.get("cases", "cases.jsonl"))
    jobs = parse_jobs(argv)
    all_warnings = [{"code": "W_UNSANDBOXED_RUNNER", "message": "runner commands execute arbitrary local code; evalctl is not a sandbox"}]
    if not suite.get("acknowledged_unsandboxed_runner") and sys.stderr.isatty():
        print(all_warnings[0]["message"], file=sys.stderr)
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
            return print_envelope(existing, json_mode=json_mode, warnings=all_warnings, started=started)
        raise EvalctlError("E_RUN_BUSY", f"run reservation exists for {run_id}", "wait and retry evalctl run with a new --run-id", 4)
    run_dir.mkdir(parents=True)
    shutil.copytree(suite_dir, run_dir / "suite-snapshot")
    timeout_override = int(value_after(argv, "--timeout")) if value_after(argv, "--timeout") else None
    case_results: dict[str, tuple[dict[str, Any], list[dict[str, Any]]]] = {}
    sorted_cases = sorted(cases, key=lambda c: c["id"])
    with concurrent.futures.ThreadPoolExecutor(max_workers=jobs) as executor:
        future_to_case = {executor.submit(run_case, suite_dir, suite, case, run_dir, timeout_override): case for case in sorted_cases}
        for future in concurrent.futures.as_completed(future_to_case):
            case = future_to_case[future]
            try:
                entry, warnings = future.result()
            except Exception as exc:
                try:
                    entry, warnings = synthesize_case_error(suite_dir, suite, case, run_dir, exc)
                except Exception as synth_exc:
                    if not (run_dir / "manifest.json").exists():
                        shutil.rmtree(run_dir, ignore_errors=True)
                    raise EvalctlError("E_SCORER_FAILED", f"case {case['id']} failed before replayable artifacts could be written: {synth_exc}", "retry evalctl run with a fresh --run-id", 3)
            case_results[case["id"]] = (entry, warnings)
    case_entries = []
    for case in sorted_cases:
        entry, warnings = case_results[case["id"]]
        all_warnings.extend(warnings)
        case_entries.append(entry)
    if any(c["status"] == "error" for c in case_entries):
        all_warnings.append({"code": "W_PARTIAL_RUN", "message": "some cases errored; report remains generable"})
    run_ok = all(c["status"] == "pass" for c in case_entries)
    manifest_doc = {
        "schema_version": 1,
        "run_id": run_id,
        "suite": {"name": suite.get("name", suite_dir.name), "hash": sha256_text(stable_json(suite)), "case_count": len(cases)},
        "created_ts": now_iso(),
        "execution": {"mode": "synchronous", "jobs": jobs, "timeout_seconds": timeout_override or suite["runner"].get("timeout_seconds", 300)},
        "replayed_from": None,
        "cases": case_entries,
    }
    write_json(run_dir / "manifest.json", manifest_doc)
    report = report_data(run_dir)
    (run_dir / "report.md").write_text(markdown_report(report))
    data = {"run_id": run_id, "run_dir": str(run_dir), "run": {"ok": run_ok, "case_count": len(cases), "status_counts": status_counts(case_entries)}, "report_hash": report["report_hash"]}
    commands = [{"command": f"evalctl report {run_id} --format json", "rationale": "regenerate deterministic JSON report"}]
    print_envelope(data, json_mode=json_mode, human=f"Run {run_id}: {'pass' if run_ok else 'fail'}", warnings=all_warnings, commands=commands, started=started)
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
    scores = score_case(case, output, runner_json, after, diff, suite.get("scorers", []))
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
    normalized = {"run": {"ok": not failures, "suite": manifest_doc["suite"]["name"], "case_count": len(cases), "status_counts": status_counts(cases)}, "failures": [{"id": f["id"], "status": f["status"], "scores": [{"scorer": s["scorer"], "ok": s["ok"], "label": s["label"], "findings": s["findings"]} for s in f["scores"]]} for f in failures], "cases": [{"id": c["id"], "status": c["status"], "ok": c["ok"]} for c in cases]}
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
        if cmd == "run":
            return command_run(argv, json_mode, started)
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
