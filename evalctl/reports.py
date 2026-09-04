from __future__ import annotations

import html
import os
from pathlib import Path
from typing import Any

from .artifacts import diff_manifests, read_json
from .run_state import status_counts
from .scoring import score_case
from .static_contract import sha256_text, stable_json


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
        runner = read_json(run_dir / "cases" / entry["id"] / "runner.json")
        case_data = {"id": score["case_id"], "status": score["status"], "ok": score["ok"], "aggregate_score": aggregate, "scores": score["scores"], "artifacts": entry["artifacts"]}
        if runner.get("stderr_redacted") is True:
            case_data["runner_stderr_redacted"] = True
        cases.append(case_data)
    failures = [c for c in cases if c["status"] != "pass"]
    failures.sort(key=lambda c: (0 if c["status"] == "error" else 1, c["aggregate_score"], c["id"]))

    def report_score(score: dict[str, Any]) -> dict[str, Any]:
        out = {"scorer": score["scorer"], "ok": score["ok"], "label": score["label"], "findings": score["findings"]}
        if "id" in score:
            out["id"] = score["id"]
        if score.get("redacted") is True:
            out["redacted"] = True
        return out

    def report_failure(failure: dict[str, Any]) -> dict[str, Any]:
        out = {"id": failure["id"], "status": failure["status"], "scores": [report_score(score) for score in failure["scores"]]}
        if failure.get("runner_stderr_redacted") is True:
            out["runner_stderr_redacted"] = True
        return out

    normalized = {"run": {"ok": not failures, "suite": manifest_doc["suite"]["name"], "case_count": len(cases), "status_counts": status_counts(cases)}, "failures": [report_failure(failure) for failure in failures], "cases": [{"id": c["id"], "status": c["status"], "ok": c["ok"]} for c in cases]}
    return {**normalized, "run_id": manifest_doc["run_id"], "report_hash": sha256_text(stable_json(normalized))}


def markdown_report(data: dict[str, Any]) -> str:
    lines = [f"# evalctl report: {data['run_id']}", "", f"Status: {'pass' if data['run']['ok'] else 'fail'}", f"Report hash: `{data['report_hash']}`", "", "## Failures"]
    if not data["failures"]:
        lines.append("None.")
    for failure in data["failures"]:
        lines.append(f"- `{failure['id']}` {failure['status']}")
        for score in failure["scores"]:
            if not score["ok"]:
                marker = " (redacted)" if score.get("redacted") is True else ""
                lines.append(f"  - {score['scorer']}: {score['label']} {score['findings']}{marker}")
        if failure.get("runner_stderr_redacted") is True:
            lines.append("  - runner stderr redacted")
    return "\n".join(lines) + "\n"


def _xml_text(value: object) -> str:
    return html.escape(str(value), quote=True)


def _junit_seconds(duration_ms: object) -> str:
    return f"{float(duration_ms or 0) / 1000:.3f}"


JUNIT_BODY_MAX_BYTES = 8192
JUNIT_TRUNCATION_MARKER = "\n[evalctl: truncated, see run artifacts]"


def _string_leaves(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value] if value else []
    if isinstance(value, list):
        return [leaf for item in value for leaf in _string_leaves(item)]
    if isinstance(value, dict):
        return [leaf for key in sorted(value, key=os.fsencode) for leaf in _string_leaves(value[key])]
    return []


def _junit_sanitize(value: str) -> str:
    def valid(character: str) -> bool:
        codepoint = ord(character)
        return codepoint in (0x9, 0xA, 0xD) or 0x20 <= codepoint <= 0xD7FF or 0xE000 <= codepoint <= 0xFFFD or 0x10000 <= codepoint <= 0x10FFFF
    return "".join(character if valid(character) else "\uFFFD" for character in value)


def _junit_cap(value: str) -> str:
    raw = value.encode("utf-8")
    if len(raw) <= JUNIT_BODY_MAX_BYTES:
        return value
    limit = JUNIT_BODY_MAX_BYTES - len(JUNIT_TRUNCATION_MARKER.encode("utf-8"))
    return raw[:limit].decode("utf-8", "ignore") + JUNIT_TRUNCATION_MARKER


def _junit_body(fragments: list[str]) -> str:
    return _xml_text(_junit_cap(_junit_sanitize("\n".join(fragment for fragment in fragments if fragment))))


