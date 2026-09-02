"""REPLANNING must not be a dead end.

`route_failure` set REPLANNING and bumped `plan_version` *before* invoking the
planner, and `run_codex_replan` raises when the worker exits non-zero rather
than returning None. So the caller's `plan_file is None` guard never ran, the
exception escaped to `main`, and the task sat at REPLANNING -- where `run`
reported "the run driver has no transition for it".

Observed on the first real replan, which died in 0.33s to the Windows
command-line limit. The limit was one cause; the dead end would have followed
from any worker crash in that window, and the state was recoverable the whole
time: `plan_file` still names the failing plan because it advances only on
success, and `plan_version` is already bumped.

This is the same shape as the `IMPLEMENTING` dead end that `fix` was added for.
Both came from a transition that wrote state before the work that justified it.
"""

import json
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


class ReplanResumeTests(TaskDirCase):
    def _parked(self, plan_version=2):
        """A task stranded exactly where the crash left one."""
        self.write_requirement()
        plan = self.write_plan("plan.json")
        _, state = self.write_state(
            status="REPLANNING",
            plan_version=plan_version,
            plan_file=str(plan),
        )
        return state

    def test_replan_is_reachable_from_replanning(self):
        self._parked()

        def fake_run(argv, **kwargs):
            # A replan now also invokes the plan critic, so the double has to
            # answer for both workers rather than assuming codex.
            if worker_name(argv) == "claude":
                return mock.Mock(
                    returncode=0,
                    stdout=json.dumps({"verdict": "pass", "issues": []}),
                    stderr="",
                )

            out = argv[argv.index("--output-last-message") + 1]
            orch.Path(out).write_text(plan_json(), encoding="utf-8")
            return mock.Mock(returncode=0, stdout="", stderr="")

        with mock.patch.object(orch.subprocess, "run", side_effect=fake_run):
            with quiet():
                code = orch.run_replan(self.task_id)

        self.assertEqual(code, 0)
        self.assertEqual(self.read_state()["status"], "AWAITING_APPROVAL")

    def test_the_revised_plan_becomes_the_plan_file(self):
        self._parked()

        def fake_run(argv, **kwargs):
            # A replan now also invokes the plan critic, so the double has to
            # answer for both workers rather than assuming codex.
            if worker_name(argv) == "claude":
                return mock.Mock(
                    returncode=0,
                    stdout=json.dumps({"verdict": "pass", "issues": []}),
                    stderr="",
                )

            out = argv[argv.index("--output-last-message") + 1]
            orch.Path(out).write_text(plan_json(), encoding="utf-8")
            return mock.Mock(returncode=0, stdout="", stderr="")

        with mock.patch.object(orch.subprocess, "run", side_effect=fake_run):
            with quiet():
                orch.run_replan(self.task_id)

        self.assertIn("plan-v2.json", self.read_state()["plan_file"])

    def test_a_worker_crash_lands_in_failed_not_replanning(self):
        """The regression: an escaping exception stranded the task."""
        self._parked()

        with mock.patch.object(
            orch.subprocess,
            "run",
            side_effect=orch.subprocess.CalledProcessError(1, ["codex"]),
        ):
            with quiet():
                code = orch.run_replan(self.task_id)

        self.assertEqual(code, 1)
        self.assertEqual(self.read_state()["status"], "FAILED")

    def test_a_worker_crash_records_the_cause(self):
        self._parked()

        with mock.patch.object(
            orch.subprocess,
            "run",
            side_effect=orch.subprocess.CalledProcessError(1, ["codex"]),
        ):
            with quiet():
                orch.run_replan(self.task_id)

        self.assertIn("replan-worker", self.read_state()["failure_reason"])
        self.assertTrue(
            [e for e in self.events() if e["event"] == "REPLAN_FAILED"]
        )

    def test_a_missing_executable_is_also_caught(self):
        """run_worker turns FileNotFoundError into RuntimeError."""
        self._parked()

        with mock.patch.object(
            orch.subprocess, "run", side_effect=FileNotFoundError()
        ):
            with quiet():
                orch.run_replan(self.task_id)

        self.assertEqual(self.read_state()["status"], "FAILED")

    def test_replan_refuses_a_task_that_is_not_replanning(self):
        self.write_requirement()
        self.write_plan()
        self.write_state(status="AWAITING_APPROVAL")

        with quiet() as out:
            code = orch.run_replan(self.task_id)

        self.assertEqual(code, 1)
        self.assertIn("requires REPLANNING", out.getvalue())

    def test_repeated_replans_are_capped(self):
        """A replan loop must terminate without a developer watching it."""
        self._parked()

        for _ in range(orch.REPLAN_MAX_ATTEMPTS):
            orch.record_event(self.task_id, "REPLAN_ATTEMPTED", plan_version=2)

        with quiet() as out:
            code = orch.run_replan(self.task_id)

        self.assertEqual(code, 1)
        self.assertIn("replan limit", out.getvalue())

    def test_each_attempt_is_recorded(self):
        self._parked()

        with mock.patch.object(
            orch.subprocess, "run", side_effect=FileNotFoundError()
        ):
            with quiet():
                orch.run_replan(self.task_id)

        attempts = [
            e for e in self.events() if e["event"] == "REPLAN_ATTEMPTED"
        ]

        self.assertEqual(len(attempts), 1)


