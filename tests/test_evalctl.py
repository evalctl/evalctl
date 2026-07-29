from __future__ import annotations

import json
import os
import pty
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import tomllib
from pathlib import Path

from evalctl import cli
from evalctl import commands
from evalctl import __version__ as evalctl_version
from evalctl import artifacts
from evalctl import spoolctl as spoolctl_module
from evalctl import static_contract
from evalctl import runner
from evalctl import suite as suite_module
from evalctl import run_state
from evalctl.static_contract import now_iso
from evalctl.processes import run_process
from tests.fakes import install_fake_inferctl, install_fake_spoolctl, run_fake_tool


ROOT = Path(__file__).resolve().parents[1]
CMD = [sys.executable, "-m", "evalctl"]
GOLDENS = ROOT / "tests" / "goldens"


class EvalctlCliTests(unittest.TestCase):
    def run_cli(self, args: list[str], cwd: Path, expect: int = 0, extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env["PYTHONPATH"] = str(ROOT) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        if extra_env:
            env.update(extra_env)
        result = subprocess.run(CMD + args, cwd=cwd, env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(result.returncode, expect, msg=f"stdout={result.stdout}\nstderr={result.stderr}")
        return result

    def envelope(self, args: list[str], cwd: Path, expect: int = 0, extra_env: dict[str, str] | None = None) -> dict:
        result = self.run_cli(args, cwd, expect, extra_env=extra_env)
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

    def fix_cr_fail_runner(self, cwd: Path) -> None:
        (self.suite_path(cwd) / "fixtures" / "cr-fail" / "runner.py").write_text(
            "from pathlib import Path\n"
            "import os\n"
            "Path(os.environ['EVALCTL_OUTPUT_FILE']).write_text('bounds check fixed\\n')\n"
            "Path('review.md').write_text('bounds check fixed\\n')\n"
        )

    def write_resume_runner(self, cwd: Path) -> None:
        runner = (
            "import json, os, time\n"
            "from pathlib import Path\n"
            "case = json.loads(Path(os.environ['EVALCTL_CASE_FILE']).read_text())\n"
            "if case['id'] == os.environ.get('SLEEP_CASE'):\n"
            "    time.sleep(30)\n"
            "log = os.environ.get('RUN_LOG')\n"
            "if log:\n"
            "    with open(log, 'a') as f:\n"
            "        f.write(case['id'] + '\\n')\n"
            "text = 'bounds check\\n' if case['id'] == 'cr-fail' else 'null dereference src/app.py:7\\n'\n"
            "Path(os.environ['EVALCTL_OUTPUT_FILE']).write_text(text)\n"
            "Path('review.md').write_text(text)\n"
        )
        suite = self.load_suite(cwd)
        suite["runner"]["env_allowlist"] = sorted(set(suite["runner"].get("env_allowlist", [])) | {"RUN_LOG", "SLEEP_CASE"})
        self.write_suite(cwd, suite)
        for case_id in ("cr-fail", "cr-pass"):
            (self.suite_path(cwd) / "fixtures" / case_id / "runner.py").write_text(runner)

    def normalize_envelope_semantic_meta(self, payload: dict) -> dict:
        normalized = json.loads(json.dumps(payload))
        meta = normalized.get("meta")
        if isinstance(meta, dict):
            for key in ("request_id", "ts_iso", "elapsed_ms"):
                meta.pop(key, None)
        return normalized

    def normalize_golden_envelope(self, payload: dict) -> dict:
        normalized = json.loads(json.dumps(payload))
        normalized["tool_version"] = "<TOOL_VERSION>"
        meta = normalized.get("meta")
        if isinstance(meta, dict):
            if "request_id" in meta:
                meta["request_id"] = "<REQUEST_ID>"
            if "ts_iso" in meta:
                meta["ts_iso"] = "<TS_ISO>"
            if "elapsed_ms" in meta:
                meta["elapsed_ms"] = "<ELAPSED_MS>"
        return normalized

    def assert_json_golden(self, name: str, payload: dict) -> None:
        actual = json.dumps(self.normalize_golden_envelope(payload), indent=2, sort_keys=True) + "\n"
        expected = (GOLDENS / name).read_text()
        self.assertEqual(actual, expected)

    def assert_text_golden(self, name: str, text: str) -> None:
        expected = (GOLDENS / name).read_text()
        self.assertEqual(text, expected)

    def test_normalized_success_output_goldens(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cwd = Path(td)
            env = {"PATH": "/nonexistent"}
            self.assert_text_golden("help.txt", self.run_cli(["--help"], cwd, extra_env=env).stdout)
            self.assert_json_golden("capabilities.json", self.envelope(["capabilities", "--json"], cwd, extra_env=env))
            self.assert_json_golden("schema-run.json", self.envelope(["schema", "run", "--json"], cwd, extra_env=env))
            self.assert_json_golden("schema-jobs.json", self.envelope(["schema", "jobs", "--json"], cwd, extra_env=env))
            self.assert_json_golden("schema-plan.json", self.envelope(["schema", "plan", "--json"], cwd, extra_env=env))
            self.assert_text_golden("robot-docs-guide.txt", self.run_cli(["robot-docs", "guide"], cwd, extra_env=env).stdout)

    def test_normalized_error_envelope_goldens(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cwd = Path(td)
            env = {"PATH": "/nonexistent"}
            self.envelope(["init", "--json"], cwd, extra_env=env)
            self.envelope(["run", "code-review", "--run-id", "existing", "--json"], cwd, extra_env=env)
            cases = {
                "errors/unknown-command.json": ["stauts", "--json"],
                "errors/unknown-bool-flag.json": ["plan", "code-review", "--jsoon"],
                "errors/unknown-value-flag-preserve-value.json": ["run", "code-review", "--run-idd", "oops", "--json"],
                "errors/unknown-value-flag-requires-value.json": ["run", "code-review", "--bogus", "--json"],
                "errors/invalid-timeout.json": ["plan", "code-review", "--timeout", "nope", "--json"],
                "errors/missing-timeout-value.json": ["run", "code-review", "--timeout", "--json"],
                "errors/empty-string-value.json": ["run", "code-review", "--run-id", "", "--json"],
                "errors/value-flag-followed-by-known-flag.json": ["run", "code-review", "--run-id", "--json"],
                "errors/malformed-format.json": ["report", "existing", "--format"],
                "errors/unknown-init-flag.json": ["init", "--forse", "--json"],
            }
            for name, args in cases.items():
                with self.subTest(name=name):
                    result = self.run_cli(args, cwd, expect=1, extra_env=env)
                    self.assert_json_golden(name, json.loads(result.stdout))

    def test_coverage_subprocess_startup_records_cli_handlers(self) -> None:
        try:
            import coverage  # noqa: F401
        except ModuleNotFoundError:
            self.skipTest("coverage is a development-only dependency")
        with tempfile.TemporaryDirectory() as td:
            cwd = Path(td)
            cov_dir = cwd / ".coverage-data"
            cov_dir.mkdir()
            coveragerc = cwd / ".coveragerc"
            coveragerc.write_text(
                "[run]\n"
                "branch = true\n"
                "parallel = true\n"
                f"source = {ROOT / 'evalctl'}\n"
                f"data_file = {cov_dir / '.coverage'}\n"
            )
            env = {
                "PATH": "/nonexistent",
                "COVERAGE_PROCESS_START": str(coveragerc),
                "COVERAGE_FILE": str(cov_dir / ".coverage"),
            }
            self.envelope(["init", "--json"], cwd, extra_env=env)
            self.envelope(["run", "code-review", "--run-id", "coverage-sanity", "--json"], cwd, extra_env=env)
            subprocess.run([sys.executable, "-m", "coverage", "combine", "--data-file", str(cov_dir / ".coverage"), str(cov_dir)], cwd=ROOT, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
            result = subprocess.run([sys.executable, "-m", "coverage", "json", "-o", "-", "--data-file", str(cov_dir / ".coverage")], cwd=ROOT, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
            data = json.loads(result.stdout)
            cli_key = next((key for key in data["files"] if key.endswith("evalctl/commands.py")), None)
            self.assertIsNotNone(cli_key, result.stdout)
            executed_lines = set(data["files"][cli_key]["executed_lines"])
            command_run_line = commands.command_run.__code__.co_firstlineno
            self.assertIn(command_run_line, executed_lines)

    def test_capabilities_and_schema_are_enveloped(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cwd = Path(td)
            caps = self.envelope(["capabilities", "--json"], cwd, extra_env={"PATH": "/nonexistent"})
            self.assertEqual(set(caps), {"ok", "tool_version", "data", "meta", "warnings", "commands", "errors"})
            self.assertTrue(caps["ok"])
            self.assertEqual(caps["meta"]["data_hash"], "sha256:ded8c6bbe9fac7519e571ab2e1cb9c790cf2ff06cd939a2c805978251b4af9f8")
            self.assertEqual(caps["tool_version"], "0.4.1")
            self.assertEqual(caps["data"]["integrations"]["spoolctl"], {"available": False, "planned": False, "minimum_version": "0.4.11", "minimum_contract": 2})
            self.assertIn("durable_runs", caps["data"]["features"])
            self.assertIn("queue_spoolctl", caps["data"]["features"])
            self.assertIn("inferctl_preflight_provenance", caps["data"]["features"])
            self.assertEqual(caps["data"]["error_codes"]["E_CASE_INVALID"]["surface"], "envelope")
            self.assertEqual(caps["data"]["error_codes"]["E_UNKNOWN_COMMAND"]["exit"], 1)
            self.assertEqual(caps["data"]["error_codes"]["E_UNKNOWN_SUBCOMMAND"]["surface"], "envelope")
            self.assertEqual(caps["data"]["error_codes"]["E_UNKNOWN_FLAG"]["surface"], "envelope")
            self.assertEqual(caps["data"]["error_codes"]["E_SPOOLCTL_UNAVAILABLE"]["exit"], 3)
            self.assertEqual(caps["data"]["error_codes"]["E_JOB_TRANSIENT"]["exit"], 4)
            self.assertEqual(caps["data"]["error_codes"]["E_INFERCTL_UNAVAILABLE"]["where"], ["doctor"])
            self.assertEqual(caps["data"]["error_codes"]["E_INFERCTL_INCOMPATIBLE"]["surface"], "envelope")
            self.assertEqual(caps["data"]["error_codes"]["W_INFERCTL_ABSENT"]["surface"], "envelope")
            self.assertEqual(caps["data"]["error_codes"]["W_INFERCTL_CAPTURE_FAILED"]["where"], ["run", "resume"])
            self.assertEqual(caps["data"]["error_codes"]["W_INFERCTL_PREFLIGHT_BLOCKED"]["class"], "warning")
            self.assertEqual(caps["data"]["error_codes"]["E_RUNNER_TIMEOUT"]["surface"], "runner_json")
            self.assertEqual(caps["data"]["error_codes"]["E_RUNNER_FAILED"]["surface"], "runner_json")
            self.assertEqual(caps["data"]["error_codes"]["E_SCORER_CASE_FAILED"]["surface"], "score_json")
            self.assertIn("replay", caps["data"]["error_codes"]["E_SCORER_CASE_FAILED"]["where"])
            self.assertIn("replay", caps["data"]["error_codes"]["W_UNSANDBOXED_RUNNER"]["where"])
            self.assertIn("--resume", caps["data"]["verbs"]["run"]["flags"])
            self.assertIn("--queue", caps["data"]["verbs"]["run"]["flags"])
            self.assertIn("--slots", caps["data"]["verbs"]["run"]["flags"])
            self.assertIn("--inferctl-task", caps["data"]["verbs"]["run"]["flags"])
            self.assertIn("--reservation-ttl", caps["data"]["verbs"]["run"]["flags"])
            self.assertEqual(caps["data"]["verbs"]["doctor"]["mega_command"], "DIAGNOSE")
            self.assertEqual(caps["data"]["verbs"]["doctor"]["exit_codes"], [0, 1])
            self.assertEqual(caps["data"]["verbs"]["plan"]["mega_command"], "PLAN")
            self.assertEqual(caps["data"]["verbs"]["plan"]["exit_codes"], [0, 1])
            for verb in ("run", "jobs", "replay", "suite", "case", "scorer", "doctor", "plan"):
                self.assertIn(verb, caps["data"]["verbs"])
            schema = self.envelope(["schema", "run", "--json"], cwd)
            self.assertTrue(schema["ok"])
            self.assertEqual(schema["meta"]["data_hash"], "sha256:90d9bb2f67c88bbd3338c3a21f4a5723ee0d6e3b46b19b35f74d98dde31ce2c6")
            self.assertIn("run", schema["data"]["schemas"])
            run_schema = schema["data"]["schemas"]["run"]
            self.assertIn("properties", run_schema)
            self.assertIn("required", run_schema)
            self.assertTrue(run_schema["additionalProperties"])
            self.assertIn("queue", run_schema["properties"])
            jobs_schema = self.envelope(["schema", "jobs", "--json"], cwd)
            self.assertEqual(jobs_schema["meta"]["data_hash"], "sha256:6c51619952aeecaf2915158c570de05659356ac517a020ef34d8492299a62935")
            self.assertIn("queue_jobs", jobs_schema["data"]["schemas"]["jobs"]["properties"])

            all_schemas = self.envelope(["schema", "--json"], cwd)
            for verb in ("capabilities", "schema", "init", "validate", "doctor", "plan", "run", "jobs", "replay", "suite", "case", "scorer", "status", "report"):
                verb_schema = all_schemas["data"]["schemas"][verb]
                self.assertIn("properties", verb_schema)
                self.assertIn("required", verb_schema)
                self.assertTrue(verb_schema["additionalProperties"])
            doctor_schema = self.envelope(["schema", "doctor", "--json"], cwd)
            self.assertEqual(doctor_schema["meta"]["data_hash"], "sha256:6582f8cd37fd09b9ad29c9422da0e2418a5d5a6f9c2105e4dd14f149ef11e0e4")
            self.assertIn("doctor", doctor_schema["data"]["schemas"])
            plan_schema = self.envelope(["schema", "plan", "--json"], cwd)
            self.assertEqual(plan_schema["meta"]["data_hash"], "sha256:1a71ef66f11e8dbc3895f21cdf60d516e17a1cb7d59576fb84bb938367bb82da")
            self.assertIn("plan", plan_schema["data"]["schemas"])
            for verb in ("jobs", "replay", "suite", "case", "scorer", "doctor", "plan"):
                single_schema = self.envelope(["schema", verb, "--json"], cwd)
                self.assertIn(verb, single_schema["data"]["schemas"])
                self.assertTrue(single_schema["data"]["schemas"][verb]["additionalProperties"])

            docs = self.run_cli(["robot-docs", "guide"], cwd)
            self.assertIn('surface:"runner_json"', docs.stdout)
            self.assertIn('surface:"score_json"', docs.stdout)
            self.assertIn("does not put the per-case reason code in `errors[]`", docs.stdout)
            self.assertIn("did_you_mean", docs.stdout)
            self.assertIn("doctor --json", docs.stdout)
            self.assertIn("plan code-review --json", docs.stdout)
            self.assertIn("--inferctl-task TASK", docs.stdout)
            self.assertIn("run --resume", docs.stdout)
            self.assertIn("--queue spoolctl", docs.stdout)

    def test_static_verb_registry_matches_capabilities(self) -> None:
        self.assertEqual(static_contract.VERB_NAMES, set(commands.capabilities_data()["verbs"]))

    def test_version_matches_package_metadata_shape(self) -> None:
        metadata = tomllib.loads((ROOT / "pyproject.toml").read_text())
        version = metadata["project"]["version"]
        self.assertEqual(evalctl_version, version)
        self.assertRegex(version, re.compile(r"^\d+\.\d+\.\d+(?:[-+][A-Za-z0-9_.-]+)?$"))
        with tempfile.TemporaryDirectory() as td:
            result = self.run_cli(["--version"], Path(td))
            self.assertEqual(result.stdout.strip(), version)

    def test_command_registry_matches_capabilities_help_and_validation_sites(self) -> None:
        caps = commands.capabilities_data()
        self.assertEqual(set(static_contract.COMMAND_SPECS), set(caps["verbs"]))
        for name, spec in static_contract.COMMAND_SPECS.items():
            with self.subTest(command=name):
                cap = caps["verbs"][name]
                self.assertEqual(cap.get("flags", []), list(spec.flags))
                self.assertEqual(cap.get("args", []), list(spec.args))
                self.assertEqual(cap["mutates"], spec.mutates)
                self.assertEqual(cap["json"], spec.json)
                self.assertEqual(cap["exit_codes"], list(spec.exit_codes))
                if spec.subcommands:
                    self.assertEqual(static_contract.SUBCOMMANDS[name], spec.subcommands)
        help_output = static_contract.help_text()
        for name in static_contract.COMMAND_SPECS:
            self.assertIn(name, help_output)
        self.assertEqual(set(caps["global_flags"]), set(static_contract.GLOBAL_FLAG_SPECS))

    def test_capabilities_live_spoolctl_probe_can_flip_available(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cwd = Path(td)
            bindir = install_fake_spoolctl(cwd)
            caps = self.envelope(["capabilities", "--json"], cwd, extra_env={"PATH": str(bindir) + os.pathsep + os.environ.get("PATH", "")})
            self.assertTrue(caps["data"]["integrations"]["spoolctl"]["available"])
            self.assertEqual(caps["data"]["integrations"]["spoolctl"]["version"], "0.4.11")

    def test_spoolctl_flag_names_accepts_real_and_compact_shapes(self) -> None:
        self.assertEqual(spoolctl_module.spoolctl_flag_names(["--cwd", "--env", "--max-crashes"]), {"--cwd", "--env", "--max-crashes"})
        self.assertEqual(
            spoolctl_module.spoolctl_flag_names([{"flag": "--cwd"}, {"flag": "--env"}, {"flag": "--max-crashes"}]),
            {"--cwd", "--env", "--max-crashes"},
        )
        self.assertEqual(spoolctl_module.spoolctl_flag_names([{"name": "--cwd"}, 7, None, "--env"]), {"--env"})
        self.assertEqual(spoolctl_module.spoolctl_flag_names({"flag": "--cwd"}), set())

    def test_parse_spoolctl_contract_is_numeric_not_lexicographic(self) -> None:
        self.assertEqual(spoolctl_module.parse_spoolctl_contract(2), 2)
        self.assertEqual(spoolctl_module.parse_spoolctl_contract("2"), 2)
        self.assertEqual(spoolctl_module.parse_spoolctl_contract(" 2 "), 2)
        for value in (10, "10", 17, "17"):
            with self.subTest(value=value):
                self.assertGreater(
                    spoolctl_module.parse_spoolctl_contract(value),
                    spoolctl_module.parse_spoolctl_contract("2"),
                )

    def test_parse_spoolctl_contract_rejects_unusable_values_with_fields(self) -> None:
        for value in (None, "", "   ", "abc", "2.1", "v2", 0, -1, True, [], {"a": 1}):
            with self.subTest(value=value):
                with self.assertRaises(static_contract.EvalctlError) as ctx:
                    spoolctl_module.parse_spoolctl_contract(value)
                error = ctx.exception.error
                self.assertEqual(error["code"], "E_SPOOLCTL_INCOMPATIBLE")
                self.assertEqual(error["exit_code"], 3)
                self.assertIn("observed_contract", error)
                self.assertEqual(error["minimum_contract"], 2)
                json.dumps(error)

    def test_spoolctl_probe_accepts_real_compact_and_raw_capabilities(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cwd = Path(td)
            bindir = install_fake_spoolctl(cwd, capabilities_shape="real")
            caps = self.envelope(["capabilities", "--json"], cwd, extra_env={"PATH": str(bindir) + os.pathsep + os.environ.get("PATH", "")})
            self.assertEqual(caps["data"]["integrations"]["spoolctl"]["version"], "0.4.11")

        with tempfile.TemporaryDirectory() as td:
            cwd = Path(td)
            bindir = install_fake_spoolctl(cwd, version="0.4.11", capabilities_shape="compact")
            caps = self.envelope(["capabilities", "--json"], cwd, extra_env={"PATH": str(bindir) + os.pathsep + os.environ.get("PATH", "")})
            self.assertEqual(caps["data"]["integrations"]["spoolctl"]["version"], "0.4.11")

        with tempfile.TemporaryDirectory() as td:
            cwd = Path(td)
            bindir = install_fake_spoolctl(cwd, version="0.4.11", capabilities_shape="raw")
            caps = self.envelope(["capabilities", "--json"], cwd, extra_env={"PATH": str(bindir) + os.pathsep + os.environ.get("PATH", "")})
            self.assertEqual(caps["data"]["integrations"]["spoolctl"]["version"], "0.4.11")

    def test_spoolctl_probe_envelope_version_is_authoritative(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cwd = Path(td)
            bindir = install_fake_spoolctl(cwd, version="0.4.11", capabilities_shape="real", data_version="9.9.9")
            caps = self.envelope(["capabilities", "--json"], cwd, extra_env={"PATH": str(bindir) + os.pathsep + os.environ.get("PATH", "")})
            self.assertEqual(caps["data"]["integrations"]["spoolctl"]["version"], "0.4.11")

    def test_spoolctl_probe_capabilities_degrades_on_incompatible_shapes(self) -> None:
        cases = [
            {"version": "0.4.0"},
            {"contract_version": 1},
            {"contract_version": "1"},
            {"contract_version": None},
            {"contract_version": "two"},
            {"capability_flags": [{"flag": "--cwd"}, {"flag": "--env"}]},
            {"capability_flags": {"flag": "--cwd"}},
            {"include_version": False},
            {"capabilities_shape": "bad-json"},
            {"capabilities_shape": "error-envelope"},
            {"capabilities_shape": "exit4"},
        ]
        for kwargs in cases:
            with self.subTest(kwargs=kwargs):
                with tempfile.TemporaryDirectory() as td:
                    cwd = Path(td)
                    bindir = install_fake_spoolctl(cwd, **kwargs)
                    caps = self.envelope(["capabilities", "--json"], cwd, extra_env={"PATH": str(bindir) + os.pathsep + os.environ.get("PATH", "")})
                    self.assertEqual(caps["data"]["integrations"]["spoolctl"], {"available": False, "planned": False, "minimum_version": "0.4.11", "minimum_contract": 2})

    def test_spoolctl_probe_run_queue_hard_errors_on_incompatible_shapes(self) -> None:
        cases = [
            ({"version": "0.4.0"}, 3, "E_SPOOLCTL_INCOMPATIBLE"),
            ({"contract_version": 1}, 3, "E_SPOOLCTL_INCOMPATIBLE"),
            ({"contract_version": "1"}, 3, "E_SPOOLCTL_INCOMPATIBLE"),
            ({"contract_version": None}, 3, "E_SPOOLCTL_INCOMPATIBLE"),
            ({"contract_version": "two"}, 3, "E_SPOOLCTL_INCOMPATIBLE"),
            ({"capability_flags": [{"flag": "--cwd"}, {"flag": "--env"}]}, 3, "E_SPOOLCTL_INCOMPATIBLE"),
            ({"capability_flags": {"flag": "--cwd"}}, 3, "E_SPOOLCTL_INCOMPATIBLE"),
            ({"include_version": False}, 3, "E_SPOOLCTL_INCOMPATIBLE"),
            ({"capabilities_shape": "bad-json"}, 3, "E_SPOOLCTL_INCOMPATIBLE"),
            ({"capabilities_shape": "error-envelope"}, 3, "E_SPOOLCTL_INCOMPATIBLE"),
            ({"capabilities_shape": "exit4"}, 4, "E_JOB_TRANSIENT"),
        ]
        for kwargs, expect, code in cases:
            with self.subTest(kwargs=kwargs):
                with tempfile.TemporaryDirectory() as td:
                    cwd = Path(td)
                    self.envelope(["init", "--json"], cwd)
                    bindir = install_fake_spoolctl(cwd, **kwargs)
                    result = self.run_cli(["run", "code-review", "--run-id", "bad", "--queue", "spoolctl", "--json"], cwd, expect=expect, extra_env={"PATH": str(bindir) + os.pathsep + os.environ.get("PATH", "")})
                    self.assertEqual(json.loads(result.stdout)["errors"][0]["code"], code)

    def test_capabilities_reports_spoolctl_contract_fields(self) -> None:
        # Version alone cannot diagnose a contract mismatch, which is the exact
        # failure this work fixes.
        with tempfile.TemporaryDirectory() as td:
            cwd = Path(td)
            bindir = install_fake_spoolctl(cwd, contract_version="7")
            caps = self.envelope(["capabilities", "--json"], cwd,
                                 extra_env={"PATH": str(bindir) + os.pathsep + os.environ.get("PATH", "")})
            spool = caps["data"]["integrations"]["spoolctl"]
            self.assertEqual(spool, {"available": True, "planned": False, "minimum_version": "0.4.11",
                                     "minimum_contract": 2, "version": "0.4.11", "contract_version": 7})
            self.assertIsInstance(spool["contract_version"], int)
            self.assertIsInstance(spool["minimum_contract"], int)

        with tempfile.TemporaryDirectory() as td:
            cwd = Path(td)
            result = self.run_cli(["capabilities", "--json"], cwd, extra_env={"PATH": "/nonexistent"})
            self.assertEqual(result.returncode, 0)
            spool = json.loads(result.stdout)["data"]["integrations"]["spoolctl"]
            self.assertEqual(spool, {"available": False, "planned": False, "minimum_version": "0.4.11", "minimum_contract": 2})

    def test_spoolctl_accepts_contracts_newer_than_the_floor(self) -> None:
        # The defect this replaces was a forward-compatibility failure: evalctl
        # rejected a spoolctl contract newer than the one it was written
        # against. Pinning only today's value would reproduce the outage one
        # spoolctl release from now and call it passing, so the regression that
        # matters is "a contract we have never seen works", not "2 works".
        # Contract 10 does double duty: "10" < "2" as strings, so it also
        # proves the comparison is numeric. Do not simplify this to a single
        # current-value assertion.
        for contract in (2, "2", 10, "10", 17):
            with self.subTest(contract=contract):
                with tempfile.TemporaryDirectory() as td:
                    cwd = Path(td)
                    bindir = install_fake_spoolctl(cwd, contract_version=contract)
                    caps = self.envelope(["capabilities", "--json"], cwd,
                                         extra_env={"PATH": str(bindir) + os.pathsep + os.environ.get("PATH", "")})
                    spool = caps["data"]["integrations"]["spoolctl"]
                    self.assertTrue(spool["available"], spool)
                    self.assertEqual(spool["version"], "0.4.11")

    def test_spoolctl_gate_reports_three_distinguishable_causes(self) -> None:
        # One message for three unrelated causes told a user whose spoolctl was
        # merely a contract ahead that flags were missing. Each cause now names
        # itself and carries its own machine-readable fields.
        cases = [
            ({"version": "0.4.0"}, {"observed_version": "0.4.0", "minimum_version": "0.4.11"}),
            ({"contract_version": 1}, {"observed_contract": 1, "minimum_contract": 2}),
            ({"capability_flags": ["--cwd", "--env"]}, {"missing_flags": ["--max-crashes"]}),
        ]
        messages = set()
        for kwargs, expected_fields in cases:
            with self.subTest(kwargs=kwargs):
                with tempfile.TemporaryDirectory() as td:
                    cwd = Path(td)
                    self.envelope(["init", "--json"], cwd)
                    bindir = install_fake_spoolctl(cwd, **kwargs)
                    result = self.run_cli(["run", "code-review", "--run-id", "gate", "--queue", "spoolctl", "--json"], cwd, expect=3,
                                          extra_env={"PATH": str(bindir) + os.pathsep + os.environ.get("PATH", "")})
                    error = json.loads(result.stdout)["errors"][0]
                    self.assertEqual(error["code"], "E_SPOOLCTL_INCOMPATIBLE")
                    self.assertEqual(error["exit_code"], 3)
                    for key, value in expected_fields.items():
                        self.assertEqual(error[key], value)
                    messages.add(error["message"])
        self.assertEqual(len(messages), 3, messages)

    def test_spoolctl_probe_version_prefix_policy_is_pinned(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cwd = Path(td)
            bindir = install_fake_spoolctl(cwd, version="0.4", capabilities_shape="real")
            caps = self.envelope(["capabilities", "--json"], cwd, extra_env={"PATH": str(bindir) + os.pathsep + os.environ.get("PATH", "")})
            self.assertEqual(caps["data"]["integrations"]["spoolctl"], {"available": False, "planned": False, "minimum_version": "0.4.11", "minimum_contract": 2})

        with tempfile.TemporaryDirectory() as td:
            cwd = Path(td)
            bindir = install_fake_spoolctl(cwd, version="0.4.11-rc1", capabilities_shape="real")
            caps = self.envelope(["capabilities", "--json"], cwd, extra_env={"PATH": str(bindir) + os.pathsep + os.environ.get("PATH", "")})
            self.assertEqual(caps["data"]["integrations"]["spoolctl"], {"available": False, "planned": False, "minimum_version": "0.4.11", "minimum_contract": 2})

    def test_spoolctl_version_prerelease_policy(self) -> None:
        # An rc of the floor release precedes the floor release and is not it.
        # The old numeric-prefix helper mapped every one of these to (0, 4, 11)
        # and let a release candidate pass as the release.
        for value in ("0.4.11", "0.4.11+local", "0.4.11.0", "0.4.12rc1", "0.4.12-rc1", "0.5.0a1", "1.0.0"):
            with self.subTest(accept=value):
                self.assertTrue(spoolctl_module.spoolctl_version_supported(value))
        for value in ("0.4.11rc1", "0.4.11-rc1", "0.4.11.dev1", "0.4.11a1", "0.4.11b2", "0.4.10", "0.4.9", "0.4", "", "nonsense"):
            with self.subTest(reject=value):
                self.assertFalse(spoolctl_module.spoolctl_version_supported(value))

    def test_parse_spoolctl_version_separates_base_from_prerelease(self) -> None:
        self.assertEqual(spoolctl_module.parse_spoolctl_version("0.4.11"), ((0, 4, 11), False))
        self.assertEqual(spoolctl_module.parse_spoolctl_version("0.4.11+local"), ((0, 4, 11), False))
        self.assertEqual(spoolctl_module.parse_spoolctl_version("0.4.11rc1"), ((0, 4, 11), True))
        self.assertEqual(spoolctl_module.parse_spoolctl_version("0.4.11-rc1"), ((0, 4, 11), True))
        self.assertEqual(spoolctl_module.parse_spoolctl_version("0.4.11.dev1"), ((0, 4, 11), True))
        self.assertEqual(spoolctl_module.parse_spoolctl_version("0.5.0a1"), ((0, 5, 0), True))

    def test_capabilities_inferctl_probe_reports_only_compatible_preflight_available(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cwd = Path(td)
            bindir = install_fake_inferctl(cwd)
            caps = self.envelope(["capabilities", "--json"], cwd, extra_env={"PATH": str(bindir) + os.pathsep + os.environ.get("PATH", "")})
            self.assertEqual(caps["data"]["integrations"]["inferctl"]["available"], True)
            self.assertEqual(caps["data"]["integrations"]["inferctl"]["preflight"], True)

            bindir = install_fake_inferctl(cwd, capabilities_shape="missing-preflight")
            missing = self.envelope(["capabilities", "--json"], cwd, extra_env={"PATH": str(bindir) + os.pathsep + os.environ.get("PATH", "")})
            self.assertEqual(missing["data"]["integrations"]["inferctl"], {"available": False, "planned": True, "preflight": False, "route": True, "contract_version": "0.2"})

            bindir = install_fake_inferctl(cwd, capabilities_shape="invalid-json")
            invalid = self.envelope(["capabilities", "--json"], cwd, extra_env={"PATH": str(bindir) + os.pathsep + os.environ.get("PATH", "")})
            self.assertEqual(invalid["data"]["integrations"]["inferctl"], {"available": False, "planned": True})

            broken = cwd / "broken-bin"
            broken.mkdir()
            (broken / "inferctl").write_text("#!/bin/sh\nexit 0\n")
            nonexec = self.envelope(["capabilities", "--json"], cwd, extra_env={"PATH": str(broken)})
            self.assertEqual(nonexec["data"]["integrations"]["inferctl"], {"available": False, "planned": True})

    def test_fake_inferctl_fixture_preserves_contract_shapes(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cwd = Path(td)
            bindir = install_fake_inferctl(cwd)
            caps = run_fake_tool(bindir, "inferctl", ["capabilities", "--json"])
            data = caps["data"]
            self.assertEqual(data["contract_version"], "0.2")
            self.assertIsInstance(data["verbs"], list)
            verbs = {verb["name"]: verb for verb in data["verbs"]}
            self.assertIn("route", verbs)
            self.assertIn("preflight", verbs)
            self.assertIn("--prompt-file", {flag["name"] for flag in verbs["route"]["flags"]})
            self.assertIn("--allow-fallback", {flag["name"] for flag in verbs["preflight"]["flags"]})
            self.assertTrue(verbs["preflight"]["emits_data_on_failure"])

            preflight = run_fake_tool(bindir, "inferctl", ["preflight", "code", "--prompt-file", "task.txt", "--json"])
            preflight_data = preflight["data"]
            for key in ("route_decision", "route", "runnability", "policy", "runnable"):
                self.assertIn(key, preflight_data)
            self.assertEqual(preflight_data["route_decision"]["selected_backend"], "ollama")
            self.assertEqual(preflight_data["route_decision"]["selected_model"], "qwen3:8b")

            route = run_fake_tool(bindir, "inferctl", ["route", "code", "--prompt-file", "task.txt", "--json"])
            route_data = route["data"]
            self.assertEqual(route_data["decision"]["selected_backend"], "ollama")
            self.assertEqual(route_data["decision"]["selected_model"], "qwen3:8b")
            self.assertIsInstance(route_data["candidates"], list)

    def test_fake_inferctl_fixture_supports_required_failure_modes(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cwd = Path(td)
            bindir = install_fake_inferctl(cwd, capabilities_shape="missing-preflight", preflight_mode="nonzero-with-data", route_mode="nonzero-without-data")
            caps = run_fake_tool(bindir, "inferctl", ["capabilities", "--json"])
            self.assertNotIn("preflight", {verb["name"] for verb in caps["data"]["verbs"]})

            preflight = run_fake_tool(bindir, "inferctl", ["preflight", "code", "--prompt-file", "task.txt", "--json"], expect=3)
            self.assertFalse(preflight["data"]["runnable"])
            self.assertEqual(preflight["data"]["runnability"]["status"], "config_error")

            route = subprocess.run([str(bindir / "inferctl"), "route", "code", "--prompt-file", "task.txt", "--json"], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            self.assertEqual(route.returncode, 3)
            self.assertEqual(route.stdout, "")
            self.assertIn("route failed", route.stderr)

        with tempfile.TemporaryDirectory() as td:
            cwd = Path(td)
            bindir = install_fake_inferctl(cwd, capabilities_shape="preflight-only", envelope_shape=False)
            raw = run_fake_tool(bindir, "inferctl", ["capabilities", "--json"])
            self.assertIn("preflight", {verb["name"] for verb in raw["verbs"]})
            self.assertNotIn("route", {verb["name"] for verb in raw["verbs"]})

        with tempfile.TemporaryDirectory() as td:
            cwd = Path(td)
            bindir = install_fake_inferctl(cwd, capabilities_shape="invalid-json")
            bad = subprocess.run([str(bindir / "inferctl"), "capabilities", "--json"], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            self.assertEqual(bad.returncode, 0)
            with self.assertRaises(json.JSONDecodeError):
                json.loads(bad.stdout)

    def test_fake_spoolctl_fixture_db_is_concurrency_safe(self) -> None:
        # Regression for CI run 30072965277: a reader observed a partially written
        # .spoolctl.db and raised JSONDecodeError. Readers must never see a torn
        # file, and concurrent writers must not lose updates or reuse a job id.
        import concurrent.futures

        with tempfile.TemporaryDirectory() as td:
            cwd = Path(td)
            bindir = install_fake_spoolctl(cwd)
            db = cwd / ".spoolctl.db"
            workdir = cwd / "work"
            workdir.mkdir()

            def add(index: int) -> str:
                data = run_fake_tool(bindir, "spoolctl", [
                    "add", "--db", str(db), "--json", "--key", f"run:case-{index}",
                    "--cwd", str(workdir), "--timeout", "30",
                    "--", "/bin/sh", "-c", "exit 0",
                ])
                return data["data"]["job_id"]

            with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
                job_ids = list(pool.map(add, range(24)))

            self.assertEqual(len(set(job_ids)), 24, "concurrent add reused a job id")
            state = json.loads(db.read_text())
            self.assertEqual(len(state["jobs"]), 24, "concurrent add lost a job entry")
            self.assertEqual(set(state["jobs"]), set(job_ids))

            worker = subprocess.Popen([str(bindir / "spoolctl"), "work", "--db", str(db), "--drain"],
                                      text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                def read(job_id: str) -> str:
                    return run_fake_tool(bindir, "spoolctl", ["show", "--db", str(db), "--json", job_id])["data"]["state"]

                with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
                    states = list(pool.map(read, job_ids * 2))
            finally:
                worker.communicate(timeout=60)

            self.assertEqual(worker.returncode, 0)
            self.assertTrue(set(states) <= {"queued", "succeeded"}, f"unexpected states: {sorted(set(states))}")
            final = run_fake_tool(bindir, "spoolctl", ["wait", "--db", str(db), "--json", *job_ids])
            self.assertTrue(final["data"]["all_succeeded"])

    def test_envelope_normalization_keeps_semantic_meta(self) -> None:
        payload = {
            "ok": True,
            "data": {"runs": []},
            "meta": {
                "request_id": "req_123",
                "ts_iso": "2026-07-23T00:00:00Z",
                "elapsed_ms": 7,
                "data_hash": "sha256:value",
                "contract_version": 1,
                "pagination": {"limit": 50, "cursor": None, "next_cursor": "abc", "has_more": True},
                "truncated": {"by_limit": True, "omitted": 17},
            },
        }
        normalized = self.normalize_envelope_semantic_meta(payload)
        self.assertNotIn("request_id", normalized["meta"])
        self.assertNotIn("ts_iso", normalized["meta"])
        self.assertNotIn("elapsed_ms", normalized["meta"])
        self.assertEqual(normalized["meta"]["pagination"]["next_cursor"], "abc")
        self.assertEqual(normalized["meta"]["truncated"]["omitted"], 17)
        self.assertEqual(normalized["meta"]["data_hash"], "sha256:value")

    def test_real_inferctl_shape_smoke_when_enabled(self) -> None:
        if not os.environ.get("EVALCTL_REAL_INFERCTL_SMOKE"):
            self.skipTest("set EVALCTL_REAL_INFERCTL_SMOKE=1 to compare fake inferctl shapes against a real binary")
        real = shutil.which("inferctl")
        if not real:
            self.skipTest("inferctl binary is not available on PATH")
        with tempfile.TemporaryDirectory() as td:
            cwd = Path(td)
            fake = install_fake_inferctl(cwd)
            fake_caps = run_fake_tool(fake, "inferctl", ["capabilities", "--json"])
            real_caps_result = subprocess.run([real, "capabilities", "--json"], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            self.assertEqual(real_caps_result.returncode, 0, msg=real_caps_result.stderr)
            real_caps = json.loads(real_caps_result.stdout)
            real_data = real_caps.get("data", real_caps)
            fake_data = fake_caps.get("data", fake_caps)
            self.assertIsInstance(real_data["verbs"], list)
            self.assertEqual({verb["name"] for verb in fake_data["verbs"]}, {verb["name"] for verb in fake_data["verbs"]} & {verb["name"] for verb in real_data["verbs"]})
            self.assertIn("preflight", {verb["name"] for verb in real_data["verbs"]})

    def test_run_captures_inferctl_preflight_provenance_without_report_drift(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cwd = Path(td)
            self.envelope(["init", "--json"], cwd)
            control = self.envelope(["run", "code-review", "--run-id", "control", "--json"], cwd, extra_env={"SOURCE_DATE_EPOCH": "1700000000"})
            bindir = install_fake_inferctl(cwd)
            env = {"PATH": str(bindir) + os.pathsep + os.environ.get("PATH", ""), "SOURCE_DATE_EPOCH": "1700000000"}
            captured = self.envelope(["run", "code-review", "--run-id", "with-inferctl", "--inferctl-task", "code", "--json"], cwd, extra_env=env)
            self.assertEqual(captured["data"]["report_hash"], control["data"]["report_hash"])
            self.assertNotIn("W_INFERCTL_CAPTURE_FAILED", {warning["code"] for warning in captured["warnings"]})

            run_dir = cwd / "evals" / "runs" / "with-inferctl"
            manifest = json.loads((run_dir / "manifest.json").read_text())
            run_inferctl = manifest["provenance"]["inferctl"]
            self.assertEqual(run_inferctl["actual_mode"], "preflight")
            self.assertEqual(run_inferctl["capture_modes"], ["preflight"])
            self.assertTrue(run_inferctl["route_available"])
            for case in manifest["cases"]:
                inferctl = case["provenance"]["inferctl"]
                self.assertTrue(inferctl["requested"])
                self.assertEqual(inferctl["task"], "code")
                self.assertEqual(inferctl["actual_mode"], "preflight")
                self.assertEqual(inferctl["selected_backend"], "ollama")
                self.assertEqual(inferctl["selected_model"], "qwen3:8b")
                self.assertTrue(inferctl["runnable"])
                self.assertTrue((run_dir / case["artifacts"]["inferctl_preflight"]).exists())
                self.assertTrue((run_dir / case["artifacts"]["inferctl_provenance"]).exists())
            self.assertFalse(list(run_dir.glob("cases/*/inferctl-route.json")))

    def test_inferctl_absent_and_incompatible_are_best_effort(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cwd = Path(td)
            self.envelope(["init", "--json"], cwd)
            absent = self.envelope(["run", "code-review", "--run-id", "absent", "--inferctl-task", "code", "--json"], cwd, extra_env={"PATH": "/nonexistent"})
            self.assertTrue(absent["data"]["run"]["case_count"])
            self.assertEqual([w["code"] for w in absent["warnings"]].count("W_INFERCTL_ABSENT"), 1)
            absent_run = cwd / "evals" / "runs" / "absent"
            absent_manifest = json.loads((absent_run / "manifest.json").read_text())
            self.assertEqual(absent_manifest["provenance"]["inferctl"]["actual_mode"], "none")
            self.assertFalse(list(absent_run.glob("cases/*/inferctl-*")))

            bindir = install_fake_inferctl(cwd, capabilities_shape="missing-preflight")
            incompatible = self.envelope(["run", "code-review", "--run-id", "incompatible", "--inferctl-task", "code", "--json"], cwd, extra_env={"PATH": str(bindir) + os.pathsep + os.environ.get("PATH", "")})
            self.assertEqual([w["code"] for w in incompatible["warnings"]].count("W_INFERCTL_INCOMPATIBLE"), 1)
            incompatible_run = cwd / "evals" / "runs" / "incompatible"
            self.assertEqual(json.loads((incompatible_run / "manifest.json").read_text())["provenance"]["inferctl"]["actual_mode"], "none")
            self.assertFalse(list(incompatible_run.glob("cases/*/inferctl-*")))

    def test_inferctl_preflight_blocked_fallback_and_capture_failure_modes(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cwd = Path(td)
            self.envelope(["init", "--json"], cwd)

            bindir = install_fake_inferctl(cwd, preflight_mode="policy-blocked")
            blocked = self.envelope(["run", "code-review", "--run-id", "blocked", "--inferctl-task", "code", "--json"], cwd, extra_env={"PATH": str(bindir) + os.pathsep + os.environ.get("PATH", "")})
            self.assertIn("W_INFERCTL_PREFLIGHT_BLOCKED", {warning["code"] for warning in blocked["warnings"]})
            blocked_manifest = json.loads((cwd / "evals" / "runs" / "blocked" / "manifest.json").read_text())
            self.assertFalse(blocked_manifest["cases"][0]["provenance"]["inferctl"]["runnable"])

            bindir = install_fake_inferctl(cwd, preflight_mode="fallback")
            fallback = self.envelope(["run", "code-review", "--run-id", "fallback", "--inferctl-task", "code", "--json"], cwd, extra_env={"PATH": str(bindir) + os.pathsep + os.environ.get("PATH", "")})
            self.assertNotIn("W_INFERCTL_PREFLIGHT_BLOCKED", {warning["code"] for warning in fallback["warnings"]})
            fallback_manifest = json.loads((cwd / "evals" / "runs" / "fallback" / "manifest.json").read_text())
            self.assertTrue(fallback_manifest["cases"][0]["provenance"]["inferctl"]["fallback_selected"])

            bindir = install_fake_inferctl(cwd, preflight_mode="invalid-json")
            failed = self.envelope(["run", "code-review", "--run-id", "capture-failed", "--inferctl-task", "code", "--json"], cwd, extra_env={"PATH": str(bindir) + os.pathsep + os.environ.get("PATH", "")})
            self.assertEqual([w["code"] for w in failed["warnings"]].count("W_INFERCTL_CAPTURE_FAILED"), 1)
            failed_run = cwd / "evals" / "runs" / "capture-failed"
            self.assertTrue(list(failed_run.glob("cases/*/inferctl-error.json")))

    def test_inferctl_provenance_queued_resume_and_replay_boundaries(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cwd = Path(td)
            self.envelope(["init", "--json"], cwd)
            bindir = install_fake_inferctl(cwd)
            spoolctl = install_fake_spoolctl(cwd)
            env = {"PATH": str(bindir) + os.pathsep + str(spoolctl) + os.pathsep + os.environ.get("PATH", "")}

            in_process = self.envelope(["run", "code-review", "--run-id", "infer-sync", "--inferctl-task", "code", "--json"], cwd, extra_env=env)
            queued = self.envelope(["run", "code-review", "--run-id", "infer-queued", "--queue", "spoolctl", "--inferctl-task", "code", "--json"], cwd, extra_env=env)
            self.assertEqual(queued["data"]["report_hash"], in_process["data"]["report_hash"])
            queued_run = cwd / "evals" / "runs" / "infer-queued"
            self.assertTrue(list(queued_run.glob("cases/*/inferctl-preflight.json")))

            self.write_resume_runner(cwd)
            log_path = cwd / "runner.log"
            resume_env = dict(env)
            resume_env.update({"RUN_LOG": str(log_path), "SLEEP_CASE": "cr-pass"})
            proc_env = os.environ.copy()
            proc_env.update(resume_env)
            proc_env["PYTHONPATH"] = str(ROOT) + (os.pathsep + proc_env["PYTHONPATH"] if proc_env.get("PYTHONPATH") else "")
            proc = subprocess.Popen(
                CMD + ["run", "code-review", "--run-id", "infer-resume", "--jobs", "1", "--reservation-ttl", "1", "--inferctl-task", "code", "--json"],
                cwd=cwd,
                env=proc_env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            first_marker = cwd / "evals" / "runs" / "infer-resume" / "cases" / "cr-fail" / "state.json"
            deadline = time.time() + 10
            while time.time() < deadline and not first_marker.exists():
                time.sleep(0.05)
            self.assertTrue(first_marker.exists(), "first case never reached terminal marker")
            proc.terminate()
            try:
                proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.communicate(timeout=5)
            time.sleep(1.2)
            resume = self.envelope(["run", "--resume", "infer-resume", "--reservation-ttl", "1", "--json"], cwd, extra_env=env)
            self.assertTrue(resume["data"]["run"]["case_count"])
            resume_manifest = json.loads((cwd / "evals" / "runs" / "infer-resume" / "manifest.json").read_text())
            self.assertEqual([case["provenance"]["inferctl"]["actual_mode"] for case in resume_manifest["cases"]], ["preflight", "preflight"])

            replay = self.envelope(["replay", "--failed", "infer-sync", "--run-id", "infer-replay", "--json"], cwd, extra_env=env)
            self.assertEqual(replay["data"]["cases_replayed"], 1)
            replay_run = cwd / "evals" / "runs" / "infer-replay"
            self.assertFalse(list(replay_run.glob("cases/*/inferctl-*")))

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
            for sidecar in ["run.json", ".reservation.json", ".spoolctl.db"]:
                sidecar_path = copied / sidecar
                if sidecar_path.exists():
                    sidecar_path.unlink()
            for marker in copied.glob("cases/*/state.json"):
                marker.unlink()
            score_files = sorted(copied.glob("cases/*/score.json"))
            self.assertGreater(len(score_files), 1)
            score_files[0].unlink()
            for score_file in score_files[1:]:
                score_file.write_text("not json\n")
            shutil.rmtree(cwd / "evals")
            replay = self.envelope(["report", "--run-dir", str(copied), "--format", "json"], cwd)
            self.assertEqual(replay["data"]["report_hash"], original_hash)

    def test_run_metadata_sidecar_and_source_date_epoch(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cwd = Path(td)
            self.envelope(["init", "--json"], cwd)
            epoch_env = {"SOURCE_DATE_EPOCH": "1700000000"}
            run = self.envelope(["run", "code-review", "--run-id", "stable", "--jobs", "1", "--timeout", "17", "--json"], cwd, extra_env=epoch_env)
            run_dir = cwd / "evals" / "runs" / "stable"
            run_json = json.loads((run_dir / "run.json").read_text())
            manifest = json.loads((run_dir / "manifest.json").read_text())
            self.assertEqual(run_json["created_ts"], "2023-11-14T22:13:20Z")
            self.assertEqual(manifest["created_ts"], run_json["created_ts"])
            self.assertEqual(manifest["execution"], run_json["execution"])
            self.assertEqual(run_json["execution"], {"mode": "synchronous", "jobs": 1, "timeout_seconds": 17})
            self.assertEqual(run_json["suite_identity"]["suite_name"], "code-review")
            self.assertEqual(len(run_json["suite_identity"]["cases"]), 2)
            self.assertEqual(sorted((c["id"], c["input_hash"]) for c in manifest["cases"]), [tuple(item) for item in run_json["suite_identity"]["cases"]])
            self.assertEqual(run["data"]["report_hash"], "sha256:89f6dee9ee258d67c8d868bd4edbf7b0d90af0012cdab31b35ca030717bac88e")

            report_before = self.envelope(["report", "stable", "--format", "json"], cwd)
            (run_dir / "run.json").write_text(json.dumps({**run_json, "created_ts": "2099-01-01T00:00:00Z"}, indent=2, sort_keys=True) + "\n")
            for state_path in run_dir.glob("cases/*/state.json"):
                state = json.loads(state_path.read_text())
                state["completed_ts"] = "2099-01-01T00:00:00Z"
                state_path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")
            report_after = self.envelope(["report", "stable", "--format", "json"], cwd)
            self.assertEqual(report_after["data"]["report_hash"], report_before["data"]["report_hash"])

            for case in manifest["cases"]:
                marker = json.loads((run_dir / "cases" / case["id"] / "state.json").read_text())
                self.assertEqual(marker["id"], case["id"])
                self.assertEqual(marker["status"], case["status"])
                self.assertIn(marker["status"], {"pass", "fail", "error"})

    def test_resume_reuses_terminal_cases_and_cleans_partial_case(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cwd = Path(td)
            self.envelope(["init", "--json"], cwd)
            self.write_resume_runner(cwd)
            log_path = cwd / "runner.log"
            env = os.environ.copy()
            env["PYTHONPATH"] = str(ROOT) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
            env.update({"SOURCE_DATE_EPOCH": "1700000000", "RUN_LOG": str(log_path), "SLEEP_CASE": "cr-pass"})
            proc = subprocess.Popen(
                CMD + ["run", "code-review", "--run-id", "resume-me", "--jobs", "1", "--reservation-ttl", "1", "--json"],
                cwd=cwd,
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            first_marker = cwd / "evals" / "runs" / "resume-me" / "cases" / "cr-fail" / "state.json"
            deadline = time.time() + 10
            while time.time() < deadline and not first_marker.exists():
                time.sleep(0.05)
            self.assertTrue(first_marker.exists(), "first case never reached terminal marker")
            proc.terminate()
            try:
                proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.communicate(timeout=5)
            partial_case_dir = cwd / "evals" / "runs" / "resume-me" / "cases" / "cr-pass"
            partial_case_dir.mkdir(parents=True, exist_ok=True)
            (partial_case_dir / "partial-sentinel.txt").write_text("must be removed before resume\n")
            time.sleep(1.2)

            resume = self.envelope(["run", "--resume", "resume-me", "--reservation-ttl", "1", "--json"], cwd, extra_env={"SOURCE_DATE_EPOCH": "1800000000", "RUN_LOG": str(log_path)})
            self.assertTrue(resume["data"]["run"]["ok"])
            self.assertIn("W_RESERVATION_RECLAIMED", {w["code"] for w in resume["warnings"]})
            self.assertFalse((partial_case_dir / "partial-sentinel.txt").exists())
            self.assertEqual(log_path.read_text().splitlines(), ["cr-fail", "cr-pass"])
            manifest = json.loads((cwd / "evals" / "runs" / "resume-me" / "manifest.json").read_text())
            self.assertEqual(manifest["created_ts"], "2023-11-14T22:13:20Z")
            self.assertEqual(manifest["execution"]["jobs"], 1)
            self.assertEqual([case["id"] for case in manifest["cases"]], ["cr-fail", "cr-pass"])

            control = cwd / "control"
            control.mkdir()
            self.envelope(["init", "--json"], control)
            self.write_resume_runner(control)
            self.envelope(["run", "code-review", "--run-id", "resume-me", "--jobs", "1", "--reservation-ttl", "1", "--json"], control, extra_env={"SOURCE_DATE_EPOCH": "1700000000", "RUN_LOG": str(control / "runner.log")})
            self.assertEqual(
                (cwd / "evals" / "runs" / "resume-me" / "manifest.json").read_text(),
                (control / "evals" / "runs" / "resume-me" / "manifest.json").read_text(),
            )

    def test_resume_completed_and_corrupt_run_guards(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cwd = Path(td)
            self.envelope(["init", "--json"], cwd)
            self.envelope(["run", "code-review", "--run-id", "done", "--json"], cwd)
            manifest_path = cwd / "evals" / "runs" / "done" / "manifest.json"
            before = manifest_path.read_text()
            resumed = self.envelope(["run", "--resume", "done", "--json"], cwd)
            self.assertTrue(resumed["data"]["existing"])
            self.assertIn("W_RESUME_NOTHING_PENDING", {w["code"] for w in resumed["warnings"]})
            self.assertEqual(manifest_path.read_text(), before)

            corrupt = cwd / "evals" / "runs" / "corrupt"
            shutil.copytree(cwd / "evals" / "runs" / "done", corrupt)
            (corrupt / "manifest.json").unlink()
            (corrupt / "run.json").write_text("not json\n")
            bad = self.run_cli(["run", "--resume", "corrupt", "--json"], cwd, expect=1)
            self.assertEqual(json.loads(bad.stdout)["errors"][0]["code"], "E_RUN_CORRUPT")

    def test_jobs_list_get_and_prune_run_state(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cwd = Path(td)
            self.envelope(["init", "--json"], cwd)
            self.envelope(["run", "code-review", "--run-id", "completed", "--json"], cwd)
            runs = cwd / "evals" / "runs"

            live = runs / "live"
            live.mkdir(parents=True)
            artifacts.write_json(live / ".reservation.json", {
                "run_id": "live",
                "pid": 123,
                "host": "test",
                "started_ts": now_iso(),
                "heartbeat_ts": now_iso(),
                "ttl_seconds": 3600,
            })

            stale = runs / "stale"
            (stale / "cases" / "case-a").mkdir(parents=True)
            artifacts.write_json(stale / ".reservation.json", {
                "run_id": "stale",
                "pid": 123,
                "host": "test",
                "started_ts": "1970-01-01T00:00:00Z",
                "heartbeat_ts": "1970-01-01T00:00:00Z",
                "ttl_seconds": 1,
            })
            artifacts.write_json(stale / "cases" / "case-a" / "job.json", {"job_id": "job-1", "state": "queued"})
            (stale / ".spoolctl.db").write_text("queue state\n")

            orphaned = runs / "orphaned"
            orphaned.mkdir()

            listed = self.envelope(["jobs", "list", "--json"], cwd, extra_env={"PATH": "/nonexistent"})
            by_id = {item["run_id"]: item for item in listed["data"]["runs"]}
            self.assertEqual(listed["data"]["count"], 4)
            self.assertEqual(listed["data"]["total_count"], 4)
            self.assertEqual(listed["meta"]["pagination"], {"limit": 50, "cursor": None, "next_cursor": None, "has_more": False})
            self.assertEqual(listed["meta"]["truncated"], {"by_limit": False, "omitted": 0})
            self.assertEqual(by_id["completed"]["state"], "completed")
            self.assertEqual(by_id["live"]["state"], "running")
            self.assertEqual(by_id["stale"]["state"], "stale")
            self.assertEqual(by_id["orphaned"]["state"], "orphaned")
            self.assertEqual(by_id["stale"]["queue_jobs"], [{"case_id": "case-a", "job_id": "job-1", "state": "queued", "spoolctl_available": False}])

            got = self.envelope(["jobs", "get", "completed", "--json"], cwd)
            self.assertEqual(got["data"]["state"], "completed")
            self.assertEqual(got["data"]["cases"]["terminal"], 2)

            bad_get = self.run_cli(["jobs", "get", "--json"], cwd, expect=1)
            self.assertEqual(json.loads(bad_get.stdout)["errors"][0]["code"], "E_CASE_INVALID")
            bad_subcommand = self.run_cli(["jobs", "wat", "--json"], cwd, expect=1)
            self.assertEqual(json.loads(bad_subcommand.stdout)["errors"][0]["code"], "E_UNKNOWN_SUBCOMMAND")

            dry = self.envelope(["jobs", "prune", "--json"], cwd)
            self.assertFalse(dry["data"]["confirmed"])
            self.assertTrue((stale / ".reservation.json").exists())
            self.assertTrue(orphaned.exists())

            pruned = self.envelope(["jobs", "prune", "--yes", "--json"], cwd)
            self.assertEqual(pruned["data"]["removed"]["reservations"], ["stale"])
            self.assertEqual(pruned["data"]["removed"]["runs"], ["orphaned"])
            self.assertTrue((runs / "completed").exists())
            self.assertTrue(live.exists())
            self.assertTrue(stale.exists())
            self.assertFalse((stale / ".reservation.json").exists())
            self.assertTrue((stale / ".spoolctl.db").exists())
            self.assertFalse(orphaned.exists())

    def test_jobs_list_is_bounded_and_cursor_paginated(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cwd = Path(td)
            runs = cwd / "evals" / "runs"
            runs.mkdir(parents=True)
            for idx in range(55):
                (runs / f"run-{idx:03d}").mkdir()

            first = self.envelope(["jobs", "list", "--json"], cwd)
            self.assertEqual(first["data"]["count"], 50)
            self.assertEqual(first["data"]["total_count"], 55)
            self.assertEqual([item["run_id"] for item in first["data"]["runs"][:3]], ["run-000", "run-001", "run-002"])
            self.assertEqual(first["data"]["runs"][-1]["run_id"], "run-049")
            self.assertEqual(first["meta"]["pagination"], {"limit": 50, "cursor": None, "next_cursor": "run-049", "has_more": True})
            self.assertEqual(first["meta"]["truncated"], {"by_limit": True, "omitted": 5})
            self.assertEqual(first["commands"][0]["command"], "evalctl jobs list --limit 50 --cursor run-049 --json")

            second = self.envelope(["jobs", "list", "--limit", "10", "--cursor", first["meta"]["pagination"]["next_cursor"], "--json"], cwd)
            self.assertEqual([item["run_id"] for item in second["data"]["runs"]], ["run-050", "run-051", "run-052", "run-053", "run-054"])
            self.assertEqual(second["meta"]["pagination"], {"limit": 10, "cursor": "run-049", "next_cursor": None, "has_more": False})
            self.assertEqual(second["meta"]["truncated"], {"by_limit": False, "omitted": 0})

            pruned_cursor = self.envelope(["jobs", "list", "--limit", "3", "--cursor", "run-020a", "--json"], cwd)
            self.assertEqual([item["run_id"] for item in pruned_cursor["data"]["runs"]], ["run-021", "run-022", "run-023"])

            normalized = self.normalize_envelope_semantic_meta(first)
            self.assertEqual(normalized["meta"]["pagination"]["next_cursor"], "run-049")
            self.assertEqual(normalized["meta"]["truncated"]["omitted"], 5)
            self.assertIn("data_hash", normalized["meta"])

    def test_jobs_list_rejects_bad_pagination_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cwd = Path(td)
            for args in (
                ["jobs", "list", "--limit", "0", "--json"],
                ["jobs", "list", "--limit", "-1", "--json"],
                ["jobs", "list", "--limit", "nope", "--json"],
                ["jobs", "list", "--limit", "1001", "--json"],
            ):
                with self.subTest(args=args):
                    result = self.run_cli(args, cwd, expect=1)
                    payload = json.loads(result.stdout)
                    self.assertEqual(payload["errors"][0]["code"], "E_CASE_INVALID")

            get_bad = self.run_cli(["jobs", "get", "completed", "--limit", "5", "--json"], cwd, expect=1)
            get_error = json.loads(get_bad.stdout)["errors"][0]
            self.assertEqual(get_error["code"], "E_CASE_INVALID")
            self.assertEqual(get_error["corrected_command"], "evalctl jobs list --limit 50 --json")

            prune_bad = self.run_cli(["jobs", "prune", "--cursor", "run-049", "--json"], cwd, expect=1)
            prune_error = json.loads(prune_bad.stdout)["errors"][0]
            self.assertEqual(prune_error["code"], "E_CASE_INVALID")
            self.assertEqual(prune_error["corrected_command"], "evalctl jobs list --limit 50 --json")

    def test_doctor_reports_initialization_and_clean_state(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cwd = Path(td)
            fresh = self.envelope(["doctor", "--json"], cwd)
            self.assertEqual(fresh["data"]["operation_outcome"]["kind"], "degraded")
            self.assertEqual(fresh["data"]["components"]["suite_root"]["state"], "degraded")
            self.assertEqual(fresh["data"]["recommended_action"]["command"], "evalctl init --json")

            self.envelope(["init", "--json"], cwd)
            initialized = self.envelope(["doctor", "--json"], cwd, extra_env={"PATH": "/nonexistent"})
            self.assertEqual(initialized["data"]["operation_outcome"]["kind"], "healthy")
            self.assertEqual(initialized["data"]["components"]["suite_root"]["state"], "healthy")
            self.assertEqual(initialized["data"]["components"]["runs_root"]["state"], "healthy")
            self.assertEqual(initialized["data"]["components"]["spoolctl"]["state"], "not_configured")
            self.assertEqual(initialized["data"]["components"]["inferctl"]["state"], "not_configured")
            self.assertIn("evalctl jobs list --limit 50 --json", {command["command"] for command in initialized["commands"]})

    def test_doctor_reports_stale_reservations_and_scoped_components(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cwd = Path(td)
            self.envelope(["init", "--json"], cwd)
            stale = cwd / "evals" / "runs" / "stale"
            stale.mkdir(parents=True)
            artifacts.write_json(stale / ".reservation.json", {
                "run_id": "stale",
                "pid": 123,
                "host": "test",
                "started_ts": "1970-01-01T00:00:00Z",
                "heartbeat_ts": "1970-01-01T00:00:00Z",
                "ttl_seconds": 1,
            })
            doctor = self.envelope(["doctor", "--json"], cwd, extra_env={"PATH": "/nonexistent"})
            self.assertEqual(doctor["data"]["operation_outcome"]["kind"], "degraded")
            self.assertEqual(doctor["data"]["components"]["reservations"]["state"], "degraded")
            self.assertEqual(doctor["data"]["recommended_action"]["command"], "evalctl jobs prune --json")

            scoped = self.envelope(["doctor", "--component", "reservations", "--json"], cwd)
            self.assertEqual(set(scoped["data"]["components"]), {"reservations"})

            bad = self.run_cli(["doctor", "--component", "nope", "--json"], cwd, expect=1)
            bad_error = json.loads(bad.stdout)["errors"][0]
            self.assertEqual(bad_error["code"], "E_UNKNOWN_COMPONENT")
            self.assertIn("inferctl", bad_error["valid_values"])

    def test_doctor_reports_optional_integration_states(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cwd = Path(td)
            self.envelope(["init", "--json"], cwd)
            bindir = install_fake_spoolctl(cwd, capabilities_shape="bad-json")
            spool = self.envelope(["doctor", "--component", "spoolctl", "--json"], cwd, extra_env={"PATH": str(bindir) + os.pathsep + os.environ.get("PATH", "")})
            self.assertEqual(spool["data"]["components"]["spoolctl"]["state"], "degraded")
            self.assertTrue(spool["data"]["components"]["spoolctl"]["errors"])

        with tempfile.TemporaryDirectory() as td:
            cwd = Path(td)
            self.envelope(["init", "--json"], cwd)
            bindir = install_fake_spoolctl(cwd, capabilities_shape="sleep")
            sleepy = self.envelope(["doctor", "--component", "spoolctl", "--json"], cwd, extra_env={"PATH": str(bindir) + os.pathsep + os.environ.get("PATH", "")})
            self.assertEqual(sleepy["data"]["components"]["spoolctl"]["state"], "degraded")
            self.assertIn("timed out", sleepy["data"]["components"]["spoolctl"]["errors"][0]["message"])

        with tempfile.TemporaryDirectory() as td:
            cwd = Path(td)
            self.envelope(["init", "--json"], cwd)
            missing = self.envelope(["doctor", "--component", "inferctl", "--json"], cwd, extra_env={"PATH": "/nonexistent"})
            self.assertEqual(missing["data"]["components"]["inferctl"]["state"], "not_configured")

            bindir = install_fake_inferctl(cwd, capabilities_shape="missing-preflight")
            incompatible = self.envelope(["doctor", "--component", "inferctl", "--json"], cwd, extra_env={"PATH": str(bindir) + os.pathsep + os.environ.get("PATH", "")})
            self.assertEqual(incompatible["data"]["components"]["inferctl"]["state"], "degraded")

            bindir = install_fake_inferctl(cwd, capabilities_shape="preflight-only")
            compatible = self.envelope(["doctor", "--component", "inferctl", "--json"], cwd, extra_env={"PATH": str(bindir) + os.pathsep + os.environ.get("PATH", "")})
            inferctl = compatible["data"]["components"]["inferctl"]
            self.assertEqual(inferctl["state"], "healthy")
            self.assertFalse(inferctl["observed"]["route_available"])

    def test_doctor_spoolctl_recommends_a_fix_not_another_diagnosis(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cwd = Path(td)
            self.envelope(["init", "--json"], cwd)
            bindir = install_fake_spoolctl(cwd, contract_version=1)
            env = {"PATH": str(bindir) + os.pathsep + os.environ.get("PATH", "")}
            doctor = self.envelope(["doctor", "--component", "spoolctl", "--json"], cwd, extra_env=env)
            spool = doctor["data"]["components"]["spoolctl"]
            self.assertEqual(spool["state"], "degraded")
            self.assertEqual(spool["observed"]["contract_version"], 1)
            self.assertEqual(spool["observed"]["minimum_contract"], 2)
            recommended = spool["recommended_action"]
            self.assertIn("0.4.11", recommended["command"])
            self.assertIn("run without --queue spoolctl", recommended["alternatives"])
            self.assertNotIn("evalctl doctor", recommended["command"])
            self.assertNotIn("evalctl doctor", doctor["data"]["recommended_action"]["command"])

        with tempfile.TemporaryDirectory() as td:
            cwd = Path(td)
            self.envelope(["init", "--json"], cwd)
            bindir = install_fake_spoolctl(cwd)
            env = {"PATH": str(bindir) + os.pathsep + os.environ.get("PATH", "")}
            healthy = self.envelope(["doctor", "--component", "spoolctl", "--json"], cwd, extra_env=env)
            observed = healthy["data"]["components"]["spoolctl"]["observed"]
            self.assertEqual(healthy["data"]["components"]["spoolctl"]["state"], "healthy")
            self.assertEqual(observed["contract_version"], 2)
            self.assertEqual(observed["version"], "0.4.11")

    def test_doctor_absent_spoolctl_recommends_an_installable_version(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cwd = Path(td)
            self.envelope(["init", "--json"], cwd)
            bindir = install_fake_spoolctl(cwd)
            env = {"PATH": str(bindir) + os.pathsep + os.environ.get("PATH", "")}
            self.run_cli(["run", "code-review", "--run-id", "queued", "--queue", "spoolctl", "--json"], cwd, extra_env=env)
            doctor = self.envelope(["doctor", "--component", "spoolctl", "--json"], cwd, extra_env={"PATH": "/nonexistent"})
            spool = doctor["data"]["components"]["spoolctl"]
            self.assertEqual(spool["state"], "degraded")
            recommended = spool["recommended_action"]
            # 0.4.1 through 0.4.10 were never published to PyPI, so the old
            # hint named a version the command it printed could not install.
            self.assertIn("0.4.11", recommended["command"])
            self.assertNotIn("evalctl doctor", recommended["command"])

    def test_doctor_exit_code_is_zero_even_with_an_unhealthy_component(self) -> None:
        # Pinned deliberately, not endorsed. evalctl exits 0 with a component in
        # `unhealthy` state while spoolctl exits 3 with data.ready. Whether
        # evalctl should follow is an open decision; this test exists so that
        # changing it is a decision rather than a discovered regression.
        with tempfile.TemporaryDirectory() as td:
            cwd = Path(td)
            self.envelope(["init", "--json"], cwd)
            bindir = install_fake_spoolctl(cwd)
            env = {"PATH": str(bindir) + os.pathsep + os.environ.get("PATH", "")}
            self.run_cli(["run", "code-review", "--run-id", "queued", "--queue", "spoolctl", "--json"], cwd, extra_env=env)
            bad = install_fake_spoolctl(cwd, contract_version=1)
            result = self.run_cli(["doctor", "--component", "spoolctl", "--json"], cwd,
                                  extra_env={"PATH": str(bad) + os.pathsep + os.environ.get("PATH", "")})
            payload = json.loads(result.stdout)
            self.assertEqual(payload["data"]["components"]["spoolctl"]["state"], "unhealthy")
            self.assertEqual(payload["data"]["operation_outcome"]["kind"], "unhealthy")
            self.assertEqual(result.returncode, 0)

    def test_plan_is_side_effect_free_and_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cwd = Path(td)
            self.envelope(["init", "--json"], cwd)
            runs = cwd / "evals" / "runs"
            before = sorted(p.name for p in runs.iterdir())

            first = self.envelope(["plan", "code-review", "--json"], cwd)
            second = self.envelope(["plan", "code-review", "--json"], cwd)
            self.assertEqual(sorted(p.name for p in runs.iterdir()), before)
            self.assertEqual(first["data"]["run"]["run_id"], None)
            self.assertEqual(first["data"]["run"]["run_id_strategy"], "generated_at_run_time")
            self.assertEqual(first["data"]["run"]["run_dir"], None)
            self.assertEqual(first["data"]["dependency_graph"], {"kind": "independent_cases", "edges": []})
            self.assertEqual(self.normalize_envelope_semantic_meta(first), self.normalize_envelope_semantic_meta(second))

            planned = self.envelope(["plan", "code-review", "--run-id", "planned", "--json"], cwd)
            self.assertEqual(planned["data"]["run"]["run_id"], "planned")
            self.assertEqual(planned["data"]["run"]["run_dir"], "evals/runs/planned")
            self.assertFalse((runs / "planned").exists())

            parallel = self.envelope(["plan", "code-review", "--jobs", "2", "--json"], cwd)
            self.assertEqual(parallel["data"]["plan"]["summary"]["parallel_tracks"], 2)
            self.assertEqual([track["id"] for track in parallel["data"]["plan"]["tracks"]], ["slot-1", "slot-2"])

            infer = self.envelope(["plan", "code-review", "--inferctl-task", "code", "--json"], cwd)
            self.assertTrue(all(case["provenance"]["inferctl"]["requested"] for case in infer["data"]["cases"]))
            self.assertFalse(list(runs.glob("*/cases/*/inferctl-*")))

            bad_flag = self.run_cli(["plan", "code-review", "--jsno"], cwd, expect=1)
            self.assertEqual(json.loads(bad_flag.stdout)["errors"][0]["code"], "E_UNKNOWN_FLAG")

    def test_plan_resume_and_blocked_queue_modes(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cwd = Path(td)
            self.envelope(["init", "--json"], cwd)
            self.write_resume_runner(cwd)
            log_path = cwd / "runner.log"
            env = os.environ.copy()
            env["PYTHONPATH"] = str(ROOT) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
            env.update({"SOURCE_DATE_EPOCH": "1700000000", "RUN_LOG": str(log_path), "SLEEP_CASE": "cr-pass"})
            proc = subprocess.Popen(
                CMD + ["run", "code-review", "--run-id", "resume-plan", "--jobs", "1", "--reservation-ttl", "1", "--json"],
                cwd=cwd,
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            first_marker = cwd / "evals" / "runs" / "resume-plan" / "cases" / "cr-fail" / "state.json"
            deadline = time.time() + 10
            while time.time() < deadline and not first_marker.exists():
                time.sleep(0.05)
            self.assertTrue(first_marker.exists(), "first case never reached terminal marker")
            proc.terminate()
            try:
                proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.communicate(timeout=5)
            time.sleep(1.2)

            resume_plan = self.envelope(["plan", "--resume", "resume-plan", "--json"], cwd)
            actions = {case["id"]: case["action"] for case in resume_plan["data"]["cases"]}
            self.assertEqual(actions["cr-fail"], "skip_terminal")
            self.assertEqual(actions["cr-pass"], "run")
            self.assertEqual(resume_plan["data"]["run"]["mode"], "resume")

            queued = self.envelope(["plan", "code-review", "--run-id", "queued-plan", "--queue", "spoolctl", "--json"], cwd, extra_env={"PATH": "/nonexistent"})
            self.assertEqual(queued["data"]["run"]["mode"], "blocked")
            self.assertEqual({case["action"] for case in queued["data"]["cases"]}, {"blocked"})
            self.assertIn("E_SPOOLCTL_UNAVAILABLE", {warning["code"] for warning in queued["warnings"]})
            self.assertFalse((cwd / "evals" / "runs" / "queued-plan").exists())

    def test_case_execution_phase_helpers_are_callable(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cwd = Path(td)
            self.envelope(["init", "--json"], cwd)
            suite_dir = self.suite_path(cwd)
            suite = self.load_suite(cwd)
            case = self.load_cases(cwd)[0]
            run_dir = cwd / "evals" / "runs" / "phases"
            run_dir.mkdir(parents=True)

            prepared = runner.prepare_case_workspace(suite_dir, suite, case, run_dir, None)
            self.assertTrue((prepared["case_dir"] / "workspace-before.json").exists())
            runner_result = runner.execute_runner_in_process(prepared)
            output_text, runner_json, normalize_warnings = runner.normalize_runner_artifacts(prepared, runner_result)
            self.assertEqual(normalize_warnings, [])
            self.assertEqual(runner_json["exit_code"], 0)
            entry, score_warnings = runner.capture_workspace_after_and_score(prepared, output_text, runner_json)
            self.assertEqual(score_warnings, [])
            self.assertIn(entry["status"], {"pass", "fail"})
            self.assertTrue((prepared["case_dir"] / "score.json").exists())
            self.assertFalse((prepared["case_dir"] / "state.json").exists())
            run_state.write_terminal_marker(prepared["case_dir"], case["id"], entry["status"])
            self.assertTrue((prepared["case_dir"] / "state.json").exists())

    def test_process_helper_covers_runner_and_scorer_lifecycle(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cwd = Path(td)
            env = os.environ.copy()

            success = run_process([sys.executable, "-c", "print('ok')"], shell=False, cwd=cwd, env=env, timeout=5)
            self.assertEqual(success.exit_code, 0)
            self.assertEqual(success.stdout, "ok\n")
            self.assertFalse(success.timed_out)
            self.assertFalse(success.spawn_failed)

            failed = run_process([sys.executable, "-c", "import sys; sys.exit(7)"], shell=False, cwd=cwd, env=env, timeout=5)
            self.assertEqual(failed.exit_code, 7)

            missing = run_process(["evalctl-definitely-missing-helper-binary"], shell=False, cwd=cwd, env=env, timeout=5)
            self.assertTrue(missing.spawn_failed)
            self.assertIsNone(missing.exit_code)

            stdin = run_process([sys.executable, "-c", "import sys; print(sys.stdin.read())"], shell=False, cwd=cwd, env=env, timeout=5, stdin_text="task text")
            self.assertEqual(stdin.stdout, "task text\n")

            no_stdin = run_process([sys.executable, "-c", "print('scorer-no-stdin')"], shell=False, cwd=cwd, env=env, timeout=5)
            self.assertEqual(no_stdin.stdout, "scorer-no-stdin\n")

            timeout = run_process([sys.executable, "-c", "import time; time.sleep(10)"], shell=False, cwd=cwd, env=env, timeout=0.2)
            self.assertTrue(timeout.timed_out)
            self.assertIsNone(timeout.exit_code)

    def test_process_helper_timeout_kills_pipe_holding_grandchild(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cwd = Path(td)
            marker = cwd / "grandchild-survived"
            grandchild = (
                "import pathlib, sys, time\n"
                "time.sleep(2)\n"
                "pathlib.Path(sys.argv[1]).write_text('alive')\n"
            )
            child = (
                "import subprocess, sys, time\n"
                f"subprocess.Popen([sys.executable, '-c', {grandchild!r}, sys.argv[1]])\n"
                "time.sleep(10)\n"
            )
            result = run_process([sys.executable, "-c", child, str(marker)], shell=False, cwd=cwd, env=os.environ.copy(), timeout=0.2)
            self.assertTrue(result.timed_out)
            time.sleep(2.5)
            self.assertFalse(marker.exists())

    def test_queue_spoolctl_matches_in_process_and_validates_backend(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cwd = Path(td)
            self.envelope(["init", "--json"], cwd)
            bindir = install_fake_spoolctl(cwd)
            queue_env = {"PATH": str(bindir) + os.pathsep + os.environ.get("PATH", "")}
            in_process = self.envelope(["run", "code-review", "--run-id", "sync", "--json"], cwd)
            queued = self.envelope(["run", "code-review", "--run-id", "queued", "--queue", "spoolctl", "--slots", "2", "--json"], cwd, extra_env=queue_env)
            self.assertEqual(queued["data"]["report_hash"], in_process["data"]["report_hash"])
            manifest = json.loads((cwd / "evals" / "runs" / "queued" / "manifest.json").read_text())
            self.assertEqual(manifest["execution"]["mode"], "queued")
            self.assertEqual(manifest["queue"]["backend"], "spoolctl")
            self.assertTrue((cwd / "evals" / "runs" / "queued" / ".spoolctl.db").exists())
            self.assertTrue(list((cwd / "evals" / "runs" / "queued" / "cases").glob("*/job.json")))

            missing = self.run_cli(["run", "code-review", "--run-id", "missing", "--queue", "spoolctl", "--json"], cwd, expect=3, extra_env={"PATH": "/nonexistent"})
            self.assertEqual(json.loads(missing.stdout)["errors"][0]["code"], "E_SPOOLCTL_UNAVAILABLE")
            bad_slots = self.run_cli(["run", "code-review", "--run-id", "slots", "--slots", "2", "--json"], cwd, expect=1)
            self.assertEqual(json.loads(bad_slots.stdout)["errors"][0]["code"], "E_CASE_INVALID")

        with tempfile.TemporaryDirectory() as td:
            cwd = Path(td)
            self.envelope(["init", "--json"], cwd)
            bindir = install_fake_spoolctl(cwd, version="0.4.0")
            incompatible = self.run_cli(["run", "code-review", "--run-id", "bad", "--queue", "spoolctl", "--json"], cwd, expect=3, extra_env={"PATH": str(bindir) + os.pathsep + os.environ.get("PATH", "")})
            self.assertEqual(json.loads(incompatible.stdout)["errors"][0]["code"], "E_SPOOLCTL_INCOMPATIBLE")

    def test_queue_spoolctl_outcome_mapping_and_stdin_wrapper(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cwd = Path(td)
            self.envelope(["init", "--json"], cwd)
            bindir = install_fake_spoolctl(cwd)
            queue_env = {"PATH": str(bindir) + os.pathsep + os.environ.get("PATH", "")}
            self.keep_first_case_only(cwd)
            suite = self.load_suite(cwd)
            suite["runner"]["argv"] = [sys.executable, "-c", "import sys; sys.exit(7)"]
            suite["scorers"] = [{"name": "exit-code", "required": True}]
            self.write_suite(cwd, suite)
            cases = self.load_cases(cwd)
            cases[0]["expect"] = {"exit_code": 7}
            self.write_cases(cwd, cases)
            nonzero = self.envelope(["run", "code-review", "--run-id", "nonzero", "--queue", "spoolctl", "--json"], cwd, extra_env=queue_env)
            self.assertTrue(nonzero["data"]["run"]["ok"])
            runner_json = json.loads((cwd / "evals" / "runs" / "nonzero" / "cases" / cases[0]["id"] / "runner.json").read_text())
            self.assertEqual(runner_json["exit_code"], 7)
            self.assertIsNone(runner_json["error_code"])

            suite["runner"]["argv"] = [sys.executable, "-c", "import time; time.sleep(2)"]
            suite["runner"]["timeout_seconds"] = 1
            self.write_suite(cwd, suite)
            timeout = self.envelope(["run", "code-review", "--run-id", "timeout-q", "--queue", "spoolctl", "--json"], cwd, extra_env=queue_env)
            self.assertEqual(timeout["data"]["run"]["status_counts"]["error"], 1)
            timed_runner = json.loads((cwd / "evals" / "runs" / "timeout-q" / "cases" / cases[0]["id"] / "runner.json").read_text())
            self.assertEqual(timed_runner["error_code"], "E_RUNNER_TIMEOUT")

            suite["runner"]["argv"] = ["evalctl-missing-spool-runner"]
            suite["runner"]["timeout_seconds"] = 30
            self.write_suite(cwd, suite)
            spawn = self.envelope(["run", "code-review", "--run-id", "spawn-q", "--queue", "spoolctl", "--json"], cwd, extra_env=queue_env)
            self.assertEqual(spawn["data"]["run"]["status_counts"]["error"], 1)
            spawn_runner = json.loads((cwd / "evals" / "runs" / "spawn-q" / "cases" / cases[0]["id"] / "runner.json").read_text())
            self.assertEqual(spawn_runner["error_code"], "E_RUNNER_FAILED")

            transient = self.run_cli(["run", "code-review", "--run-id", "transient", "--queue", "spoolctl", "--json"], cwd, expect=4, extra_env={**queue_env, "FAKE_SPOOLCTL_TRANSIENT": "1"})
            self.assertEqual(json.loads(transient.stdout)["errors"][0]["code"], "E_JOB_TRANSIENT")

        with tempfile.TemporaryDirectory() as td:
            cwd = Path(td)
            bindir = install_fake_spoolctl(cwd)
            queue_env = {"PATH": str(bindir) + os.pathsep + os.environ.get("PATH", "")}
            self.envelope(["suite", "add", "stdin-demo", "--runner-argv", f"{sys.executable} $EVALCTL_WORKSPACE/r.py", "--json"], cwd)
            suite_dir = cwd / "evals" / "suites" / "stdin-demo"
            fixture = suite_dir / "fixtures" / "x"
            fixture.mkdir(parents=True)
            (fixture / "r.py").write_text("from pathlib import Path\nimport os, sys\ntext=sys.stdin.read()\nPath(os.environ['EVALCTL_OUTPUT_FILE']).write_text(text)\n")
            suite = json.loads((suite_dir / "suite.json").read_text())
            suite["runner"]["stdin"] = "task"
            (suite_dir / "suite.json").write_text(json.dumps(suite, indent=2, sort_keys=True) + "\n")
            self.envelope(["case", "add", "stdin-demo", "--id", "x", "--task", "hello stdin", "--workspace", "fixtures/x", "--expect-json", '{"exact":"hello stdin"}', "--json"], cwd)
            self.envelope(["scorer", "add", "stdin-demo", "--name", "exact", "--required", "--json"], cwd)
            stdin_run = self.envelope(["run", "stdin-demo", "--run-id", "stdin-q", "--queue", "spoolctl", "--json"], cwd, extra_env=queue_env)
            self.assertTrue(stdin_run["data"]["run"]["ok"])

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

    def test_documented_invalid_input_enumeration(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cwd = Path(td)
            self.envelope(["init", "--json"], cwd)
            self.envelope(["run", "code-review", "--run-id", "existing", "--json"], cwd)
            runs_root = cwd / "evals" / "runs"

            command_bases = {
                "plan": ["plan", "code-review"],
                "run": ["run", "code-review"],
                "jobs": ["jobs", "list"],
                "replay": ["replay", "--failed", "existing", "--run-id", "replay-dest"],
                "suite": ["suite", "add", "demo"],
                "case": ["case", "add", "code-review"],
                "scorer": ["scorer", "add", "code-review"],
                "status": ["status", "existing"],
                "report": ["report", "existing"],
                "doctor": ["doctor"],
            }
            value_flags = {
                name: [flag for flag, spec in static_contract.COMMAND_SPECS[name].flags.items() if spec.kind != "bool"]
                for name in command_bases
            }

            cases: list[tuple[str, list[str], str]] = []
            for name, flags in value_flags.items():
                for flag in flags:
                    base = command_bases[name]
                    cases.append((f"{name} {flag} missing", [*base, flag, "--json"], "E_CASE_INVALID"))
                    cases.append((f"{name} {flag} empty", [*base, flag, "", "--json"], "E_CASE_INVALID"))
                    cases.append((f"{name} {flag} followed by known flag", [*base, flag, "--json"], "E_CASE_INVALID"))
            cases.extend([
                ("unknown command", ["stauts", "--json"], "E_UNKNOWN_COMMAND"),
                ("bad jobs limit", ["jobs", "list", "--limit", "1001", "--json"], "E_CASE_INVALID"),
                ("bad doctor component", ["doctor", "--component", "wat", "--json"], "E_UNKNOWN_COMPONENT"),
                ("bad report format", ["report", "existing", "--format", "xml", "--json"], "E_CASE_INVALID"),
                ("bad scorer name", ["scorer", "add", "code-review", "--name", "wat", "--json"], "E_CASE_INVALID"),
                ("unknown run flag", ["run", "code-review", "--bogus", "--json"], "E_UNKNOWN_FLAG"),
                ("unknown init flag", ["init", "--forse", "--json"], "E_UNKNOWN_FLAG"),
                ("unknown validate flag", ["validate", "--bogus", "--json"], "E_UNKNOWN_FLAG"),
                ("unknown status flag", ["status", "--bogus", "--json"], "E_UNKNOWN_FLAG"),
                ("unknown report flag", ["report", "nosuchrun", "--bogus", "--json"], "E_UNKNOWN_FLAG"),
                ("unknown suite flag", ["suite", "add", "demo", "--runner-argv", "python3 r.py", "--bogus", "--json"], "E_UNKNOWN_FLAG"),
                ("unknown case flag", ["case", "add", "code-review", "--id", "zz", "--task", "t", "--workspace", "fixtures", "--bogus", "--json"], "E_UNKNOWN_FLAG"),
                ("unknown scorer flag", ["scorer", "add", "code-review", "--name", "exact", "--bogus", "--json"], "E_UNKNOWN_FLAG"),
            ])

            for label, args, code in cases:
                with self.subTest(label=label, args=args):
                    before_runs = sorted(path.name for path in runs_root.iterdir())
                    result = self.run_cli(args, cwd, expect=1)
                    payload = json.loads(result.stdout)
                    self.assertFalse(payload["ok"])
                    self.assertEqual(payload["errors"][0]["code"], code)
                    self.assertNotEqual(payload["errors"][0]["code"], "E_RUNNER_FAILED")
                    self.assertEqual(sorted(path.name for path in runs_root.iterdir()), before_runs)

    def test_malformed_value_flags_are_input_errors_before_state_or_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cwd = Path(td)
            self.envelope(["init", "--json"], cwd)
            self.envelope(["run", "code-review", "--run-id", "existing", "--json"], cwd)
            runs_root = cwd / "evals" / "runs"

            cases = [
                (["plan", "code-review", "--timeout", "nope", "--json"], "E_CASE_INVALID"),
                (["run", "code-review", "--timeout", "nope", "--json"], "E_CASE_INVALID"),
                (["plan", "code-review", "--timeout", "0", "--json"], "E_CASE_INVALID"),
                (["run", "code-review", "--timeout", "0", "--json"], "E_CASE_INVALID"),
                (["plan", "code-review", "--timeout", "-3", "--json"], "E_CASE_INVALID"),
                (["run", "code-review", "--timeout", "-3", "--json"], "E_CASE_INVALID"),
                (["run", "code-review", "--jobs", "-1", "--json"], "E_CASE_INVALID"),
                (["run", "code-review", "--queue", "spoolctl", "--slots", "-1", "--json"], "E_CASE_INVALID"),
                (["run", "code-review", "--run-id", "existing", "--timeout", "nope", "--json"], "E_CASE_INVALID"),
                (["run", "code-review", "--timeout", "--bogus", "--json"], "E_CASE_INVALID"),
                (["run", "code-review", "--run-id", "--json"], "E_CASE_INVALID"),
                (["run", "code-review", "--run-id", "--", "--json"], "E_CASE_INVALID"),
                (["run", "code-review", "--run-id", "", "--json"], "E_CASE_INVALID"),
                (["run", "code-review", "--resume", "--json"], "E_CASE_INVALID"),
                (["plan", "code-review", "--run-id", "", "--json"], "E_CASE_INVALID"),
                (["replay", "--failed", "existing", "--run-id", "bad-replay", "--timeout", "nope", "--json"], "E_CASE_INVALID"),
                (["scorer", "add", "code-review", "--name", "command", "--id", "judge", "--argv", "python3 scorer.py", "--timeout", "nope", "--json"], "E_CASE_INVALID"),
                (["report", "existing", "--format"], "E_CASE_INVALID"),
                (["plan", "code-review", "--format"], "E_UNKNOWN_FLAG"),
                (["plan", "code-review", "--format", "json", "--json"], "E_UNKNOWN_FLAG"),
            ]
            for args, code in cases:
                with self.subTest(args=args):
                    before_runs = sorted(path.name for path in runs_root.iterdir())
                    result = self.run_cli(args, cwd, expect=1)
                    payload = json.loads(result.stdout)
                    self.assertFalse(payload["ok"])
                    self.assertEqual(payload["errors"][0]["code"], code)
                    self.assertNotEqual(payload["errors"][0]["code"], "E_RUNNER_FAILED")
                    self.assertEqual(sorted(path.name for path in runs_root.iterdir()), before_runs)

            free_text = self.envelope(["case", "add", "code-review", "--id", "dash-task", "--task", "-fix the parser", "--workspace", "fixtures/cr-pass", "--json"], cwd)
            self.assertEqual(free_text["data"]["id"], "dash-task")
            self.assertFalse(suite_module.is_safe_id("--json"))

    def test_did_you_mean_for_unknown_commands_subcommands_and_flags(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cwd = Path(td)
            top = self.run_cli(["stauts", "--json"], cwd, expect=1)
            top_error = json.loads(top.stdout)["errors"][0]
            self.assertEqual(top_error["code"], "E_UNKNOWN_COMMAND")
            self.assertEqual(top_error["did_you_mean"], "status")
            self.assertEqual(top_error["corrected_command"], "evalctl status --json")
            self.assertIn("Did you mean: evalctl status --json", top.stderr)

            no_match = self.run_cli(["zzzzzz", "--json"], cwd, expect=1)
            no_match_error = json.loads(no_match.stdout)["errors"][0]
            self.assertEqual(no_match_error["code"], "E_UNKNOWN_COMMAND")
            self.assertNotIn("did_you_mean", no_match_error)
            self.assertIn("capabilities", no_match_error["hint"])

            jobs = self.run_cli(["jobs", "lsit", "--json"], cwd, expect=1)
            jobs_error = json.loads(jobs.stdout)["errors"][0]
            self.assertEqual(jobs_error["code"], "E_UNKNOWN_SUBCOMMAND")
            self.assertEqual(jobs_error["did_you_mean"], "list")
            self.assertEqual(jobs_error["corrected_command"], "evalctl jobs list --json")
            self.assertEqual(jobs_error["valid_values"], ["get", "list", "prune"])

            for args, corrected in (
                (["suite", "aad", "--json"], "evalctl suite add --json"),
                (["case", "aad", "--json"], "evalctl case add --json"),
                (["scorer", "addd", "--json"], "evalctl scorer add --json"),
                (["robot-docs", "guid", "--json"], "evalctl robot-docs guide --json"),
            ):
                with self.subTest(args=args):
                    result = self.run_cli(args, cwd, expect=1)
                    error = json.loads(result.stdout)["errors"][0]
                    self.assertEqual(error["code"], "E_UNKNOWN_SUBCOMMAND")
                    self.assertEqual(error["corrected_command"], corrected)

            run_flag = self.run_cli(["run", "code-review", "--run-idd", "oops", "--json"], cwd, expect=1)
            run_error = json.loads(run_flag.stdout)["errors"][0]
            self.assertEqual(run_error["code"], "E_UNKNOWN_FLAG")
            self.assertEqual(run_error["did_you_mean"], "--run-id")
            self.assertEqual(run_error["corrected_command"], "evalctl run code-review --run-id oops --json")

            replay_flag = self.run_cli(["replay", "--failed", "source", "--run-idd", "dest", "--json"], cwd, expect=1)
            replay_error = json.loads(replay_flag.stdout)["errors"][0]
            self.assertEqual(replay_error["code"], "E_UNKNOWN_FLAG")
            self.assertEqual(replay_error["did_you_mean"], "--run-id")

            jobs_flag = self.run_cli(["jobs", "list", "--jsno"], cwd, expect=1)
            jobs_flag_error = json.loads(jobs_flag.stdout)["errors"][0]
            self.assertEqual(jobs_flag_error["code"], "E_UNKNOWN_FLAG")
            self.assertEqual(jobs_flag_error["did_you_mean"], "--json")

            good = self.run_cli(["jobs", "list", "--json"], cwd)
            self.assertEqual(json.loads(good.stdout)["errors"], [])

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

    def test_replay_failed_reruns_recomputed_failed_subset_after_fix(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cwd = Path(td)
            self.envelope(["init", "--json"], cwd)
            source = self.envelope(["run", "code-review", "--run-id", "source", "--json"], cwd)
            self.assertEqual(source["data"]["run"]["status_counts"], {"error": 0, "fail": 1, "pass": 1})
            source_run = cwd / "evals" / "runs" / "source"
            for score_file in source_run.glob("cases/*/score.json"):
                score_file.write_text("not json\n")
            report = self.envelope(["report", "source", "--format", "json"], cwd)
            self.assertEqual([f["id"] for f in report["data"]["failures"]], ["cr-fail"])

            self.fix_cr_fail_runner(cwd)
            replay = self.envelope(["replay", "--failed", "source", "--run-id", "source-replay", "--json"], cwd)
            self.assertEqual(replay["data"]["replayed_from"], "source")
            self.assertEqual(replay["data"]["cases_replayed"], 1)
            self.assertEqual(replay["data"]["run"]["status_counts"], {"error": 0, "fail": 0, "pass": 1})
            manifest = json.loads((cwd / "evals" / "runs" / "source-replay" / "manifest.json").read_text())
            self.assertEqual(manifest["replayed_from"], "source")
            self.assertEqual(manifest["suite"]["case_count"], 1)
            self.assertEqual([c["id"] for c in manifest["cases"]], ["cr-fail"])

            green = self.envelope(["replay", "--failed", "source-replay", "--json"], cwd)
            self.assertEqual(green["data"], {"replayed_from": "source-replay", "cases_replayed": 0})
            self.assertIn("W_NOTHING_TO_REPLAY", {w["code"] for w in green["warnings"]})

    def test_replay_source_parser_and_overwrite_guards(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cwd = Path(td)
            self.envelope(["init", "--json"], cwd)
            self.keep_first_case_only(cwd)
            self.envelope(["run", "code-review", "--run-id", "green", "--json"], cwd)

            first = self.envelope(["replay", "green", "--failed", "--json"], cwd)
            second = self.envelope(["replay", "--failed", "green", "--json"], cwd)
            self.assertEqual(first["data"], {"replayed_from": "green", "cases_replayed": 0})
            self.assertEqual(second["data"], {"replayed_from": "green", "cases_replayed": 0})
            by_dir = self.envelope(["replay", "--failed", "--run-dir", str(cwd / "evals" / "runs" / "green"), "--run-id", "new-dest", "--json"], cwd)
            self.assertEqual(by_dir["data"]["cases_replayed"], 0)

            missing_failed = self.run_cli(["replay", "green", "--json"], cwd, expect=1)
            self.assertEqual(json.loads(missing_failed.stdout)["errors"][0]["code"], "E_CASE_INVALID")
            both_source_forms = self.run_cli(["replay", "--failed", "green", "--run-dir", str(cwd / "evals" / "runs" / "green"), "--json"], cwd, expect=1)
            self.assertEqual(json.loads(both_source_forms.stdout)["errors"][0]["code"], "E_CASE_INVALID")

            self.envelope(["init", "--force", "--json"], cwd)
            self.envelope(["run", "code-review", "--run-id", "source", "--json"], cwd)
            self.fix_cr_fail_runner(cwd)
            self.envelope(["replay", "--failed", "source", "--run-id", "dest", "--json"], cwd)
            conflict = self.run_cli(["replay", "--failed", "source", "--run-id", "dest", "--json"], cwd, expect=5)
            self.assertEqual(json.loads(conflict.stdout)["errors"][0]["code"], "E_RUN_CONFLICT")
            forced = self.envelope(["replay", "--failed", "source", "--run-id", "dest", "--force", "--json"], cwd)
            self.assertEqual(forced["data"]["run_id"], "dest")

            source_manifest = (cwd / "evals" / "runs" / "source" / "manifest.json").read_text()
            same = self.run_cli(["replay", "--failed", "source", "--run-id", "source", "--force", "--json"], cwd, expect=5)
            self.assertEqual(json.loads(same.stdout)["errors"][0]["code"], "E_RUN_CONFLICT")
            self.assertEqual((cwd / "evals" / "runs" / "source" / "manifest.json").read_text(), source_manifest)

    def test_replay_suite_override_and_absent_case_warning(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cwd = Path(td)
            self.envelope(["init", "--json"], cwd)
            suite_dir = self.suite_path(cwd)
            suite = self.load_suite(cwd)
            suite["name"] = "logical-name"
            self.write_suite(cwd, suite)
            self.envelope(["run", str(suite_dir), "--run-id", "path-source", "--json"], cwd)
            self.fix_cr_fail_runner(cwd)
            replay = self.envelope(["replay", "--failed", "path-source", "--suite", str(suite_dir), "--run-id", "path-replay", "--json"], cwd)
            self.assertEqual(replay["data"]["run"]["status_counts"], {"error": 0, "fail": 0, "pass": 1})

            self.envelope(["init", "--force", "--json"], cwd)
            self.envelope(["run", "code-review", "--run-id", "absent-source", "--json"], cwd)
            cases = self.load_cases(cwd)
            self.write_cases(cwd, [case for case in cases if case["id"] != "cr-fail"])
            absent = self.envelope(["replay", "--failed", "absent-source", "--run-id", "absent-replay", "--json"], cwd)
            codes = {w["code"] for w in absent["warnings"]}
            self.assertIn("W_REPLAY_CASE_ABSENT", codes)
            self.assertIn("W_NOTHING_TO_REPLAY", codes)

    def test_suite_add_creates_valid_empty_suite_and_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cwd = Path(td)
            created = self.envelope(["suite", "add", "demo", "--runner-argv", "python3 $EVALCTL_WORKSPACE/r.py", "--json"], cwd)
            self.assertTrue(created["data"]["created"])
            suite_dir = cwd / "evals" / "suites" / "demo"
            self.assertTrue((suite_dir / "suite.json").exists())
            self.assertTrue((suite_dir / "cases.jsonl").exists())
            self.assertTrue((suite_dir / "fixtures").is_dir())
            valid = self.envelope(["validate", "demo", "--json"], cwd)
            self.assertEqual(valid["data"], {"suite": "demo", "case_count": 0, "valid": True})

            existing = self.envelope(["suite", "add", "demo", "--runner-argv", "python3 $EVALCTL_WORKSPACE/r.py", "--json"], cwd)
            self.assertFalse(existing["data"]["created"])
            conflict = self.run_cli(["suite", "add", "demo", "--runner-argv", "python3 $EVALCTL_WORKSPACE/other.py", "--json"], cwd, expect=5)
            self.assertEqual(json.loads(conflict.stdout)["errors"][0]["code"], "E_RUN_CONFLICT")

    def test_suite_add_rejects_unsafe_names_and_bad_runner_grammar(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cwd = Path(td)
            bad_slash = self.run_cli(["suite", "add", "bad/name", "--runner-argv", "python3 x.py", "--json"], cwd, expect=1)
            self.assertEqual(json.loads(bad_slash.stdout)["errors"][0]["code"], "E_CASE_INVALID")
            bad_dotdot = self.run_cli(["suite", "add", "..", "--runner-argv", "python3 x.py", "--json"], cwd, expect=1)
            self.assertEqual(json.loads(bad_dotdot.stdout)["errors"][0]["code"], "E_CASE_INVALID")
            no_shell = self.run_cli(["suite", "add", "demo", "--runner-command", "python3 x.py", "--json"], cwd, expect=1)
            self.assertEqual(json.loads(no_shell.stdout)["errors"][0]["code"], "E_CASE_INVALID")
            both_forms = self.run_cli(["suite", "add", "demo", "--runner-argv", "python3 x.py", "--runner-command", "python3 x.py", "--shell", "--json"], cwd, expect=1)
            self.assertEqual(json.loads(both_forms.stdout)["errors"][0]["code"], "E_CASE_INVALID")

    def test_suite_add_temp_dir_is_removed_on_validation_failure(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cwd = Path(td)
            old_cwd = Path.cwd()
            os.chdir(cwd)
            try:
                def fail_validator(path: Path) -> dict:
                    raise RuntimeError("simulated validation failure")

                with self.assertRaises(RuntimeError):
                    suite_module.suite_add_data("demo", commands.runner_from_authoring_flags(["suite", "add", "demo", "--runner-argv", "python3 x.py"]), _validator=fail_validator)
                suites_root = cwd / "evals" / "suites"
                self.assertFalse((suites_root / "demo").exists())
                self.assertEqual([p.name for p in suites_root.iterdir()], [])
            finally:
                os.chdir(old_cwd)

    def test_case_add_appends_valid_case_and_is_idempotent_without_id(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cwd = Path(td)
            self.envelope(["suite", "add", "demo", "--runner-argv", "python3 $EVALCTL_WORKSPACE/r.py", "--json"], cwd)
            fixture = cwd / "evals" / "suites" / "demo" / "fixtures" / "x"
            fixture.mkdir(parents=True)
            added = self.envelope(["case", "add", "demo", "--task", "do X", "--workspace", "fixtures/x", "--expect-json", '{"exact":"ok"}', "--json"], cwd)
            case_id = added["data"]["id"]
            self.assertTrue(added["data"]["created"])
            valid = self.envelope(["validate", "demo", "--json"], cwd)
            self.assertEqual(valid["data"]["case_count"], 1)
            cases_path = cwd / "evals" / "suites" / "demo" / "cases.jsonl"
            first_text = cases_path.read_text()
            case = json.loads(first_text.strip())
            self.assertEqual(case["id"], case_id)
            self.assertEqual(case["expect"], {"exact": "ok"})

            existing = self.envelope(["case", "add", "demo", "--task", "do X", "--workspace", "fixtures/x", "--expect-json", '{"exact":"ok"}', "--json"], cwd)
            self.assertFalse(existing["data"]["created"])
            self.assertEqual(existing["data"]["id"], case_id)
            self.assertEqual(cases_path.read_text(), first_text)

            conflict = self.run_cli(["case", "add", "demo", "--id", case_id, "--task", "changed", "--workspace", "fixtures/x", "--json"], cwd, expect=5)
            self.assertEqual(json.loads(conflict.stdout)["errors"][0]["code"], "E_RUN_CONFLICT")

    def test_case_add_rejects_bad_workspace_paths_without_writing(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cwd = Path(td)
            self.envelope(["suite", "add", "demo", "--runner-argv", "python3 $EVALCTL_WORKSPACE/r.py", "--json"], cwd)
            cases_path = cwd / "evals" / "suites" / "demo" / "cases.jsonl"
            before = cases_path.read_text()
            for bad_path in ("../x", str(cwd / "outside"), "fixtures/missing"):
                result = self.run_cli(["case", "add", "demo", "--task", "do X", "--workspace", bad_path, "--json"], cwd, expect=1)
                self.assertEqual(json.loads(result.stdout)["errors"][0]["code"], "E_CASE_INVALID")
                self.assertEqual(cases_path.read_text(), before)

    def test_case_add_preserves_unrelated_case_lines(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cwd = Path(td)
            self.envelope(["suite", "add", "demo", "--runner-argv", "python3 $EVALCTL_WORKSPACE/r.py", "--json"], cwd)
            suite_dir = cwd / "evals" / "suites" / "demo"
            (suite_dir / "fixtures" / "one").mkdir(parents=True)
            (suite_dir / "fixtures" / "two").mkdir(parents=True)
            self.envelope(["case", "add", "demo", "--id", "one", "--task", "one", "--workspace", "fixtures/one", "--json"], cwd)
            cases_path = suite_dir / "cases.jsonl"
            first_line = cases_path.read_text().splitlines()[0]
            self.envelope(["case", "add", "demo", "--id", "two", "--task", "two", "--workspace", "fixtures/two", "--json"], cwd)
            self.assertEqual(cases_path.read_text().splitlines()[0], first_line)
            self.assertEqual(len(cases_path.read_text().splitlines()), 2)

    def test_scorer_add_appends_builtin_and_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cwd = Path(td)
            self.envelope(["suite", "add", "demo", "--runner-argv", "python3 $EVALCTL_WORKSPACE/r.py", "--json"], cwd)
            added = self.envelope(["scorer", "add", "demo", "--name", "exact", "--required", "--json"], cwd)
            self.assertTrue(added["data"]["created"])
            self.envelope(["validate", "demo", "--json"], cwd)
            suite = json.loads((cwd / "evals" / "suites" / "demo" / "suite.json").read_text())
            self.assertEqual(suite["scorers"], [{"name": "exact", "required": True}])
            before = (cwd / "evals" / "suites" / "demo" / "suite.json").read_text()
            existing = self.envelope(["scorer", "add", "demo", "--name", "exact", "--required", "--json"], cwd)
            self.assertFalse(existing["data"]["created"])
            self.assertEqual((cwd / "evals" / "suites" / "demo" / "suite.json").read_text(), before)

    def test_scorer_add_command_scorer_is_usable_by_run(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cwd = Path(td)
            self.envelope(["suite", "add", "demo", "--runner-argv", f"{sys.executable} $EVALCTL_WORKSPACE/r.py", "--json"], cwd)
            suite_dir = cwd / "evals" / "suites" / "demo"
            fixture = suite_dir / "fixtures" / "x"
            fixture.mkdir(parents=True)
            (fixture / "r.py").write_text("from pathlib import Path\nimport os\nPath(os.environ['EVALCTL_OUTPUT_FILE']).write_text('ok')\n")
            scorer_path = suite_dir / "judge.py"
            scorer_path.write_text("import json\nprint(json.dumps({'ok': True, 'score': 1, 'label': 'pass', 'findings': []}))\n")
            self.envelope(["case", "add", "demo", "--id", "x", "--task", "do X", "--workspace", "fixtures/x", "--json"], cwd)
            added = self.envelope(["scorer", "add", "demo", "--name", "command", "--id", "judge1", "--argv", f"{sys.executable} {scorer_path}", "--json"], cwd)
            self.assertTrue(added["data"]["created"])
            run = self.envelope(["run", "demo", "--run-id", "authored-command", "--json"], cwd)
            self.assertTrue(run["data"]["run"]["ok"])
            self.assertTrue((cwd / "evals" / "runs" / "authored-command" / "cases" / "x" / "scorers" / "judge1.json").exists())

    def test_scorer_add_conflicts_and_invalid_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cwd = Path(td)
            self.envelope(["suite", "add", "demo", "--runner-argv", "python3 $EVALCTL_WORKSPACE/r.py", "--json"], cwd)
            suite_path = cwd / "evals" / "suites" / "demo" / "suite.json"
            self.envelope(["scorer", "add", "demo", "--name", "contains", "--required", "--json"], cwd)
            before = suite_path.read_text()
            builtin_conflict = self.run_cli(["scorer", "add", "demo", "--name", "contains", "--advisory", "--json"], cwd, expect=5)
            self.assertEqual(json.loads(builtin_conflict.stdout)["errors"][0]["code"], "E_RUN_CONFLICT")
            self.assertEqual(suite_path.read_text(), before)

            self.envelope(["scorer", "add", "demo", "--name", "command", "--id", "judge", "--argv", "python3 scorer.py", "--json"], cwd)
            command_conflict = self.run_cli(["scorer", "add", "demo", "--name", "command", "--id", "judge", "--argv", "python3 other.py", "--json"], cwd, expect=5)
            self.assertEqual(json.loads(command_conflict.stdout)["errors"][0]["code"], "E_RUN_CONFLICT")
            unknown = self.run_cli(["scorer", "add", "demo", "--name", "wat", "--json"], cwd, expect=1)
            self.assertEqual(json.loads(unknown.stdout)["errors"][0]["code"], "E_CASE_INVALID")
            bad_id = self.run_cli(["scorer", "add", "demo", "--name", "command", "--id", "bad/id", "--argv", "python3 scorer.py", "--json"], cwd, expect=1)
            self.assertEqual(json.loads(bad_id.stdout)["errors"][0]["code"], "E_CASE_INVALID")

    def test_end_to_end_authoring_loop_without_json_edits(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cwd = Path(td)
            self.envelope(["suite", "add", "demo", "--runner-argv", f"{sys.executable} $EVALCTL_WORKSPACE/r.py", "--json"], cwd)
            fixture = cwd / "evals" / "suites" / "demo" / "fixtures" / "x"
            fixture.mkdir(parents=True)
            (fixture / "r.py").write_text(
                "from pathlib import Path\n"
                "import os\n"
                "Path(os.environ['EVALCTL_OUTPUT_FILE']).write_text('ok')\n"
            )
            self.envelope(["case", "add", "demo", "--task", "say ok", "--workspace", "fixtures/x", "--expect-json", '{"exact":"ok"}', "--json"], cwd)
            self.envelope(["scorer", "add", "demo", "--name", "exact", "--required", "--json"], cwd)
            run = self.envelope(["run", "demo", "--run-id", "authored", "--json"], cwd)
            self.assertTrue(run["data"]["run"]["ok"])
            self.assertEqual(run["data"]["run"]["status_counts"], {"error": 0, "fail": 0, "pass": 1})
            report = self.envelope(["report", "authored", "--format", "json"], cwd)
            self.assertTrue(report["data"]["run"]["ok"])
            self.assertEqual(len(report["data"]["cases"]), 1)
            self.assertEqual(report["data"]["cases"][0]["status"], "pass")
            self.assertTrue(report["data"]["cases"][0]["ok"])

    def test_atomic_write_keeps_final_json_intact_on_temp_failure(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "manifest.json"
            target.write_text('{"old": true}\n')

            def fail_after_temp_write(tmp_path: Path, text: str) -> None:
                tmp_path.write_text(text[:5])
                raise RuntimeError("simulated write failure")

            with self.assertRaises(RuntimeError):
                artifacts._atomic_write(target, '{"new": true}\n', _writer=fail_after_temp_write)

            self.assertEqual(target.read_text(), '{"old": true}\n')
            self.assertEqual(list(Path(td).glob("*.tmp")), [])

            absent = Path(td) / "absent.json"
            with self.assertRaises(RuntimeError):
                artifacts._atomic_write(absent, '{"new": true}\n', _writer=fail_after_temp_write)
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

    def test_command_scorer_artifact_replay_does_not_reexecute(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cwd = Path(td)
            self.envelope(["init", "--json"], cwd)
            self.keep_first_case_only(cwd)
            scorer_path = self.suite_path(cwd) / "scorer.py"
            scorer_path.write_text(
                "import json\n"
                "print(json.dumps({'ok': True, 'score': 1.0, 'label': 'pass', 'findings': []}))\n"
            )
            suite = self.load_suite(cwd)
            suite["scorers"] = [{"name": "command", "id": "judge", "required": True, "argv": [sys.executable, str(scorer_path)]}]
            self.write_suite(cwd, suite)

            run = self.envelope(["run", "code-review", "--run-id", "cmd-pass", "--json"], cwd)
            self.assertTrue(run["data"]["run"]["ok"])
            original_hash = run["data"]["report_hash"]
            case_dir = cwd / "evals" / "runs" / "cmd-pass" / "cases" / "cr-pass"
            verdict = json.loads((case_dir / "scorers" / "judge.json").read_text())
            self.assertEqual(verdict["id"], "judge")
            manifest = json.loads((cwd / "evals" / "runs" / "cmd-pass" / "manifest.json").read_text())
            self.assertEqual(manifest["cases"][0]["scores"][0]["id"], "judge")

            copied = cwd / "copied-command-run"
            shutil.copytree(cwd / "evals" / "runs" / "cmd-pass", copied)
            scorer_path.unlink()
            report = self.envelope(["report", "--run-dir", str(copied), "--format", "json"], cwd)
            self.assertEqual(report["data"]["report_hash"], original_hash)
            self.assertTrue(report["data"]["run"]["ok"])

    def test_command_scorer_failure_projection_includes_id(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cwd = Path(td)
            self.envelope(["init", "--json"], cwd)
            self.keep_first_case_only(cwd)
            pass_path = self.suite_path(cwd) / "pass_scorer.py"
            fail_path = self.suite_path(cwd) / "fail_scorer.py"
            pass_path.write_text("import json\nprint(json.dumps({'ok': True, 'score': 1, 'label': 'pass', 'findings': []}))\n")
            fail_path.write_text("import json\nprint(json.dumps({'ok': False, 'score': 0, 'label': 'fail', 'findings': [{'why': 'nope'}]}))\n")
            suite = self.load_suite(cwd)
            suite["scorers"] = [
                {"name": "command", "id": "judge-pass", "required": True, "argv": [sys.executable, str(pass_path)]},
                {"name": "command", "id": "judge-fail", "required": True, "argv": [sys.executable, str(fail_path)]},
            ]
            self.write_suite(cwd, suite)

            run = self.envelope(["run", "code-review", "--run-id", "cmd-two", "--json"], cwd)
            self.assertEqual(run["data"]["run"]["status_counts"], {"error": 0, "fail": 1, "pass": 0})
            report = self.envelope(["report", "cmd-two", "--format", "json"], cwd)
            score_ids = {score.get("id") for score in report["data"]["failures"][0]["scores"] if score["scorer"] == "command" and not score["ok"]}
            self.assertEqual(score_ids, {"judge-fail"})

    def test_command_scorer_verdict_normalization_and_status_mapping(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cwd = Path(td)
            self.envelope(["init", "--json"], cwd)
            self.keep_first_case_only(cwd)
            scorer_path = self.suite_path(cwd) / "verdict_scorer.py"
            suite = self.load_suite(cwd)
            variants = [
                ("bad-score", "{'ok': True, 'score': '1', 'label': 'pass', 'findings': []}", True, "error"),
                ("nan-score", '{"ok": true, "score": NaN, "label": "pass", "findings": []}', True, "error"),
                ("missing-ok", "{'score': 1, 'label': 'pass', 'findings': []}", True, "error"),
                ("extra-stdout", '{"ok": true, "score": 1, "label": "pass", "findings": []} trailing', True, "error"),
                ("error-true", "{'ok': True, 'score': 1, 'label': 'pass', 'findings': [], 'error': True}", True, "error"),
                ("required-fail", "{'ok': False, 'score': 0, 'label': 'fail', 'findings': []}", True, "fail"),
                ("advisory-fail", "{'ok': False, 'score': 0, 'label': 'fail', 'findings': []}", False, "pass"),
            ]
            for name, payload, required, expected_status in variants:
                if payload.startswith("{'"):
                    scorer_path.write_text(f"import json\nprint(json.dumps({payload}))\n")
                else:
                    scorer_path.write_text(f"print({payload!r})\n")
                suite["scorers"] = [{"name": "command", "id": "judge", "required": required, "argv": [sys.executable, str(scorer_path)]}]
                self.write_suite(cwd, suite)
                run = self.envelope(["run", "code-review", "--run-id", name, "--json"], cwd)
                self.assertEqual(run["data"]["run"]["status_counts"][expected_status], 1, msg=name)
                if expected_status == "error":
                    self.assertIn("W_PARTIAL_RUN", {w["code"] for w in run["warnings"]}, msg=name)

    def test_command_scorer_nonzero_and_fail_on_fail(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cwd = Path(td)
            self.envelope(["init", "--json"], cwd)
            self.keep_first_case_only(cwd)
            scorer_path = self.suite_path(cwd) / "bad_scorer.py"
            scorer_path.write_text("import sys\nprint('not json')\nsys.exit(2)\n")
            suite = self.load_suite(cwd)
            suite["scorers"] = [{"name": "command", "id": "judge", "required": True, "argv": [sys.executable, str(scorer_path)]}]
            self.write_suite(cwd, suite)

            run = self.envelope(["run", "code-review", "--run-id", "cmd-nonzero", "--json"], cwd)
            self.assertEqual(run["data"]["run"]["status_counts"], {"error": 1, "fail": 0, "pass": 0})
            self.assertEqual(run["errors"], [])
            self.assertIn("W_PARTIAL_RUN", {w["code"] for w in run["warnings"]})
            failed = self.envelope(["run", "code-review", "--run-id", "cmd-nonzero-fail", "--fail-on-fail", "--json"], cwd, expect=6)
            self.assertTrue(failed["ok"])
            self.assertEqual(failed["errors"], [])

    def test_command_scorer_timeout_kills_group_and_redacts_debug_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cwd = Path(td)
            self.envelope(["init", "--json"], cwd)
            self.keep_first_case_only(cwd)
            scorer_path = self.suite_path(cwd) / "timeout_scorer.py"
            grandchild = (
                "import pathlib, sys, time\n"
                "time.sleep(3)\n"
                "pathlib.Path(sys.argv[1]).write_text('alive')\n"
            )
            scorer_path.write_text(
                "import os, pathlib, subprocess, sys, time\n"
                "marker = pathlib.Path(os.environ['EVALCTL_WORKSPACE']) / 'command-scorer-grandchild'\n"
                f"subprocess.Popen([sys.executable, '-c', {grandchild!r}, str(marker)])\n"
                "print('SECRET1234567890-out', flush=True)\n"
                "print('SECRET1234567890-err', file=sys.stderr, flush=True)\n"
                "time.sleep(10)\n"
            )
            suite = self.load_suite(cwd)
            suite["runner"]["max_output_bytes"] = 20
            suite["runner"]["redact_patterns"] = ["SECRET[0-9]+"]
            suite["scorers"] = [{"name": "command", "id": "judge", "required": True, "argv": [sys.executable, str(scorer_path)], "timeout_seconds": 1}]
            self.write_suite(cwd, suite)

            run = self.envelope(["run", "code-review", "--run-id", "cmd-timeout", "--json"], cwd)
            self.assertEqual(run["data"]["run"]["status_counts"], {"error": 1, "fail": 0, "pass": 0})
            self.assertIn("W_PARTIAL_RUN", {w["code"] for w in run["warnings"]})
            case_dir = cwd / "evals" / "runs" / "cmd-timeout" / "cases" / "cr-pass"
            stdout = (case_dir / "scorers" / "judge.stdout.txt").read_text()
            stderr = (case_dir / "scorers" / "judge.stderr.txt").read_text()
            self.assertNotIn("SECRET", stdout)
            self.assertNotIn("SECRET", stderr)
            self.assertLessEqual(len(stdout.encode()), 20)
            self.assertLessEqual(len(stderr.encode()), 20)
            time.sleep(3.5)
            self.assertFalse((case_dir / "workspace" / "command-scorer-grandchild").exists())

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
