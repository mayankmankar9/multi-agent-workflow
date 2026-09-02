"""Phase 1 - task creation (C8) and the `run` driver (audit 5.2).

Two gaps this closes. Task creation was entirely manual, so ``NEW`` was
unreachable by any code path and ``ANALYZING`` had no writer -- the audit calls
them decorative. And nine mechanical steps were driven by hand; the driver
collapses them into one idempotent command that stops only at a genuine human
decision.
"""

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

PLAN_BODY = plan_json()


class AllocateTaskIdTests(TaskDirCase):
    def _clear_tasks(self):
        """Drop the fixture's own task dir so allocation starts from empty."""
        import shutil

        for child in (self.tmp / ".ai" / "tasks").iterdir():
            if child.is_dir():
                shutil.rmtree(child)

    def test_first_task_when_none_exist(self):
        self._clear_tasks()

        self.assertEqual(orch.allocate_task_id(), "TASK-001")

    def test_next_after_highest_existing(self):
        self._clear_tasks()
        (self.tmp / ".ai" / "tasks" / "TASK-004").mkdir()
        (self.tmp / ".ai" / "tasks" / "TASK-011").mkdir()

        self.assertEqual(orch.allocate_task_id(), "TASK-012")

    def test_uses_the_highest_not_the_count(self):
        self._clear_tasks()
        (self.tmp / ".ai" / "tasks" / "TASK-002").mkdir()
        (self.tmp / ".ai" / "tasks" / "TASK-050").mkdir()

        self.assertEqual(orch.allocate_task_id(), "TASK-051")

    def test_ignores_non_task_directories(self):
        self._clear_tasks()
        (self.tmp / ".ai" / "tasks" / "scratch").mkdir()

        self.assertEqual(orch.allocate_task_id(), "TASK-001")

    def test_ids_past_999_do_not_collide(self):
        self._clear_tasks()
        (self.tmp / ".ai" / "tasks" / "TASK-999").mkdir()

        self.assertEqual(orch.allocate_task_id(), "TASK-1000")


class CreateTaskTests(TaskDirCase):
    def test_creates_workspace_at_new(self):
        task_id = orch.create_task("Add a widget endpoint.", "TASK-100")
        directory = self.tmp / ".ai" / "tasks" / task_id

        self.assertEqual(task_id, "TASK-100")
        self.assertTrue((directory / "requirement.md").is_file())
        self.assertIn(
            "Add a widget endpoint.", (directory / "requirement.md").read_text()
        )

        import json

        state = json.loads((directory / "state.json").read_text())
        self.assertEqual(state["status"], "NEW")
        self.assertEqual(state["plan_version"], 1)
        self.assertIsNone(state["plan_file"])

    def test_records_a_creation_event(self):
        orch.create_task("Do a thing.", "TASK-101")

        events_file = self.tmp / ".ai" / "tasks" / "TASK-101" / "events.jsonl"
        self.assertIn("TASK_CREATED", events_file.read_text())

    def test_refuses_empty_requirement(self):
        with self.assertRaises(RuntimeError):
            orch.create_task("   \n  ", "TASK-102")

    def test_refuses_to_clobber_an_existing_task(self):
        orch.create_task("First.", "TASK-103")

        with self.assertRaises(RuntimeError) as ctx:
            orch.create_task("Second.", "TASK-103")

        self.assertIn("already exists", str(ctx.exception))

    def test_refuses_a_malformed_task_id(self):
        with self.assertRaises(RuntimeError):
            orch.create_task("Thing.", "NOT-A-TASK")

    def test_new_state_is_schema_shaped(self):
        """Keys must stay within the schema, which forbids extra properties."""
        import json

        from _support import REPO_ROOT

        orch.create_task("Thing.", "TASK-104")
        state = json.loads(
            (self.tmp / ".ai" / "tasks" / "TASK-104" / "state.json").read_text()
        )

        # The real schema in the repo, not the temp tree.
        schema = json.loads(
            (REPO_ROOT / ".ai" / "schemas" / "task-state.schema.json").read_text()
        )

        self.assertEqual(set(state) - set(schema["properties"]), set())
        for key in schema["required"]:
            self.assertIn(key, state)


