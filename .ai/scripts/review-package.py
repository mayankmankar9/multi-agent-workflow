#!/usr/bin/env python3
"""Build the developer review package for a task.

This script reports only what it actually observed. It previously wrote fixed
strings asserting that no secrets were present, that changes were in scope, and
that nothing performance-sensitive changed -- none of which it checked. Those
claims fed the human approval gate, which made the package worse than no
evidence at all, because it looked like evidence.

Sections with nothing verified behind them are omitted entirely rather than
hedged: a "not assessed" line still reads as a finding to a reviewer skimming
for red flags.
"""

import json
import subprocess
import sys
from pathlib import Path
from typing import List, Optional, Tuple

GIT_TIMEOUT_S = 30

# Candidate base refs, in order, for locating the branch point.
BASE_REF_CANDIDATES = ("origin/main", "main")

# The task's own artifacts live under .ai/tasks/ and are git-tracked. Excluding
# them keeps a task's review from being dominated by its own bookkeeping.
DIFF_PATHSPEC = (".", ":(exclude).ai/tasks")


def git(args: List[str], root: Path) -> Tuple[int, str]:
    result = subprocess.run(
        ["git"] + args,
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=GIT_TIMEOUT_S,
    )
    return result.returncode, result.stdout


def resolve_base(root: Path) -> Tuple[Optional[str], Optional[str]]:
    """Find the commit where this branch diverged from its base.

    Returns ``(ref_name, commit_sha)``, or ``(None, None)`` if no candidate ref
    resolves -- in which case no diff is reported rather than a wrong one.
    """
    for ref in BASE_REF_CANDIDATES:
        code, out = git(["merge-base", ref, "HEAD"], root)

        if code == 0 and out.strip():
            return ref, out.strip()

    return None, None


def section(title: str, body: str) -> str:
    return f"## {title}\n\n{body.rstrip()}\n\n"


