from __future__ import annotations

import json
import shutil
import subprocess
import time
from typing import Any

from .artifacts import write_json
from .static_contract import EvalctlError


def _decode_subprocess_output(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return value.decode("utf-8", "replace")


def inferctl_binary() -> str | None:
    return shutil.which("inferctl")


def inferctl_capabilities(*, timeout: float | None = 3) -> dict[str, Any]:
    binary = inferctl_binary()
    if binary is None:
        raise EvalctlError("E_INFERCTL_UNAVAILABLE", "inferctl is not available on PATH", "install inferctl or omit inferctl provenance", 3)
    try:
        result = subprocess.run([binary, "capabilities", "--json"], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise EvalctlError("E_INFERCTL_INCOMPATIBLE", "inferctl capabilities timed out", "run evalctl doctor --fast or inspect inferctl separately", 3, timeout_seconds=timeout) from exc
    except OSError as exc:
        raise EvalctlError("E_INFERCTL_UNAVAILABLE", f"could not run inferctl: {exc}", "install inferctl or omit inferctl provenance", 3) from exc
    if result.returncode not in {0, 1, 3, 4, 5}:
        raise EvalctlError("E_INFERCTL_INCOMPATIBLE", f"inferctl capabilities failed: {result.stderr.strip() or result.stdout.strip()}", "inspect inferctl capabilities --json", 3)
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise EvalctlError("E_INFERCTL_INCOMPATIBLE", f"inferctl returned invalid JSON: {exc.msg}", "inspect inferctl capabilities --json", 3) from exc
    if isinstance(payload, dict) and payload.get("ok") is False:
        raise EvalctlError("E_INFERCTL_INCOMPATIBLE", "inferctl returned an error envelope", "inspect inferctl capabilities --json", 3)
    data = payload.get("data") if isinstance(payload, dict) and isinstance(payload.get("data"), dict) else payload
    if not isinstance(data, dict):
        raise EvalctlError("E_INFERCTL_INCOMPATIBLE", "inferctl capabilities data must be an object", "upgrade inferctl or omit inferctl provenance", 3)
    return data


def inferctl_verb_names(data: dict[str, Any]) -> set[str]:
    verbs = data.get("verbs", [])
    if not isinstance(verbs, list):
        raise EvalctlError("E_INFERCTL_INCOMPATIBLE", "inferctl capabilities verbs must be a list", "upgrade inferctl or omit inferctl provenance", 3)
    names = {item.get("name") for item in verbs if isinstance(item, dict)}
    return {name for name in names if isinstance(name, str)}


def inferctl_run_context(task: str | None) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if task is None:
        return {"requested": False, "task": None, "actual_mode": "none", "capture_modes": []}, []
    warnings: list[dict[str, Any]] = []
    base: dict[str, Any] = {"requested": True, "task": task, "actual_mode": "none", "capture_modes": []}
    try:
        capabilities = inferctl_capabilities(timeout=3)
        verbs = inferctl_verb_names(capabilities)
    except EvalctlError as exc:
        code = "W_INFERCTL_ABSENT" if exc.error["code"] == "E_INFERCTL_UNAVAILABLE" else "W_INFERCTL_INCOMPATIBLE"
        warnings.append({"code": code, "message": exc.error["message"]})
        return {**base, "available": False}, warnings
    if "preflight" not in verbs:
        warnings.append({"code": "W_INFERCTL_INCOMPATIBLE", "message": "inferctl is present but lacks preflight support"})
        return {**base, "available": True, "contract_version": capabilities.get("contract_version"), "verbs": sorted(verbs)}, warnings
    return {
        **base,
        "available": True,
        "actual_mode": "preflight",
        "capture_modes": ["preflight"],
        "contract_version": capabilities.get("contract_version"),
        "verbs": sorted(verbs),
        "route_available": "route" in verbs,
    }, warnings


def inferctl_meta(context: dict[str, Any]) -> dict[str, Any]:
    """Requested-vs-actual provenance pair for the run envelope's meta.

    Empty when no capture was requested. When a task was requested, the pair
    lets an agent detect a silent degradation (requested preflight, actual
    none) without parsing warning text.
    """
    if not context.get("requested"):
        return {}
    actual = context.get("actual_mode", "none")
    return {
        "inferctl": {
            "requested_mode": "preflight",
            "actual_mode": actual,
            "available": bool(context.get("available", False)),
            "degraded": actual != "preflight",
        }
    }


def inferctl_payload_data(payload: Any) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return None
    if payload.get("ok") is False:
        return payload.get("data") if isinstance(payload.get("data"), dict) else None
    data = payload.get("data")
    if isinstance(data, dict):
        return data
    return payload


def inferctl_warning_codes(data: dict[str, Any]) -> list[str]:
    codes: list[str] = []
    for item in data.get("warnings", []):
        if isinstance(item, dict) and item.get("code"):
            codes.append(str(item["code"]))
        elif isinstance(item, str):
            codes.append(item)
    return codes


def inferctl_error_codes(payload: Any) -> list[str]:
    if not isinstance(payload, dict):
        return []
    codes: list[str] = []
    for item in payload.get("errors", []):
        if isinstance(item, dict) and item.get("code"):
            codes.append(str(item["code"]))
    return codes


def inferctl_preflight_summary(payload: Any, returncode: int) -> tuple[dict[str, Any], bool]:
    data = inferctl_payload_data(payload) or {}
    runnability = data.get("runnability") if isinstance(data.get("runnability"), dict) else {}
    route = data.get("route") if isinstance(data.get("route"), dict) else {}
    decision = data.get("route_decision")
    if not isinstance(decision, dict):
        decision = route.get("decision") if isinstance(route.get("decision"), dict) else {}
    runnable = data.get("runnable", runnability.get("runnable"))
    status = data.get("runnability_status", runnability.get("status"))
    blocked = runnable is False or (status is not None and status not in {"runnable", "ready"})
    summary = {
        "returncode": returncode,
        "runnable": runnable,
        "runnability_status": status,
        "ready": decision.get("ready"),
        "selected_backend": decision.get("selected_backend"),
        "selected_model": decision.get("selected_model"),
        "fallback_selected": decision.get("is_fallback"),
        "warning_codes": inferctl_warning_codes(data),
        "error_codes": inferctl_error_codes(payload),
    }
    return summary, blocked


def capture_inferctl_preflight(prepared: dict[str, Any], inferctl_context: dict[str, Any]) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    if not inferctl_context.get("requested"):
        return None, []
    case_dir = prepared["case_dir"]
    case_rel = f"cases/{prepared['case']['id']}"
    provenance: dict[str, Any] = {
        "requested": True,
        "task": inferctl_context.get("task"),
        "actual_mode": inferctl_context.get("actual_mode", "none"),
        "capture_modes": inferctl_context.get("capture_modes", []),
        "available": inferctl_context.get("available", False),
    }
    if inferctl_context.get("actual_mode") != "preflight":
        return provenance, []

    binary = inferctl_binary()
    if binary is None:
        provenance["actual_mode"] = "none"
        write_json(case_dir / "inferctl-provenance.json", provenance)
        provenance["provenance_artifact"] = f"{case_rel}/inferctl-provenance.json"
        write_json(case_dir / "inferctl-provenance.json", provenance)
        return provenance, [{"code": "W_INFERCTL_ABSENT", "message": "inferctl is not available on PATH"}]

    args = [binary, "preflight", str(inferctl_context["task"]), "--prompt-file", str(prepared["task_txt"]), "--allow-fallback", "--json"]
    started = time.time()
    try:
        result = subprocess.run(args, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10)
        duration_ms = int((time.time() - started) * 1000)
        payload = json.loads(result.stdout)
        write_json(case_dir / "inferctl-preflight.json", payload)
        summary, blocked = inferctl_preflight_summary(payload, result.returncode)
        provenance.update(summary)
        provenance.update({
            "preflight_artifact": f"{case_rel}/inferctl-preflight.json",
            "provenance_artifact": f"{case_rel}/inferctl-provenance.json",
            "duration_ms": duration_ms,
        })
        warnings = [{"code": "W_INFERCTL_PREFLIGHT_BLOCKED", "message": "inferctl preflight reported the case is not runnable"}] if blocked else []
    except subprocess.TimeoutExpired as exc:
        error = {"code": "W_INFERCTL_CAPTURE_FAILED", "message": "inferctl preflight timed out", "timeout_seconds": 10, "stdout": _decode_subprocess_output(exc.stdout), "stderr": _decode_subprocess_output(exc.stderr)}
        write_json(case_dir / "inferctl-error.json", error)
        provenance.update({"error_code": error["code"], "error_artifact": f"{case_rel}/inferctl-error.json", "provenance_artifact": f"{case_rel}/inferctl-provenance.json"})
        warnings = [{"code": "W_INFERCTL_CAPTURE_FAILED", "message": "inferctl preflight capture failed"}]
    except (OSError, json.JSONDecodeError) as exc:
        error = {"code": "W_INFERCTL_CAPTURE_FAILED", "message": f"inferctl preflight capture failed: {exc}"}
        write_json(case_dir / "inferctl-error.json", error)
        provenance.update({"error_code": error["code"], "error_artifact": f"{case_rel}/inferctl-error.json", "provenance_artifact": f"{case_rel}/inferctl-provenance.json"})
        warnings = [{"code": "W_INFERCTL_CAPTURE_FAILED", "message": "inferctl preflight capture failed"}]
    except Exception as exc:
        error = {"code": "W_INFERCTL_CAPTURE_FAILED", "message": f"unexpected inferctl preflight capture failure: {exc}"}
        write_json(case_dir / "inferctl-error.json", error)
        provenance.update({"error_code": error["code"], "error_artifact": f"{case_rel}/inferctl-error.json", "provenance_artifact": f"{case_rel}/inferctl-provenance.json"})
        warnings = [{"code": "W_INFERCTL_CAPTURE_FAILED", "message": "inferctl preflight capture failed"}]

    write_json(case_dir / "inferctl-provenance.json", provenance)
    return provenance, warnings
