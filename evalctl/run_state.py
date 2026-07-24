from __future__ import annotations

import json
import os
import shutil
import socket
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .artifacts import load_cases, read_json, write_json
from .scoring import case_manifest_entry
from .static_contract import EvalctlError, now_iso, sha256_text, stable_json

TERMINAL_CASE_STATUSES = {"pass", "fail", "error", "canceled"}


def write_terminal_marker(case_dir: Path, case_id: str, status: str) -> None:
    write_json(case_dir / "state.json", {"id": case_id, "status": status, "completed_ts": now_iso()})


def is_terminal_marker(path: Path) -> bool:
    try:
        marker = read_json(path)
    except Exception:
        return False
    return marker.get("status") in TERMINAL_CASE_STATUSES


def terminal_marker_count(run_dir: Path) -> int:
    cases_dir = run_dir / "cases"
    if not cases_dir.exists():
        return 0
    return sum(1 for marker in cases_dir.glob("*/state.json") if is_terminal_marker(marker))


def case_entry_from_artifacts(run_dir: Path, case_id: str) -> dict[str, Any]:
    case_dir = run_dir / "cases" / case_id
    case = read_json(case_dir / "input.json")
    score_doc = read_json(case_dir / "score.json")
    provenance_path = case_dir / "inferctl-provenance.json"
    provenance = {"inferctl": read_json(provenance_path)} if provenance_path.exists() else None
    return case_manifest_entry(case, score_doc["status"], score_doc["scores"], provenance=provenance)


def reservation_path(run_dir: Path) -> Path:
    return run_dir / ".reservation.json"


def parse_iso_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def read_reservation(run_dir: Path) -> dict[str, Any] | None:
    path = reservation_path(run_dir)
    if not path.exists():
        return None
    try:
        data = read_json(path)
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def reservation_is_live(record: dict[str, Any], *, now: datetime | None = None) -> bool:
    try:
        ttl_seconds = int(record["ttl_seconds"])
        heartbeat_ts = parse_iso_timestamp(str(record["heartbeat_ts"]))
    except Exception:
        return False
    now = now or datetime.now(timezone.utc)
    return (now - heartbeat_ts).total_seconds() < ttl_seconds


def write_reservation(run_dir: Path, run_id: str, ttl_seconds: int) -> None:
    write_json(reservation_path(run_dir), {
        "run_id": run_id,
        "pid": os.getpid(),
        "host": socket.gethostname(),
        "started_ts": now_iso(),
        "heartbeat_ts": now_iso(),
        "ttl_seconds": ttl_seconds,
    })


def clear_reservation(run_dir: Path) -> None:
    try:
        reservation_path(run_dir).unlink()
    except FileNotFoundError:
        pass


class ReservationHeartbeat:
    def __init__(self, run_dir: Path, run_id: str, ttl_seconds: int) -> None:
        self.run_dir = run_dir
        self.run_id = run_id
        self.ttl_seconds = ttl_seconds
        self.stop = threading.Event()
        self.thread: threading.Thread | None = None

    def __enter__(self) -> "ReservationHeartbeat":
        write_reservation(self.run_dir, self.run_id, self.ttl_seconds)
        interval = max(0.1, min(5.0, self.ttl_seconds / 4))
        self.thread = threading.Thread(target=self._run, args=(interval,), daemon=True)
        self.thread.start()
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.stop.set()
        if self.thread:
            self.thread.join(timeout=1)

    def _run(self, interval: float) -> None:
        while not self.stop.wait(interval):
            try:
                record = read_reservation(self.run_dir) or {}
                record.update({
                    "run_id": self.run_id,
                    "pid": os.getpid(),
                    "host": socket.gethostname(),
                    "heartbeat_ts": now_iso(),
                    "ttl_seconds": self.ttl_seconds,
                })
                record.setdefault("started_ts", now_iso())
                write_json(reservation_path(self.run_dir), record)
            except Exception:
                pass


