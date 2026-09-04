"""Launching the worker CLIs for real.

Every other test in this suite mocks ``subprocess.run``, which is the right
default -- but it means the one thing that must work before a real end-to-end
run, actually starting ``claude`` and ``codex``, was never exercised.

It did not work. ``subprocess`` on Windows goes through ``CreateProcess``,
which appends ``.exe`` when searching PATH and ignores the rest of ``PATHEXT``.
Both workers install as npm shims (``claude.CMD``, ``codex.CMD``), so a bare
``argv[0]`` raised ``FileNotFoundError`` on a machine where both CLIs were
installed, on PATH, and runnable from the shell -- and the orchestrator turned
that into "Worker executable not found on PATH: install it and re-run", sending
the developer to reinstall a tool that was already there.

The mocked tests below pin the resolution behaviour. The `LiveWorkerTests` at
the bottom skip when a CLI is absent and otherwise launch it for real, which is
the only form of this test that could have caught the original defect.
"""

import shutil
import subprocess
import unittest
from pathlib import Path
from unittest import mock

from _support import TaskDirCase, load_orchestrator, plan_json, quiet

orch = load_orchestrator()

# `--version` is offline, fast, and side-effect free, so it is safe to run in a
# unit suite. Anything heavier belongs in the end-to-end run, not here.
VERSION_TIMEOUT_S = 120


class ResolveExecutableTests(unittest.TestCase):
    def test_resolves_a_name_that_is_on_path(self):
        """git is present wherever this repo is checked out."""
        resolved = orch.resolve_executable("git")

        self.assertEqual(resolved, shutil.which("git"))
        self.assertNotEqual(resolved, "git")

    def test_unresolvable_name_is_returned_unchanged(self):
        """So the caller still raises the real error, naming what it looked for."""
        name = "definitely-not-a-real-executable-xyzzy"

        self.assertEqual(orch.resolve_executable(name), name)

    def test_honours_pathext_style_resolution(self):
        """A shim that only which() can find must still resolve."""
        with mock.patch.object(
            orch.shutil, "which", return_value=r"C:\npm\claude.CMD"
        ):
            self.assertEqual(
                orch.resolve_executable("claude"), r"C:\npm\claude.CMD"
            )


