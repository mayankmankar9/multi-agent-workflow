#!/usr/bin/env python3
"""PostToolUse hook: byte-compile a Python file right after it is edited.

Surfaces a syntax error inside the implementation loop, where the worker can
still fix it cheaply, rather than at validation time where it costs a full
round trip through the state machine.

Advisory by design: it reports on stderr and exits 0. A formatter or linter
disagreement should not deny a tool call that already succeeded. Only the
PreToolUse hook denies.
"""

import json
import py_compile
import sys
from pathlib import Path


def main():
    raw = sys.stdin.read()

    if not raw.strip():
        return 0

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return 0

    if not isinstance(payload, dict):
        return 0

    tool_input = payload.get("tool_input") or payload.get("input") or {}

    if not isinstance(tool_input, dict):
        return 0

    target = tool_input.get("file_path") or tool_input.get("path")

    if not target or not str(target).endswith(".py"):
        return 0

    path = Path(target)

    if not path.is_file():
        return 0

    try:
        py_compile.compile(str(path), doraise=True, cfile=None)
    except py_compile.PyCompileError as exc:
        sys.stderr.write(
            "%s does not compile:\n%s\nFix this before continuing.\n"
            % (path, exc)
        )
    except OSError:
        pass

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