def write_validation_record(task_dir: Path) -> Optional[Path]:
    """Write validation.md from recorded evidence, if any exists.

    The README and the review summary both cite validation.md; nothing ever
    wrote it, so the evidence trail ended in a dead link.
    """
    results_file = task_dir / "test-results.json"

    if not results_file.is_file():
        return None

    try:
        results = json.loads(results_file.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None

    passed = results.get("passed")
    count = results.get("test_count")
    reason = results.get("failure_reason")

    lines = [
        "# Validation Record",
        "",
        f"- Command: `{results.get('command')}`",
        f"- Return code: {results.get('returncode')}",
        f"- Tests run: {count if count is not None else 'not reported'}",
        f"- Result: {'PASSED' if passed else 'FAILED'}",
        f"- Recorded at: {results.get('timestamp')}",
    ]

    if reason:
        lines.append(f"- Failure reason: {reason}")

    lines += ["", "Source of record: `test-results.json`.", ""]

    path = task_dir / "validation.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def validation_summary(task_dir: Path) -> Optional[str]:
    results_file = task_dir / "test-results.json"

    if not results_file.is_file():
        return None

    try:
        results = json.loads(results_file.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None

    passed = results.get("passed")
    count = results.get("test_count")
    reason = results.get("failure_reason")

    body = (
        f"- Command: `{results.get('command')}`\n"
        f"- Return code: {results.get('returncode')}\n"
        f"- Tests run: {count if count is not None else 'not reported'}\n"
        f"- Result: {'PASSED' if passed else 'FAILED'}"
    )

    if reason:
        body += f"\n- Failure reason: {reason}"

    return body


def findings_section(task_dir: Path) -> Optional[str]:
    """Render reviewer findings, if any reviewer actually ran.

    This is the section Phase 0 deleted, earned back. The difference is that
    every line here traces to a reviewer subagent that ran and produced
    schema-conforming output. A dimension that did not run is reported as not
    having run -- never as "no issues found", which is what made the original
    fixed strings dangerous.
    """
    path = task_dir / "review-findings.json"

    if not path.is_file():
        return None

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None

    dimensions = data.get("dimensions") or {}

    if not dimensions:
        return None

    lines = []

    for name in sorted(dimensions):
        outcome = dimensions[name] or {}

        if not outcome.get("ran"):
            lines.append(
                "- **%s**: did not run (%s)"
                % (name, outcome.get("error") or "no reason recorded")
            )
            continue

        findings = outcome.get("findings") or []
        checked = outcome.get("checked") or []
        scope = (" Examined: %s." % "; ".join(checked)) if checked else ""

        if not findings:
            lines.append(
                "- **%s**: ran, reported no findings.%s" % (name, scope)
            )
            continue

        lines.append("- **%s**: %d finding(s).%s" % (name, len(findings), scope))

        for finding in findings:
            location = finding.get("file") or ""

            if location and finding.get("line"):
                location += ":%s" % finding["line"]

            criteria = finding.get("acceptance_criteria") or []
            suffix = (" (%s)" % ", ".join(criteria)) if criteria else ""

            lines.append(
                "  - `%s` %s%s%s"
                % (
                    finding.get("severity", "?"),
                    finding.get("title", "untitled"),
                    (" — %s" % location) if location else "",
                    suffix,
                )
            )
            lines.append("    %s" % finding.get("detail", ""))

    return "\n".join(lines)


def acceptance_section(task_dir: Path) -> Optional[str]:
    """Render the per-criterion table from recorded validation evidence."""
    results_file = task_dir / "test-results.json"

    if not results_file.is_file():
        return None

    try:
        results = json.loads(results_file.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None

    criteria = results.get("acceptance_criteria") or {}
    entries = criteria.get("results") or []

    # No criteria were evaluated: omit the section. A "not evaluated" line
    # still reads as a finding to someone skimming for red flags.
    if not entries:
        return None

    lines = ["| Criterion | Result | Verify |", "|---|---|---|"]

    for entry in entries:
        verdict = {True: "PASS", False: "FAIL", None: "UNVERIFIED"}[
            entry.get("passed")
        ]
        lines.append(
            "| %s %s | %s | `%s` |"
            % (
                entry.get("id", "?"),
                entry.get("statement", ""),
                verdict,
                entry.get("verify", ""),
            )
        )

    return "\n".join(lines)


def scope_section(task_dir: Path) -> Optional[str]:
    results_file = task_dir / "test-results.json"

    if not results_file.is_file():
        return None

    try:
        results = json.loads(results_file.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None

    scope = results.get("diff_scope") or {}

    if scope.get("skipped_reason"):
        return None

    declared = scope.get("declared") or []
    violations = scope.get("violations") or []

    if not declared and not violations:
        return None

    body = "- Declared in the plan: %s" % (", ".join(declared) or "none")

    if violations:
        body += "\n- **Undeclared changes:** %s" % ", ".join(violations)
    else:
        body += "\n- No undeclared files changed."

    return body


def main() -> int:
    if len(sys.argv) != 2:
        print("Usage: review-package.py TASK-XXX")
        return 2

    task_id = sys.argv[1]
    root = Path.cwd()
    task_dir = root / ".ai" / "tasks" / task_id
    state_file = task_dir / "state.json"

    if not state_file.is_file():
        print(f"ERROR: Missing {state_file}")
        return 1

    with state_file.open(encoding="utf-8") as f:
        state = json.load(f)

    if state.get("status") != "VALIDATED":
        print(
            f"ERROR: {task_id} is {state.get('status')}; "
            "review package requires VALIDATED."
        )
        return 1

    base_ref, base_sha = resolve_base(root)

    diff_text = ""
    changed_files = ""

    if base_sha:
        _, diff_text = git(
            ["diff", f"{base_sha}...HEAD", "--"] + list(DIFF_PATHSPEC), root
        )
        _, changed_files = git(
            ["diff", "--name-status", f"{base_sha}...HEAD", "--"]
            + list(DIFF_PATHSPEC),
            root,
        )

    validation_file = write_validation_record(task_dir)

    summary = f"# Review Summary\n\n## Task\n\n{task_id}\n\n"
    summary += section("State", str(state.get("status")))
    summary += section("Branch", str(state.get("branch")))
    summary += section("Plan Version", str(state.get("plan_version")))

    plan_file = state.get("plan_file")

    if plan_file:
        summary += section("Approved Plan", f"`{plan_file}`")

    if base_sha:
        summary += section(
            "Diff Base", f"`{base_ref}` at `{base_sha}`\n\nExcludes `.ai/tasks/`."
        )

        if changed_files.strip():
            summary += section(
                "Files Changed", "```text\n" + changed_files.rstrip() + "\n```"
            )
        else:
            summary += section(
                "Files Changed", "No files changed against the diff base."
            )

        if diff_text.strip():
            summary += section(
                "Git Diff", "```diff\n" + diff_text.rstrip() + "\n```"
            )
        else:
            summary += section("Git Diff", "Empty against the diff base.")

    validation = validation_summary(task_dir)

    if validation:
        summary += section("Validation", validation)

    acceptance = acceptance_section(task_dir)

    if acceptance:
        summary += section("Acceptance Criteria", acceptance)

    scope = scope_section(task_dir)

    if scope:
        summary += section("Diff Scope", scope)

    findings = findings_section(task_dir)

    if findings:
        summary += section("Reviewer Findings", findings)

    # Only list evidence files that exist on disk right now.
    candidates = [
        ("Implementation record", task_dir / "implementation.md"),
        ("Validation record", validation_file),
        ("Machine-readable tests", task_dir / "test-results.json"),
        ("Event log", task_dir / "events.jsonl"),
        ("Reviewer findings", task_dir / "review-findings.json"),
    ]
    evidence = [
        f"- {label}: {path.name}"
        for label, path in candidates
        if path is not None and path.is_file()
    ]

    if evidence:
        summary += section("Evidence", "\n".join(evidence))

    (task_dir / "review-summary.md").write_text(
        summary.rstrip() + "\n", encoding="utf-8"
    )

    print(f"Review package generated for {task_id}.")
    print(f"Location: {task_dir}")

    if not base_sha:
        print(
            "WARNING: no diff base resolved "
            f"(tried {', '.join(BASE_REF_CANDIDATES)}); no diff reported."
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
