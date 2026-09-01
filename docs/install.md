---
title: Install
description: Install evalctl. Python 3.11+, standard library only, no runtime dependencies.
bucket: guides
order: 1
---

# Install

evalctl requires **Python 3.11+** and has **no runtime dependencies** — it uses
the standard library only. There is no gateway, dashboard, or SaaS account.

## From PyPI

```bash
pip install evalctl
```

Or install it as an isolated CLI tool with [uv](https://docs.astral.sh/uv/):

```bash
uv tool install evalctl
```

## From source

For an unreleased revision, install straight from the repository:

```bash
pip install "git+https://github.com/evalctl/evalctl.git"
```

## For development

Clone and install editable:

```bash
git clone https://github.com/evalctl/evalctl.git
cd evalctl
pip install -e .
```

## Verify

```bash
evalctl --version   # 0.4.4
evalctl capabilities --json
```

`capabilities --json` prints the full machine contract: verbs, flags, exit
codes, the error-code registry, environment variables, and integration status.
It is the authoritative description of the tool surface for the installed
version.

## Optional: spoolctl

The queue path is optional. If you want to delegate runner execution to a
worker, install [spoolctl](https://github.com/Ozhiaki/spoolctl) `>= 0.4.11`
(queue contract v2).
Everything else — scaffold, validate, run, resume, score, report, replay — works
with no external service. See [The spoolctl queue](/docs/spoolctl-queue/).
