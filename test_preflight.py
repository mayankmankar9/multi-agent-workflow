"""Preflight: can this machine actually run the workflow?

Every precondition here was, at some point, assumed rather than checked -- and
each assumption was wrong on the developer's own machine:

- both worker CLIs were installed, on PATH, and unlaunchable from Python;
- all four hooks were registered and silently denied nothing;
- the hook launcher's interpreter pin resolved to a different interpreter than
  the one it named.

None of those produced an error. They produced a green run that had not run
anything, which is the failure mode this repository exists to prevent. So the
preflight executes what it reports on: a CLI is launched, the launcher is run,
the schemas are parsed.
"""

import json
import os
import subprocess
import unittest
from pathlib import Path
from unittest import mock

from _support import REPO_ROOT, load_orchestrator, quiet

orch = load_orchestrator()

LAUNCHER = REPO_ROOT / ".ai" / "hooks" / "run"

# Probe programs are written without quote characters on purpose. MSYS re-parses
# the Windows command line with its own rules, so a single-quoted literal handed
# to Git Bash arrives with the quotes stripped: `print('ok')` becomes
# `print(ok)`, and the test fails for a reason that has nothing to do with the
# launcher.
PROBE_OK = "import sys; sys.exit(0)"


def run_launcher(args, env=None):
    """Run the hook launcher the way the harness does."""
    environment = dict(os.environ)
    environment.pop("ORCHESTRATOR_PYTHON", None)
    environment.update(env or {})

    return subprocess.run(
        [orch.resolve_bash(), LAUNCHER.as_posix()] + args,
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        env=environment,
        timeout=120,
    )


class ResolveBashTests(unittest.TestCase):
    def test_honours_the_harness_override(self):
        with mock.patch.dict(
            os.environ, {"CLAUDE_CODE_GIT_BASH_PATH": str(LAUNCHER)}
        ):
            self.assertEqual(orch.resolve_bash(), str(LAUNCHER))

    def test_ignores_an_override_that_does_not_exist(self):
        with mock.patch.dict(
            os.environ, {"CLAUDE_CODE_GIT_BASH_PATH": "/nope/bash"}
        ):
            self.assertNotEqual(orch.resolve_bash(), "/nope/bash")

    def test_resolves_to_a_real_file(self):
        self.assertTrue(Path(orch.resolve_bash()).is_file())

    @unittest.skipUnless(os.name == "nt", "Windows-only WSL ambiguity")
    def test_does_not_pick_the_wsl_launcher(self):
        """System32\\bash.exe is WSL: another filesystem, and env= is dropped.

        Picking it would make the preflight validate a shell the hooks never
        run under -- and it passed that way, which is worse than failing.
        """
        chosen = orch.resolve_bash().replace("\\", "/").lower()

        self.assertNotIn("system32", chosen)
        self.assertNotIn("windowsapps", chosen)


class HookLauncherTests(unittest.TestCase):
    def test_prefers_the_orchestrator_interpreter(self):
        import sys

        result = run_launcher(
            ["-c", "import sys; sys.stdout.write(sys.executable)"],
            env={"ORCHESTRATOR_PYTHON": Path(sys.executable).as_posix()},
        )

        self.assertEqual(result.returncode, 0)
        self.assertEqual(
            Path(result.stdout.strip()).resolve(), Path(sys.executable).resolve()
        )

    def test_falls_back_when_the_pin_is_unusable(self):
        """A named interpreter that will not run must not be trusted."""
        result = run_launcher(
            ["-c", PROBE_OK],
            env={"ORCHESTRATOR_PYTHON": "/nonexistent/python"},
        )

        self.assertEqual(result.returncode, 0, result.stderr)

    def test_reports_a_missing_script_argument(self):
        result = run_launcher([])

        self.assertEqual(result.returncode, 2)
        self.assertIn("no hook script", result.stderr)

    def test_denies_loudly_when_no_interpreter_exists(self):
        """Exit 2, not a silent pass: only 2 is a denial the harness honours."""
        result = run_launcher(
            ["-c", PROBE_OK],
            env={"ORCHESTRATOR_PYTHON": "", "PATH": ""},
        )

        self.assertEqual(result.returncode, 2)
        self.assertIn("no working Python interpreter", result.stderr)

    def test_passes_the_exit_code_through(self):
        result = run_launcher(["-c", "raise SystemExit(3)"])

        self.assertEqual(result.returncode, 3)


