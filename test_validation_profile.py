"""Phase 1 - validation profile, executable acceptance criteria, diff scope.

Three gaps, all the same shape: the plan declared something and nothing checked
it.

- Validation was one command whose exit code was the entire verdict.
- ``acceptance_criteria`` was well-formed and never executed, so the contract
  the developer approved and the evidence they were shown were unrelated
  documents.
- ``files_to_modify`` / ``files_to_create`` were never compared to the diff, so
  blast-radius creep was invisible.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from _support import (
    REPO_ROOT,
    TaskDirCase,
    load_orchestrator,
    plan_json,
    quiet,
)

orch = load_orchestrator()

HAVE_GIT = shutil.which("git") is not None
PASSING = "\nRan 4 tests in 0.01s\n\nOK\n"
EMPTY = "\nRan 0 tests in 0.000s\n\nOK\n"


class RepoProfileTests(unittest.TestCase):
    def test_repo_profile_is_valid_json_with_checks(self):
        profile = json.loads((REPO_ROOT / ".ai" / "validation.json").read_text())

        self.assertTrue(profile["checks"])
        for check in profile["checks"]:
            self.assertIn("name", check)
            self.assertIn("command", check)

    def test_repo_profile_has_a_test_count_check(self):
        """Something in the profile must enforce the empty-suite rule."""
        profile = json.loads((REPO_ROOT / ".ai" / "validation.json").read_text())

        self.assertTrue(
            any(c.get("expect_test_count") for c in profile["checks"])
        )


class LoadProfileTests(TaskDirCase):
    def test_falls_back_to_a_tests_only_default(self):
        profile = orch.load_validation_profile()

        self.assertEqual(len(profile["checks"]), 1)
        self.assertTrue(profile["checks"][0]["expect_test_count"])

    def test_reads_a_profile_from_disk(self):
        (self.tmp / ".ai" / "validation.json").write_text(
            json.dumps(
                {"checks": [{"name": "a", "command": "true"},
                            {"name": "b", "command": "true"}]}
            )
        )

        self.assertEqual(len(orch.load_validation_profile()["checks"]), 2)

    def test_rejects_invalid_json(self):
        (self.tmp / ".ai" / "validation.json").write_text("{nope")

        with self.assertRaises(RuntimeError) as ctx:
            orch.load_validation_profile()

        self.assertIn("not valid JSON", str(ctx.exception))

    def test_rejects_an_empty_checks_list(self):
        (self.tmp / ".ai" / "validation.json").write_text('{"checks": []}')

        with self.assertRaises(RuntimeError):
            orch.load_validation_profile()

    def test_rejects_a_check_without_a_command(self):
        (self.tmp / ".ai" / "validation.json").write_text(
            '{"checks": [{"name": "x"}]}'
        )

        with self.assertRaises(RuntimeError) as ctx:
            orch.load_validation_profile()

        self.assertIn("command", str(ctx.exception))


class RunCheckTests(unittest.TestCase):
    def test_python_placeholder_expands_to_the_running_interpreter(self):
        result = orch.run_validation_check(
            {"name": "v", "command": "{python} -c \"print('hi')\""}
        )

        self.assertTrue(result["passed"])
        self.assertIn(sys.executable, result["command"])
        self.assertIn("hi", result["stdout"])

    def test_nonzero_exit_fails(self):
        result = orch.run_validation_check(
            {"name": "v", "command": "{python} -c \"raise SystemExit(3)\""}
        )

        self.assertFalse(result["passed"])
        self.assertEqual(result["returncode"], 3)
        self.assertIn("3", result["failure_reason"])

    def test_missing_executable_is_a_failure_not_a_crash(self):
        result = orch.run_validation_check(
            {"name": "v", "command": "definitely-not-a-real-binary-xyz"}
        )

        self.assertFalse(result["passed"])
        self.assertEqual(result["returncode"], 127)
        self.assertIn("not found", result["stderr"])

    def test_expect_test_count_applies_the_empty_suite_rule(self):
        """A check that should run tests but ran none has not passed."""
        script = (
            "import sys; sys.stderr.write('Ran 0 tests in 0.000s\\n\\nOK\\n')"
        )
        result = orch.run_validation_check(
            {
                "name": "tests",
                "command": '{python} -c "%s"' % script,
                "expect_test_count": True,
            }
        )

        self.assertFalse(result["passed"])
        self.assertEqual(result["test_count"], 0)
        self.assertIn("empty test suite", result["failure_reason"])

    def test_advisory_checks_are_marked_not_required(self):
        result = orch.run_validation_check(
            {"name": "v", "command": "{python} -c \"pass\"", "required": False}
        )

        self.assertFalse(result["required"])


class AcceptanceCriteriaTests(TaskDirCase):
    def _criteria(self, *entries):
        return plan_json(acceptance_criteria=list(entries))

    def test_runs_and_passes_a_satisfied_criterion(self):
        self.write_plan(
            "plan.json",
            self._criteria(
                {"id": "AC-1", "statement": "true holds", "verify": "exit 0"}
            ),
        )
        _, state = self.write_state()

        summary = orch.evaluate_acceptance_criteria(self.task_id, state)

        self.assertEqual(summary["total"], 1)
        self.assertEqual(summary["passed"], 1)
        self.assertEqual(summary["failed"], 0)
        self.assertTrue(summary["results"][0]["passed"])

    def test_reports_a_failing_criterion(self):
        self.write_plan(
            "plan.json",
            self._criteria(
                {"id": "AC-1", "statement": "impossible", "verify": "exit 7"}
            ),
        )
        _, state = self.write_state()

        summary = orch.evaluate_acceptance_criteria(self.task_id, state)

        self.assertEqual(summary["failed"], 1)
        self.assertFalse(summary["results"][0]["passed"])
        self.assertEqual(summary["results"][0]["returncode"], 7)

    def test_judge_is_unverified_not_passed(self):
        """An unverified criterion must never count as satisfied."""
        self.write_plan(
            "plan.json",
            self._criteria(
                {"id": "AC-1", "statement": "reads well", "verify": "judge"}
            ),
        )
        _, state = self.write_state()

        summary = orch.evaluate_acceptance_criteria(self.task_id, state)

        self.assertEqual(summary["unverified"], 1)
        self.assertEqual(summary["passed"], 0)
        self.assertIsNone(summary["results"][0]["passed"])

    def test_mixed_results_are_counted_separately(self):
        self.write_plan(
            "plan.json",
            self._criteria(
                {"id": "AC-1", "statement": "a", "verify": "exit 0"},
                {"id": "AC-2", "statement": "b", "verify": "exit 1"},
                {"id": "AC-3", "statement": "c", "verify": "judge"},
            ),
        )
        _, state = self.write_state()

        summary = orch.evaluate_acceptance_criteria(self.task_id, state)

        self.assertEqual(summary["total"], 3)
        self.assertEqual(summary["passed"], 1)
        self.assertEqual(summary["failed"], 1)
        self.assertEqual(summary["unverified"], 1)

    def test_legacy_yaml_plan_says_why_it_was_skipped(self):
        """Reporting zero criteria for an unparseable plan would be a lie."""
        self.write_plan("plan.yaml", "objective: legacy\n")
        _, state = self.write_state()
        state.pop("plan_file", None)

        summary = orch.evaluate_acceptance_criteria(self.task_id, state)

        self.assertIn("skipped_reason", summary)
        self.assertEqual(summary["total"], 0)

    def test_missing_plan_says_why(self):
        _, state = self.write_state()

        summary = orch.evaluate_acceptance_criteria(self.task_id, state)

        self.assertIn("skipped_reason", summary)

    def test_criterion_output_is_captured(self):
        self.write_plan(
            "plan.json",
            self._criteria(
                {
                    "id": "AC-1",
                    "statement": "prints",
                    "verify": "echo SENTINEL_OUT",
                }
            ),
        )
        _, state = self.write_state()

        summary = orch.evaluate_acceptance_criteria(self.task_id, state)

        self.assertIn("SENTINEL_OUT", summary["results"][0]["output"])


@unittest.skipUnless(HAVE_GIT, "git not available")
class DiffScopeTests(unittest.TestCase):
    task_id = "TASK-777"

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="wf-scope-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

        def git(*args):
            subprocess.run(
                ["git"] + list(args),
                cwd=str(self.tmp),
                capture_output=True,
                check=True,
            )

        git("init", "-q", "-b", "main")
        git("config", "user.email", "t@example.com")
        git("config", "user.name", "T")
        (self.tmp / "base.txt").write_text("base\n")
        git("add", "-A")
        git("commit", "-qm", "base")
        git("checkout", "-q", "-b", "feature/TASK-777")

        self.task_path = self.tmp / ".ai" / "tasks" / self.task_id
        self.task_path.mkdir(parents=True)

        self._prev = Path.cwd()
        os.chdir(self.tmp)
        self.addCleanup(os.chdir, str(self._prev))
        self._git = git

        # Scope is measured against the task baseline, so the tests need the
        # same starting point a real task gets at the implement gate.
        orch.capture_baseline(self.task_id, {"plan_version": 1})

    def _state(self):
        return {
            "task_id": self.task_id,
            "status": "VALIDATING",
            "plan_version": 1,
            "plan_file": ".ai/tasks/%s/plan.json" % self.task_id,
        }

    def _plan(self, modify=(), create=()):
        (self.task_path / "plan.json").write_text(
            plan_json(
                files_to_modify=[
                    {"path": p, "purpose": "x"} for p in modify
                ],
                files_to_create=[
                    {"path": p, "purpose": "x"} for p in create
                ],
            )
        )

    def _scope(self):
        """Scope as run_validation runs it: against the task delta."""
        delta = orch.compute_task_delta(orch.load_baseline(self.task_id))

        return orch.evaluate_diff_scope(self.task_id, self._state(), delta)

    def _commit(self, *paths):
        for path in paths:
            full = self.tmp / path
            full.parent.mkdir(parents=True, exist_ok=True)
            full.write_text("changed\n")

        self._git("add", "-A")
        self._git("commit", "-qm", "work")

    def test_declared_changes_are_in_scope(self):
        self._plan(create=("widget.py",))
        self._commit("widget.py")

        summary = self._scope()

        self.assertEqual(summary["violations"], [])
        self.assertIn("widget.py", summary["changed"])

    def test_undeclared_change_is_a_violation(self):
        self._plan(create=("widget.py",))
        self._commit("widget.py", "sneaky.py")

        summary = self._scope()

        self.assertIn("sneaky.py", summary["violations"])
        self.assertNotIn("widget.py", summary["violations"])

    def test_task_artifacts_are_allowlisted(self):
        """A task's own bookkeeping is never a scope violation."""
        self._plan(create=("widget.py",))
        self._commit("widget.py")

        summary = self._scope()

        self.assertEqual(
            [v for v in summary["violations"] if v.startswith(".ai/tasks/")], []
        )

    def test_modify_and_create_are_both_honoured(self):
        self._plan(modify=("base.txt",), create=("widget.py",))
        self._commit("base.txt", "widget.py")

        summary = self._scope()

        self.assertEqual(summary["violations"], [])

    def test_reports_the_declared_set(self):
        self._plan(modify=("a.py",), create=("b.py",))
        self._commit("a.py")

        summary = self._scope()

        self.assertEqual(summary["declared"], ["a.py", "b.py"])

    def test_missing_plan_is_reported_not_silently_clean(self):
        summary = self._scope()

        self.assertIn("skipped_reason", summary)


