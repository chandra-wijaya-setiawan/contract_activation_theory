"""outline tool — structural outline of a Python file via jedi (docker-only)."""
from __future__ import annotations

import base64
import json
import uuid
from typing import Callable

from cat.capability.registry import Capability, ToolResult
from cat.contract.model import State
from cat.capability.tools._helpers import _truncate, OUTPUT_CAP_BYTES

_SCHEMA = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "description": "file path (relative to root or absolute)"},
        "root": {"type": "string", "description": "project root (default /testbed)"},
    },
    "required": ["path"],
}

# Shared jedi script template used by outline/goto_def/find_refs
_JEDI_SCRIPT = (
    "import base64, json, os, subprocess, sys\n"
    "{args_line}\n"
    "try:\n"
    "    import jedi\n"
    "except ImportError:\n"
    "    subprocess.check_call([sys.executable, '-m', 'pip', 'install',\n"
    "        '--quiet', '--disable-pip-version-check', '--user', 'jedi>=0.19'])\n"
    "    sys.path.insert(0, os.path.expanduser('~/.local/lib/python%d.%d/site-packages' % sys.version_info[:2]))\n"
    "    import jedi\n"
    "root = args['root']\n"
    "project = jedi.Project(root)\n"
    "action = args['action']\n"
    "out = []\n"
    "def _to_row(n):\n"
    "    p = str(n.module_path) if n.module_path else ''\n"
    "    return {{'path': p, 'line': n.line, 'col': n.column, 'type': n.type, 'name': n.name}}\n"
    "if action == 'goto_def':\n"
    "    name = args['name']\n"
    "    seen = set()\n"
    "    for hit in list(project.search(name))[:30]:\n"
    "        try:\n"
    "            for d in hit.goto(follow_imports=True):\n"
    "                row = _to_row(d)\n"
    "                key = (row['path'], row['line'], row['name'])\n"
    "                if key in seen: continue\n"
    "                seen.add(key)\n"
    "                if row['name'] == name or row['name'].endswith('.' + name):\n"
    "                    out.append(row)\n"
    "        except Exception as e:\n"
    "            continue\n"
    "    out = out[:20]\n"
    "elif action == 'find_refs':\n"
    "    name = args['name']\n"
    "    defs = []\n"
    "    for hit in list(project.search(name))[:10]:\n"
    "        if hit.name == name:\n"
    "            try:\n"
    "                defs.extend(hit.goto(follow_imports=True))\n"
    "            except Exception:\n"
    "                pass\n"
    "    if not defs:\n"
    "        print(json.dumps({{'error': f'no definition found for {name!r}'}})); sys.exit(0)\n"
    "    d = defs[0]\n"
    "    if not d.module_path:\n"
    "        print(json.dumps({{'error': 'definition has no module_path'}})); sys.exit(0)\n"
    "    script = jedi.Script(path=str(d.module_path), project=project)\n"
    "    refs = script.get_references(d.line, d.column, scope='project')\n"
    "    for r in refs[:50]:\n"
    "        row = _to_row(r)\n"
    "        try:\n"
    "            lines = open(row['path']).read().splitlines()\n"
    "            row['snippet'] = lines[row['line']-1].strip()[:120] if row['line'] <= len(lines) else ''\n"
    "        except Exception:\n"
    "            row['snippet'] = ''\n"
    "        out.append(row)\n"
    "elif action == 'outline':\n"
    "    path = args['path']\n"
    "    if not os.path.isabs(path):\n"
    "        path = os.path.join(root, path)\n"
    "    script = jedi.Script(path=path, project=project)\n"
    "    names = script.get_names(all_scopes=False, definitions=True, references=False)\n"
    "    for n in names:\n"
    "        out.append({{'name': n.name, 'type': n.type, 'line': n.line}})\n"
    "print(json.dumps(out, indent=2))\n"
)


def _run_symbol_nav(action: str, args: dict, exec_fn: Callable, default_workdir: str, tool_name: str, state: State) -> ToolResult:
    """Shared dispatcher for goto_def / find_refs / outline."""
    root = str(args.get("root") or default_workdir)
    payload = {"action": action, "root": root, **args}
    payload_b64 = base64.b64encode(json.dumps(payload).encode("utf-8")).decode("ascii")
    args_line = f"args = json.loads(base64.b64decode('{payload_b64}').decode('utf-8'))"
    script_text = _JEDI_SCRIPT.format(args_line=args_line)
    script_b64 = base64.b64encode(script_text.encode("utf-8")).decode("ascii")
    tmp = f"/tmp/cat_symnav_{uuid.uuid4().hex[:8]}.py"
    sh = (
        f"echo {script_b64} | base64 -d > {tmp} && "
        f"python3 {tmp}; rc=$?; rm -f {tmp}; exit $rc"
    )
    exit_code, output = exec_fn(["bash", "-lc", sh])
    return ToolResult(
        tool=tool_name,
        is_error=(exit_code != 0),
        output=_truncate(output, OUTPUT_CAP_BYTES),
        detail={"returncode": exit_code, "action": action},
    )


def create_outline_capability(exec_fn: Callable, default_workdir: str = "/testbed") -> Capability:
    """Create the outline capability bound to a docker exec function."""

    def _handle(args: dict, state: State) -> ToolResult:
        path = args.get("path")
        if not isinstance(path, str) or not path:
            return ToolResult(tool="outline", is_error=True, output="missing 'path'")
        return _run_symbol_nav("outline", args, exec_fn, default_workdir, "outline", state)

    return Capability(
        name="outline",
        description=(
            "List top-level classes, functions, and module-level names in a Python file "
            "(structural outline). Returns {name, type, line} per entry. "
            "Prefer this over `read` when you only need to know what's in a file."
        ),
        json_schema=_SCHEMA,
        handler=_handle,
    )
