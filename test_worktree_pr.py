"""Phase 1 - worktree per task, PR wiring, and the CI gate on completion.

``worktree`` was in the state schema and used nowhere. PR creation and merge
were entirely manual, and ``complete`` would mark a task COMPLETED without
consulting CI at all.
"""

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from _support import TaskDirCase, load_orchestrator, quiet

orch = load_orchestrator()

HAVE_GIT = shutil.which("git") is not None


@unittest.skipUnless(HAVE_GIT, "git not available")
class WorktreeTests(unittest.TestCase):
    task_id = "TASK-555"

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="wf-wt-"))
        self.addCleanup(self._cleanup)

        def git(*args):
            subprocess.run(
                ["git"] + list(args),
                cwd=str(self.tmp),
                capture_output=True,
                check=True,
            )

        git("init", "-q", "-b", "main")
        git("config", "user.email", "t@example.com")
        git("config", "user.name", "T")
        (self.tmp / "base.txt").write_text("base\n")
        git("add", "-A")
        git("commit", "-qm", "base")

        self.task_path = self.tmp / ".ai" / "tasks" / self.task_id
        self.task_path.mkdir(parents=True)
        (self.task_path / "state.json").write_text(
            json.dumps(
                {
                    "task_id": self.task_id,
                    "status": "AWAITING_APPROVAL",
                    "created_at": "2026-09-01T10:00:00+05:30",
                    "updated_at": "2026-09-01T10:00:00+05:30",
                    "plan_version": 1,
                    "branch": "feature/TASK-555",
                    "worktree": None,
                    "failure_reason": None,
                }
            )
        )

        self._prev = Path.cwd()
        os.chdir(self.tmp)

    def _cleanup(self):
        os.chdir(str(self._prev))
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _state(self):
        return json.loads((self.task_path / "state.json").read_text())

    def test_creates_a_worktree_and_records_it(self):
        with quiet() as out:
            self.assertEqual(orch.run_worktree(self.task_id), 0)

        target = self.tmp / ".worktrees" / self.task_id

        self.assertTrue(target.is_dir())
        self.assertTrue((target / "base.txt").is_file())
        self.assertEqual(self._state()["worktree"], str(Path(".worktrees") / self.task_id))
        self.assertIn("Worktree for", out.getvalue())

    def test_worktree_is_on_the_task_branch(self):
        with quiet():
            orch.run_worktree(self.task_id)

        result = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=str(self.tmp / ".worktrees" / self.task_id),
            capture_output=True,
            text=True,
        )

        self.assertEqual(result.stdout.strip(), "feature/TASK-555")

    def test_is_idempotent(self):
        with quiet():
            first = orch.run_worktree(self.task_id)
            second = orch.run_worktree(self.task_id)

        self.assertEqual(first, 0)
        self.assertEqual(second, 0)

    def test_records_a_worktree_event(self):
        with quiet():
            orch.run_worktree(self.task_id)

        events = (self.task_path / "events.jsonl").read_text()

        self.assertIn("WORKTREE_READY", events)

    def test_reuses_an_existing_branch(self):
        subprocess.run(
            ["git", "branch", "feature/TASK-555"],
            cwd=str(self.tmp),
            capture_output=True,
            check=True,
        )

        with quiet():
            self.assertEqual(orch.run_worktree(self.task_id), 0)


class CiStatusTests(unittest.TestCase):
    def test_unknown_when_gh_is_absent(self):
        with mock.patch.object(orch, "gh_available", return_value=False):
            status, detail = orch.ci_status("feature/x")

        self.assertEqual(status, "unknown")
        self.assertIn("gh", detail)

    def _with_gh(self, stdout, returncode=0):
        def fake_run(argv, **kwargs):
            return mock.Mock(returncode=returncode, stdout=stdout, stderr="")

        return mock.patch.object(
            orch.subprocess, "run", side_effect=fake_run
        ), mock.patch.object(orch, "gh_available", return_value=True)

    def test_success_when_run_concluded_success(self):
        run_patch, gh_patch = self._with_gh(
            json.dumps([{"status": "completed", "conclusion": "success"}])
        )

        with run_patch, gh_patch:
            status, _ = orch.ci_status("feature/x")

        self.assertEqual(status, "success")

    def test_failure_when_run_concluded_failure(self):
        run_patch, gh_patch = self._with_gh(
            json.dumps([{"status": "completed", "conclusion": "failure"}])
        )

        with run_patch, gh_patch:
            status, _ = orch.ci_status("feature/x")

        self.assertEqual(status, "failure")

    def test_pending_while_still_running(self):
        run_patch, gh_patch = self._with_gh(
            json.dumps([{"status": "in_progress", "conclusion": None}])
        )

        with run_patch, gh_patch:
            status, _ = orch.ci_status("feature/x")

        self.assertEqual(status, "pending")

    def test_unknown_when_no_runs_exist(self):
        run_patch, gh_patch = self._with_gh("[]")

        with run_patch, gh_patch:
            status, detail = orch.ci_status("feature/x")

        self.assertEqual(status, "unknown")
        self.assertIn("no CI runs", detail)

    def test_unknown_when_output_is_unparseable(self):
        run_patch, gh_patch = self._with_gh("not json")

        with run_patch, gh_patch:
            status, _ = orch.ci_status("feature/x")

        self.assertEqual(status, "unknown")


