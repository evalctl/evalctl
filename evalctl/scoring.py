from __future__ import annotations

import fnmatch
import json
import math
import os
import re
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .artifacts import apply_redaction, json_text, read_json, write_json
from .processes import run_process
from .redaction import (VERDICT_MAX_SERIALIZED_BYTES, VerdictLimitError,
                        VerdictParseError, parse_bounded_json, redact_json)
from .static_contract import BUILTIN_SCORERS, DEFAULT_COMMAND_SCORER_TIMEOUT_SECONDS, SAFE_ID_RE, sha256_text, stable_json


@dataclass(frozen=True)
class BoundedCapture:
    stdout: str
    stderr: str
    stdout_overflow: bool
    stderr_overflow: bool
    timed_out: bool
    spawn_failed: bool
    exit_code: int | None
    duration_ms: int


def run_bounded_command(command: str | list[str], *, shell: bool, cwd: Path, env: dict[str, str],
                        timeout: float, max_bytes: int, kill_drain_timeout: float = 2.0) -> BoundedCapture:
    """Run a command scorer while retaining at most max_bytes from each stream."""
    started = time.time()
    try:
        process = subprocess.Popen(
            command, shell=shell, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
        )
    except OSError as exc:
        return BoundedCapture("", str(exc), False, False, False, True, None,
                              int((time.time() - started) * 1000))

    stdout_data = bytearray()
    stderr_data = bytearray()
    stdout_overflow = threading.Event()
    stderr_overflow = threading.Event()
    killed = threading.Event()
    lock = threading.Lock()

    def kill_group() -> None:
        if killed.is_set():
            return
        killed.set()
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        except ProcessLookupError:
            pass

    def drain(stream: Any, retained: bytearray, overflow: threading.Event, *, kills_on_overflow: bool) -> None:
        while True:
            chunk = stream.read(64 * 1024)
            if not chunk:
                return
            with lock:
                remaining = max_bytes - len(retained)
                if remaining > 0:
                    retained.extend(chunk[:remaining])
                if len(chunk) > max(remaining, 0):
                    overflow.set()
                    if kills_on_overflow:
                        kill_group()

    stdout_thread = threading.Thread(target=drain, args=(process.stdout, stdout_data, stdout_overflow),
                                     kwargs={"kills_on_overflow": True}, daemon=True)
    stderr_thread = threading.Thread(target=drain, args=(process.stderr, stderr_data, stderr_overflow),
                                     kwargs={"kills_on_overflow": False}, daemon=True)
    stdout_thread.start()
    stderr_thread.start()
    timed_out = False
    while process.poll() is None and not stdout_overflow.is_set():
        if time.time() - started >= timeout:
            timed_out = True
            kill_group()
            break
        time.sleep(0.005)
    if stdout_overflow.is_set():
        kill_group()
    try:
        process.wait(timeout=kill_drain_timeout if killed.is_set() else max(timeout, 0.1))
    except subprocess.TimeoutExpired:
        kill_group()
        process.wait()
    join_timeout = kill_drain_timeout if killed.is_set() else max(timeout, 0.1)
    stdout_thread.join(join_timeout)
    stderr_thread.join(join_timeout)
    try:
        process.stdout.close()
        process.stderr.close()
    except OSError:
        pass
    return BoundedCapture(
        bytes(stdout_data).decode("utf-8", "replace"), bytes(stderr_data).decode("utf-8", "replace"),
        stdout_overflow.is_set(), stderr_overflow.is_set(), timed_out, False, process.returncode,
        int((time.time() - started) * 1000),
    )


def _render_runner_arg(arg: str, env: dict[str, str]) -> str:
    for key, value in env.items():
        arg = arg.replace(f"${key}", value)
    return arg


def _is_safe_id(value: str) -> bool:
    return bool(value) and not value.startswith("-") and value not in {".", ".."} and ".." not in value and bool(SAFE_ID_RE.fullmatch(value))


def score_summary(score: dict[str, Any]) -> dict[str, Any]:
    out = {"scorer": score["scorer"], "ok": score["ok"], "score": score["score"]}
    if "id" in score:
        out["id"] = score["id"]
    return out


