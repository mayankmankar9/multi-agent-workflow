"""The task baseline and the delta computed against it.

The workflow could not previously tell a change a task produced from an
artifact that was already on disk before it started, and it had two ways to be
fooled:

- ``evaluate_acceptance_criteria`` ran ``verify`` and recorded the exit code. A
  criterion whose artifact already existed passed against an implementer that
  did nothing. TASK-007 v3 shipped two such criteria.
- ``evaluate_diff_scope`` sourced its change set from ``git diff base...HEAD``,
  which compares commits. The implementer never commits, so the set was empty
  on every task ever run: TASK-006's evidence records five declared files and
  ``"changed": []``, and the check passed.

Both are the same missing thing -- a record of what the tree looked like before
the task started. These tests are about that record and what it now proves.
"""

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from _support import (
    TaskDirCase,
    load_orchestrator,
    plan_json,
    quiet,
    worker_name,
)

orch = load_orchestrator()

HAVE_GIT = shutil.which("git") is not None


class BaselineCase(TaskDirCase):
    """A task workspace inside a real git repo.

    ``enumerate_tree`` shells out to ``git ls-files``, so the tree has to be a
    repository -- and the manifest deliberately honours ``.gitignore``, which
    cannot be tested without one.
    """

    def setUp(self):
        super().setUp()

        self._git("init", "-q", "-b", "main")
        self._git("config", "user.email", "t@example.com")
        self._git("config", "user.name", "T")
        (self.tmp / "base.txt").write_text("base\n")
        # Part of the starting tree, like the real .ai/validation.json: written
        # before the baseline, so it is pre-existing rather than task-produced.
        self._profile()
        self._git("add", "-A")
        self._git("commit", "-qm", "base")
        self._git("checkout", "-q", "-b", "feature/%s" % self.task_id)

    def _git(self, *args):
        subprocess.run(
            ["git"] + list(args),
            cwd=str(self.tmp),
            capture_output=True,
            check=True,
        )

    def _plan(self, name="plan.json", modify=(), create=(), criteria=None):
        body = {
            "files_to_modify": [{"path": p, "purpose": "x"} for p in modify],
            "files_to_create": [{"path": p, "purpose": "x"} for p in create],
        }

        if criteria is not None:
            body["acceptance_criteria"] = criteria

        return self.write_plan(name, plan_json(**body))

    def _profile(self, count=4):
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

    def _capture(self, plan_version=1):
        return orch.capture_baseline(
            self.task_id, {"plan_version": plan_version}
        )

    def _evidence(self):
        return json.loads((self.task_path / "test-results.json").read_text())


@unittest.skipUnless(HAVE_GIT, "git not available")
class CaptureTests(BaselineCase):
    def test_the_manifest_covers_the_working_tree(self):
        (self.tmp / "untracked.py").write_text("x = 1\n")

        baseline = self._capture()

        self.assertIn("base.txt", baseline["entries"])
        self.assertIn("untracked.py", baseline["entries"])

    def test_gitignored_files_are_not_in_the_manifest(self):
        (self.tmp / ".gitignore").write_text("secret.txt\n")
        (self.tmp / "secret.txt").write_text("shh\n")

        baseline = self._capture()

        self.assertNotIn("secret.txt", baseline["entries"])

    def test_task_evidence_is_not_in_the_manifest(self):
        """It churns constantly and is already exempt from scope enforcement."""
        baseline = self._capture()

        self.assertEqual(
            [p for p in baseline["entries"] if p.startswith(".ai/tasks/")], []
        )

    def test_capture_records_an_event_with_the_digest(self):
        baseline = self._capture()

        captures = [
            e for e in self.events() if e["event"] == "BASELINE_CAPTURED"
        ]

        self.assertEqual(len(captures), 1)
        self.assertEqual(
            captures[0]["baseline_sha256"], baseline["baseline_sha256"]
        )

    def test_the_baseline_verifies_after_capture(self):
        self._capture()

        self.assertIsInstance(orch.load_baseline(self.task_id), dict)

    def test_an_edited_baseline_is_refused(self):
        """Evidence that can be edited after the fact is not evidence."""
        self._capture()
        path = self.task_path / "baseline.json"
        payload = json.loads(path.read_text())
        payload["entries"].pop("base.txt")
        path.write_text(json.dumps(payload, indent=2))

        with self.assertRaises(RuntimeError) as caught:
            orch.load_baseline(self.task_id)

        self.assertIn("modified since capture", str(caught.exception))

    def test_a_baseline_with_no_capture_event_is_refused(self):
        self._capture()
        (self.task_path / "events.jsonl").write_text("")

        with self.assertRaises(RuntimeError) as caught:
            orch.load_baseline(self.task_id)

        self.assertIn("cannot be verified", str(caught.exception))

    def test_a_missing_baseline_names_what_is_missing(self):
        with self.assertRaises(RuntimeError) as caught:
            orch.load_baseline(self.task_id)

        self.assertIn("no task baseline", str(caught.exception))


