"""The approved plan authorises the worker's writes.

This repository's only source is its own workflow infrastructure, so a blanket
denial of `.ai/scripts/` and `.ai/hooks/` to workers meant no worker could ever
implement anything here -- the guard was airtight and the pipeline was inert.

Approval is already hash-bound to a specific plan, and that plan already has to
declare every file the change will touch. These tests pin the consequence: the
declaration is enforced at the moment of the write, not only afterwards by
diff-scope validation, and the binding is to the *bytes* the developer approved.

Evidence stays absolutely denied. A plan that declared `state.json` would be a
plan asking the worker to write its own verdict, and no approval makes that
legitimate.
"""

import hashlib
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

from _support import TaskDirCase, plan_json

HOOKS = Path(__file__).resolve().parent / ".ai" / "hooks"

DENY = 2
ALLOW = 0


class PlanAuthorizationCase(TaskDirCase):
    """Runs the guard as a worker, against a repo-shaped temp tree."""

    def run_guard(self, payload, task_id=None):
        environment = dict(os.environ)
        environment["ORCHESTRATOR_TASK_ID"] = task_id or self.task_id

        return subprocess.run(
            [sys.executable, str(HOOKS / "pre_tool_use.py")],
            input=json.dumps(payload),
            cwd=str(self.tmp),
            capture_output=True,
            text=True,
            env=environment,
        )

    def write(self, path):
        return self.run_guard(
            {"tool_name": "Edit", "tool_input": {"file_path": path}}
        )

    def approve(self, modify=(), create=(), plan_version=1):
        """Write a plan and the PLAN_APPROVED event that binds to its bytes."""
        body = plan_json(
            files_to_modify=[{"path": p, "purpose": "x"} for p in modify],
            files_to_create=[{"path": p, "purpose": "x"} for p in create],
        )
        plan_path = self.task_path / "plan.json"
        plan_path.write_text(body, encoding="utf-8")

        self.write_state(
            status="IMPLEMENTING",
            plan_version=plan_version,
            plan_file=str(plan_path),
        )

        digest = hashlib.sha256(plan_path.read_bytes()).hexdigest()
        (self.task_path / "events.jsonl").write_text(
            json.dumps(
                {
                    "event": "PLAN_APPROVED",
                    "plan_version": plan_version,
                    "plan_sha256": digest,
                }
            )
            + "\n",
            encoding="utf-8",
        )

        return plan_path


class DeclaredInfrastructureTests(PlanAuthorizationCase):
    def test_declared_script_is_allowed(self):
        self.approve(modify=[".ai/scripts/orchestrator.py"])

        self.assertEqual(self.write(".ai/scripts/orchestrator.py").returncode, ALLOW)

    def test_undeclared_script_is_denied(self):
        self.approve(modify=[".ai/scripts/review-package.py"])

        result = self.write(".ai/scripts/orchestrator.py")

        self.assertEqual(result.returncode, DENY)
        self.assertIn("does not declare it", result.stderr)

    def test_declared_hook_is_allowed(self):
        self.approve(modify=[".ai/hooks/session_start.py"])

        self.assertEqual(self.write(".ai/hooks/session_start.py").returncode, ALLOW)

    def test_declared_file_to_create_is_allowed(self):
        self.approve(create=[".ai/schemas/new.schema.json"])

        self.assertEqual(
            self.write(".ai/schemas/new.schema.json").returncode, ALLOW
        )

    def test_ordinary_source_needs_no_declaration(self):
        """The guard covers infrastructure; diff-scope covers everything else."""
        self.approve(modify=[])

        self.assertEqual(self.write("widget.py").returncode, ALLOW)

    def test_an_absolute_path_to_a_declared_file_is_allowed(self):
        self.approve(modify=[".ai/scripts/orchestrator.py"])
        target = self.tmp / ".ai" / "scripts" / "orchestrator.py"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("x", encoding="utf-8")

        self.assertEqual(self.write(str(target)).returncode, ALLOW)


