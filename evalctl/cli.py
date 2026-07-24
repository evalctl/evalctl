from __future__ import annotations

import sys
import time

from .commands import dispatch, report_format_json_requested, wants_json
from .static_contract import EvalctlError, envelope, stable_json


def print_error(err: EvalctlError, *, json_mode: bool, started: float | None = None) -> int:
    print(err.error["message"], file=sys.stderr)
    if err.error.get("corrected_command"):
        print(f"Did you mean: {err.error['corrected_command']}", file=sys.stderr)
    elif err.error.get("did_you_mean"):
        print(f"Did you mean: {err.error['did_you_mean']}", file=sys.stderr)
    if json_mode:
        print(stable_json(envelope(None, ok=False, errors=[err.error], started=started)))
    return err.exit_code


def main(argv: list[str] | None = None) -> int:
    started = time.time()
    argv = list(sys.argv[1:] if argv is None else argv)
    json_mode = wants_json(argv)
    try:
        return dispatch(argv, json_mode, started)
    except EvalctlError as exc:
        return print_error(exc, json_mode=json_mode or report_format_json_requested(argv), started=started)
    except KeyboardInterrupt:
        return print_error(EvalctlError("E_RUNNER_FAILED", "interrupted", "retry the command", 3), json_mode=json_mode or report_format_json_requested(argv), started=started)
    except Exception as exc:
        return print_error(EvalctlError("E_RUNNER_FAILED", f"internal error: {exc}", "run with --json and inspect errors[0]", 3), json_mode=json_mode or report_format_json_requested(argv), started=started)


if __name__ == "__main__":
    raise SystemExit(main())
