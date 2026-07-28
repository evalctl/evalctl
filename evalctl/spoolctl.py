from __future__ import annotations

import json
import re
import shutil
import subprocess
from typing import Any

from .integration_contracts import MINIMUM_SPOOLCTL_CONTRACT, MINIMUM_SPOOLCTL_VERSION, SPOOLCTL_UPGRADE_HINT
from .static_contract import EvalctlError


def parse_spoolctl_version(value: str) -> tuple[tuple[int, ...], bool]:
    """Return spoolctl's version as (base release tuple, is_prerelease).

    The previous helper took the numeric prefix of each dot-segment and threw
    the rest away, so 0.4.11-rc1, 0.4.11rc1, 0.4.11.dev1, and 0.4.11+local all
    became (0, 4, 11) and a release candidate passed as its final release.
    The trailing text is kept here as a prerelease flag instead of discarded.

    Local/build metadata after "+" is not a prerelease marker and is dropped
    without setting the flag.
    """
    base_text = str(value).strip().split("+", 1)[0]
    parts: list[int] = []
    prerelease = False
    for item in base_text.split("."):
        match = re.match(r"(\d+)", item)
        if match is None:
            prerelease = prerelease or bool(item)
            continue
        parts.append(int(match.group(1)))
        if match.end() != len(item):
            prerelease = True
    return tuple(parts), prerelease


def spoolctl_version_supported(value: str) -> bool:
    """Whether an observed spoolctl version clears the supported floor.

    A prerelease must be strictly above the floor: an rc of the floor release
    precedes that release and is not the release. Local build metadata on a
    final version at or above the floor is fine.
    """
    base, prerelease = parse_spoolctl_version(value)
    floor, _ = parse_spoolctl_version(MINIMUM_SPOOLCTL_VERSION)
    return base > floor if prerelease else base >= floor


REQUIRED_ADD_FLAGS = frozenset({"--cwd", "--env", "--max-crashes"})


def contract_scalar(value: Any) -> Any:
    return value if value is None or isinstance(value, (int, str)) else repr(value)


def parse_spoolctl_contract(value: Any) -> int:
    """Return spoolctl's contract version as an int.

    The contract must become a number before it is compared. A string compare
    sorts "10" before "2", so a spoolctl newer than the floor would be rejected
    -- the same forward-compatibility failure that the old equality pin caused.

    spoolctl's JSON type for this field is not guaranteed, so both 2 and "2"
    are accepted. Missing, empty, non-numeric, and non-positive values are
    rejected with the same fields as a below-floor contract, so a consumer sees
    one error shape per cause rather than one shape for old-but-valid values
    and another for garbage.
    """
    parsed: int | None = None
    if isinstance(value, bool):
        parsed = None
    elif isinstance(value, int):
        parsed = value
    elif isinstance(value, str) and value.strip().lstrip("+").isdigit():
        parsed = int(value.strip())
    if parsed is None or parsed < 1:
        raise EvalctlError(
            "E_SPOOLCTL_INCOMPATIBLE",
            f"spoolctl reported an unusable contract version: {value!r}",
            SPOOLCTL_UPGRADE_HINT,
            3,
            observed_contract=contract_scalar(value),
            minimum_contract=MINIMUM_SPOOLCTL_CONTRACT,
        )
    return parsed