class CheckWorkerCliTests(unittest.TestCase):
    def test_absent_cli_is_reported_as_not_on_path(self):
        with mock.patch.object(orch.shutil, "which", return_value=None):
            ok, detail = orch.check_worker_cli("codex")

        self.assertFalse(ok)
        self.assertIn("not on PATH", detail)

    def test_present_but_unlaunchable_is_not_a_pass(self):
        """The Windows .CMD case: found by which(), unlaunchable by exec."""
        with mock.patch.object(orch.shutil, "which", return_value="/x/codex"):
            with mock.patch.object(
                orch.subprocess, "run", side_effect=FileNotFoundError("nope")
            ):
                ok, detail = orch.check_worker_cli("codex")

        self.assertFalse(ok)
        self.assertIn("would not start", detail)

    def test_nonzero_version_exit_is_not_a_pass(self):
        with mock.patch.object(orch.shutil, "which", return_value="/x/codex"):
            with mock.patch.object(
                orch.subprocess,
                "run",
                return_value=mock.Mock(returncode=1, stdout="", stderr=""),
            ):
                ok, _ = orch.check_worker_cli("codex")

        self.assertFalse(ok)

    def test_launchable_cli_passes_and_reports_its_version(self):
        with mock.patch.object(orch.shutil, "which", return_value="/x/codex"):
            with mock.patch.object(
                orch.subprocess,
                "run",
                return_value=mock.Mock(
                    returncode=0, stdout="codex-cli 1.2.3\n", stderr=""
                ),
            ):
                ok, detail = orch.check_worker_cli("codex")

        self.assertTrue(ok)
        self.assertIn("codex-cli 1.2.3", detail)


class CheckHookInterpreterTests(unittest.TestCase):
    def test_reports_the_interpreter_it_would_use(self):
        ok, detail = orch.check_hook_interpreter()

        self.assertTrue(ok, detail)
        self.assertTrue(detail)

    def test_missing_launcher_is_a_failure(self):
        with mock.patch.object(orch, "HOOK_LAUNCHER", Path("nope/run")):
            ok, detail = orch.check_hook_interpreter()

        self.assertFalse(ok)
        self.assertIn("missing", detail)


class PreflightChecksTests(unittest.TestCase):
    """Structure of the check list, without launching anything.

    These assertions are about which preconditions are covered and which are
    required -- not about this machine. Letting them run the real probes meant
    four separate calls, each launching claude, codex and gh against a 60s cap;
    twelve node startups that say nothing the mocked version does not, and that
    fail on a loaded machine. `CheckHookInterpreterTests` and
    `LiveWorkerTests` do the real launching, once.
    """

    @classmethod
    def setUpClass(cls):
        cls._patches = [
            mock.patch.object(
                orch, "check_worker_cli", return_value=(True, "stub 1.0")
            ),
            mock.patch.object(
                orch, "check_hook_interpreter", return_value=(True, "/x/python")
            ),
        ]

        for patch in cls._patches:
            patch.start()

        cls.results = orch.preflight_checks()

    @classmethod
    def tearDownClass(cls):
        for patch in cls._patches:
            patch.stop()

    def test_every_result_is_a_four_tuple(self):
        for entry in self.results:
            self.assertEqual(len(entry), 4)

    def test_covers_the_preconditions_a_real_run_needs(self):
        names = [name for name, _, _, _ in self.results]

        for expected in (
            "hook-interpreter",
            "worker: claude",
            "worker: codex",
            "git work tree",
            "agent definitions",
            "hook scripts",
            "hook registration",
        ):
            self.assertIn(expected, names)

    def test_gh_is_advisory_not_required(self):
        """gh is only needed by `pr` and the CI gate, so it must not block."""
        required = {name: req for name, req, _, _ in self.results}

        self.assertFalse(required["gh (pr, ci status)"])

    def test_worker_clis_are_required(self):
        required = {name: req for name, req, _, _ in self.results}

        self.assertTrue(required["worker: claude"])
        self.assertTrue(required["worker: codex"])

    def test_every_required_agent_definition_is_checked(self):
        """A missing checker definition means a checker with write tools."""
        for name in orch.REQUIRED_AGENTS:
            self.assertTrue(
                (REPO_ROOT / ".claude" / "agents" / f"{name}.md").is_file(),
                name,
            )

    def test_reviewer_dimensions_are_all_required_agents(self):
        for dimension in orch.REVIEW_DIMENSIONS:
            self.assertIn("reviewer-%s" % dimension, orch.REQUIRED_AGENTS)