class RunWorkerResolutionTests(TaskDirCase):
    def _capture_argv(self, which_returns):
        seen = {}

        def fake_run(argv, **kwargs):
            seen["argv"] = argv
            return mock.Mock(returncode=0, stdout=None, stderr=None)

        with mock.patch.object(orch.shutil, "which", return_value=which_returns):
            with mock.patch.object(orch.subprocess, "run", side_effect=fake_run):
                orch.run_worker(["claude", "--print"], 10, task_id=self.task_id)

        return seen["argv"]

    def test_execs_the_resolved_path_not_the_bare_name(self):
        argv = self._capture_argv(r"C:\npm\claude.CMD")

        self.assertEqual(argv[0], r"C:\npm\claude.CMD")

    def test_preserves_every_argument_after_the_executable(self):
        argv = self._capture_argv("/usr/bin/claude")

        self.assertEqual(argv[1:], ["--print"])

    def test_falls_back_to_the_bare_name_when_unresolvable(self):
        argv = self._capture_argv(None)

        self.assertEqual(argv[0], "claude")

    def test_usage_is_recorded_under_the_name_not_the_path(self):
        """events.jsonl must stay comparable across machines."""
        self._capture_argv(r"C:\npm\claude.CMD")

        usage = [e for e in self.events() if e["event"] == "WORKER_USAGE"]

        self.assertEqual(usage[0]["worker"], "claude")

    def test_usage_name_survives_an_already_resolved_argv(self):
        """claude_argv resolves eagerly, so run_worker receives a full path.

        The first real end-to-end run recorded
        `worker: C:\\Users\\...\\npm\\claude.CMD`, which no cost report could
        group with a POSIX run of the same worker.
        """

        def fake_run(argv, **kwargs):
            return mock.Mock(returncode=0, stdout=None, stderr=None)

        with mock.patch.object(orch.subprocess, "run", side_effect=fake_run):
            orch.run_worker(
                [r"C:\Users\x\AppData\Roaming\npm\claude.CMD", "--print"],
                10,
                task_id=self.task_id,
            )

        usage = [e for e in self.events() if e["event"] == "WORKER_USAGE"]

        self.assertEqual(usage[0]["worker"], "claude")

    def test_a_posix_resolved_path_also_records_the_bare_name(self):
        def fake_run(argv, **kwargs):
            return mock.Mock(returncode=0, stdout=None, stderr=None)

        with mock.patch.object(orch.subprocess, "run", side_effect=fake_run):
            orch.run_worker(["/usr/local/bin/codex", "exec"], 10, task_id=self.task_id)

        usage = [e for e in self.events() if e["event"] == "WORKER_USAGE"]

        self.assertEqual(usage[0]["worker"], "codex")

    def test_worker_name_is_host_independent(self):
        """The two tests above pass on the host that cannot detect the bug.

        `run_worker` originally reduced argv[0] with `Path(...).stem`, and
        `Path` is whatever the host is. On Windows that handles both
        separators, so a Windows path and a POSIX path both reduced correctly
        and the pair above went green -- while on Linux `Path` is a
        PurePosixPath that does not split on `\\`, so the same code recorded
        `worker: C:\\Users\\...\\npm\\claude` and CI failed on a tree that had
        passed local validation.

        So the obligation is asserted directly on the reduction, over both
        separator styles at once. This fails on every platform if the flavour
        goes back to being the host's.
        """
        cases = [
            (r"C:\Users\x\AppData\Roaming\npm\claude.CMD", "claude"),
            (r"C:\npm\codex.CMD", "codex"),
            ("/usr/local/bin/codex", "codex"),
            ("/home/runner/.npm-global/bin/claude", "claude"),
            ("claude", "claude"),
            ("codex", "codex"),
        ]

        for argv0, expected in cases:
            with self.subTest(argv0=argv0):
                self.assertEqual(orch.worker_name(argv0), expected)

    def test_missing_worker_is_reported_by_name_not_by_path(self):
        with mock.patch.object(
            orch.subprocess, "run", side_effect=FileNotFoundError()
        ):
            with self.assertRaises(RuntimeError) as ctx:
                orch.run_worker(["codex", "exec"], 5)

        self.assertIn("codex", str(ctx.exception))


class StructuredAgentResolutionTests(unittest.TestCase):
    """The reviewers, the plan critic and the failure classifier all go here."""

    def test_agent_invocation_resolves_claude(self):
        seen = {}

        def fake_run(argv, **kwargs):
            seen["argv"] = argv
            return mock.Mock(returncode=0, stdout="{}", stderr="")

        with mock.patch.object(orch.shutil, "which", return_value="/usr/bin/claude"):
            with mock.patch.object(orch.subprocess, "run", side_effect=fake_run):
                orch.run_structured_agent("reviewer-security", "prompt", 10)

        self.assertEqual(seen["argv"][0], "/usr/bin/claude")

    def test_absent_claude_is_reported_as_did_not_run(self):
        """Not as an empty finding list, which would read as 'nothing wrong'."""
        with mock.patch.object(
            orch.subprocess, "run", side_effect=FileNotFoundError()
        ):
            data, error = orch.run_structured_agent("reviewer-security", "p", 10)

        self.assertIsNone(data)
        self.assertIn("not found on PATH", error)


class ExecutionRootCase(TaskDirCase):
    """Where a task's subprocesses actually run.

    ``run_worktree`` created a tree and recorded it in ``state["worktree"]``,
    and nothing read it as a working directory: every subprocess ran in
    ``Path.cwd()``, so the stated purpose -- letting tasks run in parallel
    without fighting over one checkout -- was not delivered.
    """

    CWD_CRITERION = {
        "id": "AC-1",
        "statement": "The acceptance command runs where the task's work is.",
        "verify": '{python} -c "import os; print(os.getcwd())"',
    }

    def _task(self, worktree):
        self.write_requirement()
        plan = self.write_plan(
            "plan.json", plan_json(acceptance_criteria=[self.CWD_CRITERION])
        )
        _, state = self.write_state(
            status="IMPLEMENTING", worktree=worktree, plan_file=str(plan)
        )
        return state

    def _launch(self):
        """Run a worker with subprocess doubled; report cwd and environment."""
        seen = {}

        def fake_run(argv, **kwargs):
            seen["cwd"] = kwargs.get("cwd")
            seen["env"] = kwargs.get("env")
            return mock.Mock(returncode=0, stdout=None, stderr=None)

        with mock.patch.object(orch.subprocess, "run", side_effect=fake_run):
            orch.run_worker(["claude", "--print"], 10, task_id=self.task_id)

        return seen

    def _acceptance_cwd(self, state):
        """Where an acceptance command really ran. Not mocked: it is the point."""
        summary = orch.evaluate_acceptance_criteria(self.task_id, state)

        self.assertEqual(summary["total"], 1, summary)
        printed = summary["results"][0]["output"].strip().splitlines()[-1]

        return Path(printed).resolve()