class CompleteGateTests(TaskDirCase):
    def test_failing_ci_blocks_completion(self):
        self.write_state(status="PR_READY")

        with mock.patch.object(
            orch, "ci_status", return_value=("failure", "CI concluded failure")
        ):
            with quiet() as out:
                code = orch.complete_task(self.task_id)

        self.assertEqual(code, 1)
        self.assertEqual(self.read_state()["status"], "PR_READY")
        self.assertIn("CI is failure", out.getvalue())

        blocked = [e for e in self.events() if e["event"] == "MERGE_BLOCKED"]
        self.assertEqual(len(blocked), 1)

    def test_pending_ci_blocks_completion(self):
        self.write_state(status="PR_READY")

        with mock.patch.object(
            orch, "ci_status", return_value=("pending", "still running")
        ):
            with quiet():
                code = orch.complete_task(self.task_id)

        self.assertEqual(code, 1)
        self.assertEqual(self.read_state()["status"], "PR_READY")

    def test_green_ci_allows_completion(self):
        self.write_state(status="PR_READY")

        with mock.patch.object(
            orch, "ci_status", return_value=("success", "CI concluded success")
        ):
            with quiet():
                code = orch.complete_task(self.task_id)

        self.assertEqual(code, 0)
        self.assertEqual(self.read_state()["status"], "COMPLETED")

        approved = [e for e in self.events() if e["event"] == "MERGE_APPROVED"]
        self.assertEqual(approved[0]["ci_status"], "success")

    def test_unknown_ci_warns_and_records_but_does_not_block(self):
        """Unverified is never reported as success, but must not brick local use."""
        self.write_state(status="PR_READY")

        with mock.patch.object(
            orch, "ci_status", return_value=("unknown", "gh is not installed")
        ):
            with quiet() as out:
                code = orch.complete_task(self.task_id)

        self.assertEqual(code, 0)
        self.assertIn("could not be verified", out.getvalue())

        approved = [e for e in self.events() if e["event"] == "MERGE_APPROVED"]
        self.assertEqual(approved[0]["ci_status"], "unknown")


class PrVerbTests(TaskDirCase):
    def setUp(self):
        super().setUp()
        self._prev_env = os.environ.get("ORCHESTRATOR_ENABLE_PR")
        os.environ.pop("ORCHESTRATOR_ENABLE_PR", None)
        self.addCleanup(self._restore_env)

    def _restore_env(self):
        if self._prev_env is None:
            os.environ.pop("ORCHESTRATOR_ENABLE_PR", None)
        else:
            os.environ["ORCHESTRATOR_ENABLE_PR"] = self._prev_env

    def test_disabled_by_default_because_it_pushes(self):
        self.write_state(status="PR_READY")

        with quiet() as out:
            code = orch.run_pr(self.task_id)

        self.assertEqual(code, 1)
        self.assertIn("disabled", out.getvalue())
        self.assertIn("pushes the branch", out.getvalue())

    def test_requires_pr_ready_even_when_enabled(self):
        os.environ["ORCHESTRATOR_ENABLE_PR"] = "1"
        self.write_state(status="IMPLEMENTING")

        with quiet() as out:
            code = orch.run_pr(self.task_id)

        self.assertEqual(code, 1)
        self.assertIn("requires PR_READY", out.getvalue())

    def test_creates_a_draft_pr_when_enabled(self):
        os.environ["ORCHESTRATOR_ENABLE_PR"] = "1"
        self.write_state(status="PR_READY")
        (self.task_path / "review-summary.md").write_text("# Review\n")

        captured = {}

        def fake_run(argv, **kwargs):
            captured["argv"] = argv
            return mock.Mock(
                returncode=0, stdout="https://example.test/pr/1\n", stderr=""
            )

        with mock.patch.object(orch, "gh_available", return_value=True), \
                mock.patch.object(orch.subprocess, "run", side_effect=fake_run):
            with quiet() as out:
                code = orch.run_pr(self.task_id)

        self.assertEqual(code, 0)
        self.assertIn("--draft", captured["argv"])
        self.assertIn("--body-file", captured["argv"])
        self.assertIn("https://example.test/pr/1", out.getvalue())

        opened = [e for e in self.events() if e["event"] == "PR_OPENED"]
        self.assertEqual(len(opened), 1)


class DispatchTests(unittest.TestCase):
    def test_worktree_and_pr_are_dispatchable(self):
        self.assertIn("worktree", orch.ACTIONS)
        self.assertIn("pr", orch.ACTIONS)

    def test_usage_documents_the_opt_in(self):
        self.assertIn("ORCHESTRATOR_ENABLE_PR", orch.USAGE)


if __name__ == "__main__":
    unittest.main()
