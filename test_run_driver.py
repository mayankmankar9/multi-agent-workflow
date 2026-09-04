"""Phase 1 - task creation (C8) and the `run` driver (audit 5.2).

Two gaps this closes. Task creation was entirely manual, so ``NEW`` was
unreachable by any code path and ``ANALYZING`` had no writer -- the audit calls
them decorative. And nine mechanical steps were driven by hand; the driver
collapses them into one idempotent command that stops only at a genuine human
decision.

The ``recover`` cases at the bottom close the last dead end in that machine.
``IMPLEMENTING`` had exactly one exit -- ``fix`` -- and ``fix`` needs a
CLAUDE_FIX_STARTED event only ``route-failure`` can emit from FAILED. An
``implement`` run killed between IMPLEMENTATION_STARTED and its outcome
therefore had no legal verb at all, and could only be moved by hand-editing
state.json.
"""

import json
import os
import re
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

from _support import (
    REPO_ROOT,
    TaskDirCase,
    load_orchestrator,
    plan_json,
    quiet,
    worker_name,
)

orch = load_orchestrator()

PLAN_BODY = plan_json()

ORCHESTRATOR = REPO_ROOT / ".ai" / "scripts" / "orchestrator.py"
STATE_SCHEMA = REPO_ROOT / ".ai" / "schemas" / "task-state.schema.json"

# Environment keys that would otherwise leak a real developer's assertion into
# a test's recovery. Cleared per call, so a refusal test cannot be rescued by
# whatever happened to be exported in the shell that ran the suite.
RECOVERY_ENV_KEYS = (
    orch.RECOVER_ASSERTER_ENV,
    orch.RECOVER_LOCK_OVERRIDE_ENV,
)


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
        self.init_git()

        with quiet():
            orch.approve_plan(self.task_id)

        passing = "\nRan 5 tests in 0.01s\n\nOK\n"
        # git is left real: the implement gate captures a baseline from the
        # working tree, and validation compares the tree back against it.
        real_run = orch.subprocess.run

        def fake_run(argv, **kwargs):
            if worker_name(argv) == "git":
                return real_run(argv, **kwargs)

            if worker_name(argv) == "claude":
                # A worker that does the work the plan declares.
                (self.tmp / "widget.py").write_text("w = 1\n")
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

    def test_interrupted_implementing_stops_and_names_recover(self):
        """The driver still refuses -- and now says which verb does the work.

        `run` deliberately does not self-heal: resuming an interrupted
        implementation on its own would be guessing at what the killed worker
        had finished. What it must not do is leave the developer with no verb,
        which is what "inspect the task before continuing" amounted to.

        Asserted as the command form rather than the word: "recover" already
        appears in orchestrator.py as "recoverable" and "recovered", so a
        substring check proves nothing.
        """
        self.write_state(status="IMPLEMENTING")
        orch.record_event(
            self.task_id, "IMPLEMENTATION_STARTED", worker="claude",
            mode="implement",
        )

        with quiet() as out:
            self.assertEqual(orch.run_driver(self.task_id), 1)

        output = out.getvalue()

        self.assertIn("orchestrator.py %s recover" % self.task_id, output)
        self.assertIn("will not resume it on its own", output)
        # It refused: the task is exactly where it was.
        self.assertEqual(self.read_state()["status"], "IMPLEMENTING")

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


class RecoverCase(TaskDirCase):
    """Fixtures for the `recover` verb.

    Every trail is built out of real ``record_event`` calls rather than a
    hand-written events.jsonl, so eligibility is tested against the event
    vocabulary the orchestrator actually emits.
    """

    def _interrupted(self, mode="implement"):
        """A task exactly where a killed `implement` run leaves one.

        IMPLEMENTATION_STARTED is on the trail, nothing terminated it, and
        there is no CLAUDE_FIX_STARTED -- so `implement`, `validate`,
        `route-failure` and `fix` all refuse it.
        """
        self.write_requirement()
        self.write_plan("plan.json", PLAN_BODY)
        self.write_state(status="IMPLEMENTING")
        orch.record_event(
            self.task_id,
            "IMPLEMENTATION_STARTED",
            worker="claude",
            mode=mode,
            branch="feature/%s" % self.task_id,
        )

    def _recover(self, reason="the worker process was killed", **env):
        """Run `recover` with a deliberately explicit environment."""
        environment = {key: value for key, value in env.items()}

        with mock.patch.dict(orch.os.environ, environment):
            for key in RECOVERY_ENV_KEYS:
                if key not in environment:
                    orch.os.environ.pop(key, None)

            with quiet() as out:
                code = orch.run_recover(self.task_id, reason)

        return code, out.getvalue()

    def _snapshot(self):
        """The bytes of the evidence a refusal must not touch."""
        events = self.task_path / "events.jsonl"

        return (
            (self.task_path / "state.json").read_bytes(),
            events.read_bytes() if events.is_file() else b"",
        )


