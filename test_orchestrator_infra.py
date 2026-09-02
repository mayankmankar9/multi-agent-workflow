"""Fix B9 / audit 2.11 - operational hardening.

Covers: subprocess timeouts, atomic state writes, a per-task lock, the plan
structural pre-flight, and the branch-order bug where a state error surfaced as
a confusing branch error.
"""

import json
import os
import unittest
from unittest import mock

from _support import REPO_ROOT, TaskDirCase, load_orchestrator, quiet

orch = load_orchestrator()


class TimeoutTests(unittest.TestCase):
    def test_every_timeout_constant_is_positive(self):
        for name in (
            "CODEX_TIMEOUT_S",
            "CLAUDE_TIMEOUT_S",
            "VALIDATION_TIMEOUT_S",
            "GIT_TIMEOUT_S",
        ):
            self.assertGreater(getattr(orch, name), 0, name)

    def test_no_subprocess_run_call_omits_a_timeout(self):
        """A hung worker must fail the task, not block the orchestrator."""
        source = (REPO_ROOT / ".ai" / "scripts" / "orchestrator.py").read_text()
        calls = source.count("subprocess.run(")
        timeouts = source.count("timeout=")

        self.assertGreaterEqual(
            timeouts,
            calls,
            "found %d subprocess.run calls but only %d timeout= arguments"
            % (calls, timeouts),
        )


class AtomicStateWriteTests(TaskDirCase):
    def test_write_produces_valid_state_and_no_leftover_temp_files(self):
        path, state = self.write_state()
        state["status"] = "IMPLEMENTING"

        orch.save_state(path, state)

        self.assertEqual(json.loads(path.read_text())["status"], "IMPLEMENTING")
        self.assertEqual(list(self.task_path.glob("*.tmp")), [])

    def test_failed_replace_leaves_the_original_intact(self):
        """A crash mid-write must not truncate state.json."""
        path, state = self.write_state(status="AWAITING_APPROVAL")
        original = path.read_text()

        state["status"] = "CORRUPTED_ATTEMPT"

        with mock.patch.object(
            orch.os, "replace", side_effect=OSError("simulated crash")
        ):
            with self.assertRaises(OSError):
                orch.save_state(path, state)

        self.assertEqual(path.read_text(), original)
        self.assertEqual(json.loads(path.read_text())["status"], "AWAITING_APPROVAL")
        self.assertEqual(list(self.task_path.glob("*.tmp")), [])

    def test_updated_at_is_refreshed(self):
        path, state = self.write_state()
        before = state["updated_at"]

        orch.save_state(path, state)

        self.assertNotEqual(json.loads(path.read_text())["updated_at"], before)


class TaskLockTests(TaskDirCase):
    def test_lock_is_exclusive(self):
        self.write_state()

        with orch.task_lock(self.task_id):
            with self.assertRaises(RuntimeError) as ctx:
                with orch.task_lock(self.task_id):
                    pass

        self.assertIn("locked by another invocation", str(ctx.exception))

    def test_lock_is_released_on_exit(self):
        self.write_state()

        with orch.task_lock(self.task_id) as lock_path:
            self.assertTrue(lock_path.exists())

        self.assertFalse(lock_path.exists())

    def test_lock_is_released_even_when_the_body_raises(self):
        self.write_state()

        with self.assertRaises(ValueError):
            with orch.task_lock(self.task_id) as lock_path:
                raise ValueError("boom")

        self.assertFalse(lock_path.exists())

    def test_lock_records_the_holder_pid(self):
        self.write_state()

        with orch.task_lock(self.task_id) as lock_path:
            self.assertIn(str(os.getpid()), lock_path.read_text())