class EvidenceIsNeverAuthorizedTests(PlanAuthorizationCase):
    def test_a_plan_cannot_authorise_writing_state(self):
        self.approve(modify=[".ai/tasks/%s/state.json" % self.task_id])

        result = self.write(".ai/tasks/%s/state.json" % self.task_id)

        self.assertEqual(result.returncode, DENY)
        self.assertIn("No plan can authorise this", result.stderr)

    def test_a_plan_cannot_authorise_writing_test_results(self):
        self.approve(modify=[".ai/tasks/%s/test-results.json" % self.task_id])

        self.assertEqual(
            self.write(".ai/tasks/%s/test-results.json" % self.task_id).returncode,
            DENY,
        )

    def test_a_plan_cannot_authorise_writing_review_findings(self):
        self.approve(modify=[".ai/tasks/%s/review-findings.json" % self.task_id])

        self.assertEqual(
            self.write(
                ".ai/tasks/%s/review-findings.json" % self.task_id
            ).returncode,
            DENY,
        )

    def test_implementation_notes_stay_writable(self):
        """The worker is required to write these."""
        self.approve()

        self.assertEqual(
            self.write(".ai/tasks/%s/implementation.md" % self.task_id).returncode,
            ALLOW,
        )

    def test_the_blackboard_stays_writable(self):
        self.approve()

        self.assertEqual(
            self.write(".ai/tasks/%s/context.jsonl" % self.task_id).returncode,
            ALLOW,
        )


class ApprovalBindingTests(PlanAuthorizationCase):
    def test_a_plan_edited_after_approval_authorises_nothing(self):
        """Approval covers bytes, and these are not those bytes."""
        plan_path = self.approve(modify=[".ai/scripts/orchestrator.py"])
        plan_path.write_text(
            plan_json(
                files_to_modify=[
                    {"path": ".ai/scripts/orchestrator.py", "purpose": "y"}
                ]
            ),
            encoding="utf-8",
        )

        self.assertEqual(
            self.write(".ai/scripts/orchestrator.py").returncode, DENY
        )

    def test_an_approval_for_another_version_does_not_carry_forward(self):
        self.approve(modify=[".ai/scripts/orchestrator.py"], plan_version=1)
        state_path = self.task_path / "state.json"
        state = json.loads(state_path.read_text())
        state["plan_version"] = 2
        state_path.write_text(json.dumps(state), encoding="utf-8")

        self.assertEqual(
            self.write(".ai/scripts/orchestrator.py").returncode, DENY
        )

    def test_no_approval_authorises_nothing(self):
        self.write_plan()
        self.write_state(status="IMPLEMENTING")

        self.assertEqual(
            self.write(".ai/scripts/orchestrator.py").returncode, DENY
        )

    def test_a_missing_plan_authorises_nothing(self):
        self.write_state(status="IMPLEMENTING")

        self.assertEqual(
            self.write(".ai/scripts/orchestrator.py").returncode, DENY
        )

    def test_an_unparseable_plan_authorises_nothing(self):
        """Fail closed: a plan nothing can read declares nothing."""
        plan_path = self.task_path / "plan.json"
        plan_path.write_text("{not json", encoding="utf-8")
        self.write_state(status="IMPLEMENTING", plan_file=str(plan_path))

        digest = hashlib.sha256(plan_path.read_bytes()).hexdigest()
        (self.task_path / "events.jsonl").write_text(
            json.dumps(
                {
                    "event": "PLAN_APPROVED",
                    "plan_version": 1,
                    "plan_sha256": digest,
                }
            )
            + "\n",
            encoding="utf-8",
        )

        self.assertEqual(
            self.write(".ai/scripts/orchestrator.py").returncode, DENY
        )


class ShellRouteStaysClosedTests(PlanAuthorizationCase):
    def _bash(self, command):
        return self.run_guard(
            {"tool_name": "Bash", "tool_input": {"command": command}}
        )

    def test_redirection_into_a_declared_file_is_still_denied(self):
        """Declared means editable, never editable through the shell."""
        self.approve(modify=[".ai/scripts/orchestrator.py"])

        result = self._bash("echo x > .ai/scripts/orchestrator.py")

        self.assertEqual(result.returncode, DENY)
        self.assertIn("redirection", result.stderr)

    def test_redirection_into_evidence_is_denied(self):
        self.approve()

        self.assertEqual(
            self._bash(
                "echo x > .ai/tasks/%s/state.json" % self.task_id
            ).returncode,
            DENY,
        )

    def test_git_commit_is_still_denied_for_a_worker(self):
        self.approve()

        self.assertEqual(self._bash("git commit -m x").returncode, DENY)


if __name__ == "__main__":
    unittest.main()
