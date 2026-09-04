"""Fix B3 / audit 2.4 - a task in IMPLEMENTING has a legal next action.

``route_failure`` set status IMPLEMENTING and emitted CLAUDE_FIX_STARTED, but
no verb was legal from there: ``implement`` required AWAITING_APPROVAL and
``validate`` required VALIDATING. The task could only be moved by hand-editing
state.json -- the manual intervention the workflow exists to remove.
"""

import json
import unittest
from unittest import mock

from _support import (
    TaskDirCase,
    load_orchestrator,
    plan_json,
    quiet,
    worker_name,
)

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
        # `implement` captured this before the first attempt ran. `fix` refuses
        # without it rather than capturing late, which would count the failed
        # attempt's own output as pre-existing.
        self.write_baseline()
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
    """CLAUDE_FIX is autonomous work inside an approved scope.

    This test used to route with no plan and no approval on the tree at all,
    and assert IMPLEMENTING anyway. That is the defect, not the contract: the
    task landed in a state whose only exit is `fix`, and `fix` then refused for
    want of the approval nothing had given. Routing now asks the question the
    implement gate asks, before it claims the state.
    """

    def _approved(self):
        self.write_requirement()
        self.write_plan("plan.json", PLAN)
        self.write_state(status="AWAITING_APPROVAL")

        with quiet():
            orch.approve_plan(self.task_id)

        self.write_state(
            status="FAILED",
            failure_reason="Validation command failed with exit code 1",
        )

    def test_route_failure_leaves_a_state_fix_can_act_on(self):
        """An approved plan authorises an in-scope fix under it."""
        self._approved()

        with quiet():
            self.assertEqual(orch.route_failure(self.task_id), 0)

        self.assertEqual(self.read_state()["status"], "IMPLEMENTING")
        self.assertTrue(orch.has_event(self.task_id, "CLAUDE_FIX_STARTED"))

    def test_a_bumped_version_alone_does_not_block_the_fix(self):
        """The gate is the approval on the plan's bytes, not the counter.

        A replan that has not landed leaves plan_file pointing at the approved
        plan. That plan still authorises fixes under it, so the fix must not be
        refused merely because the version counter moved.
        """
        self._approved()
        # The rejection/replan bump, without a new plan having landed:
        # plan_file still resolves to the approved plan.json.
        self.write_state(
            status="FAILED",
            plan_version=2,
            failure_reason="Validation command failed with exit code 1",
        )

        with quiet():
            self.assertEqual(orch.route_failure(self.task_id), 0)

        self.assertEqual(self.read_state()["status"], "IMPLEMENTING")
        self.assertTrue(orch.has_event(self.task_id, "CLAUDE_FIX_STARTED"))