class AdvanceBookkeepingTests(TaskDirCase):
    def test_new_advances_to_analyzing(self):
        self.write_state(status="NEW")

        with quiet():
            self.assertEqual(orch.advance_bookkeeping_state(self.task_id), 0)

        self.assertEqual(self.read_state()["status"], "ANALYZING")

    def test_analyzing_advances_to_planning(self):
        self.write_state(status="ANALYZING")

        with quiet():
            self.assertEqual(orch.advance_bookkeeping_state(self.task_id), 0)

        self.assertEqual(self.read_state()["status"], "PLANNING")

    def test_records_the_transition(self):
        self.write_state(status="NEW")

        with quiet():
            orch.advance_bookkeeping_state(self.task_id)

        advanced = [e for e in self.events() if e["event"] == "STATE_ADVANCED"]
        self.assertEqual(advanced[-1]["from_state"], "NEW")
        self.assertEqual(advanced[-1]["to_state"], "ANALYZING")

    def test_refuses_from_other_states(self):
        self.write_state(status="VALIDATING")

        with quiet() as out:
            self.assertEqual(orch.advance_bookkeeping_state(self.task_id), 1)

        self.assertIn("advances NEW or ANALYZING only", out.getvalue())


class RunDriverGateTests(TaskDirCase):
    """The driver must stop at exactly the three human decisions."""

    def test_stops_at_unapproved_plan(self):
        self.write_state(status="AWAITING_APPROVAL")
        self.write_plan("plan.json", PLAN_BODY)

        with quiet() as out:
            self.assertEqual(orch.run_driver(self.task_id), 0)

        self.assertIn("needs a developer decision", out.getvalue())
        self.assertEqual(self.read_state()["status"], "AWAITING_APPROVAL")

    def test_does_not_treat_a_stale_approval_as_a_green_light(self):
        """The driver must honour the hash binding, not just the event."""
        self.write_state(status="AWAITING_APPROVAL")
        self.write_plan("plan.json", PLAN_BODY)

        with quiet():
            orch.approve_plan(self.task_id)

        self.write_plan("plan.json", plan_json(objective="tampered"))

        with quiet() as out:
            self.assertEqual(orch.run_driver(self.task_id), 0)

        self.assertIn("needs a developer decision", out.getvalue())

    def test_stops_at_pr_ready(self):
        self.write_state(status="PR_READY")

        with quiet() as out:
            self.assertEqual(orch.run_driver(self.task_id), 0)

        self.assertIn("Complete with", out.getvalue())
        self.assertEqual(self.read_state()["status"], "PR_READY")

    def test_completed_is_terminal(self):
        self.write_state(status="COMPLETED")

        with quiet() as out:
            self.assertEqual(orch.run_driver(self.task_id), 0)

        self.assertIn("is COMPLETED", out.getvalue())


