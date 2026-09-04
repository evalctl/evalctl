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
        cases.append({"id": score["case_id"], "status": score["status"], "ok": score["ok"], "aggregate_score": aggregate, "scores": score["scores"], "artifacts": entry["artifacts"]})
    failures = [c for c in cases if c["status"] != "pass"]
    failures.sort(key=lambda c: (0 if c["status"] == "error" else 1, c["aggregate_score"], c["id"]))

    def report_score(score: dict[str, Any]) -> dict[str, Any]:
        out = {"scorer": score["scorer"], "ok": score["ok"], "label": score["label"], "findings": score["findings"]}
        if "id" in score:
            out["id"] = score["id"]
        return out

    normalized = {"run": {"ok": not failures, "suite": manifest_doc["suite"]["name"], "case_count": len(cases), "status_counts": status_counts(cases)}, "failures": [{"id": f["id"], "status": f["status"], "scores": [report_score(s) for s in f["scores"]]} for f in failures], "cases": [{"id": c["id"], "status": c["status"], "ok": c["ok"]} for c in cases]}
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


def _xml_text(value: object) -> str:
    return html.escape(str(value), quote=True)


def _junit_seconds(duration_ms: object) -> str:
    return f"{float(duration_ms or 0) / 1000:.3f}"


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
            lines.append(f'    <testcase {case_attrs}><failure type="scorer-failure" message="scorer failure; see body"></failure></testcase>')
        else:
            error_type = "runner-error" if runner.get("timed_out") or runner.get("spawn_failed") else "scorer-error"
            lines.append(f'    <testcase {case_attrs}><error type="{error_type}" message="case errored; see body"></error></testcase>')
    lines.extend(["  </testsuite>", "</testsuites>"])
    return "\n".join(lines) + "\n"