@unittest.skipUnless(HAVE_GIT, "git not available")
class DeltaTests(BaselineCase):
    def test_a_new_file_is_created(self):
        baseline = self._capture()
        (self.tmp / "widget.py").write_text("w = 1\n")

        delta = orch.compute_task_delta(baseline)

        self.assertIn("widget.py", delta["created"])
        self.assertIn("widget.py", delta["produced"])

    def test_an_edited_file_is_modified(self):
        baseline = self._capture()
        (self.tmp / "base.txt").write_text("changed\n")

        delta = orch.compute_task_delta(baseline)

        self.assertIn("base.txt", delta["modified"])

    def test_a_removed_file_is_deleted(self):
        baseline = self._capture()
        (self.tmp / "base.txt").unlink()

        delta = orch.compute_task_delta(baseline)

        self.assertIn("base.txt", delta["deleted"])

    def test_an_untouched_tree_produces_nothing(self):
        baseline = self._capture()

        delta = orch.compute_task_delta(baseline)

        self.assertEqual(delta["produced"], [])

    def test_uncommitted_work_is_visible(self):
        """The whole point. A commit-based delta reports nothing here."""
        baseline = self._capture()
        (self.tmp / "widget.py").write_text("w = 1\n")

        delta = orch.compute_task_delta(baseline)
        committed = orch.changed_files_against_base(
            orch.resolve_diff_base() or "HEAD"
        )

        self.assertIn("widget.py", delta["produced"])
        self.assertEqual(committed, [])


@unittest.skipUnless(HAVE_GIT, "git not available")
class PreExistingArtifactTests(BaselineCase):
    """The TASK-007 failure: criteria satisfied by files already on disk."""

    def setUp(self):
        super().setUp()
        # The artifact exists before the task starts, exactly as
        # test_hook_resilience.py did when TASK-007 v3 declared it.
        (self.tmp / "widget.py").write_text("already here\n")
        self._capture()
        self.write_state(status="VALIDATING")

    def test_a_criterion_passing_on_a_pre_existing_file_is_unproven(self):
        self._plan(create=("widget.py",))

        with quiet():
            orch.run_validation(self.task_id)

        criteria = self._evidence()["acceptance_criteria"]
        result = criteria["results"][0]

        self.assertTrue(result["passed"])
        self.assertFalse(result["proven"])
        self.assertEqual(result["provenance"], "pre_existing")
        self.assertEqual(criteria["unproven"], 1)
        self.assertEqual(criteria["passed"], 0)

    def test_the_task_fails_even_though_the_command_exited_zero(self):
        self._plan(create=("widget.py",))

        with quiet():
            code = orch.run_validation(self.task_id)

        self.assertNotEqual(code, 0)
        self.assertEqual(self.read_state()["status"], "FAILED")

    def test_a_declared_creation_that_already_existed_is_named(self):
        self._plan(create=("widget.py",))

        with quiet():
            orch.run_validation(self.task_id)

        unproduced = self._evidence()["diff_scope"]["unproduced"]

        self.assertEqual([i["path"] for i in unproduced], ["widget.py"])
        self.assertIn("already existed", unproduced[0]["reason"])

    def test_the_failure_reason_says_which_check_blocked(self):
        self._plan(create=("widget.py",))

        with quiet():
            orch.run_validation(self.task_id)

        reason = self._evidence()["failure_reason"]

        self.assertIn("declared-not-produced", reason)
        self.assertIn("acceptance-criteria-provenance", reason)

    def test_a_declared_modification_that_did_not_change_is_named(self):
        self._plan(modify=("widget.py",))

        with quiet():
            orch.run_validation(self.task_id)

        unproduced = self._evidence()["diff_scope"]["unproduced"]

        self.assertEqual([i["path"] for i in unproduced], ["widget.py"])
        self.assertIn("byte-identical", unproduced[0]["reason"])

    def test_a_declared_creation_that_does_not_exist_is_named(self):
        self._plan(create=("absent.py",))

        with quiet():
            orch.run_validation(self.task_id)

        unproduced = self._evidence()["diff_scope"]["unproduced"]

        self.assertIn("does not exist", unproduced[0]["reason"])

    def test_a_declared_regression_guard_is_recorded_as_one(self):
        """An unchanged file is fine when the plan says so, and only then."""
        self._plan(
            modify=("base.txt",),
            criteria=[
                {
                    "id": "AC-1",
                    "statement": "the existing suite still passes",
                    "verify": "exit 0",
                    "material": False,
                }
            ],
        )
        (self.tmp / "base.txt").write_text("changed\n")

        with quiet():
            code = orch.run_validation(self.task_id)

        criteria = self._evidence()["acceptance_criteria"]

        self.assertEqual(code, 0)
        self.assertEqual(criteria["results"][0]["provenance"], "regression_guard")
        self.assertEqual(criteria["unproven"], 0)


