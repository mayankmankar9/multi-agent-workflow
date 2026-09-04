"""Fix B1 / audit 2.2 - the canonical plan pointer.

Before this fix, ``run_codex_replan`` wrote ``plan-vN.yaml`` while the
implementer and the approval gate both hardcoded ``plan.yaml``, so a
replanned contract was written to disk and never read by anything.
"""

import unittest
from unittest import mock

from _support import TaskDirCase, load_orchestrator, plan_json, quiet

orch = load_orchestrator()


class ResolvePlanFileTests(TaskDirCase):
    def test_recorded_plan_file_wins(self):
        _, state = self.write_state(
            plan_version=2,
            plan_file=".ai/tasks/TASK-999/plan-v2.json",
        )

        resolved = orch.resolve_plan_file(self.task_id, state)

        self.assertEqual(resolved.name, "plan-v2.json")

    def test_absent_plan_file_falls_back_to_plan_yaml(self):
        """A TASK-004-shaped state has no plan_file key and must stay readable."""
        _, state = self.write_state()
        state.pop("plan_file", None)

        resolved = orch.resolve_plan_file(self.task_id, state)

        self.assertEqual(resolved.name, "plan.yaml")
        self.assertEqual(
            resolved.resolve(), (self.task_path / "plan.yaml").resolve()
        )

    def test_null_plan_file_falls_back(self):
        _, state = self.write_state(plan_file=None)

        self.assertEqual(
            orch.resolve_plan_file(self.task_id, state).name, "plan.yaml"
        )


class ImplementerReadsCurrentPlanTests(TaskDirCase):
    """The mandated case: after a replan the implementer reads the NEW plan."""

    def test_implementer_receives_replanned_plan_not_stale_plan_yaml(self):
        self.write_state(
            plan_version=2,
            plan_file=".ai/tasks/TASK-999/plan-v2.json",
        )
        self.write_requirement()
        self.write_plan("plan.yaml", "objective: \"STALE v1 - must not be used\"\n")
        self.write_plan("plan-v2.json", plan_json(objective="fresh v2"))

        # Approve through the real gate so this test stays honest once
        # approval becomes hash-bound.
        with quiet():
            self.assertEqual(orch.approve_plan(self.task_id), 0)

        captured = {}

        def fake_run(argv, **kwargs):
            captured["argv"] = argv
            # The implementer's prompt arrives on stdin, not as an argument.
            captured["prompt"] = kwargs.get("input")
            return mock.Mock(returncode=0, stdout="", stderr="")

        with mock.patch.object(orch, "current_branch", return_value="feature/TASK-999"), \
                mock.patch.object(orch.subprocess, "run", side_effect=fake_run):
            with quiet():
                self.assertEqual(orch.run_implementation(self.task_id), 0)

        prompt = captured["prompt"]

        self.assertIn("plan-v2.json", prompt)
        self.assertNotIn("/plan.yaml", prompt)


if __name__ == "__main__":
    unittest.main()
