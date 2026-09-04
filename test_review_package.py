"""Fixes B5, B6 / audit 2.6, 2.7, 2.8 - the review package reports observations.

The old script wrote three fixed-string files asserting that no secrets were
present, that changes were in scope, and that nothing performance-sensitive
changed. Nothing checked any of it, and the diff pathspec named files that do
not exist in this repo -- with no base ref, so it compared worktree to index and
came out empty. That document fed the human approval gate.
"""

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from _support import REPO_ROOT, load_review_package, quiet

rp = load_review_package()

# Strings the old script asserted without checking. None may reappear.
FABRICATED_CLAIMS = (
    "No secrets detected by this workflow",
    "No authentication or authorization code changed",
    "No dependency changes introduced",
    "Changes are limited to the approved task scope",
    "Existing module-level function conventions are preserved",
    "Existing unittest conventions are preserved",
    "No performance-sensitive infrastructure or algorithms changed",
    "No performance benchmark was required for this task",
    "Ready for developer review",
)

# Hedges that still read as findings.
HEDGES = ("not assessed", "not evaluated", "no issues found", "none detected")

HAVE_GIT = shutil.which("git") is not None


def git(args, cwd):
    return subprocess.run(
        ["git"] + args,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=True,
    )


@unittest.skipUnless(HAVE_GIT, "git not available")
class ReviewPackageTests(unittest.TestCase):
    task_id = "TASK-999"

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="wf-rp-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

        git(["init", "-q", "-b", "main"], self.tmp)
        git(["config", "user.email", "t@example.com"], self.tmp)
        git(["config", "user.name", "Test"], self.tmp)

        (self.tmp / "base.txt").write_text("base\n")
        git(["add", "-A"], self.tmp)
        git(["commit", "-qm", "base"], self.tmp)

        git(["checkout", "-q", "-b", "feature/TASK-999"], self.tmp)

        # A real change on the branch, plus task artifacts that must be
        # excluded from the diff.
        (self.tmp / "feature.py").write_text("def widget():\n    return 1\n")

        self.task_dir = self.tmp / ".ai" / "tasks" / self.task_id
        self.task_dir.mkdir(parents=True)
        (self.task_dir / "state.json").write_text(
            json.dumps(
                {
                    "task_id": self.task_id,
                    "status": "VALIDATED",
                    "created_at": "2026-09-01T10:00:00+05:30",
                    "updated_at": "2026-09-01T10:00:00+05:30",
                    "plan_version": 1,
                    "plan_file": ".ai/tasks/TASK-999/plan.json",
                    "branch": "feature/TASK-999",
                    "worktree": None,
                    "failure_reason": None,
                }
            )
        )
        (self.task_dir / "implementation.md").write_text("# Notes\n")
        (self.task_dir / "test-results.json").write_text(
            json.dumps(
                {
                    "task_id": self.task_id,
                    "command": "python3 -m unittest -v",
                    "returncode": 0,
                    "passed": True,
                    "test_count": 7,
                    "failure_reason": None,
                    "timestamp": "2026-09-01T11:05:29+05:30",
                    "stdout": "",
                    "stderr": "Ran 7 tests in 0.01s\n\nOK\n",
                }
            )
        )

        git(["add", "-A"], self.tmp)
        git(["commit", "-qm", "work"], self.tmp)

        import os

        self._prev = Path.cwd()
        os.chdir(self.tmp)
        self.addCleanup(os.chdir, str(self._prev))

        self._prev_argv = sys.argv
        sys.argv = ["review-package.py", self.task_id]
        self.addCleanup(self._restore_argv)

    def _restore_argv(self):
        sys.argv = self._prev_argv

    def run_package(self):
        with quiet() as out:
            code = rp.main()
        return code, out.getvalue()

    def summary(self):
        return (self.task_dir / "review-summary.md").read_text()

    def test_generates_successfully(self):
        code, _ = self.run_package()
        self.assertEqual(code, 0)

    def test_no_fabricated_claims_anywhere_in_output(self):
        self.run_package()

        produced = "\n".join(p.read_text() for p in self.task_dir.glob("*.md"))

        for claim in FABRICATED_CLAIMS:
            self.assertNotIn(
                claim, produced, "fabricated claim present: " + claim
            )

    def test_no_hedged_findings(self):
        self.run_package()
        lowered = self.summary().lower()

        for hedge in HEDGES:
            self.assertNotIn(hedge, lowered, "hedge present: " + hedge)

    def test_unverified_review_files_are_not_created(self):
        self.run_package()

        for name in (
            "security-review.md",
            "quality-review.md",
            "performance-review.md",
        ):
            self.assertFalse(
                (self.task_dir / name).exists(),
                name + " was generated with nothing behind it",
            )

    def test_reports_the_real_diff(self):
        self.run_package()
        summary = self.summary()

        self.assertIn("feature.py", summary)
        self.assertIn("def widget", summary)

    def test_excludes_task_artifacts_from_the_diff(self):
        self.run_package()
        before_evidence = self.summary().split("## Evidence")[0]

        self.assertNotIn(".ai/tasks/TASK-999/state.json", before_evidence)
        self.assertNotIn("test-results.json", before_evidence)

    def test_reports_the_diff_base(self):
        self.run_package()
        self.assertIn("## Diff Base", self.summary())

    def test_code_fences_are_balanced(self):
        """audit 2.7 - the old template opened a fence and never closed it."""
        self.run_package()
        fences = self.summary().count("`" * 3)

        self.assertEqual(fences % 2, 0, "unbalanced code fence in summary")

    def test_writes_validation_record(self):
        """audit 2.8 - validation.md was cited everywhere, written nowhere."""
        self.run_package()
        path = self.task_dir / "validation.md"

        self.assertTrue(path.is_file())
        body = path.read_text()
        self.assertIn("Tests run: 7", body)
        self.assertIn("PASSED", body)

    def test_validation_section_reflects_recorded_evidence(self):
        self.run_package()
        summary = self.summary()

        self.assertIn("## Validation", summary)
        self.assertIn("Tests run: 7", summary)

    def test_evidence_lists_only_existing_files(self):
        self.run_package()
        evidence = self.summary().split("## Evidence")[-1]

        self.assertIn("implementation.md", evidence)
        self.assertIn("validation.md", evidence)
        self.assertNotIn("security-review.md", evidence)
        self.assertNotIn("performance-review.md", evidence)

    def test_validation_section_omitted_when_no_evidence(self):
        (self.task_dir / "test-results.json").unlink()
        self.run_package()

        self.assertNotIn("## Validation", self.summary())
        self.assertFalse((self.task_dir / "validation.md").exists())

    def test_refuses_when_task_is_not_validated(self):
        state_path = self.task_dir / "state.json"
        state = json.loads(state_path.read_text())
        state["status"] = "IMPLEMENTING"
        state_path.write_text(json.dumps(state))

        code, _ = self.run_package()
        self.assertEqual(code, 1)