@unittest.skipUnless(HAVE_GIT, "git not available")
class LegitimateChangeTests(BaselineCase):
    def setUp(self):
        super().setUp()
        self._capture()
        self.write_state(status="VALIDATING")

    def test_a_created_file_and_a_modified_file_both_validate(self):
        self._plan(modify=("base.txt",), create=("widget.py",))
        (self.tmp / "widget.py").write_text("w = 1\n")
        (self.tmp / "base.txt").write_text("changed\n")

        with quiet():
            code = orch.run_validation(self.task_id)

        self.assertEqual(code, 0)
        self.assertEqual(self.read_state()["status"], "VALIDATED")

    def test_the_criterion_is_recorded_as_task_produced(self):
        self._plan(create=("widget.py",))
        (self.tmp / "widget.py").write_text("w = 1\n")

        with quiet():
            orch.run_validation(self.task_id)

        criteria = self._evidence()["acceptance_criteria"]

        self.assertEqual(criteria["results"][0]["provenance"], "task_produced")
        self.assertEqual(criteria["passed"], 1)
        self.assertEqual(criteria["unproven"], 0)

    def test_the_delta_is_recorded_as_evidence(self):
        self._plan(create=("widget.py",))
        (self.tmp / "widget.py").write_text("w = 1\n")

        with quiet():
            orch.run_validation(self.task_id)

        delta = self._evidence()["task_delta"]

        self.assertEqual(delta["created"], ["widget.py"])
        self.assertEqual(delta["modified"], [])

    def test_depends_on_narrows_what_counts_as_evidence(self):
        """A criterion about one file is not proven by touching another."""
        self._plan(
            modify=("base.txt",),
            create=("widget.py",),
            criteria=[
                {
                    "id": "AC-1",
                    "statement": "widget.py holds",
                    "verify": "exit 0",
                    "depends_on": ["widget.py"],
                }
            ],
        )
        (self.tmp / "base.txt").write_text("changed\n")

        with quiet():
            orch.run_validation(self.task_id)

        criteria = self._evidence()["acceptance_criteria"]

        self.assertEqual(criteria["results"][0]["provenance"], "pre_existing")

    def test_windows_separators_in_a_declared_path_still_match(self):
        self._plan(create=("sub\\widget.py",))
        (self.tmp / "sub").mkdir()
        (self.tmp / "sub" / "widget.py").write_text("w = 1\n")

        with quiet():
            orch.run_validation(self.task_id)

        scope = self._evidence()["diff_scope"]

        self.assertEqual(scope["unproduced"], [])
        self.assertEqual(scope["violations"], [])


