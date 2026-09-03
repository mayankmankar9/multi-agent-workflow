"""Who may be handed a plan to work on, and what the state machine may claim.

TASK-007 exposed a state-machine defect. Its three plans had all been rejected
and none approved, so no implementation had ever been authorised -- yet
`route_failure` accepted a classifier's CLAUDE_FIX route, set status
IMPLEMENTING, and returned 0. `fix` then refused for want of an approval that
did not exist, and IMPLEMENTING has exactly one exit, so the task was stranded
in a state no verb accepted.

Two questions were being conflated, and they are kept apart here:

- *May implementation begin?* Entering IMPLEMENTING from AWAITING_APPROVAL
  means the developer has just decided on that specific plan version, so
  `verify_approval` requires the approval to be filed under it. A materially
  changed plan waits for its own approval. Unchanged by this work.
- *May a fix run inside an already-approved plan?* CLAUDE_FIX is autonomous
  work inside an approved scope, so what matters is whether an approval covers
  the bytes of the plan the worker would be handed -- not whether the version
  counter has moved. A replan that has not landed does not revoke the approved
  plan it would have replaced.
"""

import json
import unittest
from unittest import mock

from _support import TaskDirCase, load_orchestrator, plan_json, quiet

orch = load_orchestrator()

PLAN_A = plan_json(objective="Plan A.")
PLAN_B = plan_json(objective="Plan B, materially different.")


class AuthorizationCase(TaskDirCase):
    def _approve(self, name="plan.json", body=PLAN_A, plan_version=1):
        self.write_requirement()
        plan = self.write_plan(name, body)
        self.write_state(
            status="AWAITING_APPROVAL",
            plan_version=plan_version,
            plan_file=str(plan),
        )

        with quiet():
            orch.approve_plan(self.task_id)

        return plan

    def _reject(self, reason="AC-1 cannot fail."):
        with quiet():
            orch.reject_plan(self.task_id, reason)

    def _failed(self, **overrides):
        fields = {
            "status": "FAILED",
            "failure_reason": "Validation command failed with exit code 1",
        }
        fields.update(overrides)
        self.write_state(**fields)

    def _route(self):
        """Route a failure with the planner stubbed out.

        The classifier is forced to CLAUDE_FIX so the test is about the state
        machine's own check rather than about what a classifier happened to
        say -- the whole point is that routing must not take that on trust.
        """
        seen = {"workers": []}

        def fake_run(argv, **kwargs):
            seen["workers"].append(orch.Path(argv[0]).stem.lower())

            if "--output-last-message" in argv:
                out = argv[argv.index("--output-last-message") + 1]
                orch.Path(out).write_text(plan_json(), encoding="utf-8")

            return mock.Mock(
                returncode=0,
                stdout=json.dumps({"verdict": "pass", "issues": []}),
                stderr="",
            )

        with mock.patch.object(
            orch, "classify_failure_with_agent",
            return_value=("CLAUDE_FIX", {"source": "test"}),
        ), mock.patch.object(
            orch, "current_branch", return_value="feature/%s" % self.task_id
        ), mock.patch.object(orch.subprocess, "run", side_effect=fake_run):
            with quiet() as out:
                code = orch.route_failure(self.task_id)

        return code, seen, out.getvalue()

    def _overrides(self):
        return [
            e
            for e in self.events()
            if e["event"] == "FAILURE_ROUTE_OVERRIDDEN"
        ]


class ApprovedPlanAuthorizesAFixTests(AuthorizationCase):
    """The case that must keep working: in-scope autonomous fixing."""

    def test_an_approved_plan_authorizes_the_fix(self):
        self._approve()
        self._failed()

        code, _, _ = self._route()

        self.assertEqual(code, 0)
        self.assertEqual(self.read_state()["status"], "IMPLEMENTING")
        self.assertTrue(orch.has_event(self.task_id, "CLAUDE_FIX_STARTED"))
        self.assertEqual(self._overrides(), [])

    def test_a_bumped_plan_version_alone_does_not_block_it(self):
        """plan_file still points at the approved plan; the counter moved."""
        self._approve()
        self._failed(plan_version=4)

        code, _, _ = self._route()

        self.assertEqual(code, 0)
        self.assertEqual(self.read_state()["status"], "IMPLEMENTING")
        self.assertEqual(self._overrides(), [])

    def test_authorization_is_reported_against_the_plan_on_disk(self):
        self._approve()
        self._failed(plan_version=4)

        authorized, kind, _ = orch.fix_authorization(
            self.task_id, self.read_state()
        )

        self.assertTrue(authorized)
        self.assertEqual(kind, "approved")

    def test_a_reapproval_after_a_rejection_reinstates_it(self):
        """Ordered replay, not 'a rejection exists somewhere'."""
        self._approve()
        self._reject()
        self.write_state(
            status="AWAITING_APPROVAL", plan_version=2, plan_file=None
        )

        with quiet():
            orch.approve_plan(self.task_id)

        self._failed(plan_version=2)
        authorized, _, _ = orch.fix_authorization(
            self.task_id, self.read_state()
        )

        self.assertTrue(authorized)


