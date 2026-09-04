"""Repository hygiene: .gitignore says what we mean.

A .gitignore is easy to write once and then drift. These tests ask git itself
what it would do, via `git check-ignore`, rather than eyeballing patterns -- the
same reason the rest of this repo tests its guards instead of trusting them.

The dangerous direction is over-ignoring. Task artifacts under .ai/tasks/ are
the audit trail and are tracked on purpose; a pattern that swallowed them would
silently stop recording evidence, which is the failure mode this whole workflow
exists to prevent.
"""

import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from _support import REPO_ROOT

HAVE_GIT = shutil.which("git") is not None
GITIGNORE = REPO_ROOT / ".gitignore"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"

KEY_RE = re.compile(r"^(?P<key>[A-Za-z_][A-Za-z0-9_.\-]*):(?:\s+(?P<rest>.*))?$")
BLOCK_SCALAR = ("|", ">", "|-", ">-", "|+", ">+")


class WorkflowError(ValueError):
    """The workflow could not be read as the structure this check needs.

    Its own exception type so a test can demand a *parse* failure rather than
    accept any ValueError raised somewhere along the way.
    """


def _strip_comment(line):
    """Drop a trailing comment, honouring quotes.

    Narrow on purpose: `#` inside single or double quotes is content, anywhere
    else it starts a comment. That is the whole of the comment syntax this
    workflow uses.
    """
    quote = None
    out = []

    for index, char in enumerate(line):
        if quote:
            if char == quote:
                quote = None
        elif char in "\"'":
            quote = char
        elif char == "#" and (index == 0 or line[index - 1] in " \t"):
            break

        out.append(char)

    if quote:
        raise WorkflowError("unterminated quote: %r" % line)

    return "".join(out).rstrip()


def _parse_scalar(text):
    """A scalar or a flow sequence. Anything else is unrecognised."""
    text = text.strip()

    if text.startswith("["):
        if not text.endswith("]"):
            raise WorkflowError("unterminated flow sequence: %r" % text)

        inner = text[1:-1].strip()

        if not inner:
            return []

        return [_parse_scalar(item) for item in inner.split(",")]

    if text.startswith("{"):
        # Flow mappings are not used by this workflow, and guessing at one is
        # how a reader starts reporting on structure it never understood.
        raise WorkflowError("flow mappings are not supported: %r" % text)

    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        return text[1:-1]

    return text


class _Line:
    __slots__ = ("indent", "text", "number")

    def __init__(self, indent, text, number):
        self.indent = indent
        self.text = text
        self.number = number


def _significant_lines(text):
    lines = []

    for number, raw in enumerate(text.splitlines(), 1):
        if raw.lstrip().startswith("---") or raw.lstrip().startswith("..."):
            continue

        body = _strip_comment(raw)

        if not body.strip():
            continue

        stripped = body.lstrip(" ")
        indent = len(body) - len(stripped)

        if "\t" in body[:indent]:
            raise WorkflowError("tab indentation at line %d" % number)

        lines.append(_Line(indent, stripped, number))

    return lines


def _skip_deeper(lines, pos, indent):
    while pos < len(lines) and lines[pos].indent > indent:
        pos += 1

    return pos


def _parse_block(lines, pos, indent):
    if lines[pos].text == "-" or lines[pos].text.startswith("- "):
        return _parse_sequence(lines, pos, indent)

    return _parse_mapping(lines, pos, indent)


def _parse_sequence(lines, pos, indent):
    items = []

    while pos < len(lines) and lines[pos].indent == indent:
        line = lines[pos]

        if line.text != "-" and not line.text.startswith("- "):
            raise WorkflowError(
                "expected a sequence item at line %d: %r"
                % (line.number, line.text)
            )

        rest = line.text[2:] if line.text.startswith("- ") else ""
        child = indent + 2

        if KEY_RE.match(rest):
            # `- name: Checkout` opens a mapping whose first key sits in the
            # item's own column, with any further keys indented to match.
            synthetic = [_Line(child, rest, line.number)]
            pos += 1

            while pos < len(lines) and lines[pos].indent >= child:
                synthetic.append(lines[pos])
                pos += 1

            value, _ = _parse_mapping(synthetic, 0, child)
            items.append(value)
            continue

        pos += 1

        if rest.strip():
            items.append(_parse_scalar(rest))
        elif pos < len(lines) and lines[pos].indent > indent:
            value, pos = _parse_block(lines, pos, lines[pos].indent)
            items.append(value)
        else:
            items.append(None)

    return items, pos


