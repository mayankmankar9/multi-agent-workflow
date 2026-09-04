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

    def test_the_cap_still_holds_against_a_raised_budget(self):
        """Raising the budget moves the limit; it does not remove it."""
        self._parked()

        for _ in range(3):
            orch.record_event(self.task_id, "REPLAN_ATTEMPTED", plan_version=2)

        with mock.patch.dict(
            orch.os.environ, {orch.REPLAN_BUDGET_ENV: "3"}
        ):
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


class ReplanBudgetTests(TaskDirCase):
    """The cap counts a task's whole life, so it needs a documented raise.

    TASK-007 had three plans, all written before the task-baseline rules
    existed, and no budget left to write one that satisfied them. The cap was
    right to stop the loop and wrong as a dead end: a developer had no recorded
    way to extend it, and the only alternatives were editing events.jsonl by
    hand or raising the limit for every task forever.
    """

    def _parked(self):
        self.write_requirement()
        plan = self.write_plan("plan.json")
        self.write_state(
            status="REPLANNING", plan_version=2, plan_file=str(plan)
        )

    def _replan_with(self, value):
        self._parked()

        for _ in range(orch.REPLAN_MAX_ATTEMPTS):
            orch.record_event(self.task_id, "REPLAN_ATTEMPTED", plan_version=2)

        def fake_run(argv, **kwargs):
            if worker_name(argv) == "claude":
                return mock.Mock(
                    returncode=0,
                    stdout=json.dumps({"verdict": "pass", "issues": []}),
                    stderr="",
                )

            out = argv[argv.index("--output-last-message") + 1]
            orch.Path(out).write_text(plan_json(), encoding="utf-8")
            return mock.Mock(returncode=0, stdout="", stderr="")

        with mock.patch.dict(
            orch.os.environ, {orch.REPLAN_BUDGET_ENV: value}
        ):
            with mock.patch.object(
                orch.subprocess, "run", side_effect=fake_run
            ):
                with quiet() as out:
                    code = orch.run_replan(self.task_id)

        return code, out.getvalue()

    def test_the_default_is_unchanged_without_an_override(self):
        limit, override = orch.replan_budget()

        self.assertEqual(limit, orch.REPLAN_MAX_ATTEMPTS)
        self.assertIsNone(override)

    def test_an_override_raises_the_budget(self):
        with mock.patch.dict(
            orch.os.environ, {orch.REPLAN_BUDGET_ENV: "4"}
        ):
            limit, override = orch.replan_budget()

        self.assertEqual(limit, 4)
        self.assertEqual(override, "4")

    def test_a_raised_budget_lets_an_exhausted_task_replan(self):
        code, _ = self._replan_with("3")

        self.assertEqual(code, 0)
        self.assertEqual(self.read_state()["status"], "AWAITING_APPROVAL")

    def test_the_raise_is_recorded_in_the_task_trail(self):
        """Not only in the shell that ran it."""
        self._replan_with("3")

        raised = [
            e for e in self.events() if e["event"] == "REPLAN_BUDGET_OVERRIDDEN"
        ]

        self.assertEqual(len(raised), 1)
        self.assertEqual(raised[0]["limit"], 3)
        self.assertEqual(raised[0]["default"], orch.REPLAN_MAX_ATTEMPTS)
        self.assertEqual(raised[0]["attempts_used"], orch.REPLAN_MAX_ATTEMPTS)

    def test_the_raise_is_reported_to_the_developer(self):
        _, output = self._replan_with("3")

        self.assertIn("Replan budget raised to 3", output)

    def test_the_critic_still_runs_under_a_raised_budget(self):
        self._replan_with("3")

        critiques = [e for e in self.events() if e["event"] == "PLAN_CRITIQUED"]

        self.assertTrue(critiques)
        self.assertTrue(critiques[-1]["ran"])

    def test_a_non_numeric_override_is_refused(self):
        """Never silently fall back: that grants the opposite of the ask."""
        with mock.patch.dict(
            orch.os.environ, {orch.REPLAN_BUDGET_ENV: "lots"}
        ):
            with self.assertRaises(RuntimeError) as caught:
                orch.replan_budget()

        self.assertIn("must be an integer", str(caught.exception))

    def test_a_zero_override_is_refused(self):
        with mock.patch.dict(
            orch.os.environ, {orch.REPLAN_BUDGET_ENV: "0"}
        ):
            with self.assertRaises(RuntimeError):
                orch.replan_budget()

    def test_a_malformed_override_stops_the_replan(self):
        code, output = self._replan_with("lots")

        self.assertEqual(code, 1)
        self.assertIn("must be an integer", output)
        self.assertEqual(self.read_state()["status"], "REPLANNING")

    def test_no_attempt_is_consumed_when_the_override_is_malformed(self):
        code, _ = self._replan_with("lots")

        attempts = [
            e for e in self.events() if e["event"] == "REPLAN_ATTEMPTED"
        ]

        self.assertEqual(len(attempts), orch.REPLAN_MAX_ATTEMPTS)


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


