"""Queued-run coverage against a real spoolctl binary.

Every other queued test in this repo drives tests/fakes.py:install_fake_spoolctl.
That fixture is built from evalctl's own expectations, so by construction it
cannot notice when the real tool's contract moves -- which is exactly how
spoolctl's CONTRACT_VERSION 1 -> 2 bump broke every queued run and stayed
undetected for nine spoolctl releases.

This module drives the installed binary instead. It skips when spoolctl is
absent so local discovery stays green, but setting EVALCTL_REQUIRE_REAL_SPOOLCTL
turns a missing or below-floor binary into a hard failure at import. CI sets it.
A job that can pass by skipping its own tests reproduces the blind spot this
module exists to close.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from evalctl.integration_contracts import MINIMUM_SPOOLCTL_CONTRACT, MINIMUM_SPOOLCTL_VERSION
from evalctl.spoolctl import parse_spoolctl_contract, spoolctl_version_supported

ROOT = Path(__file__).resolve().parents[1]
CMD = [sys.executable, "-m", "evalctl"]
REQUIRE_ENV = "EVALCTL_REQUIRE_REAL_SPOOLCTL"


def probe_installed_spoolctl() -> tuple[str | None, str, int | None, str]:
    """Return (binary, version, contract, skip_reason) for the spoolctl on PATH.

    Runs the binary directly rather than through evalctl.spoolctl.probe_spoolctl:
    the point is to observe what the real tool reports, not to re-assert the
    gate under test.
    """
    binary = shutil.which("spoolctl")
    if binary is None:
        return None, "", None, "spoolctl is not installed on PATH"
    try:
        result = subprocess.run([binary, "capabilities", "--json"], text=True, timeout=30,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return binary, "", None, f"spoolctl capabilities did not respond: {exc}"
    if result.returncode != 0:
        return binary, "", None, f"spoolctl capabilities exited {result.returncode}: {result.stderr.strip()}"
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        return binary, "", None, f"spoolctl capabilities emitted invalid JSON: {exc.msg}"
    data = payload.get("data") if isinstance(payload, dict) and "ok" in payload else payload
    if not isinstance(data, dict):
        return binary, "", None, "spoolctl capabilities data is not an object"
    version = str(payload.get("tool_version") or data.get("tool_version") or data.get("version") or "")
    try:
        contract = parse_spoolctl_contract(data.get("contract_version"))
    except Exception:
        return binary, version, None, f"spoolctl reported an unusable contract: {data.get('contract_version')!r}"
    if not spoolctl_version_supported(version):
        return binary, version, contract, f"spoolctl {version or '(unreported)'} is below the {MINIMUM_SPOOLCTL_VERSION} floor"
    if contract < MINIMUM_SPOOLCTL_CONTRACT:
        return binary, version, contract, f"spoolctl speaks contract {contract}, below the {MINIMUM_SPOOLCTL_CONTRACT} floor"
    return binary, version, contract, ""


SPOOLCTL_BINARY, SPOOLCTL_VERSION, SPOOLCTL_CONTRACT, SKIP_REASON = probe_installed_spoolctl()

# Record what this run actually proved. A CI log that says "ran real-spoolctl
# tests" without naming the version and contract cannot be audited later.
print(f"[real-spoolctl] binary={SPOOLCTL_BINARY} version={SPOOLCTL_VERSION or '(unreported)'} "
      f"contract={SPOOLCTL_CONTRACT} floor={MINIMUM_SPOOLCTL_VERSION}/contract>={MINIMUM_SPOOLCTL_CONTRACT} "
      f"usable={not SKIP_REASON}", file=sys.stderr, flush=True)

if SKIP_REASON and os.environ.get(REQUIRE_ENV):
    raise RuntimeError(
        f"{REQUIRE_ENV} is set, so real-spoolctl coverage must run, but it cannot: {SKIP_REASON}. "
        f"Install spoolctl>={MINIMUM_SPOOLCTL_VERSION} or unset {REQUIRE_ENV}."
    )


@unittest.skipIf(bool(SKIP_REASON), SKIP_REASON or "")
class RealSpoolctlQueueTests(unittest.TestCase):
    """Queued behaviors evalctl depends on, verified against the real binary."""

    # Deliberately a small local harness rather than a shared base class.
    # Extracting the CLI helpers out of tests/test_evalctl.py is separate work
    # and would make this module's diff a refactor instead of new coverage.
    def run_cli(self, args: list[str], cwd: Path, expect: int = 0, extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env["PYTHONPATH"] = str(ROOT) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        if extra_env:
            env.update(extra_env)
        result = subprocess.run(CMD + args, cwd=cwd, env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(result.returncode, expect, msg=f"stdout={result.stdout}\nstderr={result.stderr}")
        return result

    def envelope(self, args: list[str], cwd: Path, expect: int = 0, extra_env: dict[str, str] | None = None) -> dict:
        return json.loads(self.run_cli(args, cwd, expect, extra_env=extra_env).stdout)

    def suite_path(self, cwd: Path) -> Path:
        return cwd / "evals" / "suites" / "code-review"

    def load_suite(self, cwd: Path) -> dict:
        return json.loads((self.suite_path(cwd) / "suite.json").read_text())

    def write_suite(self, cwd: Path, suite: dict) -> None:
        (self.suite_path(cwd) / "suite.json").write_text(json.dumps(suite, indent=2, sort_keys=True) + "\n")

    def load_cases(self, cwd: Path) -> list[dict]:
        return [json.loads(line) for line in (self.suite_path(cwd) / "cases.jsonl").read_text().splitlines() if line.strip()]

    def write_cases(self, cwd: Path, cases: list[dict]) -> None:
        (self.suite_path(cwd) / "cases.jsonl").write_text(
            "\n".join(json.dumps(case, sort_keys=True, separators=(",", ":")) for case in cases) + "\n")

    def keep_first_case_only(self, cwd: Path) -> None:
        cases_path = self.suite_path(cwd) / "cases.jsonl"
        cases_path.write_text(cases_path.read_text().splitlines()[0] + "\n")

    def runner_json(self, cwd: Path, run_id: str, case_id: str) -> dict:
        return json.loads((cwd / "evals" / "runs" / run_id / "cases" / case_id / "runner.json").read_text())

    def test_real_spoolctl_meets_the_advertised_floor(self) -> None:
        # The floor evalctl advertises must be satisfiable by an installable
        # spoolctl. The previous floor, 0.4.1, was never published to PyPI.
        self.assertTrue(spoolctl_version_supported(SPOOLCTL_VERSION),
                        f"installed spoolctl {SPOOLCTL_VERSION} is below {MINIMUM_SPOOLCTL_VERSION}")
        self.assertGreaterEqual(SPOOLCTL_CONTRACT, MINIMUM_SPOOLCTL_CONTRACT)

    def test_capabilities_reports_the_real_spoolctl(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cwd = Path(td)
            caps = self.envelope(["capabilities", "--json"], cwd)
            spool = caps["data"]["integrations"]["spoolctl"]
            self.assertTrue(spool["available"])
            self.assertEqual(spool["version"], SPOOLCTL_VERSION)
            self.assertEqual(spool["contract_version"], SPOOLCTL_CONTRACT)
            self.assertEqual(spool["minimum_version"], MINIMUM_SPOOLCTL_VERSION)
            self.assertEqual(spool["minimum_contract"], MINIMUM_SPOOLCTL_CONTRACT)

    def test_doctor_reports_real_spoolctl_healthy(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cwd = Path(td)
            self.envelope(["init", "--json"], cwd)
            spool = self.envelope(["doctor", "--component", "spoolctl", "--json"], cwd)["data"]["components"]["spoolctl"]
            self.assertEqual(spool["state"], "healthy")
            self.assertEqual(spool["observed"]["version"], SPOOLCTL_VERSION)
            self.assertEqual(spool["observed"]["contract_version"], SPOOLCTL_CONTRACT)

    def test_queued_and_in_process_runs_agree_on_report_hash(self) -> None:
        # The queue must move where the runner executes and nothing else. A
        # queued run whose report differs from the synchronous one would make
        # results incomparable across execution modes.
        with tempfile.TemporaryDirectory() as td:
            cwd = Path(td)
            self.envelope(["init", "--json"], cwd)
            sync = self.envelope(["run", "code-review", "--run-id", "real-sync", "--json"], cwd)
            queued = self.envelope(["run", "code-review", "--run-id", "real-queued", "--queue", "spoolctl", "--json"], cwd)
            self.assertEqual(queued["data"]["report_hash"], sync["data"]["report_hash"])
            self.assertEqual(queued["data"]["run"]["status_counts"], sync["data"]["run"]["status_counts"])

    def test_queued_run_records_queue_provenance_in_the_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cwd = Path(td)
            self.envelope(["init", "--json"], cwd)
            self.envelope(["run", "code-review", "--run-id", "real-provenance", "--queue", "spoolctl", "--json"], cwd)
            run_dir = cwd / "evals" / "runs" / "real-provenance"
            manifest = json.loads((run_dir / "manifest.json").read_text())
            self.assertEqual(manifest["queue"]["backend"], "spoolctl")
            self.assertEqual(manifest["queue"]["db"], ".spoolctl.db")
            self.assertTrue((run_dir / ".spoolctl.db").exists())
            job_docs = sorted(run_dir.glob("cases/*/job.json"))
            self.assertTrue(job_docs, "queued run wrote no per-case job.json")
            for path in job_docs:
                job = json.loads(path.read_text())
                self.assertTrue(job["job_id"])
                self.assertEqual(job["state"], "succeeded")

    def test_queued_nonzero_runner_exit_is_scored_not_an_infrastructure_error(self) -> None:
        # A failing task and broken infrastructure are different outcomes.
        # Reporting the former as the latter silently corrupts eval results
        # instead of failing loudly, so this stays pinned against the real
        # queue's own exit-code reporting.
        with tempfile.TemporaryDirectory() as td:
            cwd = Path(td)
            self.envelope(["init", "--json"], cwd)
            self.keep_first_case_only(cwd)
            case_id = self.load_cases(cwd)[0]["id"]
            suite = self.load_suite(cwd)
            suite["runner"]["argv"] = [sys.executable, "-c", "import sys; sys.exit(7)"]
            suite["scorers"] = [{"name": "exit-code", "required": True}]
            self.write_suite(cwd, suite)
            cases = self.load_cases(cwd)
            cases[0]["expect"] = {"exit_code": 7}
            self.write_cases(cwd, cases)

            nonzero = self.envelope(["run", "code-review", "--run-id", "real-nonzero", "--queue", "spoolctl", "--json"], cwd)
            self.assertTrue(nonzero["data"]["run"]["ok"])
            self.assertEqual(nonzero["data"]["run"]["status_counts"].get("error", 0), 0)
            runner_json = self.runner_json(cwd, "real-nonzero", case_id)
            self.assertEqual(runner_json["exit_code"], 7)
            self.assertIsNone(runner_json["error_code"])

    def test_queued_runner_timeout_maps_to_runner_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cwd = Path(td)
            self.envelope(["init", "--json"], cwd)
            self.keep_first_case_only(cwd)
            case_id = self.load_cases(cwd)[0]["id"]
            suite = self.load_suite(cwd)
            suite["runner"]["argv"] = [sys.executable, "-c", "import time; time.sleep(30)"]
            suite["runner"]["timeout_seconds"] = 1
            self.write_suite(cwd, suite)

            timeout = self.envelope(["run", "code-review", "--run-id", "real-timeout", "--queue", "spoolctl", "--json"], cwd)
            self.assertEqual(timeout["data"]["run"]["status_counts"]["error"], 1)
            self.assertEqual(self.runner_json(cwd, "real-timeout", case_id)["error_code"], "E_RUNNER_TIMEOUT")

    def test_queued_spawn_failure_maps_to_runner_failed(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cwd = Path(td)
            self.envelope(["init", "--json"], cwd)
            self.keep_first_case_only(cwd)
            case_id = self.load_cases(cwd)[0]["id"]
            suite = self.load_suite(cwd)
            suite["runner"]["argv"] = ["evalctl-no-such-runner-binary"]
            self.write_suite(cwd, suite)

            spawn = self.envelope(["run", "code-review", "--run-id", "real-spawn", "--queue", "spoolctl", "--json"], cwd)
            self.assertEqual(spawn["data"]["run"]["status_counts"]["error"], 1)
            self.assertEqual(self.runner_json(cwd, "real-spawn", case_id)["error_code"], "E_RUNNER_FAILED")

    def test_queued_stdin_task_wrapper_delivers_the_task(self) -> None:
        # Queued runs cannot pipe stdin into a job, so evalctl wraps the runner
        # in a shim that reopens EVALCTL_TASK_FILE. The shim only works if the
        # real queue passes argv and env through unmodified.
        with tempfile.TemporaryDirectory() as td:
            cwd = Path(td)
            self.envelope(["suite", "add", "stdin-demo", "--runner-argv", f"{sys.executable} $EVALCTL_WORKSPACE/r.py", "--json"], cwd)
            suite_dir = cwd / "evals" / "suites" / "stdin-demo"
            fixture = suite_dir / "fixtures" / "x"
            fixture.mkdir(parents=True)
            (fixture / "r.py").write_text(
                "from pathlib import Path\nimport os, sys\n"
                "Path(os.environ['EVALCTL_OUTPUT_FILE']).write_text(sys.stdin.read())\n"
            )
            suite = json.loads((suite_dir / "suite.json").read_text())
            suite["runner"]["stdin"] = "task"
            (suite_dir / "suite.json").write_text(json.dumps(suite, indent=2, sort_keys=True) + "\n")
            self.envelope(["case", "add", "stdin-demo", "--id", "x", "--task", "hello stdin",
                           "--workspace", "fixtures/x", "--expect-json", '{"exact":"hello stdin"}', "--json"], cwd)
            self.envelope(["scorer", "add", "stdin-demo", "--name", "exact", "--required", "--json"], cwd)

            stdin_run = self.envelope(["run", "stdin-demo", "--run-id", "real-stdin", "--queue", "spoolctl", "--json"], cwd)
            self.assertTrue(stdin_run["data"]["run"]["ok"])
            self.assertEqual(self.runner_json(cwd, "real-stdin", "x")["exit_code"], 0)

    def test_interrupted_queued_run_resumes_and_keeps_the_report_hash(self) -> None:
        # A queued run killed mid-flight must be resumable into the same run id
        # with a report identical to an uninterrupted queued run, or crash
        # recovery silently produces a different result than the original.
        with tempfile.TemporaryDirectory() as td:
            cwd = Path(td)
            self.envelope(["init", "--json"], cwd)

            slow_runner = (
                "import json, os, time\n"
                "from pathlib import Path\n"
                "case = json.loads(Path(os.environ['EVALCTL_CASE_FILE']).read_text())\n"
                "if case['id'] == os.environ.get('SLEEP_CASE'):\n"
                "    time.sleep(30)\n"
                "text = 'bounds check\\n' if case['id'] == 'cr-fail' else 'null dereference src/app.py:7\\n'\n"
                "Path(os.environ['EVALCTL_OUTPUT_FILE']).write_text(text)\n"
                "Path('review.md').write_text(text)\n"
            )
            suite = self.load_suite(cwd)
            suite["runner"]["env_allowlist"] = sorted(set(suite["runner"].get("env_allowlist", [])) | {"SLEEP_CASE"})
            self.write_suite(cwd, suite)
            for case in self.load_cases(cwd):
                (self.suite_path(cwd) / "fixtures" / case["id"] / "runner.py").write_text(slow_runner)

            # The reference is taken against the same suite and runners the
            # interrupted run will use, so a hash difference can only come from
            # the interruption itself.
            reference = self.envelope(["run", "code-review", "--run-id", "real-reference", "--queue", "spoolctl", "--json"], cwd)

            env = os.environ.copy()
            env["PYTHONPATH"] = str(ROOT) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
            env["SLEEP_CASE"] = "cr-pass"
            proc = subprocess.Popen(
                CMD + ["run", "code-review", "--run-id", "real-resume", "--queue", "spoolctl",
                       "--reservation-ttl", "1", "--json"],
                cwd=cwd, env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            run_dir = cwd / "evals" / "runs" / "real-resume"
            deadline = time.time() + 60
            while time.time() < deadline and not (run_dir / ".spoolctl.db").exists():
                time.sleep(0.1)
            self.assertTrue((run_dir / ".spoolctl.db").exists(), "queued run never created its queue database")
            proc.kill()
            proc.communicate(timeout=60)

            # Without this the test could pass on a run that had already
            # finished, proving nothing about resume.
            case_ids = {case["id"] for case in self.load_cases(cwd)}
            terminal = {path.parent.name for path in run_dir.glob("cases/*/state.json")}
            self.assertTrue(case_ids - terminal, "the run finished before it was interrupted")
            self.assertTrue((run_dir / "run.json").exists(), "interrupted queued run left no durable run metadata")

            # The reservation must expire before --resume will reclaim it.
            time.sleep(2)
            for case in self.load_cases(cwd):
                (self.suite_path(cwd) / "fixtures" / case["id"] / "runner.py").write_text(
                    slow_runner.replace("time.sleep(30)", "pass"))
            resumed = self.envelope(["run", "--resume", "real-resume", "--queue", "spoolctl", "--json"], cwd)
            self.assertEqual(resumed["data"]["run_id"], "real-resume")
            self.assertEqual(resumed["data"]["report_hash"], reference["data"]["report_hash"])


if __name__ == "__main__":
    unittest.main()