def _parse_mapping(lines, pos, indent):
    mapping = {}

    while pos < len(lines):
        line = lines[pos]

        if line.indent < indent:
            break

        if line.indent > indent:
            raise WorkflowError(
                "unexpected indentation at line %d: %r"
                % (line.number, line.text)
            )

        match = KEY_RE.match(line.text)

        if not match:
            raise WorkflowError(
                "unrecognised line %d: %r" % (line.number, line.text)
            )

        key = match.group("key")
        rest = (match.group("rest") or "").strip()
        pos += 1

        if rest in BLOCK_SCALAR:
            end = _skip_deeper(lines, pos, indent)
            mapping[key] = "\n".join(
                item.text for item in lines[pos:end]
            )
            pos = end
        elif rest:
            mapping[key] = _parse_scalar(rest)
        elif pos < len(lines) and lines[pos].indent > indent:
            mapping[key], pos = _parse_block(lines, pos, lines[pos].indent)
        else:
            mapping[key] = None

    return mapping, pos


def parse_workflow(text):
    """The workflow as nested dicts, lists and strings.

    A deliberately small hand-written reader: the repository is standard
    library only, and the alternative -- matching `"3.9"` as a substring --
    passes on a comment and fails on a cosmetic reformat. It supports the
    constructs this workflow actually uses and **raises** on anything else, so
    "we could not understand the file" can never be reported as "the matrix is
    fine".
    """
    lines = _significant_lines(text)

    if not lines:
        raise WorkflowError("workflow is empty")

    if lines[0].indent != 0:
        raise WorkflowError("workflow does not start at column 0")

    document, pos = _parse_mapping(lines, 0, 0)

    if pos != len(lines):
        raise WorkflowError(
            "trailing content at line %d" % lines[pos].number
        )

    if not document:
        raise WorkflowError("workflow has no top-level keys")

    return document


def matrix_python_versions(path):
    """Every Python version the workflow's job matrices declare.

    Raises WorkflowError when the file is absent, unreadable, unparsable or
    structurally not what this check reads -- never an empty result, which a
    caller could mistake for "no versions are configured, so nothing to see".
    """
    path = Path(path)

    if not path.is_file():
        raise WorkflowError("no workflow at %s" % path)

    try:
        document = parse_workflow(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError) as exc:
        raise WorkflowError("workflow is unreadable: %s" % exc)

    jobs = document.get("jobs")

    if not isinstance(jobs, dict) or not jobs:
        raise WorkflowError("workflow declares no jobs")

    versions = []

    for name, job in jobs.items():
        if not isinstance(job, dict):
            raise WorkflowError("job %s is not a mapping" % name)

        matrix = (job.get("strategy") or {})

        if not isinstance(matrix, dict):
            raise WorkflowError("job %s has an unreadable strategy" % name)

        matrix = matrix.get("matrix")

        if matrix is None:
            continue

        if not isinstance(matrix, dict):
            raise WorkflowError("job %s has an unreadable matrix" % name)

        declared = matrix.get("python-version")

        if declared is None:
            continue

        if not isinstance(declared, list) or not declared:
            raise WorkflowError(
                "job %s declares python-version as %r, which is not a "
                "non-empty sequence" % (name, declared)
            )

        for entry in declared:
            if not isinstance(entry, str) or not entry.strip():
                raise WorkflowError(
                    "job %s declares a non-string version: %r" % (name, entry)
                )

            versions.append(entry.strip())

    if not versions:
        raise WorkflowError("no job declares a python-version matrix")

    return versions


def is_ignored(path):
    """Ask git, rather than re-implementing its matching rules."""
    return bool(ignored_among([path]))