@unittest.skipUnless(HAVE_GIT, "git not available")
class UndeclaredChangeTests(BaselineCase):
    def setUp(self):
        super().setUp()
        self._capture()
        self.write_state(status="VALIDATING")
        self._plan(create=("widget.py",))
        (self.tmp / "widget.py").write_text("w = 1\n")

    def test_an_undeclared_file_is_a_violation(self):
        (self.tmp / "sneaky.py").write_text("s = 1\n")

        with quiet():
            code = orch.run_validation(self.task_id)

        scope = self._evidence()["diff_scope"]

        self.assertNotEqual(code, 0)
        self.assertIn("sneaky.py", scope["violations"])
        self.assertNotIn("widget.py", scope["violations"])

    def test_an_undeclared_change_is_caught_while_uncommitted(self):
        """The regression this replaces: nothing was committed, so the old
        commit-based check saw an empty change set and passed."""
        (self.tmp / "sneaky.py").write_text("s = 1\n")

        with quiet():
            orch.run_validation(self.task_id)

        scope = self._evidence()["diff_scope"]

        self.assertEqual(scope.get("committed_changed"), [])
        self.assertTrue(scope["violations"])

    def test_an_undeclared_edit_to_an_existing_file_is_a_violation(self):
        (self.tmp / "base.txt").write_text("meddled\n")

        with quiet():
            orch.run_validation(self.task_id)

        self.assertIn("base.txt", self._evidence()["diff_scope"]["violations"])

    def test_task_evidence_is_never_a_violation(self):
        with quiet():
            orch.run_validation(self.task_id)

        scope = self._evidence()["diff_scope"]

        self.assertEqual(
            [v for v in scope["violations"] if v.startswith(".ai/")], []
        )


@unittest.skipUnless(HAVE_GIT, "git not available")
class BaselineSurvivesReplanTests(BaselineCase):
    """A replan must not re-capture.

    If it did, work the task already did under an earlier plan version would be
    relabelled as pre-existing -- the same false pass, reintroduced by the
    mechanism meant to close it.
    """

    def _bytes(self):
        return (self.task_path / "baseline.json").read_bytes()

    def test_a_second_capture_returns_the_first(self):
        first = self._capture(plan_version=1)
        before = self._bytes()

        (self.tmp / "widget.py").write_text("w = 1\n")
        second = self._capture(plan_version=4)

        self.assertEqual(second["baseline_sha256"], first["baseline_sha256"])
        self.assertEqual(self._bytes(), before)
        self.assertEqual(second["plan_version_at_capture"], 1)

    def test_only_one_capture_event_is_ever_recorded(self):
        self._capture()
        self._capture(plan_version=2)

        self.assertEqual(
            len([e for e in self.events() if e["event"] == "BASELINE_CAPTURED"]),
            1,
        )

    def test_work_from_an_earlier_attempt_stays_task_produced(self):
        baseline = self._capture(plan_version=1)
        (self.tmp / "widget.py").write_text("attempt one\n")

        # A replan bumps the version; the baseline is untouched, so the file
        # the first attempt wrote is still this task's own output.
        self._capture(plan_version=2)
        delta = orch.compute_task_delta(orch.load_baseline(self.task_id))

        self.assertEqual(baseline["plan_version_at_capture"], 1)
        self.assertIn("widget.py", delta["created"])

    def test_rejecting_a_plan_leaves_the_baseline_alone(self):
        self._capture()
        before = self._bytes()
        plan = self._plan()
        self.write_state(
            status="AWAITING_APPROVAL", plan_version=1, plan_file=str(plan)
        )

        with quiet():
            orch.reject_plan(self.task_id, "AC-1 cannot fail")

        self.assertEqual(self._bytes(), before)
        self.assertEqual(self.read_state()["plan_version"], 2)

    def test_replanning_leaves_the_baseline_alone(self):
        self._capture()
        before = self._bytes()
        self.write_requirement()
        plan = self._plan()
        self.write_state(
            status="REPLANNING", plan_version=2, plan_file=str(plan)
        )

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

        with mock.patch.object(orch.subprocess, "run", side_effect=fake_run):
            with quiet():
                orch.run_replan(self.task_id)

        self.assertEqual(self._bytes(), before)
        self.assertEqual(self.read_state()["status"], "AWAITING_APPROVAL")

    def test_the_critic_still_runs_on_that_replan(self):
        """The plan-critic-on-replan fix and the baseline are independent."""
        self._capture()
        self.write_requirement()
        plan = self._plan()
        self.write_state(
            status="REPLANNING", plan_version=2, plan_file=str(plan)
        )
        seen = {"critic": 0}

        def fake_run(argv, **kwargs):
            if worker_name(argv) == "claude":
                seen["critic"] += 1
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

        self.assertGreaterEqual(seen["critic"], 1)


