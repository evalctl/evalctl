---
title: Scorers and command scorers
description: Deterministic built-in scorers, and the external command-scorer protocol.
bucket: concepts
order: 3
---

# Scorers and command scorers

Scoring in evalctl is about what an agent actually produced — files written,
diffs, exit codes, and text — not just the text of a response. Scorers are
attached to a suite and can be **required** (they gate pass/fail) or **advisory**.

## Built-in scorers

Add a built-in scorer by name:

```bash
evalctl scorer add demo --name exact --required --json
```

Built-ins include exact match, regex, JSON comparison, and numeric threshold
scorers. Cases declare expectations — e.g. `case add --expect-json` — and the
scorer grades the resulting workspace and output against them.

## Command scorers

A command scorer runs your own binary to produce a verdict:

```bash
evalctl scorer add demo --name command --id judge --argv "python3 scorer.py"
```

The scorer process receives three environment variables and emits **one JSON
verdict** on stdout:

| Variable | Meaning |
| --- | --- |
| `EVALCTL_CASE_FILE` | the materialized case JSON |
| `EVALCTL_OUTPUT_FILE` | the runner's captured response |
| `EVALCTL_WORKSPACE` | the per-case workspace to inspect |

## Verdicts are captured once

A command scorer is executed **once**, at run time. Its normalized verdict is
stored under `cases/<case_id>/scorers/<id>.json`. Reports and artifact replay read
that artifact and **never re-execute the scorer binary** — which is exactly why a
copied run directory stays reportable with no access to your original tooling.

A per-case scorer failure surfaces as `E_SCORER_CASE_FAILED` on the
`score_json` surface; it is reportable case data, not a command-level error. See
[Error and exit codes](/docs/errors/).

## Safety

Command scorers run **arbitrary local code**, exactly like runners, and are
covered by the same `W_UNSANDBOXED_RUNNER` warning. evalctl does not sandbox
them. See [Security posture](/docs/security/).

> LLM-as-judge scoring is a roadmap item, not a shipped command scorer type. Any
> model-based judgment today is your own binary emitting a JSON verdict.
