from __future__ import annotations

import os
import signal
import subprocess
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Mapping, Sequence


@dataclass(frozen=True)
class ProcessResult:
    stdout: str
    stderr: str
    timed_out: bool
    spawn_failed: bool
    exit_code: int | None
    signal: int | None
    duration_ms: int

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def _decode_subprocess_output(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return value


def run_process(command: str | Sequence[str], *, shell: bool, cwd: str | Path,
                env: Mapping[str, str], timeout: float, stdin_text: str | None = None,
                kill_drain_timeout: float = 2.0) -> ProcessResult:
    started = time.time()
    proc: subprocess.Popen[str] | None = None
    stdout = ""
    stderr = ""
    timed_out = False
    spawn_failed = False
    exit_code: int | None = None
    signal_value: int | None = None
    try:
        proc = subprocess.Popen(
            command,
            shell=shell,
            cwd=cwd,
            env=dict(env),
            text=True,
            stdin=subprocess.PIPE if stdin_text is not None else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        stdout, stderr = proc.communicate(input=stdin_text, timeout=timeout)
        exit_code = proc.returncode
        if proc.returncode < 0:
            signal_value = -proc.returncode
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        if proc is not None:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                drained_stdout, drained_stderr = proc.communicate(timeout=kill_drain_timeout)
            except subprocess.TimeoutExpired:
                drained_stdout, drained_stderr = "", ""
            stdout = _decode_subprocess_output(exc.stdout) + _decode_subprocess_output(drained_stdout)
            stderr = _decode_subprocess_output(exc.stderr) + _decode_subprocess_output(drained_stderr)
    except OSError as exc:
        spawn_failed = True
        stderr = str(exc)
    duration_ms = int((time.time() - started) * 1000)
    return ProcessResult(
        stdout=stdout,
        stderr=stderr,
        timed_out=timed_out,
        spawn_failed=spawn_failed,
        exit_code=exit_code,
        signal=signal_value,
        duration_ms=duration_ms,
    )
