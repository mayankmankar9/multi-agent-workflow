#!/usr/bin/env python3
"""Stop hook: refuse to end a worker session that wrote nothing back.

Rule 2 of the blackboard: every worker must write back before it exits. Prompt
text asking for it is a request; a Stop hook is a control.

Two things are required of an implementation worker:

1. ``implementation.md`` exists and is non-empty.
2. At least one context block was appended by someone other than the
   orchestrator.

**Exit code 2 is the only code that blocks.** Exit 1 blocks nothing -- it is the
single most common hooks bug, so this script never uses it for a refusal.

Registered as a cwd-relative command, so the copy that runs belongs to the
worker's cwd -- its recorded worktree, once worker execution is rooted there.
The evidence it checks is not in that copy's tree: ``implementation.md`` and
the blackboard live in the orchestrator checkout, and a guard resolving them
against the worker's cwd would check an empty directory and pass everything.
The authoritative location arrives as an absolute path from ``worker_env()``,
with cwd kept as the fallback outside an orchestrated run.
"""

import json
import os
import sys
from pathlib import Path

CONTEXT_FILENAME = "context.jsonl"

BLOCK = 2  # the only exit code the harness treats as a refusal
ALLOW = 0


def repo_root():
    """The orchestrator checkout, where task evidence is single-homed."""
    raw = (os.environ.get("ORCHESTRATOR_REPO_ROOT") or "").strip()

    if raw and Path(raw).is_dir():
        return Path(raw)

    return Path.cwd()


def task_directory(task_id):
    raw = (os.environ.get("ORCHESTRATOR_TASK_DIR") or "").strip()

    if raw:
        return Path(raw)

    return repo_root() / ".ai" / "tasks" / task_id


class Unreadable(Exception):
    """A file the guard must read could not be decoded.

    Raised rather than swallowed so the caller blocks. Reading a
    worker-writable artifact with a strict codec and no handler meant an
    undecodable byte killed the script with exit 1 -- and exit 1 blocks
    nothing, so the write-back guard simply did not run. Switching to UTF-8
    made that worse, not better: the previous locale decode on Windows rejected
    five byte values, while UTF-8 rejects any malformed high-byte sequence.

    A guard that cannot read its own input must fail closed.
    """


def read_guarded(path):
    """Read a file the guard's verdict depends on, or refuse to have a verdict."""
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise Unreadable(
            "%s is not valid UTF-8 (%s). The guard cannot confirm the "
            "write-back happened, so it refuses rather than assuming it did."
            % (path, exc)
        )
    except OSError as exc:
        raise Unreadable("%s could not be read (%s)." % (path, exc))


def context_blocks(task_id):
    path = task_directory(task_id) / CONTEXT_FILENAME

    if not path.is_file():
        return []

    found = []

    for line in read_guarded(path).splitlines():
        if not line.strip():
            continue

        try:
            found.append(json.loads(line))
        except json.JSONDecodeError:
            continue

    return found


def main():
    task_id = os.environ.get("ORCHESTRATOR_TASK_ID", "").strip()

    # Outside an orchestrated worker run there is nothing to enforce.
    if not task_id:
        return ALLOW

    if os.environ.get("ORCHESTRATOR_SKIP_STOP_GUARD") == "1":
        return ALLOW

    task_dir = task_directory(task_id)
    problems = []

    notes = task_dir / "implementation.md"

    try:
        if not notes.is_file() or not read_guarded(notes).strip():
            problems.append(
                "%s is missing or empty. Write what you changed, any deviation "
                "from the approved plan, and anything you could not complete."
                % notes
            )

        written = [
            block
            for block in context_blocks(task_id)
            if block.get("author") and block["author"] != "orchestrator"
        ]
    except Unreadable as exc:
        # Block, do not die. Exit 1 here would let the session end with the
        # guard silently skipped, which is the failure this catches.
        sys.stderr.write("Session cannot end yet:\n\n- %s\n" % exc)
        return BLOCK

    if not written:
        problems.append(
            "No context block was appended to %s. Record what you learned "
            "that the next worker would otherwise rediscover -- a "
            "repo_finding, a decision, a constraint_discovered, or a "
            "deviation." % (task_dir / CONTEXT_FILENAME)
        )

    if not problems:
        return ALLOW

    sys.stderr.write(
        "Session cannot end yet:\n\n"
        + "\n\n".join("- " + problem for problem in problems)
        + "\n"
    )
    return BLOCK


if __name__ == "__main__":
    raise SystemExit(main())
