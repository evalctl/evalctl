from __future__ import annotations

import json
import os
import pty
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CMD = [sys.executable, "-m", "evalctl"]


class EvalctlCliTests(unittest.TestCase):
    def run_cli(self, args: list[str], cwd: Path, expect: int = 0) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env["PYTHONPATH"] = str(ROOT) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        result = subprocess.run(CMD + args, cwd=cwd, env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(result.returncode, expect, msg=f"stdout={result.stdout}\nstderr={result.stderr}")
        return result

    def envelope(self, args: list[str], cwd: Path, expect: int = 0) -> dict:
        result = self.run_cli(args, cwd, expect)
        return json.loads(result.stdout)

    def run_cli_with_controlling_tty(self, args: list[str], cwd: Path, expect: int = 0) -> str:
        env = os.environ.copy()
        env["PYTHONPATH"] = str(ROOT) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        pid, fd = pty.fork()
        if pid == 0:
            os.chdir(cwd)
            os.execvpe(CMD[0], CMD + args, env)
        output = b""
        try:
            while True:
                chunk = os.read(fd, 4096)
                if not chunk:
                    break
                output += chunk
        except OSError:
            pass
        finally:
            os.close(fd)
        _, status = os.waitpid(pid, 0)
        code = os.waitstatus_to_exitcode(status)
        text = output.decode("utf-8", "replace")
        self.assertEqual(code, expect, msg=text)
        return text

    def suite_path(self, cwd: Path) -> Path:
        return cwd / "evals" / "suites" / "code-review"

    def write_suite(self, cwd: Path, suite: dict) -> None:
        (self.suite_path(cwd) / "suite.json").write_text(json.dumps(suite, indent=2, sort_keys=True) + "\n")

    def load_suite(self, cwd: Path) -> dict:
        return json.loads((self.suite_path(cwd) / "suite.json").read_text())

    def keep_first_case_only(self, cwd: Path) -> None:
        cases_path = self.suite_path(cwd) / "cases.jsonl"
        first = cases_path.read_text().splitlines()[0]
        cases_path.write_text(first + "\n")

    def test_capabilities_and_schema_are_enveloped(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cwd = Path(td)
            caps = self.envelope(["capabilities", "--json"], cwd)
            self.assertEqual(set(caps), {"ok", "tool_version", "data", "meta", "warnings", "commands", "errors"})
            self.assertTrue(caps["ok"])
            self.assertEqual(caps["meta"]["data_hash"], "sha256:a044988b5d109a5dc2fe29a744bfcc5827b9e4591a3a297c945164284f66ad65")
            self.assertEqual(caps["data"]["integrations"]["spoolctl"], {"available": False, "planned": True})
            self.assertEqual(caps["data"]["error_codes"]["E_CASE_INVALID"]["surface"], "envelope")
            self.assertEqual(caps["data"]["error_codes"]["E_RUNNER_TIMEOUT"]["surface"], "runner_json")
            self.assertEqual(caps["data"]["error_codes"]["E_RUNNER_FAILED"]["surface"], "runner_json")
            self.assertIn("run", caps["data"]["verbs"])
            schema = self.envelope(["schema", "run", "--json"], cwd)
            self.assertTrue(schema["ok"])
            self.assertEqual(schema["meta"]["data_hash"], "sha256:b959ad53a3609dfbf03e8026a4709be835766b44d4e00f08ecfba70bc6c4f30b")
            self.assertIn("run", schema["data"]["schemas"])

            docs = self.run_cli(["robot-docs", "guide"], cwd)
            self.assertIn('surface:"runner_json"', docs.stdout)
            self.assertIn("does not put the runner reason code in `errors[]`", docs.stdout)

    def test_init_validate_run_status_report_and_artifact_replay(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cwd = Path(td)
            self.envelope(["init", "--json"], cwd)
            valid = self.envelope(["validate", "code-review", "--json"], cwd)
            self.assertEqual(valid["data"]["case_count"], 2)

            run = self.envelope(["run", "code-review", "--run-id", "r1", "--json"], cwd)
            self.assertFalse(run["data"]["run"]["ok"])
            self.assertEqual(run["data"]["run"]["status_counts"], {"error": 0, "fail": 1, "pass": 1})
            self.assertIn("W_UNSANDBOXED_RUNNER", {w["code"] for w in run["warnings"]})
            original_hash = run["data"]["report_hash"]
            self.assertEqual(original_hash, "sha256:89f6dee9ee258d67c8d868bd4edbf7b0d90af0012cdab31b35ca030717bac88e")

            existing = self.envelope(["run", "code-review", "--run-id", "r1", "--json"], cwd)
            self.assertTrue(existing["data"]["existing"])
            self.assertIn("W_UNSANDBOXED_RUNNER", {w["code"] for w in existing["warnings"]})

            status = self.envelope(["status", "r1", "--json"], cwd)
            self.assertEqual(status["data"]["recommended_action"]["command"], "evalctl report --run-dir evals/runs/r1 --format json")

            report = self.envelope(["report", "r1", "--format", "json"], cwd)
            self.assertEqual(report["data"]["report_hash"], original_hash)
            self.assertEqual([f["id"] for f in report["data"]["failures"]], ["cr-fail"])

            copied = cwd / "copied-run"
            shutil.copytree(cwd / "evals" / "runs" / "r1", copied)
            shutil.rmtree(cwd / "evals")
            replay = self.envelope(["report", "--run-dir", str(copied), "--format", "json"], cwd)
            self.assertEqual(replay["data"]["report_hash"], original_hash)

    def test_cli_input_grammar_and_error_channels(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cwd = Path(td)
            missing = self.run_cli(["run", "--json"], cwd, expect=1)
            parsed = json.loads(missing.stdout)
            self.assertFalse(parsed["ok"])
            self.assertEqual(parsed["errors"][0]["code"], "E_SUITE_NOT_FOUND")
            self.assertIn("run requires a suite name", missing.stderr)

            self.envelope(["init", "--json"], cwd)
            self.envelope(["run", "code-review", "--run-id", "grammar", "--json"], cwd)
            bad_format = self.run_cli(["report", "grammar", "--format", "xml", "--json"], cwd, expect=1)
            parsed_bad = json.loads(bad_format.stdout)
            self.assertFalse(parsed_bad["ok"])
            self.assertEqual(parsed_bad["errors"][0]["code"], "E_CASE_INVALID")

            conflict = self.run_cli(["init", "--json"], cwd, expect=5)
            conflict_payload = json.loads(conflict.stdout)
            self.assertEqual(conflict_payload["errors"][0]["code"], "E_RUN_CONFLICT")

    def test_fail_on_fail_uses_exit_six_with_envelope(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cwd = Path(td)
            self.envelope(["init", "--json"], cwd)
            result = self.run_cli(["run", "code-review", "--run-id", "r2", "--fail-on-fail", "--json"], cwd, expect=6)
            payload = json.loads(result.stdout)
            self.assertTrue(payload["ok"])
            self.assertFalse(payload["data"]["run"]["ok"])

    def test_unsandboxed_warning_stderr_only_when_unacknowledged_tty(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cwd = Path(td)
            self.envelope(["init", "--json"], cwd)
            acknowledged = self.run_cli(["run", "code-review", "--run-id", "ack", "--json"], cwd)
            ack_payload = json.loads(acknowledged.stdout)
            self.assertIn("W_UNSANDBOXED_RUNNER", {w["code"] for w in ack_payload["warnings"]})
            self.assertEqual(acknowledged.stderr, "")

            suite = self.load_suite(cwd)
            suite["acknowledged_unsandboxed_runner"] = False
            self.write_suite(cwd, suite)

            unack_output = self.run_cli_with_controlling_tty(["run", "code-review", "--run-id", "unack", "--json"], cwd)
            unack_payload = json.loads([line for line in unack_output.splitlines() if line.startswith("{")][-1])
            self.assertIn("W_UNSANDBOXED_RUNNER", {w["code"] for w in unack_payload["warnings"]})
            self.assertIn("runner commands execute arbitrary local code", unack_output)

    def test_runner_timeout_is_reportable_case_error(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cwd = Path(td)
            self.envelope(["init", "--json"], cwd)
            self.keep_first_case_only(cwd)
            suite = self.load_suite(cwd)
            suite["runner"]["argv"] = [sys.executable, "-c", "import time; time.sleep(2)"]
            suite["runner"]["timeout_seconds"] = 1
            self.write_suite(cwd, suite)

            run = self.envelope(["run", "code-review", "--run-id", "timeout", "--json"], cwd)
            self.assertTrue(run["ok"])
            self.assertFalse(run["data"]["run"]["ok"])
            self.assertEqual(run["data"]["run"]["status_counts"], {"error": 1, "fail": 0, "pass": 0})
            self.assertEqual(run["errors"], [])
            self.assertIn("W_PARTIAL_RUN", {w["code"] for w in run["warnings"]})

            case_dir = cwd / "evals" / "runs" / "timeout" / "cases" / "cr-pass"
            runner_json = json.loads((case_dir / "runner.json").read_text())
            self.assertTrue(runner_json["timed_out"])
            self.assertFalse(runner_json["spawn_failed"])
            self.assertEqual(runner_json["error_code"], "E_RUNNER_TIMEOUT")
            score_json = json.loads((case_dir / "score.json").read_text())
            self.assertEqual(score_json["status"], "error")

            report = self.envelope(["report", "timeout", "--format", "json"], cwd)
            self.assertEqual(report["data"]["run"]["status_counts"], {"error": 1, "fail": 0, "pass": 0})

            failed = self.envelope(["run", "code-review", "--run-id", "timeout-fail", "--fail-on-fail", "--json"], cwd, expect=6)
            self.assertTrue(failed["ok"])
            self.assertEqual(failed["errors"], [])

    def test_runner_spawn_failure_is_reportable_case_error(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cwd = Path(td)
            self.envelope(["init", "--json"], cwd)
            self.keep_first_case_only(cwd)
            suite = self.load_suite(cwd)
            suite["runner"]["argv"] = ["evalctl-definitely-missing-runner-binary"]
            self.write_suite(cwd, suite)

            run = self.envelope(["run", "code-review", "--run-id", "spawn", "--json"], cwd)
            self.assertTrue(run["ok"])
            self.assertFalse(run["data"]["run"]["ok"])
            self.assertEqual(run["data"]["run"]["status_counts"], {"error": 1, "fail": 0, "pass": 0})
            self.assertEqual(run["errors"], [])
            self.assertIn("W_PARTIAL_RUN", {w["code"] for w in run["warnings"]})

            case_dir = cwd / "evals" / "runs" / "spawn" / "cases" / "cr-pass"
            runner_json = json.loads((case_dir / "runner.json").read_text())
            self.assertFalse(runner_json["timed_out"])
            self.assertTrue(runner_json["spawn_failed"])
            self.assertEqual(runner_json["error_code"], "E_RUNNER_FAILED")
            score_json = json.loads((case_dir / "score.json").read_text())
            self.assertEqual(score_json["status"], "error")


if __name__ == "__main__":
    unittest.main()
