from __future__ import annotations

import json
import re
import shutil
import subprocess
from typing import Any

from .static_contract import EvalctlError


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


def run_spoolctl_json(args: list[str], *, allow_exit_codes: set[int] | None = None, timeout: float | None = None) -> tuple[int, Any]:
    allow_exit_codes = allow_exit_codes or {0}
    try:
        result = subprocess.run([spoolctl_binary(), *args], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        stderr = exc.stderr if isinstance(exc.stderr, str) else (exc.stderr or b"").decode("utf-8", "replace")
        stdout = exc.stdout if isinstance(exc.stdout, str) else (exc.stdout or b"").decode("utf-8", "replace")
        detail = (stderr or stdout or "timed out").strip()
        raise EvalctlError("E_SPOOLCTL_INCOMPATIBLE", f"spoolctl operation timed out: {detail}", "upgrade, restart, or bypass spoolctl", 3, timeout_seconds=timeout)
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
    return result.returncode, payload


def spoolctl_json(args: list[str], *, allow_exit_codes: set[int] | None = None, timeout: float | None = None) -> dict[str, Any]:
    returncode, payload = run_spoolctl_json(args, allow_exit_codes=allow_exit_codes, timeout=timeout)
    if isinstance(payload, dict) and "ok" in payload:
        if not payload.get("ok") and returncode != 6:
            raise EvalctlError("E_SPOOLCTL_INCOMPATIBLE", "spoolctl returned an error envelope", "inspect spoolctl output or drop --queue spoolctl", 3)
        data = payload.get("data")
        return data if isinstance(data, dict) else {"value": data}
    if isinstance(payload, dict):
        return payload
    raise EvalctlError("E_SPOOLCTL_INCOMPATIBLE", "spoolctl JSON output must be an object", "upgrade spoolctl to >= 0.4.1 or drop --queue spoolctl", 3)


def spoolctl_flag_names(flags: Any) -> set[str]:
    names: set[str] = set()
    if not isinstance(flags, list):
        return names
    for item in flags:
        if isinstance(item, str):
            names.add(item)
        elif isinstance(item, dict) and isinstance(item.get("flag"), str):
            names.add(item["flag"])
    return names


def probe_spoolctl(*, timeout: float | None = None) -> dict[str, Any]:
    _, payload = run_spoolctl_json(["capabilities", "--json"], timeout=timeout)
    if not isinstance(payload, dict):
        raise EvalctlError("E_SPOOLCTL_INCOMPATIBLE", "spoolctl capabilities output must be a JSON object", "upgrade spoolctl to >= 0.4.1", 3)
    is_envelope = "ok" in payload
    if is_envelope:
        if not payload.get("ok"):
            raise EvalctlError("E_SPOOLCTL_INCOMPATIBLE", "spoolctl capabilities returned an error envelope", "inspect spoolctl output or upgrade spoolctl", 3)
        data = payload.get("data")
        if not isinstance(data, dict):
            raise EvalctlError("E_SPOOLCTL_INCOMPATIBLE", "spoolctl capabilities data must be an object", "upgrade spoolctl to >= 0.4.1", 3)
    else:
        data = payload
    envelope_version = str(payload.get("tool_version") or "") if is_envelope else ""
    version = str(envelope_version or data.get("tool_version") or data.get("version") or "")
    contract = str(data.get("contract_version") or "")
    verbs = data.get("verbs", {})
    add_flags = set()
    if isinstance(verbs, dict):
        add_info = verbs.get("add", {})
        if isinstance(add_info, dict):
            add_flags = spoolctl_flag_names(add_info.get("flags", []))
    if version_tuple(version) < (0, 4, 1) or contract != "1" or not {"--cwd", "--env", "--max-crashes"} <= add_flags:
        raise EvalctlError("E_SPOOLCTL_INCOMPATIBLE", "spoolctl is missing required evalctl queue capabilities", "upgrade spoolctl to >= 0.4.1", 3)
    return {**data, "version": version} if envelope_version else data