def _junit_command_fragment(score: dict[str, Any], case_dir: Path) -> str:
    scorer_id = str(score["id"])
    if score.get("diagnostics_redaction_version") != 1:
        return f"command:{scorer_id}: diagnostic artifact does not prove redaction protocol v1; diagnostic text omitted (see run artifacts)"
    items: list[str] = []
    label = score.get("label")
    if isinstance(label, str) and label:
        items.append(label)
    items.extend(_string_leaves(score.get("findings", [])))
    stderr_path = case_dir / "scorers" / f"{scorer_id}.stderr.txt"
    if stderr_path.exists():
        stderr = stderr_path.read_text(errors="replace")
        if stderr:
            items.append(stderr)
    return "\n".join(items)


def _junit_builtin_fragment(score: dict[str, Any]) -> str:
    name = str(score["scorer"])
    return f"builtin:{name}\nbuilt-in scorer {name} failed; see run artifacts"


def _junit_score_fragments(scores: list[dict[str, Any]], case_dir: Path, *, errors_only: bool) -> list[str]:
    selected = [score for score in scores if bool(score.get("error"))] if errors_only else [score for score in scores if not score.get("ok")]
    selected.sort(key=lambda score: (0, str(score["scorer"])) if score.get("scorer") != "command" else (1, str(score.get("id", ""))))
    return [_junit_command_fragment(score, case_dir) if score.get("scorer") == "command" else _junit_builtin_fragment(score) for score in selected]


def _junit_runner_fragment(runner: dict[str, Any], case_dir: Path) -> str:
    reason = "runner timed out" if runner.get("timed_out") else "runner failed to start"
    items = [f"runner: {reason}"]
    stderr_path = case_dir / "runner.stderr.txt"
    if runner.get("diagnostics_redaction_version") == 1 and stderr_path.exists():
        stderr = stderr_path.read_text(errors="replace")
        if stderr:
            items.append(stderr)
    return "\n".join(items)


def junit_report(run_dir: Path) -> str:
    """Render the fixed 1.1 JUnit element and attribute set for one run."""
    manifest_doc = read_json(run_dir / "manifest.json")
    suite = read_json(run_dir / "suite-snapshot" / "suite.json")
    suite_name = str(manifest_doc["suite"]["name"])
    cases: list[tuple[dict[str, Any], dict[str, Any], dict[str, Any]]] = []
    for entry in sorted(manifest_doc["cases"], key=lambda case: os.fsencode(case["id"])):
        score = recompute_case_score(run_dir, entry, suite)
        runner = read_json(run_dir / "cases" / entry["id"] / "runner.json")
        cases.append((entry, score, runner))
    failures = sum(score["status"] == "fail" for _, score, _ in cases)
    errors = sum(score["status"] == "error" for _, score, _ in cases)
    total_seconds = sum(float(runner.get("duration_ms") or 0) / 1000 for _, _, runner in cases)
    attrs = f'name="{_xml_text(suite_name)}" tests="{len(cases)}" failures="{failures}" errors="{errors}" time="{total_seconds:.3f}"'
    lines = ['<?xml version="1.0" encoding="UTF-8"?>', f"<testsuites {attrs}>", f"  <testsuite {attrs}>"]
    for entry, score, runner in cases:
        case_attrs = f'name="{_xml_text(entry["id"])}" classname="{_xml_text(suite_name)}" time="{_junit_seconds(runner.get("duration_ms"))}"'
        if score["status"] == "pass":
            lines.append(f"    <testcase {case_attrs}/>")
        elif score["status"] == "fail":
            body = _junit_body(_junit_score_fragments(score["scores"], run_dir / "cases" / entry["id"], errors_only=False))
            lines.append(f'    <testcase {case_attrs}><failure type="scorer-failure" message="scorer failure; see body">{body}</failure></testcase>')
        else:
            error_type = "runner-error" if runner.get("timed_out") or runner.get("spawn_failed") else "scorer-error"
            fragments = [_junit_runner_fragment(runner, run_dir / "cases" / entry["id"])] if error_type == "runner-error" else _junit_score_fragments(score["scores"], run_dir / "cases" / entry["id"], errors_only=True)
            body = _junit_body(fragments)
            lines.append(f'    <testcase {case_attrs}><error type="{error_type}" message="case errored; see body">{body}</error></testcase>')
    lines.extend(["  </testsuite>", "</testsuites>"])
    return "\n".join(lines) + "\n"