class CapturePointTests(unittest.TestCase):
    """Where the baseline may be taken from, asserted against the source.

    A capture on any path other than the implement gate is the bug: a baseline
    taken after a worker ran counts that worker's output as pre-existing.
    """

    def test_capture_is_called_from_exactly_one_place(self):
        source = (orch.Path(".ai") / "scripts" / "orchestrator.py").read_text(
            encoding="utf-8"
        )

        # The definition plus one call site.
        self.assertEqual(source.count("capture_baseline("), 2)


@unittest.skipUnless(HAVE_GIT, "git not available")
class ValidationRequiresABaselineTests(BaselineCase):
    """No baseline is a blocking failure, never a silent pass."""

    def setUp(self):
        super().setUp()
        self.write_state(status="VALIDATING")
        self._plan(create=("widget.py",))
        (self.tmp / "widget.py").write_text("w = 1\n")

    def test_validation_without_a_baseline_fails(self):
        with quiet():
            code = orch.run_validation(self.task_id)

        self.assertNotEqual(code, 0)
        self.assertEqual(self.read_state()["status"], "FAILED")

    def test_the_missing_baseline_is_named_in_the_evidence(self):
        with quiet():
            orch.run_validation(self.task_id)

        evidence = self._evidence()

        self.assertIn("task-baseline", evidence["failure_reason"])
        self.assertIn("no task baseline", evidence["task_baseline_error"])
        self.assertIsNone(evidence["task_delta"])

    def test_provenance_is_unknown_rather_than_assumed(self):
        with quiet():
            orch.run_validation(self.task_id)

        criteria = self._evidence()["acceptance_criteria"]

        self.assertEqual(criteria["results"][0]["provenance"], "unknown")
        self.assertEqual(criteria["unproven"], 0)

    def test_scope_reports_why_it_could_not_check(self):
        with quiet():
            orch.run_validation(self.task_id)

        self.assertIn("skipped_reason", self._evidence()["diff_scope"])

    def test_a_tampered_baseline_blocks_validation(self):
        self._capture()
        path = self.task_path / "baseline.json"
        payload = json.loads(path.read_text())
        payload["entries"]["widget.py"] = {"sha256": "0" * 64, "size": 1}
        path.write_text(json.dumps(payload, indent=2))

        with quiet():
            code = orch.run_validation(self.task_id)

        self.assertNotEqual(code, 0)
        self.assertIn(
            "modified since capture", self._evidence()["task_baseline_error"]
        )

    def test_a_fix_refuses_to_run_without_a_baseline(self):
        self.write_requirement()
        plan = self._plan()
        self.write_state(
            status="IMPLEMENTING", plan_version=1, plan_file=str(plan)
        )
        orch.record_event(self.task_id, "CLAUDE_FIX_STARTED", worker="claude")
        orch.record_event(
            self.task_id,
            "PLAN_APPROVED",
            plan_version=1,
            plan_sha256=orch.sha256_of(plan),
        )

        with quiet() as out:
            code = orch.run_fix(self.task_id)

        self.assertNotEqual(code, 0)
        self.assertIn("no task baseline", out.getvalue())


@unittest.skipUnless(HAVE_GIT, "git not available")
class ImplementGateTests(BaselineCase):
    """The implement verb captures the baseline before the worker runs."""

    def setUp(self):
        super().setUp()
        self.write_requirement()
        self.plan = self._plan(create=("widget.py",))
        self.write_state(
            status="AWAITING_APPROVAL", plan_file=str(self.plan)
        )
        orch.record_event(
            self.task_id,
            "PLAN_APPROVED",
            plan_version=1,
            plan_sha256=orch.sha256_of(self.plan),
        )

    def _implement(self):
        def worker(task_id, plan_file):
            # The worker only ever sees a tree the baseline already describes.
            (self.tmp / "widget.py").write_text("w = 1\n")

        with mock.patch.object(
            orch, "run_claude_implementation", side_effect=worker
        ):
            with quiet() as out:
                code = orch.run_implementation(self.task_id)

        return code, out.getvalue()

    def test_the_baseline_exists_before_the_worker_writes(self):
        code, _ = self._implement()

        baseline = orch.load_baseline(self.task_id)

        self.assertEqual(code, 0)
        self.assertNotIn("widget.py", baseline["entries"])

    def test_the_state_records_the_baseline(self):
        self._implement()

        state = self.read_state()

        self.assertTrue(state["baseline_file"].endswith("baseline.json"))
        self.assertEqual(
            state["baseline_sha256"],
            orch.load_baseline(self.task_id)["baseline_sha256"],
        )

    def test_the_worker_output_is_task_produced(self):
        self._implement()

        delta = orch.compute_task_delta(orch.load_baseline(self.task_id))

        self.assertIn("widget.py", delta["created"])


