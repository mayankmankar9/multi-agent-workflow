#!/usr/bin/env python3
"""
PreToolUse hook: hard-deny what CLAUDE.md only asks for.

Everything the project instructions request -- do not write state.json, do not
touch .ai/scripts/, do not commit or push -- was enforced by prompt text, which
is a request, not a control. This makes it a guarantee.

**Exit code 2 is the only code that denies.** Exit 1 denies nothing; it is the
single most common hooks bug, so this script never uses it for a refusal.

The hook payload arrives as JSON on stdin. Unrecognised shapes are allowed
through rather than blocking on a payload change: a hook that fails closed on
its own input format would make the repo unusable after any harness update.

Two tiers, because they answer different questions:

*Evidence* -- state.json, events.jsonl, baseline.json, test-results.json,
validation.md, review-findings.json, critique-<plan-stem>-round-<round>.json --
is never writable by a worker. No plan can authorise a worker to write its own
results; that is forging, and a plan that asked for it would be the clearest
possible sign something had gone wrong. baseline.json belongs in this tier for
a specific reason: it is the record of what the tree looked like before the
worker ran, so a worker able to edit it could make its own output look
pre-existing, or make pre-existing files look like work it did. The critique
artifacts belong here for the mirror-image reason: they are a read-only
checker's verdict on a plan, and a worker able to author one could hand the
developer a critique nothing criticised.

This hook is registered as a cwd-relative command, so the copy that executes
belongs to the worker's cwd -- its recorded worktree, once worker execution is
rooted there. The approved plan and the task's evidence it authorises against
live in the orchestrator checkout, which arrives as an absolute path from
``worker_env()``; cwd remains the fallback outside an orchestrated run.

*Infrastructure* -- .ai/scripts/, .ai/schemas/, .ai/hooks/, settings.json -- is
denied unless the approved plan declares the file. This repository's only source
IS its workflow infrastructure, so a blanket denial meant no worker could ever
implement anything here. The developer's approval is already hash-bound to a
specific plan, and that plan already declares every file the change may touch;
this makes that declaration mean something at the tool boundary rather than only
at validation time, after the writes have happened.
"""

import hashlib
import json
import os
import re
import shlex
import sys
from pathlib import Path

DENY = 2  # the only exit code the harness treats as a denial
ALLOW = 0

# Results the orchestrator owns. Denied to a worker unconditionally: a plan
# cannot authorise a worker to write its own verdict.
PROTECTED_EVIDENCE_PATTERNS = (
    r"\.ai/tasks/[^/]+/state\.json$",
    r"\.ai/tasks/[^/]+/events\.jsonl$",
    r"\.ai/tasks/[^/]+/baseline\.json$",
    r"\.ai/tasks/[^/]+/test-results\.json$",
    r"\.ai/tasks/[^/]+/validation\.md$",
    r"\.ai/tasks/[^/]+/review-findings\.json$",
    # The whole naming family, not one example of it: a guard matching only
    # `critique-plan-round-1.json` would leave `critique-plan-v4-round-2.json`
    # forgeable, which is the artifact the developer actually reads at the
    # approval gate.
    r"\.ai/tasks/[^/]+/critique-[^/]+-round-[0-9]+\.json$",
)

# Workflow infrastructure. Denied to a worker unless the approved plan declares
# the file.
PROTECTED_INFRA_PATTERNS = (
    r"\.ai/scripts/",
    r"\.ai/schemas/",
    r"\.ai/hooks/",
    r"\.claude/settings\.json$",
)

WRITE_TOOLS = {"Edit", "Write", "NotebookEdit", "MultiEdit"}

# Git subcommands that publish or rewrite history.
FORBIDDEN_GIT = {"commit", "push", "reset", "rebase", "tag"}

TASKS_RELATIVE = Path(".ai") / "tasks"


def exported_dir(name):
    """An absolute directory the orchestrator exported, if it is usable."""
    raw = (os.environ.get(name) or "").strip()

    if not raw:
        return None

    candidate = Path(raw)

    return candidate if candidate.is_dir() else None


