from __future__ import annotations

import json
import os
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

    def test_capabilities_and_schema_are_enveloped(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cwd = Path(td)
            caps = self.envelope(["capabilities", "--json"], cwd)
            self.assertEqual(set(caps), {"ok", "tool_version", "data", "meta", "warnings", "commands", "errors"})
            self.assertTrue(caps["ok"])
            self.assertEqual(caps["meta"]["data_hash"], "sha256:d0dc39347b04efcaa3544a840f8b9f94a773085b8343bd88109abd997aa04ef8")
            self.assertEqual(caps["data"]["integrations"]["spoolctl"], {"available": False, "planned": True})
            self.assertIn("run", caps["data"]["verbs"])
            schema = self.envelope(["schema", "run", "--json"], cwd)
            self.assertTrue(schema["ok"])
            self.assertEqual(schema["meta"]["data_hash"], "sha256:b959ad53a3609dfbf03e8026a4709be835766b44d4e00f08ecfba70bc6c4f30b")
            self.assertIn("run", schema["data"]["schemas"])

    def test_init_validate_run_status_report_and_artifact_replay(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cwd = Path(td)
            self.envelope(["init", "--json"], cwd)
            valid = self.envelope(["validate", "code-review", "--json"], cwd)
            self.assertEqual(valid["data"]["case_count"], 2)

            run = self.envelope(["run", "code-review", "--run-id", "r1", "--json"], cwd)
            self.assertFalse(run["data"]["run"]["ok"])
            self.assertEqual(run["data"]["run"]["status_counts"], {"error": 0, "fail": 1, "pass": 1})
            original_hash = run["data"]["report_hash"]
            self.assertEqual(original_hash, "sha256:89f6dee9ee258d67c8d868bd4edbf7b0d90af0012cdab31b35ca030717bac88e")

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


if __name__ == "__main__":
    unittest.main()
