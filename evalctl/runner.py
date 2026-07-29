from __future__ import annotations

import concurrent.futures
import math
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from .artifacts import (
    apply_redaction,
    diff_manifests,
    manifest,
    read_json,
    render_text_diff,
    write_json,
)
from .inferctl import capture_inferctl_preflight
from .integration_contracts import SPOOLCTL_UPGRADE_HINT
from .processes import run_process
from .run_state import (
    clean_pending_case_dirs,
    terminal_marker_count,
    write_terminal_marker,
)
from .scoring import case_manifest_entry, score_case
from .spoolctl import spoolctl_binary, spoolctl_json
from .static_contract import EvalctlError


def render_runner_arg(arg: str, env: dict[str, str]) -> str:
    for key, value in env.items():
        arg = arg.replace(f"${key}", value)
    return arg


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
        raise EvalctlError("E_SPOOLCTL_INCOMPATIBLE", "spoolctl add did not return data.job_id", SPOOLCTL_UPGRADE_HINT, 3)
    write_json(prepared["case_dir"] / "job.json", {"job_id": job_id, "state": data.get("state", "queued")})
    return job_id


def latest_terminal_attempt(job_detail: dict[str, Any]) -> dict[str, Any]:
    attempts = job_detail.get("attempts")
    if not isinstance(attempts, list) or not attempts:
        raise EvalctlError("E_SPOOLCTL_INCOMPATIBLE", "spoolctl show did not include attempts", SPOOLCTL_UPGRADE_HINT, 3)
    return attempts[-1]


def spoolctl_attempt_duration_ms(attempt: dict[str, Any]) -> int:
    """Derive a queued attempt's duration from its timestamps.

    Real spoolctl emits started_at and finished_at as epoch-second floats and
    has never emitted duration_ms. Reading duration_ms recorded 0 on every
    queued case for two releases, so there is deliberately no fallback to it.
    """
    started = attempt.get("started_at")
    finished = attempt.get("finished_at")
    usable = all(isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
                 for value in (started, finished))
    return max(0, round((finished - started) * 1000)) if usable else 0


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
        "duration_ms": spoolctl_attempt_duration_ms(attempt),
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