class RecoverInterruptedImplementationTests(RecoverCase):
    def test_recover_moves_eligible_interruption_to_failed(self):
        """The mandated case: a routable state, reached by a verb."""
        self._interrupted()

        code, _ = self._recover()

        self.assertEqual(code, 0)

        state = self.read_state()
        self.assertEqual(state["status"], "FAILED")
        self.assertIn("interrupted-implementation", state["failure_reason"])

        # FAILED is routable, which is the whole point: route-failure accepts
        # it, so the task is no longer stranded.
        self.assertEqual(
            orch.recovery_eligibility(self.task_id, state)[0], False
        )

    def test_recovery_event_records_reason_and_developer(self):
        self._interrupted()

        code, _ = self._recover(
            reason="host rebooted mid-implementation",
            **{orch.RECOVER_ASSERTER_ENV: "mayank"},
        )

        self.assertEqual(code, 0)

        interrupted = [
            e
            for e in self.events()
            if e["event"] == "IMPLEMENTATION_INTERRUPTED"
        ]

        self.assertEqual(len(interrupted), 1)
        self.assertEqual(
            interrupted[0]["reason"], "host rebooted mid-implementation"
        )
        self.assertEqual(interrupted[0]["asserted_by"], "mayank")
        self.assertEqual(interrupted[0]["assertion"], "human")
        self.assertEqual(interrupted[0]["mode"], "implement")

        recovered = [e for e in self.events() if e["event"] == "TASK_RECOVERED"]
        self.assertEqual(recovered[-1]["from_state"], "IMPLEMENTING")
        self.assertEqual(recovered[-1]["to_state"], "FAILED")
        self.assertEqual(recovered[-1]["asserted_by"], "mayank")

    def test_recover_preserves_interrupted_work_bytes(self):
        """"Preserves all work on disk" has to be an assertion, not prose.

        A recovery that reset the tree, re-captured the baseline or checked
        anything out would satisfy every other criterion here while destroying
        the evidence the verb exists to keep.
        """
        self._interrupted()

        edited = self.tmp / "widget.py"
        edited.write_bytes(b"SENTINEL = 1  # half-finished\r\nx = 2\n")
        created = self.tmp / "new_module.py"
        created.write_bytes(b"\xef\xbb\xbfvalue = 'partial'\n")
        baseline_before = (self.task_path / "baseline.json")

        before = {
            path: path.read_bytes() for path in (edited, created)
        }

        code, _ = self._recover()

        self.assertEqual(code, 0)

        for path, content in before.items():
            self.assertEqual(path.read_bytes(), content, path.name)

        # And nothing re-captured a baseline behind the developer's back.
        self.assertFalse(baseline_before.is_file())
        self.assertFalse(
            [e for e in self.events() if e["event"] == "BASELINE_CAPTURED"]
        )

    def test_recover_needs_a_reason(self):
        self._interrupted()

        code, output = self._recover(reason="   ")

        self.assertEqual(code, 1)
        self.assertIn("needs a reason", output)
        self.assertEqual(self.read_state()["status"], "IMPLEMENTING")