class PlanPreflightTests(unittest.TestCase):
    """Structural checks only. This is deliberately NOT a YAML parse."""

    def _plan(self, **overrides):
        keys = {key: '"x"' for key in orch.PLAN_REQUIRED_KEYS}
        keys.update(overrides)
        return "".join("%s: %s\n" % (k, v) for k, v in keys.items())

    def test_accepts_a_complete_plan(self):
        self.assertEqual(orch.check_plan_wellformed(self._plan()), [])

    def test_accepts_the_committed_task_004_plan(self):
        """A real Codex plan already in the repo must still pass."""
        plan = REPO_ROOT / ".ai" / "tasks" / "TASK-004" / "plan.yaml"

        if not plan.is_file():
            self.skipTest("TASK-004 plan.yaml not present")

        self.assertEqual(orch.check_plan_wellformed(plan.read_text()), [])

    def test_rejects_empty(self):
        self.assertEqual(orch.check_plan_wellformed("   \n\n"), ["plan is empty"])

    def test_rejects_markdown_fence(self):
        fenced = "`" * 3 + "yaml\n" + self._plan() + "`" * 3 + "\n"
        problems = orch.check_plan_wellformed(fenced)

        self.assertTrue(any("code fence" in p for p in problems))

    def test_rejects_prose_preamble(self):
        problems = orch.check_plan_wellformed(
            "Here is the plan you asked for:\n\n" + self._plan()
        )

        self.assertTrue(
            any("does not begin with a top-level key" in p for p in problems)
        )

    def test_reports_missing_keys(self):
        partial = 'objective: "x"\nrequirements: "y"\n'
        problems = orch.check_plan_wellformed(partial)

        joined = " ".join(problems)
        self.assertIn("missing top-level keys", joined)
        self.assertIn("acceptance_criteria", joined)

    def test_tolerates_leading_comments(self):
        self.assertEqual(
            orch.check_plan_wellformed("# generated plan\n" + self._plan()), []
        )


class BranchOrderTests(TaskDirCase):
    def test_state_error_is_reported_before_branch_error(self):
        """audit 2.11 - the branch check used to run first and mask the cause."""
        self.write_state(status="COMPLETED")
        self.write_requirement()
        self.write_plan("plan.yaml", 'objective: "demo"\n')

        def boom():
            raise AssertionError("branch check ran before the status check")

        with mock.patch.object(orch, "current_branch", side_effect=boom):
            with quiet() as out:
                code = orch.run_implementation(self.task_id)

        self.assertEqual(code, 1)
        self.assertIn("implementation requires AWAITING_APPROVAL", out.getvalue())


class DispatchTests(unittest.TestCase):
    def test_status_is_not_locked(self):
        """status must stay readable while another invocation holds the lock."""
        self.assertNotIn("status", orch.ACTIONS)

    def test_all_mutating_verbs_are_dispatchable(self):
        for verb in (
            "plan",
            "approve",
            "implement",
            "fix",
            "validate",
            "review",
            "route-failure",
            "complete",
        ):
            self.assertIn(verb, orch.ACTIONS, verb)

    def test_shebang_is_the_first_line(self):
        source = (REPO_ROOT / ".ai" / "scripts" / "orchestrator.py").read_text()

        self.assertTrue(source.startswith("#!/usr/bin/env python3"))



class InterpreterAndWorkerTests(unittest.TestCase):
    """No hardcoded interpreter names; missing workers fail legibly."""

    def test_no_hardcoded_python3_invocation_remains(self):
        source = (REPO_ROOT / ".ai" / "scripts" / "orchestrator.py").read_text()

        # A bare "python3" as the first element of an argv list is the bug:
        # on Windows it resolves to a Store stub that runs nothing.
        self.assertNotIn('["python3"', source)
        self.assertNotIn('"python3",\n', source.replace(
            'sys.executable or "python3",\n', ""
        ))

    def test_missing_worker_reports_which_executable(self):
        with mock.patch.object(
            orch.subprocess, "run", side_effect=FileNotFoundError()
        ):
            with self.assertRaises(RuntimeError) as ctx:
                orch.run_worker(["codex", "exec"], 5)

        message = str(ctx.exception)
        self.assertIn("codex", message)
        self.assertIn("not found on PATH", message)

    def test_run_worker_passes_the_timeout_through(self):
        captured = {}

        def fake_run(argv, **kwargs):
            captured.update(kwargs)
            return mock.Mock(returncode=0)

        with mock.patch.object(orch.subprocess, "run", side_effect=fake_run):
            orch.run_worker(["claude", "--print"], 1234)

        self.assertEqual(captured.get("timeout"), 1234)
        self.assertTrue(captured.get("check"))


if __name__ == "__main__":
    unittest.main()
