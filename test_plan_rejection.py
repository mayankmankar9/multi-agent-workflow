"""The human gate can say no.

`AWAITING_APPROVAL` had exactly one exit: `approve`. A developer who read a
plan and found it wrong had no recorded action — the only ways forward were to
approve something they disagreed with, or to edit `state.json` by hand, which
is the unearned evidence this workflow exists to prevent. The gate could say
yes, and it could say nothing.

Found by needing it: TASK-007's first plan carried an acceptance criterion
(`py -3.9 -m unittest && py -3.12 -m unittest`) that exits 103 on the machine
it was planned on, because uv registers its interpreters under
`Astral/CPython3.9.25` rather than as plain `3.9`. The plan critic flagged it
as a medium and returned `pass`, because the gate only bounces on `blocking`.
There was then no way to refuse the plan without faking state.

Also pinned here: the planning guidance that stops a criterion reaching for an
interpreter it cannot have.
"""

import json
import unittest
from unittest import mock

from _support import REPO_ROOT, TaskDirCase, load_orchestrator, quiet

orch = load_orchestrator()


class RejectPlanTests(TaskDirCase):
    def _awaiting(self, plan_version=1):
        self.write_requirement()
        plan = self.write_plan("plan.json")
        self.write_state(
            status="AWAITING_APPROVAL",
            plan_version=plan_version,
            plan_file=str(plan),
        )
        return plan

    def test_rejection_moves_the_task_to_replanning(self):
        self._awaiting()

        with quiet():
            code = orch.reject_plan(self.task_id, "AC-5 cannot run here")

        self.assertEqual(code, 0)
        self.assertEqual(self.read_state()["status"], "REPLANNING")

    def test_rejection_bumps_the_plan_version(self):
        """So the rejected plan keeps its own version on disk."""
        self._awaiting(plan_version=1)

        with quiet():
            orch.reject_plan(self.task_id, "unrunnable criterion")

        self.assertEqual(self.read_state()["plan_version"], 2)

    def test_the_reason_reaches_the_replanner(self):
        """A rejection the replanner cannot see is one it will repeat."""
        self._awaiting()

        with quiet():
            orch.reject_plan(self.task_id, "SENTINEL_REASON")

        self.assertIn("SENTINEL_REASON", self.read_state()["failure_reason"])

    def test_rejection_is_recorded_as_an_event(self):
        plan = self._awaiting()

        with quiet():
            orch.reject_plan(self.task_id, "no good")

        rejected = [e for e in self.events() if e["event"] == "PLAN_REJECTED"]

        self.assertEqual(len(rejected), 1)
        self.assertEqual(rejected[0]["rejected_by"], "developer")
        self.assertEqual(rejected[0]["plan_version"], 1)
        self.assertEqual(rejected[0]["reason"], "no good")

    def test_the_rejected_plan_is_identified_by_hash(self):
        """Which plan was refused must be recoverable, not inferred."""
        plan = self._awaiting()

        with quiet():
            orch.reject_plan(self.task_id, "no good")

        rejected = [e for e in self.events() if e["event"] == "PLAN_REJECTED"][0]

        self.assertEqual(rejected["plan_sha256"], orch.sha256_of(plan))

    def test_rejection_lands_on_the_blackboard(self):
        self._awaiting()

        with quiet():
            orch.reject_plan(self.task_id, "AC-5 unrunnable")

        blocks = orch.read_context(self.task_id)
        contents = " ".join(block.get("content", "") for block in blocks)

        self.assertIn("rejected plan v1", contents)
        self.assertIn("AC-5 unrunnable", contents)

    def test_a_reason_is_required(self):
        self._awaiting()

        with quiet() as out:
            code = orch.reject_plan(self.task_id, "")

        self.assertEqual(code, 1)
        self.assertIn("needs a reason", out.getvalue())

    def test_a_whitespace_reason_is_not_a_reason(self):
        self._awaiting()

        with quiet():
            self.assertEqual(orch.reject_plan(self.task_id, "   "), 1)

    def test_a_refused_rejection_does_not_move_the_task(self):
        self._awaiting()

        with quiet():
            orch.reject_plan(self.task_id, "")

        self.assertEqual(self.read_state()["status"], "AWAITING_APPROVAL")

    def test_rejection_requires_the_approval_gate(self):
        self.write_requirement()
        self.write_plan()
        self.write_state(status="IMPLEMENTING")

        with quiet() as out:
            code = orch.reject_plan(self.task_id, "too late")

        self.assertEqual(code, 1)
        self.assertIn("requires AWAITING_APPROVAL", out.getvalue())

    def test_the_replan_path_picks_it_up(self):
        """Rejection must leave the task somewhere the driver can act on."""
        self._awaiting()

        with quiet():
            orch.reject_plan(self.task_id, "AC-5 unrunnable")

        def fake_run(argv, **kwargs):
            out = argv[argv.index("--output-last-message") + 1]
            orch.Path(out).write_text(orch.json.dumps({}), encoding="utf-8")
            return mock.Mock(returncode=0, stdout="", stderr="")

        with mock.patch.object(orch, "run_codex_replan", return_value=None):
            with quiet():
                orch.run_replan(self.task_id)

        # The replan itself failing is not the point; reaching it is.
        self.assertIn(
            self.read_state()["status"], ("AWAITING_APPROVAL", "FAILED")
        )


class RejectDispatchTests(unittest.TestCase):
    def test_reject_is_reachable_with_a_reason(self):
        argv = ["orchestrator.py", "TASK-001", "reject", "because"]

        with mock.patch.object(orch.sys, "argv", argv):
            with mock.patch.object(orch, "reject_plan", return_value=0) as ran:
                with mock.patch.object(orch, "task_lock"):
                    self.assertEqual(orch.main(), 0)

        ran.assert_called_once_with("TASK-001", "because")

    def test_usage_documents_reject(self):
        self.assertIn("reject", orch.USAGE)

    def test_reject_without_a_reason_is_not_silently_accepted(self):
        """Three-arg form falls through to the action table, which has no
        `reject` -- so it reports an unknown action rather than rejecting with
        an empty reason."""
        self.assertNotIn("reject", orch.ACTIONS)


class InterpreterMatrixGuidanceTests(unittest.TestCase):
    """A criterion cannot reach a second interpreter, so it must not try."""

    def _prompt(self):
        return orch.plan_prompt("TASK-001", "do a thing", replan=False)

    def test_the_prompt_forbids_a_second_interpreter(self):
        prompt = self._prompt()

        self.assertIn("py -3.9", prompt)
        self.assertIn("CI matrix", prompt)

    def test_the_prompt_warns_about_bare_discovery(self):
        """`unittest discover` exits 0 on an empty suite."""
        prompt = self._prompt()

        self.assertIn("Ran 0 tests", prompt)

    def test_the_schema_documents_the_single_interpreter_rule(self):
        schema = json.loads(
            (REPO_ROOT / ".ai" / "schemas" / "plan.schema.json").read_text(
                encoding="utf-8"
            )
        )
        description = schema["$defs"]["acceptanceCriterion"]["properties"][
            "verify"
        ]["description"]

        self.assertIn("only interpreter", description)
        self.assertIn("CI matrix", description)

    def test_the_repo_profile_still_owns_the_empty_suite_rule(self):
        """Criteria cannot express it, so the validation profile must."""
        profile = json.loads(
            (REPO_ROOT / ".ai" / "validation.json").read_text(encoding="utf-8")
        )

        self.assertTrue(
            any(check.get("expect_test_count") for check in profile["checks"])
        )


if __name__ == "__main__":
    unittest.main()