class ValidationIntegrationTests(TaskDirCase):
    """run_validation must block on criteria and scope, not just tests."""

    def _profile(self, stderr):
        script = "import sys; sys.stderr.write(%r)" % stderr
        (self.tmp / ".ai").mkdir(exist_ok=True)
        (self.tmp / ".ai" / "validation.json").write_text(
            json.dumps(
                {
                    "checks": [
                        {
                            "name": "tests",
                            "command": '{python} -c "%s"'
                            % script.replace('"', '\\"'),
                            "required": True,
                            "expect_test_count": True,
                        }
                    ]
                }
            )
        )

    def test_failing_acceptance_criterion_fails_validation(self):
        self.write_state(status="VALIDATING")
        self.write_plan(
            "plan.json",
            plan_json(
                acceptance_criteria=[
                    {"id": "AC-1", "statement": "nope", "verify": "exit 5"}
                ]
            ),
        )
        self._profile("Ran 4 tests in 0.01s\\n\\nOK\\n")
        self.write_baseline()
        (self.tmp / "widget.py").write_text("w = 1\n")

        with quiet() as out:
            code = orch.run_validation(self.task_id)

        self.assertNotEqual(code, 0)
        self.assertEqual(self.read_state()["status"], "FAILED")
        self.assertIn("acceptance", out.getvalue().lower())

        evidence = json.loads(
            (self.task_path / "test-results.json").read_text()
        )
        self.assertEqual(evidence["acceptance_criteria"]["failed"], 1)
        self.assertFalse(evidence["passed"])

    def test_passing_criteria_and_tests_validate(self):
        self.write_state(status="VALIDATING")
        self.write_plan(
            "plan.json",
            plan_json(
                acceptance_criteria=[
                    {"id": "AC-1", "statement": "ok", "verify": "exit 0"}
                ]
            ),
        )
        self._profile("Ran 4 tests in 0.01s\\n\\nOK\\n")
        # The baseline goes here: after the fixture has written the tree the
        # task starts from, before the task produces anything.
        self.write_baseline()
        (self.tmp / "widget.py").write_text("w = 1\n")

        with quiet():
            code = orch.run_validation(self.task_id)

        self.assertEqual(code, 0)
        self.assertEqual(self.read_state()["status"], "VALIDATED")

        evidence = json.loads(
            (self.task_path / "test-results.json").read_text()
        )
        self.assertEqual(evidence["acceptance_criteria"]["passed"], 1)
        self.assertEqual(evidence["test_count"], 4)

    def test_evidence_records_every_check_separately(self):
        self.write_state(status="VALIDATING")
        self.write_plan(
            "plan.json",
            plan_json(
                acceptance_criteria=[
                    {"id": "AC-1", "statement": "ok", "verify": "exit 0"}
                ]
            ),
        )
        (self.tmp / ".ai" / "validation.json").write_text(
            json.dumps(
                {
                    "checks": [
                        {
                            "name": "tests",
                            "command": '{python} -c "import sys; sys.stderr.write(\'Ran 2 tests in 0.0s\\n\\nOK\\n\')"',
                            "expect_test_count": True,
                        },
                        {"name": "advisory", "command": "exit 1",
                         "required": False},
                    ]
                }
            )
        )
        self.write_baseline()
        (self.tmp / "widget.py").write_text("w = 1\n")

        with quiet():
            orch.run_validation(self.task_id)

        evidence = json.loads(
            (self.task_path / "test-results.json").read_text()
        )
        names = [c["name"] for c in evidence["checks"]]

        self.assertEqual(names, ["tests", "advisory"])
        self.assertTrue(evidence["checks"][0]["passed"])
        self.assertFalse(evidence["checks"][1]["passed"])
        # An advisory failure is recorded but does not block.
        self.assertTrue(evidence["passed"])

    def test_legacy_evidence_fields_are_preserved(self):
        """review-package.py and older readers depend on these."""
        self.write_state(status="VALIDATING")
        self.write_plan()
        self._profile("Ran 4 tests in 0.01s\\n\\nOK\\n")

        with quiet():
            orch.run_validation(self.task_id)

        evidence = json.loads(
            (self.task_path / "test-results.json").read_text()
        )

        for key in ("command", "returncode", "passed", "test_count",
                    "failure_reason", "timestamp", "stdout", "stderr"):
            self.assertIn(key, evidence)