class PersistedCritiqueTests(TaskDirCase):
    """A critique that is counted is not a critique that is recorded.

    ``critique_round`` recorded ``issues=len(...)`` and ``blocking=len(...)``
    and sent the text to stdout. So a developer at the approval gate was told
    two blocking issues existed with no way to read them -- observed on
    TASK-008's own plan v1, whose critique had to be recovered from a transient
    524 KB log.

    Driven through the ordinary planning and replanning control flow, not by
    calling ``persist_critique`` directly: what is under test is that the
    artifact exists by the time the developer is sent to read it.
    """

    HIGH = {
        "severity": "high",
        "issue": "AC-3 cannot fail: its verify command exits 0 regardless.",
        "suggestion": "Assert the refusal, not the happy path.",
    }
    LOW = {
        "severity": "low",
        "issue": "The objective repeats the requirement's first line.",
        "suggestion": "Say what changes, not what was asked.",
    }

    def _planning(self):
        self.write_requirement()
        self.write_state(status="PLANNING", plan_file=None)

    def _run_planning(self, reply):
        """Plan for real, with codex and the critic doubled."""

        def fake_run(argv, **kwargs):
            if worker_name(argv) == "claude":
                return mock.Mock(
                    returncode=0, stdout=json.dumps(reply), stderr=""
                )

            (self.task_path / "plan.json").write_text(
                plan_json(), encoding="utf-8"
            )
            return mock.Mock(returncode=0, stdout="", stderr="")

        with mock.patch.object(orch.subprocess, "run", side_effect=fake_run):
            with quiet() as out:
                code = orch.run_plan(self.task_id)

        return code, out.getvalue()

    def _read(self, path):
        return json.loads(path.read_text(encoding="utf-8"))

    def test_persisted_critique_contains_issue_text_and_severity(self):
        """Readable off disk, without consulting process output."""
        self._planning()

        reply = {
            "verdict": "pass",
            "issues": [self.LOW],
        }
        code, _ = self._run_planning(reply)

        self.assertEqual(code, 0)

        path = orch.critique_file(
            self.task_id, self.task_path / "plan.json", 1
        )
        self.assertTrue(path.is_file(), "no critique artifact at %s" % path)

        recorded = self._read(path)

        self.assertEqual(recorded["issues"], [self.LOW])
        self.assertEqual(recorded["issues"][0]["severity"], "low")
        self.assertIn("repeats the requirement", recorded["issues"][0]["issue"])
        self.assertEqual(recorded["verdict"], "pass")
        self.assertEqual(recorded["task_id"], self.task_id)
        self.assertTrue(recorded["ran"])

        # And the trail names the file, so the artifact is discoverable from
        # the evidence rather than only by construction.
        events = [
            e
            for e in self.events()
            if e["event"] == "PLAN_CRITIQUE_RECORDED"
        ]
        self.assertEqual(len(events), 1)
        self.assertEqual(orch.Path(events[-1]["critique_file"]), path)

    def test_rounds_exhausted_persists_current_blocking_critique_identity(
        self,
    ):
        """The artifact the developer is told to read before approving."""
        self._planning()

        reply = {
            "verdict": "revise",
            "issues": [self.HIGH, self.LOW],
            "blocking": [self.HIGH],
        }
        code, output = self._run_planning(reply)

        self.assertEqual(code, 0)
        self.assertIn("still objects", output)
        self.assertEqual(self.read_state()["status"], "AWAITING_APPROVAL")

        plan_file = self.task_path / "plan.json"
        final = orch.critique_file(
            self.task_id, plan_file, orch.CRITIC_MAX_ROUNDS
        )
        self.assertTrue(final.is_file(), "no final critique at %s" % final)

        recorded = self._read(final)

        # It is the current one, and it says so: round and plan version, not
        # just "a critique happened".
        self.assertTrue(recorded["rounds_exhausted"])
        self.assertFalse(recorded["acceptable"])
        self.assertEqual(recorded["round"], orch.CRITIC_MAX_ROUNDS)
        self.assertEqual(recorded["max_rounds"], orch.CRITIC_MAX_ROUNDS)
        self.assertEqual(recorded["plan_version"], 1)
        self.assertEqual(recorded["plan_stem"], "plan")
        self.assertEqual(recorded["verdict"], "revise")

        # The blocking issue text and both severities survived.
        self.assertEqual(recorded["blocking"], [self.HIGH])
        self.assertEqual(recorded["blocking_count"], 1)
        self.assertEqual(
            [issue["severity"] for issue in recorded["issues"]],
            ["high", "low"],
        )
        self.assertIn("cannot fail", recorded["blocking"][0]["issue"])
        self.assertEqual(
            recorded["blocking"][0]["suggestion"], self.HIGH["suggestion"]
        )

        # Every earlier round is on disk too, and none of them claims to be
        # the exhausted one.
        earlier = self._read(orch.critique_file(self.task_id, plan_file, 1))

        self.assertEqual(earlier["round"], 1)
        self.assertFalse(earlier["rounds_exhausted"])

    def test_artifact_name_distinguishes_plan_version_and_round(self):
        """A stale round must not be able to pass for the current one."""
        self._planning()

        reply = {
            "verdict": "revise",
            "issues": [self.HIGH],
            "blocking": [self.HIGH],
        }
        self._run_planning(reply)

        # A replan writes plan-v2.json, so its critique lands beside the
        # first plan's rather than on top of it.
        plan_v2 = self.task_path / "plan-v2.json"
        plan_v2.write_text(plan_json(), encoding="utf-8")
        _, state = orch.load_state(self.task_id)
        state["plan_version"] = 2

        with mock.patch.object(
            orch, "critique_plan", return_value=(False, {
                "ran": True,
                "verdict": "revise",
                "issues": [self.HIGH],
                "blocking": [self.HIGH],
            })
        ):
            with quiet():
                orch.critique_round(self.task_id, plan_v2, state, 1)

        names = sorted(
            path.name for path in self.task_path.glob("critique-*.json")
        )

        self.assertEqual(
            names,
            [
                "critique-plan-round-1.json",
                "critique-plan-round-2.json",
                "critique-plan-v2-round-1.json",
            ],
        )

        # The path is derivable from task id, plan stem and round -- which is
        # what lets a developer find the current artifact without reading the
        # implementation.
        derived = orch.critique_file(self.task_id, plan_v2, 1)

        self.assertEqual(derived.name, "critique-plan-v2-round-1.json")
        # Task-relative, like every other piece of evidence: the orchestrator
        # runs from the checkout, so this resolves into the task directory.
        self.assertEqual(derived.resolve().parent, self.task_path.resolve())

        # And the payload identifies itself, so a file copied out of the
        # directory is still attributable.
        current = self._read(derived)
        stale = self._read(
            orch.critique_file(self.task_id, self.task_path / "plan.json", 1)
        )

        self.assertEqual((current["plan_version"], current["round"]), (2, 1))
        self.assertEqual((stale["plan_version"], stale["round"]), (1, 1))
        self.assertNotEqual(current["plan_stem"], stale["plan_stem"])


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
