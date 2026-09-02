"""Fix B2 / audit 2.1 - approval is bound to (plan_version, sha256).

Before this fix, ``approve_plan`` short-circuited on any prior PLAN_APPROVED
event and ``run_implementation`` gated on the same unqualified check. After a
replan bumped plan_version, the stale v1 approval made the v2 plan look
pre-approved, so the developer never saw it.
"""

import unittest
from unittest import mock

from _support import TaskDirCase, load_orchestrator, plan_json, quiet

orch = load_orchestrator()

PLAN_V1 = plan_json(objective="v1")
PLAN_V2 = plan_json(objective="v2 - never shown to the developer")


class ApprovalRecordTests(TaskDirCase):
    def test_approval_records_version_and_hash(self):
        self.write_state()
        plan = self.write_plan("plan.json", PLAN_V1)

        with quiet():
            self.assertEqual(orch.approve_plan(self.task_id), 0)

        approvals = [
            e for e in self.events() if e["event"] == "PLAN_APPROVED"
        ]

        self.assertEqual(len(approvals), 1)
        self.assertEqual(approvals[0]["plan_version"], 1)
        self.assertEqual(approvals[0]["plan_sha256"], orch.sha256_of(plan))
        self.assertIn("plan.json", approvals[0]["plan_file"])

    def test_reapproving_identical_bytes_is_idempotent(self):
        self.write_state()
        self.write_plan("plan.json", PLAN_V1)

        with quiet():
            orch.approve_plan(self.task_id)
            orch.approve_plan(self.task_id)

        approvals = [e for e in self.events() if e["event"] == "PLAN_APPROVED"]

        self.assertEqual(len(approvals), 1)

    def test_editing_the_plan_allows_a_fresh_approval(self):
        self.write_state()
        self.write_plan("plan.json", PLAN_V1)

        with quiet():
            orch.approve_plan(self.task_id)

        self.write_plan("plan.json", plan_json(objective="edited"))

        with quiet():
            orch.approve_plan(self.task_id)

        approvals = [e for e in self.events() if e["event"] == "PLAN_APPROVED"]

        self.assertEqual(len(approvals), 2)
        self.assertNotEqual(
            approvals[0]["plan_sha256"], approvals[1]["plan_sha256"]
        )


class FindApprovalTests(TaskDirCase):
    def test_v1_approval_does_not_satisfy_v2(self):
        """The mandated case: approval must not carry across a version bump."""
        self.write_state(plan_version=1)
        self.write_plan("plan.json", PLAN_V1)

        with quiet():
            orch.approve_plan(self.task_id)

        self.assertIsNotNone(orch.find_approval(self.task_id, 1))
        self.assertIsNone(orch.find_approval(self.task_id, 2))


class ImplementGateTests(TaskDirCase):
    def _implement(self):
        captured = {}

        def fake_run(argv, **kwargs):
            captured["argv"] = argv
            return mock.Mock(returncode=0, stdout="", stderr="")

        with mock.patch.object(
            orch, "current_branch", return_value="feature/TASK-999"
        ), mock.patch.object(orch.subprocess, "run", side_effect=fake_run):
            with quiet() as out:
                code = orch.run_implementation(self.task_id)

        return code, captured, out.getvalue()

    def test_replan_without_reapproval_is_refused(self):
        """v1 approved, then replanned to v2: implement must refuse."""
        self.write_state(plan_version=1)
        self.write_requirement()
        self.write_plan("plan.json", PLAN_V1)

        with quiet():
            orch.approve_plan(self.task_id)

        # Simulate the replan: new version, new canonical plan file.
        _, state = self.write_state(
            plan_version=2,
            plan_file=".ai/tasks/TASK-999/plan-v2.json",
        )
        self.write_plan("plan-v2.json", PLAN_V2)

        code, captured, out = self._implement()

        self.assertEqual(code, 1)
        self.assertNotIn("argv", captured)
        self.assertIn("no PLAN_APPROVED event for plan version 2", out)
        self.assertEqual(self.read_state()["status"], "AWAITING_APPROVAL")

    def test_tampered_plan_is_rejected_at_implement_time(self):
        """The mandated case: edited-after-approval plan must not implement."""
        self.write_state()
        self.write_requirement()
        self.write_plan("plan.json", PLAN_V1)

        with quiet():
            orch.approve_plan(self.task_id)

        # Post-approval tampering window.
        self.write_plan("plan.json", plan_json(objective="tampered"))

        code, captured, out = self._implement()

        self.assertEqual(code, 1)
        self.assertNotIn("argv", captured)
        self.assertIn("has changed since approval", out)
        self.assertEqual(self.read_state()["status"], "AWAITING_APPROVAL")

        failures = [
            e
            for e in self.events()
            if e["event"] == "IMPLEMENTATION_FAILED"
            and e.get("stage") == "approval_verification"
        ]
        self.assertEqual(len(failures), 1)

    def test_untampered_approved_plan_proceeds(self):
        self.write_state()
        self.write_requirement()
        self.write_plan("plan.json", PLAN_V1)

        with quiet():
            orch.approve_plan(self.task_id)

        code, captured, _ = self._implement()

        self.assertEqual(code, 0)
        self.assertIn("argv", captured)
        self.assertEqual(self.read_state()["status"], "VALIDATING")

    def test_legacy_approval_without_hash_fails_closed(self):
        """A TASK-004-era approval carries no hash and cannot be verified."""
        self.write_state()
        self.write_requirement()
        self.write_plan("plan.json", PLAN_V1)

        orch.record_event(
            self.task_id,
            "PLAN_APPROVED",
            plan_version=1,
            approved_by="developer",
        )

        code, captured, out = self._implement()

        self.assertEqual(code, 1)
        self.assertNotIn("argv", captured)
        self.assertIn("predates plan hashing", out)


if __name__ == "__main__":
    unittest.main()