def case_manifest_entry(case: dict[str, Any], status: str, scores: list[dict[str, Any]], *,
                        provenance: dict[str, Any] | None = None) -> dict[str, Any]:
    entry = {
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
    if provenance is not None:
        entry["provenance"] = provenance
        artifacts = entry["artifacts"]
        inferctl = provenance.get("inferctl") if isinstance(provenance, dict) else None
        if isinstance(inferctl, dict):
            if inferctl.get("preflight_artifact"):
                artifacts["inferctl_preflight"] = inferctl["preflight_artifact"]
            if inferctl.get("provenance_artifact"):
                artifacts["inferctl_provenance"] = inferctl["provenance_artifact"]
            if inferctl.get("error_artifact"):
                artifacts["inferctl_error"] = inferctl["error_artifact"]
    return entry


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


def scorer_limit_failure(scorer: dict[str, Any], required: bool, limit: str) -> dict[str, Any]:
    result = scorer_failure(scorer, required, f"verdict exceeded {limit} limit; see run artifacts")
    result["verdict_truncated"] = True
    return result


def prepare_command_verdict(result: dict[str, Any], scorer: dict[str, Any], required: bool,
                            patterns: list[str], env_values: list[str], max_bytes: int,
                            stdout_capture_truncated: bool = False,
                            stderr_capture_truncated: bool = False) -> tuple[dict[str, Any], str]:
    result, verdict_redacted = redact_json(result, patterns, env_values, max_bytes)
    result["diagnostics_redaction_version"] = 1
    result["stdout_capture_truncated"] = stdout_capture_truncated
    result["stderr_capture_truncated"] = stderr_capture_truncated
    if verdict_redacted:
        result["redacted"] = True
    serialized = json_text(result)
    if len(serialized.encode("utf-8")) > VERDICT_MAX_SERIALIZED_BYTES:
        result = scorer_limit_failure(scorer, required, "serialized-size")
        result, verdict_redacted = redact_json(result, patterns, env_values, max_bytes)
        result["diagnostics_redaction_version"] = 1
        result["stdout_capture_truncated"] = stdout_capture_truncated
        result["stderr_capture_truncated"] = stderr_capture_truncated
        if verdict_redacted:
            result["redacted"] = True
        serialized = json_text(result)
    return result, serialized


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
    if not _is_safe_id(scorer_id):
        return scorer_failure(scorer, required, "command scorer id must be path-safe")
    if case_dir is None:
        return scorer_failure(scorer, required, "command scorer requires case_dir")
    scorers_dir = case_dir / "scorers"
    verdict_path = scorers_dir / f"{scorer_id}.json"
    if not execute:
        verdict = read_json(verdict_path)
        if not isinstance(verdict, dict):
            return scorer_failure(scorer, required, "command scorer verdict artifact must be an object")
        verdict.setdefault("stdout_capture_truncated", False)
        verdict.setdefault("stderr_capture_truncated", False)
        return verdict
    scorers_dir.mkdir(parents=True, exist_ok=True)
    timeout = int(scorer.get("timeout_seconds") or DEFAULT_COMMAND_SCORER_TIMEOUT_SECONDS)
    runner = suite.get("runner", {})
    max_bytes = int(scorer.get("max_output_bytes") or runner.get("max_output_bytes") or 5 * 1024 * 1024)
    env = {k: os.environ[k] for k in runner.get("env_allowlist", []) if k in os.environ}
    env.update(eval_env)
    env_values = [os.environ.get(k, "") for k in runner.get("redact_env_values", [])]
    patterns = runner.get("redact_patterns", [])
    if scorer.get("shell", False):
        command = scorer.get("command")
        if not isinstance(command, str) or not command:
            result = scorer_failure(scorer, required, "command scorer shell mode requires command")
            result, serialized = prepare_command_verdict(result, scorer, required, patterns, env_values, max_bytes)
            write_json(verdict_path, result, serialized=serialized)
            return result
        cmd: str | list[str] = _render_runner_arg(command, eval_env)
        shell = True
    else:
        argv_raw = scorer.get("argv")
        if not isinstance(argv_raw, list) or not argv_raw:
            result = scorer_failure(scorer, required, "command scorer requires argv when shell:false")
            result, serialized = prepare_command_verdict(result, scorer, required, patterns, env_values, max_bytes)
            write_json(verdict_path, result, serialized=serialized)
            return result
        cmd = [_render_runner_arg(str(a), eval_env) for a in argv_raw]
        shell = False
    process_result = run_bounded_command(cmd, shell=shell, cwd=case_dir / "workspace", env=env,
                                         timeout=timeout, max_bytes=max_bytes)
    stdout = process_result.stdout
    stderr = process_result.stderr
    if process_result.spawn_failed:
        result = scorer_failure(scorer, required, f"command scorer spawn failed: {stderr}")
    elif process_result.stdout_overflow:
        result = scorer_limit_failure(scorer, required, "stdout-bytes")
    elif process_result.timed_out:
        result = scorer_failure(scorer, required, f"command scorer timed out after {timeout}s")
    elif process_result.exit_code != 0:
        result = scorer_failure(scorer, required, f"command scorer exited {process_result.exit_code}")
    else:
        try:
            raw = parse_bounded_json(stdout.strip())
        except VerdictLimitError as exc:
            result = scorer_limit_failure(scorer, required, exc.limit)
        except VerdictParseError as exc:
            result = scorer_failure(scorer, required, f"invalid command scorer JSON: {exc}")
        else:
            result = normalize_command_verdict(raw, scorer, required)
    stdout, _ = apply_redaction(stdout, patterns, env_values)
    stderr, _ = apply_redaction(stderr, patterns, env_values)
    stdout = stdout.encode()[:max_bytes].decode("utf-8", "replace")
    stderr = stderr.encode()[:max_bytes].decode("utf-8", "replace")
    (scorers_dir / f"{scorer_id}.stdout.txt").write_text(stdout)
    (scorers_dir / f"{scorer_id}.stderr.txt").write_text(stderr)
    result, serialized = prepare_command_verdict(
        result, scorer, required, patterns, env_values, max_bytes,
        stdout_capture_truncated=process_result.stdout_overflow,
        stderr_capture_truncated=process_result.stderr_overflow,
    )
    write_json(verdict_path, result, serialized=serialized)
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