class DriverCoversReplanningTests(TaskDirCase):
    def test_the_driver_has_a_transition_for_replanning(self):
        """`run` used to stop dead here."""
        self.write_requirement()
        plan = self.write_plan("plan.json")
        self.write_state(
            status="REPLANNING", plan_version=2, plan_file=str(plan)
        )

        with mock.patch.object(orch, "run_replan", return_value=0) as replanned:
            with quiet():
                orch.run_driver(self.task_id)

        replanned.assert_called()

    def test_replanning_is_not_reported_as_having_no_transition(self):
        self.write_requirement()
        plan = self.write_plan("plan.json")
        self.write_state(
            status="REPLANNING", plan_version=2, plan_file=str(plan)
        )

        def advance(task_id):
            path = self.task_path / "state.json"
            state = json.loads(path.read_text(encoding="utf-8"))
            state["status"] = "AWAITING_APPROVAL"
            path.write_text(json.dumps(state), encoding="utf-8")
            return 0

        with mock.patch.object(orch, "run_replan", side_effect=advance):
            with quiet() as out:
                orch.run_driver(self.task_id)

        self.assertNotIn("no transition for it", out.getvalue())


class ReplanIsCritiquedTests(TaskDirCase):
    """A replanned plan must face the critic too.

    The critique loop lived only in `run_plan`, so a plan produced by a replan
    reached the human gate uncritiqued -- and a plan written in response to a
    failure is the one most worth criticising.

    TASK-007 paid for it. v1 drew six critic issues. v2 came from a replan,
    drew none, and arrived at the gate with an acceptance criterion naming a
    test class that appeared nowhere in the plan or the tree.
    """

    def _parked(self):
        self.write_requirement()
        plan = self.write_plan("plan.json")
        self.write_state(
            status="REPLANNING", plan_version=2, plan_file=str(plan)
        )

    def _run(self, critic_reply):
        seen = {"critic": 0}

        def fake_run(argv, **kwargs):
            if worker_name(argv) == "claude":
                seen["critic"] += 1
                return mock.Mock(
                    returncode=0, stdout=json.dumps(critic_reply), stderr=""
                )

            out = argv[argv.index("--output-last-message") + 1]
            orch.Path(out).write_text(plan_json(), encoding="utf-8")
            return mock.Mock(returncode=0, stdout="", stderr="")

        with mock.patch.object(orch.subprocess, "run", side_effect=fake_run):
            with quiet() as out:
                orch.run_replan(self.task_id)

        return seen, out.getvalue()

    def test_the_critic_runs_on_a_replan(self):
        seen, _ = self._run({"verdict": "pass", "issues": []})

        self.assertGreaterEqual(seen["critic"], 1)

    def test_the_critique_is_recorded(self):
        self._run({"verdict": "pass", "issues": []})

        critiques = [e for e in self.events() if e["event"] == "PLAN_CRITIQUED"]

        self.assertTrue(critiques)
        self.assertTrue(critiques[-1]["ran"])

    def test_an_objection_bounces_the_plan_back(self):
        """The critic's whole purpose: another round before the human sees it."""
        seen, _ = self._run(
            {
                "verdict": "revise",
                "issues": [
                    {"severity": "high", "issue": "AC-1 cannot fail"}
                ],
                "blocking": [{"severity": "high", "issue": "AC-1 cannot fail"}],
            }
        )

        self.assertGreaterEqual(seen["critic"], 2)

    def test_the_round_limit_still_terminates(self):
        _, output = self._run(
            {
                "verdict": "revise",
                "issues": [{"severity": "high", "issue": "no"}],
                "blocking": [{"severity": "high", "issue": "no"}],
            }
        )

        self.assertIn("still objects", output)
        self.assertEqual(self.read_state()["status"], "AWAITING_APPROVAL")

    def test_a_critic_that_cannot_run_does_not_block_the_replan(self):
        def fake_run(argv, **kwargs):
            if worker_name(argv) == "claude":
                raise FileNotFoundError()

            out = argv[argv.index("--output-last-message") + 1]
            orch.Path(out).write_text(plan_json(), encoding="utf-8")
            return mock.Mock(returncode=0, stdout="", stderr="")

        self._parked()

        with mock.patch.object(orch.subprocess, "run", side_effect=fake_run):
            with quiet():
                code = orch.run_replan(self.task_id)

        self.assertEqual(code, 0)
        self.assertEqual(self.read_state()["status"], "AWAITING_APPROVAL")

    def test_a_critic_that_cannot_run_is_recorded_as_not_run(self):
        """Never as a pass. Those are different claims."""

        def fake_run(argv, **kwargs):
            if worker_name(argv) == "claude":
                raise FileNotFoundError()

            out = argv[argv.index("--output-last-message") + 1]
            orch.Path(out).write_text(plan_json(), encoding="utf-8")
            return mock.Mock(returncode=0, stdout="", stderr="")

        self._parked()

        with mock.patch.object(orch.subprocess, "run", side_effect=fake_run):
            with quiet():
                orch.run_replan(self.task_id)

        critiques = [e for e in self.events() if e["event"] == "PLAN_CRITIQUED"]

        self.assertTrue(critiques)
        self.assertFalse(critiques[-1]["ran"])

    def setUp(self):
        super().setUp()
        self._parked()


class CritiqueRoundTests(unittest.TestCase):
    """The shared helper, so planning and replanning cannot drift apart."""

    def test_both_paths_use_it(self):
        source = (orch.Path(".ai") / "scripts" / "orchestrator.py").read_text(
            encoding="utf-8"
        )

        self.assertEqual(source.count("critique_round("), 3)

    def test_a_critic_that_did_not_run_is_treated_as_acceptable(self):
        with mock.patch.object(
            orch, "critique_plan", return_value=(False, {"ran": False})
        ):
            with quiet():
                acceptable, ran, _ = orch.critique_round(
                    "TASK-001", orch.Path("plan.json"), {}, 1
                )

        self.assertTrue(acceptable)
        self.assertFalse(ran)


class ReplanDispatchTests(unittest.TestCase):
    def test_replan_is_a_dispatchable_verb(self):
        self.assertIn("replan", orch.ACTIONS)

    def test_usage_documents_replan(self):
        self.assertIn("replan", orch.USAGE)

    def test_replan_takes_the_task_lock(self):
        """It mutates state, so it must not race another invocation."""
        self.assertIs(orch.ACTIONS["replan"], orch.run_replan)


if __name__ == "__main__":
    unittest.main()
