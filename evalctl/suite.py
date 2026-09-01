from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any

from .artifacts import _atomic_write, load_cases, read_json, write_json
from .static_contract import EvalctlError, SAFE_ID_RE, sha256_text, stable_json


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


def suite_add_data(name: str, runner: dict[str, Any], *, _validator: Any = validate_suite) -> dict[str, Any]:
    validate_suite_name(name)
    root = Path("evals") / "suites"
    dest = root / name
    suite_json = {
        "name": name,
        "cases": "cases.jsonl",
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