class FailureReasonLifecycleTests(TaskDirCase):
    """A resolved failure must not follow the task around.

    TASK-007's state.json carried ``fix-precondition: plan_rejected`` from a
    superseded routing decision while its status was IMPLEMENTING and its
    approval gate had passed: every later prompt and report read a live failure
    that no longer existed. The reason belongs in the append-only trail, which
    is why clearing it from the live state loses nothing.
    """

    REASON = "Validation command failed with exit code 1"

    def _reset(self):
        """Clear the task's evidence so the next route starts from FAILED."""
        for child in sorted(self.task_path.iterdir()):
            if child.is_file():
                child.unlink()

    def _failed(self, **overrides):
        self.write_requirement()
        self.write_plan("plan.json", PLAN)
        self.write_state(status="AWAITING_APPROVAL")

        with quiet():
            orch.approve_plan(self.task_id)

        self.write_state(
            status="FAILED", failure_reason=self.REASON, **overrides
        )

    def _route(self, route):
        """Route a FAILED task, with both workers doubled."""

        def fake_run(argv, **kwargs):
            if worker_name(argv) == "claude":
                return mock.Mock(
                    returncode=0,
                    stdout=json.dumps({"verdict": "pass", "issues": []}),
                    stderr="",
                )

            out = argv[argv.index("--output-last-message") + 1]
            orch.Path(out).write_text(plan_json(), encoding="utf-8")
            return mock.Mock(returncode=0, stdout="", stderr="")

        with mock.patch.object(
            orch,
            "classify_failure_with_agent",
            return_value=(route, {"source": "test"}),
        ), mock.patch.object(orch.subprocess, "run", side_effect=fake_run):
            with quiet() as out:
                code = orch.route_failure(self.task_id)

        return code, out.getvalue()

    def test_failure_reason_is_cleared_on_every_exit_from_failed(self):
        """Each route that leaves FAILED, and the one that does not.

        Enumerated against ``FAILURE_ROUTES`` rather than against a list this
        test happens to know, so a route added later cannot slip past with no
        assertion about the reason it leaves behind.
        """
        self.assertEqual(
            set(orch.FAILURE_ROUTES),
            {"CLAUDE_FIX", "CODEX_REPLAN", "DEVELOPER_CLARIFICATION"},
            "a new failure route needs its own failure_reason assertion here",
        )

        exits = {"CLAUDE_FIX": "IMPLEMENTING", "CODEX_REPLAN": "AWAITING_APPROVAL"}

        for route, expected in exits.items():
            with self.subTest(route=route):
                self._reset()
                self._failed()

                code, _ = self._route(route)
                state = self.read_state()

                self.assertEqual(code, 0)
                self.assertEqual(state["status"], expected)
                self.assertNotEqual(state["status"], "FAILED")
                self.assertIsNone(
                    state["failure_reason"],
                    "%s left the failure reason on a task that is no longer "
                    "failed" % route,
                )
                # Nothing was lost: the reason is in the append-only trail, and
                # the prompt builders read it from there.
                routed = [
                    e for e in self.events() if e["event"] == "FAILURE_ROUTED"
                ]
                self.assertEqual(routed[-1]["failure_reason"], self.REASON)
                self.assertEqual(
                    orch.last_recorded_failure_reason(self.task_id),
                    self.REASON,
                )

        # DEVELOPER_CLARIFICATION is not an exit: the task stays FAILED, so its
        # reason is still live and must survive.
        self._reset()
        self._failed()
        self._route("DEVELOPER_CLARIFICATION")

        held = self.read_state()
        self.assertEqual(held["status"], "FAILED")
        self.assertEqual(held["failure_reason"], self.REASON)

    def test_the_fix_prompt_still_has_the_reason_after_clearing(self):
        """Clearing must not cost the fix worker its evidence.

        ``run_fix`` builds its prompt after routing has already cleared the
        live field, so the evidence has to come out of the trail.
        """
        self._failed()
        self.write_baseline()
        self._route("CLAUDE_FIX")

        self.assertIsNone(self.read_state()["failure_reason"])

        _, state = orch.load_state(self.task_id)

        self.assertIn(self.REASON, orch.failure_evidence(self.task_id, state))

    def test_a_replan_records_the_reason_before_clearing_it(self):
        """The replan prompt is built from the pre-clear snapshot."""
        self._failed()

        captured = {}

        def fake_run(argv, **kwargs):
            if worker_name(argv) == "claude":
                return mock.Mock(
                    returncode=0,
                    stdout=json.dumps({"verdict": "pass", "issues": []}),
                    stderr="",
                )

            captured["prompt"] = kwargs.get("input")
            out = argv[argv.index("--output-last-message") + 1]
            orch.Path(out).write_text(plan_json(), encoding="utf-8")
            return mock.Mock(returncode=0, stdout="", stderr="")

        with mock.patch.object(
            orch,
            "classify_failure_with_agent",
            return_value=("CODEX_REPLAN", {"source": "test"}),
        ), mock.patch.object(orch.subprocess, "run", side_effect=fake_run):
            with quiet():
                orch.route_failure(self.task_id)

        self.assertIsNone(self.read_state()["failure_reason"])
        self.assertIn(self.REASON, captured["prompt"])


if __name__ == "__main__":
    unittest.main()
