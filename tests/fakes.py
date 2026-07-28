from __future__ import annotations

import json
import subprocess
from pathlib import Path


def install_fake_spoolctl(cwd: Path, *, version: str = "0.4.11", capabilities_shape: str = "real",
                          capability_flags: object | None = None, include_version: bool = True,
                          data_version: str | None = None) -> Path:
    bindir = cwd / "bin"
    bindir.mkdir(exist_ok=True)
    script = bindir / "spoolctl"
    if capability_flags is None:
        if capabilities_shape == "compact":
            capability_flags = ["--cwd", "--env", "--max-crashes"]
        else:
            capability_flags = [
                {"flag": "--cwd", "type": "str", "default": None},
                {"flag": "--env", "type": "str", "default": []},
                {"flag": "--max-crashes", "type": "int", "default": None},
            ]
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import fcntl, json, os, subprocess, sys, tempfile, time\n"
        "from pathlib import Path\n"
        f"VERSION = {version!r}\n"
        f"CAPABILITIES_SHAPE = {capabilities_shape!r}\n"
        f"CAPABILITY_FLAGS = {capability_flags!r}\n"
        f"INCLUDE_VERSION = {include_version!r}\n"
        f"DATA_VERSION = {data_version!r}\n"
        "def emit(data, code=0):\n"
        "    print(json.dumps({'ok': True, 'data': data}, sort_keys=True))\n"
        "    raise SystemExit(code)\n"
        "def emit_envelope(data, code=0, tool_version=VERSION):\n"
        "    env = {'ok': True, 'data': data}\n"
        "    if INCLUDE_VERSION and tool_version is not None:\n"
        "        env['tool_version'] = tool_version\n"
        "    print(json.dumps(env, sort_keys=True))\n"
        "    raise SystemExit(code)\n"
        "class dblock:\n"
        "    def __init__(self, db): self.db = db\n"
        "    def __enter__(self):\n"
        "        p = Path(self.db); p.parent.mkdir(parents=True, exist_ok=True)\n"
        "        self.fh = open(str(p) + '.lock', 'a+')\n"
        "        fcntl.flock(self.fh, fcntl.LOCK_EX); return self\n"
        "    def __exit__(self, *exc):\n"
        "        fcntl.flock(self.fh, fcntl.LOCK_UN); self.fh.close(); return False\n"
        "def load(db):\n"
        "    p = Path(db)\n"
        "    if not p.exists(): return {'jobs': {}, 'keys': {}, 'next_job': 0}\n"
        "    return json.loads(p.read_text())\n"
        "def save(db, data):\n"
        "    p = Path(db); p.parent.mkdir(parents=True, exist_ok=True)\n"
        "    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=p.name + '.tmp')\n"
        "    try:\n"
        "        with os.fdopen(fd, 'w') as fh: fh.write(json.dumps(data, sort_keys=True))\n"
        "        os.replace(tmp, str(p))\n"
        "    except BaseException:\n"
        "        Path(tmp).unlink(missing_ok=True); raise\n"
        "args = sys.argv[1:]\n"
        "if args[:2] == ['capabilities', '--json']:\n"
        "    if CAPABILITIES_SHAPE == 'sleep': time.sleep(30)\n"
        "    if CAPABILITIES_SHAPE == 'bad-json':\n"
        "        print('{not json'); raise SystemExit(0)\n"
        "    if CAPABILITIES_SHAPE == 'error-envelope':\n"
        "        print(json.dumps({'ok': False, 'errors': [{'code': 'BROKEN'}]}, sort_keys=True)); raise SystemExit(0)\n"
        "    if CAPABILITIES_SHAPE == 'exit4':\n"
        "        print(json.dumps({'ok': False, 'errors': [{'code': 'TRANSIENT'}]}, sort_keys=True)); raise SystemExit(4)\n"
        "    data = {'contract_version': '1', 'verbs': {'add': {'flags': CAPABILITY_FLAGS}}}\n"
        "    if INCLUDE_VERSION and (CAPABILITIES_SHAPE in ('compact', 'raw') or DATA_VERSION is not None):\n"
        "        data['version'] = DATA_VERSION or VERSION\n"
        "    if CAPABILITIES_SHAPE == 'raw':\n"
        "        print(json.dumps(data, sort_keys=True)); raise SystemExit(0)\n"
        "    if CAPABILITIES_SHAPE == 'compact':\n"
        "        emit(data)\n"
        "    emit_envelope(data)\n"
        "cmd = args[0] if args else ''\n"
        "if os.environ.get('FAKE_SPOOLCTL_TRANSIENT') and cmd == 'wait':\n"
        "    print(json.dumps({'ok': False, 'errors': [{'code': 'TRANSIENT'}]})); raise SystemExit(4)\n"
        "def val(flag):\n"
        "    return args[args.index(flag)+1]\n"
        "if cmd == 'add':\n"
        "    db = val('--db'); key = val('--key'); cwd = val('--cwd'); timeout = int(val('--timeout'))\n"
        "    sep = args.index('--'); command = args[sep+1:]\n"
        "    env = {}\n"
        "    i = 0\n"
        "    while i < sep:\n"
        "        if args[i] == '--env':\n"
        "            k, v = args[i+1].split('=', 1); env[k] = v; i += 2\n"
        "        else: i += 1\n"
        "    with dblock(db):\n"
        "        data = load(db)\n"
        "        if key in data['keys']:\n"
        "            job_id = data['keys'][key]\n"
        "        else:\n"
        "            seq = data.get('next_job', len(data['jobs'])) + 1\n"
        "            data['next_job'] = seq\n"
        "            job_id = 'job-' + str(seq); data['keys'][key] = job_id\n"
        "            data['jobs'][job_id] = {'id': job_id, 'key': key, 'cwd': cwd, 'env': env, 'command': command, 'timeout': timeout, 'state': 'queued', 'attempts': []}\n"
        "            save(db, data)\n"
        "    emit({'job_id': job_id, 'state': data['jobs'][job_id]['state']})\n"
        "if cmd == 'work':\n"
        "    db = val('--db')\n"
        "    with dblock(db):\n"
        "        data = load(db)\n"
        "        base = Path(db).parent\n"
        "        for job in data['jobs'].values():\n"
        "            if job['state'] not in ('queued', 'running'): continue\n"
        "            out = base / (job['id'] + '.stdout.txt'); err = base / (job['id'] + '.stderr.txt')\n"
        "            env = os.environ.copy(); env.update(job['env'])\n"
        "            start = time.time()\n"
        "            try:\n"
        "                res = subprocess.run(job['command'], cwd=job['cwd'], env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=job['timeout'])\n"
        "                out.write_text(res.stdout); err.write_text(res.stderr)\n"
        "                state = 'succeeded' if res.returncode == 0 else 'failed'; exit_code = res.returncode; error = None\n"
        "            except subprocess.TimeoutExpired as exc:\n"
        "                out.write_text((exc.stdout or '') if isinstance(exc.stdout, str) else (exc.stdout or b'').decode('utf-8', 'replace'))\n"
        "                err.write_text((exc.stderr or '') if isinstance(exc.stderr, str) else (exc.stderr or b'').decode('utf-8', 'replace'))\n"
        "                state = 'timed_out'; exit_code = None; error = 'timed out'\n"
        "            except OSError as exc:\n"
        "                out.write_text(''); err.write_text(str(exc)); state = 'failed'; exit_code = None; error = 'spawn failed: ' + str(exc)\n"
        "            job['state'] = state\n"
        "            job['attempts'] = [{'state': state, 'exit_code': exit_code, 'error': error, 'stdout_path': str(out), 'stderr_path': str(err), 'duration_ms': int((time.time()-start)*1000)}]\n"
        "        save(db, data)\n"
        "    emit({'drained': True})\n"
        "if cmd == 'wait':\n"
        "    db = val('--db'); ids = [a for a in args[args.index('--json')+1:] if not a.startswith('--')]\n"
        "    with dblock(db):\n"
        "        data = load(db)\n"
        "    all_succeeded = all(data['jobs'][j]['state'] == 'succeeded' for j in ids)\n"
        "    emit({'all_succeeded': all_succeeded, 'jobs': [data['jobs'][j] for j in ids]}, 0 if all_succeeded else 6)\n"
        "if cmd == 'show':\n"
        "    db = val('--db'); job_id = args[-1]\n"
        "    with dblock(db):\n"
        "        data = load(db)\n"
        "    emit(data['jobs'][job_id])\n"
        "print('bad fake spoolctl invocation', args, file=sys.stderr); raise SystemExit(2)\n"
    )
    script.chmod(0o755)
    return bindir

