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
evalctl run demo --json
```

The run produces a portable, resumable run directory. The runner is arbitrary
local code — **evalctl is not a sandbox**, and every run envelope carries the
`W_UNSANDBOXED_RUNNER` warning.

## 5. Inspect and report

```bash
evalctl status <run-id> --json
evalctl report <run-id> --format json
```

Use `--format markdown` for a human-readable report instead.

## The full loop

```bash
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