def ignored_among(paths):
    """Which of ``paths`` git would ignore, in one invocation.

    Same question as ``is_ignored`` and the same answer from the same tool,
    asked once for the whole set: ``--stdin`` prints back only the paths that
    match, so a per-path call per tracked file (one git process each) is not
    needed to check a repository's worth of them.

    Byte I/O with ``-z`` rather than text mode: ``text=True`` rewrites the
    separator to CRLF on Windows, and git then treats the CR as part of the
    path and quotes it, so nothing ever matches.
    """
    candidates = [str(path) for path in paths]

    if not candidates:
        return []

    result = subprocess.run(
        ["git", "check-ignore", "--no-index", "--stdin", "-z"],
        cwd=str(REPO_ROOT),
        input=("\0".join(candidates) + "\0").encode("utf-8"),
        capture_output=True,
    )

    # 0: something matched, 1: nothing matched. Anything else is git failing
    # to answer, which must not read as "nothing is ignored".
    if result.returncode not in (0, 1):
        raise AssertionError(
            "git check-ignore failed: %s"
            % (result.stderr.decode("utf-8", "replace").strip() or "no output")
        )

    matched = {
        entry
        for entry in result.stdout.decode("utf-8").split("\0")
        if entry
    }

    return [path for path in candidates if path in matched]


@unittest.skipUnless(HAVE_GIT, "git not available")
class IgnoredArtifactTests(unittest.TestCase):
    """Transient runtime state must never reach a commit."""

    def test_task_lock_is_ignored(self):
        """A committed lock would make every later run refuse to start."""
        self.assertTrue(is_ignored(".ai/tasks/TASK-001/.lock"))

    def test_atomic_write_temp_files_are_ignored(self):
        """Sibling temp files from save_state, left behind only by a crash."""
        self.assertTrue(is_ignored(".ai/tasks/TASK-001/state.json.abc123.tmp"))

    def test_worktrees_are_ignored(self):
        self.assertTrue(is_ignored(".worktrees/TASK-001/widget.py"))

    def test_pycache_is_ignored(self):
        self.assertTrue(is_ignored("__pycache__/orchestrator.cpython-312.pyc"))
        self.assertTrue(is_ignored(".ai/scripts/__pycache__/x.pyc"))

    def test_local_claude_settings_are_ignored(self):
        """settings.json is shared; settings.local.json is personal."""
        self.assertTrue(is_ignored(".claude/settings.local.json"))

    def test_virtualenvs_are_ignored(self):
        self.assertTrue(is_ignored(".venv/bin/python"))

    def test_tool_caches_are_ignored(self):
        for path in (
            ".mypy_cache/x",
            ".ruff_cache/x",
            ".pytest_cache/x",
            ".coverage",
        ):
            self.assertTrue(is_ignored(path), path)

    def test_os_and_editor_noise_is_ignored(self):
        for path in (".DS_Store", "Thumbs.db", ".idea/workspace.xml", "a.swp"):
            self.assertTrue(is_ignored(path), path)


@unittest.skipUnless(HAVE_GIT, "git not available")
class TrackedArtifactTests(unittest.TestCase):
    """The audit trail and the workflow itself must stay tracked."""

    def test_task_evidence_is_not_ignored(self):
        paths = [
            ".ai/tasks/TASK-001/" + name
            for name in (
                "state.json",
                "events.jsonl",
                "test-results.json",
                "validation.md",
                "plan.json",
                "implementation.md",
                "context.jsonl",
                "review-findings.json",
                "review-summary.md",
                "clarifications.json",
            )
        ]

        self.assertEqual(ignored_among(paths), [])

    def test_committed_task_004_artifacts_are_not_ignored(self):
        """Regression: the existing audit trail must remain visible."""
        paths = [
            path.relative_to(REPO_ROOT).as_posix()
            for path in REPO_ROOT.glob(".ai/tasks/TASK-004/*")
        ]

        self.assertEqual(ignored_among(paths), [])

    def test_workflow_infrastructure_is_not_ignored(self):
        paths = [
            ".ai/scripts/orchestrator.py",
            ".ai/scripts/review-package.py",
            ".ai/hooks/pre_tool_use.py",
            ".ai/schemas/plan.schema.json",
            ".ai/validation.json",
            ".ai/constitution.md",
            ".ai/adr/0001-plan-is-json.md",
            ".claude/settings.json",
            ".claude/agents/implementer.md",
            ".github/workflows/ci.yml",
        ]

        self.assertEqual(ignored_among(paths), [])

    def test_tests_and_pins_are_not_ignored(self):
        paths = ["test_hardening.py", "_support.py", ".python-version"]

        self.assertEqual(ignored_among(paths), [])

    def test_no_currently_tracked_file_is_ignored(self):
        """A tracked-but-ignored file is a contradiction git will not resolve."""
        tracked = subprocess.run(
            ["git", "ls-files"],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            check=True,
        ).stdout.split()

        offenders = ignored_among(tracked)

        self.assertEqual(offenders, [], "tracked files matched by .gitignore")


