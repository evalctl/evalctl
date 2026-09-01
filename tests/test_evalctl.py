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
from tests.fakes import (REAL_SPOOLCTL_ATTEMPT_KEYS, REAL_SPOOLCTL_SHOW_KEYS, REAL_SPOOLCTL_TERMINAL_JOB_STATES,
                         install_fake_inferctl, install_fake_spoolctl, run_fake_tool)


ROOT = Path(__file__).resolve().parents[1]
CMD = [sys.executable, "-m", "evalctl"]
GOLDENS = ROOT / "tests" / "goldens"


def setUpModule() -> None:
    # run/replay refuse with exit 2 unless the invoker acknowledges the unsandboxed
    # runner. Tests drive real runners, so acknowledge by default; the gate itself is
    # tested explicitly by unsetting this in the relevant cases' env.
    os.environ["EVALCTL_ACKNOWLEDGE_UNSANDBOXED_RUNNER"] = "1"


class EvalctlCliTests(unittest.TestCase):
    def run_cli(self, args: list[str], cwd: Path, expect: int = 0, extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env["PYTHONPATH"] = str(ROOT) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        env.setdefault("EVALCTL_ACKNOWLEDGE_UNSANDBOXED_RUNNER", "1")
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
            for verb in sorted(static_contract.DATA_SCHEMAS):
                with self.subTest(schema=verb):
                    self.assert_json_golden(f"schema-{verb}.json", self.envelope(["schema", verb, "--json"], cwd, extra_env=env))
            self.assert_text_golden("robot-docs-guide.txt", self.run_cli(["robot-docs", "guide"], cwd, extra_env=env).stdout)
            self.assert_text_golden("help-run.txt", self.run_cli(["run", "--help"], cwd, extra_env=env).stdout)

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
            self.assertEqual(caps["meta"]["data_hash"], "sha256:3f8c84e559c77bc1f669b6bfb3a3cdf6e03fe1243c0883dc46a895d7d491d7a4")
            self.assertEqual(caps["tool_version"], "0.4.4")
            self.assertEqual(caps["data"]["integrations"]["spoolctl"], {"available": False, "planned": False, "minimum_version": "0.4.11", "minimum_contract": 2})
            self.assertIn("durable_runs", caps["data"]["features"])
            self.assertIn("queue_spoolctl", caps["data"]["features"])
            self.assertIn("inferctl_preflight_provenance", caps["data"]["features"])
            self.assertEqual(caps["data"]["error_codes"]["E_CASE_INVALID"]["surface"], "envelope")
            self.assertEqual(caps["data"]["error_codes"]["E_UNKNOWN_COMMAND"]["exit"], 1)
            self.assertEqual(caps["data"]["error_codes"]["E_UNSANDBOXED_RUNNER_UNACK"], {"class": "safety", "exit": 2, "retryable": False, "surface": "envelope", "where": ["run", "replay"]})
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
            self.assertEqual(schema["meta"]["data_hash"], "sha256:1a4336e28be06330270691a30c97846be21a45dcd8f4dc3906a6443b5d3c2b24")
            self.assertIn("run", schema["data"]["schemas"])
            run_schema = schema["data"]["schemas"]["run"]
            self.assertIn("properties", run_schema)
            self.assertIn("required", run_schema)
            self.assertTrue(run_schema["additionalProperties"])
            self.assertIn("queue", run_schema["properties"])
            jobs_schema = self.envelope(["schema", "jobs", "--json"], cwd)
            self.assertEqual(jobs_schema["meta"]["data_hash"], "sha256:bdc97a2dc8ace7c42b57317f118f772d1947898de8ed02a062399c812a3c06a3")
            self.assertIn("queue_jobs", jobs_schema["data"]["schemas"]["jobs"]["properties"])

            all_schemas = self.envelope(["schema", "--json"], cwd)
            for verb in ("capabilities", "schema", "init", "validate", "doctor", "plan", "run", "jobs", "replay", "suite", "case", "scorer", "status", "report"):
                verb_schema = all_schemas["data"]["schemas"][verb]
                self.assertIn("properties", verb_schema)
                self.assertIn("required", verb_schema)
                self.assertTrue(verb_schema["additionalProperties"])
            doctor_schema = self.envelope(["schema", "doctor", "--json"], cwd)
            self.assertEqual(doctor_schema["meta"]["data_hash"], "sha256:08c2d17ff5d14c58b8ede3c893e2a3a0eb230205ee6c1e2d81fba72a1b6616e3")
            self.assertIn("doctor", doctor_schema["data"]["schemas"])
            plan_schema = self.envelope(["schema", "plan", "--json"], cwd)
            self.assertEqual(plan_schema["meta"]["data_hash"], "sha256:9f2d8a9a5ab140c1b427cf2f528c6b63aeb3e91f00deb5757d2c3e3c2c8ffb32")
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

    def test_every_json_verb_has_a_pinned_schema_golden(self) -> None:
        # Every verb that emits a JSON data payload has a data schema, and every
        # data schema has a golden. Adding a JSON verb without either fails here.
        json_verbs = static_contract.VERB_NAMES - {"robot-docs"}
        self.assertEqual(set(static_contract.DATA_SCHEMAS), json_verbs)
        for verb in json_verbs:
            with self.subTest(verb=verb):
                self.assertTrue((GOLDENS / f"schema-{verb}.json").exists(), f"missing schema golden for {verb}")

    def test_schema_publishes_output_vocabularies_as_enums(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cwd = Path(td)
            schema = self.envelope(["schema", "--json"], cwd)["data"]
            definitions = schema["definitions"]
            self.assertEqual(definitions["case_status"]["enum"], ["error", "fail", "pass"])
            self.assertEqual(definitions["run_state"]["enum"], ["completed", "orphaned", "running", "stale"])
            self.assertEqual(definitions["plan_action"]["enum"], ["blocked", "run", "skip_terminal"])

            # No schema restates a vocabulary inline; the only enums in the whole
            # payload live in definitions, and every carrier reaches them by $ref.
            def refs_and_enums(node: object, in_definitions: bool = False) -> tuple[set[str], int]:
                refs: set[str] = set()
                enums = 0
                if isinstance(node, dict):
                    if "$ref" in node:
                        refs.add(node["$ref"])
                    if "enum" in node and not in_definitions:
                        enums += 1
                    for key, value in node.items():
                        child_refs, child_enums = refs_and_enums(value, in_definitions or key == "definitions")
                        refs |= child_refs
                        enums += child_enums
                elif isinstance(node, list):
                    for value in node:
                        child_refs, child_enums = refs_and_enums(value, in_definitions)
                        refs |= child_refs
                        enums += child_enums
                return refs, enums

            refs, inline_enums = refs_and_enums(schema)
            self.assertEqual(inline_enums, 0, "a schema restates an enum instead of referencing definitions")
            for name in definitions:
                self.assertIn(f"#/definitions/{name}", refs, f"{name} is published but never referenced")

            # Single-verb requests must still carry the definitions their refs need.
            self.assertEqual(self.envelope(["schema", "status", "--json"], cwd)["data"]["definitions"], definitions)

    def test_published_case_status_enum_is_exactly_what_a_run_produces(self) -> None:
        published = set(static_contract.DEFINITIONS["case_status"]["enum"])
        # The counter that a real run reports through status_counts is seeded with
        # exactly the status vocabulary; pin the published enum to it so neither
        # can gain a value the other lacks.
        self.assertEqual(set(run_state.status_counts([])), published)
        with tempfile.TemporaryDirectory() as td:
            cwd = Path(td)
            self.envelope(["init", "--json"], cwd)
            self.envelope(["run", "code-review", "--run-id", "vocab", "--json"], cwd)
            report = self.envelope(["report", "vocab", "--format", "json"], cwd)["data"]
            produced = {case["status"] for case in report["cases"]}
            self.assertTrue(produced <= published, f"run produced a status outside the published enum: {produced - published}")
            self.assertTrue(set(report["run"]["status_counts"]) <= published)

    def test_published_run_state_and_plan_action_enums_bound_real_output(self) -> None:
        run_states = set(static_contract.DEFINITIONS["run_state"]["enum"])
        plan_actions = set(static_contract.DEFINITIONS["plan_action"]["enum"])
        with tempfile.TemporaryDirectory() as td:
            cwd = Path(td)
            self.envelope(["init", "--json"], cwd)
            plan = self.envelope(["plan", "code-review", "--json"], cwd)["data"]
            self.assertTrue({case["action"] for case in plan["cases"]} <= plan_actions)
            self.envelope(["run", "code-review", "--run-id", "state-vocab", "--json"], cwd)
            status = self.envelope(["status", "state-vocab", "--json"], cwd)["data"]
            self.assertIn(status["state"], run_states)

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

    def test_published_exit_dictionary_matches_the_exits_verbs_declare(self) -> None:
        # F22 of the v0.4.3 conformance sweep: exit 2 was published as "safety
        # block" but no verb declared it and nothing could emit it -- a dead code
        # in the dictionary. Reconcile both directions so a dead code cannot be
        # published again, and so a verb cannot declare an exit the dictionary
        # never defined.
        caps = commands.capabilities_data()
        published = {int(k) for k in caps["exit_codes"]}
        declared = set()
        for spec in static_contract.COMMAND_SPECS.values():
            declared |= set(spec.exit_codes)
        self.assertEqual(
            published,
            declared,
            f"published exits {sorted(published)} != verb-declared exits {sorted(declared)}",
        )

    def test_per_verb_help_exists_and_never_mutates(self) -> None:
        # F12 of the v0.4.3 conformance sweep: --help was a global flag that only
        # the top-level dispatcher acted on, so `evalctl init --help` scaffolded a
        # tree and `evalctl run <suite> --help` executed the whole suite.
        for verb in sorted(static_contract.COMMAND_SPECS):
            for form in (["--help"], ["-h"]):
                with self.subTest(verb=verb, form=form[0]), tempfile.TemporaryDirectory() as td:
                    cwd = Path(td)
                    result = self.run_cli([verb, *form], cwd, extra_env={"PATH": "/nonexistent"})
                    self.assertTrue(result.stdout.startswith(f"evalctl {verb}  "), result.stdout[:120])
                    self.assertIn("AGENT/AUTOMATION:", result.stdout)
                    self.assertIn("Machine contract: evalctl capabilities --json", result.stdout)
                    self.assertEqual(sorted(p.name for p in cwd.iterdir()), [])

    def test_help_on_a_mutating_verb_leaves_the_project_untouched(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cwd = Path(td)
            env = {"PATH": "/nonexistent"}
            self.envelope(["init", "--json"], cwd, extra_env=env)
            runs = cwd / "evals" / "runs"
            before = sorted(p.name for p in runs.iterdir()) if runs.exists() else []
            self.run_cli(["run", "code-review", "--help"], cwd, extra_env=env)
            self.run_cli(["init", "--force", "--help"], cwd, extra_env=env)
            after = sorted(p.name for p in runs.iterdir()) if runs.exists() else []
            self.assertEqual(before, after)

    def test_help_detection_follows_the_flag_grammar(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cwd = Path(td)
            env = {"PATH": "/nonexistent"}
            self.envelope(["init", "--json"], cwd, extra_env=env)
            # --help as the value of a dash-tolerant flag is a value, not a request.
            missing = self.run_cli(["case", "add", "code-review", "--task", "--help", "--json"], cwd, expect=1, extra_env=env)
            self.assertEqual(json.loads(missing.stdout)["errors"][0]["flag"], "--task")
            # After --, it is a positional.
            after_terminator = self.run_cli(["status", "--", "--help", "--json"], cwd, expect=1, extra_env=env)
            self.assertNotIn("USAGE:", after_terminator.stdout)
            # A flag the verb does not have still reaches the parser.
            unknown = self.run_cli(["status", "--nope"], cwd, expect=1, extra_env=env)
            self.assertEqual(json.loads(unknown.stdout)["errors"][0]["code"], "E_UNKNOWN_FLAG")

    def test_json_flag_is_rejected_on_verbs_that_declare_no_envelope(self) -> None:
        # F23. The reject list is derived from CommandSpec.json, the same field
        # capabilities publishes, so the accepted set cannot drift from the
        # advertised one.
        caps = commands.capabilities_data()["verbs"]
        textual = sorted(name for name, spec in static_contract.COMMAND_SPECS.items() if not spec.json)
        self.assertEqual(textual, ["robot-docs"])
        with tempfile.TemporaryDirectory() as td:
            cwd = Path(td)
            for verb in textual:
                with self.subTest(verb=verb):
                    self.assertFalse(caps[verb]["json"])
                    result = self.run_cli([verb, "guide", "--json"], cwd, expect=1, extra_env={"PATH": "/nonexistent"})
                    error = json.loads(result.stdout)["errors"][0]
                    self.assertEqual(error["code"], "E_UNKNOWN_FLAG")
                    self.assertEqual(error["exit_code"], 1)
                    self.assertNotIn("--json", error["valid_values"])
                    # The fuzzy suggestion for --json is --version, which is valid
                    # syntax with unrelated semantics. It must not be offered.
                    self.assertNotIn("did_you_mean", error)
                    self.assertNotIn("corrected_command", error)

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
                    return run_fake_tool(bindir, "spoolctl", ["show", "--db", str(db), "--json", job_id])["data"]["job"]["state"]

                with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
                    states = list(pool.map(read, job_ids * 2))
            finally:
                worker.communicate(timeout=60)

            self.assertEqual(worker.returncode, 0)
            self.assertTrue(set(states) <= {"queued", "done"}, f"unexpected states: {sorted(set(states))}")
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

    def test_status_and_report_agree_with_jobs_get_on_an_in_flight_run(self) -> None:
        # While a run executes, `jobs get`, `status`, and `report` must not
        # disagree about whether the run exists. Before this fix status/report
        # keyed on manifest.json (written only at finalize) and returned
        # E_RUN_NOT_FOUND for a run `jobs get` reported as running.
        with tempfile.TemporaryDirectory() as td:
            cwd = Path(td)
            self.envelope(["init", "--json"], cwd)
            self.write_resume_runner(cwd)
            env = os.environ.copy()
            env["PYTHONPATH"] = str(ROOT) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
            env.update({"RUN_LOG": str(cwd / "runner.log"), "SLEEP_CASE": "cr-pass"})
            proc = subprocess.Popen(
                CMD + ["run", "code-review", "--run-id", "midflight", "--jobs", "1", "--reservation-ttl", "1", "--json"],
                cwd=cwd, env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            run_dir = cwd / "evals" / "runs" / "midflight"
            first_marker = run_dir / "cases" / "cr-fail" / "state.json"
            try:
                deadline = time.time() + 10
                while time.time() < deadline and not first_marker.exists():
                    time.sleep(0.05)
                # Mid-flight: one case is terminal, cr-pass is still sleeping, so
                # run.json exists but manifest.json (finalize-only) does not.
                self.assertTrue(first_marker.exists(), "first case never reached terminal marker")
                self.assertTrue((run_dir / "run.json").exists())
                self.assertFalse((run_dir / "manifest.json").exists())

                # (c) All three verbs agree the run exists.
                jobs_get = self.envelope(["jobs", "get", "midflight", "--json"], cwd)
                self.assertEqual(jobs_get["data"]["state"], "running")

                # (a) status returns exit 0 with live state and per-case progress.
                status = self.envelope(["status", "midflight", "--json"], cwd)
                self.assertEqual(status["data"]["run_id"], "midflight")
                self.assertEqual(status["data"]["state"], "running")
                self.assertEqual(status["data"]["progress"]["case_count"], 2)
                self.assertEqual(status["data"]["progress"]["terminal"], 1)
                self.assertEqual(status["data"]["progress"]["pending"], 1)

                # (b) report names the in-flight state; it must not be
                # E_RUN_NOT_FOUND, and it points the caller at status.
                report = self.envelope(["report", "midflight", "--format", "json"], cwd, expect=4)
                codes = {e["code"] for e in report["errors"]}
                self.assertIn("E_RUN_IN_FLIGHT", codes)
                self.assertNotIn("E_RUN_NOT_FOUND", codes)
                self.assertIn("evalctl status midflight", report["errors"][0]["hint"])
            finally:
                proc.terminate()
                try:
                    proc.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.communicate(timeout=5)

            # (e) E_RUN_NOT_FOUND is reserved for ids with no run directory at
            # all; all three verbs agree there too.
            absent = self.envelope(["jobs", "get", "no-such", "--json"], cwd, expect=1)
            self.assertEqual({e["code"] for e in absent["errors"]}, {"E_RUN_NOT_FOUND"})
            status_absent = self.envelope(["status", "no-such", "--json"], cwd, expect=1)
            self.assertEqual({e["code"] for e in status_absent["errors"]}, {"E_RUN_NOT_FOUND"})
            report_absent = self.envelope(["report", "no-such", "--format", "json"], cwd, expect=1)
            self.assertEqual({e["code"] for e in report_absent["errors"]}, {"E_RUN_NOT_FOUND"})

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

            # F4: a cursor the tool never issued (here a value that sorts between
            # real run ids but names no run) is a user-input error, not a silent
            # empty page. The tool only ever emits an existing run id as
            # next_cursor, so an unissued cursor cannot masquerade as done.
            bad_cursor = self.run_cli(["jobs", "list", "--limit", "3", "--cursor", "run-020a", "--json"], cwd, expect=1)
            bad_payload = json.loads(bad_cursor.stdout)
            self.assertEqual(bad_payload["errors"][0]["code"], "E_CASE_INVALID")
            self.assertIn("run-020a", bad_payload["errors"][0]["message"])

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

    def page_all_cases(self, cwd: Path, base_args: list[str]) -> list[dict]:
        """Walk every page of a per-case collection, following meta.pagination.
        Returns the concatenated `cases` across pages."""
        collected: list[dict] = []
        cursor = None
        while True:
            args = list(base_args) + ["--limit", "4"]
            if cursor is not None:
                args += ["--cursor", cursor]
            page = self.envelope(args, cwd)
            collected.extend(page["data"]["cases"])
            pagination = page["meta"]["pagination"]
            self.assertEqual(pagination["limit"], 4)
            if not pagination["has_more"]:
                self.assertIsNone(pagination["next_cursor"])
                self.assertFalse(page["meta"]["truncated"]["by_limit"])
                break
            self.assertTrue(page["meta"]["truncated"]["by_limit"])
            cursor = pagination["next_cursor"]
            self.assertIsNotNone(cursor)
        return collected

    def test_per_case_collections_page_every_case_exactly_once(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cwd = Path(td)
            self.envelope(["init", "--json"], cwd)
            base = self.load_cases(cwd)[0]
            expected_ids = [f"case-{i:03d}" for i in range(10)]
            self.write_cases(cwd, [{**base, "id": cid} for cid in expected_ids])

            # plan reads the suite directly (no run needed).
            plan_cases = self.page_all_cases(cwd, ["plan", "code-review", "--json"])
            plan_ids = [case["id"] for case in plan_cases]
            self.assertEqual(sorted(plan_ids), expected_ids)
            self.assertEqual(len(plan_ids), len(set(plan_ids)))

            # report and status page the same collection off a finalized run.
            self.envelope(["run", "code-review", "--run-id", "paged", "--json"], cwd)
            report_ids = [case["id"] for case in self.page_all_cases(cwd, ["report", "paged", "--format", "json"])]
            self.assertEqual(sorted(report_ids), expected_ids)
            self.assertEqual(len(report_ids), len(set(report_ids)))
            status_ids = [case["id"] for case in self.page_all_cases(cwd, ["status", "paged", "--json"])]
            self.assertEqual(sorted(status_ids), expected_ids)
            self.assertEqual(len(status_ids), len(set(status_ids)))

            # An unbounded call on a small suite carries no pagination meta, so it
            # stays byte-identical to pre-pagination output.
            self.keep_first_case_only(cwd)
            unbounded = self.envelope(["plan", "code-review", "--json"], cwd)
            self.assertNotIn("pagination", unbounded["meta"])
            self.assertNotIn("truncated", unbounded["meta"])

    def test_per_case_collections_reject_unissued_cursor(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cwd = Path(td)
            self.envelope(["init", "--json"], cwd)
            self.envelope(["run", "code-review", "--run-id", "paged", "--json"], cwd)
            for args in (
                ["plan", "code-review", "--cursor", "not-a-case", "--json"],
                ["report", "paged", "--cursor", "not-a-case", "--format", "json"],
                ["status", "paged", "--cursor", "not-a-case", "--json"],
            ):
                with self.subTest(args=args):
                    result = self.run_cli(args, cwd, expect=1)
                    payload = json.loads(result.stdout)
                    self.assertEqual(payload["errors"][0]["code"], "E_CASE_INVALID")
                    self.assertIn("not-a-case", payload["errors"][0]["message"])

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

    def test_plan_marks_a_live_reservation_as_an_external_blocker(self) -> None:
        # F3: plan must not propose work a live reservation would refuse. With a
        # live reservation held, plan marks the work blocked, carries a
        # branchable blocked_by_external field, and recommends inspecting the
        # blocker rather than the run command that would be refused. With the
        # reservation released, the plan returns to its normal shape.
        with tempfile.TemporaryDirectory() as td:
            cwd = Path(td)
            self.envelope(["init", "--json"], cwd)
            self.write_resume_runner(cwd)
            env = os.environ.copy()
            env["PYTHONPATH"] = str(ROOT) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
            env.update({"RUN_LOG": str(cwd / "runner.log"), "SLEEP_CASE": "cr-pass"})
            proc = subprocess.Popen(
                CMD + ["run", "code-review", "--run-id", "held", "--jobs", "1", "--reservation-ttl", "1", "--json"],
                cwd=cwd, env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            run_dir = cwd / "evals" / "runs" / "held"
            first_marker = run_dir / "cases" / "cr-fail" / "state.json"
            try:
                deadline = time.time() + 10
                while time.time() < deadline and not first_marker.exists():
                    time.sleep(0.05)
                self.assertTrue(first_marker.exists(), "first case never reached terminal marker")

                # While the reservation is live, resuming or re-running the id
                # would be refused, so the plan must say so as data.
                blocked = self.envelope(["plan", "--resume", "held", "--json"], cwd)
                self.assertEqual(blocked["data"]["run"]["mode"], "blocked")
                blocked_actions = {c["id"]: c["action"] for c in blocked["data"]["cases"]}
                self.assertEqual(blocked_actions["cr-pass"], "blocked")
                blocker = blocked["data"]["blocked_by_external"]
                self.assertEqual(blocker["kind"], "reservation")
                self.assertEqual(blocker["run_id"], "held")
                cmds = [c["command"] for c in blocked["commands"]]
                self.assertIn("evalctl status held --json", cmds)
                self.assertNotIn("evalctl run --resume held --json", cmds)
            finally:
                proc.terminate()
                try:
                    proc.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.communicate(timeout=5)

            # Reservation released (proc dead, ttl=1): plan returns to normal and
            # carries no blocker field.
            time.sleep(1.2)
            released = self.envelope(["plan", "--resume", "held", "--json"], cwd)
            self.assertEqual(released["data"]["run"]["mode"], "resume")
            self.assertNotIn("blocked_by_external", released["data"])
            self.assertIn("evalctl run --resume held --json", [c["command"] for c in released["commands"]])

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

    def fake_queue_attempt(self, cwd: Path, run_id: str, case_id: str) -> dict:
        """Return the raw attempt record the fake queue synthesized for a case.

        Read straight out of the fake's database rather than out of runner.json,
        because the point is what the fixture emitted, not what evalctl made of
        it.
        """
        db = json.loads((cwd / "evals" / "runs" / run_id / ".spoolctl.db").read_text())
        for job in db["jobs"].values():
            if job["key"].endswith(":" + case_id):
                return job["attempts"][-1]
        self.fail(f"fake queue has no job for case {case_id}")

    def test_fake_spoolctl_attempt_payload_matches_the_real_field_set(self) -> None:
        # The fixture used to be written from evalctl's expectations: it emitted
        # duration_ms, which real spoolctl has never emitted, and omitted
        # failure_reason, which real spoolctl always emits. That divergence hid
        # a live duration bug from the whole suite.
        with tempfile.TemporaryDirectory() as td:
            cwd = Path(td)
            self.envelope(["init", "--json"], cwd)
            bindir = install_fake_spoolctl(cwd)
            queue_env = {"PATH": str(bindir) + os.pathsep + os.environ.get("PATH", "")}
            self.keep_first_case_only(cwd)
            case_id = self.load_cases(cwd)[0]["id"]

            self.envelope(["run", "code-review", "--run-id", "shape", "--queue", "spoolctl", "--json"], cwd, extra_env=queue_env)
            attempt = self.fake_queue_attempt(cwd, "shape", case_id)
            self.assertEqual(set(attempt), set(REAL_SPOOLCTL_ATTEMPT_KEYS))
            self.assertNotIn("duration_ms", attempt)
            self.assertIsNone(attempt["failure_reason"])
            self.assertEqual(attempt["exit_code"], 0)
            self.assertEqual(attempt["state"], "succeeded")
            self.assertIsInstance(attempt["started_at"], float)
            self.assertIsInstance(attempt["finished_at"], float)
            self.assertGreaterEqual(attempt["finished_at"], attempt["started_at"])

            # Reasons must match what the real binary produces for the same
            # three failures, or the fake teaches the classification wrong rows.
            suite = self.load_suite(cwd)
            suite["runner"]["argv"] = [sys.executable, "-c", "import sys; sys.exit(7)"]
            self.write_suite(cwd, suite)
            self.envelope(["run", "code-review", "--run-id", "exit7", "--queue", "spoolctl", "--json"], cwd, extra_env=queue_env)
            exited = self.fake_queue_attempt(cwd, "exit7", case_id)
            self.assertEqual((exited["failure_reason"], exited["exit_code"]), ("process_exit", 7))

            suite["runner"]["argv"] = [sys.executable, "-c", "import time; time.sleep(30)"]
            suite["runner"]["timeout_seconds"] = 1
            self.write_suite(cwd, suite)
            self.envelope(["run", "code-review", "--run-id", "slow", "--queue", "spoolctl", "--json"], cwd, extra_env=queue_env)
            timed_out = self.fake_queue_attempt(cwd, "slow", case_id)
            self.assertEqual((timed_out["failure_reason"], timed_out["exit_code"]), ("timeout", None))

            suite["runner"]["argv"] = ["evalctl-no-such-runner-binary"]
            suite["runner"]["timeout_seconds"] = 30
            self.write_suite(cwd, suite)
            self.envelope(["run", "code-review", "--run-id", "nospawn", "--queue", "spoolctl", "--json"], cwd, extra_env=queue_env)
            unspawned = self.fake_queue_attempt(cwd, "nospawn", case_id)
            self.assertEqual((unspawned["failure_reason"], unspawned["exit_code"]), ("spawn_failed", None))

    def test_fake_spoolctl_knobs_rewrite_the_attempt_without_touching_the_subprocess(self) -> None:
        # The knobs exist so the fake can synthesize the reasons the real binary
        # cannot be driven to produce in CI. If they could also change whether
        # the command runs, the fixture would be faking the test's expectations
        # again rather than faking spoolctl.
        with tempfile.TemporaryDirectory() as td:
            cwd = Path(td)
            self.envelope(["init", "--json"], cwd)
            bindir = install_fake_spoolctl(cwd)
            queue_env = {"PATH": str(bindir) + os.pathsep + os.environ.get("PATH", "")}
            self.keep_first_case_only(cwd)
            case_id = self.load_cases(cwd)[0]["id"]
            marker = "knob-did-not-suppress-the-subprocess"
            suite = self.load_suite(cwd)
            suite["runner"]["argv"] = [sys.executable, "-c", f"print({marker!r})"]
            self.write_suite(cwd, suite)

            for run_id, knobs, expected_reason, expected_code in [
                ("knob-crash", {"FAKE_SPOOLCTL_FAILURE_REASON": "worker_crash"}, "worker_crash", 0),
                ("knob-future", {"FAKE_SPOOLCTL_FAILURE_REASON": "reason_from_a_future_spoolctl"}, "reason_from_a_future_spoolctl", 0),
                ("knob-blank-reason", {"FAKE_SPOOLCTL_FAILURE_REASON": ""}, None, 0),
                ("knob-code", {"FAKE_SPOOLCTL_EXIT_CODE": "42"}, None, 42),
                ("knob-blank-code", {"FAKE_SPOOLCTL_EXIT_CODE": ""}, None, None),
                ("knob-both", {"FAKE_SPOOLCTL_FAILURE_REASON": "canceled", "FAKE_SPOOLCTL_EXIT_CODE": ""}, "canceled", None),
            ]:
                with self.subTest(run_id=run_id):
                    self.envelope(["run", "code-review", "--run-id", run_id, "--queue", "spoolctl", "--json"], cwd,
                                  extra_env={**queue_env, **knobs})
                    attempt = self.fake_queue_attempt(cwd, run_id, case_id)
                    self.assertEqual(attempt["failure_reason"], expected_reason)
                    self.assertEqual(attempt["exit_code"], expected_code)
                    # The real command still ran, and its stdout was still
                    # captured, with the knob set.
                    self.assertIn(marker, Path(attempt["stdout_path"]).read_text())
                    self.assertEqual(set(attempt), set(REAL_SPOOLCTL_ATTEMPT_KEYS))

    def classification_run(self, cwd: Path, bindir: Path, run_id: str, knobs: dict[str, str]) -> tuple[dict, dict]:
        """Run the one-case suite through the fake with the given knobs set.

        Returns (envelope data, runner.json).
        """
        env = {"PATH": str(bindir) + os.pathsep + os.environ.get("PATH", ""), **knobs}
        data = self.envelope(["run", "code-review", "--run-id", run_id, "--queue", "spoolctl", "--json"], cwd, extra_env=env)["data"]
        runner_json = json.loads((cwd / "evals" / "runs" / run_id / "cases" / "cr-pass" / "runner.json").read_text())
        return data, runner_json

    def install_single_case_queue(self, cwd: Path) -> Path:
        self.envelope(["init", "--json"], cwd)
        self.keep_first_case_only(cwd)
        self.assertEqual(self.load_cases(cwd)[0]["id"], "cr-pass")
        return install_fake_spoolctl(cwd)

    def test_queued_classification_covers_every_failure_reason_row(self) -> None:
        # Four of these reasons -- worker_crash, canceled, unknown, and any
        # future member -- cannot be produced by the real binary in CI, and two
        # more rows are defensive. Without the fake they ship unexercised, which
        # is how the classification this replaced went two releases with a bug.
        rows = [
            ("row-clean", {}, None, False, False, "pass"),
            ("row-null-nonzero", {"FAKE_SPOOLCTL_FAILURE_REASON": "", "FAKE_SPOOLCTL_EXIT_CODE": "9"}, "E_RUNNER_FAILED", False, True, "error"),
            ("row-null-absent", {"FAKE_SPOOLCTL_FAILURE_REASON": "", "FAKE_SPOOLCTL_EXIT_CODE": ""}, "E_RUNNER_FAILED", False, True, "error"),
            ("row-exit-concrete", {"FAKE_SPOOLCTL_FAILURE_REASON": "process_exit", "FAKE_SPOOLCTL_EXIT_CODE": "7"}, None, False, False, "fail"),
            ("row-exit-absent", {"FAKE_SPOOLCTL_FAILURE_REASON": "process_exit", "FAKE_SPOOLCTL_EXIT_CODE": ""}, "E_RUNNER_FAILED", False, True, "error"),
            ("row-timeout", {"FAKE_SPOOLCTL_FAILURE_REASON": "timeout"}, "E_RUNNER_TIMEOUT", True, False, "error"),
            ("row-spawn", {"FAKE_SPOOLCTL_FAILURE_REASON": "spawn_failed"}, "E_RUNNER_FAILED", False, True, "error"),
            ("row-crash", {"FAKE_SPOOLCTL_FAILURE_REASON": "worker_crash"}, "E_RUNNER_FAILED", False, True, "error"),
            ("row-canceled", {"FAKE_SPOOLCTL_FAILURE_REASON": "canceled"}, "E_RUNNER_FAILED", False, True, "error"),
            ("row-unknown", {"FAKE_SPOOLCTL_FAILURE_REASON": "unknown"}, "E_RUNNER_FAILED", False, True, "error"),
            ("row-future", {"FAKE_SPOOLCTL_FAILURE_REASON": "reason_from_a_future_spoolctl"}, "E_RUNNER_FAILED", False, True, "error"),
        ]
        with tempfile.TemporaryDirectory() as td:
            cwd = Path(td)
            bindir = self.install_single_case_queue(cwd)
            for run_id, knobs, error_code, timed_out, spawn_failed, status in rows:
                with self.subTest(run_id=run_id):
                    data, runner_json = self.classification_run(cwd, bindir, run_id, knobs)
                    self.assertEqual(runner_json["error_code"], error_code)
                    self.assertEqual(runner_json["timed_out"], timed_out)
                    self.assertEqual(runner_json["spawn_failed"], spawn_failed)
                    counts = data["run"]["status_counts"]
                    self.assertEqual(counts[status], 1)
                    self.assertEqual(sum(counts.values()), 1)

    def test_queued_unrecognized_failure_reason_is_runner_failed_not_spoolctl_incompatible(self) -> None:
        # Forward compatibility: a spoolctl that adds an enum member must not
        # take evalctl down. The run has to complete normally, not raise the
        # integration-gate error.
        with tempfile.TemporaryDirectory() as td:
            cwd = Path(td)
            bindir = self.install_single_case_queue(cwd)
            result = self.run_cli(["run", "code-review", "--run-id", "future", "--queue", "spoolctl", "--json"], cwd,
                                  extra_env={"PATH": str(bindir) + os.pathsep + os.environ.get("PATH", ""),
                                             "FAKE_SPOOLCTL_FAILURE_REASON": "reason_evalctl_has_never_heard_of"})
            envelope = json.loads(result.stdout)
            codes = [error["code"] for error in envelope.get("errors") or []]
            self.assertNotIn("E_SPOOLCTL_INCOMPATIBLE", codes)
            self.assertEqual(codes, [])
            runner_json = json.loads((cwd / "evals" / "runs" / "future" / "cases" / "cr-pass" / "runner.json").read_text())
            self.assertEqual(runner_json["error_code"], "E_RUNNER_FAILED")

    def test_queued_null_failure_reason_with_a_nonzero_exit_is_not_a_scored_failure(self) -> None:
        # Counter-intuitive on purpose, and the opposite of what evalctl does
        # everywhere else. Real spoolctl always sets process_exit alongside a
        # nonzero exit code, so a null reason with one is malformed data rather
        # than a failing case. Scoring it would let a contradictory payload
        # produce a result.
        with tempfile.TemporaryDirectory() as td:
            cwd = Path(td)
            bindir = self.install_single_case_queue(cwd)
            data, runner_json = self.classification_run(cwd, bindir, "null-nonzero",
                                                        {"FAKE_SPOOLCTL_FAILURE_REASON": "", "FAKE_SPOOLCTL_EXIT_CODE": "3"})
            self.assertEqual(runner_json["error_code"], "E_RUNNER_FAILED")
            self.assertEqual(runner_json["exit_code"], 3)
            self.assertEqual(data["run"]["status_counts"]["error"], 1)
            self.assertEqual(sum(data["run"]["status_counts"].values()), 1)

    def test_queued_process_exit_without_an_exit_code_is_not_a_pass(self) -> None:
        # The other counter-intuitive row. process_exit means the process ran
        # and its exit code is the fact; an attempt claiming it without one is
        # contradicting its own contract, so it goes to the infrastructure
        # class rather than being scored as a clean exit.
        with tempfile.TemporaryDirectory() as td:
            cwd = Path(td)
            bindir = self.install_single_case_queue(cwd)
            data, runner_json = self.classification_run(cwd, bindir, "exit-absent",
                                                        {"FAKE_SPOOLCTL_FAILURE_REASON": "process_exit", "FAKE_SPOOLCTL_EXIT_CODE": ""})
            self.assertEqual(runner_json["error_code"], "E_RUNNER_FAILED")
            self.assertIsNone(runner_json["exit_code"])
            self.assertEqual(data["run"]["status_counts"]["error"], 1)
            self.assertEqual(sum(data["run"]["status_counts"].values()), 1)

    def test_queued_duration_ms_is_derived_from_the_attempt_timestamps(self) -> None:
        # Pinned end to end through the queue, because the old read of a field
        # spoolctl never emits recorded 0 on every queued case and no test
        # noticed. Fixed bounds rather than a comparison against an in-process
        # timing, which is itself variable.
        with tempfile.TemporaryDirectory() as td:
            cwd = Path(td)
            bindir = self.install_single_case_queue(cwd)
            suite = self.load_suite(cwd)
            suite["runner"]["argv"] = [sys.executable, "-c", "import time; time.sleep(0.4)"]
            self.write_suite(cwd, suite)
            _, runner_json = self.classification_run(cwd, bindir, "timed", {})
            attempt = self.fake_queue_attempt(cwd, "timed", "cr-pass")
            self.assertGreaterEqual(runner_json["duration_ms"], 400)
            self.assertLess(runner_json["duration_ms"], 30000)
            self.assertEqual(runner_json["duration_ms"],
                             max(0, round((attempt["finished_at"] - attempt["started_at"]) * 1000)))

    def test_spoolctl_attempt_duration_clamps_reversed_and_missing_timestamps(self) -> None:
        # Payload shapes no spoolctl produces. They are unit-tested directly
        # rather than through the fake because giving the fixture a timestamp
        # knob would grow the control surface for inputs the real tool cannot
        # emit -- the fake is for reasons CI cannot reach, not for malformed
        # records.
        self.assertEqual(runner.spoolctl_attempt_duration_ms({"started_at": 100.0, "finished_at": 100.25}), 250)
        self.assertEqual(runner.spoolctl_attempt_duration_ms({"started_at": 100, "finished_at": 101}), 1000)
        self.assertEqual(runner.spoolctl_attempt_duration_ms({"started_at": 200.0, "finished_at": 100.0}), 0)
        self.assertEqual(runner.spoolctl_attempt_duration_ms({"finished_at": 100.0}), 0)
        self.assertEqual(runner.spoolctl_attempt_duration_ms({"started_at": 100.0}), 0)
        self.assertEqual(runner.spoolctl_attempt_duration_ms({}), 0)
        self.assertEqual(runner.spoolctl_attempt_duration_ms({"started_at": None, "finished_at": 100.0}), 0)
        self.assertEqual(runner.spoolctl_attempt_duration_ms({"started_at": "100.0", "finished_at": "101.0"}), 0)
        self.assertEqual(runner.spoolctl_attempt_duration_ms({"started_at": float("nan"), "finished_at": 100.0}), 0)

    def test_canceled_job_with_no_attempts_is_a_case_error_not_a_spoolctl_incompatibility(self) -> None:
        # The whole point of the fix. A job somebody canceled before a worker
        # picked it up is an ordinary queue outcome; reporting it as
        # E_SPOOLCTL_INCOMPATIBLE told the operator to upgrade spoolctl, which
        # would never have helped. The run must complete and score the case as
        # an error.
        with tempfile.TemporaryDirectory() as td:
            cwd = Path(td)
            bindir = self.install_single_case_queue(cwd)
            result = self.run_cli(["run", "code-review", "--run-id", "canceled", "--queue", "spoolctl", "--json"], cwd,
                                  extra_env={"PATH": str(bindir) + os.pathsep + os.environ.get("PATH", ""),
                                             "FAKE_SPOOLCTL_DROP_ATTEMPTS": "1"})
            envelope = json.loads(result.stdout)
            codes = [error["code"] for error in envelope.get("errors") or []]
            self.assertNotIn("E_SPOOLCTL_INCOMPATIBLE", codes)
            self.assertEqual(codes, [])
            self.assertNotEqual(result.returncode, 3)
            runner_json = json.loads((cwd / "evals" / "runs" / "canceled" / "cases" / "cr-pass" / "runner.json").read_text())
            self.assertEqual(runner_json["error_code"], "E_RUNNER_FAILED")
            self.assertTrue(runner_json["spawn_failed"])
            self.assertFalse(runner_json["timed_out"])
            self.assertIsNone(runner_json["exit_code"])
            counts = envelope["data"]["run"]["status_counts"]
            self.assertEqual(counts["error"], 1)
            self.assertEqual(sum(counts.values()), 1)

    def test_fake_spoolctl_show_envelope_matches_the_real_shape(self) -> None:
        # The other half of the drift check tests/test_real_spoolctl.py runs
        # against the binary. The fake emitted the job record flat, with state
        # and attempts at the top level, so evalctl's read of the job's own
        # state passed here and found nothing against the real tool.
        with tempfile.TemporaryDirectory() as td:
            cwd = Path(td)
            bindir = self.install_single_case_queue(cwd)
            queue_env = {"PATH": str(bindir) + os.pathsep + os.environ.get("PATH", "")}
            # The dead row needs a runner that really fails: the exit-code knob
            # rewrites the attempt record only, by design, so it cannot move the
            # job's state.
            failing = [sys.executable, "-c", "import sys; sys.exit(7)"]
            rows = [("env-done", {}, None, "done"),
                    ("env-canceled", {"FAKE_SPOOLCTL_DROP_ATTEMPTS": "1"}, None, "canceled"),
                    ("env-dead", {}, failing, "dead")]
            for run_id, knobs, argv, expected_state in rows:
                with self.subTest(run_id=run_id):
                    if argv is not None:
                        suite = self.load_suite(cwd)
                        suite["runner"]["argv"] = argv
                        self.write_suite(cwd, suite)
                    self.envelope(["run", "code-review", "--run-id", run_id, "--queue", "spoolctl", "--json"], cwd,
                                  extra_env={**queue_env, **knobs})
                    db = cwd / "evals" / "runs" / run_id / ".spoolctl.db"
                    job_id = json.loads((cwd / "evals" / "runs" / run_id / "cases" / "cr-pass" / "job.json").read_text())["job_id"]
                    payload = run_fake_tool(bindir, "spoolctl", ["show", "--db", str(db), "--json", job_id])["data"]
                    self.assertEqual(set(payload), set(REAL_SPOOLCTL_SHOW_KEYS))
                    self.assertNotIn("attempts", payload["job"])
                    self.assertEqual(payload["job"]["state"], expected_state)
                    self.assertIn(payload["job"]["state"], REAL_SPOOLCTL_TERMINAL_JOB_STATES)

    def test_queued_job_json_records_the_queue_state_not_the_attempt_state(self) -> None:
        # job.json is queue provenance, so it carries spoolctl's verdict on the
        # job. The attempt's own state would restate what runner.json already
        # records, and does not exist at all for a job no worker ran -- which is
        # the row that used to write null here.
        with tempfile.TemporaryDirectory() as td:
            cwd = Path(td)
            bindir = self.install_single_case_queue(cwd)
            queue_env = {"PATH": str(bindir) + os.pathsep + os.environ.get("PATH", "")}
            failing = [sys.executable, "-c", "import sys; sys.exit(7)"]
            rows = [("state-done", {}, None, "done"),
                    ("state-canceled", {"FAKE_SPOOLCTL_DROP_ATTEMPTS": "1"}, None, "canceled"),
                    ("state-dead", {}, failing, "dead")]
            for run_id, knobs, argv, expected_state in rows:
                with self.subTest(run_id=run_id):
                    if argv is not None:
                        suite = self.load_suite(cwd)
                        suite["runner"]["argv"] = argv
                        self.write_suite(cwd, suite)
                    self.envelope(["run", "code-review", "--run-id", run_id, "--queue", "spoolctl", "--json"], cwd,
                                  extra_env={**queue_env, **knobs})
                    job = json.loads((cwd / "evals" / "runs" / run_id / "cases" / "cr-pass" / "job.json").read_text())
                    self.assertEqual(job["state"], expected_state)
                    self.assertTrue(job["job_id"])

    def test_spoolctl_job_state_reads_the_nested_job_and_nothing_else(self) -> None:
        # Pinned directly, because the bug it replaces was a read that returned
        # None on every real payload while a flat fixture made it look correct.
        self.assertEqual(runner.spoolctl_job_state({"job": {"state": "done"}, "attempts": [], "events": []}), "done")
        self.assertIsNone(runner.spoolctl_job_state({"state": "done", "attempts": []}))
        self.assertIsNone(runner.spoolctl_job_state({"job": {}}))
        self.assertIsNone(runner.spoolctl_job_state({"job": None}))
        self.assertIsNone(runner.spoolctl_job_state({}))

    def test_latest_terminal_attempt_separates_an_unrun_job_from_an_unreadable_payload(self) -> None:
        # An empty attempts list is a fact about the job. A missing or non-list
        # attempts key is a fact about the spoolctl on the other end, and only
        # that second one is a compatibility problem.
        self.assertIsNone(runner.latest_terminal_attempt({"state": "canceled", "attempts": []}))
        self.assertEqual(runner.latest_terminal_attempt({"attempts": [{"attempt_no": 1}, {"attempt_no": 2}]}), {"attempt_no": 2})
        for payload in ({}, {"state": "done"}, {"attempts": None}, {"attempts": {}}, {"attempts": "1"}):
            with self.subTest(payload=payload):
                with self.assertRaises(static_contract.EvalctlError) as ctx:
                    runner.latest_terminal_attempt(payload)
                self.assertEqual(ctx.exception.error["code"], "E_SPOOLCTL_INCOMPATIBLE")
                self.assertEqual(ctx.exception.exit_code, 3)

    def test_unrun_job_result_names_the_cause_without_claiming_a_process_ran(self) -> None:
        # No exit code and no signal, because no process produced either. The
        # stderr text is the only place an operator learns why the case errored,
        # so it has to say what happened rather than stay empty.
        result = runner.runner_result_for_unrun_job()
        self.assertEqual(set(result), {"stdout", "stderr", "timed_out", "spawn_failed", "exit_code", "signal", "duration_ms"})
        self.assertTrue(result["spawn_failed"])
        self.assertFalse(result["timed_out"])
        self.assertIsNone(result["exit_code"])
        self.assertIsNone(result["signal"])
        self.assertEqual(result["duration_ms"], 0)
        self.assertEqual(result["stdout"], "")
        self.assertIn("canceled", result["stderr"])

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

    def test_not_found_errors_name_valid_sets_and_schema_uses_unknown_command(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cwd = Path(td)
            self.envelope(["init", "--json"], cwd)

            # F20: schema on an unknown verb is E_UNKNOWN_COMMAND carrying the
            # verb names it already holds, with a near-match suggestion.
            bad_schema = self.run_cli(["schema", "ruh", "--json"], cwd, expect=1)
            schema_err = json.loads(bad_schema.stdout)["errors"][0]
            self.assertEqual(schema_err["code"], "E_UNKNOWN_COMMAND")
            self.assertIn("run", schema_err["valid_values"])
            self.assertEqual(schema_err["did_you_mean"], "run")
            self.assertEqual(schema_err["corrected_command"], "evalctl schema run")

            # F7: suite-not-found names the suites on disk.
            bad_suite = self.run_cli(["validate", "kode-review", "--json"], cwd, expect=1)
            suite_err = json.loads(bad_suite.stdout)["errors"][0]
            self.assertEqual(suite_err["code"], "E_SUITE_NOT_FOUND")
            self.assertEqual(suite_err["valid_values"], ["code-review"])
            self.assertEqual(suite_err["did_you_mean"], "code-review")

            # F7: run-not-found names the runs on disk.
            self.envelope(["run", "code-review", "--run-id", "realrun", "--json"], cwd)
            bad_run = self.run_cli(["status", "reelrun", "--json"], cwd, expect=1)
            run_err = json.loads(bad_run.stdout)["errors"][0]
            self.assertEqual(run_err["code"], "E_RUN_NOT_FOUND")
            self.assertIn("realrun", run_err["valid_values"])
            self.assertEqual(run_err["did_you_mean"], "realrun")

    def test_init_reports_unwritable_directory_at_declared_exit(self) -> None:
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            self.skipTest("root ignores directory permissions")
        with tempfile.TemporaryDirectory() as td:
            cwd = Path(td)
            os.chmod(cwd, 0o555)
            try:
                # F17: an unwritable target is a declared tool-environment error
                # at exit 3, not an undeclared internal error.
                result = self.run_cli(["init", "--json"], cwd, expect=3)
                err = json.loads(result.stdout)["errors"][0]
                self.assertEqual(err["code"], "E_INIT_UNWRITABLE")
                self.assertEqual(err["exit_code"], 3)
            finally:
                os.chmod(cwd, 0o755)

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
                # robot-docs declares json:false, so --json is rejected before the
                # subcommand is looked at. The envelope still prints: stdout is a pipe.
                (["robot-docs", "guid"], "evalctl robot-docs guide"),
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
            # A retry with the same source and case set is idempotent -- it returns
            # the existing run rather than refusing, matching `run --run-id`.
            repeat = self.envelope(["replay", "--failed", "source", "--run-id", "dest", "--json"], cwd)
            self.assertTrue(repeat["data"]["existing"])
            self.assertEqual(repeat["data"]["run_id"], "dest")
            forced = self.envelope(["replay", "--failed", "source", "--run-id", "dest", "--force", "--json"], cwd)
            self.assertEqual(forced["data"]["run_id"], "dest")
            self.assertNotIn("existing", forced["data"])

            source_manifest = (cwd / "evals" / "runs" / "source" / "manifest.json").read_text()
            same = self.run_cli(["replay", "--failed", "source", "--run-id", "source", "--force", "--json"], cwd, expect=5)
            self.assertEqual(json.loads(same.stdout)["errors"][0]["code"], "E_RUN_CONFLICT")
            self.assertEqual((cwd / "evals" / "runs" / "source" / "manifest.json").read_text(), source_manifest)

    def test_replay_default_run_id_is_clock_independent_and_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cwd = Path(td)
            self.envelope(["init", "--json"], cwd)
            self.envelope(["run", "code-review", "--run-id", "source", "--json"], cwd)
            self.fix_cr_fail_runner(cwd)

            # Three replays of the same source with no --run-id. The clock is
            # forced to differ: same second (A, B share an epoch), then a second
            # far away (C). If the default id were clock-derived, C would land on
            # a different id and spawn a second run. It must not.
            same_second = {"SOURCE_DATE_EPOCH": "1700000000"}
            later_second = {"SOURCE_DATE_EPOCH": "1900000000"}
            first = self.envelope(["replay", "--failed", "source", "--json"], cwd, extra_env=same_second)
            second = self.envelope(["replay", "--failed", "source", "--json"], cwd, extra_env=same_second)
            third = self.envelope(["replay", "--failed", "source", "--json"], cwd, extra_env=later_second)

            self.assertIn("-replay-", first["data"]["run_id"])
            self.assertEqual(first["data"]["run_id"], second["data"]["run_id"])
            self.assertEqual(first["data"]["run_id"], third["data"]["run_id"])
            self.assertNotIn("existing", first["data"])
            self.assertTrue(second["data"]["existing"])
            self.assertTrue(third["data"]["existing"])
            self.assertEqual(first["data"]["cases_replayed"], 1)
            self.assertEqual(second["data"]["cases_replayed"], 1)
            self.assertEqual({first["data"]["report_hash"], second["data"]["report_hash"], third["data"]["report_hash"]}, {first["data"]["report_hash"]})

            replay_dirs = [p.name for p in (cwd / "evals" / "runs").iterdir() if "-replay-" in p.name]
            self.assertEqual(len(replay_dirs), 1, f"a retry spawned a second run: {sorted(replay_dirs)}")

            # Idempotency keys on identity, not just the id string: a genuinely
            # different suite/case set on the same explicit id is still a conflict,
            # so a retry never silently overwrites a different run.
            self.envelope(["replay", "--failed", "source", "--run-id", "explicit-dest", "--json"], cwd)
            suite = self.load_suite(cwd)
            suite["name"] = "code-review-renamed"
            self.write_suite(cwd, suite)
            conflict = self.run_cli(["replay", "--failed", "source", "--suite", "code-review", "--run-id", "explicit-dest", "--json"], cwd, expect=5)
            self.assertEqual(json.loads(conflict.stdout)["errors"][0]["code"], "E_RUN_CONFLICT")

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
            # F15: --runner-argv takes a shell-style string; a JSON array was
            # silently coerced into one literal argv token. Reject it by shape.
            json_array = self.run_cli(["suite", "add", "demo", "--runner-argv", '["python3","x.py"]', "--json"], cwd, expect=1)
            json_array_error = json.loads(json_array.stdout)["errors"][0]
            self.assertEqual(json_array_error["code"], "E_CASE_INVALID")
            self.assertIn("JSON array", json_array_error["message"])
            self.assertFalse((cwd / "evals" / "suites" / "demo").exists())

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

    def test_validate_resolves_scorer_names_and_runner_executable(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cwd = Path(td)
            self.envelope(["init", "--json"], cwd)
            suite_json = cwd / "evals" / "suites" / "code-review" / "suite.json"

            # Baseline: the sample suite is valid with no warnings (argv[0] is
            # python3, which resolves on PATH).
            clean = self.envelope(["validate", "code-review", "--json"], cwd)
            self.assertTrue(clean["data"]["valid"])
            self.assertEqual(clean["warnings"], [])

            # F16: an unknown scorer name is an error naming the valid set with a
            # near-match suggestion, not a silent valid:true.
            suite = json.loads(suite_json.read_text())
            suite["scorers"].append({"name": "containz", "required": True})
            suite_json.write_text(json.dumps(suite, indent=2, sort_keys=True) + "\n")
            bad_scorer = self.run_cli(["validate", "code-review", "--json"], cwd, expect=1)
            scorer_error = json.loads(bad_scorer.stdout)["errors"][0]
            self.assertEqual(scorer_error["code"], "E_CASE_INVALID")
            self.assertIn("contains", scorer_error["valid_values"])
            self.assertEqual(scorer_error["did_you_mean"], "contains")

            # F16: an unresolvable runner executable is a warning (it may resolve
            # in the run environment), not a failure. An env-placeholder argv[0]
            # is left alone because it only resolves at run time.
            suite["scorers"] = [{"name": "contains", "required": True}]
            suite["runner"]["argv"] = ["nonesuch-binary-xyz", "runner.py"]
            suite_json.write_text(json.dumps(suite, indent=2, sort_keys=True) + "\n")
            warned = self.envelope(["validate", "code-review", "--json"], cwd)
            self.assertTrue(warned["data"]["valid"])
            self.assertEqual(warned["warnings"][0]["code"], "W_RUNNER_UNRESOLVED")
            self.assertIn("nonesuch-binary-xyz", warned["warnings"][0]["message"])

            suite["runner"]["argv"] = ["$EVALCTL_WORKSPACE/runner.py"]
            suite_json.write_text(json.dumps(suite, indent=2, sort_keys=True) + "\n")
            placeholder = self.envelope(["validate", "code-review", "--json"], cwd)
            self.assertEqual(placeholder["warnings"], [])

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
            self.assertTrue(payload["data"]["fail_on_fail_triggered"])
            self.assertEqual(payload["errors"], [])
            self.assertIn("eval failure:", result.stderr)

    def test_fail_on_fail_not_triggered_when_run_passes(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cwd = Path(td)
            self.envelope(["init", "--json"], cwd)
            self.keep_first_case_only(cwd)  # cr-pass passes
            result = self.run_cli(["run", "code-review", "--run-id", "pass", "--fail-on-fail", "--json"], cwd, expect=0)
            payload = json.loads(result.stdout)
            self.assertTrue(payload["data"]["run"]["ok"])
            self.assertFalse(payload["data"]["fail_on_fail_triggered"])
            self.assertNotIn("eval failure:", result.stderr)

    def test_unsandboxed_runner_warning_present_when_acknowledged(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cwd = Path(td)
            self.envelope(["init", "--json"], cwd)
            acknowledged = self.run_cli(["run", "code-review", "--run-id", "ack", "--json"], cwd)
            ack_payload = json.loads(acknowledged.stdout)
            self.assertIn("W_UNSANDBOXED_RUNNER", {w["code"] for w in ack_payload["warnings"]})

    def test_run_refuses_unacknowledged_unsandboxed_runner_with_exit_two(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cwd = Path(td)
            self.envelope(["init", "--json"], cwd)
            no_ack = {"EVALCTL_ACKNOWLEDGE_UNSANDBOXED_RUNNER": ""}
            refused = self.run_cli(["run", "code-review", "--run-id", "unack", "--json"], cwd, expect=2, extra_env=no_ack)
            payload = json.loads(refused.stdout)
            self.assertFalse(payload["ok"])
            self.assertEqual([e["code"] for e in payload["errors"]], ["E_UNSANDBOXED_RUNNER_UNACK"])
            self.assertEqual(payload["errors"][0]["exit_code"], 2)
            # gate fires before any runner executes: no run directory is created
            self.assertFalse((cwd / "evals" / "runs" / "unack").exists())

    def test_flag_acknowledges_unsandboxed_runner(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cwd = Path(td)
            self.envelope(["init", "--json"], cwd)
            no_ack = {"EVALCTL_ACKNOWLEDGE_UNSANDBOXED_RUNNER": ""}
            ok = self.run_cli(["run", "code-review", "--run-id", "flag", "--acknowledge-unsandboxed-runner", "--json"], cwd, expect=0, extra_env=no_ack)
            self.assertTrue(json.loads(ok.stdout)["ok"])

    def test_suite_field_does_not_satisfy_the_gate(self) -> None:
        # A safety acknowledgment stored inside the suite (attacker-controllable)
        # must NOT let an unacknowledged invoker execute the suite's runner.
        with tempfile.TemporaryDirectory() as td:
            cwd = Path(td)
            self.envelope(["init", "--json"], cwd)
            suite = self.load_suite(cwd)
            suite["acknowledged_unsandboxed_runner"] = True
            self.write_suite(cwd, suite)
            no_ack = {"EVALCTL_ACKNOWLEDGE_UNSANDBOXED_RUNNER": ""}
            refused = self.run_cli(["run", "code-review", "--run-id", "field", "--json"], cwd, expect=2, extra_env=no_ack)
            self.assertEqual([e["code"] for e in json.loads(refused.stdout)["errors"]], ["E_UNSANDBOXED_RUNNER_UNACK"])

    def test_replay_refuses_unacknowledged_unsandboxed_runner(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cwd = Path(td)
            self.envelope(["init", "--json"], cwd)
            self.run_cli(["run", "code-review", "--run-id", "src", "--json"], cwd)
            no_ack = {"EVALCTL_ACKNOWLEDGE_UNSANDBOXED_RUNNER": ""}
            refused = self.run_cli(["replay", "--failed", "src", "--run-id", "rp", "--json"], cwd, expect=2, extra_env=no_ack)
            self.assertEqual([e["code"] for e in json.loads(refused.stdout)["errors"]], ["E_UNSANDBOXED_RUNNER_UNACK"])

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