def spoolctl_binary() -> str:
    path = shutil.which("spoolctl")
    if not path:
        raise EvalctlError("E_SPOOLCTL_UNAVAILABLE", "spoolctl is not available on PATH", SPOOLCTL_UPGRADE_HINT, 3)
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
        raise EvalctlError("E_SPOOLCTL_UNAVAILABLE", f"could not run spoolctl: {exc}", SPOOLCTL_UPGRADE_HINT, 3)
    if result.returncode == 4:
        raise EvalctlError("E_JOB_TRANSIENT", "spoolctl reported a transient job-system failure", "retry the queued run or resume it later", 4)
    if result.returncode not in allow_exit_codes:
        raise EvalctlError("E_SPOOLCTL_INCOMPATIBLE", f"spoolctl command failed: {result.stderr.strip() or result.stdout.strip()}", SPOOLCTL_UPGRADE_HINT, 3)
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise EvalctlError("E_SPOOLCTL_INCOMPATIBLE", f"spoolctl returned invalid JSON: {exc.msg}", SPOOLCTL_UPGRADE_HINT, 3)
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
    raise EvalctlError("E_SPOOLCTL_INCOMPATIBLE", "spoolctl JSON output must be an object", SPOOLCTL_UPGRADE_HINT, 3)


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
        raise EvalctlError("E_SPOOLCTL_INCOMPATIBLE", "spoolctl capabilities output must be a JSON object", SPOOLCTL_UPGRADE_HINT, 3)
    is_envelope = "ok" in payload
    if is_envelope:
        if not payload.get("ok"):
            raise EvalctlError("E_SPOOLCTL_INCOMPATIBLE", "spoolctl capabilities returned an error envelope", "inspect spoolctl output or upgrade spoolctl", 3)
        data = payload.get("data")
        if not isinstance(data, dict):
            raise EvalctlError("E_SPOOLCTL_INCOMPATIBLE", "spoolctl capabilities data must be an object", SPOOLCTL_UPGRADE_HINT, 3)
    else:
        data = payload
    envelope_version = str(payload.get("tool_version") or "") if is_envelope else ""
    version = str(envelope_version or data.get("tool_version") or data.get("version") or "")
    verbs = data.get("verbs", {})
    add_flags = set()
    if isinstance(verbs, dict):
        add_info = verbs.get("add", {})
        if isinstance(add_info, dict):
            add_flags = spoolctl_flag_names(add_info.get("flags", []))

    # Three unrelated questions, three answers. One boolean covering all of them
    # told a user whose spoolctl was merely a contract ahead that flags were
    # missing.
    if not spoolctl_version_supported(version):
        raise EvalctlError(
            "E_SPOOLCTL_INCOMPATIBLE",
            f"spoolctl {version or '(unreported version)'} is below the supported minimum {MINIMUM_SPOOLCTL_VERSION}",
            SPOOLCTL_UPGRADE_HINT,
            3,
            observed_version=version,
            minimum_version=MINIMUM_SPOOLCTL_VERSION,
        )

    # A floor, deliberately, not an equality check. The equality form looks more
    # careful and is what broke this: spoolctl moved CONTRACT_VERSION 1 -> 2 in
    # its 0.4.5 and every queued evalctl run failed for nine releases. Each of a
    # sibling tool's routine releases must not become an evalctl outage.
    # Newer-than-floor contracts are accepted with no upper bound: a
    # maximum-tested constant would need bumping on every spoolctl release and
    # would go stale, so the observed value is reported instead of policed.
    observed_contract = parse_spoolctl_contract(data.get("contract_version"))
    if observed_contract < MINIMUM_SPOOLCTL_CONTRACT:
        raise EvalctlError(
            "E_SPOOLCTL_INCOMPATIBLE",
            f"spoolctl speaks contract {observed_contract}, below the supported minimum {MINIMUM_SPOOLCTL_CONTRACT}",
            SPOOLCTL_UPGRADE_HINT,
            3,
            observed_contract=observed_contract,
            minimum_contract=MINIMUM_SPOOLCTL_CONTRACT,
        )

    missing_flags = sorted(REQUIRED_ADD_FLAGS - add_flags)
    if missing_flags:
        raise EvalctlError(
            "E_SPOOLCTL_INCOMPATIBLE",
            f"spoolctl add is missing flags required by evalctl: {', '.join(missing_flags)}",
            SPOOLCTL_UPGRADE_HINT,
            3,
            missing_flags=missing_flags,
        )

    resolved = {**data, "contract_version": observed_contract}
    if envelope_version:
        resolved["version"] = version
    return resolved
