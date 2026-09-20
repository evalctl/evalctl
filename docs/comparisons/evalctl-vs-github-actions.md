---
title: evalctl and GitHub Actions
description: Why continuous integration alone can't grade an agent run, and how evalctl turns one into a pass/fail check with per-case detail.
bucket: project
order: 9
draft: false
---

# evalctl and GitHub Actions

You already run CI. So the question isn't "evalctl instead of GitHub Actions" — it
is "what runs inside the job to grade an agent's work?" CI runs commands and turns
their exit code into a green or red check. It has no opinion about what an agent
did to a workspace. evalctl is the step that produces that verdict. This page shows
how they fit.

## What CI does, and what it leaves to you

GitHub Actions (or any CI) runs a job and passes or fails it on the exit code of
the commands you list. That is all it promises. It does not know what an agent run
is, whether a diff touched the right files, or whether a frozen test was edited to
fake a green. Something has to compute that answer and exit non-zero when the
answer is no.

That something is what you drop into the job. If the only thing your job runs is
the agent's own test command, CI inherits every blind spot that command has —
including passing when the agent edited the test to pass.

## How evalctl fits in the job

evalctl runs as a step. It runs the agent's work, grades the workspace it left,
and exits non-zero if a required check fails. The exit code gates the job the same
way any test command does — so a failed agent run is a red check on the pull
request, with no extra wiring.

Two things make it more than a pass/fail:

- **Per-case detail in a standard format.** `evalctl report <run-id> --format
  junit` writes a JUnit XML file
  ([changelog 1.1.0](/docs/changelog/)). JUnit is the format CI test reporters
  already read, so a reporter step can turn each scored case into an annotated
  result instead of one opaque pass or fail.
- **A portable run directory.** The run leaves a directory another machine can
  re-score offline. Upload it as a build artifact and anyone can replay how the
  run was graded, without your original tooling
  ([replay](/docs/replay/)).

## A minimal workflow

```yaml
jobs:
  agent-eval:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"
      - run: pip install evalctl
      - run: evalctl run fixbug --run-id ci      # fails the job on a required scorer
      - run: evalctl report ci --format junit > results.xml
        if: always()
      - uses: actions/upload-artifact@v4         # keep the report to read later
        if: always()
        with:
          name: evalctl-run
          path: results.xml
```

The `run` step gates the check on its exit code. The `report` and upload steps run
even on failure (`if: always()`), so a red run still leaves the JUnit file and the
artifact to read. A JUnit reporter action is optional on top, to surface per-case
results in the checks UI.

Note that junit is a raw-output format: it prints the XML itself, so it doesn't
take `--json` and isn't paginated.

## When you need each

- **Always:** CI to run the job and gate the branch.
- **To grade an agent run inside it:** evalctl, as the step that produces the
  verdict CI acts on.
- **For per-case results, not one pass/fail:** evalctl's JUnit output plus a test
  reporter.
- **For a replayable record of the run:** evalctl's run directory, uploaded as an
  artifact.

## Don't say

- Don't say evalctl replaces CI. It runs inside a CI job.
- Don't say CI can grade an agent run on its own. It gates on an exit code;
  something has to produce that exit code from the agent's work.
- Don't say GitHub renders the JUnit file by itself. The exit code gates the
  check; a reporter step is what turns the XML into per-case annotations.
- Don't say evalctl needs a service or account to run in CI. It installs with
  `pip` and needs no model key by default.

## Sources

- evalctl JUnit report (`report --format junit`, shipped in 1.1.0):
  [Changelog](/docs/changelog/)
- evalctl replay and portable run directory: [Replay](/docs/replay/)
- evalctl scorers (what produces the pass/fail):
  [Scorers and command scorers](/docs/command-scorers/)
- evalctl scope and sandboxing: [How evalctl compares](/docs/comparison/),
  [Security and sandboxing](/docs/security/)