class RunDriverProgressTests(TaskDirCase):
    def test_walks_new_through_to_the_approval_gate(self):
        """NEW -> ANALYZING -> PLANNING -> plan -> stop for the developer."""
        self.write_state(status="NEW")
        self.write_requirement()

        def fake_run(argv, **kwargs):
            if worker_name(argv) == "codex":
                # Stand in for codex: write the plan it was asked for.
                out = argv[argv.index("--output-last-message") + 1]
                orch.Path(out).write_text(PLAN_BODY)
                return mock.Mock(returncode=0, stdout="", stderr="")

            if worker_name(argv) == "claude":
                # The plan critic. Approve the plan.
                return mock.Mock(
                    returncode=0,
                    stdout='{"verdict": "pass", "issues": []}',
                    stderr="",
                )

            return mock.Mock(returncode=0, stdout="", stderr="")

        with mock.patch.object(orch.subprocess, "run", side_effect=fake_run):
            with quiet() as out:
                self.assertEqual(orch.run_driver(self.task_id), 0)

        self.assertEqual(self.read_state()["status"], "AWAITING_APPROVAL")
        self.assertIn("needs a developer decision", out.getvalue())

        states = [
            (e.get("from_state"), e.get("to_state"))
            for e in self.events()
            if e["event"] == "STATE_ADVANCED"
        ]
        self.assertIn(("NEW", "ANALYZING"), states)
        self.assertIn(("ANALYZING", "PLANNING"), states)

    def test_drives_approved_plan_through_to_pr_ready(self):
        """Implement, validate and review with no developer commands."""
        self.write_state(status="AWAITING_APPROVAL")
        self.write_requirement()
        self.write_plan("plan.json", PLAN_BODY)

        with quiet():
            orch.approve_plan(self.task_id)

        passing = "\nRan 5 tests in 0.01s\n\nOK\n"

        def fake_run(argv, **kwargs):
            if worker_name(argv) == "claude":
                return mock.Mock(returncode=0, stdout="", stderr="")

            if "unittest" in argv:
                return mock.Mock(returncode=0, stdout="", stderr=passing)

            # review-package.py
            return mock.Mock(returncode=0, stdout="", stderr="")

        with mock.patch.object(
            orch, "current_branch", return_value="feature/TASK-999"
        ), mock.patch.object(orch.subprocess, "run", side_effect=fake_run):
            with quiet() as out:
                self.assertEqual(orch.run_driver(self.task_id), 0)

        self.assertEqual(self.read_state()["status"], "PR_READY")
        self.assertIn("Complete with", out.getvalue())

    def test_routes_a_failure_and_retries_the_fix(self):
        self.write_state(status="AWAITING_APPROVAL")
        self.write_requirement()
        self.write_plan("plan.json", PLAN_BODY)

        with quiet():
            orch.approve_plan(self.task_id)

        failing = "\nRan 5 tests in 0.01s\n\nFAILED (failures=1)\n"
        calls = {"unittest": 0}

        def fake_run(argv, **kwargs):
            if worker_name(argv) == "claude":
                return mock.Mock(returncode=0, stdout="", stderr="")

            if "unittest" in argv:
                calls["unittest"] += 1
                return mock.Mock(returncode=1, stdout="", stderr=failing)

            return mock.Mock(returncode=0, stdout="", stderr="")

        with mock.patch.object(
            orch, "current_branch", return_value="feature/TASK-999"
        ), mock.patch.object(orch.subprocess, "run", side_effect=fake_run):
            with quiet() as out:
                code = orch.run_driver(self.task_id)

        # Bounded: it gives up rather than looping forever.
        self.assertEqual(code, 1)
        self.assertIn("fix", out.getvalue().lower())
        self.assertEqual(
            orch.count_fix_attempts(self.task_id), orch.RUN_MAX_FIX_ATTEMPTS
        )

    def test_is_idempotent_at_a_gate(self):
        self.write_state(status="PR_READY")

        with quiet():
            first = orch.run_driver(self.task_id)
            second = orch.run_driver(self.task_id)

        self.assertEqual(first, 0)
        self.assertEqual(second, 0)
        self.assertEqual(self.read_state()["status"], "PR_READY")

    def test_refuses_implementing_without_a_routed_failure(self):
        """An interrupted run must not be silently resumed as a fix."""
        self.write_state(status="IMPLEMENTING")

        with quiet() as out:
            self.assertEqual(orch.run_driver(self.task_id), 1)

        self.assertIn("no routed failure", out.getvalue())

    def test_step_budget_is_bounded(self):
        self.assertGreater(orch.RUN_MAX_STEPS, 0)
        self.assertGreater(orch.RUN_MAX_FIX_ATTEMPTS, 0)


class DispatchTests(unittest.TestCase):
    def test_run_and_advance_are_dispatchable(self):
        self.assertIn("run", orch.ACTIONS)
        self.assertIn("advance", orch.ACTIONS)

    def test_usage_documents_new_and_run(self):
        self.assertIn("new", orch.USAGE)
        self.assertIn("run", orch.USAGE)


if __name__ == "__main__":
    unittest.main()