class DiffBaseResolutionTests(unittest.TestCase):
    def test_returns_none_when_no_candidate_ref_resolves(self):
        tmp = Path(tempfile.mkdtemp(prefix="wf-nogit-"))
        self.addCleanup(shutil.rmtree, tmp, True)

        ref, sha = rp.resolve_base(tmp)

        self.assertIsNone(ref)
        self.assertIsNone(sha)

    def test_excludes_task_dir_via_pathspec(self):
        self.assertIn(":(exclude).ai/tasks", rp.DIFF_PATHSPEC)


class OldScriptRegressionTests(unittest.TestCase):
    def test_hardcoded_app_py_pathspec_is_gone(self):
        source = (
            REPO_ROOT / ".ai" / "scripts" / "review-package.py"
        ).read_text()

        self.assertNotIn("app.py", source)
        self.assertNotIn("test_app.py", source)



@unittest.skipUnless(HAVE_GIT, "git not available")
class ReviewerFindingsSectionTests(ReviewPackageTests):
    """Phase 2 earns back the deleted review sections -- without the lies.

    The original security/quality/performance files asserted absences nobody
    verified. These sections are allowed to exist only because a real read-only
    agent produced them, and a dimension that did not run says so.
    """

    def _findings(self, dimensions):
        (self.task_dir / "review-findings.json").write_text(
            json.dumps({"task_id": self.task_id, "dimensions": dimensions})
        )

    def test_reports_a_real_finding(self):
        self._findings(
            {
                "security": {
                    "ran": True,
                    "checked": ["orchestrator.py"],
                    "findings": [
                        {
                            "severity": "high",
                            "title": "shell injection",
                            "detail": "verify runs through the shell",
                            "file": "orchestrator.py",
                            "line": 12,
                            "acceptance_criteria": ["AC-1"],
                        }
                    ],
                }
            }
        )
        self.run_package()
        summary = self.summary()

        self.assertIn("## Reviewer Findings", summary)
        self.assertIn("shell injection", summary)
        self.assertIn("orchestrator.py:12", summary)
        self.assertIn("AC-1", summary)

    def test_distinguishes_no_findings_from_did_not_run(self):
        """The distinction the fabricated files erased."""
        self._findings(
            {
                "correctness": {"ran": True, "findings": []},
                "security": {"ran": False, "error": "agent exited 3"},
            }
        )
        self.run_package()
        summary = self.summary()

        self.assertIn("ran, reported no findings", summary)
        self.assertIn("did not run", summary)
        self.assertIn("agent exited 3", summary)

    def test_a_failed_dimension_is_never_reported_as_clean(self):
        self._findings({"security": {"ran": False, "error": "timed out"}})
        self.run_package()
        section = self.summary().split("## Reviewer Findings")[-1]

        self.assertNotIn("no findings", section)
        self.assertNotIn("no issues", section.lower())

    def test_section_absent_when_no_reviewer_ran_at_all(self):
        self.run_package()

        self.assertNotIn("## Reviewer Findings", self.summary())

    def test_fabricated_claims_still_absent_with_findings_present(self):
        self._findings({"security": {"ran": True, "findings": []}})
        self.run_package()
        produced = "\n".join(p.read_text() for p in self.task_dir.glob("*.md"))

        for claim in FABRICATED_CLAIMS:
            self.assertNotIn(claim, produced, claim)

    def test_no_hedges_with_findings_present(self):
        self._findings({"security": {"ran": False, "error": "timed out"}})
        self.run_package()
        lowered = self.summary().lower()

        for hedge in HEDGES:
            self.assertNotIn(hedge, lowered, hedge)