class UnapprovedPlanReachesNoWorkerTests(AuthorizationCase):
    """No unapproved plan may be handed to the implementation worker."""

    def test_a_rejected_plan_does_not_authorize_a_fix(self):
        self._approve()
        self._reject()
        self._failed(plan_version=2)

        authorized, kind, _ = orch.fix_authorization(
            self.task_id, self.read_state()
        )

        self.assertFalse(authorized)
        self.assertEqual(kind, "plan_rejected")

    def test_a_task_with_no_approval_at_all_does_not_authorize_a_fix(self):
        """TASK-007 exactly: three plans, three rejections, no approval."""
        self.write_requirement()
        plan = self.write_plan("plan.json", PLAN_A)
        self.write_state(
            status="AWAITING_APPROVAL", plan_version=1, plan_file=str(plan)
        )
        self._reject()
        self._failed(plan_version=2)

        code, seen, output = self._route()

        # Replanned, critiqued, and parked at the human gate -- never handed
        # to an implementation worker.
        self.assertEqual(self.read_state()["status"], "AWAITING_APPROVAL")
        self.assertIn("not authorised", output)
        self.assertFalse(orch.has_event(self.task_id, "CLAUDE_FIX_STARTED"))
        self.assertFalse(
            orch.has_event(self.task_id, "IMPLEMENTATION_STARTED")
        )

    def test_the_override_is_recorded_with_its_reason(self):
        self._approve()
        self._reject()
        self._failed(plan_version=2)

        self._route()
        overrides = self._overrides()

        self.assertEqual(len(overrides), 1)
        self.assertEqual(overrides[0]["classifier_route"], "CLAUDE_FIX")
        self.assertEqual(overrides[0]["route"], "CODEX_REPLAN")
        self.assertEqual(overrides[0]["reason"], "plan_rejected")

    def test_a_rejected_plan_routes_to_replanning_not_implementing(self):
        self._approve()
        self._reject()
        self._failed(plan_version=2)

        with mock.patch.dict(
            orch.os.environ, {orch.REPLAN_BUDGET_ENV: "4"}
        ):
            self._route()

        self.assertEqual(self.read_state()["status"], "AWAITING_APPROVAL")
        self.assertFalse(orch.has_event(self.task_id, "CLAUDE_FIX_STARTED"))

    def test_the_replan_it_routes_to_still_faces_the_critic(self):
        self._approve()
        self._reject()
        self._failed(plan_version=2)

        with mock.patch.dict(
            orch.os.environ, {orch.REPLAN_BUDGET_ENV: "4"}
        ):
            self._route()

        critiques = [e for e in self.events() if e["event"] == "PLAN_CRITIQUED"]

        self.assertTrue(critiques)
        self.assertTrue(critiques[-1]["ran"])

    def test_an_unapproved_candidate_plan_asks_the_developer(self):
        """A plan awaiting a decision is not something to replan around."""
        self.write_requirement()
        plan = self.write_plan("plan.json", PLAN_A)
        self._failed(plan_version=1, plan_file=str(plan))

        self._route()

        self.assertEqual(self._overrides()[0]["route"], "DEVELOPER_CLARIFICATION")
        self.assertTrue(
            orch.has_event(self.task_id, "DEVELOPER_CLARIFICATION_REQUIRED")
        )

    def test_a_missing_plan_file_is_not_authorization(self):
        self._failed()

        authorized, kind, _ = orch.fix_authorization(
            self.task_id, self.read_state()
        )

        self.assertFalse(authorized)
        self.assertEqual(kind, "missing_plan")


class MateriallyChangedPlanWaitsForApprovalTests(AuthorizationCase):
    """The implement gate is unchanged, and stays the stricter check."""

    def _implement(self):
        captured = {}

        def fake_run(argv, **kwargs):
            captured["argv"] = argv
            return mock.Mock(returncode=0, stdout="", stderr="")

        with mock.patch.object(
            orch, "current_branch", return_value="feature/%s" % self.task_id
        ), mock.patch.object(orch.subprocess, "run", side_effect=fake_run):
            with quiet() as out:
                code = orch.run_implementation(self.task_id)

        return code, captured, out.getvalue()

    def test_a_replanned_plan_cannot_be_implemented_unapproved(self):
        self._approve()
        plan_b = self.write_plan("plan-v2.json", PLAN_B)
        self.write_state(
            status="AWAITING_APPROVAL", plan_version=2, plan_file=str(plan_b)
        )

        code, captured, output = self._implement()

        self.assertEqual(code, 1)
        self.assertNotIn("argv", captured)
        self.assertIn("no PLAN_APPROVED event for plan version 2", output)
        self.assertEqual(self.read_state()["status"], "AWAITING_APPROVAL")

    def test_a_replanned_plan_does_not_authorize_a_fix_either(self):
        self._approve()
        plan_b = self.write_plan("plan-v2.json", PLAN_B)
        self._failed(plan_version=2, plan_file=str(plan_b))

        authorized, kind, _ = orch.fix_authorization(
            self.task_id, self.read_state()
        )

        self.assertFalse(authorized)
        self.assertEqual(kind, "no_approval")

    def test_approving_the_new_plan_is_what_unblocks_it(self):
        self._approve()
        plan_b = self.write_plan("plan-v2.json", PLAN_B)
        self.write_state(
            status="AWAITING_APPROVAL", plan_version=2, plan_file=str(plan_b)
        )

        with quiet():
            orch.approve_plan(self.task_id)

        self.write_baseline()
        code, captured, _ = self._implement()

        self.assertEqual(code, 0)
        self.assertIn("argv", captured)


