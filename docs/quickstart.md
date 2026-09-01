---
title: Quickstart
description: Scaffold a project, author a suite, run it, and read the report.
bucket: guides
order: 2
---

# Quickstart

evalctl treats eval cases as files, runners as shell commands, and results as
durable artifacts. This walks the full loop: scaffold, author, run, report.

## 1. Scaffold

```bash
evalctl init --json
```

`init` writes an `evals/` tree with a sample code-review suite. Re-running is a
conflict unless you pass `--force`.

## 2. Author a suite

You can build a suite without hand-editing `suite.json` or `cases.jsonl`:

```bash
evalctl suite add demo --runner-argv "python3 $EVALCTL_WORKSPACE/r.py" --json
evalctl case add demo --task "do X" --workspace fixtures/x --expect-json '{"exact":"ok"}' --json
evalctl scorer add demo --name exact --required --json
```

Authoring verbs are idempotent: re-adding the same canonical object returns
`created:false`; reusing a key with different content returns `E_RUN_CONFLICT`.

## 3. Validate

Always validate before executing local code:

```bash
evalctl validate demo --json
```

This checks `suite.json`, `cases.jsonl`, fixtures, scorer references, and runner
config.

## 4. Run

```bash
evalctl run demo --acknowledge-unsandboxed-runner --json
```

The runner is arbitrary local code — **evalctl is not a sandbox**. `run` and
`replay` refuse with exit `2` until the invoker acknowledges this, either with
`--acknowledge-unsandboxed-runner` or by setting
`EVALCTL_ACKNOWLEDGE_UNSANDBOXED_RUNNER=1`. Once acknowledged, the run produces a
portable, resumable run directory, and every run envelope still carries the
`W_UNSANDBOXED_RUNNER` warning.

## 5. Inspect and report

```bash
evalctl status <run-id> --json
evalctl report <run-id> --format json
```

Use `--format markdown` for a human-readable report instead.

## The full loop

```bash
export EVALCTL_ACKNOWLEDGE_UNSANDBOXED_RUNNER=1  # or pass --acknowledge-unsandboxed-runner per run
evalctl init --json
evalctl suite add demo --runner-argv "python3 $EVALCTL_WORKSPACE/r.py" --json
evalctl case add demo --task "do X" --workspace fixtures/x --expect-json '{"exact":"ok"}' --json
evalctl scorer add demo --name exact --required --json
evalctl run demo --json
evalctl run --resume <run-id> --json
evalctl jobs list --json
evalctl run demo --queue spoolctl --slots 4 --json
evalctl replay --failed <run-id> --json
evalctl report <run-id> --format json
```

Next: the full [command surface](/docs/commands/), or how to drive evalctl
[from an agent](/docs/agent-guide/).
