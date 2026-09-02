"""Repository hygiene: .gitignore says what we mean.

A .gitignore is easy to write once and then drift. These tests ask git itself
what it would do, via `git check-ignore`, rather than eyeballing patterns -- the
same reason the rest of this repo tests its guards instead of trusting them.

The dangerous direction is over-ignoring. Task artifacts under .ai/tasks/ are
the audit trail and are tracked on purpose; a pattern that swallowed them would
silently stop recording evidence, which is the failure mode this whole workflow
exists to prevent.
"""

import shutil
import subprocess
import unittest
from pathlib import Path

from _support import REPO_ROOT

HAVE_GIT = shutil.which("git") is not None
GITIGNORE = REPO_ROOT / ".gitignore"


def is_ignored(path):
    """Ask git, rather than re-implementing its matching rules."""
    result = subprocess.run(
        ["git", "check-ignore", "-q", "--no-index", path],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


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
        ):
            path = ".ai/tasks/TASK-001/" + name
            self.assertFalse(is_ignored(path), path)

    def test_committed_task_004_artifacts_are_not_ignored(self):
        """Regression: the existing audit trail must remain visible."""
        for path in REPO_ROOT.glob(".ai/tasks/TASK-004/*"):
            relative = path.relative_to(REPO_ROOT).as_posix()
            self.assertFalse(is_ignored(relative), relative)

    def test_workflow_infrastructure_is_not_ignored(self):
        for path in (
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
        ):
            self.assertFalse(is_ignored(path), path)

    def test_tests_and_pins_are_not_ignored(self):
        for path in ("test_hardening.py", "_support.py", ".python-version"):
            self.assertFalse(is_ignored(path), path)

    def test_no_currently_tracked_file_is_ignored(self):
        """A tracked-but-ignored file is a contradiction git will not resolve."""
        tracked = subprocess.run(
            ["git", "ls-files"],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            check=True,
        ).stdout.split()

        offenders = [path for path in tracked if is_ignored(path)]

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


if __name__ == "__main__":
    unittest.main()