@unittest.skipUnless(HAVE_GIT, "git not available")
class AcceptanceAndScopeSectionTests(ReviewPackageTests):
    def _evidence(self, **extra):
        data = {
            "task_id": self.task_id,
            "command": "python -m unittest -v",
            "returncode": 0,
            "passed": True,
            "test_count": 7,
            "failure_reason": None,
            "timestamp": "2026-09-01T11:05:29+05:30",
            "stdout": "",
            "stderr": "Ran 7 tests in 0.01s\n\nOK\n",
        }
        data.update(extra)
        (self.task_dir / "test-results.json").write_text(json.dumps(data))

    def test_renders_the_per_criterion_table(self):
        self._evidence(
            acceptance_criteria={
                "total": 3,
                "passed": 1,
                "failed": 1,
                "unverified": 1,
                "results": [
                    {"id": "AC-1", "statement": "a", "verify": "exit 0",
                     "passed": True},
                    {"id": "AC-2", "statement": "b", "verify": "exit 1",
                     "passed": False},
                    {"id": "AC-3", "statement": "c", "verify": "judge",
                     "passed": None},
                ],
            }
        )
        self.run_package()
        summary = self.summary()

        self.assertIn("## Acceptance Criteria", summary)
        self.assertIn("PASS", summary)
        self.assertIn("FAIL", summary)
        self.assertIn("UNVERIFIED", summary)

    def test_criteria_section_omitted_when_none_were_evaluated(self):
        """Omit, do not hedge -- a 'not evaluated' line reads as a finding."""
        self._evidence(
            acceptance_criteria={"total": 0, "results": [],
                                 "skipped_reason": "legacy plan"}
        )
        self.run_package()

        self.assertNotIn("## Acceptance Criteria", self.summary())
        self.assertNotIn("not evaluated", self.summary().lower())

    def test_renders_scope_violations(self):
        self._evidence(
            diff_scope={
                "declared": ["widget.py"],
                "changed": ["widget.py", "sneaky.py"],
                "violations": ["sneaky.py"],
            }
        )
        self.run_package()
        summary = self.summary()

        self.assertIn("## Diff Scope", summary)
        self.assertIn("Undeclared changes", summary)
        self.assertIn("sneaky.py", summary)

    def test_scope_section_omitted_when_skipped(self):
        self._evidence(diff_scope={"skipped_reason": "no base"})
        self.run_package()

        self.assertNotIn("## Diff Scope", self.summary())

    def test_reports_a_clean_scope_positively(self):
        self._evidence(
            diff_scope={"declared": ["widget.py"], "changed": ["widget.py"],
                        "violations": []}
        )
        self.run_package()

        self.assertIn("No undeclared files changed", self.summary())


if __name__ == "__main__":
    unittest.main()
