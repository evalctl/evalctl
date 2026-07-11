# evalctl — one-page positioning

Status: positioning draft
Date: 2026-07-11

## Wedge

> Local-first evals for **agents**, not just prompts.

### Variants (pick one, kill the rest)

1. Local-first evals for agents, not just prompts.
2. Prove your agent didn't regress — on your machine, as a file you can commit.
3. Evals that score what agents *do*: files, diffs, commands — not just what they say.
4. The boring local CLI for agent evals. No dashboard, no gateway, no login.
5. Regression tests for agents: cases are files, scorers are commands, runs are artifacts.

6. Put agent evals in CI without renting a dashboard.
7. Every eval run is a sealed record the next agent can rerun.
8. Bake off local models on your own cases, on your own metal.
9. Deterministic scorers first — LLM judges optional, never required.
10. inferctl routes, spoolctl runs, evalctl grades.
11. Stop shipping prompt and agent changes on vibes.
12. If it touched a file, it can be scored.
13. Run ten thousand cases, lose the laptop, resume where it died.

Rank for the README: #1 is the sharpest positioning against promptfoo; #3 is
the strongest if the audience already knows why agent evals differ from prompt
evals; #4 leans hardest on the local/no-SaaS angle. Best of the deeper cuts:
#7 (sealed rerunnable record), #10 (the family split), #13 (crash-resume).

evalctl treats eval cases as files, runners as shell commands, and results as
durable artifacts. It scores what agents actually do — files written, diffs
produced, commands run — on your own machine, with no gateway, dashboard, or
SaaS account. Execution can be delegated to spoolctl for crash-safe async runs;
model selection can be delegated to inferctl for local-stack awareness.

**The one-breath differentiation.** promptfoo is the incumbent local eval CLI,
and it is prompt/chat-shaped. evalctl is agent-shaped. Four concrete
differences:

| Axis | promptfoo | evalctl |
| --- | --- | --- |
| Unit under test | prompt → completion | agent run → files, diffs, command logs, artifacts |
| Scoring surface | text of a response | resulting workspace: git diff, expected/forbidden file changes, exit codes, plus text |
| Execution | in-process, synchronous | optional spoolctl: bounded concurrency, retries, dead-letter, **resume failed across a crash** |
| Model context | provider API keys | optional inferctl: local backend route + preflight recorded per case |

If a reader can't repeat this contrast after one read, the wedge is too soft.
Lead every description with the agent framing; never lead with prompt
regression.

## Who it's for

Engineers building agent and LLM workflows who need to answer "did this change
regress behavior?" and want the answer as a reproducible local artifact they can
diff, commit, and hand to the next agent — not a chart behind a login.

## What we do NOT build

- No hosted service, no required server, no web UI by default.
- No inference proxy — evalctl never sits in the request path.
- No DAG engine, no Python framework requirement, no cloud credentials.
- LLM-as-judge is supported but never the first or only scorer. Deterministic
  local scorers ship first because they are credible.

## v0.1 cut — prove the differentiated path, not the commodity one

Ship the agent path first. Prompt-regression evals (the crowded part promptfoo
owns) can follow; they are not the wedge.

Target: **one code-review eval suite, end to end, with spoolctl and inferctl
wired in.**

- `evalctl init` + JSONL/dir case ingestion
- a case that supplies a diff fixture, a task prompt, and expected findings
- command runner that invokes the agent under test
- built-in scorers: contains, regex, JSON-schema, command; plus a
  workspace-diff scorer (expected vs forbidden file changes)
- `--queue spoolctl` for durable async execution; `--inferctl-task <task>` to
  record route/preflight per case
- local run directory with `manifest.json` as source of truth; report derived
- `evalctl compare run-a run-b`
- `evalctl replay --failed`

Explicitly deferred past v0.1: shard, sample, junit export, bless/freeze, HTML
reports, LLM-judge scorer, prompt-regression suites.

## Success test for v0.1

A second agent, given only the run directory, can rerun the failed cases and
produce the same report — without reconstructing context from shell history.
If that works, the durability and provenance story is proven in one artifact.

## Why this showcases the whole family

- **inferctl** proves it can route and preflight real local model work.
- **spoolctl** proves it can run many async cases safely and resume after death.
- **evalctl** proves the outputs are measurable, comparable, and reproducible.

Each tool earns its keep; none absorbs another's job.

## Pinned underneath: the provenance manifest

evalctl's per-run `manifest.json` (inputs, hashes, route/preflight snapshots,
job IDs, raw outputs, scorer outputs, report links) **is** a local provenance
attestation — the value the separate `provctl` idea was chasing, but captured
automatically because the harness drives every step instead of relying on the
caller to record it. Keep the manifest schema clean and portable. If a
standalone provenance need survives later, factor the schema out of evalctl;
do not build it first.
