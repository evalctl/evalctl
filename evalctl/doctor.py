from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from . import __version__
from .inferctl import inferctl_binary, inferctl_capabilities, inferctl_verb_names
from .integration_contracts import MINIMUM_SPOOLCTL_VERSION
from .reports import report_data
from .run_state import classify_run_dir, runs_root, runs_with_inferctl_state, runs_with_queue_state
from .spoolctl import probe_spoolctl
from .static_contract import DOCTOR_COMPONENTS, EvalctlError
from .suite import validate_suite


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
    classified = [classify_run_dir(path, report_builder=report_data) for path in run_dirs]
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
        recommended = action(f"install spoolctl >= {MINIMUM_SPOOLCTL_VERSION} or run without --queue spoolctl", "Queued state exists but spoolctl is absent.") if queued else None
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