class RunPreflightTests(unittest.TestCase):
    def test_passes_when_every_required_check_passes(self):
        fake = [("a", True, True, "ok"), ("b", False, False, "advisory")]

        with mock.patch.object(orch, "preflight_checks", return_value=fake):
            with quiet() as out:
                code = orch.run_preflight()

        self.assertEqual(code, 0)
        self.assertIn("advisory", out.getvalue())

    def test_fails_when_a_required_check_fails(self):
        fake = [("a", True, False, "broken")]

        with mock.patch.object(orch, "preflight_checks", return_value=fake):
            with quiet() as out:
                code = orch.run_preflight()

        self.assertEqual(code, 1)
        self.assertIn("Not ready", out.getvalue())

    def test_an_advisory_failure_does_not_fail_the_run(self):
        fake = [("gh", False, False, "not on PATH")]

        with mock.patch.object(orch, "preflight_checks", return_value=fake):
            with quiet():
                self.assertEqual(orch.run_preflight(), 0)


class DispatchTests(unittest.TestCase):
    def test_preflight_is_reachable_without_a_task_id(self):
        with mock.patch.object(orch.sys, "argv", ["orchestrator.py", "preflight"]):
            with mock.patch.object(orch, "run_preflight", return_value=0) as ran:
                self.assertEqual(orch.main(), 0)

        ran.assert_called_once()

    def test_usage_documents_preflight(self):
        self.assertIn("preflight", orch.USAGE)

    def test_preflight_takes_no_lock(self):
        """It must stay runnable while a task is in flight."""
        self.assertNotIn("preflight", orch.ACTIONS)


class WorkerEnvTests(unittest.TestCase):
    def test_exports_the_interpreter_for_the_hook_launcher(self):
        env = orch.worker_env("TASK-001")

        self.assertIn("ORCHESTRATOR_PYTHON", env)

    def test_interpreter_is_a_posix_path(self):
        """bash cannot exec a Windows backslash path."""
        env = orch.worker_env("TASK-001")

        self.assertNotIn("\\", env["ORCHESTRATOR_PYTHON"])

    def test_exports_the_task_id_so_hooks_know_they_guard_a_worker(self):
        self.assertEqual(
            orch.worker_env("TASK-001")["ORCHESTRATOR_TASK_ID"], "TASK-001"
        )

    def test_no_task_means_the_child_inherits(self):
        self.assertIsNone(orch.worker_env(None))


class SettingsRegistrationTests(unittest.TestCase):
    def test_hooks_are_launched_through_the_resolver(self):
        """Not through a bare `python`, which is a stub on Windows.

        This is the regression: the hooks were registered as
        `python .ai/hooks/x.py` and denied nothing for their entire existence.
        """
        settings = json.loads(
            (REPO_ROOT / ".claude" / "settings.json").read_text()
        )

        for event in settings["hooks"].values():
            for entry in event:
                for hook in entry["hooks"]:
                    self.assertIn(".ai/hooks/run", hook["command"])

    def test_no_hook_invokes_a_bare_interpreter_name(self):
        settings = json.loads(
            (REPO_ROOT / ".claude" / "settings.json").read_text()
        )

        for event in settings["hooks"].values():
            for entry in event:
                for hook in entry["hooks"]:
                    first = hook["command"].split()[0]
                    self.assertNotIn(first, ("python", "python3", "py"))


if __name__ == "__main__":
    unittest.main()