class StartingStateNotesTests(TaskDirCase):
    """The same defect, caught one layer earlier -- at the plan gate."""

    def test_a_create_that_already_exists_is_flagged(self):
        (self.tmp / "widget.py").write_text("here\n")

        notes = orch.plan_starting_state_notes(
            {"files_to_create": [{"path": "widget.py", "purpose": "x"}]}
        )

        self.assertEqual(len(notes), 1)
        self.assertIn("already exists", notes[0])

    def test_a_modify_that_does_not_exist_is_flagged(self):
        notes = orch.plan_starting_state_notes(
            {"files_to_modify": [{"path": "absent.py", "purpose": "x"}]}
        )

        self.assertIn("does not exist", notes[0])

    def test_a_plan_that_matches_the_tree_is_quiet(self):
        (self.tmp / "base.txt").write_text("here\n")

        notes = orch.plan_starting_state_notes(
            {
                "files_to_modify": [{"path": "base.txt", "purpose": "x"}],
                "files_to_create": [{"path": "widget.py", "purpose": "x"}],
            }
        )

        self.assertEqual(notes, [])

    def test_an_unparseable_plan_is_not_a_second_error_path(self):
        (self.task_path / "plan.json").write_text("not json")

        self.assertEqual(
            orch.plan_starting_state_problems(self.task_path / "plan.json"), []
        )

    def test_approval_warns_about_the_mismatch(self):
        (self.tmp / "widget.py").write_text("here\n")
        plan = self.write_plan("plan.json")
        self.write_state(status="AWAITING_APPROVAL", plan_file=str(plan))

        with quiet() as out:
            orch.approve_plan(self.task_id)

        self.assertIn("already exists", out.getvalue())


class CriterionContractTests(unittest.TestCase):
    def test_the_schema_documents_provenance_fields(self):
        schema = json.loads(
            (orch.Path(".ai") / "schemas" / "plan.schema.json").read_text(
                encoding="utf-8"
            )
        )
        criterion = schema["$defs"]["acceptanceCriterion"]["properties"]

        self.assertIn("depends_on", criterion)
        self.assertIn("material", criterion)

    def test_depends_on_must_be_a_list_of_paths(self):
        plan = json.loads(plan_json())
        plan["acceptance_criteria"][0]["depends_on"] = "widget.py"

        self.assertTrue(
            any("depends_on" in p for p in orch.validate_plan(plan))
        )

    def test_material_must_be_a_boolean(self):
        plan = json.loads(plan_json())
        plan["acceptance_criteria"][0]["material"] = "no"

        self.assertTrue(
            any("material" in p for p in orch.validate_plan(plan))
        )

    def test_the_provenance_fields_are_optional(self):
        self.assertEqual(orch.validate_plan(json.loads(plan_json())), [])


class ProtectedEvidenceTests(unittest.TestCase):
    """A worker that could edit the baseline could rewrite its own history."""

    def test_the_hook_denies_writing_a_baseline(self):
        payload = {
            "tool_name": "Write",
            "tool_input": {
                "file_path": ".ai/tasks/TASK-007/baseline.json",
                "content": "{}",
            },
        }
        result = subprocess.run(
            [orch.interpreter(), str(Path(".ai") / "hooks" / "pre_tool_use.py")],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            env={**os.environ, "ORCHESTRATOR_TASK_ID": "TASK-007"},
        )

        self.assertEqual(result.returncode, 2)
        self.assertIn("orchestrator-owned", result.stderr)


if __name__ == "__main__":
    unittest.main()