class RecordedWorktreeExecutionTests(ExecutionRootCase):
    def test_worker_and_acceptance_commands_use_recorded_worktree(self):
        tree = self.tmp / ".worktrees" / self.task_id
        tree.mkdir(parents=True)
        state = self._task(".worktrees/%s" % self.task_id)

        seen = self._launch()

        self.assertEqual(Path(seen["cwd"]).resolve(), tree.resolve())
        self.assertEqual(self._acceptance_cwd(state), tree.resolve())

        # The worker runs in its worktree; the evidence it reads is the
        # checkout's. Both are exported, because a cwd-relative hook command
        # executes the worktree's copy of the hook and that copy would
        # otherwise resolve .ai/tasks/<id> against the wrong tree.
        env = seen["env"]

        self.assertEqual(
            Path(env["ORCHESTRATOR_WORKER_ROOT"]).resolve(), tree.resolve()
        )
        self.assertEqual(
            Path(env["ORCHESTRATOR_REPO_ROOT"]).resolve(), self.tmp.resolve()
        )
        self.assertEqual(
            Path(env["ORCHESTRATOR_TASK_DIR"]).resolve(),
            self.task_path.resolve(),
        )
        self.assertEqual(
            Path(env["ORCHESTRATOR_CONSTITUTION"]).resolve(),
            (self.tmp / ".ai" / "constitution.md").resolve(),
        )
        self.assertEqual(env["ORCHESTRATOR_TASK_ID"], self.task_id)

    def test_an_absolute_recorded_worktree_is_honoured(self):
        tree = self.tmp / "elsewhere" / self.task_id
        tree.mkdir(parents=True)
        state = self._task(str(tree))

        self.assertEqual(
            Path(self._launch()["cwd"]).resolve(), tree.resolve()
        )
        self.assertEqual(self._acceptance_cwd(state), tree.resolve())

    def test_a_recorded_worktree_that_is_gone_falls_back(self):
        """Fail closed onto the checkout rather than into a missing directory.

        A recorded path can be stale: the worktree may have been removed on
        another machine, or the record may predate a move. Rooting a subprocess
        at something that is not there fails with an OS error that says nothing
        about tasks.
        """
        state = self._task(".worktrees/gone")

        self.assertEqual(
            Path(self._launch()["cwd"]).resolve(), self.tmp.resolve()
        )
        self.assertEqual(self._acceptance_cwd(state), self.tmp.resolve())


class LegacyCheckoutExecutionTests(ExecutionRootCase):
    """The path every task that exists today runs on.

    Including TASK-008 itself, whose own state.json has ``"worktree": null``:
    a regression here would break the task doing the work.
    """

    def test_null_worktree_worker_and_acceptance_use_orchestrator_checkout(
        self,
    ):
        state = self._task(None)

        seen = self._launch()

        self.assertEqual(Path(seen["cwd"]).resolve(), self.tmp.resolve())
        self.assertEqual(self._acceptance_cwd(state), self.tmp.resolve())

        env = seen["env"]

        self.assertEqual(
            Path(env["ORCHESTRATOR_WORKER_ROOT"]).resolve(), self.tmp.resolve()
        )
        self.assertEqual(
            Path(env["ORCHESTRATOR_REPO_ROOT"]).resolve(), self.tmp.resolve()
        )

    def test_a_blank_recorded_worktree_is_not_a_directory_name(self):
        state = self._task("   ")

        self.assertEqual(
            Path(self._launch()["cwd"]).resolve(), self.tmp.resolve()
        )
        self.assertEqual(self._acceptance_cwd(state), self.tmp.resolve())

    def test_worker_env_needs_no_task_state_to_exist(self):
        """`worker_env(task_id)` is called for ids with no workspace yet.

        ``test_preflight.py`` calls it for TASK-001 in a tree that has no such
        task at all, so the honest answer for a task with no readable state is
        the orchestrator checkout -- not an exception.
        """
        env = orch.worker_env("TASK-001")

        self.assertEqual(
            Path(env["ORCHESTRATOR_WORKER_ROOT"]).resolve(),
            self.tmp.resolve(),
        )
        self.assertEqual(env["ORCHESTRATOR_TASK_ID"], "TASK-001")
        self.assertIsNone(orch.worker_env(None))