class CrossPlatformCommandTests(TaskDirCase):
    r"""Regression: shlex is POSIX, and mangled the Windows interpreter path.

    ``shlex.split`` treats backslashes as escapes, so splitting a command that
    already contained ``C:\Users\...\python.exe`` produced ``C:Users...`` and
    the check could never run. Tokenise first, substitute after.
    """

    def test_interpreter_path_survives_tokenisation(self):
        result = orch.run_validation_check(
            {"name": "v", "command": "{python} -c \"print('ok')\""}
        )

        self.assertTrue(result["passed"], result["stderr"])
        self.assertEqual(result["returncode"], 0)

    def test_recorded_command_contains_the_real_interpreter(self):
        result = orch.run_validation_check(
            {"name": "v", "command": "{python} --version"}
        )

        self.assertIn(sys.executable, result["command"])
        self.assertTrue(result["passed"])

    def test_backslashes_in_the_interpreter_path_are_not_eaten(self):
        if "\\" not in sys.executable:
            self.skipTest("interpreter path has no backslashes on this platform")

        result = orch.run_validation_check(
            {"name": "v", "command": "{python} -c \"pass\""}
        )

        self.assertIn("\\", result["command"])
        self.assertTrue(result["passed"])

    def test_criteria_support_the_python_placeholder(self):
        self.write_plan(
            "plan.json",
            plan_json(
                acceptance_criteria=[
                    {
                        "id": "AC-1",
                        "statement": "the interpreter runs",
                        "verify": '{python} -c "raise SystemExit(0)"',
                    }
                ]
            ),
        )
        _, state = self.write_state()

        summary = orch.evaluate_acceptance_criteria(self.task_id, state)

        self.assertEqual(summary["passed"], 1)

    def test_criteria_placeholder_failure_is_detected(self):
        self.write_plan(
            "plan.json",
            plan_json(
                acceptance_criteria=[
                    {
                        "id": "AC-1",
                        "statement": "the interpreter fails",
                        "verify": '{python} -c "raise SystemExit(3)"',
                    }
                ]
            ),
        )
        _, state = self.write_state()

        summary = orch.evaluate_acceptance_criteria(self.task_id, state)

        self.assertEqual(summary["failed"], 1)
        self.assertEqual(summary["results"][0]["returncode"], 3)


if __name__ == "__main__":
    unittest.main()