class RecoverEligibilityTests(RecoverCase):
    """Three refusals, each on its own.

    Without them `recover` could be an unconditional status setter: it would
    accept a task in any state, one whose attempt already ended, or one already
    routed to `fix`, and every other criterion would still pass. That is the
    hand edit wearing a verb's clothes.
    """

    def test_recover_refuses_non_implementing_task(self):
        self._interrupted()
        self.write_state(status="AWAITING_APPROVAL")
        before = self._snapshot()

        code, output = self._recover()

        self.assertEqual(code, 1)
        self.assertIn("requires IMPLEMENTING", output)
        self.assertEqual(self._snapshot(), before)

    def test_recover_refuses_terminated_implementation(self):
        """The attempt ended. It was not interrupted."""
        self._interrupted()
        orch.record_event(
            self.task_id,
            "VALIDATION_STARTED",
            command="python -m unittest",
        )
        before = self._snapshot()

        code, output = self._recover()

        self.assertEqual(code, 1)
        self.assertIn("VALIDATION_STARTED", output)
        self.assertEqual(self._snapshot(), before)

    def test_recover_refuses_a_terminated_attempt_for_every_terminator(self):
        """Enumerated, so a missing terminator cannot quietly let one through."""
        for terminator in orch.IMPLEMENTATION_TERMINATING_EVENTS:
            with self.subTest(terminator=terminator):
                for name in ("events.jsonl", "state.json"):
                    path = self.task_path / name

                    if path.is_file():
                        path.unlink()

                self._interrupted()
                orch.record_event(self.task_id, terminator)

                code, output = self._recover()

                self.assertEqual(code, 1)
                self.assertIn(terminator, output)

    def test_recover_refuses_started_fix(self):
        """`fix` is that task's legal verb; `recover` must not duplicate it."""
        self._interrupted()
        orch.record_event(self.task_id, "CLAUDE_FIX_STARTED", worker="claude")
        before = self._snapshot()

        code, output = self._recover()

        self.assertEqual(code, 1)
        self.assertIn("CLAUDE_FIX_STARTED", output)
        self.assertEqual(self._snapshot(), before)

    def test_recover_refuses_an_implementing_task_with_no_attempt(self):
        """Fail closed: no recorded attempt means no interruption to establish."""
        self.write_requirement()
        self.write_plan("plan.json", PLAN_BODY)
        self.write_state(status="IMPLEMENTING")

        code, output = self._recover()

        self.assertEqual(code, 1)
        self.assertIn("no IMPLEMENTATION_STARTED", output)
        self.assertEqual(self.read_state()["status"], "IMPLEMENTING")