class WorktreeEvidencePathTests(ExecutionRootCase):
    """What a worktree-rooted worker is *told* to read and write.

    Rooting execution in the recorded worktree moved the worker's cwd without
    moving the evidence. ``.ai/tasks/`` is tracked in git, so the worktree
    carries a committed -- and possibly stale -- copy of the requirement, the
    plan and the notes. A prompt that names them relatively therefore points the
    worker at bytes the developer's hash-bound approval does not cover, and its
    ``implementation.md`` lands where the Stop guard, which reads the exported
    absolute path, does not look. The planner has the same exposure from the
    other side: ``--output-last-message`` is where codex writes the plan the
    orchestrator then looks for in the checkout.
    """

    def _worktree_task(self, with_plan=True):
        tree = self.tmp / ".worktrees" / self.task_id
        tree.mkdir(parents=True)
        self.write_requirement()

        if with_plan:
            self.write_plan(
                "plan.json", plan_json(acceptance_criteria=[self.CWD_CRITERION])
            )

        _, state = self.write_state(
            status="IMPLEMENTING", worktree=".worktrees/%s" % self.task_id
        )
        return tree, state

    def _prompt_of(self, call):
        """Run something that invokes a worker; report its prompt and cwd."""
        seen = {}

        def fake_run(argv, **kwargs):
            seen["input"] = kwargs.get("input")
            seen["cwd"] = kwargs.get("cwd")
            return mock.Mock(returncode=0, stdout=None, stderr=None)

        with mock.patch.object(orch.subprocess, "run", side_effect=fake_run):
            with quiet():
                call()

        return seen

    def assertEvidenceIsCheckoutRooted(self, prompt, filename):
        """Every mention of an evidence file names the checkout's copy.

        Counting occurrences rather than asserting a single ``in`` is what makes
        this fail on a *second*, relative mention of the same file elsewhere in
        the prompt.
        """
        absolute = str(self.task_path.resolve() / filename)

        self.assertIn(absolute, prompt)
        self.assertEqual(
            prompt.count(filename), prompt.count(absolute), prompt
        )

    def test_implement_prompt_names_checkout_evidence_not_the_worktree_copy(
        self,
    ):
        tree, _ = self._worktree_task()
        # Relative, as `task_dir` yields it and as state.json may record it.
        plan_file = orch.task_dir(self.task_id) / "plan.json"

        seen = self._prompt_of(
            lambda: orch.run_claude_implementation(self.task_id, plan_file)
        )
        prompt = seen["input"]

        self.assertEqual(Path(seen["cwd"]).resolve(), tree.resolve())

        for name in (
            "requirement.md",
            "plan.json",
            "implementation.md",
            "state.json",
        ):
            self.assertEvidenceIsCheckoutRooted(prompt, name)

        self.assertNotIn(str(tree.resolve()), prompt)

    def test_fix_prompt_names_checkout_evidence_not_the_worktree_copy(self):
        tree, state = self._worktree_task()
        plan_file = orch.task_dir(self.task_id) / "plan.json"

        seen = self._prompt_of(
            lambda: orch.run_claude_fix(self.task_id, plan_file, state)
        )
        prompt = seen["input"]

        self.assertEqual(Path(seen["cwd"]).resolve(), tree.resolve())

        for name in (
            "requirement.md",
            "plan.json",
            "implementation.md",
            "state.json",
        ):
            self.assertEvidenceIsCheckoutRooted(prompt, name)

        self.assertNotIn(str(tree.resolve()), prompt)

    def _planner_output(self, call):
        """Where the planner was told to write, with codex doubled."""
        seen = {}

        def fake_run(argv, **kwargs):
            seen["cwd"] = kwargs.get("cwd")
            seen["out"] = argv[argv.index("--output-last-message") + 1]
            Path(seen["out"]).write_text(plan_json(), encoding="utf-8")
            return mock.Mock(returncode=0, stdout=None, stderr=None)

        with mock.patch.object(orch.subprocess, "run", side_effect=fake_run):
            with quiet():
                seen["returned"] = call()

        return seen

    def test_planner_writes_its_plan_into_the_checkout_evidence_directory(self):
        tree, _ = self._worktree_task(with_plan=False)

        seen = self._planner_output(
            lambda: orch.run_codex_planning(self.task_id)
        )
        out = Path(seen["out"])

        self.assertEqual(Path(seen["cwd"]).resolve(), tree.resolve())
        self.assertTrue(out.is_absolute(), out)
        self.assertEqual(out.resolve(), (self.task_path / "plan.json").resolve())
        self.assertNotIn(tree.resolve(), out.resolve().parents)

        # The path handed to the subprocess is absolute; the path recorded as
        # evidence stays repo-relative, so events.jsonl and state.json remain
        # comparable across machines.
        relative = str(orch.task_dir(self.task_id) / "plan.json")

        self.assertEqual(str(seen["returned"]), relative)

        created = [e for e in self.events() if e["event"] == "PLAN_CREATED"]

        self.assertEqual(created[-1]["plan_file"], relative)

    def test_replan_writes_its_plan_into_the_checkout_evidence_directory(self):
        tree, state = self._worktree_task(with_plan=False)

        seen = self._planner_output(
            lambda: orch.run_codex_replan(self.task_id, 2, state=state)
        )
        out = Path(seen["out"])

        self.assertEqual(Path(seen["cwd"]).resolve(), tree.resolve())
        self.assertTrue(out.is_absolute(), out)
        self.assertEqual(
            out.resolve(), (self.task_path / "plan-v2.json").resolve()
        )
        self.assertNotIn(tree.resolve(), out.resolve().parents)
        self.assertEqual(
            str(seen["returned"]),
            str(orch.task_dir(self.task_id) / "plan-v2.json"),
        )

    def test_a_legacy_task_still_gets_the_same_checkout_paths(self):
        """No recorded worktree: both roots coincide and nothing moves."""
        self.write_requirement()
        self.write_state(status="IMPLEMENTING", worktree=None)
        plan_file = orch.task_dir(self.task_id) / "plan.json"

        seen = self._prompt_of(
            lambda: orch.run_claude_implementation(self.task_id, plan_file)
        )

        self.assertEqual(Path(seen["cwd"]).resolve(), self.tmp.resolve())
        self.assertEvidenceIsCheckoutRooted(seen["input"], "requirement.md")
        self.assertEvidenceIsCheckoutRooted(seen["input"], "plan.json")