def install_fake_inferctl(cwd: Path, *, capabilities_shape: str = "compatible",
                          preflight_mode: str = "success", route_mode: str = "success",
                          envelope_shape: bool = True) -> Path:
    bindir = cwd / "bin"
    bindir.mkdir(exist_ok=True)
    script = bindir / "inferctl"
    # Shapes copied from inferctl v0.2 contract goldens:
    # internal/contract/capabilities.golden.json and testdata/contract/{preflight,route}.golden.json.
    capabilities = {
        "tool": "inferctl",
        "binary": "inferctl",
        "contract_version": "0.2",
        "features": ["json_envelope", "contract_goldens", "mega_command_plan"],
        "global_flags": {
            "--help": {"type": "bool", "default": False, "description": "Show terse human help."},
            "--json": {"type": "bool", "default": False, "description": "Emit the universal JSON envelope."},
        },
        "verbs": [
            {
                "name": "route",
                "summary": "Compute and explain a route for a configured task.",
                "mega_command": "PLAN",
                "flags": [
                    {"name": "--prompt-file", "type": "string", "default": None},
                    {"name": "--prompt", "type": "string", "default": None},
                    {"name": "--from-stdin", "type": "bool", "default": False},
                    {"name": "--json", "type": "bool", "default": False},
                ],
                "args": [{"name": "task", "required": True}],
                "exit_codes": [0, 1, 3, 4],
                "output_schema_ref": "#/schemas/route_explanation",
                "emits_data_on_failure": False,
            },
            {
                "name": "preflight",
                "summary": "Decide whether automation may attempt a configured task.",
                "mega_command": "TASK_READINESS",
                "flags": [
                    {"name": "--prompt-file", "type": "string", "default": None},
                    {"name": "--prompt", "type": "string", "default": None},
                    {"name": "--from-stdin", "type": "bool", "default": False},
                    {"name": "--allow-fallback", "type": "bool", "default": False},
                    {"name": "--require-ready", "type": "bool", "default": False},
                    {"name": "--json", "type": "bool", "default": False},
                ],
                "args": [{"name": "task", "required": True}],
                "exit_codes": [0, 1, 3, 4, 5],
                "output_schema_ref": "#/schemas/preflight_report",
                "emits_data_on_failure": True,
            },
        ],
    }
    route = {
        "task": "code",
        "input": {"prompt_chars": 0, "estimated_tokens": 0, "source": "none"},
        "decision": {
            "selected_model": "qwen3:8b",
            "selected_backend": "ollama",
            "is_fallback": False,
            "fallback_index": None,
            "ready": True,
            "estimated_first_token_ms": None,
            "estimated_total_ms": None,
            "reason": "primary model is available",
        },
        "candidates": [
            {
                "model": "qwen3:8b",
                "backend": "ollama",
                "role": "primary",
                "fallback_index": None,
                "available": True,
                "unavailability_reason": None,
                "loaded": True,
                "estimated_first_token_ms": None,
            }
        ],
        "constraints": {
            "profile": "default_local_workstation",
            "max_context_tokens": 8192,
            "context_used_tokens": 0,
            "context_pct": 0,
            "max_concurrent_models": 1,
            "current_loaded_count": 1,
            "allow_premium": False,
            "selected_is_premium": None,
        },
    }
    preflight = {
        "policy": {"allow_fallback": False, "require_ready": False},
        "preflight_schema_version": "0.1",
        "prompt": {"source_kind": "none", "source": "none", "prompt_chars": 0, "estimated_tokens": 0},
        "recommended_action": {
            "command": "inferctl route code --json",
            "rationale": "Review the underlying route candidates and constraints",
            "alternatives": [{"command": "inferctl backends --filter ollama --json", "rationale": "Check reachability"}],
        },
        "route": route,
        "route_decision": route["decision"],
        "runnability": {"status": "runnable", "runnable": True, "exit_code": 0, "reason": "route satisfies preflight policy"},
        "runnability_status": "runnable",
        "runnable": True,
        "summary": {"status": "runnable", "message": "route satisfies preflight policy"},
        "warnings": [],
    }
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys, time\n"
        f"CAPABILITIES = {capabilities!r}\n"
        f"PREFLIGHT = {preflight!r}\n"
        f"ROUTE = {route!r}\n"
        f"CAPABILITIES_SHAPE = {capabilities_shape!r}\n"
        f"PREFLIGHT_MODE = {preflight_mode!r}\n"
        f"ROUTE_MODE = {route_mode!r}\n"
        f"ENVELOPE_SHAPE = {envelope_shape!r}\n"
        "def emit(data, code=0):\n"
        "    print(json.dumps({'ok': True, 'data': data}, sort_keys=True) if ENVELOPE_SHAPE else json.dumps(data, sort_keys=True))\n"
        "    raise SystemExit(code)\n"
        "args = sys.argv[1:]\n"
        "if args[:2] == ['capabilities', '--json']:\n"
        "    if CAPABILITIES_SHAPE == 'invalid-json': print('{not json'); raise SystemExit(0)\n"
        "    data = dict(CAPABILITIES)\n"
        "    if CAPABILITIES_SHAPE == 'missing-preflight': data['verbs'] = [v for v in data['verbs'] if v['name'] != 'preflight']\n"
        "    if CAPABILITIES_SHAPE == 'preflight-only': data['verbs'] = [v for v in data['verbs'] if v['name'] != 'route']\n"
        "    if CAPABILITIES_SHAPE == 'error-envelope': print(json.dumps({'ok': False, 'errors': [{'code': 'BROKEN'}]}, sort_keys=True)); raise SystemExit(0)\n"
        "    emit(data)\n"
        "if args and args[0] == 'preflight':\n"
        "    if PREFLIGHT_MODE == 'hang': time.sleep(30)\n"
        "    if PREFLIGHT_MODE == 'invalid-json': print('{not json'); raise SystemExit(0)\n"
        "    data = dict(PREFLIGHT)\n"
        "    if PREFLIGHT_MODE == 'nonzero-with-data':\n"
        "        data['runnability'] = {'status': 'config_error', 'runnable': False, 'exit_code': 3, 'reason': 'configuration unavailable'}\n"
        "        data['runnability_status'] = 'config_error'; data['runnable'] = False; data['summary'] = {'status': 'config_error', 'message': 'configuration unavailable'}\n"
        "        emit(data, 3)\n"
        "    if PREFLIGHT_MODE == 'policy-blocked':\n"
        "        data['runnability'] = {'status': 'policy_blocked', 'runnable': False, 'exit_code': 5, 'reason': 'policy blocked route'}\n"
        "        data['runnability_status'] = 'policy_blocked'; data['runnable'] = False; data['summary'] = {'status': 'policy_blocked', 'message': 'policy blocked route'}\n"
        "        emit(data, 5)\n"
        "    if PREFLIGHT_MODE == 'fallback':\n"
        "        data['route_decision'] = dict(data['route_decision']); data['route_decision']['is_fallback'] = True\n"
        "        data['route'] = dict(data['route']); data['route']['decision'] = data['route_decision']\n"
        "        emit(data)\n"
        "    emit(data)\n"
        "if args and args[0] == 'route':\n"
        "    if ROUTE_MODE == 'nonzero-without-data': print('route failed', file=sys.stderr); raise SystemExit(3)\n"
        "    if ROUTE_MODE == 'invalid-json': print('{not json'); raise SystemExit(0)\n"
        "    emit(ROUTE)\n"
        "print('bad fake inferctl invocation', args, file=sys.stderr); raise SystemExit(2)\n"
    )
    script.chmod(0o755)
    return bindir

def run_fake_tool(bindir: Path, binary: str, args: list[str], expect: int = 0) -> dict:
    result = subprocess.run([str(bindir / binary), *args], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode != expect:
        raise AssertionError(f"stdout={result.stdout}\nstderr={result.stderr}")
    return json.loads(result.stdout)

