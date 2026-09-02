"""Fix B3 / audit 2.4 - a task in IMPLEMENTING has a legal next action.

``route_failure`` set status IMPLEMENTING and emitted CLAUDE_FIX_STARTED, but
no verb was legal from there: ``implement`` required AWAITING_APPROVAL and
``validate`` required VALIDATING. The task could only be moved by hand-editing
state.json -- the manual intervention the workflow exists to remove.
"""

import json
import unittest
from unittest import mock

from _support import TaskDirCase, load_orchestrator, plan_json, quiet

orch = load_orchestrator()

PLAN = plan_json()


class FixVerbRegisteredTests(unittest.TestCase):
    def test_fix_is_a_dispatchable_action(self):
        self.assertIn("fix", orch.ACTIONS)

    def test_implementing_advertises_a_reachable_next_state(self):
        self.assertEqual(orch.VALID_NEXT_STATES["IMPLEMENTING"], "VALIDATING")


class FixLoopTests(TaskDirCase):
    def _routed_failure_state(self):
        """Put the task where route_failure leaves it after a CLAUDE_FIX."""
        self.write_state(
            status="IMPLEMENTING",
            failure_reason="Validation command failed with exit code 1",
        )
        self.write_requirement()
        self.write_plan("plan.json", PLAN)
        (self.task_path / "test-results.json").write_text(
            json.dumps(
                {
                    "passed": False,
                    "returncode": 1,
                    "test_count": 2,
                    "stdout": "",
                    "stderr": "FAIL: test_widget\nAssertionError: 1 != 2\n",
                }
            )
        )

    def _approve_for_current_plan(self):
        path, state = self.write_state(status="AWAITING_APPROVAL")

        with quiet():
            orch.approve_plan(self.task_id)

    def _run_fix(self):
        captured = {}

        def fake_run(argv, **kwargs):
            captured["argv"] = argv
            # The fix prompt reaches the worker on stdin, not as an argument.
            captured["prompt"] = kwargs.get("input")
            return mock.Mock(returncode=0, stdout="", stderr="")

        with mock.patch.object(
            orch, "current_branch", return_value="feature/TASK-999"
        ), mock.patch.object(orch.subprocess, "run", side_effect=fake_run):
            with quiet() as out:
                code = orch.run_fix(self.task_id)

        return code, captured, out.getvalue()

    def test_fix_advances_implementing_to_validating(self):
        """The mandated case: IMPLEMENTING is no longer a dead end."""
        self.write_requirement()
        self.write_plan("plan.json", PLAN)
        self._approve_for_current_plan()
        self._routed_failure_state()
        orch.record_event(self.task_id, "CLAUDE_FIX_STARTED", worker="claude")

        code, captured, _ = self._run_fix()

        self.assertEqual(code, 0)
        self.assertIn("argv", captured)
        self.assertEqual(self.read_state()["status"], "VALIDATING")

    def test_fix_prompt_carries_the_failure_evidence(self):
        self.write_requirement()
        self.write_plan("plan.json", PLAN)
        self._approve_for_current_plan()
        self._routed_failure_state()
        orch.record_event(self.task_id, "CLAUDE_FIX_STARTED", worker="claude")

        _, captured, _ = self._run_fix()
        prompt = captured["prompt"]

        self.assertIn("Validation command failed with exit code 1", prompt)
        self.assertIn("AssertionError: 1 != 2", prompt)
        self.assertIn("do not weaken or delete tests", prompt)

    def test_fix_records_mode_fix_on_implementation_started(self):
        self.write_requirement()
        self.write_plan("plan.json", PLAN)
        self._approve_for_current_plan()
        self._routed_failure_state()
        orch.record_event(self.task_id, "CLAUDE_FIX_STARTED", worker="claude")

        self._run_fix()

        modes = [
            e.get("mode")
            for e in self.events()
            if e["event"] == "IMPLEMENTATION_STARTED"
        ]
        self.assertIn("fix", modes)

    def test_fix_without_routed_failure_is_refused(self):
        self.write_requirement()
        self.write_plan("plan.json", PLAN)
        self._approve_for_current_plan()
        self._routed_failure_state()
        # No CLAUDE_FIX_STARTED event.

        code, captured, out = self._run_fix()

        self.assertEqual(code, 1)
        self.assertNotIn("argv", captured)
        self.assertIn("no CLAUDE_FIX_STARTED event", out)

    def test_fix_from_wrong_state_is_refused(self):
        self.write_requirement()
        self.write_plan("plan.json", PLAN)
        self.write_state(status="AWAITING_APPROVAL")
        orch.record_event(self.task_id, "CLAUDE_FIX_STARTED", worker="claude")

        code, captured, out = self._run_fix()

        self.assertEqual(code, 1)
        self.assertNotIn("argv", captured)
        self.assertIn("fix requires IMPLEMENTING", out)


class RouteFailureHandoffTests(TaskDirCase):
    def test_route_failure_leaves_a_state_fix_can_act_on(self):
        self.write_state(
            status="FAILED",
            failure_reason="Validation command failed with exit code 1",
        )

        with quiet():
            self.assertEqual(orch.route_failure(self.task_id), 0)

        self.assertEqual(self.read_state()["status"], "IMPLEMENTING")
        self.assertTrue(orch.has_event(self.task_id, "CLAUDE_FIX_STARTED"))


if __name__ == "__main__":
    unittest.main()