class RecoverLockSafetyTests(RecoverCase):
    """Recovery is fail-closed on the lock, and only a human opens it."""

    def _lock(self, body="pid=424242 at=2026-09-02T20:21:00+05:30\n"):
        path = self.task_path / ".lock"
        path.write_text(body, encoding="utf-8")
        return path

    def test_existing_lock_refuses_without_override(self):
        self._interrupted()
        lock = self._lock()
        before = self._snapshot()

        code, output = self._recover()

        self.assertEqual(code, 1)
        self.assertIn("locked", output)
        self.assertEqual(self.read_state()["status"], "IMPLEMENTING")
        # The lock is left alone: removing it would be the same unearned
        # judgement the refusal exists to avoid.
        self.assertTrue(lock.is_file())
        self.assertEqual(self._snapshot(), before)

    def test_dead_pid_lock_still_refuses_without_override(self):
        """A pid that is demonstrably gone is still not authorisation.

        The pid comes from a child this test ran to completion, so it is not a
        guess about liveness. And `os.kill` is booby-trapped for the duration:
        if the implementation probed it, this fails -- which is the point.
        `os.kill(pid, 0)` is unsafe on Windows, where Python maps a non-CTRL
        signal to TerminateProcess, so a "is it alive" probe can terminate a
        running orchestrator.
        """
        child = subprocess.Popen(
            [sys.executable, "-c", "pass"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        child.wait()
        dead_pid = child.pid

        self._interrupted()
        self._lock("pid=%d at=2026-09-02T20:21:00+05:30\n" % dead_pid)

        def refuse_to_probe(*args, **kwargs):
            raise AssertionError(
                "recovery probed process liveness; a pid must never authorise"
            )

        with mock.patch.object(orch.os, "kill", side_effect=refuse_to_probe):
            code, output = self._recover()

        self.assertEqual(code, 1)
        self.assertIn("locked", output)
        self.assertEqual(self.read_state()["status"], "IMPLEMENTING")

    def test_override_records_attributable_human_assertion(self):
        self._interrupted()
        lock = self._lock()

        code, _ = self._recover(
            **{
                orch.RECOVER_LOCK_OVERRIDE_ENV: "mayank",
                orch.RECOVER_ASSERTER_ENV: "mayank",
            }
        )

        self.assertEqual(code, 0)

        overrides = [
            e
            for e in self.events()
            if e["event"] == "RECOVERY_LOCK_OVERRIDDEN"
        ]

        self.assertEqual(len(overrides), 1)
        self.assertEqual(overrides[0]["asserted_by"], "mayank")
        self.assertTrue(overrides[0]["asserted_by"].strip())
        self.assertEqual(overrides[0]["assertion"], "human")
        # The pid is carried as detail, not as the reason it was allowed.
        self.assertIn("pid=", overrides[0]["lock_holder"])
        self.assertIn("advisory", overrides[0]["note"])
        # The stale lock is cleared, so the next verb can take one.
        self.assertFalse(lock.is_file())
        self.assertEqual(self.read_state()["status"], "FAILED")

    def test_an_empty_lock_is_still_a_lock(self):
        """A zero-byte lock is the interruption case, not a missing lock.

        ``task_lock`` creates the file with O_CREAT|O_EXCL and writes the pid
        line afterwards, so a process killed between the two leaves exactly
        this. Reading the *holder* as the flag meaning "a lock existed" made
        such a lock override itself: no RECOVERY_LOCK_OVERRIDDEN event, the
        human assertion unrecorded, and the file never removed -- so the next
        mutating verb, ``route-failure``, refused and the task was stranded
        again by the verb that exists to unstrand it.
        """
        self._interrupted()
        lock = self._lock("")

        code, output = self._recover()

        self.assertEqual(code, 1)
        self.assertIn("locked", output)
        self.assertTrue(lock.is_file())
        self.assertEqual(self.read_state()["status"], "IMPLEMENTING")

        code, _ = self._recover(
            **{
                orch.RECOVER_LOCK_OVERRIDE_ENV: "mayank",
                orch.RECOVER_ASSERTER_ENV: "mayank",
            }
        )

        self.assertEqual(code, 0)

        overrides = [
            e for e in self.events() if e["event"] == "RECOVERY_LOCK_OVERRIDDEN"
        ]

        self.assertEqual(len(overrides), 1)
        self.assertEqual(overrides[0]["asserted_by"], "mayank")
        self.assertEqual(overrides[0]["assertion"], "human")
        self.assertTrue(overrides[0]["lock_removed"])

        interrupted = [
            e
            for e in self.events()
            if e["event"] == "IMPLEMENTATION_INTERRUPTED"
        ]

        self.assertTrue(interrupted[-1]["lock_overridden"])
        # Removed, so route-failure -- the whole point of moving to FAILED --
        # can take the lock it needs.
        self.assertFalse(lock.is_file())

    def test_a_lock_that_cannot_be_removed_is_reported_not_swallowed(self):
        """Telling the developer it was removed is the failure mode here.

        The removal fails precisely when another process still holds the file
        open -- the case the override was wrong about -- and the next mutating
        verb then refuses on a lock the developer was told had gone.
        """
        self._interrupted()
        self._lock()

        def refuse(*args, **kwargs):
            raise PermissionError(13, "in use by another process")

        with mock.patch.object(orch.Path, "unlink", side_effect=refuse):
            code, output = self._recover(
                **{orch.RECOVER_LOCK_OVERRIDE_ENV: "mayank"}
            )

        self.assertEqual(code, 0)
        self.assertIn("could not be removed", output)
        self.assertNotIn("  Removed ", output)

        overrides = [
            e for e in self.events() if e["event"] == "RECOVERY_LOCK_OVERRIDDEN"
        ]

        self.assertFalse(overrides[0]["lock_removed"])

    def test_a_blank_override_is_not_an_assertion(self):
        self._interrupted()
        self._lock()

        code, output = self._recover(
            **{orch.RECOVER_LOCK_OVERRIDE_ENV: "   "}
        )

        self.assertEqual(code, 1)
        self.assertIn("locked", output)


class RecoverValidationRouteTests(RecoverCase):
    """A recovered task reaches VALIDATED only by validating.

    "Recovery emits no validation event" would not settle this. What settles it
    is driving the route afterwards and watching where VALIDATED comes from.
    """

    def _profile(self, count=3):
        stderr = "Ran %d tests in 0.01s\\n\\nOK\\n" % count
        (self.tmp / ".ai").mkdir(exist_ok=True)
        (self.tmp / ".ai" / "validation.json").write_text(
            json.dumps(
                {
                    "checks": [
                        {
                            "name": "tests",
                            "command": '{python} -c "import sys; '
                            "sys.stderr.write('%s')\"" % stderr,
                            "required": True,
                            "expect_test_count": True,
                        }
                    ]
                }
            )
        )

    def test_recovered_task_requires_validation_before_validated(self):
        self.write_requirement()
        self.write_plan("plan.json", PLAN_BODY)
        self.write_state(status="AWAITING_APPROVAL")

        with quiet():
            orch.approve_plan(self.task_id)

        self.write_state(status="IMPLEMENTING")
        orch.record_event(
            self.task_id, "IMPLEMENTATION_STARTED", worker="claude",
            mode="implement",
        )
        self.init_git()
        # The validation profile is fixture scaffolding, so it has to exist
        # before the baseline is captured: a file written afterwards counts as
        # task-produced and an undeclared .ai/validation.json would fail the
        # scope check for a reason that has nothing to do with recovery.
        self._profile()
        self.write_baseline()

        self.assertEqual(self._recover()[0], 0)
        self.assertEqual(self.read_state()["status"], "FAILED")

        # Routing cannot hand out VALIDATED either.
        with mock.patch.object(
            orch,
            "classify_failure_with_agent",
            return_value=("CLAUDE_FIX", {"source": "test"}),
        ):
            with quiet():
                self.assertEqual(orch.route_failure(self.task_id), 0)

        self.assertEqual(self.read_state()["status"], "IMPLEMENTING")
        self.assertNotEqual(self.read_state()["status"], "VALIDATED")

        def fake_run(argv, **kwargs):
            if worker_name(argv) == "claude":
                (self.tmp / "widget.py").write_text("w = 1\n")

            return mock.Mock(returncode=0, stdout="", stderr="")

        with mock.patch.object(
            orch, "current_branch", return_value="feature/TASK-999"
        ), mock.patch.object(orch.subprocess, "run", side_effect=fake_run):
            with quiet():
                self.assertEqual(orch.run_fix(self.task_id), 0)

        # Still not VALIDATED, and no validation verdict has been recorded.
        self.assertEqual(self.read_state()["status"], "VALIDATING")
        self.assertFalse(
            [e for e in self.events() if e["event"] == "VALIDATION"]
        )
        self.assertFalse((self.task_path / "test-results.json").is_file())

        with quiet():
            orch.run_validation(self.task_id)

        # VALIDATED arrived from validation, with evidence behind it.
        self.assertEqual(self.read_state()["status"], "VALIDATED")
        verdicts = [e for e in self.events() if e["event"] == "VALIDATION"]
        self.assertTrue(verdicts)
        self.assertTrue(verdicts[-1]["passed"])
        self.assertTrue((self.task_path / "test-results.json").is_file())


def validate_against_schema(instance, schema, path="state"):
    """A narrow standard-library validator for the task-state schema.

    Deliberately not a general JSON Schema implementation. It supports exactly
    the keywords this repository's schema uses and **raises** on any it does
    not recognise, so a schema that grows a construct cannot be silently
    treated as satisfied. Returns a list of problems.
    """
    supported = {
        "$schema",
        "title",
        "description",
        "type",
        "required",
        "properties",
        "enum",
        "pattern",
        "minimum",
        "additionalProperties",
    }
    unsupported = set(schema) - supported

    if unsupported:
        raise ValueError(
            "schema at %s uses unsupported keywords: %s"
            % (path, sorted(unsupported))
        )

    problems = []
    types = schema.get("type")

    if types is not None:
        expected = types if isinstance(types, list) else [types]
        checkers = {
            "object": dict,
            "string": str,
            "integer": int,
            "number": (int, float),
            "boolean": bool,
            "array": list,
        }

        unknown = [name for name in expected if name not in checkers
                   and name != "null"]

        if unknown:
            raise ValueError("schema at %s uses unknown types: %s"
                             % (path, unknown))

        def ok(name):
            if name == "null":
                return instance is None

            if name in ("integer", "number") and isinstance(instance, bool):
                return False

            return isinstance(instance, checkers[name])

        if not any(ok(name) for name in expected):
            problems.append("%s: expected %s, got %s"
                            % (path, expected, type(instance).__name__))
            return problems

    if "enum" in schema and instance not in schema["enum"]:
        problems.append("%s: %r is not one of %s"
                        % (path, instance, schema["enum"]))

    if "pattern" in schema and isinstance(instance, str):
        if not re.search(schema["pattern"], instance):
            problems.append("%s: %r does not match %s"
                            % (path, instance, schema["pattern"]))

    if "minimum" in schema and isinstance(instance, (int, float)):
        if instance < schema["minimum"]:
            problems.append("%s: %r is below %s"
                            % (path, instance, schema["minimum"]))

    if isinstance(instance, dict):
        properties = schema.get("properties") or {}

        for key in schema.get("required") or []:
            if key not in instance:
                problems.append("%s: required property %r is missing"
                                % (path, key))

        if schema.get("additionalProperties") is False:
            for key in sorted(set(instance) - set(properties)):
                problems.append("%s: property %r is not permitted"
                                % (path, key))

        for key, value in sorted(instance.items()):
            if key in properties:
                problems.extend(
                    validate_against_schema(
                        value, properties[key], "%s.%s" % (path, key)
                    )
                )

    return problems


class RecoverStateSchemaTests(RecoverCase):
    """Recovery metadata belongs in events.jsonl, and that is checked.

    ``task-state.schema.json`` sets ``additionalProperties: false``, so a
    ``recovered_at`` or ``recovered_by`` field in state.json would ship fine
    and fail state validation a week later. A documented decision that nothing
    checks is how that happens.
    """

    def test_post_recovery_state_validates_against_repository_schema(self):
        self._interrupted()

        self.assertEqual(
            self._recover(**{orch.RECOVER_ASSERTER_ENV: "mayank"})[0], 0
        )

        # The real schema in the repo, not a copy in the temp tree. A missing
        # or unparsable schema fails here rather than skipping.
        schema = json.loads(STATE_SCHEMA.read_text(encoding="utf-8"))
        state = self.read_state()

        self.assertIs(schema.get("additionalProperties"), False)
        self.assertEqual(validate_against_schema(state, schema), [])

        # And the attribution really is in the trail rather than in the state.
        self.assertNotIn("recovered_by", state)
        interrupted = [
            e
            for e in self.events()
            if e["event"] == "IMPLEMENTATION_INTERRUPTED"
        ]
        self.assertEqual(interrupted[-1]["asserted_by"], "mayank")

    def test_the_validator_rejects_an_undeclared_field(self):
        """Otherwise the check above could pass against anything."""
        schema = json.loads(STATE_SCHEMA.read_text(encoding="utf-8"))
        state = {
            "task_id": "TASK-999",
            "status": "FAILED",
            "created_at": "x",
            "updated_at": "y",
            "recovered_at": "z",
        }

        problems = validate_against_schema(state, schema)

        self.assertTrue(any("recovered_at" in p for p in problems), problems)

    def test_the_validator_refuses_an_unsupported_schema_construct(self):
        with self.assertRaises(ValueError):
            validate_against_schema({}, {"type": "object", "oneOf": []})


class RecoverCliDispatchTests(RecoverCase):
    """Drive the real CLI, in a subprocess, through real argument parsing.

    Every other criterion here calls a function this suite also authored. A
    unit test that patches internals passes happily while argument parsing,
    command registration or dispatch is broken -- the failure mode that hid
    four separate defects before `preflight` existed.
    """

    def _cli(self, *args, env=None):
        environment = dict(os.environ)

        for key in RECOVERY_ENV_KEYS:
            environment.pop(key, None)

        environment.update(env or {})

        return subprocess.run(
            [sys.executable, str(ORCHESTRATOR), self.task_id] + list(args),
            cwd=str(self.tmp),
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=environment,
            timeout=120,
        )

    def test_recover_dispatches_through_real_cli(self):
        self._interrupted()

        first = self._cli(
            "recover",
            "the implementer process was killed",
            env={orch.RECOVER_ASSERTER_ENV: "mayank"},
        )

        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        self.assertEqual(self.read_state()["status"], "FAILED")
        self.assertIn(
            "interrupted-implementation", self.read_state()["failure_reason"]
        )

        interrupted = [
            e
            for e in self.events()
            if e["event"] == "IMPLEMENTATION_INTERRUPTED"
        ]
        self.assertEqual(interrupted[-1]["asserted_by"], "mayank")

        # The same command a second time is now ineligible -- the task is
        # FAILED -- and the CLI must say so with a non-zero exit.
        second = self._cli("recover", "again")

        self.assertNotEqual(second.returncode, 0)
        self.assertIn("cannot be recovered", second.stdout + second.stderr)

    def test_recover_without_a_reason_exits_non_zero_through_the_cli(self):
        self._interrupted()

        result = self._cli("recover")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("needs a reason", result.stdout + result.stderr)
        self.assertEqual(self.read_state()["status"], "IMPLEMENTING")


def documented_verbs(text):
    """The verb list CLAUDE.md publishes, parsed out of its fenced block."""
    match = re.search(r"^## Verbs\s*\n+```\n(.*?)\n```", text,
                      re.MULTILINE | re.DOTALL)

    if match is None:
        raise ValueError("CLAUDE.md has no fenced verb list under '## Verbs'")

    return {
        token.strip()
        for token in match.group(1).replace("\n", " ").split("|")
        if token.strip()
    }


def documented_implementing_exits(text):
    """The exits from IMPLEMENTING that CLAUDE.md claims."""
    match = re.search(
        r"^- `IMPLEMENTING` exits: (.+?)\.\s", text, re.MULTILINE
    )

    if match is None:
        raise ValueError(
            "CLAUDE.md does not state which verbs exit IMPLEMENTING"
        )

    return set(re.findall(r"`([a-z-]+)`", match.group(1)))


class DocumentedLifecycleContractTests(TaskDirCase):
    """A documented contract that contradicts the code is a defect.

    Parsed rather than grepped: "recover" already appears in orchestrator.py as
    "recoverable" and "recovered", so a substring check is satisfied by an
    incidental mention. These compare a parsed contract against dispatch, and
    then drive the dispatch.
    """

    def test_documented_verbs_and_implementing_exits_match_dispatch(self):
        text = (REPO_ROOT / "CLAUDE.md").read_text(encoding="utf-8")

        self.assertEqual(documented_verbs(text), orch.dispatchable_verbs())

        exits = documented_implementing_exits(text)
        self.assertEqual(exits, set(orch.IMPLEMENTING_EXITS))

        for verb in exits:
            self.assertTrue(
                verb in orch.ACTIONS or verb in orch.TEXT_ACTIONS,
                "%s is documented as an exit but is not dispatchable" % verb,
            )

        # Now the behaviour, not the table: the documented exits are the verbs
        # whose status gate accepts IMPLEMENTING, and nothing else's does.
        self.write_requirement()
        self.write_plan("plan.json", PLAN_BODY)
        self.write_state(status="IMPLEMENTING")

        refusals = {}

        for verb, handler in (
            ("implement", orch.run_implementation),
            ("validate", orch.run_validation),
            ("replan", orch.run_replan),
            ("route-failure", orch.route_failure),
            ("approve", orch.approve_plan),
        ):
            with quiet() as out:
                code = handler(self.task_id)

            refusals[verb] = (code, out.getvalue())

        for verb, (code, output) in refusals.items():
            self.assertEqual(code, 1, verb)
            self.assertIn("is in state IMPLEMENTING", output, verb)

        # `fix` and `recover` get past the status gate and refuse for their own
        # reasons instead -- which is what "exits IMPLEMENTING" means.
        with quiet() as out:
            self.assertEqual(orch.run_fix(self.task_id), 1)

        self.assertIn("no CLAUDE_FIX_STARTED event", out.getvalue())

        with quiet() as out:
            self.assertEqual(orch.run_recover(self.task_id, "killed"), 1)

        self.assertIn("no IMPLEMENTATION_STARTED", out.getvalue())

    def test_the_verb_parser_fails_on_input_it_cannot_read(self):
        """So a reformatted or missing section fails rather than passing."""
        with self.assertRaises(ValueError):
            documented_verbs("# CLAUDE\n\nno verbs here\n")

        with self.assertRaises(ValueError):
            documented_implementing_exits("nothing about implementing\n")


class RecoverDispatchTests(unittest.TestCase):
    def test_recover_is_a_dispatchable_text_verb(self):
        self.assertIn("recover", orch.TEXT_ACTIONS)
        self.assertIs(orch.TEXT_ACTIONS["recover"], orch.run_recover)

    def test_recover_does_not_take_the_task_lock(self):
        """Its subject is a leftover lock; taking one would mask it."""
        self.assertIn("recover", orch.UNLOCKED_ACTIONS)
        self.assertNotIn("recover", orch.ACTIONS)

    def test_usage_documents_recover(self):
        self.assertIn("recover", orch.USAGE)


if __name__ == "__main__":
    unittest.main()