def split_completed_and_pending(run_dir: Path, cases: list[dict[str, Any]]) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    completed: dict[str, dict[str, Any]] = {}
    pending: list[dict[str, Any]] = []
    for case in sorted(cases, key=lambda c: c["id"]):
        marker_path = run_dir / "cases" / case["id"] / "state.json"
        if is_terminal_marker(marker_path):
            try:
                completed[case["id"]] = case_entry_from_artifacts(run_dir, case["id"])
                continue
            except Exception:
                pass
        pending.append(case)
    return completed, pending


def clean_pending_case_dirs(run_dir: Path, pending_cases: list[dict[str, Any]]) -> None:
    for case in pending_cases:
        shutil.rmtree(run_dir / "cases" / case["id"], ignore_errors=True)


def timeout_seconds_for_run(suite: dict[str, Any], timeout_override: int | None) -> int:
    return int(timeout_override if timeout_override is not None else suite["runner"].get("timeout_seconds", 300))


def build_run_metadata(suite: dict[str, Any], suite_dir: Path, cases: list[dict[str, Any]], run_id: str,
                       jobs: int, timeout_override: int | None, replayed_from: str | None,
                       queue: dict[str, Any] | None = None, mode: str = "synchronous",
                       provenance: dict[str, Any] | None = None) -> dict[str, Any]:
    metadata = {
        "schema_version": 1,
        "run_id": run_id,
        "created_ts": now_iso(),
        "suite_identity": run_identity(suite, suite_dir, cases),
        "execution": {"mode": mode, "jobs": jobs, "timeout_seconds": timeout_seconds_for_run(suite, timeout_override)},
        "replayed_from": replayed_from,
        "queue": queue,
    }
    if provenance is not None:
        metadata["provenance"] = provenance
    return metadata


def write_run_metadata_once(run_dir: Path, metadata: dict[str, Any]) -> None:
    path = run_dir / "run.json"
    if path.exists():
        raise EvalctlError("E_RUN_BUSY", f"run metadata already exists for {metadata['run_id']}", "use evalctl run --resume to continue an incomplete run", 4)
    write_json(path, metadata)


def manifest_from_run_metadata(metadata: dict[str, Any], case_entries: list[dict[str, Any]]) -> dict[str, Any]:
    identity = metadata["suite_identity"]
    manifest_doc: dict[str, Any] = {
        "schema_version": 1,
        "run_id": metadata["run_id"],
        "suite": {"name": identity["suite_name"], "hash": identity["suite_hash"], "case_count": len(identity["cases"])},
        "created_ts": metadata["created_ts"],
        "execution": metadata["execution"],
        "replayed_from": metadata.get("replayed_from"),
        "cases": case_entries,
    }
    if metadata.get("queue") is not None:
        manifest_doc["queue"] = metadata["queue"]
    if metadata.get("provenance") is not None:
        manifest_doc["provenance"] = metadata["provenance"]
    return manifest_doc


def read_run_metadata(run_dir: Path) -> dict[str, Any]:
    path = run_dir / "run.json"
    if not path.exists():
        raise EvalctlError("E_RUN_NOT_FOUND", f"run metadata not found: {path}", "resume requires a run created with durable metadata", 1)
    try:
        metadata = json.loads(path.read_text())
        if not isinstance(metadata, dict):
            raise ValueError("run.json must be an object")
        for key in ("schema_version", "run_id", "created_ts", "suite_identity", "execution"):
            if key not in metadata:
                raise ValueError(f"run.json missing {key}")
        identity = metadata["suite_identity"]
        if not isinstance(identity, dict) or not isinstance(identity.get("cases"), list):
            raise ValueError("run.json suite_identity is malformed")
        execution = metadata["execution"]
        if not isinstance(execution, dict) or not {"mode", "jobs", "timeout_seconds"} <= set(execution):
            raise ValueError("run.json execution is malformed")
        return metadata
    except EvalctlError:
        raise
    except Exception as exc:
        raise EvalctlError("E_RUN_CORRUPT", f"run metadata is corrupt for {run_dir}: {exc}", "inspect or remove run.json, then retry with a valid run", 1)