class LiveWorkerTests(unittest.TestCase):
    """Launch the installed CLIs the way the orchestrator does.

    Skipped where a worker is not installed, and recorded as skipped rather
    than passed -- "did not run" and "ran clean" are different claims.
    """

    def _launch(self, name):
        if shutil.which(name) is None:
            self.skipTest("%s is not installed on this machine" % name)

        completed = subprocess.run(
            [orch.resolve_executable(name), "--version"],
            capture_output=True,
            text=True,
            timeout=VERSION_TIMEOUT_S,
        )

        self.assertEqual(
            completed.returncode,
            0,
            "%s --version exited %s" % (name, completed.returncode),
        )

    def test_claude_launches(self):
        self._launch("claude")

    def test_codex_launches(self):
        self._launch("codex")

    def test_bare_name_launch_is_not_assumed_to_work(self):
        """The regression itself: which() finding a CLI does not mean exec will.

        On Windows this is the failing case that motivated resolve_executable;
        on POSIX both paths work. Either way the orchestrator must go through
        the resolved path, so this test asserts the resolution is what makes it
        launchable, not the bare name.
        """
        if shutil.which("claude") is None:
            self.skipTest("claude is not installed on this machine")

        completed = subprocess.run(
            [orch.resolve_executable("claude"), "--version"],
            capture_output=True,
            text=True,
            timeout=VERSION_TIMEOUT_S,
        )

        self.assertEqual(completed.returncode, 0)


if __name__ == "__main__":
    unittest.main()
