"""Action executor.

Receives the story (any text the VLM produced) via stdin JSON. Scans
the text for executable function calls using AST parsing. Executes
any valid calls found. Returns everything -- the full narrative, the
extracted calls, any errors, and a screenshot.

The story is not code. It is a living text that may contain function
calls anywhere within it -- between sentences, after observations,
inside reasoning blocks, wrapped in markdown, or standing alone. The
executor finds them all without rejecting anything.

Strategy:
  1. If markdown code fences exist, extract their contents
  2. Parse each line of the resulting text as a Python statement
  3. Lines that parse as valid function calls are executed
  4. Lines that fail parsing are silently skipped (they are narrative)
  5. All executed actions and any errors are reported in feedback
  6. The story is never modified -- only actions are extracted from it

Designed for Python 3.13 on Windows 11. No pip dependencies.
"""

from __future__ import annotations

import ast
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Final

import config as franz_config
import tools

CAPTURE_SCRIPT: Final = Path(__file__).parent / "capture.py"

_FUNC_LIST: Final = ", ".join(tools.TOOL_NAMES)

_SAFE_BUILTINS: Final[dict[str, object]] = {
    n: (
        __builtins__[n] if isinstance(__builtins__, dict) and n in __builtins__
        else getattr(__builtins__, n, None)
    )
    for n in (
        "range", "int", "str", "float", "bool", "len", "abs",
        "max", "min", "round", "sorted", "reversed",
        "list", "tuple", "dict", "set", "frozenset",
        "enumerate", "zip", "map", "filter",
        "isinstance", "type", "True", "False", "None",
    )
    if (isinstance(__builtins__, dict) and n in __builtins__)
    or hasattr(__builtins__, n)
}

_FENCE_RE: Final = re.compile(
    r"```(?:python|py)?\s*\n(.*?)```",
    re.DOTALL,
)


def _log(msg: str) -> None:
    sys.stderr.write(f"[execute.py] {msg}\n")
    sys.stderr.flush()


def _extract_executable_lines(raw: str) -> list[str]:
    """Extract lines that look like valid Python function calls.

    Strategy:
    1. If markdown fences found, extract their content first
    2. Split all available text into individual lines
    3. Try to parse each line as a Python expression
    4. Keep only lines that are function calls to known tools
    5. Skip everything else silently -- it is narrative, not code

    Returns list of executable line strings.
    """
    # Gather candidate text: fenced blocks if present, else full text
    fenced = _FENCE_RE.findall(raw)
    if fenced:
        candidate_text = "\n".join(b.strip() for b in fenced)
        _log(f"Found {len(fenced)} markdown code block(s)")
    else:
        candidate_text = raw

    # Also include lines outside fences that might be bare function calls
    # by scanning the full raw text line by line
    all_lines: list[str] = []
    seen: set[str] = set()

    for source in ([candidate_text] if fenced else []) + [raw]:
        for line in source.splitlines():
            stripped = line.strip()
            if not stripped or stripped in seen:
                continue
            seen.add(stripped)
            all_lines.append(stripped)

    # Filter to valid function calls targeting known tools
    executable: list[str] = []
    for line in all_lines:
        try:
            tree = ast.parse(line, mode="eval")
        except SyntaxError:
            continue
        if not isinstance(tree.body, ast.Call):
            continue
        # Get the function name
        func = tree.body.func
        if isinstance(func, ast.Name) and func.id in tools.TOOL_NAMES:
            executable.append(line)
        elif isinstance(func, ast.Attribute) and func.attr in tools.TOOL_NAMES:
            executable.append(line)

    return executable


def _make_namespace(run_dir: str) -> dict[str, object]:
    ns: dict[str, object] = {"__builtins__": dict(_SAFE_BUILTINS)}
    for name in tools.TOOL_NAMES:
        ns[name] = getattr(tools, name)
    printed: list[str] = []

    def _print(*args: object, **kwargs: object) -> None:
        text = kwargs.get("sep", " ").join(str(a) for a in args)
        end = str(kwargs.get("end", "\n"))
        full = text + end
        printed.append(full)
        tools.write(full)

    ns["print"] = _print
    ns["_printed"] = printed
    return ns


