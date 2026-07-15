# evalctl

**Local-first evals for agents, not just prompts.**

evalctl treats eval cases as files, runners as shell commands, and results as
durable artifacts. It scores what agents actually do — files written, diffs
produced, commands run — on your own machine, with no gateway, dashboard, or
SaaS account.

v0.2 runs synchronously in-process, writes self-contained run directories, adds
CLI authoring verbs, supports targeted execution replay, and includes an
external command-scorer protocol.
The contract leaves room for future [spoolctl](https://github.com/Ozhiaki/spoolctl)
and [inferctl](https://inferctl.dev) integrations, but neither is required or
implemented in v0.2.

## About

evalctl is an agent-first evaluation harness for local workflows. It runs eval
cases as filesystem fixtures, invokes agents through ordinary runner commands,
and grades the resulting workspace with deterministic scorers. The core artifact
is a portable run directory that another agent can inspect, report on, and
re-score without access to the original shell history.

Keywords: agent evals, LLM evaluation, AI agents, local-first, CLI, code review,
workspace diff, deterministic scoring, artifact replay, eval harness.

## How it differs from promptfoo

[promptfoo](https://www.promptfoo.dev/) is the incumbent local eval CLI, and it
is prompt/chat-shaped.
evalctl is agent-shaped.

| Axis | promptfoo | evalctl |
| --- | --- | --- |
| Unit under test | prompt → completion | agent run → files, diffs, command logs, artifacts |
| Scoring surface | text of a response | resulting workspace: git diff, expected/forbidden file changes, exit codes, plus text |
| Execution | in-process, synchronous | v0.2 synchronous; `replay --failed` reruns failed cases; spoolctl async is deferred |
| Model context | provider API keys | [inferctl](https://inferctl.dev) route/preflight provenance is deferred |

## Status

Python pre-release. v0.2 provides scaffold, validate, bounded parallel run
execution, status, report, deterministic local scorers, CLI authoring verbs,
execution replay for failed cases, command scorers, truthful warnings/errors,
real schema output, and artifact replay from a copied run directory.
`contract_version` remains `1`.

## Commands

```bash
evalctl init --json
evalctl suite add demo --runner-argv "python3 $EVALCTL_WORKSPACE/r.py" --json
evalctl case add demo --task "do X" --workspace fixtures/x --expect-json '{"exact":"ok"}' --json
evalctl scorer add demo --name exact --required --json
evalctl run demo --json
evalctl replay --failed <run-id> --json
evalctl report <run-id> --format json
```

## Authoring

`suite add`, `case add`, and `scorer add` let agents build a suite without
hand-editing `suite.json` or `cases.jsonl`. Authoring verbs are idempotent on
retry: adding the same canonical object returns `created:false`; reusing the
same key with different content returns `E_RUN_CONFLICT`.

`case add` only writes paths under the suite tree. Absolute paths and `..`
segments are rejected so generated suite files remain portable.

## Replay

`replay --failed` selects failed/errored cases from the source run's recomputed
report projection, not from `score.json` or stored manifest status. It re-runs
only those cases against the current suite and writes a fresh partial run whose
manifest `replayed_from` names the source run.

`replay --run-id` names the destination run. It never resolves the source. Pass
`--suite <suite-or-path>` when replaying a copied run or when the current suite
cannot be resolved by manifest suite name.

## Command Scorers

`scorer add <suite> --name command --id judge --argv "python3 scorer.py"` adds an
external scorer. The scorer receives `EVALCTL_CASE_FILE`,
`EVALCTL_OUTPUT_FILE`, and `EVALCTL_WORKSPACE`, and emits one JSON verdict.

Command-scorer verdicts are captured once under
`cases/<case_id>/scorers/<id>.json`. Reports and artifact replay read that
artifact and do not re-execute the scorer binary. Command scorers run arbitrary
local code and are covered by the same unsandboxed-runner warning as runners.

## Home

- Site: [evalctl.dev](https://evalctl.dev)
- Org: [github.com/evalctl](https://github.com/evalctl)
