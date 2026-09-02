"""Fix B4 / audit 2.5 - an empty test suite is not a pass.

TASK-004 went VALIDATING -> VALIDATED -> PR_READY -> COMPLETED on
``Ran 0 tests in 0.000s / OK``. The gate could not distinguish "everything
passed" from "nothing ran".

Note on design: the rule lives in ``evaluate_validation``, a pure function, and
NOT in this suite. A test asserting "the suite is non-empty" would make the
suite non-empty and could therefore never fail. Keeping the rule pure lets
these tests feed it a synthetic empty run and assert the verdict.
"""

import json
import subprocess
import sys
import unittest
from unittest import mock

from _support import TaskDirCase, load_orchestrator, quiet

orch = load_orchestrator()

EMPTY_SUITE_STDERR = "\n" + "-" * 70 + "\nRan 0 tests in 0.000s\n\nOK\n"
THREE_PASS_STDERR = "\n" + "-" * 70 + "\nRan 3 tests in 0.012s\n\nOK\n"
ONE_PASS_STDERR = "\n" + "-" * 70 + "\nRan 1 test in 0.001s\n\nOK\n"


class ParseTestCountTests(unittest.TestCase):
    def test_reads_count_from_stderr(self):
        self.assertEqual(orch.parse_test_count("", THREE_PASS_STDERR), 3)

    def test_reads_zero(self):
        self.assertEqual(orch.parse_test_count("", EMPTY_SUITE_STDERR), 0)

    def test_handles_singular_test(self):
        self.assertEqual(orch.parse_test_count("", ONE_PASS_STDERR), 1)

    def test_falls_back_to_stdout(self):
        self.assertEqual(orch.parse_test_count(THREE_PASS_STDERR, ""), 3)

    def test_returns_none_when_absent(self):
        self.assertIsNone(orch.parse_test_count("", "some unrelated output"))

    def test_last_summary_wins(self):
        combined = THREE_PASS_STDERR + ONE_PASS_STDERR
        self.assertEqual(orch.parse_test_count("", combined), 1)


class EvaluateValidationTests(unittest.TestCase):
    def test_empty_suite_with_exit_zero_fails(self):
        """The mandated case."""
        passed, reason, count = orch.evaluate_validation(
            0, "", EMPTY_SUITE_STDERR
        )

        self.assertFalse(passed)
        self.assertEqual(count, 0)
        self.assertIn("empty test suite is not a pass", reason)

    def test_tests_ran_and_passed(self):
        passed, reason, count = orch.evaluate_validation(
            0, "", THREE_PASS_STDERR
        )

        self.assertTrue(passed)
        self.assertIsNone(reason)
        self.assertEqual(count, 3)

    def test_nonzero_exit_fails_even_with_tests(self):
        passed, reason, count = orch.evaluate_validation(
            1, "", THREE_PASS_STDERR
        )

        self.assertFalse(passed)
        self.assertEqual(count, 3)
        self.assertIn("exit code 1", reason)

    def test_unparseable_output_fails(self):
        passed, reason, count = orch.evaluate_validation(0, "", "no summary")

        self.assertFalse(passed)
        self.assertIsNone(count)
        self.assertIn("no test count", reason)

    def test_empty_suite_is_diagnosed_as_empty_even_when_exit_is_nonzero(self):
        """Python 3.12 exits 5 on a zero-test run; 3.9 exits 0.

        Either way the useful diagnosis is "no tests ran", not the exit code.
        """
        passed, reason, count = orch.evaluate_validation(
            5, "", EMPTY_SUITE_STDERR
        )

        self.assertFalse(passed)
        self.assertEqual(count, 0)
        self.assertIn("empty test suite", reason)
        self.assertNotIn("exit code", reason)

    def test_real_failures_still_report_the_exit_code(self):
        failing = "\nRan 3 tests in 0.01s\n\nFAILED (failures=1)\n"
        passed, reason, count = orch.evaluate_validation(1, "", failing)

        self.assertFalse(passed)
        self.assertEqual(count, 3)
        self.assertIn("exit code 1", reason)

    def test_exact_task_004_evidence_now_fails(self):
        """Replay the committed TASK-004 evidence through the new gate."""
        passed, _, _ = orch.evaluate_validation(
            0, "", "\n----------------------------------------------------------------------\nRan 0 tests in 0.000s\n\nOK\n"
        )

        self.assertFalse(passed)


class ValidationCommandTests(unittest.TestCase):
    """The command must use the running interpreter, not a bare 'python3'.

    On Windows 'python3' resolves to a Microsoft Store stub that exits non-zero
    without running anything, so validation failed for reasons unrelated to the
    code under test.
    """

    def test_uses_the_running_interpreter(self):
        self.assertEqual(orch.validation_command()[0], sys.executable)

    def test_does_not_hardcode_python3(self):
        self.assertNotEqual(orch.validation_command()[0], "python3")

    def test_still_runs_unittest_verbosely(self):
        self.assertEqual(
            orch.validation_command()[1:], ["-m", "unittest", "-v"]
        )

    def test_falls_back_when_executable_is_unknown(self):
        with mock.patch.object(orch.sys, "executable", ""):
            self.assertEqual(orch.validation_command()[0], "python3")

    def test_the_command_actually_runs_on_this_host(self):
        """The regression that motivated this: the command must be executable."""
        result = subprocess.run(
            orch.validation_command()[:1] + ["-c", "print('ok')"],
            capture_output=True,
            text=True,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("ok", result.stdout)


class RunValidationTests(TaskDirCase):
    def _validate(self, returncode, stderr):
        def fake_run(argv, **kwargs):
            return mock.Mock(returncode=returncode, stdout="", stderr=stderr)

        with mock.patch.object(orch.subprocess, "run", side_effect=fake_run):
            with quiet() as out:
                code = orch.run_validation(self.task_id)

        return code, out.getvalue()

    def test_empty_suite_moves_task_to_failed(self):
        self.write_state(status="VALIDATING")

        code, out = self._validate(0, EMPTY_SUITE_STDERR)

        self.assertNotEqual(code, 0)
        self.assertEqual(self.read_state()["status"], "FAILED")

        evidence = json.loads(
            (self.task_path / "test-results.json").read_text()
        )
        self.assertFalse(evidence["passed"])
        self.assertEqual(evidence["test_count"], 0)
        self.assertIn("empty test suite", evidence["failure_reason"])

        recorded = [e for e in self.events() if e["event"] == "VALIDATION"]
        self.assertFalse(recorded[-1]["passed"])
        self.assertEqual(recorded[-1]["test_count"], 0)

    def test_real_tests_move_task_to_validated(self):
        self.write_state(status="VALIDATING")

        code, _ = self._validate(0, THREE_PASS_STDERR)

        self.assertEqual(code, 0)
        self.assertEqual(self.read_state()["status"], "VALIDATED")

        evidence = json.loads(
            (self.task_path / "test-results.json").read_text()
        )
        self.assertTrue(evidence["passed"])
        self.assertEqual(evidence["test_count"], 3)

    def test_validation_timeout_is_a_failure_not_a_hang(self):
        self.write_state(status="VALIDATING")

        def fake_run(argv, **kwargs):
            raise orch.subprocess.TimeoutExpired(cmd=argv, timeout=1)

        with mock.patch.object(orch.subprocess, "run", side_effect=fake_run):
            with quiet():
                code = orch.run_validation(self.task_id)

        self.assertNotEqual(code, 0)
        self.assertEqual(self.read_state()["status"], "FAILED")


if __name__ == "__main__":
    unittest.main()