def _hint(status: str) -> str:
    sl = status.lower()
    if "nameerror" in sl:
        return f"{status}\n  (Available: {_FUNC_LIST}. No imports.)"
    if "valueerror" in sl and "1000" in sl:
        return f"{status}\n  (Coordinates: integers 0-1000)"
    if "typeerror" in sl:
        return f"{status}\n  (Use help(fn) for signature)"
    return status


def _run_capture(actions: list[str], run_dir: str) -> str:
    try:
        r = subprocess.run(
            [sys.executable, str(CAPTURE_SCRIPT)],
            input=json.dumps({"actions": actions, "run_dir": run_dir}),
            capture_output=True,
            text=True,
            timeout=60,
        )
    except subprocess.TimeoutExpired:
        _log("ERROR: capture.py timed out after 60s")
        return ""
    except Exception as exc:
        _log(f"ERROR: capture.py failed to start: {exc}")
        return ""

    if r.stderr and r.stderr.strip():
        for line in r.stderr.strip().splitlines():
            _log(f"[capture] {line}")

    if r.returncode != 0:
        _log(f"[capture] exited with code {r.returncode}")

    if not r.stdout or not r.stdout.strip():
        _log("[capture] WARNING: empty stdout -- no screenshot produced")
        return ""

    try:
        data = json.loads(r.stdout)
        b64 = str(data.get("screenshot_b64", ""))
        if not b64:
            _log("[capture] WARNING: screenshot_b64 is empty in JSON output")
            if "error" in data:
                _log(f"[capture] error reported: {data['error']}")
        else:
            _log(f"[capture] screenshot captured: {len(b64)} chars base64")
        return b64
    except json.JSONDecodeError:
        _log(f"[capture] JSON parse failed. stdout preview: {r.stdout[:300]}")
        return ""


def _output(data: dict) -> None:
    sys.stdout.write(json.dumps(data))
    sys.stdout.flush()


def main() -> None:
    try:
        req = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError:
        _log("ERROR: failed to parse stdin JSON")
        _output({
            "executed": [], "extracted_code": [], "malformed": ["Invalid input JSON"],
            "ignored": [], "screenshot_b64": "", "feedback": "Internal error: bad input",
        })
        return

    raw = str(req.get("raw", ""))
    run_dir = str(req.get("run_dir", ""))

    master = bool(franz_config.EXECUTE_ACTIONS)
    overlay_debug = bool(franz_config.OVERLAY_DEBUG)
    physical = bool(franz_config.PHYSICAL_EXECUTION) and not overlay_debug

    tools.configure(execute=master, physical=physical, run_dir=run_dir)
    ns = _make_namespace(run_dir)

    # Extract executable function calls from the story
    executable_lines = _extract_executable_lines(raw.strip())
    _log(f"Extracted {len(executable_lines)} executable lines from story")

    # Execute each extracted line individually
    errors: list[str] = []
    for line in executable_lines:
        try:
            compiled = compile(line, "<agent>", "eval")
            eval(compiled, ns)
        except Exception as exc:
            err = f"{type(exc).__name__}: {exc}"
            errors.append(err)
            _log(f"Execution error on '{line[:80]}': {err}")

    executed, ignored = tools.get_results()
    _log(f"Executed {len(executed)} actions, {len(ignored)} ignored, "
         f"{len(errors)} errors")

    screenshot_b64 = _run_capture(executed, run_dir)

    # Build feedback -- the story of what happened this turn
    parts: list[str] = []
    for action in executed:
        parts.append(f"{action} -> OK")
    for err in errors:
        parts.append(_hint(err))
    if not executed and not errors:
        parts.append(f"No actions found in your story. "
                     f"You can use: {_FUNC_LIST}")
    if not screenshot_b64:
        parts.append("(Screenshot capture failed)")

    feedback = "\n".join(parts)

    _output({
        "executed": executed,
        "extracted_code": executable_lines,
        "malformed": errors,
        "ignored": ignored,
        "screenshot_b64": screenshot_b64,
        "feedback": feedback,
    })


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        _log(f"FATAL: {exc}")
        try:
            _output({
                "executed": [], "extracted_code": [], "malformed": [str(exc)],
                "ignored": [], "screenshot_b64": "",
                "feedback": f"Internal executor error: {exc}",
            })
        except Exception:
            pass