def finalize_run(run_dir: Path, metadata: dict[str, Any], case_entries: list[dict[str, Any]], *,
                 report_builder: Callable[[Path], dict[str, Any]], markdown_renderer: Callable[[dict[str, Any]], str]) -> tuple[dict[str, Any], bool]:
    manifest_doc = manifest_from_run_metadata(metadata, case_entries)
    write_json(run_dir / "manifest.json", manifest_doc)
    report = report_builder(run_dir)
    (run_dir / "report.md").write_text(markdown_renderer(report))
    run_ok = all(c["status"] == "pass" for c in case_entries)
    data = {
        "run_id": metadata["run_id"],
        "run_dir": str(run_dir),
        "run": {"ok": run_ok, "case_count": len(case_entries), "status_counts": status_counts(case_entries)},
        "report_hash": report["report_hash"],
    }
    if metadata.get("replayed_from") is not None:
        data["replayed_from"] = metadata["replayed_from"]
        data["cases_replayed"] = len(case_entries)
    return data, run_ok


def runs_root() -> Path:
    return Path("evals") / "runs"


def stored_queue_jobs(run_dir: Path) -> list[dict[str, Any]]:
    jobs: list[dict[str, Any]] = []
    for path in sorted((run_dir / "cases").glob("*/job.json")):
        try:
            data = read_json(path)
        except Exception:
            continue
        case_id = path.parent.name
        job_id = data.get("job_id") if isinstance(data, dict) else None
        if job_id is not None:
            jobs.append({"case_id": case_id, "job_id": job_id, "state": data.get("state"), "spoolctl_available": shutil.which("spoolctl") is not None})
    return jobs


def run_case_counts(run_dir: Path) -> dict[str, int]:
    case_count = 0
    suite_dir = run_dir / "suite-snapshot"
    if suite_dir.exists():
        try:
            suite = read_json(suite_dir / "suite.json")
            case_count = len(load_cases(suite_dir / suite.get("cases", "cases.jsonl")))
        except Exception:
            case_count = 0
    marker_count = terminal_marker_count(run_dir)
    if case_count == 0:
        case_count = len(list((run_dir / "cases").glob("*")))
    return {"case_count": case_count, "terminal": marker_count, "pending": max(case_count - marker_count, 0)}


def classify_run_dir(run_dir: Path, *, report_builder: Callable[[Path], dict[str, Any]] | None = None) -> dict[str, Any]:
    manifest_path = run_dir / "manifest.json"
    reservation = read_reservation(run_dir)
    reservation_live = bool(reservation and reservation_is_live(reservation))
    if manifest_path.exists():
        state = "completed"
    elif reservation_live:
        state = "running"
    elif reservation is not None:
        state = "stale"
    else:
        state = "orphaned"
    data: dict[str, Any] = {
        "run_id": run_dir.name,
        "run_dir": str(run_dir),
        "state": state,
        "reservation": {"present": reservation is not None, "live": reservation_live},
        "cases": run_case_counts(run_dir),
        "queue_jobs": stored_queue_jobs(run_dir),
    }
    if manifest_path.exists() and report_builder is not None:
        try:
            report = report_builder(run_dir)
            data["run"] = report["run"]
            data["report_hash"] = report["report_hash"]
        except Exception:
            pass
    return data


def runs_with_queue_state(run_dirs: list[Path]) -> list[Path]:
    return [path for path in run_dirs if (path / ".spoolctl.db").exists() or any((path / "cases").glob("*/job.json"))]


def runs_with_inferctl_state(run_dirs: list[Path]) -> list[Path]:
    return [path for path in run_dirs if any((path / "cases").glob("*/inferctl-provenance.json"))]


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
