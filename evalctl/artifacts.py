from __future__ import annotations

import json
import os
import re
import stat
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any

from .static_contract import EvalctlError, sha256_bytes, sha256_text, stable_json


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        raise EvalctlError("E_SUITE_NOT_FOUND", f"missing file: {path}", f"create {path} or run evalctl init", 1)
    except json.JSONDecodeError as exc:
        raise EvalctlError("E_SCHEMA_VIOLATION", f"invalid JSON in {path}: {exc.msg}", f"fix {path}:{exc.lineno}", 1)


def load_cases(cases_path: Path) -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    seen: set[str] = set()
    try:
        lines = cases_path.read_text().splitlines()
    except FileNotFoundError:
        raise EvalctlError("E_SUITE_NOT_FOUND", f"missing file: {cases_path}", f"create {cases_path}", 1)
    for line_no, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            case = json.loads(line)
        except json.JSONDecodeError as exc:
            raise EvalctlError("E_CASE_INVALID", f"invalid JSONL at {cases_path}:{line_no}: {exc.msg}", f"fix line {line_no}", 1)
        case_id = case.get("id") or sha256_text(stable_json(case))[7:19]
        if case_id in seen:
            raise EvalctlError("E_CASE_INVALID", f"duplicate case id: {case_id}", "choose unique case ids", 1)
        seen.add(case_id)
        case["id"] = case_id
        cases.append(case)
    return cases


def normalize_rel(path: Path, root: Path) -> str:
    rel = path.relative_to(root)
    parts = rel.parts
    posix = PurePosixPath(*parts).as_posix()
    if posix in ("", ".") or ".." in parts:
        raise ValueError("invalid relative path")
    posix.encode("utf-8", "strict")
    return posix


def display_path_name(path: Path) -> str:
    return os.fsencode(path.name).decode("utf-8", "replace")


def manifest(root: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    warnings: list[dict[str, Any]] = []
    entries: list[dict[str, Any]] = []
    for path in sorted(root.rglob("*"), key=os.fsencode):
        try:
            rel = normalize_rel(path, root)
            st = path.lstat()
            if stat.S_ISDIR(st.st_mode):
                entries.append({"path": rel, "kind": "directory"})
            elif stat.S_ISLNK(st.st_mode):
                target = os.readlink(path)
                entries.append({"path": rel, "kind": "symlink", "target": target, "sha256": sha256_bytes(target.encode("utf-8")), "broken": not path.exists()})
            elif stat.S_ISREG(st.st_mode):
                entries.append({"path": rel, "kind": "file", "sha256": sha256_bytes(path.read_bytes()), "size": st.st_size})
            else:
                subtype = "fifo" if stat.S_ISFIFO(st.st_mode) else "socket" if stat.S_ISSOCK(st.st_mode) else "device" if stat.S_ISCHR(st.st_mode) or stat.S_ISBLK(st.st_mode) else "unknown"
                entries.append({"path": rel, "kind": "other", "subtype": subtype})
        except Exception as exc:
            warnings.append({"code": "W_PATH_UNREADABLE", "message": f"could not read path {display_path_name(path)}: {exc}"})
    return {"root": ".", "entries": entries}, warnings


def diff_manifests(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    b = {e["path"]: e for e in before["entries"]}
    a = {e["path"]: e for e in after["entries"]}
    changed = []
    for path in sorted(set(b) | set(a)):
        if path not in b:
            changed.append({"path": path, "status": "added", "kind": a[path]["kind"], "sha256_before": None, "sha256_after": a[path].get("sha256")})
        elif path not in a:
            changed.append({"path": path, "status": "deleted", "kind": b[path]["kind"], "sha256_before": b[path].get("sha256"), "sha256_after": None})
        elif b[path].get("sha256") != a[path].get("sha256") or b[path].get("kind") != a[path].get("kind"):
            changed.append({"path": path, "status": "modified", "kind": a[path]["kind"], "sha256_before": b[path].get("sha256"), "sha256_after": a[path].get("sha256")})
    return {"changed_paths": changed}


def apply_redaction(text: str, patterns: list[str], values: list[str]) -> tuple[str, bool]:
    changed = False
    for value in values:
        if value and value in text:
            text = text.replace(value, "[REDACTED]")
            changed = True
    for pattern in patterns:
        text2 = re.sub(pattern, "[REDACTED]", text)
        changed = changed or text2 != text
        text = text2
    return text, changed


def _atomic_write(path: Path, text: str, *, _writer: Any | None = None) -> None:
    writer = _writer or (lambda tmp_path, value: tmp_path.write_text(value))
    tmp_name = None
    with tempfile.NamedTemporaryFile("w", dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", delete=False) as tmp:
        tmp_name = tmp.name
    tmp_path = Path(tmp_name)
    try:
        writer(tmp_path, text)
        os.replace(tmp_path, path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()


def json_text(data: Any) -> str:
    return json.dumps(data, indent=2, sort_keys=True) + "\n"


def write_json(path: Path, data: Any, *, serialized: str | None = None) -> None:
    _atomic_write(path, serialized if serialized is not None else json_text(data))


def render_text_diff(diff: dict[str, Any]) -> str:
    return "\n".join(f"{p['status']}\t{p['path']}" for p in diff["changed_paths"]) + ("\n" if diff["changed_paths"] else "")