class NoStrandingTests(AuthorizationCase):
    """A precondition a fix cannot satisfy must not leave it in IMPLEMENTING.

    IMPLEMENTING's only exit is `fix`. A bare non-zero return from `fix` is
    therefore a dead end, which is the defect `fix` was introduced to remove --
    reached through a different door.
    """

    def _fix(self):
        def fake_run(argv, **kwargs):
            return mock.Mock(returncode=0, stdout="", stderr="")

        with mock.patch.object(
            orch, "current_branch", return_value="feature/%s" % self.task_id
        ), mock.patch.object(orch.subprocess, "run", side_effect=fake_run):
            with quiet() as out:
                code = orch.run_fix(self.task_id)

        return code, out.getvalue()

    def _parked_in_implementing(self):
        orch.record_event(self.task_id, "CLAUDE_FIX_STARTED", worker="claude")
        self.write_state(status="IMPLEMENTING", plan_version=1)

    def test_a_missing_approval_lands_in_failed_not_implementing(self):
        self.write_requirement()
        self.write_plan("plan.json", PLAN_A)
        self._parked_in_implementing()

        code, _ = self._fix()

        self.assertNotEqual(code, 0)
        self.assertEqual(self.read_state()["status"], "FAILED")
        self.assertIn(
            "fix-precondition", self.read_state()["failure_reason"]
        )

    def test_a_missing_baseline_lands_in_failed_not_implementing(self):
        self._approve()
        self._parked_in_implementing()

        code, output = self._fix()

        self.assertNotEqual(code, 0)
        self.assertEqual(self.read_state()["status"], "FAILED")
        self.assertIn("no task baseline", output)

    def test_a_missing_plan_lands_in_failed_not_implementing(self):
        self._parked_in_implementing()

        code, _ = self._fix()

        self.assertNotEqual(code, 0)
        self.assertEqual(self.read_state()["status"], "FAILED")

    def test_the_failed_state_it_lands_in_is_routable(self):
        """FAILED is actionable; IMPLEMENTING without an approval is not."""
        self.write_requirement()
        self.write_plan("plan.json", PLAN_A)
        self._parked_in_implementing()
        self._fix()

        self.assertEqual(self.read_state()["status"], "FAILED")
        # route_failure accepts FAILED, and will not send it back to a fix it
        # has already established is unauthorised.
        self._route()

        self.assertNotEqual(self.read_state()["status"], "IMPLEMENTING")

    def test_the_worker_is_never_invoked_when_unauthorized(self):
        self.write_requirement()
        self.write_plan("plan.json", PLAN_A)
        self._parked_in_implementing()
        calls = []

        def fake_run(argv, **kwargs):
            calls.append(argv)
            return mock.Mock(returncode=0, stdout="", stderr="")

        with mock.patch.object(
            orch, "current_branch", return_value="feature/%s" % self.task_id
        ), mock.patch.object(orch.subprocess, "run", side_effect=fake_run):
            with quiet():
                orch.run_fix(self.task_id)

        self.assertEqual(
            [c for c in calls if orch.Path(c[0]).stem.lower() == "claude"], []
        )


class OneAuthorizationQuestionTests(unittest.TestCase):
    """The router and the fix verb must not be able to drift apart.

    The stranding came from two checks answering the same question
    differently: routing said yes on a classifier's word, the gate said no.
    Sharing one function is the structural fix; this asserts it stays shared.
    """

    def test_the_router_and_the_fix_verb_ask_the_same_function(self):
        source = (orch.Path(".ai") / "scripts" / "orchestrator.py").read_text(
            encoding="utf-8"
        )

        # The definition, plus the call in route_failure and the one in run_fix.
        self.assertEqual(source.count("fix_authorization("), 3)

    def test_entering_implementation_still_uses_the_stricter_gate(self):
        """verify_approval is untouched and still version-bound."""
        source = (orch.Path(".ai") / "scripts" / "orchestrator.py").read_text(
            encoding="utf-8"
        )

        self.assertIn("def verify_approval(", source)
        self.assertIn("find_approval(task_id, plan_version)", source)


if __name__ == "__main__":
    unittest.main()