def repo_root():
    """The orchestrator checkout: where the approved plan and evidence live."""
    return exported_dir("ORCHESTRATOR_REPO_ROOT") or Path.cwd()


def worker_root():
    """The tree the worker is writing into: its worktree, or the checkout."""
    return exported_dir("ORCHESTRATOR_WORKER_ROOT") or Path.cwd()


def task_directory():
    raw = (os.environ.get("ORCHESTRATOR_TASK_DIR") or "").strip()

    if raw:
        return Path(raw)

    return repo_root() / TASKS_RELATIVE / task_id()


def worker_session():
    """Whether this session is a worker the orchestrator launched.

    The threat this hook exists for is a *worker* forging evidence: writing its
    own state.json, or committing on the developer's behalf.

    ORCHESTRATOR_TASK_ID is exported by worker_env() and by nothing else, so it
    is the same signal SessionStart and stop_guard already gate on.

    Developer-driven sessions are allowed to modify workflow infrastructure
    because the enforcement target is an orchestrator-launched worker.
    """
    return bool((os.environ.get("ORCHESTRATOR_TASK_ID") or "").strip())


def task_id():
    return (os.environ.get("ORCHESTRATOR_TASK_ID") or "").strip()


def normalise(path):
    """Canonicalise a path for matching.

    Note: ``lstrip("./")`` would be wrong here -- it strips any leading run of
    '.' and '/' characters, turning ".ai/tasks/x/state.json" into
    "ai/tasks/x/state.json" and defeating every pattern. Strip the "./" prefix
    explicitly instead.
    """
    candidate = (path or "").replace("\\", "/")

    while candidate.startswith("./"):
        candidate = candidate[2:]

    return candidate


def matches(path, patterns):
    candidate = normalise(path)

    return any(re.search(pattern, candidate) for pattern in patterns)


def relative_to_repo(path):
    """Candidate repo-relative forms of a path, for plan comparison.

    Agents pass absolute paths as often as relative ones. Comparing raw strings
    would let the same file be denied one way and allowed the other.

    Two roots are tried, because a worker rooted in a recorded worktree writes
    paths under that tree while the plan declares them relative to the
    repository. Both resolve to the same declared path, and a guard that knew
    only one of them would deny a write the developer had approved.
    """
    candidate = normalise(path)
    forms = [candidate]

    try:
        resolved = Path(candidate)

        if not resolved.is_absolute():
            resolved = worker_root() / resolved

        resolved = resolved.resolve()
    except OSError:
        return forms

    for root in (worker_root(), repo_root()):
        try:
            forms.append(resolved.relative_to(root.resolve()).as_posix())
        except (ValueError, OSError):
            continue

    return forms


