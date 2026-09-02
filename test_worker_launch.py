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
from unittest import mock

from _support import TaskDirCase, load_orchestrator

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
