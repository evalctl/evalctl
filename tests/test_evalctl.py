from __future__ import annotations

import json
import os
import pty
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from evalctl import cli


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

    def load_cases(self, cwd: Path) -> list[dict]:
        return [json.loads(line) for line in (self.suite_path(cwd) / "cases.jsonl").read_text().splitlines() if line.strip()]

    def write_cases(self, cwd: Path, cases: list[dict]) -> None:
        (self.suite_path(cwd) / "cases.jsonl").write_text("\n".join(json.dumps(c, sort_keys=True, separators=(",", ":")) for c in cases) + "\n")

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
            self.assertEqual(schema["meta"]["data_hash"], "sha256:668cfa4ab24174f7e3187f7e011be060f50e3197d92564a53b807d151bc7e5d6")
            self.assertIn("run", schema["data"]["schemas"])
            run_schema = schema["data"]["schemas"]["run"]
            self.assertIn("properties", run_schema)
            self.assertIn("required", run_schema)
            self.assertTrue(run_schema["additionalProperties"])

            all_schemas = self.envelope(["schema", "--json"], cwd)
            for verb in ("capabilities", "schema", "init", "validate", "run", "status", "report"):
                verb_schema = all_schemas["data"]["schemas"][verb]
                self.assertIn("properties", verb_schema)
                self.assertIn("required", verb_schema)
                self.assertTrue(verb_schema["additionalProperties"])

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
            self.assertEqual(existing["data"]["run_id"], "r1")
            self.assertEqual(existing["data"]["run_dir"], "evals/runs/r1")
            self.assertEqual(existing["data"]["run"], run["data"]["run"])
            self.assertEqual(existing["data"]["report_hash"], original_hash)
            self.assertIn("W_UNSANDBOXED_RUNNER", {w["code"] for w in existing["warnings"]})

            status = self.envelope(["status", "r1", "--json"], cwd)
            self.assertEqual(status["data"]["recommended_action"]["command"], "evalctl report --run-dir evals/runs/r1 --format json")

            report = self.envelope(["report", "r1", "--format", "json"], cwd)
            self.assertEqual(report["data"]["report_hash"], original_hash)
            self.assertEqual([f["id"] for f in report["data"]["failures"]], ["cr-fail"])

            copied = cwd / "copied-run"
            shutil.copytree(cwd / "evals" / "runs" / "r1", copied)
            score_files = sorted(copied.glob("cases/*/score.json"))
            self.assertGreater(len(score_files), 1)
            score_files[0].unlink()
            for score_file in score_files[1:]:
                score_file.write_text("not json\n")
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

            bad_jobs = self.run_cli(["run", "code-review", "--jobs", "0", "--json"], cwd, expect=1)
            bad_jobs_payload = json.loads(bad_jobs.stdout)
            self.assertEqual(bad_jobs_payload["errors"][0]["code"], "E_CASE_INVALID")

    def test_run_id_reuse_detects_semantic_conflict(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cwd = Path(td)
            self.envelope(["init", "--json"], cwd)
            first = self.envelope(["run", "code-review", "--run-id", "same", "--json"], cwd)
            manifest_path = cwd / "evals" / "runs" / "same" / "manifest.json"
            manifest_before = manifest_path.read_text()

            cases = self.load_cases(cwd)
            cases[0]["task"] = cases[0]["task"] + " Changed."
            self.write_cases(cwd, cases)
            conflict = self.run_cli(["run", "code-review", "--run-id", "same", "--json"], cwd, expect=5)
            conflict_payload = json.loads(conflict.stdout)
            self.assertEqual(conflict_payload["errors"][0]["code"], "E_RUN_CONFLICT")
            self.assertEqual(manifest_path.read_text(), manifest_before)
            self.assertEqual(first["data"]["report_hash"], self.envelope(["report", "same", "--format", "json"], cwd)["data"]["report_hash"])

    def test_run_id_reuse_detects_case_id_rename_and_busy_reservation(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cwd = Path(td)
            self.envelope(["init", "--json"], cwd)
            self.envelope(["run", "code-review", "--run-id", "rename", "--json"], cwd)
            manifest_path = cwd / "evals" / "runs" / "rename" / "manifest.json"
            manifest_before = manifest_path.read_text()
            cases = self.load_cases(cwd)
            cases[0]["id"] = "cr-renamed"
            self.write_cases(cwd, cases)

            conflict = self.run_cli(["run", "code-review", "--run-id", "rename", "--json"], cwd, expect=5)
            conflict_payload = json.loads(conflict.stdout)
            self.assertEqual(conflict_payload["errors"][0]["code"], "E_RUN_CONFLICT")
            self.assertEqual(manifest_path.read_text(), manifest_before)

            (cwd / "evals" / "runs" / "busy").mkdir()
            busy = self.run_cli(["run", "code-review", "--run-id", "busy", "--json"], cwd, expect=4)
            busy_payload = json.loads(busy.stdout)
            self.assertEqual(busy_payload["errors"][0]["code"], "E_RUN_BUSY")

    def test_atomic_write_keeps_final_json_intact_on_temp_failure(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "manifest.json"
            target.write_text('{"old": true}\n')

            def fail_after_temp_write(tmp_path: Path, text: str) -> None:
                tmp_path.write_text(text[:5])
                raise RuntimeError("simulated write failure")

            with self.assertRaises(RuntimeError):
                cli._atomic_write(target, '{"new": true}\n', _writer=fail_after_temp_write)

            self.assertEqual(target.read_text(), '{"old": true}\n')
            self.assertEqual(list(Path(td).glob("*.tmp")), [])

            absent = Path(td) / "absent.json"
            with self.assertRaises(RuntimeError):
                cli._atomic_write(absent, '{"new": true}\n', _writer=fail_after_temp_write)
            self.assertFalse(absent.exists())
            self.assertEqual(list(Path(td).glob("*.tmp")), [])

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

    def test_runner_timeout_kills_process_group_grandchild(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cwd = Path(td)
            self.envelope(["init", "--json"], cwd)
            self.keep_first_case_only(cwd)
            suite = self.load_suite(cwd)
            grandchild = (
                "import pathlib, sys, time\n"
                "time.sleep(3)\n"
                "pathlib.Path(sys.argv[1]).write_text('alive')\n"
            )
            child = (
                "import subprocess, sys, time\n"
                f"subprocess.Popen([sys.executable, '-c', {grandchild!r}, sys.argv[1]])\n"
                "time.sleep(10)\n"
            )
            runner = (
                "import os, subprocess, sys, time\n"
                "marker = os.path.join(os.environ['EVALCTL_WORKSPACE'], 'grandchild-survived')\n"
                f"subprocess.Popen([sys.executable, '-c', {child!r}, marker])\n"
                "time.sleep(10)\n"
            )
            suite["runner"]["argv"] = [sys.executable, "-c", runner]
            suite["runner"]["timeout_seconds"] = 1
            suite["scorers"] = [{"name": "exit-code", "required": True}]
            self.write_suite(cwd, suite)

            run = self.envelope(["run", "code-review", "--run-id", "pg-timeout", "--json"], cwd)
            self.assertTrue(run["ok"])
            self.assertEqual(run["errors"], [])
            self.assertIn("W_PARTIAL_RUN", {w["code"] for w in run["warnings"]})
            case_dir = cwd / "evals" / "runs" / "pg-timeout" / "cases" / "cr-pass"
            runner_json = json.loads((case_dir / "runner.json").read_text())
            self.assertTrue(runner_json["timed_out"])
            self.assertEqual(runner_json["error_code"], "E_RUNNER_TIMEOUT")
            time.sleep(3.5)
            self.assertFalse((case_dir / "workspace" / "grandchild-survived").exists())

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

    def test_output_file_truncation_caps_persisted_scored_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cwd = Path(td)
            self.envelope(["init", "--json"], cwd)
            self.keep_first_case_only(cwd)
            cases = self.load_cases(cwd)
            cases[0]["expect"] = {"exact": "a" * 10}
            self.write_cases(cwd, cases)
            suite = self.load_suite(cwd)
            runner = (
                "import os, pathlib\n"
                "pathlib.Path(os.environ['EVALCTL_OUTPUT_FILE']).write_text('a' * 20)\n"
            )
            suite["runner"]["argv"] = [sys.executable, "-c", runner]
            suite["runner"]["max_output_bytes"] = 10
            suite["scorers"] = [{"name": "exact", "required": True}]
            self.write_suite(cwd, suite)

            run = self.envelope(["run", "code-review", "--run-id", "trunc-output", "--json"], cwd)
            self.assertTrue(run["data"]["run"]["ok"])
            self.assertIn("W_OUTPUT_TRUNCATED", {w["code"] for w in run["warnings"]})
            case_dir = cwd / "evals" / "runs" / "trunc-output" / "cases" / "cr-pass"
            runner_json = json.loads((case_dir / "runner.json").read_text())
            self.assertTrue(runner_json["output_truncated"])
            self.assertEqual((case_dir / "output.txt").read_bytes(), b"a" * 10)

            report = self.envelope(["report", "trunc-output", "--format", "json"], cwd)
            self.assertEqual(report["data"]["report_hash"], run["data"]["report_hash"])
            self.assertTrue(report["data"]["run"]["ok"])

    def test_scorer_matrix_and_advisory_aggregation(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cwd = Path(td)
            self.envelope(["init", "--json"], cwd)
            cases = [
                {"id": "exact-case", "task": "exact", "workspace": "fixtures/cr-pass", "expect": {"exact": "exact ok", "text_contains": ["optional missing"]}},
                {"id": "regex-case", "task": "regex", "workspace": "fixtures/cr-pass", "expect": {"text_regex": ["^regex [0-9]+$"]}},
                {"id": "json-case", "task": "json", "workspace": "fixtures/cr-pass", "expect": {"json_schema": {}}},
                {"id": "numeric-case", "task": "numeric", "workspace": "fixtures/cr-pass", "expect": {"numeric_threshold": {"path": "score", "gte": 0.8}}},
            ]
            self.write_cases(cwd, cases)
            suite = self.load_suite(cwd)
            runner = (
                "import json, os, pathlib\n"
                "case = json.load(open(os.environ['EVALCTL_CASE_FILE']))\n"
                "outputs = {\n"
                "  'exact-case': 'exact ok',\n"
                "  'regex-case': 'regex 42',\n"
                "  'json-case': '{\"ok\": true}',\n"
                "  'numeric-case': '{\"score\": 0.9}',\n"
                "}\n"
                "pathlib.Path(os.environ['EVALCTL_OUTPUT_FILE']).write_text(outputs[case['id']])\n"
            )
            suite["runner"]["argv"] = [sys.executable, "-c", runner]
            suite["scorers"] = [
                {"name": "exact", "required": True},
                {"name": "regex", "required": True},
                {"name": "json-schema", "required": True},
                {"name": "numeric-threshold", "required": True},
                {"name": "contains", "required": False},
            ]
            self.write_suite(cwd, suite)

            run = self.envelope(["run", "code-review", "--run-id", "scorers", "--json"], cwd)
            self.assertTrue(run["data"]["run"]["ok"])
            report = self.envelope(["report", "scorers", "--format", "json"], cwd)
            self.assertTrue(report["data"]["run"]["ok"])
            self.assertEqual(report["data"]["failures"], [])

            exact_score = json.loads((cwd / "evals" / "runs" / "scorers" / "cases" / "exact-case" / "score.json").read_text())
            advisory = [score for score in exact_score["scores"] if score["scorer"] == "contains"][0]
            self.assertFalse(advisory["ok"])
            self.assertFalse(advisory["required"])

    def test_non_utf8_workspace_path_is_warned_and_omitted(self) -> None:
        if os.name == "nt":
            self.skipTest("raw non-UTF-8 path fixture is POSIX-only")
        with tempfile.TemporaryDirectory() as td:
            cwd = Path(td)
            self.envelope(["init", "--json"], cwd)
            self.keep_first_case_only(cwd)
            suite = self.load_suite(cwd)
            suite["scorers"] = [{"name": "exit-code", "required": True}]
            self.write_suite(cwd, suite)
            workspace = self.suite_path(cwd) / "fixtures" / "cr-pass"
            try:
                os.mkdir(os.fsencode(workspace) + b"/\xff")
            except Exception as exc:
                self.skipTest(f"filesystem does not allow raw non-UTF-8 fixture: {exc}")

            run = self.envelope(["run", "code-review", "--run-id", "bad-path", "--json"], cwd)
            self.assertTrue(run["data"]["run"]["ok"])
            path_warnings = [w for w in run["warnings"] if w["code"] == "W_PATH_UNREADABLE"]
            self.assertTrue(path_warnings)
            self.assertTrue(any("\ufffd" in w["message"] for w in path_warnings))
            before = json.loads((cwd / "evals" / "runs" / "bad-path" / "cases" / "cr-pass" / "workspace-before.json").read_text())
            paths = {entry["path"] for entry in before["entries"]}
            self.assertFalse(any("\ufffd" in path for path in paths))

    def test_jobs_parallelize_and_preserve_normalized_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cwd = Path(td)
            self.envelope(["init", "--json"], cwd)
            suite = self.load_suite(cwd)
            runner = (
                "import os, pathlib, time\n"
                "time.sleep(2)\n"
                "pathlib.Path(os.environ['EVALCTL_OUTPUT_FILE']).write_text('ok\\n')\n"
            )
            suite["runner"]["argv"] = [sys.executable, "-c", runner]
            suite["scorers"] = [{"name": "exit-code", "required": True}]
            self.write_suite(cwd, suite)

            start = time.monotonic()
            serial = self.envelope(["run", "code-review", "--run-id", "jobs1", "--jobs", "1", "--json"], cwd)
            serial_duration = time.monotonic() - start
            start = time.monotonic()
            parallel = self.envelope(["run", "code-review", "--run-id", "jobs2", "--jobs", "2", "--json"], cwd)
            parallel_duration = time.monotonic() - start

            self.assertGreater(serial_duration, 3.5)
            self.assertLess(parallel_duration, 3.5)
            self.assertEqual(serial["data"]["report_hash"], parallel["data"]["report_hash"])

            manifest1 = json.loads((cwd / "evals" / "runs" / "jobs1" / "manifest.json").read_text())
            manifest2 = json.loads((cwd / "evals" / "runs" / "jobs2" / "manifest.json").read_text())
            self.assertEqual(manifest1["execution"]["jobs"], 1)
            self.assertEqual(manifest2["execution"]["jobs"], 2)
            projection1 = [(c["id"], c["status"], c["scores"], c["artifacts"]) for c in manifest1["cases"]]
            projection2 = [(c["id"], c["status"], c["scores"], c["artifacts"]) for c in manifest2["cases"]]
            self.assertEqual(projection1, projection2)

    def test_unreplayable_future_failure_does_not_wedge_run_id(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cwd = Path(td)
            self.envelope(["init", "--json"], cwd)
            cases = self.load_cases(cwd)
            cases[0]["workspace"] = "suite.json"
            self.write_cases(cwd, [cases[0]])

            failed = self.run_cli(["run", "code-review", "--run-id", "badworkspace", "--json"], cwd, expect=3)
            payload = json.loads(failed.stdout)
            self.assertFalse(payload["ok"])
            self.assertEqual(payload["errors"][0]["code"], "E_SCORER_FAILED")
            self.assertFalse((cwd / "evals" / "runs" / "badworkspace").exists())


if __name__ == "__main__":
    unittest.main()