def approved_plan():
    """The plan the developer approved, or None.

    Fails closed by returning None: every caller treats "no verifiable approved
    plan" as "authorise nothing". The hash is re-checked here rather than
    trusted from state, so a plan edited after approval authorises no writes --
    the same rule verify_approval() applies before the implementer is invoked,
    enforced again at the point the write actually happens.
    """
    directory = task_directory()

    try:
        state = json.loads((directory / "state.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None

    plan_name = state.get("plan_file") or "plan.json"
    plan_path = Path(plan_name)

    if not plan_path.is_absolute():
        # plan_file is recorded as a repo-relative path in some states and a
        # bare filename in others. Resolved against the orchestrator checkout,
        # never the worker's cwd: the plan the developer approved is the one in
        # the checkout, and a worktree copy of it would authorise its own bytes.
        candidate = repo_root() / plan_name
        plan_path = (
            candidate
            if candidate.is_file()
            else directory / Path(plan_name).name
        )

    try:
        raw = plan_path.read_bytes()
    except OSError:
        return None

    version = state.get("plan_version", 1)
    approval = None

    try:
        for line in (directory / "events.jsonl").read_text(
            encoding="utf-8"
        ).splitlines():
            if not line.strip():
                continue

            try:
                payload = json.loads(line)
            except ValueError:
                continue

            if (
                payload.get("event") == "PLAN_APPROVED"
                and payload.get("plan_version") == version
            ):
                approval = payload
    except OSError:
        return None

    if approval is None:
        return None

    if approval.get("plan_sha256") != hashlib.sha256(raw).hexdigest():
        # The plan changed after it was approved. The approval covers bytes
        # that are no longer on disk, so it authorises nothing.
        return None

    try:
        return json.loads(raw.decode("utf-8"))
    except ValueError:
        return None


def declared_files():
    """Paths the approved plan says this change may touch."""
    plan = approved_plan()

    if not isinstance(plan, dict):
        return set()

    declared = set()

    for key in ("files_to_modify", "files_to_create"):
        for entry in plan.get(key) or []:
            if isinstance(entry, dict) and entry.get("path"):
                declared.add(normalise(entry["path"]))
            elif isinstance(entry, str):
                declared.add(normalise(entry))

    return declared


def deny(message):
    sys.stderr.write(message + "\n")
    return DENY


def check_write(tool_input):
    for key in ("file_path", "path", "notebook_path"):
        target = tool_input.get(key)

        if not target:
            continue

        forms = relative_to_repo(target)

        if any(matches(form, PROTECTED_EVIDENCE_PATTERNS) for form in forms):
            return deny(
                "Denied: %s is orchestrator-owned evidence. The orchestrator "
                "writes task state, validation results and plan critiques; a "
                "worker writing them would be forging evidence. No plan can "
                "authorise this." % target
            )

        if any(matches(form, PROTECTED_INFRA_PATTERNS) for form in forms):
            declared = declared_files()

            if any(form in declared for form in forms):
                continue

            return deny(
                "Denied: %s is workflow infrastructure and the approved plan "
                "does not declare it. Declare every file the change will touch "
                "in files_to_modify or files_to_create, then re-approve -- "
                "approval is bound to the plan's bytes." % target
            )

    return ALLOW


def check_bash(tool_input):
    command = tool_input.get("command") or ""

    try:
        tokens = shlex.split(command)
    except ValueError:
        tokens = command.split()

    # git commit / push / history rewrites
    for index, token in enumerate(tokens):
        if token == "git" and index + 1 < len(tokens):
            subcommand = tokens[index + 1]

            if subcommand in FORBIDDEN_GIT:
                return deny(
                    "Denied: `git %s` is not permitted. The developer commits "
                    "and pushes; agents do not." % subcommand
                )

    # Shell redirection into a protected path, which would route a file
    # mutation around the Edit/Write permission surface entirely. Infrastructure
    # is included regardless of what the plan declares: a declared file may be
    # edited, but never through the shell.
    guarded = PROTECTED_EVIDENCE_PATTERNS + PROTECTED_INFRA_PATTERNS

    for match in re.finditer(r">>?\s*([^\s;|&]+)", command):
        if matches(match.group(1), guarded):
            return deny(
                "Denied: shell redirection into %s. Write files through "
                "Edit/Write, never through shell redirection -- redirection "
                "bypasses the tool-level permission surface."
                % match.group(1)
            )

    for token in tokens:
        if matches(token, guarded) and any(
            writer in tokens
            for writer in ("tee", "truncate", "dd", "sed")
        ):
            return deny("Denied: writing to %s through the shell." % token)

    return ALLOW


def main():
    raw = sys.stdin.read()

    if not raw.strip():
        return ALLOW

    if not worker_session():
        # A developer-driven session. The orchestrator-owned paths stay
        # documented in CLAUDE.md and the constitution; they are simply not
        # machine-enforced here, because the enforcement target is a worker.
        return ALLOW

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return ALLOW

    if not isinstance(payload, dict):
        return ALLOW

    tool = payload.get("tool_name") or payload.get("tool") or ""
    tool_input = payload.get("tool_input") or payload.get("input") or {}

    if not isinstance(tool_input, dict):
        return ALLOW

    if tool in WRITE_TOOLS:
        return check_write(tool_input)

    if tool == "Bash":
        return check_bash(tool_input)

    return ALLOW


if __name__ == "__main__":
    raise SystemExit(main())