class GitignoreContentTests(unittest.TestCase):
    def test_gitignore_exists(self):
        self.assertTrue(GITIGNORE.is_file())

    def test_does_not_ignore_the_task_directory_wholesale(self):
        """The one mistake that would quietly delete the audit trail."""
        lines = [
            line.strip()
            for line in GITIGNORE.read_text().splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]

        for pattern in (".ai/", ".ai/tasks/", ".ai/tasks/*", "*.json"):
            self.assertNotIn(pattern, lines)

    def test_explains_why_task_artifacts_stay_tracked(self):
        """The next person to edit this file needs to know before they widen it."""
        # Comment prose wraps, so compare on collapsed whitespace.
        text = " ".join(GITIGNORE.read_text().split())

        self.assertIn("audit trail", text)
        self.assertIn(".ai/tasks/", text)


class CiMatrixTests(unittest.TestCase):
    """The CI matrix still runs both supported interpreters.

    A deliberate regression guard: nothing in this task touches the workflow,
    and that is the point -- multi-version coverage is CI's job, because an
    acceptance command may only use `{python}`.
    """

    def test_ci_matrix_includes_python_39_and_312(self):
        versions = matrix_python_versions(WORKFLOW)

        self.assertIn("3.9", versions)
        self.assertIn("3.12", versions)

    def test_a_cosmetic_reformat_does_not_fail_the_check(self):
        """Structure, not spelling: the same matrix written as a block list."""
        reformatted = """
name: CI
jobs:
  test:
    runs-on: ubuntu-latest
    strategy:
      matrix:
        python-version:
          - '3.9'
          - "3.12"
    steps:
      - name: Run tests
        run: |
          python -m unittest -v
"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ci.yml"
            path.write_text(reformatted, encoding="utf-8")

            self.assertEqual(
                matrix_python_versions(path), ["3.9", "3.12"]
            )

    def test_missing_or_unparsable_workflow_fails(self):
        """"Cannot parse" must never be a pass, and must never be a skip.

        Every input here is one this check has no answer for. Each has to raise
        rather than return an empty list a caller could read as "no versions
        are configured".
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)

            with self.assertRaises(WorkflowError):
                matrix_python_versions(root / "absent.yml")

            unparsable = {
                "empty": "",
                "comments only": "# nothing here\n",
                "not yaml at all": "\x00\x01 binary-ish garbage\n",
                "tab indented": "jobs:\n\ttest:\n\t\truns-on: x\n",
                "no jobs": "name: CI\non:\n  push:\n    branches:\n      - main\n",
                "job is a scalar": "jobs:\n  test: ubuntu\n",
                "matrix is a scalar": (
                    "jobs:\n  test:\n    strategy:\n      matrix: 3.9\n"
                ),
                "version is not a sequence": (
                    "jobs:\n  test:\n    strategy:\n      matrix:\n"
                    "        python-version: 3.9\n"
                ),
                "no matrix at all": (
                    "jobs:\n  test:\n    runs-on: ubuntu-latest\n"
                ),
                "unterminated flow sequence": (
                    "jobs:\n  test:\n    strategy:\n      matrix:\n"
                    '        python-version: ["3.9", "3.12"\n'
                ),
            }

            for label, content in unparsable.items():
                path = root / "ci.yml"
                path.write_text(content, encoding="utf-8")

                with self.subTest(label):
                    with self.assertRaises(WorkflowError):
                        matrix_python_versions(path)

    def test_the_real_workflow_still_runs_the_suite(self):
        """The matrix is only evidence if the matrixed job runs the tests."""
        document = parse_workflow(WORKFLOW.read_text(encoding="utf-8"))
        steps = document["jobs"]["test"]["steps"]
        commands = [
            step.get("run", "") for step in steps if isinstance(step, dict)
        ]

        self.assertTrue(
            any("unittest" in command for command in commands), commands
        )


if __name__ == "__main__":
    unittest.main()
